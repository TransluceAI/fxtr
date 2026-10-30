"""Sandboxed-binding serving over a fake launcher (sandbox-free).

The whole `bindings.BindingHost` funnel, unit-tested against a fake
`SandboxLauncherProto` so the core suite never touches nix or a sandbox
runtime: record → launch (per-binding cache, in-flight dedup,
re-attempt-per-demand) → connect-per-use with eager dependency
resolution (pin re-checks, recursion, cycle detection) and the uniform
`BindingConnectRequest`; the fault-class errors landing runs
``interrupted`` at both consumption paths (executor prologue and step
``connect``); release-and-forget shutdown; launch states; ephemeral
uniformity (``deps``/``config`` on `BindingHost.bind`). The real
launcher is covered by the gated e2e suite
(``test_sandbox_e2e.py``); the durable record CRUD + capability
surface by ``test_sandbox_bindings.py``.
"""

from pathlib import Path
from typing import AsyncIterator
import uuid

import anyio
import pytest

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef
from fxtr2.data.system_entities import define_type
from fxtr2.services.service_cap_common import UnknownService
from fxtr_sandbox_types import BindingConnectRequest

from fxtr_experiment import encoding as enc
from fxtr_experiment_task import model as m
from fxtr_experiment.bindings import LaunchedSandboxProto, wrap_singleton
from fxtr_experiment_task.errors import CellFaultError, SandboxLaunchError, UnknownBindingError
from fxtr_experiment.state_store import WorkflowSession
from fxtr_experiment.storage import FAILURE_FAULT
from fxtr_experiment_task.task_adapter import TaskFnAdapter
from fxtr_experiment_task.view_runtime import step_fn

from .test_executor import CELL, FNS, INPUTS, A_PATH, B_PATH, INPUT_BINDING, EchoTask, Env
from .test_executor_steps import FNS_BINDING
from .test_ops_engine import _some_entity
from .test_run_step import PingImpl, PingProto

_NS = uuid.UUID("7c2e91d4-3a5b-4f80-b6c1-2e9d7f04a8e5")

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeLaunched:
    """A fake launched process: hands out one pre-mounted service ref."""

    def __init__(self, ref: ServiceRef[UnknownService]) -> None:
        self.ref = ref
        self.requests: list[BindingConnectRequest] = []
        self.shutdowns = 0

    async def connect(
        self, request: BindingConnectRequest, *, into_scope: FxtrScope
    ) -> ServiceRef[UnknownService]:
        self.requests.append(request)
        return self.ref

    def shutdown(self) -> None:
        self.shutdowns += 1

    async def wait_closed(self) -> None:
        pass


class FakeLauncher:
    """A fake `SandboxLauncherProto`: serves refs by ``flake_attr``.

    Knobs: ``fail_message`` makes the next launches raise; ``gate`` (an
    event) holds launches in flight for dedup tests.
    """

    def __init__(self) -> None:
        self.services: dict[str, ServiceRef[UnknownService]] = {}
        self.launches: list[m.SandboxDefn] = []
        self.launched: list[FakeLaunched] = []
        self.fail_message: str | None = None
        self.gate: anyio.Event | None = None

    async def launch(self, defn: m.SandboxDefn) -> LaunchedSandboxProto:
        self.launches.append(defn)
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_message is not None:
            raise SandboxLaunchError(self.fail_message)
        launched = FakeLaunched(self.services[defn.flake_attr])
        self.launched.append(launched)
        return launched

    async def aclose(self) -> None:
        pass


def _defn(attr: str) -> m.SandboxDefn:
    return m.SandboxDefn(git_commit="0" * 40, flake_attr=f"fxtr.services.{attr}")


def _key(name: str) -> FxtrID:
    """A distinct stored handle key entity."""
    return (
        enc.SliceTemplateEntity(template=m.SliceTemplate(path=(), target_axes=(), output=name))
        .store()
        .entity_id
    )


