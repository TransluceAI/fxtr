"""Executor step tests: resume-mid-task through the public demand path —
cancel-based and crash-simulated checkpoint resume, connect poisoning end
to end (bind + re-demand recovery), and the safety pass catching a
nondeterministic step invocation. Engine-level step behavior (duplicate
calls, payload rejection, direct replay) is `test_run_step.py`'s."""

import uuid
from pathlib import Path
from typing import AsyncIterator

import anyio
import pytest

from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.system_entities import define_type

from fxtr_experiment import encoding as enc
from fxtr_experiment_task import model as m
from fxtr_experiment_task.errors import CellFaultError
from fxtr_experiment.state_store import WorkflowSession
from fxtr_experiment.storage import FAILURE_ERROR, FAILURE_FAULT
from fxtr_experiment.bindings import wrap_singleton
from fxtr_experiment_task.view_runtime import step_fn

from .test_executor import CELL, FNS, INPUT_BINDING, INPUTS, Env
from .test_ops_engine import _some_entity
from .test_run_step import PingImpl, PingProto

_TEST_NS = uuid.UUID("3f9a1c2e-8b4d-4e6f-9a70-5d2c8e1f6b03")
T_PATH = enc.encode_key_path(["t"])

# A read view of the whole `fns` handle grid, as a rule input (the task
# reads keys from it; the prologue's own fn read targets the same grid —
# both are reads, so read-xor-write is satisfied).
FNS_BINDING = m.SliceBinding(
    slice=m.SliceTemplate(
        path=(m.SubgridStepTemplate(cell=(), key="fns"),),
        target_axes=(m.KeepAxis(),),
    ),
    mode="read",
    received_axis_names=("fn",),
)


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


@pytest.mark.anyio
async def test_cancel_mid_step_then_resume_replays_completed_steps(env: Env) -> None:
    """The flagship, cancel-based: a 3-step task is cancelled mid-step-2 →
    interrupted (step 2's checkpoint unwritten); re-demanding resumes the
    same generation — step 1 is served from its checkpoint (its fn ran
    exactly once overall), steps 2–3 run, and the task completes. The
    safety pass doubles task-fn invocations, never step invocations."""
    step_calls = {"1": 0, "2": 0, "3": 0}
    step2_entered = anyio.Event()
    gate = anyio.Event()

    @step_fn(type_id=define_type(_TEST_NS, "gated_step"))
    async def gated(ctx, *, i):
        step_calls[i] += 1
        if i == "2" and not gate.is_set():
            step2_entered.set()
            await gate.wait()
        return f"done-{i}"

    class ThreeSteps:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, ctx, /, *, inp):
            self.calls += 1
            value = await inp.read(x="a")
            ckpt = await ctx.own_internals().open_checkpoint(
                "steps", axes=(m.AxisSpec("i", "str"),)
            )
            for i in ("1", "2", "3"):
                assert await ckpt.run_step(gated, args={"i": i}, coords={"i": i}) == f"done-{i}"
            return value

    task = ThreeSteps()
    await env.bind_fn("threestep", task)
    v1 = await env.write_data(INPUTS, '["a"]', _some_entity())
    await env.create_task_grid(T_PATH, "threestep", {"inp": INPUT_BINDING})

    async with anyio.create_task_group() as tg:

        async def demander() -> None:
            await env.demand(T_PATH, CELL)

        tg.start_soon(demander)
        await step2_entered.wait()
        tg.cancel_scope.cancel()

    row = await env.session.get_generation(T_PATH, CELL)
    assert row is not None and row[2] == "interrupted"
    failures = await env.session.load_failures(row[0])
    assert any(kind == FAILURE_FAULT for kind, _, _ in failures)
    assert step_calls == {"1": 1, "2": 1, "3": 0}
    # Step 1's checkpoint is durable; step 2's was never written (the
    # cancellation cut the miss path off before persist-then-record).
    ckpt_path = enc.encode_key_path(["t", "steps"])
    assert await env.session.get_checkpoint(ckpt_path, '["1"]') is not None
    assert await env.session.get_checkpoint(ckpt_path, '["2"]') is None

    gate.set()
    served = await env.demand(T_PATH, CELL)
    assert served == {"result": v1}
    row2 = await env.session.get_generation(T_PATH, CELL)
    assert row2 is not None and row2[2] == "complete"
    assert row2[1] == row[1]  # the SAME generation resumed
    # Step 1 never re-ran (checkpoint hit); step 2 ran twice (aborted +
    # real); step 3 once. The safety pass replayed every step as a hit.
    assert step_calls == {"1": 1, "2": 2, "3": 1}
    assert task.calls == 3  # aborted run + (resumed run + safety replay)


