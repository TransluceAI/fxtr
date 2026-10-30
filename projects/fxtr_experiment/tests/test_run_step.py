"""run_step tests, direct-engine: checkpoint record-check + replay, the
one-call-per-cell rule, connect authority/poisoning, payload rejection —
driven with a trivial StepHost over the ops-engine harness."""

import uuid
from types import SimpleNamespace
from typing import Any, AsyncIterator, Mapping, Protocol

import pytest
from frozendict import frozendict

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef
from fxtr2.data.system_entities import define_type
from fxtr2.services.service_cap_common import FxtrServiceData, UnknownService
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method

from fxtr_experiment import encoding as enc
from fxtr_experiment_task import model as m
from fxtr_experiment_task.errors import (
    DuplicateStepError,
    InternalRunnerError,
    NondeterminismError,
    StepInvocationFailedError,
    StepPayloadError,
)
from fxtr_experiment.ops_engine import OpsEngine, StepHost
from fxtr_experiment_task.runner_api import StepCapabilityProto, ViewSel, WorkflowStepFnProto
from fxtr_experiment.state_store import WorkflowSession
from fxtr_experiment.storage import FAILURE_ERROR, FAILURE_FAULT
from fxtr_experiment.bindings import BindingHost, wrap_singleton
from fxtr_experiment_task.task_adapter import TaskFnAdapter, task_fn
from fxtr_experiment_task.view_runtime import build_view, step_fn
from fxtr_experiment_task.view_spec import LeafViewSpec, SubtreeViewSpec, ViewSpec
from fxtr_experiment_task.views import CheckpointView

from .boundary import run_task_over_engine
from .test_ops_engine import Harness, _some_entity

_TEST_NS = uuid.UUID("7f0d2c9a-1b5e-4f83-a6d0-3c2b9e8f4a17")
ECHO_STEP_TYPE = define_type(_TEST_NS, "echo_step")
OTHER_STEP_TYPE = define_type(_TEST_NS, "other_step")
CONNECT_STEP_TYPE = define_type(_TEST_NS, "connect_step")
PING_TYPE = define_type(_TEST_NS, "ping_service")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def h(tmp_path) -> AsyncIterator[Harness]:
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    async with WorkflowSession.open(
        str(tmp_path / "state.sqlite"), str(tmp_path / "entities.sqlite")
    ) as session:
        harness = Harness(session)
        await harness.seed()
        yield harness


@pytest.fixture
async def env(h: Harness) -> AsyncIterator[SimpleNamespace]:
    """A step host over its own scope + binding registry."""
    async with FxtrScope(name="wf3-step-host") as scope:
        bindings = BindingHost(scope, h.session)
        yield SimpleNamespace(
            h=h, scope=scope, bindings=bindings, host=StepHost(scope=scope, bindings=bindings)
        )


class _StepBase(TypedServiceImpl[WorkflowStepFnProto]):
    _type_id = ECHO_STEP_TYPE

    def _target_type_annotation(self) -> Any:
        return WorkflowStepFnProto

    def _service_type_id(self) -> FxtrID:
        return self._type_id


