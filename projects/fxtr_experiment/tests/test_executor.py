"""Executor tests: the first real end-to-end path (demand → task fn behind
ServiceRefs → record-checked ops → durable results), plus the
execution-status policy rows and terminal-status derivation from the
failure set."""

from pathlib import Path
from typing import AsyncIterator

import anyio
import pytest
from frozendict import frozendict

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope

from fxtr_experiment import encoding as enc
from fxtr_experiment_task import model as m
from fxtr_experiment_task.errors import CellFaultError
from fxtr_experiment.executor import Executor
from fxtr_experiment.state_store import WorkflowSession
from fxtr_experiment.storage import (
    FAILURE_ERROR,
    FAILURE_FAULT,
    OBS_KIND_CELL_VALUE,
    REQ_KIND_DATA,
)
from fxtr_experiment.bindings import BindingHost, BindingRelease, wrap_singleton
from fxtr_experiment_task.task_adapter import TaskFnAdapter

from .test_ops_engine import _some_entity

FNS = enc.encode_key_path(["fns"])
INPUTS = enc.encode_key_path(["inputs"])
A_PATH = enc.encode_key_path(["a"])
B_PATH = enc.encode_key_path(["b"])
CELL = enc.encode_coords(())


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Env:
    """One session + runner scope + bindings + executor.

    ``launcher`` / ``sandbox_repo`` pass through to the `BindingHost`
    (the sandbox-serving suite injects its fake launcher here)."""

    def __init__(
        self,
        session: WorkflowSession,
        runner_scope: FxtrScope,
        *,
        launcher: object = None,
        sandbox_repo: Path | None = None,
    ) -> None:
        self.session = session
        self.runner_scope = runner_scope
        self.bindings = BindingHost(
            runner_scope,
            session,
            launcher=launcher,  # type: ignore[arg-type]
            sandbox_repo=sandbox_repo,
        )
        self.executor = Executor(session, self.bindings, runner_scope)

    async def _source_grid(self, grid_path: str) -> FxtrID:
        """Assert grid-level intent (a user sourcing record pinned to the
        grid's current config), returning that config id. Every grid these
        tests seed at session level is user-authored, so under the strict
        grounding gate it needs this record to ground (`notes/grounding.md`,
        "maintaining the root facts"); real user creates auto-upsert it."""
        entry = await self.session.get_grid(grid_path)
        assert entry is not None
        await self.session.upsert_user_sourcing(grid_path, "", entry[1])
        return entry[1]

    async def seed_base(self) -> None:
        await self.session.create_grid(
            FNS,
            "handle",
            enc.GridConfigEntity(config=m.HandleGridConfig(axes=(m.AxisSpec("fn", "str"),))),
        )
        await self._source_grid(FNS)
        await self.session.create_grid(
            INPUTS,
            "data",
            enc.GridConfigEntity(config=m.DataGridConfig(axes=(m.AxisSpec("x", "str"),))),
        )
        await self._source_grid(INPUTS)

    async def write_data(self, grid_path: str, coords: str, value: object) -> FxtrID:
        """Write a data/handle cell and source the value (writing *is*
        sourcing — the user-write auto-upsert of `notes/grounding.md`), so
        the run's gated read of it grounds. Re-sourcing on every write keeps
        the pin tracking the current value across overwrites/reverts."""
        value_id = await self.session.write_cell(grid_path, coords, value)  # type: ignore[arg-type]
        await self.session.upsert_user_sourcing(grid_path, coords, value_id)
        return value_id

    async def bind_fn(self, name: str, task: object) -> FxtrID:
        """Write a distinct handle entity at fns[name] and bind ``task`` to it."""
        key = (
            enc.SliceTemplateEntity(template=m.SliceTemplate(path=(), target_axes=(), output=name))
            .store()
            .entity_id
        )
        await self.write_data(FNS, enc.encode_coords((name,)), key)
        ref = await self.runner_scope.mount(TaskFnAdapter(task))  # type: ignore[arg-type]
        connector = await wrap_singleton(self.runner_scope, ref)
        await self.bindings.bind(key, connector, connector_scope=self.runner_scope)
        return key

    async def create_task_grid(
        self, path: str, fn_name: str, inputs: dict[str, m.SliceBinding]
    ) -> None:
        config = m.TaskGridConfig(
            axes=(),
            rule=m.TaskRule(
                task_fn=m.SliceTemplate(
                    path=(m.SubgridStepTemplate(cell=(), key="fns"),),
                    target_axes=(m.LiteralCoord(fn_name),),
                ),
                inputs=frozendict(inputs),
            ),
            outputs=m.DEFAULT_TASK_OUTPUTS,
        )
        await self.session.create_grid(path, "task", enc.GridConfigEntity(config=config))
        # A directly-seeded task grid grounds only via user sourcing (no
        # running generation asserted it); this also satisfies the
        # grid-before-cell invariant so a target can attach to its cells.
        await self._source_grid(path)

    async def target(self, grid_path: str, coords: str) -> None:
        """Attach a target on a task cell (the demand-requires-target rule).

        Grounding derives wantedness from the denormalized grid_path/coords
        and expected_config == the grid's current config (`grounding.py`),
        so the slice entity is just a stable, pinned identity here."""
        entry = await self.session.get_grid(grid_path)
        assert entry is not None
        slice_id = enc.store_slice(
            m.SliceTemplate(
                path=tuple(
                    m.SubgridStepTemplate(cell=(), key=k) for k in enc.decode_key_path(grid_path)
                ),
                target_axes=(),
            ),
            scope=self.runner_scope,
        )
        await self.session.add_target(slice_id, entry[1], grid_path, coords)

    async def demand(
        self, grid_path: str, coords: str, *, target: bool = True
    ) -> dict[str, FxtrID]:
        """Auto-target then demand — the `Workflow` wrapper's default. Pass
        ``target=False`` for a cell that must be reached only via the
        in-flight demand chain (its demander vouches it)."""
        if target:
            await self.target(grid_path, coords)
        return await self.executor.demand(grid_path, coords)


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    async with WorkflowSession.open(
        str(tmp_path / "state.sqlite"), str(tmp_path / "entities.sqlite")
    ) as session:
        async with FxtrScope(name="wf3-runner", include=[session.entity_scope]) as runner_scope:
            e = Env(session, runner_scope)
            await e.seed_base()
            yield e


