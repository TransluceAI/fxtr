"""External-only stateless service provider.

``BasicStatelessServiceProvider`` is the minimum impl that (a) implements the
spawner protocol the workflow runner consumes and (b) lets you attach external,
promised-stateless services. No caching, no nix. It is itself a regular fxtr2
service (``TypedServiceImpl``) so it can be threaded through a workstation or
across RPC, and it exercises the impl mount-lifespan system: all per-mount state
(the registration map, the mounted spawner sub-impl) lives in the lifespan
state rather than on the instance.

The provider's registration surface (``BasicStatelessServiceProviderProto``) is
defined here rather than in the shared ``protocols`` module: registration is
provider-specific (the sandboxed provider's surface differs), whereas the base
spawner capability the runner consumes lives in ``protocols``.

Capability split: the provider mounts a separate ``_SpawnerImpl`` sub-service
that shares the registration map but exposes only ``spawn`` — holding the
spawner ref cannot reach ``register_external``.
"""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import AsyncIterator
from typing import Any, Protocol

from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef
from fxtr2.data.system_entities import define_type
from fxtr2.services.service_cap_common import UnknownService
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method

from .._ns import WORKFLOWS_NS
from .handles import ExternalServiceHandle
from .protocols import ExternalConnectorProto, StatelessServiceSpawnerProto

BASIC_PROVIDER_TYPE_ID = define_type(WORKFLOWS_NS, "basic_stateless_service_provider")
BASIC_SPAWNER_TYPE_ID = define_type(WORKFLOWS_NS, "basic_stateless_service_spawner")
SINGLETON_CONNECTOR_TYPE_ID = define_type(WORKFLOWS_NS, "singleton_external_connector")


class UnknownHandleError(Exception):
    """Raised by ``spawn`` when no service is registered for a handle."""


@service_protocol
class BasicStatelessServiceProviderProto(Protocol):
    """Registration surface of the external-only provider.

    Provider-specific (not part of the base interface the runner consumes): the
    runner only needs the spawner. Holding this capability lets you register
    external services *and* derive the spawner; holding only the spawner cannot
    upgrade to this.
    """

    async def register_external(
        self,
        key: EntityRef[UnknownEntity],
        connector: ServiceRef[ExternalConnectorProto],
    ) -> EntityRef[ExternalServiceHandle]:
        """Register an external service under ``key`` and return its handle.

        The returned ``ExternalServiceHandle``'s id (a function of ``key``'s id)
        is the spawn identity: pass it to ``spawner().spawn(...)`` to get a
        fresh service via the connector.

        Connector lifetime: the connector is retained for the *provider's*
        lifetime (see the impl for the exact mechanism), so it stays spawnable
        after this call returns and after the caller drops its own reference.

        Args:
            key: A registrant-chosen key entity. Re-registering the same key
                (even against a different-but-equivalent connector) yields the
                same handle id — the "promised stateless" trust contract.
            connector: The factory whose ``connect()`` produces the service.

        Returns:
            A ref to the ``ExternalServiceHandle`` for this registration.
        """
        ...

    async def spawner(self) -> ServiceRef[StatelessServiceSpawnerProto]:
        """Return the spawn-only sub-capability.

        Returns:
            A ``ServiceRef`` to the provider's spawner sub-service. It can spawn
            registered handles but cannot register new ones (capability split).
        """
        ...


@dataclasses.dataclass
class _ProviderState:
    """Per-mount shared state for the provider + its spawner sub-impl.

    Keyed by the handle entity id (provenance-blind ``FxtrID`` identity). The
    connector refs are bridged into the provider's mount scope at registration
    time, so they outlive individual calls and stay resolvable for the
    provider's lifetime.
    """

    mount_scope: FxtrScope
    registrations: dict[FxtrID, ServiceRef[ExternalConnectorProto]] = dataclasses.field(
        default_factory=dict
    )
    spawner_ref: ServiceRef[StatelessServiceSpawnerProto] | None = None


