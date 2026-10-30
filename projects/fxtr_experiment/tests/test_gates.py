"""The chunk-3 gate scenarios: grounding verdicts affecting execution.

The gate table of ``notes/grounding.md`` as behavior, over the durable
request record: the cleared-driver window (strict refuses to let new work
bake in an orphaned side-written value), handle-sourcing revocation
faulting a rerun's prologue connect, the three-deep only-top-targeted
cascade (self-vouching from durable request edges — nothing threads
through the executor), the serve-time re-check (a revocation faults the
demander at arrival while currency stays fresh — the axes are
orthogonal), the emergent launch-without-target rejection at the raw
executor (the capability-level `NotTargetedError` law is covered in
``test_sourcing_surface.py``; the sub-workflow cascade under strict is
``test_user_surface.py``'s sub-workflow test), the structural mint gate's
grounding-enriched diagnosis, and the strict/``off`` knob as pure policy
(behavior flips across a reopen of the same files; stored verdicts don't
move).

Driving gate faults through the ``Workflow`` wrapper here also pins the
wire contract: ``UngroundedSubjectError`` arrives *typed* through
``typed_wire_call`` (it is a ``WorkflowError``, so the transit-category
reconstruction covers it).
"""

from pathlib import Path
from typing import AsyncIterator

import pytest

from fxtr2.data.entity_ref_scope import FxtrScope

from fxtr_experiment import encoding as enc
from fxtr_experiment_task import model as m
from fxtr_experiment_task.errors import CellFaultError, UngroundedSubjectError
from fxtr_experiment.state_store import WorkflowSession
from fxtr_experiment.user_service import open_local
from fxtr_experiment_task.views import DataView, TaskView

from .test_executor import (
    A_PATH,
    B_PATH,
    CELL,
    INPUT_BINDING,
    INPUTS,
    A_OUTPUT_BINDING,
    EchoTask,
    Env,
    ForwardTask,
)
from .test_ops_engine import _some_entity
from .test_user_surface import (
    FN_AXES,
    X_AXES,
    MakesDataGrid,
    _bind,
    _paths,
)
from .test_user_surface import EchoTask as SurfaceEchoTask


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    async with WorkflowSession.open(
        str(tmp_path / "state.sqlite"), str(tmp_path / "entities.sqlite")
    ) as session:
        async with FxtrScope(name="wf3-runner", include=[session.entity_scope]) as runner_scope:
            e = Env(session, runner_scope)
            await e.seed_base()
            yield e


C_PATH = enc.encode_key_path(["c"])
B_OUTPUT_BINDING = m.SliceBinding(
    slice=m.SliceTemplate(
        path=(m.SubgridStepTemplate(cell=(), key="b"),),
        target_axes=(),
        output="result",
    ),
    mode="read",
    received_axis_names=(),
)


class ReadsCell:
    """Reads the task-written cell (k='cell') of its input grid."""

    async def __call__(self, ctx, /, *, inp):
        return await inp.read(k="cell")


# -- 1. The cleared-driver window (the motivating exhibit) ----------------------


@pytest.mark.anyio
async def test_cleared_driver_window(tmp_path: Path) -> None:
    """A task-written value whose writer is cleared is orphaned: untouched
    and currency-innocent, but the flow of intent no longer specifies it —
    strict faults both a user read and a new task's read of it."""
    state, ents = _paths(tmp_path)
    maker = MakesDataGrid()
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            v1 = _some_entity().store().entity_id
            await inputs.write(v1, x="a")
            await _bind(wf, host, fns, "maker", maker)
            sub = await wf.root.open_subtree("sub", axes=())
            t = await wf.root.open_task(
                "t",
                axes=(),
                task_fn=fns.at(fn="maker"),
                args={"sub": sub, "seed": inputs.read_only()},
            )
            assert await wf.demand(t.output_entity()) == v1  # auto-targets; the driver runs

            # The driver's claims ground the side-written grid + cell: a
            # user read of the task-written value serves under strict.
            td = await wf.attach(sub, "task_data")
            assert isinstance(td, DataView)
            assert await td.read(k="cell") == v1

            # Clear the driver: its claims expire with the generation —
            # the value is untouched, but nothing specifies it anymore.
            await wf.clear(t)
            report = await wf.inspect(td, k="cell")
            assert report.value == v1  # the labeled escape still shows it
            assert report.grounding == "orphaned"
            with pytest.raises(UngroundedSubjectError, match="orphaned"):
                await td.read(k="cell")

            # New work must not silently bake it in: a fresh task reading
            # the orphan faults at its gated read → interrupted, with the
            # gate fault as the recorded evidence.
            await _bind(wf, host, fns, "reader", ReadsCell())
            r = await wf.root.open_task(
                "r",
                axes=(),
                task_fn=fns.at(fn="reader"),
                args={"inp": td.read_only()},
            )
            with pytest.raises(CellFaultError, match="interrupted"):
                await wf.demand(r.output_entity())
            r_report = await wf.inspect(r)
            assert r_report.status == "interrupted"
            assert "UngroundedSubjectError" in (r_report.reason or "")


