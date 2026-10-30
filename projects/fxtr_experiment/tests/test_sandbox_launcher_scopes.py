"""Regression: `FxtrSandboxLauncher` holder tasks run scope-poisoned.

The launcher starts its environment / per-process holder tasks lazily,
from inside whichever service *dispatch* first demands a binding. If a
holder task kept the inherited dispatch context and then called library
code that opens a non-isolated scope, that long-lived scope would adopt
the ephemeral per-call scope as a structural parent, which then could
never end while the process lives — a teardown deadlock (investigation
record: `plans/phase6_sandboxing/chunk3_scope_map.md`).

The launcher therefore starts its holders via
`start_with_scope(scope=None)`: NO ambient scope, so implicit
active-scope use anywhere in the holder's call tree raises
`NoActiveScopeError` at the offending line. Two tests pin this at
core-suite cost, both driving the *real* launcher from inside a real
service dispatch with the guest ref re-exported as the dispatch's
return (the shape of the step-connect path):

- a fake `create_sandbox_environment` modeling a WORST-CASE library
  (non-isolated host scope — which the real one is NOT) fails to
  launch with the pointed error instead of silently wiring the
  deadlock;
- a disciplined fake (isolated host scope, mirroring the real one)
  serves connects and completes the full teardown, guarded so a
  regression to parking fails loudly with the scope-drain report.
"""

from __future__ import annotations

import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Protocol

import anyio
import pytest

from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.debug.scope_report import fail_after_and_report_hung_scopes
from fxtr2.data.entity_ref_scope import (
    GLOBAL_SCOPE,
    FxtrScope,
    ServiceRef,
    get_active_scope,
)
from fxtr2.services.service_cap_common import UnknownService
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method
from fxtr_sandbox_types import (
    BindingConnectRequest,
    SandboxConnectProto,
    SandboxSpawnerProto,
)

from fxtr_experiment_task import model as m
from fxtr_experiment import sandbox_launcher
from fxtr_experiment.sandbox_launcher import FxtrSandboxLauncher

from .test_run_step import PingImpl, PingProto
from .test_sandbox_serving import _key

pytestmark = pytest.mark.anyio

_NS = uuid.UUID("5f1c2a88-9d3e-4b47-8c25-7e60a1b4d9f3")

_DEFN = m.SandboxDefn(git_commit="0" * 40, flake_attr="fxtr.services.fake")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakeConnectWrapperImpl(
    TypedServiceImpl[SandboxConnectProto[BindingConnectRequest, UnknownService]]
):
    """The guest's connect surface: mounts a fresh `PingImpl` per connect
    on the connect dispatch's call scope (the guest-side dispatch-scope
    analog) and returns its ref."""

    def _target_type_annotation(self) -> Any:
        return SandboxConnectProto[BindingConnectRequest, UnknownService]

    def _service_type_id(self) -> FxtrID:
        return system_entities.define_type(_NS, "fake_connect_wrapper")

    @service_method
    async def connect(self, request: BindingConnectRequest) -> ServiceRef[UnknownService]:
        # Dispatch method: the active scope is the per-call scope
        # (scope-discipline case 3); the returned ref chain-bridges out.
        ref = await get_active_scope().mount(PingImpl())
        return ref.replace_service_type(UnknownService)


class _FakeSpawnerImpl(TypedServiceImpl[SandboxSpawnerProto]):
    """`SandboxSpawnerImpl` minus nix: mounts the connect wrapper on the
    spawn dispatch's call scope (the real spawner's mount shape) and
    returns its ref."""

    def _target_type_annotation(self) -> Any:
        return SandboxSpawnerProto

    def _service_type_id(self) -> FxtrID:
        return system_entities.define_type(_NS, "fake_spawner")

    @service_method
    async def spawn(self, bundle: Any) -> ServiceRef[Any]:
        wrapper_ref = await get_active_scope().mount(_FakeConnectWrapperImpl())
        return wrapper_ref.replace_service_type(UnknownService)


class _Env:
    def __init__(self, spawner_ref: ServiceRef[SandboxSpawnerProto], host_scope: FxtrScope) -> None:
        self.spawner_ref = spawner_ref
        self.host_scope = host_scope


@asynccontextmanager
async def _hazard_sandbox_environment(
    *, cache_dir: Path, git_repo_path: Path | None = None
) -> AsyncIterator[Any]:
    """`create_sandbox_environment` minus nix, deliberately modeling a
    WORST-CASE library: the host scope is non-isolated, so it captures
    the opening task's ambient active scope as a structural parent —
    which the real one does not (it opens isolated). Under the
    launcher's scope-poisoned holder tasks this must fail to launch
    with `NoActiveScopeError` rather than silently wiring the deadlock."""
    async with FxtrScope(name="hazard_sandbox_env") as host_scope:
        spawner_ref = await host_scope.mount(_FakeSpawnerImpl())
        yield _Env(spawner_ref, host_scope)


