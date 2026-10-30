"""CoordView tests (phase 1 chunk 1): the path-less coordinate view.

Covers the chunk's list: value round-trip through real runs (consumed,
kept, literal, prefix-axis), token-backed forwarding into child rules
(consumed→Param translation, kept local creation task-side, and the
unmapped batch-invariance rejection), engine ops rejecting coord tokens,
config identity (attach vs kind/received-name mismatches), canonical
encoding of the binding union, unrecorded-pure reads (no record rows, no
staleness edge), and resume across an interruption.
"""

from pathlib import Path
from typing import AsyncIterator

import pytest
from frozendict import frozendict

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope

from fxtr_experiment import encoding as enc
from fxtr_experiment_task import model as m
from fxtr_experiment_task.errors import CellFaultError, GridConfigMismatchError, ViewAxisError
from fxtr_experiment_task.runner_api import ViewSel
from fxtr_experiment.state_store import WorkflowSession
from fxtr_experiment.user_service import open_local
from fxtr_experiment_task.view_spec import CoordViewSpec
from fxtr_experiment_task.views import int_coord, str_coord

from .test_executor import Env
from .test_ops_engine import Harness, _some_entity
from .test_user_surface import _bind, _paths

FN_AXES = (m.AxisSpec("fn", "str"),)
I_AXES = (m.AxisSpec("i", "int"),)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _marker(tag: str) -> FxtrID:
    """A distinct content-addressed entity per tag (active-scope staged)."""
    return (
        enc.SliceTemplateEntity(template=m.SliceTemplate(path=(), target_axes=(), output=tag))
        .store()
        .entity_id
    )


# ---------------------------------------------------------------------------
# Local view semantics (pure, no runner at all)
# ---------------------------------------------------------------------------


def test_local_coord_view_calculus() -> None:
    v = int_coord("i")
    assert v.axes == (m.AxisSpec("i", "int"),)
    assert v.value(i=5) == 5
    assert v.at(i=7).value() == 7
    assert v.rename_axes(j="i").value(j=9) == 9
    assert v.read_only() is v
    with pytest.raises(ViewAxisError, match="left unbound"):
        v.value()
    with pytest.raises(ViewAxisError, match="absent axis"):
        v.value(j=5)
    with pytest.raises(ViewAxisError, match="kind"):
        v.at(i="x")
    with pytest.raises(ViewAxisError, match="absent axis"):
        v.at(i=7).value(i=7)
    assert str_coord("s").value(s="red") == "red"


@pytest.mark.anyio
async def test_wrapper_rejects_coord_views(tmp_path: Path) -> None:
    """Privileged Workflow methods reject coord views with the typed error
    — coord views address no stored cells, so no privileged op applies."""
    state, ents = _paths(tmp_path)
    async with open_local(state, ents) as wf:
        with pytest.raises(ViewAxisError, match="no stored cells"):
            await wf.inspect(int_coord("i"), i=3)
        with pytest.raises(ViewAxisError, match="no stored cells"):
            await wf.add_target(int_coord("i"), i=2)
        with pytest.raises(ViewAxisError, match="no stored cells"):
            await wf.list_cells(str_coord("s"))


def test_coord_binding_encoding_round_trip() -> None:
    """The rule-input union's tagged encoding: round-trip + distinctness."""
    variants = [
        m.CoordBinding(kind="int", template=m.Param(0), received_axis_name=None),
        m.CoordBinding(kind="str", template=m.Param(0), received_axis_name=None),
        m.CoordBinding(kind="int", template=m.LiteralCoord(5), received_axis_name=None),
        m.CoordBinding(kind="int", template=m.KeepAxis(), received_axis_name="j"),
        m.CoordBinding(kind="int", template=m.KeepAxis(), received_axis_name="jj"),
    ]
    configs = [
        m.TaskGridConfig(
            axes=I_AXES,
            rule=m.TaskRule(
                task_fn=m.SliceTemplate(
                    path=(m.SubgridStepTemplate(cell=(), key="fns"),),
                    target_axes=(m.LiteralCoord("f"),),
                ),
                inputs=frozendict({"c": b}),
            ),
            outputs=m.DEFAULT_TASK_OUTPUTS,
        )
        for b in variants
    ]
    for config in configs:
        assert enc.config_from_json(enc.config_to_json(config)) == config
    ids = {enc.store_config(c) for c in configs}
    assert len(ids) == len(configs)  # every variant is identity-bearing