class EchoStep(_StepBase):
    """Echoes selected args (incl. an entity id round-trip); counts calls."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    @service_method
    async def run_step(
        self, capability: ServiceRef[UnknownService], args: Mapping[str, FxtrServiceData]
    ) -> FxtrServiceData:
        self.calls += 1
        return {"echo": args["x"], "id": args["id"], "seq": [1, 2]}


class OtherStep(EchoStep):
    _type_id = OTHER_STEP_TYPE


@service_protocol
class PingProto(Protocol):
    async def ping(self) -> str: ...


class PingImpl(TypedServiceImpl[PingProto]):
    def _target_type_annotation(self) -> Any:
        return PingProto

    def _service_type_id(self) -> FxtrID:
        return PING_TYPE

    @service_method
    async def ping(self) -> str:
        return "pong"


class ConnectingStep(_StepBase):
    """Connects the key in its args, calls the service, returns the reply."""

    _type_id = CONNECT_STEP_TYPE

    @service_method
    async def run_step(
        self, capability: ServiceRef[UnknownService], args: Mapping[str, FxtrServiceData]
    ) -> FxtrServiceData:
        cap = await capability.replace_service_type(StepCapabilityProto).resolve()
        service = await cap.connect(args["key"])  # type: ignore[arg-type]
        ping = await service.replace_service_type(PingProto).resolve()
        await cap.log("connected fine")
        return await ping.ping()


class CatchingConnectStep(_StepBase):
    """Catches its connect failure and returns a value anyway (illegally)."""

    _type_id = CONNECT_STEP_TYPE

    @service_method
    async def run_step(
        self, capability: ServiceRef[UnknownService], args: Mapping[str, FxtrServiceData]
    ) -> FxtrServiceData:
        cap = await capability.replace_service_type(StepCapabilityProto).resolve()
        try:
            await cap.connect(args["key"])  # type: ignore[arg-type]
        except Exception:
            pass  # swallowed — must not evade the poisoning
        return "fine"


class RefReturningStep(_StepBase):
    """Tries to smuggle a ServiceRef into its (durable) result."""

    @service_method
    async def run_step(
        self, capability: ServiceRef[UnknownService], args: Mapping[str, FxtrServiceData]
    ) -> FxtrServiceData:
        return {"svc": capability}


def _fns_spec() -> ViewSpec:
    """A read view of the whole `fns` handle grid."""
    return LeafViewSpec(
        kind="handle",
        path=(m.SubgridStepTemplate(cell=(), key="fns"),),
        target_axes=(m.KeepAxis(),),
        axes=(m.AxisSpec("fn", "str"),),
    )


def _internals_spec() -> ViewSpec:
    return SubtreeViewSpec(
        path=(m.SubgridStepTemplate(cell=(), key="driver"),),
        target_axes=(),
        axes=(),
        mode="write",
        internals=True,
    )


async def _checkpoint_sel(h: Harness, engine: OpsEngine) -> ViewSel:
    """Open driver-internals → a zero-axis checkpoint grid; return its sel."""
    internals = h.registry.mint(_internals_spec())
    grant = await engine.open_checkpoint(ViewSel(token=internals.token), "calls", ())
    return ViewSel(token=grant.token)


async def _mounted_step(env: SimpleNamespace, step: _StepBase) -> ServiceRef[UnknownService]:
    return await env.scope.mount(step)


CKPT_PATH = enc.encode_key_path(["driver", "calls"])


@pytest.mark.anyio
async def test_step_runs_persists_and_replays(env: SimpleNamespace) -> None:
    h = env.h
    step = EchoStep()
    ref = await _mounted_step(env, step)
    inner = _some_entity().store().entity_id
    args = {"x": "hi", "id": inner}

    engine = await h.engine(step_host=env.host)
    sel = await _checkpoint_sel(h, engine)
    result = await engine.run_step(sel, ref, args)
    # Canonical round-trip served: ids as bare FxtrIDs, sequences as lists.
    assert result == {"echo": "hi", "id": inner, "seq": [1, 2]}
    assert step.calls == 1
    row = await h.session.get_checkpoint(CKPT_PATH, "[]")
    assert row is not None and row[0] == h.exec_id and row[1] == ECHO_STEP_TYPE

    # Resume: a fresh engine replays the checkpoint — the step never runs.
    resumed = await h.engine(step_host=env.host)
    sel2 = await _checkpoint_sel(h, resumed)
    assert await resumed.run_step(sel2, ref, args) == result
    assert step.calls == 1
    assert resumed.run_failures == []


@pytest.mark.anyio
async def test_invocation_mismatch_is_nondeterminism(env: SimpleNamespace) -> None:
    h = env.h
    echo = EchoStep()
    ref = await _mounted_step(env, echo)
    inner = _some_entity().store().entity_id

    engine = await h.engine(step_host=env.host)
    sel = await _checkpoint_sel(h, engine)
    await engine.run_step(sel, ref, {"x": "hi", "id": inner})

    # Same step, different args.
    replay = await h.engine(step_host=env.host)
    sel2 = await _checkpoint_sel(h, replay)
    with pytest.raises(NondeterminismError):
        await replay.run_step(sel2, ref, {"x": "OTHER", "id": inner})
    assert replay.run_failures and replay.run_failures[0][1] == "NondeterminismError"

    # Same args, different step identity (service type id).
    other_ref = await _mounted_step(env, OtherStep())
    replay2 = await h.engine(step_host=env.host)
    sel3 = await _checkpoint_sel(h, replay2)
    with pytest.raises(NondeterminismError):
        await replay2.run_step(sel3, other_ref, {"x": "hi", "id": inner})


@pytest.mark.anyio
async def test_one_call_per_cell_per_run(env: SimpleNamespace) -> None:
    h = env.h
    step = EchoStep()
    ref = await _mounted_step(env, step)
    inner = _some_entity().store().entity_id
    engine = await h.engine(step_host=env.host)
    sel = await _checkpoint_sel(h, engine)
    await engine.run_step(sel, ref, {"x": "hi", "id": inner})
    with pytest.raises(DuplicateStepError):  # even with the identical invocation
        await engine.run_step(sel, ref, {"x": "hi", "id": inner})
    assert step.calls == 1
    assert any(cls == "DuplicateStepError" for _, cls, _ in engine.run_failures)


@pytest.mark.anyio
async def test_connect_happy_path(env: SimpleNamespace) -> None:
    """Read the handle key at the outer level, hand it to the step as plain
    data, connect + call the bound service inside the step."""
    h = env.h
    key = await h.seed_fns()  # fns["child"] holds a handle key entity
    ping_ref = await env.scope.mount(PingImpl())
    connector = await wrap_singleton(env.scope, ping_ref)
    await env.bindings.bind(key, connector, connector_scope=env.scope)

    engine = await h.engine(step_host=env.host)
    fns_grant = h.registry.mint(_fns_spec())
    read_key = await engine.read_handle_key(
        ViewSel(token=fns_grant.token, bound=frozendict({0: "child"}))
    )
    assert read_key == key

    ref = await _mounted_step(env, ConnectingStep())
    sel = await _checkpoint_sel(h, engine)
    assert await engine.run_step(sel, ref, {"key": key}) == "pong"
    assert engine.run_failures == []


@pytest.mark.anyio
async def test_connect_unread_key_is_authority_error(env: SimpleNamespace) -> None:
    """Knowing an id grants nothing: a key that was never read from a
    handle grant cannot be connected — and catching the error inside the
    step doesn't evade the poisoning (errored-class; no checkpoint)."""
    h = env.h
    key = await h.seed_fns()  # bound, but never READ by this generation
    ping_ref = await env.scope.mount(PingImpl())
    connector = await wrap_singleton(env.scope, ping_ref)
    await env.bindings.bind(key, connector, connector_scope=env.scope)

    engine = await h.engine(step_host=env.host)
    ref = await _mounted_step(env, CatchingConnectStep())
    sel = await _checkpoint_sel(h, engine)
    with pytest.raises(StepInvocationFailedError, match="ConnectAuthorityError"):
        await engine.run_step(sel, ref, {"key": key})
    # Exactly one row: the originating raise site recorded the poisoning;
    # the signal is never recorded (two events, one durable failure).
    (failure,) = engine.run_failures
    assert failure[0] == FAILURE_ERROR and failure[1] == "ConnectAuthorityError"
    assert await h.session.get_checkpoint(CKPT_PATH, "[]") is None