INPUT_BINDING = m.SliceBinding(
    slice=m.SliceTemplate(
        path=(m.SubgridStepTemplate(cell=(), key="inputs"),),
        target_axes=(m.KeepAxis(),),
    ),
    mode="read",
    received_axis_names=("x",),
)
A_OUTPUT_BINDING = m.SliceBinding(
    slice=m.SliceTemplate(
        path=(m.SubgridStepTemplate(cell=(), key="a"),),
        target_axes=(),
        output="result",
    ),
    mode="read",
    received_axis_names=(),
)


class EchoTask:
    """Reads inputs[x='a'], opens a scratch internals subtree, echoes."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, ctx, /, *, inp):
        self.calls += 1
        value = await inp.read(x="a")
        await ctx.own_internals().open_subtree("scratch", axes=())
        return value


class RaisesTask:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, ctx, /):
        self.calls += 1
        raise RuntimeError("task exploded")


class CatchesTask:
    """Catches its input fault and returns a valid value anyway."""

    async def __call__(self, ctx, /, *, inp):
        try:
            return await inp.read(x="a")
        except Exception:
            return _some_entity().store().entity_id


class ForwardTask:
    """Echoes its upstream input — which arrives as a plain *data view*
    (the binding pre-selected task A's "result" output; the callee never
    sees a task view)."""

    async def __call__(self, ctx, /, *, up):
        return await up.read()


@pytest.mark.anyio
async def test_first_end_to_end(env: Env) -> None:
    """Demand → task fn behind ServiceRefs → record-checked ops → durable
    results; a second demand serves without re-running."""
    task = EchoTask()
    await env.bind_fn("echo", task)
    v1 = await env.write_data(INPUTS, '["a"]', _some_entity())
    await env.create_task_grid(A_PATH, "echo", {"inp": INPUT_BINDING})

    served = await env.demand(A_PATH, CELL)
    assert served == {"result": v1}  # the validated (value-pinned) results
    row = await env.session.get_generation(A_PATH, CELL)
    assert row is not None and row[2] == "complete"
    assert await env.session.get_results(row[0]) == {"result": v1}
    assert await env.session.load_failures(row[0]) == ()
    # The record holds the prologue fn observation + the input read.
    subjects = {(r[0], r[1]) for r in await env.session.load_record(row[1])}
    assert (FNS, '["echo"]') in subjects
    assert (INPUTS, '["a"]') in subjects
    # Serving a complete cell does not re-run (and serves the same fact).
    assert await env.demand(A_PATH, CELL) == {"result": v1}
    assert task.calls == 2  # the one completed run: main run + safety replay


@pytest.mark.anyio
async def test_missing_fn_faults_without_minting(env: Env) -> None:
    """A missing task-fn cell is a pre-run fault: nothing is attempted and
    no generation state is created."""
    await env.create_task_grid(A_PATH, "ghost", {})
    with pytest.raises(CellFaultError):
        await env.demand(A_PATH, CELL)
    assert await env.session.get_generation(A_PATH, CELL) is None


@pytest.mark.anyio
async def test_task_error_then_demand_retries_once(env: Env) -> None:
    """A task raising its own error ends errored (TaskRaisedError failure);
    each later demand performs exactly one retry run."""
    task = RaisesTask()
    await env.bind_fn("boom", task)
    await env.create_task_grid(A_PATH, "boom", {})

    with pytest.raises(CellFaultError):
        await env.demand(A_PATH, CELL)
    assert task.calls == 1
    row = await env.session.get_generation(A_PATH, CELL)
    assert row is not None and row[2] == "errored"
    (failure,) = await env.session.load_failures(row[0])
    assert failure[0] == FAILURE_ERROR and failure[1] == "TaskRaisedError"

    with pytest.raises(CellFaultError):  # the demand IS the retry; fails again
        await env.demand(A_PATH, CELL)
    assert task.calls == 2
    row = await env.session.get_generation(A_PATH, CELL)
    assert row is not None and row[2] == "errored"


@pytest.mark.anyio
async def test_interrupted_then_resume_completes(env: Env) -> None:
    """A missing input interrupts (fault-class failure); once the input
    exists, re-demanding resumes the same generation to completion."""
    task = EchoTask()
    await env.bind_fn("echo", task)
    await env.create_task_grid(A_PATH, "echo", {"inp": INPUT_BINDING})

    with pytest.raises(CellFaultError):
        await env.demand(A_PATH, CELL)
    row = await env.session.get_generation(A_PATH, CELL)
    assert row is not None and row[2] == "interrupted"
    (failure,) = await env.session.load_failures(row[0])
    assert failure[0] == FAILURE_FAULT and failure[1] == "CellFaultError"
    generation = row[1]

    v1 = await env.write_data(INPUTS, '["a"]', _some_entity())
    await env.demand(A_PATH, CELL)
    row = await env.session.get_generation(A_PATH, CELL)
    assert row is not None and row[2] == "complete"
    assert row[1] == generation  # the SAME generation resumed
    assert await env.session.get_results(row[0]) == {"result": v1}
    assert await env.session.load_failures(row[0]) == ()  # evidence cleared
    assert task.calls == 3  # failed run + (resumed run + safety replay)


@pytest.mark.anyio
async def test_catch_and_return_ends_interrupted_without_results(env: Env) -> None:
    """The poisoning contract through the whole executor: a task that
    catches its input fault and returns a valid value still ends
    interrupted, and its returned results are NOT persisted."""
    await env.bind_fn("catches", CatchesTask())
    await env.create_task_grid(A_PATH, "catches", {"inp": INPUT_BINDING})
    with pytest.raises(CellFaultError):
        await env.demand(A_PATH, CELL)
    row = await env.session.get_generation(A_PATH, CELL)
    assert row is not None and row[2] == "interrupted"
    assert await env.session.get_results(row[0]) == {}


@pytest.mark.anyio
async def test_transitive_demand_runs_upstream_first(env: Env) -> None:
    """Task B reads task A's output: demanding B executes A depth-first
    through the demand hook, and both land complete."""
    a_task = EchoTask()
    await env.bind_fn("echo", a_task)
    v1 = await env.write_data(INPUTS, '["a"]', _some_entity())
    await env.create_task_grid(A_PATH, "echo", {"inp": INPUT_BINDING})
    await env.bind_fn("fwd", ForwardTask())
    await env.create_task_grid(B_PATH, "fwd", {"up": A_OUTPUT_BINDING})

    await env.demand(B_PATH, CELL)
    for path in (A_PATH, B_PATH):
        row = await env.session.get_generation(path, CELL)
        assert row is not None and row[2] == "complete"
        assert await env.session.get_results(row[0]) == {"result": v1}
    assert a_task.calls == 2  # A's one completed run: main + safety replay


OTHER = enc.SliceTemplateEntity(template=m.SliceTemplate(path=(), target_axes=(), output="v2"))


@pytest.mark.anyio
async def test_stale_clear_rerun_and_revert_resurrection(env: Env) -> None:
    """The currency battery: overwrite → fault (nothing discarded); revert
    → resurrection (same generation serves); clear → a fresh generation
    computes the new value."""
    task = EchoTask()
    await env.bind_fn("echo", task)
    v1 = await env.write_data(INPUTS, '["a"]', _some_entity())
    await env.create_task_grid(A_PATH, "echo", {"inp": INPUT_BINDING})
    await env.demand(A_PATH, CELL)
    row = await env.session.get_generation(A_PATH, CELL)
    assert row is not None

    v2 = await env.write_data(INPUTS, '["a"]', OTHER)  # user overwrite
    with pytest.raises(CellFaultError, match="stale"):
        await env.demand(A_PATH, CELL)
    after = await env.session.get_generation(A_PATH, CELL)
    assert after is not None and after[2] == "complete"  # nothing discarded

    await env.write_data(INPUTS, '["a"]', v1)  # revert → resurrection
    await env.demand(A_PATH, CELL)  # serves, no re-run
    assert task.calls == 2  # the one completed run: main + safety replay

    await env.write_data(INPUTS, '["a"]', v2)  # overwrite again
    with pytest.raises(CellFaultError, match="stale"):
        await env.demand(A_PATH, CELL)
    await env.session.delete_generation(row[0])  # the user's explicit clear
    await env.demand(A_PATH, CELL)  # a fresh generation runs
    fresh = await env.session.get_generation(A_PATH, CELL)
    assert fresh is not None and fresh[1] != row[1] and fresh[2] == "complete"
    assert await env.session.get_results(fresh[0]) == {"result": v2}
    assert task.calls == 4  # two completed runs, each main + safety replay


@pytest.mark.anyio
async def test_unsettled_pipeline_resolves_both_ways(env: Env) -> None:
    """B observed A's output; A's input changes: A is stale but B is only
    *unsettled* — condemned or resurrected by how A's rerun resolves."""
    a_task = EchoTask()
    await env.bind_fn("echo", a_task)
    v1 = await env.write_data(INPUTS, '["a"]', _some_entity())
    await env.create_task_grid(A_PATH, "echo", {"inp": INPUT_BINDING})
    await env.bind_fn("fwd", ForwardTask())
    await env.create_task_grid(B_PATH, "fwd", {"up": A_OUTPUT_BINDING})
    await env.demand(B_PATH, CELL)

    v2 = await env.write_data(INPUTS, '["a"]', OTHER)
    a_report = await env.session.cell_currency(A_PATH, CELL)
    b_report = await env.session.cell_currency(B_PATH, CELL)
    assert a_report is not None and a_report.currency == "stale"
    assert b_report is not None and b_report.currency == "unsettled"
    with pytest.raises(CellFaultError, match="unsettled"):
        await env.demand(B_PATH, CELL)

    # Resolve-to-fresh: revert A's input — the whole pipeline resurrects.
    await env.write_data(INPUTS, '["a"]', v1)
    await env.demand(B_PATH, CELL)  # serves; nothing re-ran
    assert a_task.calls == 2  # A's one completed run: main + safety replay

    # Resolve-to-stale: change the input, clear + re-run A → different
    # output → B is now definitely stale (not unsettled).
    await env.write_data(INPUTS, '["a"]', v2)
    a_row = await env.session.get_generation(A_PATH, CELL)
    assert a_row is not None
    await env.session.delete_generation(a_row[0])
    await env.demand(A_PATH, CELL)
    b_report = await env.session.cell_currency(B_PATH, CELL)
    assert b_report is not None and b_report.currency == "stale"
    # Clearing B lets the pipeline converge on the new value.
    b_row = await env.session.get_generation(B_PATH, CELL)
    assert b_row is not None
    await env.session.delete_generation(b_row[0])
    await env.demand(B_PATH, CELL)
    b_row = await env.session.get_generation(B_PATH, CELL)
    assert b_row is not None
    assert await env.session.get_results(b_row[0]) == {"result": v2}