# ---------------------------------------------------------------------------
# End to end through the public surface
# ---------------------------------------------------------------------------


class IdxTask:
    """Returns an entity derived from its own (consumed) coordinate."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, ctx, /, *, idx):
        self.calls += 1
        return _marker(f"idx{idx.value()}")


class KeptAndLiteralTask:
    """Reads a kept coord (the identity fn) and a literal-bound constant."""

    async def __call__(self, ctx, /, *, j, c):
        assert j.value(j=41) == 41
        assert j.at(j=2).value() == 2
        return _marker(f"kl{j.value(j=41)}-{c.value()}")


@pytest.mark.anyio
async def test_consumed_coord_end_to_end(tmp_path: Path) -> None:
    """Each task cell learns its own coordinate; config identity behaves."""
    state, ents = _paths(tmp_path)
    task = IdxTask()
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            await _bind(wf, host, fns, "idx", task)
            t = await wf.root.open_task(
                "t", axes=I_AXES, task_fn=fns.at(fn="idx"), args={"idx": int_coord("i")}
            )
            # User demands require attached targets (grounding's root facts).
            await wf.add_target(t, i=3)
            await wf.add_target(t, i=7)
            assert await t.output_entity().read(i=3) == _marker("idx3")
            assert await t.output_entity().read(i=7) == _marker("idx7")
            assert task.calls == 4  # two cells × (run + safety replay)

            # Identical wiring attaches (create-or-attach on canonical config).
            await wf.root.open_task(
                "t", axes=I_AXES, task_fn=fns.at(fn="idx"), args={"idx": int_coord("i")}
            )
            # A different coordinate kind is a different canonical config.
            with pytest.raises(GridConfigMismatchError):
                await wf.root.open_task(
                    "t", axes=I_AXES, task_fn=fns.at(fn="idx"), args={"idx": str_coord("i")}
                )


@pytest.mark.anyio
async def test_kept_and_literal_coords(tmp_path: Path) -> None:
    state, ents = _paths(tmp_path)
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            await _bind(wf, host, fns, "kl", KeptAndLiteralTask())
            t = await wf.root.open_task(
                "k",
                axes=(),
                task_fn=fns.at(fn="kl"),
                args={"j": int_coord("j"), "c": int_coord("z").at(z=7)},
            )
            await wf.add_target(t)
            assert await t.output_entity().read() == _marker("kl41-7")
            # The kept axis's received name is identity-bearing.
            with pytest.raises(GridConfigMismatchError):
                await wf.root.open_task(
                    "k",
                    axes=(),
                    task_fn=fns.at(fn="kl"),
                    args={"j": int_coord("jj"), "c": int_coord("z").at(z=7)},
                )
            # ... and so is the bound literal.
            with pytest.raises(GridConfigMismatchError):
                await wf.root.open_task(
                    "k",
                    axes=(),
                    task_fn=fns.at(fn="kl"),
                    args={"j": int_coord("j"), "c": int_coord("z").at(z=8)},
                )


class WhichTask:
    """Learns its enclosing subtree coordinate (a prefix-axis consume)."""

    async def __call__(self, ctx, /, *, which):
        return _marker(f"ws-{which.value()}")


@pytest.mark.anyio
async def test_prefix_axis_consume(tmp_path: Path) -> None:
    """A coord consumed by a *subtree* axis: the task learns its enclosing
    coordinate (the consumable space is kept prefix positions ++ own axes)."""
    state, ents = _paths(tmp_path)
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            await _bind(wf, host, fns, "which", WhichTask())
            ws = await wf.root.open_subtree("ws", (m.AxisSpec("v", "str"),))
            t = await ws.open_task(
                "t", axes=(), task_fn=fns.at(fn="which"), args={"which": str_coord("v")}
            )
            await wf.add_target(t, v="red")
            await wf.add_target(t, v="blue")
            assert await t.output_entity().read(v="red") == _marker("ws-red")
            assert await t.output_entity().read(v="blue") == _marker("ws-blue")


# ---------------------------------------------------------------------------
# Forwarding received coord views into child rules (token-backed)
# ---------------------------------------------------------------------------


class ParentTask:
    """Forwards its received (consumed) coord into a child task's rule, and
    creates a fresh kept coord for the child too."""

    async def __call__(self, ctx, /, *, idx, ws, fns):
        child = await ws.open_task(
            "child",
            axes=(),
            task_fn=fns.at(fn="leaf"),
            args={"idx": idx, "j": int_coord("j")},
        )
        return await child.output_entity().read()


class LeafTask:
    """Reads the forwarded coord (its grandparent-cell coordinate) and the
    kept identity coord."""

    async def __call__(self, ctx, /, *, idx, j):
        return _marker(f"leaf{idx.value()}-{j.value(j=9)}")


class UnmappedParentTask:
    """Tries to forward its per-cell coord into a workspace shared across
    its cells — the batch-invariance violation."""

    async def __call__(self, ctx, /, *, idx, shared, fns):
        await shared.open_task("child", axes=(), task_fn=fns.at(fn="leaf"), args={"idx": idx})
        return _marker("unreachable")


@pytest.mark.anyio
async def test_forwarding_received_coord(tmp_path: Path) -> None:
    state, ents = _paths(tmp_path)
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            await _bind(wf, host, fns, "parent", ParentTask())
            await _bind(wf, host, fns, "leaf", LeafTask())
            # The workspace's own axis is consumed by the parent's "i", so
            # the creation path maps the parent's holder axis (forwarding is
            # legal), and the child's frame tracks i one-to-one.
            ws = await wf.root.open_subtree("ws2", I_AXES)
            p = await wf.root.open_task(
                "p",
                axes=I_AXES,
                task_fn=fns.at(fn="parent"),
                args={"idx": int_coord("i"), "ws": ws, "fns": fns},
            )
            await wf.add_target(p, i=2)
            await wf.add_target(p, i=5)
            assert await p.output_entity().read(i=2) == _marker("leaf2-9")
            assert await p.output_entity().read(i=5) == _marker("leaf5-9")


@pytest.mark.anyio
async def test_forwarding_unmapped_coord_rejected(tmp_path: Path) -> None:
    """Forwarding a per-cell coord into an uncorrelated workspace is the
    batch-invariance rejection, surfacing as the task's own error."""
    state, ents = _paths(tmp_path)
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            await _bind(wf, host, fns, "parent", UnmappedParentTask())
            await _bind(wf, host, fns, "leaf", LeafTask())
            shared = await wf.root.open_subtree("shared", ())
            p = await wf.root.open_task(
                "p",
                axes=I_AXES,
                task_fn=fns.at(fn="parent"),
                args={"idx": int_coord("i"), "shared": shared, "fns": fns},
            )
            await wf.add_target(p, i=2)  # target first, so the read reaches the demand
            with pytest.raises(CellFaultError, match="does not map"):
                await p.output_entity().read(i=2)


