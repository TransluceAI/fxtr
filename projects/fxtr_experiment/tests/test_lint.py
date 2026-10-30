"""The chunk-5 lint suites: the clean-state inventory as behavior.

Two halves. The **per-removal-kind unit tests** pin the report's sections
and diagnoses one mutation at a time (remove a target; revoke grid/cell
sourcing; clear a writer; delete a dep grid; config drift; the
adopted-grid-with-unsourced-parent case). The **lint-arc scenarios** pin
the operational half of the replay contract — the system re-creates what
grounding says it would: task-re-creates-its-grid replay-in-place, and
ordering-fault convergence for side-written data. Everything drives the
public surface (`open_local` → ``Workflow.lint``), so the report's wire
shape is exercised throughout (the real-transport form is the RPC smoke
in ``test_user_surface.py``).

The sound/clean split shows up as behavior here: *pending* targets
(cleared cells) are never findings, so a state can be sound while its
inventory still names structural damage — ``clean`` is the real green.
"""

from pathlib import Path

import pytest

from fxtr2.data.entity_ref_scope import FxtrScope

from fxtr_experiment import encoding as enc
from fxtr_experiment_task import model as m
from fxtr_experiment_task.errors import CellFaultError
from fxtr_experiment.user_api import LintReport, UngroundedFact
from fxtr_experiment.user_service import Workflow, open_local
from fxtr_experiment_task.views import DataView, SubtreeView

from .test_ops_engine import _some_entity
from .test_user_surface import (
    FN_AXES,
    X_AXES,
    EchoTask,
    MakesDataGrid,
    _bind,
    _paths,
)

K_AXES = (m.AxisSpec("k", "str"),)

T_PATH = enc.encode_key_path(["t"])
R_PATH = enc.encode_key_path(["r"])
W_PATH = enc.encode_key_path(["w"])
INPUTS_PATH = enc.encode_key_path(["inputs"])
D_PATH = enc.encode_key_path(["d"])
TASK_DATA_PATH = enc.encode_key_path(["sub", "task_data"])
CELL0 = enc.encode_coords(())
COORD_A = enc.encode_coords(("a",))
COORD_CELL = enc.encode_coords(("cell",))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _ungrounded(report: LintReport) -> dict[tuple[str, str, str], UngroundedFact]:
    return {(u.kind, u.grid_path, u.coords): u for u in report.ungrounded}


async def _echo_pipeline(wf: Workflow, host: FxtrScope):
    """fns + inputs["a"]=v1 + a zero-axis echo task t, demanded (auto-target)."""
    fns = await wf.open_handle(wf.root, "fns", FN_AXES)
    inputs = await wf.root.open_data("inputs", X_AXES)
    v1 = _some_entity().store().entity_id
    await inputs.write(v1, x="a")
    await _bind(wf, host, fns, "echo", EchoTask())
    t = await wf.root.open_task(
        "t", axes=(), task_fn=fns.at(fn="echo"), args={"inp": inputs.read_only()}
    )
    assert await wf.demand(t.output_entity()) == v1
    return fns, inputs, t, v1


# -- the per-removal-kind unit tests ---------------------------------------------


@pytest.mark.anyio
async def test_remove_target_orphans_the_generation(tmp_path: Path) -> None:
    """Removing the only target leaves the completed generation locally
    unwanted: an orphaned task-cell inventory entry — and nothing else
    (sound: no dangling intent, no red targets)."""
    state, ents = _paths(tmp_path)
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            _, _, t, _ = await _echo_pipeline(wf, host)
            assert (await wf.lint()).clean

            # The demand's auto-target pinned the output slice — remove by
            # the same slice identity.
            await wf.remove_target(t.output_entity())
            report = await wf.lint()
            assert report.sound and not report.clean
            (row,) = report.ungrounded
            assert (row.kind, row.grid_path, row.coords) == ("task_cell", T_PATH, CELL0)
            assert row.verdict == "orphaned"
            assert "locally unwanted" in (row.reason or "")


@pytest.mark.anyio
async def test_revoke_grid_sourcing_orphans_grid_and_cells(tmp_path: Path) -> None:
    """Force-revoking a grid's sourcing (cascading its cell records)
    orphans the grid and its populated cells, with the no-claimant
    diagnoses."""
    state, ents = _paths(tmp_path)
    async with open_local(state, ents) as wf:
        d = await wf.root.open_data("d", X_AXES)
        await d.write(_some_entity().store().entity_id, x="a")
        assert (await wf.lint()).clean

        assert await wf.revoke_grid_sourcing(wf.root, "d", force=True) is True
        report = await wf.lint()
        assert report.sound and not report.clean
        rows = _ungrounded(report)
        assert set(rows) == {("grid", D_PATH, ""), ("data_cell", D_PATH, COORD_A)}
        grid_row = rows[("grid", D_PATH, "")]
        assert grid_row.verdict == "orphaned"
        assert "no sourcing record and no current-config assertion" in (grid_row.reason or "")
        cell_row = rows[("data_cell", D_PATH, COORD_A)]
        assert cell_row.verdict == "orphaned"
        assert "no sourcing record and no current-value write claim" in (cell_row.reason or "")