@pytest.mark.anyio
async def test_grid_config_staleness_and_path_identity_resurrection(env: Env) -> None:
    """Deleting a grid the task opened stales it (a settled user action);
    recreating it identically at the same keys resurrects the observer
    (path-keyed identity: same canonical config → same fingerprint)."""
    task = EchoTask()
    await env.bind_fn("echo", task)
    await env.write_data(INPUTS, '["a"]', _some_entity())
    await env.create_task_grid(A_PATH, "echo", {"inp": INPUT_BINDING})
    await env.demand(A_PATH, CELL)

    scratch = enc.encode_key_path(["a", "scratch"])
    await env.session.delete_grid(scratch)
    with pytest.raises(CellFaultError, match="stale"):
        await env.demand(A_PATH, CELL)
    await env.session.create_grid(
        scratch, "subtree", enc.GridConfigEntity(config=m.SubtreeGridConfig(axes=()))
    )
    await env.demand(A_PATH, CELL)  # resurrection: serves again
    assert task.calls == 2  # the one completed run: main + safety replay


@pytest.mark.anyio
async def test_stale_on_arrival(env: Env) -> None:
    """v0 lets a running-but-stale generation finish: the result lands
    coherently (computed from the observed snapshot) — but the demand
    never returns it as fresh."""
    read_done = anyio.Event()
    gate = anyio.Event()

    class BlockingEcho:
        async def __call__(self, ctx, /, *, inp):
            value = await inp.read(x="a")
            read_done.set()
            await gate.wait()
            return value

    v1 = await env.write_data(INPUTS, '["a"]', _some_entity())
    await env.bind_fn("block", BlockingEcho())
    await env.create_task_grid(A_PATH, "block", {"inp": INPUT_BINDING})

    async with anyio.create_task_group() as tg:

        async def demander() -> None:
            with pytest.raises(CellFaultError, match="stale"):
                await env.demand(A_PATH, CELL)

        tg.start_soon(demander)
        await read_done.wait()  # the run observed v1
        await env.write_data(INPUTS, '["a"]', OTHER)  # mid-run edit
        gate.set()
    # The generation finished and its (coherent) result landed durably...
    row = await env.session.get_generation(A_PATH, CELL)
    assert row is not None and row[2] == "complete"
    assert await env.session.get_results(row[0]) == {"result": v1}
    # ...but every demand faults stale until cleared or reverted.
    with pytest.raises(CellFaultError, match="stale"):
        await env.demand(A_PATH, CELL)
    await env.write_data(INPUTS, '["a"]', v1)  # revert → fresh again
    await env.demand(A_PATH, CELL)