class ServingEnv(Env):
    """`test_executor.Env` + the fake launcher and sandbox-bind helpers."""

    def __init__(self, session: WorkflowSession, runner_scope: FxtrScope) -> None:
        self.launcher = FakeLauncher()
        super().__init__(
            session, runner_scope, launcher=self.launcher, sandbox_repo=Path("/fake-repo")
        )

    async def sandbox_bind_fn(
        self,
        name: str,
        task: object,
        *,
        deps: dict[str, tuple[str, str]] | None = None,
        config_json: str | None = None,
    ) -> FxtrID:
        """Write a handle key at fns[name], record a sandboxed binding for
        it, and point the fake launcher's service at ``task``."""
        key = _key(name)
        await self.write_data(FNS, enc.encode_coords((name,)), key)
        await self.session.add_sandbox_binding(key, _defn(name), deps or {}, config_json)
        ref = await self.runner_scope.mount(TaskFnAdapter(task))  # type: ignore[arg-type]
        self.launcher.services[f"fxtr.services.{name}"] = ref
        return key

    async def sandbox_bind_service(
        self,
        name: str,
        ref: ServiceRef[UnknownService],
        *,
        cell: str | None = None,
        deps: dict[str, tuple[str, str]] | None = None,
        config_json: str | None = None,
    ) -> FxtrID:
        """Record a sandboxed binding for a fresh key (optionally written
        at fns[``cell``]) served by ``ref`` through the fake."""
        key = _key(name)
        if cell is not None:
            await self.write_data(FNS, enc.encode_coords((cell,)), key)
        await self.session.add_sandbox_binding(key, _defn(name), deps or {}, config_json)
        self.launcher.services[f"fxtr.services.{name}"] = ref
        return key


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[ServingEnv]:
    async with WorkflowSession.open(
        str(tmp_path / "state.sqlite"), str(tmp_path / "entities.sqlite")
    ) as session:
        async with FxtrScope(name="wf3-runner", include=[session.entity_scope]) as runner_scope:
            e = ServingEnv(session, runner_scope)
            await e.seed_base()
            yield e


async def test_sandbox_bound_task_fn_serves_end_to_end(env: ServingEnv) -> None:
    """The executor path: a sandbox-bound task-fn key launches once (the
    process cache spans run + safety pass and later demands), connects
    per use, and the run completes; the request carries the key and the
    parsed config."""
    task = EchoTask()
    key = await env.sandbox_bind_fn("echo", task, config_json='{"tokens":5}')
    v1 = await env.write_data(INPUTS, '["a"]', _some_entity())
    await env.create_task_grid(A_PATH, "echo", {"inp": INPUT_BINDING})

    assert await env.demand(A_PATH, CELL) == {"result": v1}
    assert task.calls == 2  # run + safety pass
    assert [d.flake_attr for d in env.launcher.launches] == ["fxtr.services.echo"]
    (launched,) = env.launcher.launched
    assert len(launched.requests) == 2  # connect-per-use: run + pass
    for request in launched.requests:
        assert request.key == key
        assert request.config == {"tokens": 5}
        assert dict(request.handles) == {}
    assert env.bindings.sandbox_launch_state(key) == ("running", None)

    # A later demand serves the complete cell: no new launch, no connect.
    assert await env.demand(A_PATH, CELL) == {"result": v1}
    assert len(env.launcher.launches) == 1 and len(launched.requests) == 2


async def test_step_connect_serves_sandbox_bound_handle(env: ServingEnv) -> None:
    """The step path: ``ctx.connect`` of a sandbox-bound key goes through
    the same funnel (launch + connect) and the step's service works."""

    @step_fn(type_id=define_type(_NS, "pinging_step"))
    async def pinging(ctx, *, key):
        svc = await ctx.connect(key)
        ping = await svc.replace_service_type(PingProto).resolve()
        return await ping.ping()

    class ConnectingTask:
        async def __call__(self, ctx, /, *, inp, fns):
            value = await inp.read(x="a")
            key = await fns.read_key(fn="svc")
            ckpt = await ctx.own_internals().open_checkpoint("c", axes=())
            assert await ckpt.run_step(pinging, args={"key": key}, coords={}) == "pong"
            return value

    await env.bind_fn("conn", ConnectingTask())
    v1 = await env.write_data(INPUTS, '["a"]', _some_entity())
    ping_ref = await env.runner_scope.mount(PingImpl())
    await env.sandbox_bind_service("svc", ping_ref, cell="svc")
    await env.create_task_grid(B_PATH, "conn", {"inp": INPUT_BINDING, "fns": FNS_BINDING})

    assert await env.demand(B_PATH, CELL) == {"result": v1}
    # One launch, one connect: the step ran once; the safety pass replays
    # its checkpoint as a hit and never re-connects.
    assert [d.flake_attr for d in env.launcher.launches] == ["fxtr.services.svc"]
    (launched,) = env.launcher.launched
    assert len(launched.requests) == 1


