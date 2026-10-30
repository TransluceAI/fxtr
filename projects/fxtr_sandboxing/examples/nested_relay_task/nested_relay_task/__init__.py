"""Stage-4 demo service: outer WASM sandbox that spawns an inner sandbox.

Validates the nested-spawn case end-to-end:

- The outer receives a ``ServiceRef[SandboxSpawnerProto]`` (the env's
  spawner ref, bridged in via config), an ``EntityRef`` for an inner
  bundle (e.g. ``TaskBundle`` or ``NixContainerServiceDefn``), and a
  ``ServiceRef[NetworkHandleAuthorizerProto]`` for an HTTP route.
- Per call to ``relay_http_get(paths)``, the outer:
    1. ``spawner.spawn(inner_bundle_ref)`` then
       ``replace_service_type(SandboxConnectProto[..., ...])``, then
       ``resolve()`` — spawns a fresh inner sandbox under the host env
       (sibling of the outer at the runtime layer).
    2. ``sandbox.connect(authorizer_ref).resolve()`` — connects to the
       inner's main service, passing the authorizer through.
    3. ``replace_service_type(TransporterProto)`` re-types the
       inner's ref so we can call ``http_get`` on it directly.
    4. Maps ``http_get`` over ``paths``, wraps each result with a
       ``[outer-wrapped] `` prefix so the test can confirm the call
       really went through the outer, returns the list.
- All resolves use ``get_active_scope()`` (the outer's dispatch
  scope), so when ``relay_http_get`` returns and that scope drains,
  the bridges drop and the inner sandbox tears down. No long-lived
  state on ``_Impl``.

The capability flow is the whole point: an inner sandbox can only
redeem authorizers that have been explicitly bridged into it. The
outer threads the authorizer through ``connect``'s config without
ever resolving it itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import EntityRef, ServiceRef, get_active_scope
from fxtr2.services.service_cap_common import UnknownService
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method
from fxtr_sandbox_types.protocols import (
    NetworkHandleAuthorizerProto,
    SandboxConnectProto,
    SandboxSpawnerProto,
)

NESTED_RELAY_NS = uuid.UUID("a13e6f8c-5d22-4b91-9af7-6c3d8e0b7128")

RELAY_TYPE_ID = system_entities.define_type(NESTED_RELAY_NS, "nested_relay")


@service_protocol
class NestedRelayProto(Protocol):
    """Protocol the outer (relay) service serves to the host.

    ``relay_http_get`` takes a list of paths and returns a list of
    wrapped response bodies (one per path), in order.
    """

    async def relay_http_get(self, paths: list[str]) -> list[str]: ...


@service_protocol
class TransporterProto(Protocol):
    """Mirror of ``transport_task.TransporterProto`` /
    ``container_transport_task.TransporterProto``.

    ``replace_service_type`` only needs the structural method shape to
    match — we don't need to share UUIDs with the inner here, because
    the outer never *mounts* a TransporterProto impl (the inner does,
    and we just call through the bridged ref).
    """

    async def raw_echo(self, msg: str) -> str: ...
    async def http_get(self, path: str) -> str: ...
    async def redeem_explicit_token(self, token: str) -> str: ...


@dataclass(frozen=True)
class NestedRelayConfig:
    """Plain Python dataclass used as the entry-point config.

    Per stage-4 plan: try a plain ``@dataclass(frozen=True)`` first
    (the most readable shape); if the service dialect doesn't accept
    it, fall back to a typed 3-tuple. ``DataclassEntity`` won't work
    because its fields can't carry ``ServiceRef``.
    """

    spawner_ref: ServiceRef[SandboxSpawnerProto]
    inner_bundle_ref: EntityRef[Any]
    authorizer_ref: ServiceRef[NetworkHandleAuthorizerProto]


class _Impl(TypedServiceImpl[NestedRelayProto]):
    def __init__(self, config: NestedRelayConfig) -> None:
        self._spawner_ref = config.spawner_ref
        self._inner_bundle_ref = config.inner_bundle_ref
        self._authorizer_ref = config.authorizer_ref

    def _target_type_annotation(self) -> Any:
        return NestedRelayProto

    def _service_type_id(self) -> FxtrID:
        return RELAY_TYPE_ID

    @service_method
    async def relay_http_get(self, paths: list[str]) -> list[str]:
        """Spawn the inner, connect, run http_get on each path, return wrapped results.

        Per-call spawn/teardown: each invocation gets its own inner
        sandbox, drained at end of call when the dispatch scope ends.
        """
        scope = get_active_scope()
        spawner = await self._spawner_ref.resolve(scope)
        sandbox_ref = await spawner.spawn(self._inner_bundle_ref)
        sandbox_ref = sandbox_ref.replace_service_type(
            SandboxConnectProto[ServiceRef[NetworkHandleAuthorizerProto], UnknownService]
        )
        sandbox = await sandbox_ref.resolve(scope)
        inner_any_ref = await sandbox.connect(self._authorizer_ref)
        inner_ref = inner_any_ref.replace_service_type(TransporterProto)
        inner = await inner_ref.resolve(scope)
        results: list[str] = []
        for path in paths:
            body = await inner.http_get(path)
            results.append(f"[outer-wrapped] {body}")
        return results


async def run(config: NestedRelayConfig) -> ServiceRef[NestedRelayProto]:
    """fxtr.service entry point.

    The active scope at call time is the ``connect`` dispatch scope on
    the outer's side; the three refs in ``config`` are already bridged
    into it. Mounting on the active scope means method calls can
    resolve the refs through the dispatch hierarchy.
    """
    return await get_active_scope().mount(_Impl(config))