@pytest.mark.anyio
async def test_binding_vanishing_before_safety_pass_interrupts(env: Env) -> None:
    """A fault-class escape in the pass's *setup* (the task-fn binding
    released between the run and the pass — the pass re-invokes the task
    fn, so its spawn shares a run's not-ready surface) lands the
    generation interrupted with fault-class evidence, never errored as
    nondeterminism (the shared escape rule); a re-bind + re-demand
    resumes the same generation to completion."""

    class ReleasesOwnBinding:
        """Sabotages its own binding mid-run: the run itself completes,
        but the safety pass's task-fn spawn finds the binding gone."""

        def __init__(self) -> None:
            self.calls = 0
            self.release: BindingRelease | None = None  # set by the test after binding

        async def __call__(self, ctx, /):
            self.calls += 1
            if self.release is not None:
                self.release.release()
                self.release = None
            return _some_entity().store().entity_id

    task = ReleasesOwnBinding()
    key = (
        enc.SliceTemplateEntity(template=m.SliceTemplate(path=(), target_axes=(), output="sab"))
        .store()
        .entity_id
    )
    await env.write_data(FNS, enc.encode_coords(("sab",)), key)
    ref = await env.runner_scope.mount(TaskFnAdapter(task))  # type: ignore[arg-type]
    connector = await wrap_singleton(env.runner_scope, ref)
    task.release = await env.bindings.bind(key, connector, connector_scope=env.runner_scope)
    await env.create_task_grid(A_PATH, "sab", {})

    with pytest.raises(CellFaultError):
        await env.demand(A_PATH, CELL)
    assert task.calls == 1  # the run ran; the pass's spawn faulted before invoking
    row = await env.session.get_generation(A_PATH, CELL)
    assert row is not None and row[2] == "interrupted"
    (failure,) = await env.session.load_failures(row[0])
    assert failure[0] == FAILURE_FAULT and failure[1] == "HandleUnboundError"
    generation = row[1]

    # Re-bind (the remediation) and re-demand: the SAME generation
    # resumes — the run replays to hits — and completes through the pass.
    connector2 = await wrap_singleton(env.runner_scope, ref)
    await env.bindings.bind(key, connector2, connector_scope=env.runner_scope)
    served = await env.demand(A_PATH, CELL)
    assert served == {"result": _some_entity().store().entity_id}
    row = await env.session.get_generation(A_PATH, CELL)
    assert row is not None and row[2] == "complete" and row[1] == generation
    assert task.calls == 3  # sabotaged run + (resumed run + safety replay)