# -- 2. Handle-sourcing revocation faults the rerun's prologue connect ----------


@pytest.mark.anyio
async def test_handle_revocation_faults_prologue(tmp_path: Path) -> None:
    """Revoking a handle cell's sourcing retires the external service from
    the experiment's intent: the next run's prologue read of the fn handle
    (the connect authority) faults orphaned; re-sourcing recovers."""
    state, ents = _paths(tmp_path)
    task = SurfaceEchoTask()
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            v1 = _some_entity().store().entity_id
            await inputs.write(v1, x="a")
            await _bind(wf, host, fns, "echo", task)
            t = await wf.root.open_task(
                "t", axes=(), task_fn=fns.at(fn="echo"), args={"inp": inputs.read_only()}
            )
            assert await wf.demand(t.output_entity()) == v1

            # Revoke the handle's sourcing; the completed result still
            # serves (its recorded reads resolved long ago)...
            assert await wf.revoke_cell_sourcing(fns, fn="echo") is True
            # ...but handle values ground only by user sourcing (tasks
            # never write handles), so a *rerun* faults at the prologue.
            await wf.clear(t)
            with pytest.raises(CellFaultError, match="interrupted"):
                await wf.demand(t.output_entity())
            report = await wf.inspect(t)
            assert report.status == "interrupted"
            assert "UngroundedSubjectError" in (report.reason or "")

            # Re-sourcing the handle is the recovery.
            await wf.source_cell(fns, fn="echo")
            assert await wf.demand(t.output_entity()) == v1


# -- 3. The three-deep cascade, only the top targeted ---------------------------


@pytest.mark.anyio
async def test_three_deep_cascade_only_top_targeted(env: Env) -> None:
    """C reads B reads A; only C is targeted. The whole fresh cascade runs
    clean under strict: each engine's request edge is durable before its
    demand hook fires, so `wanted` derives for every member from state
    alone (the option-(B) form of self-vouching — nothing threads through
    the executor)."""
    a_task = EchoTask()
    await env.bind_fn("echo", a_task)
    v1 = await env.write_data(INPUTS, '["a"]', _some_entity())
    await env.create_task_grid(A_PATH, "echo", {"inp": INPUT_BINDING})
    await env.bind_fn("fwd", ForwardTask())
    await env.create_task_grid(B_PATH, "fwd", {"up": A_OUTPUT_BINDING})
    await env.bind_fn("fwd2", ForwardTask())
    await env.create_task_grid(C_PATH, "fwd2", {"up": B_OUTPUT_BINDING})

    served = await env.demand(C_PATH, CELL)  # targets C alone
    assert served == {"result": v1}
    for path in (A_PATH, B_PATH, C_PATH):
        row = await env.session.get_generation(path, CELL)
        assert row is not None and row[2] == "complete"
    # Re-demand serves from the same durable demand structure: A and B
    # stay wanted through the recorded request edges, no re-run.
    assert await env.demand(C_PATH, CELL) == {"result": v1}
    assert a_task.calls == 2  # the one completed run: main + safety replay


# -- 4. Serve time is the re-check moment (and the axes are orthogonal) ---------


@pytest.mark.anyio
async def test_serve_recheck_faults_on_revoked_input(tmp_path: Path) -> None:
    """Revoking an input's sourcing moves no currency verdict (nothing
    observable changed) but unanchors the consumer: the next demand faults
    at serve — typed across the wire — with no re-run; re-sourcing serves
    again, still with no re-run."""
    state, ents = _paths(tmp_path)
    task = SurfaceEchoTask()
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            v1 = _some_entity().store().entity_id
            await inputs.write(v1, x="a")
            await _bind(wf, host, fns, "echo", task)
            t = await wf.root.open_task(
                "t", axes=(), task_fn=fns.at(fn="echo"), args={"inp": inputs.read_only()}
            )
            assert await wf.demand(t.output_entity()) == v1
            assert task.calls == 2

            assert await wf.revoke_cell_sourcing(inputs, x="a") is True
            with pytest.raises(UngroundedSubjectError, match="unanchored"):
                await wf.demand(t.output_entity())
            assert task.calls == 2  # refused at serve; nothing ran

            # Orthogonality, one snapshot: the value axis is untouched.
            report = await wf.inspect(t)
            assert report.currency == "fresh"
            assert report.grounding == "unanchored"

            await wf.source_cell(inputs, x="a")
            assert await wf.demand(t.output_entity()) == v1
            assert task.calls == 2  # served, not re-run


