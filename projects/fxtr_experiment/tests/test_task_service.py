"""Service-boundary tests: a user task fn runs behind real mounted
ServiceRefs, its ops crossing the typed wire into the real ops engine."""

import math
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

import anyio
import pytest
from anyio.streams.stapled import StapledObjectStream
from fxtr_sandbox_types import BindingConnectRequest

from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope, ServiceRef, get_active_scope
from fxtr2.data.system_entities import define_type
from fxtr2.leases.leased_cap_ir import IRException
from fxtr2.rpc.lowering_types import RPCMessage
from fxtr2.rpc.scope_bridge import open_rpc_scope
from fxtr2.services.service_cap_common import UnknownService
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method

from fxtr_experiment import encoding as enc
from fxtr_experiment_task import model as m
from fxtr_experiment_task.errors import HandleUnboundError, TaskOutputError
from fxtr_experiment.state_store import WorkflowSession
from fxtr_experiment.bindings import BindingHost, ConnectorProto, wrap_singleton
from fxtr_experiment_task.task_adapter import TaskFnAdapter, normalize_outputs

from .boundary import run_task_over_engine
from fxtr_experiment_task.view_spec import LeafViewSpec, SubtreeViewSpec, ViewSpec

from .test_ops_engine import Harness, _some_entity

_TEST_NS = uuid.UUID("3b8a1d6e-9c2f-4e57-b1a4-8d0c5f7e2a93")
RECORDING_CONNECTOR_TYPE = define_type(_TEST_NS, "recording_connector")


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


def _input_spec() -> ViewSpec:
    """A read view of the whole ``inputs`` data grid (received name kept)."""
    return LeafViewSpec(
        kind="data",
        path=(m.SubgridStepTemplate(cell=(), key="inputs"),),
        target_axes=(m.KeepAxis(),),
        axes=(m.AxisSpec("x", "str"),),
    )


def _internals_spec() -> ViewSpec:
    """The zero-axis driver cell's internals view."""
    return SubtreeViewSpec(
        path=(m.SubgridStepTemplate(cell=(), key="driver"),),
        target_axes=(),
        axes=(),
        mode="write",
        internals=True,
    )


class ReadAndEchoTask:
    """Reads its input cell, opens a scratch internals subtree, echoes."""

    def __init__(self) -> None:
        self.seen_axes: list[str] | None = None

    async def __call__(self, ctx, /, *, inp):
        self.seen_axes = [a.name for a in inp.axes]
        value = await inp.read(x="a")
        await ctx.own_internals().open_subtree("scratch", axes=())
        return value  # bare return; single declared output


@pytest.mark.anyio
async def test_task_runs_behind_service_refs(h: Harness) -> None:
    """The full boundary along the real chunk-5 path: task fn bound via a
    connector, spawned from the registry, capability mounted per run, ops
    flowing through typed proxies into the real engine."""
    await h.session.create_grid(
        enc.encode_key_path(["inputs"]),
        "data",
        enc.GridConfigEntity(config=m.DataGridConfig(axes=(m.AxisSpec("x", "str"),))),
    )
    v1 = await h.session.write_cell(enc.encode_key_path(["inputs"]), '["a"]', _some_entity())
    engine = await h.engine()
    task = ReadAndEchoTask()
    handle = _some_entity().store().entity_id
    async with FxtrScope(name="runner") as runner_scope:
        registry = BindingHost(runner_scope, h.session)
        fn_ref = await runner_scope.mount(TaskFnAdapter(task))
        connector = await wrap_singleton(runner_scope, fn_ref)
        await registry.bind(handle, connector, connector_scope=runner_scope)
        spawned = await registry.spawn(handle, into_scope=runner_scope)
        result = await run_task_over_engine(
            spawned,
            engine,
            runner_scope=runner_scope,
            args={"inp": h.registry.mint(_input_spec())},
            internals=h.registry.mint(_internals_spec()),
            outputs=m.DEFAULT_TASK_OUTPUTS,
        )
    assert result == {"result": v1}
    assert task.seen_axes == ["x"]  # received names arrived through the wire
    # The run's ops landed durably: the read observation + the created grid.
    record = await h.session.load_record(h.generation)
    subjects = {(r[0], r[1]) for r in record}
    assert (enc.encode_key_path(["inputs"]), '["a"]') in subjects
    assert (enc.encode_key_path(["driver", "scratch"]), "") in subjects