@pytest.mark.anyio
async def test_connect_unbound_key_interrupts(env: SimpleNamespace) -> None:
    """Authority is fine (the key WAS read) but no binding exists: a
    not-ready dependency — fault-class (→ interrupted), poisons the
    invocation even though the step swallowed the error."""
    h = env.h
    key = await h.seed_fns()  # readable, but never bound
    engine = await h.engine(step_host=env.host)
    fns_grant = h.registry.mint(_fns_spec())
    await engine.read_handle_key(ViewSel(token=fns_grant.token, bound=frozendict({0: "child"})))

    ref = await _mounted_step(env, CatchingConnectStep())
    sel = await _checkpoint_sel(h, engine)
    with pytest.raises(StepInvocationFailedError, match="HandleUnboundError"):
        await engine.run_step(sel, ref, {"key": key})
    (failure,) = engine.run_failures  # exactly one row, from the raise site
    assert failure[0] == FAILURE_FAULT  # → interrupted at the run level
    assert failure[1] == "HandleUnboundError"
    assert await h.session.get_checkpoint(CKPT_PATH, "[]") is None


@pytest.mark.anyio
async def test_service_ref_in_result_rejected(env: SimpleNamespace) -> None:
    h = env.h
    engine = await h.engine(step_host=env.host)
    ref = await _mounted_step(env, RefReturningStep())
    sel = await _checkpoint_sel(h, engine)
    with pytest.raises(StepPayloadError):
        await engine.run_step(sel, ref, {})
    assert await h.session.get_checkpoint(CKPT_PATH, "[]") is None