@pytest.mark.anyio
async def test_safety_pass_catches_divergent_reads(env: Env) -> None:
    """A task whose replay reads a different subject hits the frozen
    frontier: the generation ends errored with a NondeterminismError
    failure, never complete."""

    class ReadsDifferentCells:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, ctx, /, *, inp):
            self.calls += 1
            return await inp.read(x="a" if self.calls == 1 else "b")

    await env.bind_fn("flaky", ReadsDifferentCells())
    await env.write_data(INPUTS, '["a"]', _some_entity())
    await env.write_data(INPUTS, '["b"]', OTHER)
    await env.create_task_grid(A_PATH, "flaky", {"inp": INPUT_BINDING})
    with pytest.raises(CellFaultError, match="errored"):
        await env.demand(A_PATH, CELL)
    row = await env.session.get_generation(A_PATH, CELL)
    assert row is not None and row[2] == "errored"
    failures = await env.session.load_failures(row[0])
    assert any(cls == "NondeterminismError" and "frontier" in msg for _, cls, msg in failures)


@pytest.mark.anyio
async def test_safety_pass_catches_omitted_ops(env: Env) -> None:
    """A task whose replay skips a recorded op (it memoized its own answer)
    fails the op-set comparison."""

    class MemoizesItsRead:
        def __init__(self) -> None:
            self.value = None

        async def __call__(self, ctx, /, *, inp):
            if self.value is None:
                self.value = await inp.read(x="a")
            return self.value

    await env.bind_fn("memo", MemoizesItsRead())
    await env.write_data(INPUTS, '["a"]', _some_entity())
    await env.create_task_grid(A_PATH, "memo", {"inp": INPUT_BINDING})
    with pytest.raises(CellFaultError, match="errored"):
        await env.demand(A_PATH, CELL)
    row = await env.session.get_generation(A_PATH, CELL)
    assert row is not None and row[2] == "errored"
    failures = await env.session.load_failures(row[0])
    assert any(
        cls == "NondeterminismError" and "recorded-but-not-replayed" in msg
        for _, cls, msg in failures
    )


