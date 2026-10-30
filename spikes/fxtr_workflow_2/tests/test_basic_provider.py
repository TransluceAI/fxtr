"""Tests for the external-only stateless service provider (stage 15 part 1).

Covers register + spawn round-trip, the typed ``spawn_resolved`` helper, the
capability split (a spawner ref can't register), the singleton connector helper,
handle entity id stability, and connector registration both from the provider's
own scope (the ``is_tracked`` shortcut) and from a separate scope / concurrent
task (the actual ``bridge_service_from_scope`` retention path).
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any, Protocol

import anyio
import pytest

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method
from fxtr_workflow_2 import (
    BasicStatelessServiceProvider,
    BasicStatelessServiceProviderProto,
    ExternalServiceHandle,
    spawn_resolved,
    wrap_singleton,
)

pytestmark = pytest.mark.anyio

_TEST_NS = uuid.UUID("3f9e1a2b-7c64-4d18-9a2e-5b8c1d6f0e34")
_ECHO_TYPE_ID = system_entities.define_type(_TEST_NS, "echo_service")


@service_protocol
class EchoProto(Protocol):
    async def echo(self, x: str) -> str: ...


class EchoImpl(TypedServiceImpl[EchoProto]):
    def __init__(self, prefix: str = "echo") -> None:
        self._prefix = prefix

    def _target_type_annotation(self) -> Any:
        return EchoProto

    def _service_type_id(self) -> FxtrID:
        return _ECHO_TYPE_ID

    @service_method
    async def echo(self, x: str) -> str:
        return f"{self._prefix}:{x}"


@dataclasses.dataclass(frozen=True)
class _Key(DataclassEntity, fxtr_type=system_entities.define_type(_TEST_NS, "key")):
    name: str


async def _mounted_provider(
    scope: FxtrScope,
) -> BasicStatelessServiceProviderProto:
    ref = await scope.mount(BasicStatelessServiceProvider())
    return await ref.resolve(scope)


async def test_register_and_spawn_roundtrip() -> None:
    async with FxtrScope(name="wf_provider_test") as scope:
        provider = await _mounted_provider(scope)
        echo_ref = await scope.mount(EchoImpl())
        connector = await wrap_singleton(scope, echo_ref)
        key = _Key(name="echo").store()

        handle_ref = await provider.register_external(key, connector)
        spawner = await (await provider.spawner()).resolve(scope)

        svc_ref = await spawner.spawn(handle_ref)
        echo = await svc_ref.replace_service_type(EchoProto).resolve(scope)
        assert await echo.echo("hi") == "echo:hi"


async def test_register_connector_from_parent_scope() -> None:
    """Connector lives on an outer scope that outlives the provider, so the
    provider actually bridges it into its mount scope (``is_tracked`` is False —
    the non-shortcut path the same-scope tests never hit)."""
    async with FxtrScope(name="wf_host") as host:
        echo_ref = await host.mount(EchoImpl(prefix="ext"))
        connector = await wrap_singleton(host, echo_ref)
        async with FxtrScope(name="wf_provider_inner") as provider_scope:
            provider = await _mounted_provider(provider_scope)
            handle_ref = await provider.register_external(_Key(name="ext").store(), connector)
            spawner = await (await provider.spawner()).resolve(provider_scope)
            svc = (
                await (await spawner.spawn(handle_ref))
                .replace_service_type(EchoProto)
                .resolve(provider_scope)
            )
            assert await svc.echo("hi") == "ext:hi"


async def test_register_connector_from_separate_task() -> None:
    """The connector is mounted + held open in a *separate* concurrent task and
    bridged to the caller; the provider then bridges it into its own mount
    scope. Exercises cross-task connector ownership end-to-end."""
    async with anyio.create_task_group() as tg:
        release = anyio.Event()
        ready = anyio.Event()
        box: dict[str, Any] = {}

        async def _connector_host() -> None:
            async with FxtrScope(
                name="wf_connector_host", include=[GLOBAL_SCOPE], isolated=True
            ) as h:
                echo_ref = await h.mount(EchoImpl(prefix="task"))
                box["connector"] = await wrap_singleton(h, echo_ref)
                box["home"] = h
                ready.set()
                await release.wait()

        tg.start_soon(_connector_host)
        await ready.wait()

        async with FxtrScope(name="wf_provider_main") as provider_scope:
            # Make the connector (mounted on the other task's scope) reachable
            # here so we can pass it as an arg.
            connector = box["connector"]
            await provider_scope.bridge_service_from_scope(
                connector, box["home"], keepalive_via="independent_lease"
            )
            provider = await _mounted_provider(provider_scope)
            handle_ref = await provider.register_external(_Key(name="task-ext").store(), connector)
            spawner = await (await provider.spawner()).resolve(provider_scope)
            svc = (
                await (await spawner.spawn(handle_ref))
                .replace_service_type(EchoProto)
                .resolve(provider_scope)
            )
            assert await svc.echo("hi") == "task:hi"

        release.set()


async def test_spawn_resolved_helper() -> None:
    async with FxtrScope(name="wf_spawn_resolved_test") as scope:
        provider = await _mounted_provider(scope)
        echo_ref = await scope.mount(EchoImpl(prefix="sr"))
        connector = await wrap_singleton(scope, echo_ref)
        handle_ref = await provider.register_external(_Key(name="sr").store(), connector)
        spawner = await (await provider.spawner()).resolve(scope)

        # ExternalServiceHandle is ServiceHandle[UnknownService], so this
        # resolves to an UnknownServiceProxy: __getattr__-driven calls work.
        svc = await spawn_resolved(spawner, handle_ref, scope)
        assert await svc.echo("yo") == "sr:yo"


async def test_unknown_handle_errors() -> None:
    async with FxtrScope(name="wf_unknown_handle_test") as scope:
        provider = await _mounted_provider(scope)
        spawner = await (await provider.spawner()).resolve(scope)
        # A handle that was never registered.
        bogus = ExternalServiceHandle(key=_Key(name="never").store()).store()
        with pytest.raises(Exception) as excinfo:  # noqa: PT011 - crosses wire as IRException
            await spawner.spawn(bogus)
        assert "no external service registered" in str(excinfo.value)


async def test_capability_split_spawner_cannot_register() -> None:
    async with FxtrScope(name="wf_capsplit_test") as scope:
        provider = await _mounted_provider(scope)
        spawner = await (await provider.spawner()).resolve(scope)
        # The spawner proxy speaks only StatelessServiceSpawnerProto — it has
        # no registration method to reach.
        assert not hasattr(spawner, "register_external")
        assert hasattr(spawner, "spawn")


async def test_handle_id_is_stable_and_provenance_blind() -> None:
    async with FxtrScope(name="wf_handle_id_test") as scope:
        key = _Key(name="dup").store()
        id1 = ExternalServiceHandle(key=key).store().entity_id
        id2 = ExternalServiceHandle(key=key).store().entity_id
        assert id1 == id2


async def test_two_providers_have_independent_registrations() -> None:
    async with FxtrScope(name="wf_two_providers_test") as scope:
        p1 = await _mounted_provider(scope)
        p2 = await _mounted_provider(scope)
        echo_ref = await scope.mount(EchoImpl())
        connector = await wrap_singleton(scope, echo_ref)
        handle_ref = await p1.register_external(_Key(name="only-p1").store(), connector)

        # p2 never saw this handle.
        spawner2 = await (await p2.spawner()).resolve(scope)
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            await spawner2.spawn(handle_ref)
        assert "no external service registered" in str(excinfo.value)

        # p1 still resolves it.
        spawner1 = await (await p1.spawner()).resolve(scope)
        svc = (
            await (await spawner1.spawn(handle_ref)).replace_service_type(EchoProto).resolve(scope)
        )
        assert await svc.echo("x") == "echo:x"