async def test_dependencies_resolve_eagerly_into_the_request(env: ServingEnv) -> None:
    """A sandboxed binding's pinned dependency (itself ephemerally bound)
    arrives pre-spawned in ``request.handles``, and the ref works."""

    class DepCheckingTask(EchoTask):
        pass

    # The dependency: a ping service behind an *ephemeral* binding, its
    # key sitting in a handle cell the sandbox binding pins.
    ping_ref = await env.runner_scope.mount(PingImpl())
    dep_key = _key("dep")
    await env.write_data(FNS, '["dep"]', dep_key)
    connector = await wrap_singleton(env.runner_scope, ping_ref)
    await env.bindings.bind(dep_key, connector, connector_scope=env.runner_scope)

    task = DepCheckingTask()
    await env.sandbox_bind_fn("echo", task, deps={"pinger": (FNS, '["dep"]')})
    v1 = await env.write_data(INPUTS, '["a"]', _some_entity())
    await env.create_task_grid(A_PATH, "echo", {"inp": INPUT_BINDING})

    assert await env.demand(A_PATH, CELL) == {"result": v1}
    (launched,) = env.launcher.launched
    request = launched.requests[0]
    assert set(request.handles) == {"pinger"}
    ping = await request.handles["pinger"].replace_service_type(PingProto).resolve(env.runner_scope)
    assert await ping.ping() == "pong"


async def test_pin_mismatch_faults_interrupted_then_revert_completes(env: ServingEnv) -> None:
    """A rewired dependency cell faults the consuming run (fault-class,
    retryable): interrupted with a ``BindingDependencyFaultError`` row;
    reverting the cell lets the same generation resume and complete."""
    ping_ref = await env.runner_scope.mount(PingImpl())
    dep_key = _key("dep")
    await env.write_data(FNS, '["dep"]', dep_key)
    connector = await wrap_singleton(env.runner_scope, ping_ref)
    await env.bindings.bind(dep_key, connector, connector_scope=env.runner_scope)

    task = EchoTask()
    await env.sandbox_bind_fn("echo", task, deps={"pinger": (FNS, '["dep"]')})
    v1 = await env.write_data(INPUTS, '["a"]', _some_entity())
    await env.create_task_grid(A_PATH, "echo", {"inp": INPUT_BINDING})

    other = _key("other")
    await env.write_data(FNS, '["dep"]', other)  # rewire: identity-level change
    with pytest.raises(CellFaultError):
        await env.demand(A_PATH, CELL)
    row = await env.session.get_generation(A_PATH, CELL)
    assert row is not None and row[2] == "interrupted"
    (failure,) = await env.session.load_failures(row[0])
    assert failure[0] == FAILURE_FAULT and failure[1] == "BindingDependencyFaultError"
    assert env.launcher.launches == []  # deps resolve before any launch

    await env.write_data(FNS, '["dep"]', dep_key)  # revert to the pin
    assert await env.demand(A_PATH, CELL) == {"result": v1}
    row = await env.session.get_generation(A_PATH, CELL)
    assert row is not None and row[2] == "complete"


async def test_dependency_cycle_faults(env: ServingEnv) -> None:
    """Bindings whose pinned dependencies form a cycle fault with a
    ``BindingCycleError`` (interrupted, retryable after re-binding)."""
    task = EchoTask()
    # a's binding depends on the cell holding b's key and vice versa
    # (both cells populated before either bind — bind-time pinning
    # requires it; the cycle only bites at connect).
    key_a, key_b = _key("echo"), _key("b")
    await env.write_data(FNS, '["echo"]', key_a)
    await env.write_data(FNS, '["b"]', key_b)
    await env.session.add_sandbox_binding(key_a, _defn("echo"), {"b": (FNS, '["b"]')}, None)
    await env.session.add_sandbox_binding(key_b, _defn("b"), {"a": (FNS, '["echo"]')}, None)
    ref = await env.runner_scope.mount(TaskFnAdapter(task))  # type: ignore[arg-type]
    env.launcher.services["fxtr.services.echo"] = ref
    env.launcher.services["fxtr.services.b"] = ref
    await env.write_data(INPUTS, '["a"]', _some_entity())
    await env.create_task_grid(A_PATH, "echo", {"inp": INPUT_BINDING})

    with pytest.raises(CellFaultError):
        await env.demand(A_PATH, CELL)
    row = await env.session.get_generation(A_PATH, CELL)
    assert row is not None and row[2] == "interrupted"
    (failure,) = await env.session.load_failures(row[0])
    assert failure[0] == FAILURE_FAULT and failure[1] == "BindingCycleError"