@pytest.mark.anyio
async def test_safety_pass_catches_divergent_outputs(env: Env) -> None:
    """A task whose replay does the same ops but returns a different value
    fails the output comparison."""

    class FreshEntityEachTime:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, ctx, /, *, inp):
            await inp.read(x="a")
            self.calls += 1
            return (
                enc.SliceTemplateEntity(
                    template=m.SliceTemplate(path=(), target_axes=(), output=str(self.calls))
                )
                .store()
                .entity_id
            )

    await env.bind_fn("fresh", FreshEntityEachTime())
    await env.write_data(INPUTS, '["a"]', _some_entity())
    await env.create_task_grid(A_PATH, "fresh", {"inp": INPUT_BINDING})
    with pytest.raises(CellFaultError, match="errored"):
        await env.demand(A_PATH, CELL)
    row = await env.session.get_generation(A_PATH, CELL)
    assert row is not None and row[2] == "errored"
    failures = await env.session.load_failures(row[0])
    assert any(
        cls == "NondeterminismError" and "different outputs" in msg for _, cls, msg in failures
    )


@pytest.mark.anyio
async def test_safety_pass_rejects_skipped_request_on_resume(env: Env) -> None:
    """Every durable per-generation fact is a replay obligation: a request
    that faulted on the first attempt leaves its edge (correctly — the
    resume re-requests at the frontier), so a resumed run that *skips* the
    request and completes anyway is nondeterministic, and the safety
    pass's request-edge comparison rejects the generation. Without that
    comparison the dangling edge would survive into a complete generation
    as a phantom ``wanted`` fact."""

    class SkipsAfterFault:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, ctx, /, *, inp):
            self.calls += 1
            if self.calls == 1:
                return await inp.read(x="a")  # missing → faults; edge durable
            return _some_entity().store().entity_id  # skips the request

    await env.bind_fn("skipper", SkipsAfterFault())
    await env.create_task_grid(A_PATH, "skipper", {"inp": INPUT_BINDING})
    with pytest.raises(CellFaultError):
        await env.demand(A_PATH, CELL)  # attempt 1: no input value
    row = await env.session.get_generation(A_PATH, CELL)
    assert row is not None and row[2] == "interrupted"
    # The aborted request's edge is durable although no observation is.
    assert (INPUTS, '["a"]', REQ_KIND_DATA) in set(await env.session.load_request_edges(row[0]))

    with pytest.raises(CellFaultError, match="errored"):
        await env.demand(A_PATH, CELL)  # attempt 2 completes... rejected
    row2 = await env.session.get_generation(A_PATH, CELL)
    assert row2 is not None and row2[2] == "errored"
    failures = await env.session.load_failures(row2[0])
    assert any(
        cls == "NondeterminismError" and "request-edge mismatch" in msg for _, cls, msg in failures
    )