# ---------------------------------------------------------------------------
# Record transparency, staleness, resume (executor-level, hand-built rule)
# ---------------------------------------------------------------------------

FNS = enc.encode_key_path(["fns"])
INPUTS = enc.encode_key_path(["inputs"])
A_PATH = enc.encode_key_path(["a"])

IDX_BINDING = m.CoordBinding(kind="int", template=m.Param(0), received_axis_name=None)
INPUT_BINDING = m.SliceBinding(
    slice=m.SliceTemplate(
        path=(m.SubgridStepTemplate(cell=(), key="inputs"),),
        target_axes=(m.KeepAxis(),),
    ),
    mode="read",
    received_axis_names=("x",),
)


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    async with WorkflowSession.open(
        str(tmp_path / "state.sqlite"), str(tmp_path / "entities.sqlite")
    ) as session:
        async with FxtrScope(name="wf3-runner", include=[session.entity_scope]) as runner_scope:
            e = Env(session, runner_scope)
            await e.seed_base()
            yield e


async def _create_coord_task(env: Env, fn_name: str, inputs: dict) -> None:
    config = m.TaskGridConfig(
        axes=I_AXES,
        rule=m.TaskRule(
            task_fn=m.SliceTemplate(
                path=(m.SubgridStepTemplate(cell=(), key="fns"),),
                target_axes=(m.LiteralCoord(fn_name),),
            ),
            inputs=frozendict(inputs),
        ),
        outputs=m.DEFAULT_TASK_OUTPUTS,
    )
    await env.session.create_grid(A_PATH, "task", enc.GridConfigEntity(config=config))
    # User-sourced like every directly-seeded grid (the strict grounding
    # gate) and so a per-cell target can attach (grid-before-cell invariant).
    await env._source_grid(A_PATH)