@pytest.mark.anyio
async def test_revoke_cell_sourcing_unanchors_the_reader(tmp_path: Path) -> None:
    """Revoking an input cell's sourcing orphans the value and unanchors
    the generation that read it — surfacing as a red target whose reason
    names the cell."""
    state, ents = _paths(tmp_path)
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            _, inputs, _, _ = await _echo_pipeline(wf, host)
            assert await wf.revoke_cell_sourcing(inputs, x="a") is True

            report = await wf.lint()
            assert not report.sound
            (finding,) = report.target_findings
            assert (finding.grid_path, finding.coords) == (T_PATH, CELL0)
            assert finding.verdict == "unanchored"
            assert f"read data cell {INPUTS_PATH}{COORD_A} is not grounded" in (
                finding.reason or ""
            )
            rows = _ungrounded(report)
            assert set(rows) == {
                ("data_cell", INPUTS_PATH, COORD_A),
                ("task_cell", T_PATH, CELL0),
            }
            assert rows[("data_cell", INPUTS_PATH, COORD_A)].verdict == "orphaned"
            assert rows[("task_cell", T_PATH, CELL0)].verdict == "unanchored"


@pytest.mark.anyio
async def test_clear_writer_orphans_side_written_facts(tmp_path: Path) -> None:
    """Clearing a creator expires its claims: the task-created grid and
    its side-written cell orphan. The cleared cell's own target is
    *pending intent*, not a finding — the report is sound but not clean."""
    state, ents = _paths(tmp_path)
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            v1 = _some_entity().store().entity_id
            await inputs.write(v1, x="a")
            await _bind(wf, host, fns, "maker", MakesDataGrid())
            sub = await wf.root.open_subtree("sub", axes=())
            t = await wf.root.open_task(
                "t",
                axes=(),
                task_fn=fns.at(fn="maker"),
                args={"sub": sub, "seed": inputs.read_only()},
            )
            assert await wf.demand(t.output_entity()) == v1
            assert (await wf.lint()).clean

            await wf.clear(t)
            report = await wf.lint()
            assert report.sound and not report.clean  # the target on t is pending
            rows = _ungrounded(report)
            assert set(rows) == {
                ("grid", TASK_DATA_PATH, ""),
                ("data_cell", TASK_DATA_PATH, COORD_CELL),
            }
            assert all(row.verdict == "orphaned" for row in rows.values())


@pytest.mark.anyio
async def test_delete_dep_grid_unanchors_dependents(tmp_path: Path) -> None:
    """Deleting a rule-referenced grid dangles its sourcing records,
    unanchors the dependent grid (the chain names the missing reference),
    and reddens the dependent's target."""
    state, ents = _paths(tmp_path)
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            await _echo_pipeline(wf, host)
            delete_report = await wf.delete_grid(wf.root, "inputs")
            assert delete_report.broken_referrers == (T_PATH,)

            report = await wf.lint()
            assert not report.sound
            assert {(s.grid_path, s.coords, s.status) for s in report.dangling_sourcing} == {
                (INPUTS_PATH, "", "dangling_missing"),
                (INPUTS_PATH, COORD_A, "dangling_missing"),
            }
            (finding,) = report.target_findings
            assert (finding.grid_path, finding.verdict) == (T_PATH, "unanchored")
            assert f"grid {T_PATH} is not grounded" in (finding.reason or "")
            rows = _ungrounded(report)
            assert set(rows) == {("grid", T_PATH, ""), ("task_cell", T_PATH, CELL0)}
            grid_row = rows[("grid", T_PATH, "")]
            assert grid_row.verdict == "unanchored"
            assert f"rule reference {INPUTS_PATH} is not grounded" in (grid_row.reason or "")


