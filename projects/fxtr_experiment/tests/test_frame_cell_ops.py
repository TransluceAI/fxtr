"""Frame cell ops end to end (phase 5 chunk 3): engine write/read
semantics over real storage, frame task outputs through the executor
(results loop + the safety pass's canonicalized output comparison,
pinned by a task that permutes row order per invocation), the demand
path serving frames through the projection, per-output staleness on
frame ids, the user surface (validation, overwrite/adopt, sourcing
pinning the frame id, ``demand_table``), output-kind checks on both
read ops, projection rebuild across a session reopen, the at-rest
round trip, introspection non-breakage, and the projection LRU."""

from pathlib import Path
from typing import AsyncIterator

import polars as pl
import pytest

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope

from fxtr_experiment import encoding as enc
from fxtr_experiment_task import model as m
from fxtr_experiment_task import tables
from fxtr_experiment_task.errors import (
    CellFaultError,
    GridNotSourcedError,
    NondeterminismError,
    ReadWriteExclusionError,
    UngroundedSubjectError,
    ViewAxisError,
    WriteConflictError,
    typed_wire_call,
)
from fxtr_experiment.frame_encoding import encode_row, store_frame
from fxtr_experiment.state_store import FrameProjection, WorkflowSession
from fxtr_experiment.user_service import Workflow, open_local
from fxtr_experiment_task.views import DataFrameView

from .test_ops_engine import Harness, _some_entity
from .test_user_surface import FN_AXES, _bind, _fn_key, _paths

SCHEMA = (m.ColumnSpec("k", "str"), m.ColumnSpec("n", "int"))
NULLABLE_SCHEMA = (m.ColumnSpec("k", "str"), m.ColumnSpec("n", "int", nullable=True))
X_AXES = (m.AxisSpec("x", "str"),)

ROWS = [("b", 2), ("a", 1), ("c", 3)]
OTHER_ROWS = [("z", 9)]


def canonical(rows: list[tuple]) -> list[tuple]:
    """The spec's expected serving order: sorted by canonical row bytes."""
    return sorted(rows, key=lambda r: encode_row(SCHEMA, r))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[Harness]:
    async with WorkflowSession.open(
        str(tmp_path / "state.sqlite"), str(tmp_path / "entities.sqlite")
    ) as session:
        harness = Harness(session)
        await harness.seed()
        yield harness


# -- engine semantics over real storage ----------------------------------------------


@pytest.mark.anyio
async def test_engine_frame_write_read_semantics(h: Harness) -> None:
    """The write op's record-check rules on multiset identity, and the
    read op serving canonical order — driven through real views over the
    engine."""
    engine = await h.engine()
    root = h.root(engine)
    d = await root.open_dataframe("frames", axes=X_AXES, schema=SCHEMA)
    await d.write_table(tables.make_frame(SCHEMA, ROWS), x="a")
    # A permuted rewrite of the same multiset is an idempotent replay hit.
    await d.write_table(tables.make_frame(SCHEMA, list(reversed(ROWS))), x="a")
    # A different multiset against this generation's own record.
    with pytest.raises(NondeterminismError):
        await d.write_table(tables.make_frame(SCHEMA, OTHER_ROWS), x="a")
    # Reading back your own write is read-xor-write.
    with pytest.raises(ReadWriteExclusionError):
        await d.read_table(x="a")

    # A fresh generation of the driver: live conflict rules + the read path.
    await h.session.delete_generation(h.exec_id)
    await h.seed_regen()
    engine2 = await h.engine()
    root2 = h.root(engine2)
    d2 = await root2.open_dataframe("frames", axes=X_AXES, schema=SCHEMA)
    with pytest.raises(WriteConflictError):
        await d2.write_table(tables.make_frame(SCHEMA, OTHER_ROWS), x="a")
    # The same multiset, permuted, is an idempotent live write.
    await d2.write_table(tables.make_frame(SCHEMA, list(reversed(ROWS))), x="a")

    # A third generation reads: canonical order, canonical dtypes.
    await h.session.delete_generation(h.exec_id)
    await h.seed_regen()
    engine3 = await h.engine()
    root3 = h.root(engine3)
    d3 = await root3.open_dataframe("frames", axes=X_AXES, schema=SCHEMA)
    served = await d3.read_table(x="a")
    assert served.rows() == canonical(ROWS)
    assert served.schema == tables.polars_schema(SCHEMA)
    # Record hit serves identically (and still via bytes).
    assert (await d3.read_table(x="a")).rows() == canonical(ROWS)


