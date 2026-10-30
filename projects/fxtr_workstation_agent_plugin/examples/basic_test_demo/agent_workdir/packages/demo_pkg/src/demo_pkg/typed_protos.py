"""Typed service Protocols shared between the host-side
:mod:`run_demo_workstation` and the agent's sandboxed scripts.

Both sides import these Protocols so that the wire round-trip is
typed end-to-end:

- Host side (``run_demo_workstation.py``): mounts impls that
  inherit ``TypedServiceImpl[<Proto>]``. The impl method
  annotations drive the dialect's encode/decode of args + returns.
- Sandbox side (agent scripts via ``preamble.py``): passes the
  Protocols to ``get_service`` / ``lookup_service_by_name`` for
  typed dispatch through the sandbox's ``schedule_invoke`` builtin
  (see :mod:`fxtr_ws_mcp_sandbox_bridge.builtins` for the typed
  encoding path).

The Protocols live in ``demo_pkg`` (the agent's local package,
volume-mounted into the sandbox under ``/local-pkgs``) so the same
class identity is shared between host and sandbox — encoder and
decoder agree on the type, no wire-shape inference needed.

``WorkstationSessionEventInbox`` is re-exported from
:mod:`fxtr_ws_mcp_sandbox_bridge.protocols`. Both host
(via the MCP package's transitive deps) and sandbox (via the
volume-mounted bridge) have that package available, so this is the
single canonical class.
"""

from __future__ import annotations

from typing import Any, Protocol

from fxtr2.data.entity_ref_scope import ServiceRef
from fxtr2.services.typed_service import service_protocol

#: Re-export from the sandbox bridge — the canonical
#: ``WorkstationSessionEventInbox`` available on both sides.
from fxtr_ws_mcp_sandbox_bridge.protocols import WorkstationSessionEventInbox

__all__ = [
    "CallbackTesterProto",
    "DelayedEchoProto",
    "WorkstationSessionEventInbox",
]


@service_protocol
class DelayedEchoProto(Protocol):
    """Service that echoes back any argument after an optional
    delay. ``value`` can be any wire-shaped value the workstation
    knows how to round-trip — scalars, lists, dicts, ``bytes``,
    entity refs, service refs."""

    async def echo(self, value: Any, delay: float) -> Any: ...


@service_protocol
class CallbackTesterProto(Protocol):
    """Service that stashes a callback ref and fires
    fire-and-forget notifications back to it.

    Pattern: the agent creates a workstation-managed callback,
    then schedules ``register_callback(cb)`` (stashing the ref),
    then schedules one or more ``fire_notify(payload, delay)``
    calls. Each ``fire_notify`` returns immediately while a
    background task on the demo service sleeps ``delay`` and then
    calls ``cb.notify(payload)``. The MCP receives the resulting
    :class:`CallbackEvent` and emits a
    ``notifications/claude/channel`` frame.
    """

    async def register_callback(self, cb: ServiceRef[WorkstationSessionEventInbox]) -> None: ...

    async def fire_notify(self, payload: Any, delay: float) -> None: ...