@pytest.mark.anyio
async def test_config_drift_dangles_sourcing_and_target(tmp_path: Path) -> None:
    """Delete + recreate with a different config: the target dangles
    mismatched (its pinned config no longer matches), the old cell's
    sourcing record dangles missing — across a session reopen, so the
    report's served ids exercise the re-attach rule."""
    state, ents = _paths(tmp_path)
    async with open_local(state, ents) as wf:
        aux = await wf.root.open_data("aux", K_AXES)
        v = _some_entity().store().entity_id
        await aux.write(v, k="z")
        await wf.add_target(aux, k="z")  # a data-cell target (checked read-root)
        assert (await wf.lint()).clean

        await wf.delete_grid(wf.root, "aux")
        await wf.root.open_data("aux", (m.AxisSpec("j", "int"),))  # drifted config

    async with open_local(state, ents) as wf2:
        report = await wf2.lint()
        assert not report.sound
        (target,) = report.dangling_targets
        assert target.status == "dangling_mismatched"
        # The recreate's auto-sourcing re-pinned the grid record (attached);
        # only the old cell record dangles.
        (srow,) = report.dangling_sourcing
        assert (srow.grid_path, srow.coords, srow.status) == (
            enc.encode_key_path(["aux"]),
            enc.encode_coords(("z",)),
            "dangling_missing",
        )
        assert srow.fingerprint == v  # pinned id, re-attached and served
        assert not report.ungrounded  # the recreated grid is sourced


class MakesNestedGrid:
    """Creates a subtree and a data grid inside it (both task-created) and
    writes a cell — the adoption-pulls-in-parents shape."""

    async def __call__(self, ctx, /, *, sub, seed):
        v = await seed.read(x="a")
        mid = await sub.open_subtree("mid", axes=())
        leaf = await mid.open_data("leaf", K_AXES)
        await leaf.write(v, k="cell")
        return v


@pytest.mark.anyio
async def test_adopted_grid_with_unsourced_parent(tmp_path: Path) -> None:
    """Adopting an inner grid does not adopt its enclosing (task-created)
    subtree: once the creator is cleared, the adopted grid is unanchored
    with the parent named — the linter shows exactly what the adoption
    pulls in — and its unadopted cell orphans."""
    state, ents = _paths(tmp_path)
    mid_path = enc.encode_key_path(["sub", "mid"])
    leaf_path = enc.encode_key_path(["sub", "mid", "leaf"])
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            v1 = _some_entity().store().entity_id
            await inputs.write(v1, x="a")
            await _bind(wf, host, fns, "nested", MakesNestedGrid())
            sub = await wf.root.open_subtree("sub", axes=())
            t = await wf.root.open_task(
                "t",
                axes=(),
                task_fn=fns.at(fn="nested"),
                args={"sub": sub, "seed": inputs.read_only()},
            )
            assert await wf.demand(t.output_entity()) == v1

            mid = await wf.attach(sub, "mid")
            assert isinstance(mid, SubtreeView)
            await wf.source_grid(mid, "leaf")  # adoption: co-assert the inner grid
            assert (await wf.lint()).clean

            await wf.clear(t)
            report = await wf.lint()
            assert report.sound and not report.clean
            rows = _ungrounded(report)
            assert set(rows) == {
                ("grid", mid_path, ""),
                ("grid", leaf_path, ""),
                ("data_cell", leaf_path, COORD_CELL),
            }
            assert rows[("grid", mid_path, "")].verdict == "orphaned"
            leaf_row = rows[("grid", leaf_path, "")]
            assert leaf_row.verdict == "unanchored"  # adopted, but the chain breaks
            assert f"enclosing grid {mid_path} is not grounded" in (leaf_row.reason or "")
            assert rows[("data_cell", leaf_path, COORD_CELL)].verdict == "orphaned"


# -- the lint-arc scenarios -------------------------------------------------------


class ReadsTokenAndCell:
    """Reads its upstream token (making the ordering explicit in the
    demand graph — the one-pass idiom) and then the side-written cell."""

    async def __call__(self, ctx, /, *, tok, cell):
        await tok.read()
        return await cell.read(k="cell")