@pytest.mark.anyio
async def test_engine_kind_checks_and_validation(h: Harness) -> None:
    """Wire-level guards: entity reads refuse dataframe grids, frame reads
    refuse entity targets, writes validate against the declared schema."""
    from fxtr_experiment_task.runner_api import ViewSel

    engine = await h.engine()
    root = h.root(engine)
    d = await root.open_dataframe("frames", axes=(), schema=SCHEMA)
    plain = await root.open_data("plain", axes=())
    d_token = d._shadow.grant.token  # type: ignore[attr-defined]  # wire-level test
    p_token = plain._shadow.grant.token  # type: ignore[attr-defined]
    with pytest.raises(ViewAxisError, match="read it as a frame"):
        await engine.read_cell_entity(ViewSel(token=d_token))
    with pytest.raises(ViewAxisError, match="does not address a frame value"):
        await engine.read_cell_frame(ViewSel(token=p_token))
    # Validation failures at the write boundary, wrapped as ViewAxisError.
    with pytest.raises(ViewAxisError, match="declared schema"):
        await d.write_table(pl.DataFrame({"k": ["a"], "wrong": [1]}))
    with pytest.raises(ViewAxisError, match="declared schema"):
        await d.write_table(
            pl.DataFrame({"k": ["a"], "n": [1]}, schema={"k": pl.String, "n": pl.Int32})
        )
    with pytest.raises(ViewAxisError, match="declared schema"):
        await d.write_table(tables.make_frame(SCHEMA, [(None, 1)]))  # null, non-nullable
    # Nullable column accepts its null (and round-trips it).
    dn = await root.open_dataframe("nullable", axes=(), schema=NULLABLE_SCHEMA)
    await dn.write_table(tables.make_frame(NULLABLE_SCHEMA, [("a", None)]))


# -- frame task outputs + the safety pass (executor e2e) -----------------------------