@pytest.mark.anyio
async def test_workflow_errors_cross_the_boundary(h: Harness) -> None:
    """An op fault inside the task surfaces to the invoker in its
    ``IRException`` transit form, category = the original class name.

    That form is *informational only* (tracebacks, assertions like this
    one): durable classification and poisoning persistence happen
    runner-side, where the original typed exception is available — the
    executor never parses categories off the wire.
    """

    class ReadsMissing:
        async def __call__(self, ctx, /, *, inp):
            return await inp.read(x="nope")

    await h.session.create_grid(
        enc.encode_key_path(["inputs"]),
        "data",
        enc.GridConfigEntity(config=m.DataGridConfig(axes=(m.AxisSpec("x", "str"),))),
    )
    engine = await h.engine()
    async with FxtrScope(name="runner") as runner_scope:
        fn_ref = await runner_scope.mount(TaskFnAdapter(ReadsMissing()))
        with pytest.raises(IRException) as excinfo:
            await run_task_over_engine(
                fn_ref,
                engine,
                runner_scope=runner_scope,
                args={"inp": h.registry.mint(_input_spec())},
                internals=h.registry.mint(_internals_spec()),
                outputs=m.DEFAULT_TASK_OUTPUTS,
            )
    assert excinfo.value.category == "CellFaultError"
    assert "no value at" in str(excinfo.value)


def test_normalize_outputs() -> None:
    some = _some_entity().store().entity_id
    single = m.DEFAULT_TASK_OUTPUTS
    multi = {"a": m.EntityOutput(), "b": m.EntityOutput()}
    assert normalize_outputs(some, single) == {"result": some}
    assert normalize_outputs({"a": some, "b": some}, multi) == {"a": some, "b": some}
    with pytest.raises(TaskOutputError):
        normalize_outputs(some, multi)  # bare return, multi-output grid
    with pytest.raises(TaskOutputError):
        normalize_outputs({"a": some}, multi)  # missing output
    with pytest.raises(TaskOutputError):
        normalize_outputs({"result": "not-an-id"}, single)  # wrong shape


@pytest.mark.anyio
async def test_binding_registry_lifecycle(h: Harness) -> None:
    """Bind / double-bind / release / rebind semantics over connectors,
    with the connector mounted on the host scope (`is_tracked`
    shortcut — no bridge)."""
    handle = _some_entity().store().entity_id
    async with FxtrScope(name="runner") as runner_scope:
        registry = BindingHost(runner_scope, h.session)
        with pytest.raises(HandleUnboundError):
            await registry.spawn(handle, into_scope=runner_scope)
        fn_ref = await runner_scope.mount(TaskFnAdapter(ReadAndEchoTask()))
        connector = await wrap_singleton(runner_scope, fn_ref)
        release = await registry.bind(handle, connector, connector_scope=runner_scope)
        assert registry.is_bound(handle)
        with pytest.raises(ValueError):  # double-bind: release first
            await registry.bind(handle, connector, connector_scope=runner_scope)
        release.release()
        release.release()  # idempotent
        assert not registry.is_bound(handle)
        with pytest.raises(HandleUnboundError):
            await registry.spawn(handle, into_scope=runner_scope)
        # Rebinding after release is fine (the promised-stateless contract).
        await registry.bind(handle, connector, connector_scope=runner_scope)
        spawned = await registry.spawn(handle, into_scope=runner_scope)
        assert spawned.handle == fn_ref.handle  # the singleton round-trips


@pytest.mark.anyio
async def test_binding_bridges_connector_from_outer_scope(h: Harness) -> None:
    """The retention path: the connector lives on an *outer* scope, so bind
    actually bridges it into the registry scope (wf2 basic_provider's
    pattern) instead of hitting the same-scope shortcut."""
    async with FxtrScope(name="binder-host") as host:
        fn_ref = await host.mount(TaskFnAdapter(ReadAndEchoTask()))
        connector = await wrap_singleton(host, fn_ref)
        async with FxtrScope(name="runner") as runner_scope:
            registry = BindingHost(runner_scope, h.session)
            handle = _some_entity().store().entity_id
            await registry.bind(handle, connector, connector_scope=host)
            spawned = await registry.spawn(handle, into_scope=runner_scope)
            assert spawned.handle == fn_ref.handle


class RecordingConnector(TypedServiceImpl[ConnectorProto]):
    """Connector that records each request and loads its key.

    The load is the point: `BindingHost.spawn`'s contract says the
    call arena covers the key's closure, so the dispatch scope must be
    able to load the key entity (this is what lets one connector serve
    a whole family of handles by dispatching on the key's contents).
    """

    def __init__(self, fn_ref: ServiceRef[UnknownService]) -> None:
        super().__init__()
        self._fn_ref = fn_ref
        self.requests: list[BindingConnectRequest] = []

    def _target_type_annotation(self) -> Any:
        return ConnectorProto

    def _service_type_id(self) -> FxtrID:
        return RECORDING_CONNECTOR_TYPE

    @service_method
    async def connect(self, request: BindingConnectRequest) -> ServiceRef[UnknownService]:
        # Membership: spawn's contract — the connect call's arena covers
        # the key, so the dispatch (active) scope loads it.
        await get_active_scope().load(EntityRef(request.key, UnknownEntity))
        self.requests.append(request)
        return self._fn_ref


