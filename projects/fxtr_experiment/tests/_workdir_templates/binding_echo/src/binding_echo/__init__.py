"""Binding-echo service fixture for the sandboxed-binding e2e tests.

A sandbox service whose entrypoint takes the **standard binding connect
request** (`fxtr_sandbox_types.BindingConnectRequest` — the shape every
`fxtr_experiment` binding delivers, either class): it serves a service
that echoes the request's key + config and calls a wired dependency ref,
proving the whole request — key closure, config blob, pre-spawned
dependency handles — crosses the sandbox boundary intact.

Built as `fxtr.services.binding_echo` (see the sibling `flake.nix`) and
exercised via the step-connect path in ``tests/test_sandbox_e2e.py``,
which declares wire-compatible host-side duplicates of the protocols
below (same ``BINDING_ECHO_NS`` + ``define_type`` strings).
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Protocol

from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef, get_active_scope
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method
from fxtr_sandbox_types import BindingConnectRequest

# Shared with the host test module: same UUID + define_type strings → the
# same wire-form types on both sides (the established fixture pattern).
BINDING_ECHO_NS = uuid.UUID("5b8e2f61-4c9a-4d17-8e35-a02d6b9c41f7")

BINDING_ECHO_TYPE_ID = system_entities.define_type(BINDING_ECHO_NS, "binding_echo")


@service_protocol
class BindingEchoProto(Protocol):
    """The bound service the guest serves per connect."""

    async def describe(self) -> str: ...
    async def ping_dep(self, name: str) -> str: ...


@service_protocol
class DepPingProto(Protocol):
    """The dependency's protocol (the host binds a ping service to the
    pinned dependency key; the guest calls it through ``handles``)."""

    async def ping(self) -> str: ...


class _Impl(TypedServiceImpl[BindingEchoProto]):
    def __init__(self, request: BindingConnectRequest) -> None:
        self._request = request

    def _target_type_annotation(self) -> Any:
        return BindingEchoProto

    def _service_type_id(self) -> FxtrID:
        return BINDING_ECHO_TYPE_ID

    @service_method
    async def describe(self) -> str:
        """The request's identity half, canonically: key id + config."""
        config = json.dumps(self._request.config, sort_keys=True, separators=(",", ":"))
        return f"key={self._request.key.id_string()} config={config}"

    @service_method
    async def ping_dep(self, name: str) -> str:
        """Call the named pre-spawned dependency ref (an in-guest RPC back
        through the sandbox boundary to wherever the dep is served)."""
        ref = self._request.handles[name]
        dep = await ref.replace_service_type(DepPingProto).resolve()
        return await dep.ping()


async def run(request: BindingConnectRequest) -> ServiceRef[BindingEchoProto]:
    """fxtr.service entry point: the request *is* the connect config.

    The active scope at call time is the ``connect`` dispatch scope —
    the request's key closure and dependency refs are already bridged
    in. Mount and return; the framework chain-bridges the ref back.
    """
    return await get_active_scope().mount(_Impl(request))