class PermutingFrameTask:
    """Writes a frame cell and returns a frame output, permuting the row
    order every invocation — the completion-order nondeterminism multiset
    identity exists to absorb. Without host-side canonicalization the
    safety-pass replay (invocation 2) would flag both the write
    (record-hit mismatch) and the output comparison."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, ctx, /, *, sink):
        self.calls += 1
        rows = list(ROWS) if self.calls % 2 else list(reversed(ROWS))
        await sink.write_table(tables.make_frame(SCHEMA, rows))
        return tables.make_frame(SCHEMA, rows)


class ConsumeFrameTask:
    """Reads an upstream frame output (a pre-selected rule binding) and
    reduces it to an entity — covering the demand path serving frames and
    the pre-selected-output schema resolution."""

    def __init__(self) -> None:
        self.calls = 0
        self.seen: list[list[tuple]] = []

    async def __call__(self, ctx, /, *, table):
        self.calls += 1
        df = await table.read_table()
        self.seen.append(df.rows())
        return _some_entity().store().entity_id


@pytest.mark.anyio
async def test_frame_outputs_and_safety_pass_end_to_end(tmp_path: Path) -> None:
    """The whole chain: a permuting producer completes (its safety replay
    emits different row order — canonicalized comparison passes), a
    consumer demands its frame output through the projection in canonical
    order, and an idempotent producer rerun keeps the consumer fresh
    (per-output staleness on the frame id)."""
    state, ents = _paths(tmp_path)
    producer, consumer = PermutingFrameTask(), ConsumeFrameTask()
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            await _bind(wf, host, fns, "produce", producer)
            await _bind(wf, host, fns, "consume", consumer)
            sink = await wf.root.open_dataframe("sink", (), SCHEMA)
            t_prod = await wf.root.open_task(
                "prod",
                axes=(),
                task_fn=fns.at(fn="produce"),
                args={"sink": sink},
                outputs={"table": m.FrameOutput(schema=SCHEMA)},
            )
            t_cons = await wf.root.open_task(
                "cons",
                axes=(),
                task_fn=fns.at(fn="consume"),
                args={"table": t_prod.output_frame("table")},
            )
            # Demand the consumer: the producer runs as its dependency.
            assert await wf.demand(t_cons.output_entity()) is not None
            assert producer.calls == 2  # main + safety replay, permuted order
            assert consumer.calls == 2
            assert consumer.seen == [canonical(ROWS)] * 2  # canonical serving
            # The cell write landed canonically too (user read, gated).
            assert (await sink.read_table()).rows() == canonical(ROWS)

            # demand_table on the producer's output: auto-targets + serves.
            df = await wf.demand_table(t_prod.output_frame("table"))
            assert df.rows() == canonical(ROWS)
            assert producer.calls == 2  # served, no re-run
            targets = {(t.grid_path, t.coords) for t in await wf.targets()}
            assert (enc.encode_key_path(["prod"]), "[]") in targets

            # Per-output staleness on the frame id: clear the producer,
            # re-demand it — the rerun reproduces the identical frame
            # entity, so the consumer's observation still matches and it
            # serves fresh without re-running.
            await wf.clear(t_prod)
            await wf.demand_table(t_prod.output_frame("table"))
            assert producer.calls == 4  # one fresh run (+ its safety replay)
            assert await wf.demand(t_cons.output_entity()) is not None
            assert consumer.calls == 2  # still fresh: no re-run


class BadFrameOutputTask:
    """Returns a frame that violates the declared output schema — the
    host-side check must fail the generation as a TaskOutputError."""

    async def __call__(self, ctx, /):
        return pl.DataFrame({"k": ["a"], "wrong": [1]})


@pytest.mark.anyio
async def test_frame_output_validation_fails_the_generation(tmp_path: Path) -> None:
    state, ents = _paths(tmp_path)
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            await _bind(wf, host, fns, "bad", BadFrameOutputTask())
            t = await wf.root.open_task(
                "t",
                axes=(),
                task_fn=fns.at(fn="bad"),
                args={},
                outputs={"table": m.FrameOutput(schema=SCHEMA)},
            )
            with pytest.raises(CellFaultError, match="TaskOutputError"):
                await wf.demand_table(t.output_frame("table"))
            (cell,) = await wf.list_cells(t)
            assert cell.status == "errored"


@pytest.mark.anyio
async def test_output_kind_checks_on_the_user_capability(tmp_path: Path) -> None:
    """The wire-level guards on the user capability: an entity output read
    as a frame (and vice versa) is refused before anything records."""
    state, ents = _paths(tmp_path)
    async with open_local(state, ents) as wf:
        fns = await wf.open_handle(wf.root, "fns", FN_AXES)
        await wf.write_handle(fns, _fn_key("f"), fn="f")
        t = await wf.root.open_task(
            "t",
            axes=(),
            task_fn=fns.at(fn="f"),
            args={},
            outputs={"ent": m.EntityOutput(), "tab": m.FrameOutput(schema=SCHEMA)},
        )
        ent_sel = Workflow._sel(t.output_entity("ent"))
        tab_sel = Workflow._sel(t.output_frame("tab"))
        with pytest.raises(ViewAxisError, match="entity output"):
            await typed_wire_call(wf.capability.read_cell_frame(ent_sel))
        with pytest.raises(ViewAxisError, match="frame output"):
            await typed_wire_call(wf.capability.read_cell_entity(tab_sel))
        # The view layer's own typed selectors agree.
        with pytest.raises(ViewAxisError):
            t.output_frame("ent")
        with pytest.raises(ViewAxisError):
            t.output_entity("tab")


# -- the user surface -----------------------------------------------------------------


@pytest.mark.anyio
async def test_user_surface_write_read_sourcing(tmp_path: Path) -> None:
    """User frame writes: idempotence, conflict, overwrite_table, adopt,
    and sourcing pinned to the canonical frame id."""
    state, ents = _paths(tmp_path)
    async with open_local(state, ents) as wf:
        d = await wf.root.open_dataframe("d", X_AXES, SCHEMA)
        assert isinstance(d, DataFrameView) and d.schema == SCHEMA
        await d.write_table(tables.make_frame(SCHEMA, ROWS), x="a")
        assert (await d.read_table(x="a")).rows() == canonical(ROWS)
        # Idempotent permuted rewrite; different multiset conflicts.
        await d.write_table(tables.make_frame(SCHEMA, list(reversed(ROWS))), x="a")
        with pytest.raises(WriteConflictError):
            await d.write_table(tables.make_frame(SCHEMA, OTHER_ROWS), x="a")
        # Sourcing pinned the canonical frame id (writing is sourcing).
        frame_id = store_frame(SCHEMA, ROWS)
        d_path = enc.encode_key_path(["d"])
        pins = {(s.grid_path, s.coords): s for s in await wf.sourcing()}
        assert pins[(d_path, '["a"]')].fingerprint == frame_id
        assert pins[(d_path, '["a"]')].status == "attached"
        # Privileged replace.
        await wf.overwrite_table(d, tables.make_frame(SCHEMA, OTHER_ROWS), x="a")
        assert (await d.read_table(x="a")).rows() == OTHER_ROWS
        # adopt: revoke the grid's sourcing, then a plain write refuses and
        # an adopt write re-sources (capability-level, as the wrapper's
        # write_table carries no adopt flag).
        assert await wf.revoke_grid_sourcing(wf.root, "d", force=True)
        payload = tables.frame_to_ipc(tables.make_frame(SCHEMA, ROWS))
        with pytest.raises(GridNotSourcedError):
            await typed_wire_call(
                wf.capability.write_cell_frame(Workflow._sel(d, {"x": "b"}), payload)
            )
        await typed_wire_call(
            wf.capability.write_cell_frame(Workflow._sel(d, {"x": "b"}), payload, adopt=True)
        )
        assert (await d.read_table(x="b")).rows() == canonical(ROWS)
        # The grounding read gate applies to frame reads: cell "a" lost its
        # sourcing in the force-revoke above and is now an ungrounded fact.
        with pytest.raises(UngroundedSubjectError):
            await d.read_table(x="a")


@pytest.mark.anyio
async def test_projection_rebuild_and_at_rest_round_trip(tmp_path: Path) -> None:
    """A reopened session (empty projection) rebuilds serving bytes from
    the canonical entity; export/import round-trips populated frame cells
    into another fresh projection."""
    state, ents = _paths(tmp_path)
    async with open_local(state, ents) as wf:
        d = await wf.root.open_dataframe("d", X_AXES, SCHEMA)
        await d.write_table(tables.make_frame(SCHEMA, ROWS), x="a")
        await d.write_table(tables.make_frame(SCHEMA, []), x="empty")  # a valid value
    async with open_local(state, ents) as wf2:
        d2 = await wf2.root.open_dataframe("d", X_AXES, SCHEMA)  # attach
        assert (await d2.read_table(x="a")).rows() == canonical(ROWS)
        assert (await d2.read_table(x="a")).rows() == canonical(ROWS)  # cache hit
        empty = await d2.read_table(x="empty")
        assert empty.height == 0 and empty.schema == tables.polars_schema(SCHEMA)
        await wf2.export_state(str(tmp_path / "export"))
    # Import: fresh state db over the shared entity store (the at-rest
    # contract: git-repo layout + entity store), fresh empty projection.
    async with open_local(str(tmp_path / "s2.sqlite"), ents) as wf3:
        await wf3.import_state(str(tmp_path / "export"))
        d3 = await wf3.root.open_dataframe("d", X_AXES, SCHEMA)
        assert (await d3.read_table(x="a")).rows() == canonical(ROWS)


@pytest.mark.anyio
async def test_introspection_over_frame_cells(tmp_path: Path) -> None:
    """inspect / list_cells / delete_cell / lint over frame cells: sane
    id-level reports, no crashes (the preview variant is chunk 5)."""
    state, ents = _paths(tmp_path)
    async with open_local(state, ents) as wf:
        d = await wf.root.open_dataframe("d", X_AXES, SCHEMA)
        await d.write_table(tables.make_frame(SCHEMA, ROWS), x="a")
        frame_id = store_frame(SCHEMA, ROWS)
        report = await wf.inspect(d, x="a")
        assert report.value == frame_id and report.status is None
        assert report.grounding == "grounded"
        (cell,) = await wf.list_cells(d)
        assert cell.coords == ("a",)
        rows = {g.path: g for g in (await wf.summarize()).grids}
        assert rows[enc.encode_key_path(["d"])].populated == 1
        lint = await wf.lint()
        assert lint.sound
        assert await wf.delete_cell(d, x="a") is True
        with pytest.raises(CellFaultError):
            await d.read_table(x="a")


# -- the projection cache -------------------------------------------------------------


def _ids(n: int) -> list[FxtrID]:
    return [_fn_key(f"proj-{i}") for i in range(n)]


def test_frame_projection_lru() -> None:
    """Bytes-budget LRU: eviction order, recency refresh, oversize
    entries served uncached, idempotent re-puts."""
    a, b, c = _ids(3)
    proj = FrameProjection(budget=100)
    proj.put(a, b"x" * 40)
    proj.put(b, b"y" * 40)
    assert proj.get(a) == b"x" * 40  # refreshes a's recency
    proj.put(c, b"z" * 40)  # over budget: evicts b (LRU), not a
    assert proj.get(b) is None
    assert proj.get(a) == b"x" * 40 and proj.get(c) == b"z" * 40
    proj.put(a, b"x" * 40)  # idempotent re-put: no double accounting
    assert proj.get(c) == b"z" * 40
    big = _ids(4)[3]
    proj.put(big, b"w" * 101)  # oversize: served uncached, cache untouched
    assert proj.get(big) is None
    assert proj.get(a) == b"x" * 40 and proj.get(c) == b"z" * 40
