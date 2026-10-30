"""Protocol definition for the workstation-exposed
:class:`AgentMcpRunningSessionImpl` (host-side).

The MCP plugin mounts an :class:`AgentMcpRunningSessionImpl` in its own
process and registers it on the workstation under the external name
``agent_mcp:{session_id}``. Frontends (separate ``/rpc`` peers) resolve
the service by external name and drive the agent's session via the
three methods declared here:

- :meth:`get_state` — current head of the typed event log (or ``None``
  for a fresh session). Frontends walk backward through
  :attr:`WsAgentLogEventChain.prev` to read the full history.
- :meth:`subscribe` — subscribe a
  :class:`~fxtr2.services.subscription_types.Subscriber` to receive
  the chain head on every extension (the protocol subclasses
  ``EventSource[EntityRef[WsAgentLogEventChain]]``). The current head
  is delivered as the subscription's **first event** (when one
  exists), so callers initialize their view from the stream itself —
  no separate :meth:`get_state` round-trip. Skip-ahead semantics:
  intermediate heads can be missed but the subscriber always
  eventually sees the latest; intermediate heads are reachable via
  ``prev`` walks.
- :meth:`send` — push a user message into the agent's session.
  Under the hood, ``send`` invokes the reserved ``workstation_inbox``
  callback's ``notify`` so the message rides the standard callback-
  event machinery and lands in the log as a
  :class:`CallbackNotificationLogEvent(slot_key="workstation_inbox",
  ...)`.

This module declares the protocol + type id only. The impl + the
hub-backed subscription machinery live in :mod:`agent_mcp_impl`.
"""

from __future__ import annotations

from typing import Protocol

from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.subscription_types import EventSource, Subscriber, UnsubscribeHandle
from fxtr2.services.typed_service import service_protocol

from .agent_log_types import WS_AGENT_NS, WsAgentLogEventChain

# Type-id for ``AgentMcpRunningSessionProto``. Stable namespace +
# label string so wire identity is independent of Python class layout.
AGENT_MCP_RUNNING_SESSION_TYPE_ID: FxtrID = system_entities.define_type(
    WS_AGENT_NS, "agent_mcp_running_session"
)


@service_protocol
class AgentMcpRunningSessionProto(EventSource[EntityRef[WsAgentLogEventChain]], Protocol):
    """The workstation-exposed surface of one MCP-driven session.

    Mounted by the MCP under the external name
    ``agent_mcp:{session_id}``. Frontends discover and resolve it via
    the workstation's standard
    :meth:`fxtr_workstation.protocols.WorkstationServiceProto.lookup_short_id_by_external_name`
    + ``get_service_by_external_name``.

    A genuine :class:`~fxtr2.services.subscription_types.EventSource`
    subclass whose payload is the chain-head
    ``EntityRef[WsAgentLogEventChain]`` (exactly what the impl's hub
    publishes); ``subscribe`` is re-declared below purely for its
    docstring.

    All three methods are async (the impl crosses /rpc back to the
    MCP process for each invocation).
    """

    async def get_state(self) -> EntityRef[WsAgentLogEventChain] | None:
        """Return the current head of the agent's typed event log.

        ``None`` if the session is fresh and no events have been
        recorded yet. Otherwise the chain head — frontends walk
        ``head.prev`` recursively to enumerate older entries.

        The returned :class:`EntityRef` is chain-bridged into the
        caller's scope by the typed-service framework, so frontends
        can ``await ref.load()`` it without further setup.
        """
        ...

    async def subscribe(
        self, subscriber: ServiceRef[Subscriber[EntityRef[WsAgentLogEventChain]]]
    ) -> ServiceRef[UnsubscribeHandle]:
        """Subscribe ``subscriber`` to receive the latest chain head on
        every extension.

        Re-declaration of the inherited
        :meth:`fxtr2.services.subscription_types.EventSource.subscribe`
        purely for this docstring (same annotations, same wire shape).

        Race-free initialization: the **current chain head is
        delivered as the subscription's first event** (when the log is
        non-empty; a fresh session delivers nothing until the first
        extension). The seed and the subscription registration are
        atomic with respect to chain extensions — no lost or
        duplicated head (the hub's seed push and the registration
        share one no-await window).

        Skip-ahead semantics: a slow subscriber may miss intermediate
        heads but always sees the most recent one (coalesce-mode
        delivery); intermediate heads remain reachable via
        ``head.prev`` walks. The standard subscription contract
        applies: hold the returned unsubscribe handle and invoke it
        when done (frontends route this through their workstation
        inbox's ``subscribe_to``, which owns the lifecycle); a
        subscriber whose ``on_event`` raises is dropped (crash
        backstop).

        Subscriptions are scoped to one MCP supervisor cycle: a
        workstation restart or MCP reconnect tears them down.
        Frontends reconnecting after such a reset need to
        re-subscribe.
        """
        ...

    async def send(self, payload: FxtrServiceData) -> None:
        """Send ``payload`` to the agent's session.

        The MCP routes ``send`` through the reserved
        ``workstation_inbox`` callback's ``notify``, so the payload
        rides the standard callback-event machinery: the workstation
        records a :class:`fxtr_workstation.client_protocol.CallbackEvent`,
        the MCP's ``/client`` subscription picks it up, and the MCP's
        log builder appends a
        :class:`CallbackNotificationLogEvent(slot_key="workstation_inbox",
        ...)` to the chain.

        Returns once the inbox callback's ``notify`` succeeds — the
        log extension lands asynchronously on the next ``/client``
        frame from the workstation.
        """
        ...


__all__ = [
    "AGENT_MCP_RUNNING_SESSION_TYPE_ID",
    "AgentMcpRunningSessionProto",
]