class _SpawnerImpl(TypedServiceImpl[StatelessServiceSpawnerProto]):
    """Spawn-only sub-service. Shares ``_ProviderState`` but exposes no
    registration method, enforcing the capability split."""

    def __init__(self, state: _ProviderState) -> None:
        self._state = state

    def _target_type_annotation(self) -> Any:
        return StatelessServiceSpawnerProto

    def _service_type_id(self) -> FxtrID:
        return BASIC_SPAWNER_TYPE_ID

    @service_method
    async def spawn(self, handle: EntityRef[UnknownEntity]) -> ServiceRef[UnknownService]:
        """Spawn the external service registered for ``handle``.

        Args:
            handle: A ref to a registered ``ExternalServiceHandle`` (only its id
                is used to look up the connector).

        Returns:
            The ``ServiceRef`` returned by the connector's ``connect()``.

        Raises:
            UnknownHandleError: If no service is registered for ``handle``.
        """
        connector_ref = self._state.registrations.get(handle.entity_id)
        if connector_ref is None:
            raise UnknownHandleError(
                f"no external service registered for handle {handle.entity_id.id_string()}"
            )
        connector = await connector_ref.resolve()
        return await connector.connect()


class BasicStatelessServiceProvider(TypedServiceImpl[BasicStatelessServiceProviderProto]):
    """External-only provider. Mount via ``await scope.mount(provider)``."""

    def _target_type_annotation(self) -> Any:
        return BasicStatelessServiceProviderProto

    def _service_type_id(self) -> FxtrID:
        return BASIC_PROVIDER_TYPE_ID

    @contextlib.asynccontextmanager
    async def _fxtr_mount_lifespan(self, scope: FxtrScope) -> AsyncIterator[_ProviderState]:
        # Setup runs authorized + with ``scope`` active, so we can mount the
        # spawner sub-service against ``scope`` here (sanctioned per the
        # _fxtr_mount_lifespan contract).
        state = _ProviderState(mount_scope=scope)
        state.spawner_ref = await scope.mount(_SpawnerImpl(state))
        yield state

    @service_method
    async def register_external(
        self,
        key: EntityRef[UnknownEntity],
        connector: ServiceRef[ExternalConnectorProto],
    ) -> EntityRef[ExternalServiceHandle]:
        """Register ``connector`` under ``key``; see protocol for the contract.

        Connector-retention mechanism: the connector arrives bound to the call's
        input scope, which closes when the call returns. To keep it spawnable
        for the provider's lifetime, we bridge it from the input scope into the
        (longer-lived) mount scope with ``keepalive_via="independent_lease"``,
        so the mount scope holds a standing lease until it drains. The one
        exception is when the connector is *already* registered on the mount
        scope (an in-process registrant mounting on the same scope as the
        provider): its own mount already keeps it alive, so we skip re-bridging
        (re-bridging the same ref onto the same scope is an error).

        Args:
            key: The registrant-chosen key entity.
            connector: The external-connector service ref.

        Returns:
            A ref to the ``ExternalServiceHandle`` for this registration.
        """
        state: _ProviderState = self._fxtr_get_lifespan_state()
        mount_scope = self._fxtr_get_mount_scope()
        input_scope = self._fxtr_get_input_scope()
        if not mount_scope.is_tracked(connector):
            await mount_scope.bridge_service_from_scope(
                connector, input_scope, keepalive_via="independent_lease"
            )
        handle_ref = ExternalServiceHandle(key=key).store()
        state.registrations[handle_ref.entity_id] = connector
        return handle_ref

    @service_method
    async def spawner(self) -> ServiceRef[StatelessServiceSpawnerProto]:
        """Return the provider's spawn-only sub-capability ref."""
        state: _ProviderState = self._fxtr_get_lifespan_state()
        assert state.spawner_ref is not None  # set in mount lifespan
        return state.spawner_ref


class _SingletonConnector(TypedServiceImpl[ExternalConnectorProto]):
    """Connector that always returns a known-stateless singleton service."""

    def __init__(self, ref: ServiceRef[UnknownService]) -> None:
        self._ref = ref

    def _target_type_annotation(self) -> Any:
        return ExternalConnectorProto

    def _service_type_id(self) -> FxtrID:
        return SINGLETON_CONNECTOR_TYPE_ID

    @service_method
    async def connect(self) -> ServiceRef[UnknownService]:
        """Return the wrapped singleton service ref (same ref every call)."""
        return self._ref


async def wrap_singleton(
    scope: FxtrScope, ref: ServiceRef[UnknownService]
) -> ServiceRef[ExternalConnectorProto]:
    """Mount a connector whose ``connect()`` returns the singleton ``ref``.

    Use when you want to register a service you already hold and know to be
    stateless.

    Args:
        scope: Scope to mount the connector on (governs its lifetime).
        ref: The singleton service ref the connector should hand out.

    Returns:
        A ``ServiceRef`` to the mounted connector, ready to pass to
        ``register_external``.
    """
    return await scope.mount(_SingletonConnector(ref))