def _distinct_key(tag: str) -> FxtrID:
    """A stored key entity distinguishable by ``tag`` (content-addressed —
    same-content keys would collide into one id)."""
    return (
        enc.GridConfigEntity(config=m.DataGridConfig(axes=(m.AxisSpec(tag, "str"),)))
        .store()
        .entity_id
    )


@pytest.mark.anyio
async def test_one_connector_serves_a_family_of_handles(h: Harness) -> None:
    """Each spawn's request carries the key it serves (config/handles
    dormant), so one connector bound to several handle keys can
    dispatch on the key entity's contents."""
    async with FxtrScope(name="runner") as runner_scope:
        registry = BindingHost(runner_scope, h.session)
        fn_ref = await runner_scope.mount(TaskFnAdapter(ReadAndEchoTask()))
        connector_impl = RecordingConnector(fn_ref)
        connector = await runner_scope.mount(connector_impl)
        k1, k2 = _distinct_key("one"), _distinct_key("two")
        await registry.bind(k1, connector, connector_scope=runner_scope)
        await registry.bind(k2, connector, connector_scope=runner_scope)
        await registry.spawn(k1, into_scope=runner_scope)
        await registry.spawn(k2, into_scope=runner_scope)
    assert [r.key for r in connector_impl.requests] == [k1, k2]
    for r in connector_impl.requests:
        assert r.config is None  # dormant until the durable records land
        assert dict(r.handles) == {}


@pytest.mark.anyio
async def test_connect_request_crosses_a_real_wire() -> None:
    """The full request shape over a real RPC transport: the key's
    closure ships with the call (the remote connector loads it), the
    JSON config round-trips, and a ServiceRef in ``handles`` resolves
    on the far side back to the caller's service (the shape chunk 3's
    dependency resolution will rely on)."""
    from .test_run_step import PingImpl, PingProto

    a_to_b_send, a_to_b_recv = anyio.create_memory_object_stream[RPCMessage](math.inf)
    b_to_a_send, b_to_a_recv = anyio.create_memory_object_stream[RPCMessage](math.inf)
    server_transport = StapledObjectStream(a_to_b_send, b_to_a_recv)
    client_transport = StapledObjectStream(b_to_a_send, a_to_b_recv)
    client_done = anyio.Event()
    seen: dict[str, object] = {}
    sent_config = {"a": [1, "x", None], "n": 2}

    class WireConnector(TypedServiceImpl[ConnectorProto]):
        def _target_type_annotation(self) -> Any:
            return ConnectorProto

        def _service_type_id(self) -> FxtrID:
            return RECORDING_CONNECTOR_TYPE

        @service_method
        async def connect(self, request: BindingConnectRequest) -> ServiceRef[UnknownService]:
            scope = get_active_scope()
            # Membership: the connect call's arena covers the key (it
            # crossed the wire with the request).
            await scope.load(EntityRef(request.key, UnknownEntity))
            seen["key"] = request.key
            seen["config"] = request.config
            # A dependency ref resolves here and proxies back to the
            # caller's mounted service over the same connection.
            dep = await request.handles["dep"].replace_service_type(PingProto).resolve(scope)
            seen["dep_reply"] = await dep.ping()
            return await scope.mount(PingImpl())

    async def server() -> None:
        async with FxtrScope(name="wire-connector-host") as host:
            connector_ref = await host.mount(WireConnector())
            async with open_rpc_scope(
                server_transport, connector_ref, include=[host], name="connector-server"
            ):
                await client_done.wait()

    async def client() -> None:
        try:
            async with open_rpc_scope(
                client_transport, remote_service_type=ConnectorProto, name="connector-client"
            ) as session:
                try:
                    proxy = await session.remote_service.resolve(session.scope)
                    key = _distinct_key("wire")  # client-side store; ships via the arena
                    dep_ref = await session.scope.mount(PingImpl())
                    spawned = await proxy.connect(
                        BindingConnectRequest(key=key, config=sent_config, handles={"dep": dep_ref})
                    )
                    svc = await spawned.replace_service_type(PingProto).resolve(session.scope)
                    assert await svc.ping() == "pong"
                    seen["sent_key"] = key
                finally:
                    # Unblock the server BEFORE this side's rpc-scope exit —
                    # both sides must drain concurrently (see the rpc smoke
                    # in test_user_surface.py for the cross-wait hazard).
                    client_done.set()
        finally:
            client_done.set()  # belt-and-braces for pre-connection failures

    with anyio.fail_after(30):
        async with anyio.create_task_group() as tg:
            tg.start_soon(server)
            tg.start_soon(client)
    assert seen["key"] == seen["sent_key"]
    assert seen["config"] == sent_config
    assert seen["dep_reply"] == "pong"