@asynccontextmanager
async def _disciplined_sandbox_environment(
    *, cache_dir: Path, git_repo_path: Path | None = None
) -> AsyncIterator[Any]:
    """`create_sandbox_environment` minus nix, mirroring the real one's
    scope arrangement (isolated host scope, nothing ambient)."""
    async with FxtrScope(
        isolated=True, include=[GLOBAL_SCOPE], name="disciplined_sandbox_env"
    ) as host_scope:
        spawner_ref = await host_scope.mount(_FakeSpawnerImpl())
        yield _Env(spawner_ref, host_scope)


@service_protocol
class SpawnGateProto(Protocol):
    async def get(self) -> ServiceRef[UnknownService]: ...


class SpawnGateImpl(TypedServiceImpl[SpawnGateProto]):
    """The step-connect analog: launch + connect from inside a dispatch,
    into the per-call scope, re-exporting the guest ref as the return."""

    def __init__(self, launcher: FxtrSandboxLauncher, key: FxtrID) -> None:
        super().__init__()
        self._launcher = launcher
        self._key = key

    def _target_type_annotation(self) -> Any:
        return SpawnGateProto

    def _service_type_id(self) -> FxtrID:
        return system_entities.define_type(_NS, "spawn_gate")

    @service_method
    async def get(self) -> ServiceRef[UnknownService]:
        launched = await self._launcher.launch(_DEFN)
        request = BindingConnectRequest(key=self._key, config=None, handles={})
        return await launched.connect(request, into_scope=get_active_scope())


async def test_hazard_library_fails_loudly_under_poisoned_holders(monkeypatch: Any) -> None:
    """A library that implicitly consults ambient scope inside the
    holder tasks cannot launch: the poison surfaces `NoActiveScopeError`
    through the demanding dispatch instead of wiring the deadlock."""
    monkeypatch.setattr(sandbox_launcher, "create_sandbox_environment", _hazard_sandbox_environment)
    async with fail_after_and_report_hung_scopes(30):
        async with FxtrScope(
            name="wf3-runner", isolated=True, include=[GLOBAL_SCOPE]
        ) as runner_scope:
            async with AsyncExitStack() as stack:
                tg = await stack.enter_async_context(anyio.create_task_group())
                launcher = FxtrSandboxLauncher(
                    runner_scope,
                    sandbox_repo=Path("/fake-repo"),
                    cache_dir=Path("/fake-cache"),
                    task_group=tg,
                )
                stack.push_async_callback(launcher.aclose)
                gate_ref = await runner_scope.mount(SpawnGateImpl(launcher, _key("svc")))
                async with FxtrScope(
                    name="consumer", isolated=True, include=[runner_scope]
                ) as consumer:
                    gate = await gate_ref.resolve(consumer)
                    with pytest.raises(Exception, match="No active scope"):
                        await gate.get()


async def test_disciplined_environment_full_teardown(monkeypatch: Any) -> None:
    """Spawn-and-connect inside a dispatch, then full teardown, over the
    disciplined fake environment (the real scope arrangement). A
    regression to parking fails loudly via the guard's drain report."""
    monkeypatch.setattr(
        sandbox_launcher, "create_sandbox_environment", _disciplined_sandbox_environment
    )
    async with fail_after_and_report_hung_scopes(30):
        async with FxtrScope(
            name="wf3-runner", isolated=True, include=[GLOBAL_SCOPE]
        ) as runner_scope:
            async with AsyncExitStack() as stack:
                tg = await stack.enter_async_context(anyio.create_task_group())
                launcher = FxtrSandboxLauncher(
                    runner_scope,
                    sandbox_repo=Path("/fake-repo"),
                    cache_dir=Path("/fake-cache"),
                    task_group=tg,
                )
                stack.push_async_callback(launcher.aclose)
                # The key entity only rides the connect request; store it
                # in the ambient scope (`_key`'s contract) like any test
                # entity — the runner scope isn't needed for it.
                key = _key("svc")
                gate_ref = await runner_scope.mount(SpawnGateImpl(launcher, key))
                for round_no in (1, 2):
                    async with FxtrScope(
                        name=f"consumer-{round_no}", isolated=True, include=[runner_scope]
                    ) as consumer:
                        gate = await gate_ref.resolve(consumer)
                        svc_ref = await gate.get()
                        svc = await svc_ref.replace_service_type(PingProto).resolve(consumer)
                        assert await svc.ping() == "pong"
                # runner_scope's own drain (the gate's parked call scopes)
                # and launcher.aclose (holder drains) are the regression
                # surface: both parked forever pre-fix.