@pytest.mark.anyio
async def test_crash_simulated_resume_serves_durable_checkpoint(tmp_path: Path) -> None:
    """The flagship, crash-simulated: an attempt persists step 1's
    checkpoint and fails at step 2; the status is flipped back to
    ``running`` as if the runner died mid-run, and the session is closed
    and reopened — the startup flip lands ``interrupted`` with a
    ``RunnerCrash`` fault row; re-demanding resumes across the process
    boundary, serving step 1 from the durable entry."""
    state_db = str(tmp_path / "state.sqlite")
    entity_db = str(tmp_path / "entities.sqlite")
    step_calls = {"1": 0, "2": 0}
    armed = {"crash": True}

    @step_fn(type_id=define_type(_TEST_NS, "crashy_step"))
    async def crashy(ctx, *, i):
        step_calls[i] += 1
        if i == "2" and armed["crash"]:
            raise RuntimeError("simulated infra failure")
        return f"ok-{i}"

    class TwoSteps:
        async def __call__(self, ctx, /, *, inp):
            value = await inp.read(x="a")
            ckpt = await ctx.own_internals().open_checkpoint(
                "steps", axes=(m.AxisSpec("i", "str"),)
            )
            for i in ("1", "2"):
                await ckpt.run_step(crashy, args={"i": i}, coords={"i": i})
            return value

    task = TwoSteps()
    ckpt_path = enc.encode_key_path(["t", "steps"])

    async with WorkflowSession.open(state_db, entity_db) as session:
        async with FxtrScope(name="wf3-runner", include=[session.entity_scope]) as runner_scope:
            e = Env(session, runner_scope)
            await e.seed_base()
            await e.bind_fn("twostep", task)
            v1 = await e.write_data(INPUTS, '["a"]', _some_entity())
            await e.create_task_grid(T_PATH, "twostep", {"inp": INPUT_BINDING})
            with pytest.raises(CellFaultError):
                await e.demand(T_PATH, CELL)
            row = await session.get_generation(T_PATH, CELL)
            assert row is not None and row[2] == "errored"  # StepRaisedError
            assert await session.get_checkpoint(ckpt_path, '["1"]') is not None
            # Simulate the crash window: the process died before writing a
            # terminal status.
            await session.set_status(row[0], "running")
            generation = row[1]

    armed["crash"] = False
    async with WorkflowSession.open(state_db, entity_db) as session:
        async with FxtrScope(name="wf3-runner", include=[session.entity_scope]) as runner_scope:
            # Grids, cells, and checkpoints are durable; only the bindings
            # are process state — re-bind the same task (the handle key is
            # content-addressed, so bind_fn rewrites the identical cell).
            e = Env(session, runner_scope)
            await e.bind_fn("twostep", task)
            row = await session.get_generation(T_PATH, CELL)
            assert row is not None and row[2] == "interrupted"  # startup flip
            failures = await session.load_failures(row[0])
            assert any(cls == "RunnerCrash" for _, cls, _ in failures)

            served = await e.demand(T_PATH, CELL)
            assert served == {"result": v1}
            row2 = await session.get_generation(T_PATH, CELL)
            assert row2 is not None and row2[2] == "complete"
            assert row2[1] == generation  # the SAME generation, resumed
    assert step_calls == {"1": 1, "2": 2}  # step 1 served from the checkpoint