@pytest.mark.anyio
async def test_replay_in_place_recreates_task_grid(tmp_path: Path) -> None:
    """The operational half of the replay contract: delete a task-created
    grid → lint red (the reader's grid and generation unanchor, the
    reader's target reddens); clear the invalidated generations → sound
    (pending targets are not findings) but not clean (the broken rule
    reference stays in the inventory until the creator re-runs);
    re-demand → the creator re-creates the grid (the ``asserted_grid``
    claim exercised as behavior), the cone rebuilds with matching
    fingerprints, and lint is clean."""
    state, ents = _paths(tmp_path)
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            v1 = _some_entity().store().entity_id
            await inputs.write(v1, x="a")
            await _bind(wf, host, fns, "maker", MakesDataGrid())
            await _bind(wf, host, fns, "reader", ReadsTokenAndCell())
            sub = await wf.root.open_subtree("sub", axes=())
            t = await wf.root.open_task(
                "t",
                axes=(),
                task_fn=fns.at(fn="maker"),
                args={"sub": sub, "seed": inputs.read_only()},
            )
            assert await wf.demand(t.output_entity()) == v1  # creates sub/task_data
            td = await wf.attach(sub, "task_data")
            assert isinstance(td, DataView)
            r = await wf.root.open_task(
                "r",
                axes=(),
                task_fn=fns.at(fn="reader"),
                args={"tok": t.output_entity(), "cell": td.read_only()},
            )
            assert await wf.demand(r.output_entity()) == v1
            assert (await wf.lint()).clean

            # Delete the task-created grid: red.
            delete_report = await wf.delete_grid(sub, "task_data")
            assert delete_report.broken_referrers == (R_PATH,)
            report = await wf.lint()
            assert not report.sound
            (finding,) = report.target_findings  # t's generation stays grounded
            assert (finding.grid_path, finding.verdict) == (R_PATH, "unanchored")
            rows = _ungrounded(report)
            assert set(rows) == {("grid", R_PATH, ""), ("task_cell", R_PATH, CELL0)}
            assert f"rule reference {TASK_DATA_PATH} is not grounded" in (
                rows[("grid", R_PATH, "")].reason or ""
            )
            assert not report.dangling_sourcing  # task-created: no user record to dangle

            # Clear the invalidated generations (the writer asserted/observed
            # the grid; the reader read into it): pending targets are not
            # findings — sound — but the broken rule reference stays named
            # until the creator re-runs.
            await wf.clear(t)
            await wf.clear(r)
            report = await wf.lint()
            assert report.sound and not report.clean
            (row,) = report.ungrounded
            assert (row.kind, row.grid_path) == ("grid", R_PATH)

            # The iterate-to-quiescence path: re-demanding the reader FIRST
            # cannot rebuild the cascade — grant minting precedes the run,
            # so the mint faults on the missing grid (named, with the
            # grounding-enriched diagnosis) before the token read could
            # ever cascade to the creator. Convergence is by re-demand.
            with pytest.raises(CellFaultError, match="unanchored") as ei:
                await wf.demand(r.output_entity())
            assert "task_data" in str(ei.value)

            # Re-demand: the creator re-creates the grid; the cone rebuilds
            # with matching fingerprints; clean.
            assert await wf.demand(t.output_entity()) == v1
            assert await wf.demand(r.output_entity()) == v1
            td2 = await wf.attach(sub, "task_data")
            assert isinstance(td2, DataView)
            assert await td2.read(k="cell") == v1  # the same content-addressed id
            assert (await wf.lint()).clean


class WritesCell:
    """Writes its seed into the granted grid's k='cell' (a side-write)."""

    async def __call__(self, ctx, /, *, out, seed):
        v = await seed.read(x="a")
        await out.write(v, k="cell")
        return v


class ReadsCellOnly:
    """Reads the side-written cell with no upstream token — the ordering
    the demand graph does not express."""

    async def __call__(self, ctx, /, *, cell):
        return await cell.read(k="cell")


@pytest.mark.anyio
async def test_ordering_fault_convergence(tmp_path: Path) -> None:
    """The convergence-is-iteration caveat for side-written data: demanding
    the reader before its writer ever ran faults on the missing cell and
    lints red (the reader's target, naming the cell); demanding the writer
    and re-demanding the reader converges to clean."""
    state, ents = _paths(tmp_path)
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            v1 = _some_entity().store().entity_id
            await inputs.write(v1, x="a")
            await _bind(wf, host, fns, "writer", WritesCell())
            await _bind(wf, host, fns, "reader", ReadsCellOnly())
            d = await wf.root.open_data("d", K_AXES)  # user-created, empty
            w = await wf.root.open_task(
                "w",
                axes=(),
                task_fn=fns.at(fn="writer"),
                args={"out": d, "seed": inputs.read_only()},
            )
            r = await wf.root.open_task(
                "r", axes=(), task_fn=fns.at(fn="reader"), args={"cell": d.read_only()}
            )

            with pytest.raises(CellFaultError, match="interrupted"):
                await wf.demand(r.output_entity())  # the writer never ran
            report = await wf.lint()
            assert not report.sound
            (finding,) = report.target_findings
            assert (finding.grid_path, finding.verdict) == (R_PATH, "unanchored")
            assert f"read data cell {D_PATH}{COORD_CELL} is not grounded" in (finding.reason or "")
            rows = _ungrounded(report)
            assert set(rows) == {("task_cell", R_PATH, CELL0)}

            # Converge by re-demand: run the writer, then the reader.
            assert await wf.demand(w.output_entity()) == v1
            assert await wf.demand(r.output_entity()) == v1
            assert (await wf.lint()).clean