async def test_repo_unconfigured_faults(tmp_path: Path) -> None:
    """A sandbox-bound key demanded on a launcher-less runner faults
    ``SandboxRepoUnconfiguredError`` (fault-class → interrupted)."""
    async with WorkflowSession.open(
        str(tmp_path / "state.sqlite"), str(tmp_path / "entities.sqlite")
    ) as session:
        async with FxtrScope(name="wf3-runner", include=[session.entity_scope]) as runner_scope:
            env = Env(session, runner_scope)  # no launcher, no repo
            await env.seed_base()
            key = _key("echo")
            await env.write_data(FNS, '["echo"]', key)
            await session.add_sandbox_binding(key, _defn("echo"), {}, None)
            await env.create_task_grid(A_PATH, "echo", {})
            with pytest.raises(CellFaultError):
                await env.demand(A_PATH, CELL)
            row = await session.get_generation(A_PATH, CELL)
            assert row is not None and row[2] == "interrupted"
            (failure,) = await session.load_failures(row[0])
            assert failure[0] == FAILURE_FAULT
            assert failure[1] == "SandboxRepoUnconfiguredError"


async def test_concurrent_demands_share_one_launch(env: ServingEnv) -> None:
    """In-flight dedup: two cells demanding one binding concurrently
    share a single launch (and both complete against it)."""
    task = EchoTask()
    await env.sandbox_bind_fn("echo", task)
    v1 = await env.write_data(INPUTS, '["a"]', _some_entity())
    await env.create_task_grid(A_PATH, "echo", {"inp": INPUT_BINDING})
    await env.create_task_grid(B_PATH, "echo", {"inp": INPUT_BINDING})

    gate = anyio.Event()
    env.launcher.gate = gate
    results: dict[str, dict[str, FxtrID]] = {}

    async def demand(path: str) -> None:
        results[path] = await env.demand(path, CELL)

    async with anyio.create_task_group() as tg:
        tg.start_soon(demand, A_PATH)
        tg.start_soon(demand, B_PATH)
        # Wait until the first demand's launch is in flight (the second
        # is then parked on the same slot), then release the gate.
        while not env.launcher.launches:
            await anyio.sleep(0.01)
        gate.set()

    assert results[A_PATH] == {"result": v1} and results[B_PATH] == {"result": v1}
    assert len(env.launcher.launches) == 1


async def test_launch_failure_faults_then_reattempts(env: ServingEnv) -> None:
    """A failed launch lands the demand interrupted with the full message
    (``sandbox_faulted`` state, evidence kept); the next demand
    re-attempts (no negative caching) and completes."""
    task = EchoTask()
    key = await env.sandbox_bind_fn("echo", task)
    v1 = await env.write_data(INPUTS, '["a"]', _some_entity())
    await env.create_task_grid(A_PATH, "echo", {"inp": INPUT_BINDING})

    env.launcher.fail_message = "nix build failed: attr 'echo' broke\n<full stderr here>"
    with pytest.raises(CellFaultError):
        await env.demand(A_PATH, CELL)
    row = await env.session.get_generation(A_PATH, CELL)
    assert row is not None and row[2] == "interrupted"
    (failure,) = await env.session.load_failures(row[0])
    assert failure[0] == FAILURE_FAULT and failure[1] == "SandboxLaunchError"
    assert "<full stderr here>" in failure[2]
    state, message = env.bindings.sandbox_launch_state(key)
    assert state == "faulted" and message is not None
    assert "<full stderr here>" in message

    env.launcher.fail_message = None
    assert await env.demand(A_PATH, CELL) == {"result": v1}
    assert len(env.launcher.launches) == 2  # re-attempted per demand
    assert env.bindings.sandbox_launch_state(key) == ("running", None)