@pytest.mark.anyio
async def test_unbound_handle_step_poisons_then_bind_and_resume(env: Env) -> None:
    """Connect poisoning end to end: a step whose ``connect`` finds no
    binding — and swallows the error, as does the task around ``run_step``
    — still interrupts the cell with a ``HandleUnboundError`` fault row and
    no checkpoint entry; binding the handle and re-demanding re-runs the
    step and completes."""
    connect_attempts = {"n": 0}

    @step_fn(type_id=define_type(_TEST_NS, "swallowing_connect_step"))
    async def swallowing(ctx, *, key):
        connect_attempts["n"] += 1
        try:
            svc = await ctx.connect(key)
        except Exception:
            return "swallowed"  # must not evade the poisoning
        ping = await svc.replace_service_type(PingProto).resolve()
        return await ping.ping()

    class ConnectingTask:
        async def __call__(self, ctx, /, *, inp, fns):
            value = await inp.read(x="a")
            key = await fns.read_key(fn="svc")
            ckpt = await ctx.own_internals().open_checkpoint("c", axes=())
            try:
                await ckpt.run_step(swallowing, args={"key": key}, coords={})
            except Exception:
                pass  # caught-and-swallowed at the task level too
            return value

    await env.bind_fn("conn", ConnectingTask())
    v1 = await env.write_data(INPUTS, '["a"]', _some_entity())
    svc_key = (
        enc.SliceTemplateEntity(template=m.SliceTemplate(path=(), target_axes=(), output="svc"))
        .store()
        .entity_id
    )
    await env.write_data(FNS, '["svc"]', svc_key)
    await env.create_task_grid(T_PATH, "conn", {"inp": INPUT_BINDING, "fns": FNS_BINDING})

    with pytest.raises(CellFaultError):
        await env.demand(T_PATH, CELL)
    row = await env.session.get_generation(T_PATH, CELL)
    assert row is not None and row[2] == "interrupted"
    failures = await env.session.load_failures(row[0])
    assert any(kind == FAILURE_FAULT and cls == "HandleUnboundError" for kind, cls, _ in failures)
    assert await env.session.get_checkpoint(enc.encode_key_path(["t", "c"]), "[]") is None
    assert connect_attempts["n"] == 1

    # Bind the handle; the re-demand resumes and the step re-runs (the
    # poisoned invocation wrote no checkpoint entry).
    ping_ref = await env.runner_scope.mount(PingImpl())
    connector = await wrap_singleton(env.runner_scope, ping_ref)
    await env.bindings.bind(svc_key, connector, connector_scope=env.runner_scope)
    assert await env.demand(T_PATH, CELL) == {"result": v1}
    assert connect_attempts["n"] == 2


@pytest.mark.anyio
async def test_connect_unread_key_errors_through_executor(env: Env) -> None:
    """Authority follows the record: a step connecting an id the task read
    from a *data* cell (never from a handle grant) ends the cell errored
    with a ``ConnectAuthorityError`` failure."""

    @step_fn(type_id=define_type(_TEST_NS, "thieving_step"))
    async def thieving(ctx, *, key):
        await ctx.connect(key)  # uncaught — the signal propagates
        return "never"

    class ThievingTask:
        async def __call__(self, ctx, /, *, inp):
            value = await inp.read(x="a")  # a data read confers no authority
            ckpt = await ctx.own_internals().open_checkpoint("c", axes=())
            await ckpt.run_step(thieving, args={"key": value}, coords={})
            return value

    await env.bind_fn("thief", ThievingTask())
    await env.write_data(INPUTS, '["a"]', _some_entity())
    await env.create_task_grid(T_PATH, "thief", {"inp": INPUT_BINDING})
    with pytest.raises(CellFaultError):
        await env.demand(T_PATH, CELL)
    row = await env.session.get_generation(T_PATH, CELL)
    assert row is not None and row[2] == "errored"
    failures = await env.session.load_failures(row[0])
    assert any(
        kind == FAILURE_ERROR and cls == "ConnectAuthorityError" for kind, cls, _ in failures
    )


@pytest.mark.anyio
async def test_safety_pass_catches_drifting_step_args(env: Env) -> None:
    """A task computing different step args on the replay is a
    nondeterministic *invocation*: the safety pass's replayed ``run_step``
    hits the recorded checkpoint with a mismatched args fingerprint →
    ``NondeterminismError`` → errored. The step fn itself never re-runs."""
    step_runs = {"n": 0}

    @step_fn(type_id=define_type(_TEST_NS, "innocent_step"))
    async def innocent(ctx, *, n):
        step_runs["n"] += 1
        return n

    class DriftingArgs:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, ctx, /, *, inp):
            self.calls += 1
            value = await inp.read(x="a")
            ckpt = await ctx.own_internals().open_checkpoint("c", axes=())
            await ckpt.run_step(innocent, args={"n": self.calls}, coords={})
            return value

    await env.bind_fn("drift", DriftingArgs())
    await env.write_data(INPUTS, '["a"]', _some_entity())
    await env.create_task_grid(T_PATH, "drift", {"inp": INPUT_BINDING})
    with pytest.raises(CellFaultError, match="errored"):
        await env.demand(T_PATH, CELL)
    row = await env.session.get_generation(T_PATH, CELL)
    assert row is not None and row[2] == "errored"
    failures = await env.session.load_failures(row[0])
    assert any(cls == "NondeterminismError" for _, cls, _ in failures)
    assert step_runs["n"] == 1  # the pass replays the hit; no step re-run