# -- 5. Launch-without-target is rejected at the gate itself --------------------


@pytest.mark.anyio
async def test_launch_without_target_rejected_at_gate(env: Env) -> None:
    """The emergent rejection, below the capability's `NotTargetedError`
    law: a raw executor demand of a never-run, unwanted cell executes but
    faults at the arrival gate (nothing durable justifies serving it);
    targeting the cell makes the same result servable with no re-run."""
    task = EchoTask()
    await env.bind_fn("echo", task)
    await env.write_data(INPUTS, '["a"]', _some_entity())
    await env.create_task_grid(A_PATH, "echo", {"inp": INPUT_BINDING})

    with pytest.raises(UngroundedSubjectError, match="not wanted"):
        await env.demand(A_PATH, CELL, target=False)
    row = await env.session.get_generation(A_PATH, CELL)
    assert row is not None and row[2] == "complete"  # ran fine; never served

    await env.target(A_PATH, CELL)
    assert await env.demand(A_PATH, CELL, target=False) != {}
    assert task.calls == 2  # the one completed run: main + safety replay


# -- 6. The structural mint gate's grounding-enriched diagnosis -----------------


@pytest.mark.anyio
async def test_mint_gate_diagnosis(tmp_path: Path) -> None:
    """Grant minting against a missing, then config-drifted, rule reference
    faults immediately with the grid's grounding diagnosis appended."""
    state, ents = _paths(tmp_path)
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            await inputs.write(_some_entity().store().entity_id, x="a")
            await _bind(wf, host, fns, "echo", SurfaceEchoTask())
            t = await wf.root.open_task(
                "t", axes=(), task_fn=fns.at(fn="echo"), args={"inp": inputs.read_only()}
            )

            # Deleted dep: the mint failure names the grid's verdict.
            await wf.delete(wf.root, "inputs")
            with pytest.raises(CellFaultError, match="unanchored") as ei:
                await wf.demand(t.output_entity())
            assert "inputs" in str(ei.value)

            # Config-drifted dep: recreated with a different *shape* (axis
            # renames alone are not structural), the rule no longer mints —
            # same immediate, enriched diagnosis.
            await wf.root.open_data("inputs", (m.AxisSpec("x", "str"), m.AxisSpec("z", "str")))
            with pytest.raises(CellFaultError, match="unanchored") as ei:
                await wf.demand(t.output_entity())
            assert "inputs" in str(ei.value)


# -- 7. The knob is pure policy: behavior flips, stored state doesn't -----------


@pytest.mark.anyio
async def test_off_knob_pure_policy(tmp_path: Path) -> None:
    """The same durable state across three reopens: strict faults the
    ungrounded serve and read, ``off`` serves both, strict faults again —
    and the verdicts (maintained regardless of policy) read identically
    ungrounded throughout."""
    state, ents = _paths(tmp_path)
    task = SurfaceEchoTask()
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            v1 = _some_entity().store().entity_id
            await inputs.write(v1, x="a")
            await _bind(wf, host, fns, "echo", task)
            t = await wf.root.open_task(
                "t", axes=(), task_fn=fns.at(fn="echo"), args={"inp": inputs.read_only()}
            )
            assert await wf.demand(t.output_entity()) == v1
            # The ungrounded exhibit: revoke the input's sourcing.
            assert await wf.revoke_cell_sourcing(inputs, x="a") is True
            with pytest.raises(UngroundedSubjectError):
                await wf.demand(t.output_entity())
            with pytest.raises(UngroundedSubjectError):
                await inputs.read(x="a")

        # Same files, policy off: both serve (no runs — no bindings
        # needed), and the verdicts still label the state ungrounded.
        async with open_local(state, ents, grounding_policy="off") as wf2:
            t2 = await wf2.attach(wf2.root, "t")
            assert isinstance(t2, TaskView)
            inputs2 = await wf2.attach(wf2.root, "inputs")
            assert isinstance(inputs2, DataView)
            assert await wf2.demand(t2.output_entity()) == v1
            assert await inputs2.read(x="a") == v1
            report = await wf2.inspect(t2)
            assert report.currency == "fresh" and report.grounding == "unanchored"
            assert (await wf2.inspect(inputs2, x="a")).grounding == "orphaned"

        # Strict again: the off session changed nothing durable — the same
        # faults, the same verdicts.
        async with open_local(state, ents) as wf3:
            t3 = await wf3.attach(wf3.root, "t")
            assert isinstance(t3, TaskView)
            inputs3 = await wf3.attach(wf3.root, "inputs")
            assert isinstance(inputs3, DataView)
            with pytest.raises(UngroundedSubjectError):
                await wf3.demand(t3.output_entity())
            with pytest.raises(UngroundedSubjectError):
                await inputs3.read(x="a")
            assert (await wf3.inspect(t3)).grounding == "unanchored"