async def test_shutdown_is_release_and_forget(env: ServingEnv) -> None:
    """``shutdown_sandbox`` drops the cache and signals teardown without
    waiting; the next use relaunches; an unknown key faults; a removed
    binding's process is dropped the same way."""
    task = EchoTask()
    key = await env.sandbox_bind_fn("echo", task)
    v1 = await env.write_data(INPUTS, '["a"]', _some_entity())
    await env.create_task_grid(A_PATH, "echo", {"inp": INPUT_BINDING})
    await env.create_task_grid(B_PATH, "echo", {"inp": INPUT_BINDING})

    assert await env.demand(A_PATH, CELL) == {"result": v1}
    await env.bindings.shutdown_sandbox(key)
    (launched,) = env.launcher.launched
    assert launched.shutdowns == 1
    assert env.bindings.sandbox_launch_state(key) == ("unlaunched", None)

    with pytest.raises(UnknownBindingError):
        await env.bindings.shutdown_sandbox(_key("unbound"))

    assert await env.demand(B_PATH, CELL) == {"result": v1}  # relaunches
    assert len(env.launcher.launches) == 2

    await env.session.remove_sandbox_binding(key)
    await env.bindings.drop_process(key)
    assert env.launcher.launched[1].shutdowns == 1
    assert env.bindings.sandbox_launch_state(key) == ("unlaunched", None)


async def test_ephemeral_bind_carries_deps_and_config(env: ServingEnv) -> None:
    """The ephemeral class is uniform: pins + config on `BindingHost.bind`
    reach the connector's request through the same funnel, and a rewired
    dep cell faults its connects the same way."""
    from .test_task_service import RecordingConnector

    ping_ref = await env.runner_scope.mount(PingImpl())
    dep_key = _key("dep")
    await env.write_data(FNS, '["dep"]', dep_key)
    dep_connector = await wrap_singleton(env.runner_scope, ping_ref)
    await env.bindings.bind(dep_key, dep_connector, connector_scope=env.runner_scope)

    task = EchoTask()
    key = _key("echo")
    await env.write_data(FNS, '["echo"]', key)
    fn_ref = await env.runner_scope.mount(TaskFnAdapter(task))  # type: ignore[arg-type]
    connector_impl = RecordingConnector(fn_ref)
    connector = await env.runner_scope.mount(connector_impl)

    pins = await env.session.pin_dependency_cells({"pinger": (FNS, '["dep"]')})
    await env.bindings.bind(
        key,
        connector,
        connector_scope=env.runner_scope,
        deps=pins,
        config_json='{"mode":"fast"}',
    )

    v1 = await env.write_data(INPUTS, '["a"]', _some_entity())
    await env.create_task_grid(A_PATH, "echo", {"inp": INPUT_BINDING})
    assert await env.demand(A_PATH, CELL) == {"result": v1}
    request = connector_impl.requests[0]
    assert request.key == key and request.config == {"mode": "fast"}
    assert set(request.handles) == {"pinger"}

    # Rewire the dep cell: the ephemeral binding's connects now fault
    # exactly like the sandboxed class's.
    await env.write_data(FNS, '["dep"]', _key("other"))
    await env.create_task_grid(B_PATH, "echo", {"inp": INPUT_BINDING})
    with pytest.raises(CellFaultError):
        await env.demand(B_PATH, CELL)
    row = await env.session.get_generation(B_PATH, CELL)
    assert row is not None and row[2] == "interrupted"
    (failure,) = await env.session.load_failures(row[0])
    assert failure[1] == "BindingDependencyFaultError"


async def test_launch_state_transitions_emit_the_bindings_hint(env: ServingEnv) -> None:
    """Sandbox launch-state transitions fire the host's change listener
    (the ``bindings`` watch hint source), like binds/releases do."""
    notifications = {"n": 0}
    env.bindings.set_change_listener(lambda: notifications.__setitem__("n", notifications["n"] + 1))

    task = EchoTask()
    key = await env.sandbox_bind_fn("echo", task)
    del key
    await env.write_data(INPUTS, '["a"]', _some_entity())
    await env.create_task_grid(A_PATH, "echo", {"inp": INPUT_BINDING})
    before = notifications["n"]
    await env.demand(A_PATH, CELL)
    # At least launching → running fired (exact count is not contractual).
    assert notifications["n"] >= before + 2