@pytest.mark.anyio
async def test_hostless_miss_is_an_internal_error(env: SimpleNamespace) -> None:
    h = env.h
    ref = await _mounted_step(env, EchoStep())
    inner = _some_entity().store().entity_id
    engine = await h.engine()  # no StepHost
    sel = await _checkpoint_sel(h, engine)
    with pytest.raises(InternalRunnerError):
        await engine.run_step(sel, ref, {"x": "hi", "id": inner})
    assert any(cls == "InternalRunnerError" for _, cls, _ in engine.run_failures)


# -- the task side (7.3): decorators + plain step fns through the boundary ---


@pytest.mark.anyio
async def test_decorators_wrap_call_style_fns() -> None:
    @task_fn
    async def t(ctx, *, x):
        return ("task", ctx, x)

    assert await t("CTX", x=1) == ("task", "CTX", 1)

    tid = define_type(_TEST_NS, "unit_step")

    @step_fn(type_id=tid)
    async def st(ctx, *, x):
        return ("step", ctx, x)

    assert st.step_type_id == tid
    assert await st("CTX", x=2) == ("step", "CTX", 2)


@pytest.mark.anyio
async def test_plain_step_fn_through_the_full_boundary(env: SimpleNamespace) -> None:
    """The whole 7.x stack at once: a task fn behind the service boundary
    reads a handle key, runs a @step_fn-decorated plain fn into a
    checkpoint, the step connects the key + logs, and a second run replays
    the checkpoint without re-running the step."""
    h = env.h
    key = await h.seed_fns()
    ping_ref = await env.scope.mount(PingImpl())
    connector = await wrap_singleton(env.scope, ping_ref)
    await env.bindings.bind(key, connector, connector_scope=env.scope)

    step_calls: list[int] = []

    @step_fn(type_id=define_type(_TEST_NS, "pingy_step"))
    async def pingy(ctx, *, key):
        step_calls.append(1)
        svc = await ctx.connect(key)
        ping = await svc.replace_service_type(PingProto).resolve()
        await ctx.log("pinging")
        return await ping.ping()

    result_entity = _some_entity().store().entity_id
    replies: list[object] = []

    class SteppingTask:
        async def __call__(self, task_ctx, /, *, fns):
            k = await fns.read_key(fn="child")
            ckpt = await task_ctx.own_internals().open_checkpoint("calls", axes=())
            replies.append(await ckpt.run_step(pingy, args={"key": k}, coords={}))
            return result_entity

    async with FxtrScope(name="runner") as runner_scope:
        fn_ref = await runner_scope.mount(TaskFnAdapter(SteppingTask()))
        for _ in range(2):  # first run + a resumed run (fresh engine)
            engine = await h.engine(step_host=env.host)
            out = await run_task_over_engine(
                fn_ref,
                engine,
                runner_scope=runner_scope,
                args={"fns": h.registry.mint(_fns_spec())},
                internals=h.registry.mint(_internals_spec()),
                outputs=m.DEFAULT_TASK_OUTPUTS,
            )
            assert out == {"result": result_entity}
            assert engine.run_failures == []
    assert replies == ["pong", "pong"]  # second reply served from the checkpoint
    assert step_calls == [1]  # the step ran exactly once across both runs


@pytest.mark.anyio
async def test_undeclared_plain_step_fn_is_rejected(env: SimpleNamespace) -> None:
    """A bare async fn carries no identity: the view refuses to mount it."""
    h = env.h
    engine = await h.engine(step_host=env.host)
    internals = h.registry.mint(_internals_spec())
    grant = await engine.open_checkpoint(ViewSel(token=internals.token), "calls", ())
    view = build_view(engine, grant)
    assert isinstance(view, CheckpointView)

    async def naked(ctx, **args):
        return None

    with pytest.raises(TypeError, match="step_fn"):
        await view.run_step(naked, args={}, coords={})