@pytest.mark.anyio
async def test_safety_pass_accepts_refetched_request_on_resume(env: Env) -> None:
    """The passing twin: the resume re-requests at the frontier (the edge
    insert is idempotent), the edge set converges to the record's
    read-class projection, and the pass is clean."""
    task = EchoTask()
    await env.bind_fn("echo", task)
    await env.create_task_grid(A_PATH, "echo", {"inp": INPUT_BINDING})
    with pytest.raises(CellFaultError):
        await env.demand(A_PATH, CELL)  # attempt 1: no input value
    row = await env.session.get_generation(A_PATH, CELL)
    assert row is not None and row[2] == "interrupted"
    assert (INPUTS, '["a"]', REQ_KIND_DATA) in set(await env.session.load_request_edges(row[0]))

    v1 = await env.write_data(INPUTS, '["a"]', _some_entity())
    assert await env.demand(A_PATH, CELL) == {"result": v1}
    row2 = await env.session.get_generation(A_PATH, CELL)
    assert row2 is not None and row2[2] == "complete" and row2[1] == row[1]
    # Convergence at completion: requests ⇔ the record's read projection.
    reads = {
        (g, c)
        for g, c, _out, _fp, kind in await env.session.load_record(row2[1])
        if kind == OBS_KIND_CELL_VALUE
    }
    assert {(g, c) for g, c, _k in await env.session.load_request_edges(row2[0])} == reads


