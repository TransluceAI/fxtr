"""Shared stateless-service-provider protocols + the ``spawn_resolved`` helper.

This module holds the *base* surface — the part that is common across provider
implementations and that consumers (the workflow runner) depend on:

- :class:`StatelessServiceSpawnerProto` — the spawn-only capability the runner
  consumes. This is the only provider capability the runner needs.
- :class:`ExternalConnectorProto` — the external-service contract, shared by any
  provider that supports registering promised-stateless external services.

The *registration surface* (which methods a provider exposes for registering
handles) is provider-specific and lives with each provider impl — e.g.
``BasicStatelessServiceProviderProto`` in :mod:`.basic_provider`. The wire
protocols are intentionally untyped (per-method TypeVars aren't yet supported by
the typed codec).
"""

from __future__ import annotations

import types
import typing
from typing import Any, Protocol

from fxtr2.conversion.base_registry import substitute_typevars
from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef
from fxtr2.services.service_cap_common import UnknownService
from fxtr2.services.typed_service import service_protocol

from .handles import ServiceHandle


@service_protocol
class StatelessServiceSpawnerProto(Protocol):
    """Spawn-only capability the workflow runner consumes.

    This is the base capability: given a service-handle entity ref, return a
    fresh running service. It cannot register new handles — that capability is
    provider-specific (see each provider's registration protocol).
    """

    async def spawn(self, handle: EntityRef[UnknownEntity]) -> ServiceRef[UnknownService]:
        """Spawn the service identified by ``handle`` and return it.

        Args:
            handle: A ref to a service-handle entity the provider recognizes
                (e.g. an ``ExternalServiceHandle`` for the basic provider). Only
                the handle's id participates; the ref's target type is ignored
                on the wire.

        Returns:
            A ``ServiceRef`` to a freshly spawned service. Retype it with
            ``replace_service_type`` (or use :func:`spawn_resolved`) to call it
            against a known protocol.

        Raises:
            Exception: If no service is registered for ``handle`` (the concrete
                error type is provider-specific; it crosses the wire as an
                ``IRException``).
        """
        ...


@service_protocol
class ExternalConnectorProto(Protocol):
    """Registrant-supplied factory for a promised-stateless external service.

    Shared by any provider that supports external services. The registrant
    promises the produced service is observably stateless across ``connect``
    calls.
    """

    async def connect(self) -> ServiceRef[UnknownService]:
        """Produce the external service to use.

        Returns:
            A ``ServiceRef`` to the service the task should use. Called once per
            spawn; the registrant promises statelessness across calls.
        """
        ...


def service_handle_target_type(handle_type: Any) -> Any:
    """Recover the service-protocol ``T`` from a ``ServiceHandle[T]`` subclass.

    Walks ``types.get_original_bases`` for the ``ServiceHandle[...]``
    specialization, composing per-hop TypeVar substitutions (mirrors the
    ``ChannelTag`` specialization walk in :mod:`fxtr2.common.channels`).

    Args:
        handle_type: A concrete handle entity type (or any type).

    Returns:
        The service-protocol annotation ``T`` from the ``ServiceHandle[T]``
        base, or :data:`UnknownService` if ``handle_type`` doesn't specialize
        ``ServiceHandle`` (e.g. unparameterized).
    """
    if not isinstance(handle_type, type) or not issubclass(handle_type, ServiceHandle):
        return UnknownService
    for base in types.get_original_bases(handle_type):
        origin = typing.get_origin(base) or base
        if not isinstance(origin, type) or not issubclass(origin, ServiceHandle):
            continue
        if origin is ServiceHandle:
            args = typing.get_args(base)
            return args[0] if args else UnknownService
        inner = service_handle_target_type(origin)
        params = tuple(getattr(origin, "__parameters__", ()) or ())
        args = typing.get_args(base)
        subst = dict(zip(params, args)) if args else {}
        return substitute_typevars(inner, subst)
    return UnknownService


async def spawn_resolved[
    T
](
    spawner: StatelessServiceSpawnerProto,
    handle_ref: EntityRef[ServiceHandle[T]],
    scope: FxtrScope | None = None,
) -> T:
    """Spawn ``handle_ref`` and resolve it as its declared protocol ``T``.

    Recovers ``T`` from the handle entity type's ``ServiceHandle[T]`` base,
    retypes the spawned ``ServiceRef[UnknownService]`` accordingly, and resolves
    it. Transitional until ``spawn`` itself can be typed (tracked followup).

    Args:
        spawner: A resolved spawner proxy.
        handle_ref: A ref to a ``ServiceHandle[T]`` entity; its target type
            drives the protocol the result is retyped to.
        scope: Scope to resolve the proxy against (default: the active scope).

    Returns:
        The spawned service resolved as a ``T`` proxy.
    """
    # spawn's wire type is the generic EntityRef[UnknownEntity]; a typed handle
    # ref is fine at runtime (the id is all that travels).
    ref = await spawner.spawn(typing.cast(EntityRef[UnknownEntity], handle_ref))
    target = service_handle_target_type(handle_ref.target_type)
    return await ref.replace_service_type(target).resolve(scope)