class CoordAndInputTask:
    """Reads its coord and one data cell; the coord must leave no record."""

    async def __call__(self, ctx, /, *, idx, inp):
        value = await inp.read(x="a")
        assert value is not None
        return _marker(f"ci{idx.value()}")


@pytest.mark.anyio
async def test_coord_reads_are_unrecorded_and_unimplicable(env: Env) -> None:
    await env.bind_fn("ci", CoordAndInputTask())
    await env.write_data(INPUTS, '["a"]', _some_entity())
    await _create_coord_task(env, "ci", {"idx": IDX_BINDING, "inp": INPUT_BINDING})

    served = await env.demand(A_PATH, "[5]")
    assert served == {"result": _marker("ci5")}
    row = await env.session.get_generation(A_PATH, "[5]")
    assert row is not None and row[2] == "complete"
    # Coordinates are structure: the record holds exactly the prologue fn
    # observation and the data read — nothing for the coord.
    subjects = {(r[0], r[1]) for r in await env.session.load_record(row[1])}
    assert subjects == {(FNS, '["ci"]'), (INPUTS, '["a"]')}
    # No staleness edge exists to the coord: the only way this cell goes
    # stale is through its recorded subjects.
    report = await env.session.cell_currency(A_PATH, "[5]")
    assert report is not None and report.currency == "fresh"


class IdxThenMissingTask:
    """Reads its coord, then a cell that may not exist yet (fault path)."""

    async def __call__(self, ctx, /, *, idx, inp):
        i = idx.value()
        await inp.read(x="b")  # missing on the first run → interrupted
        return _marker(f"resumed{i}")


@pytest.mark.anyio
async def test_coord_value_across_interruption_resume(env: Env) -> None:
    """A resumed generation re-mints identical coord grants (the value is
    per-cell structure, not recorded state)."""
    await env.bind_fn("r", IdxThenMissingTask())
    await _create_coord_task(env, "r", {"idx": IDX_BINDING, "inp": INPUT_BINDING})

    with pytest.raises(CellFaultError):
        await env.demand(A_PATH, "[8]")
    row = await env.session.get_generation(A_PATH, "[8]")
    assert row is not None and row[2] == "interrupted"

    await env.write_data(INPUTS, '["b"]', _some_entity())
    served = await env.demand(A_PATH, "[8]")
    assert served == {"result": _marker("resumed8")}  # same generation, same coord
    row2 = await env.session.get_generation(A_PATH, "[8]")
    assert row2 is not None and row2[1] == row[1]  # resumed, not re-minted


# ---------------------------------------------------------------------------
# Engine ops reject coord tokens
# ---------------------------------------------------------------------------


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[Harness]:
    async with WorkflowSession.open(
        str(tmp_path / "state.sqlite"), str(tmp_path / "entities.sqlite")
    ) as session:
        harness = Harness(session)
        await harness.seed()
        yield harness


@pytest.mark.anyio
async def test_engine_ops_reject_coord_tokens(h: Harness) -> None:
    """No effectful op accepts a coord token — the value rides the grant;
    there is nothing to read, write, or open."""
    engine = await h.engine()
    grant = h.registry.mint(
        CoordViewSpec(value_kind="int", template=m.LiteralCoord(3), axes=()), value=3
    )
    sel = ViewSel(token=grant.token)
    with pytest.raises(ViewAxisError, match="coord"):
        await engine.read_cell_entity(sel)
    with pytest.raises(ViewAxisError, match="coord"):
        await engine.read_handle_key(sel)
    with pytest.raises(ViewAxisError, match="coord"):
        await engine.write_cell_entity(sel, _marker("x"))
    with pytest.raises(ViewAxisError, match="cannot open children"):
        await engine.open_data(ViewSel(token=grant.token), "child", ())