GHOST_BINDING = m.SliceBinding(
    slice=m.SliceTemplate(
        path=(m.SubgridStepTemplate(cell=(), key="ghost"),),
        target_axes=(m.KeepAxis(),),
    ),
    mode="read",
    received_axis_names=("x",),
)
AUX = enc.encode_key_path(["aux"])
AUX_BINDING = m.SliceBinding(
    slice=m.SliceTemplate(
        path=(m.SubgridStepTemplate(cell=(), key="aux"),),
        target_axes=(m.KeepAxis(),),
    ),
    mode="read",
    received_axis_names=("x",),
)


@pytest.mark.anyio
async def test_missing_rule_grid_faults_without_minting(env: Env) -> None:
    """A rule referencing a missing grid is a pre-run fault, like a missing
    task fn: grants mint before the generation row is touched, so nothing
    is attempted and no generation state is created."""
    await env.bind_fn("echo", EchoTask())
    await env.create_task_grid(A_PATH, "echo", {"inp": GHOST_BINDING})
    with pytest.raises(CellFaultError, match="no grid"):
        await env.demand(A_PATH, CELL)
    assert await env.session.get_generation(A_PATH, CELL) is None


@pytest.mark.anyio
async def test_missing_rule_grid_on_resume_preserves_evidence(env: Env) -> None:
    """Re-demanding an interrupted generation whose rule references a
    since-deleted grid — one the failed run never *observed* (holding a
    grant records nothing), so the generation is still fresh — faults
    pre-run: the generation stays interrupted and the prior attempt's
    failure evidence is not cleared."""

    class ReadsOnlyInp:
        async def __call__(self, ctx, /, *, inp, aux):
            return await inp.read(x="a")

    await env.session.create_grid(
        AUX, "data", enc.GridConfigEntity(config=m.DataGridConfig(axes=(m.AxisSpec("x", "str"),)))
    )
    await env.bind_fn("ro", ReadsOnlyInp())
    await env.create_task_grid(A_PATH, "ro", {"inp": INPUT_BINDING, "aux": AUX_BINDING})
    with pytest.raises(CellFaultError):  # no input value → interrupted
        await env.demand(A_PATH, CELL)
    row = await env.session.get_generation(A_PATH, CELL)
    assert row is not None and row[2] == "interrupted"

    await env.session.delete_grid(AUX)
    await env.write_data(INPUTS, '["a"]', _some_entity())  # unblock the input
    with pytest.raises(CellFaultError, match="no grid"):
        await env.demand(A_PATH, CELL)
    after = await env.session.get_generation(A_PATH, CELL)
    assert after is not None and after[2] == "interrupted"  # not stranded running
    assert after[1] == row[1]  # the same generation, untouched
    (failure,) = await env.session.load_failures(after[0])  # evidence intact
    assert failure[0] == FAILURE_FAULT and failure[1] == "CellFaultError"


@pytest.mark.anyio
async def test_deleted_input_grid_stales_observers_interpretably(env: Env) -> None:
    """The full lifecycle of deleting a rule-referenced input grid the task
    *observed*: the cell goes stale with a "was deleted" diagnosis, demands
    fault carrying that same reason, and a rerun after the user's clear
    faults pre-run naming the missing grid, with nothing minted. (The
    unobserved-grant counterpart — the generation stays fresh and serves —
    is the resume-preserves-evidence test above.)"""
    task = EchoTask()
    await env.bind_fn("echo", task)
    await env.write_data(INPUTS, '["a"]', _some_entity())
    await env.create_task_grid(A_PATH, "echo", {"inp": INPUT_BINDING})
    await env.demand(A_PATH, CELL)

    await env.session.delete_grid(INPUTS)
    report = await env.session.cell_currency(A_PATH, CELL)
    assert report is not None and report.currency == "stale"
    assert report.reason == 'observed cell ["inputs"]["a"] was deleted'
    with pytest.raises(CellFaultError, match="was deleted"):
        await env.demand(A_PATH, CELL)

    row = await env.session.get_generation(A_PATH, CELL)
    assert row is not None
    await env.session.delete_generation(row[0])  # the user's explicit clear
    with pytest.raises(CellFaultError, match="no grid"):
        await env.demand(A_PATH, CELL)
    assert await env.session.get_generation(A_PATH, CELL) is None  # nothing minted
