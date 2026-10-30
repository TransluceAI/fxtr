"""Host-side implementation of :class:`AgentMcpRunningSessionProto`.

Mounted in the MCP process on a per-cycle ``impl_scope`` (owned by
the supervisor's per-session subtask). The MCP registers its
:class:`ServiceRef` with the workstation under the external name
``agent_mcp:{session_id}`` so frontends can resolve it via
``get_service_by_external_name``. Incoming method calls route
workstation → MCP-over-``/rpc`` → this impl's dispatch.

## Subscription model

Chain-head subscriptions are a coalesce-mode
:class:`fxtr2.services.subscription_util.SubscriberHub` held by the
impl (``task_group=impl_tg``; the hub owns no scope — events ride
per-event origin leases on ``impl_scope``, where heads are stored):

- :meth:`AgentMcpRunningSessionImpl.subscribe` delegates to
  ``hub.subscribe(subscriber, get_active_scope(), seed=[head],
  seed_origin=impl_scope)`` (seed omitted when no head exists yet) —
  the impl retains the latest head itself (``_chain_head``) and seeds
  it as the subscription's first delivery, replacing the old
  ``watch_state`` atomic snapshot-return.
- ``update_head`` publishes the new head through the hub. Coalesce
  mode gives the old skip-ahead semantics: multiple updates during
  one in-flight ``on_event`` collapse to one delivery of the latest
  head; ``publish`` never awaits, so slow subscribers are isolated
  from each other and from the log-builder path.
- Subscriptions end via the returned
  :class:`~fxtr2.services.subscription_types.UnsubscribeHandle` or
  the ``on_event``-raise crash backstop; the hub owns the
  per-subscription worker / scope / bridge machinery.

## Scope discipline (see ``v2/notes/scope_discipline.md``)

The impl takes its ``scope`` explicitly at construction. Per the
discipline:

- ``get_state`` returns the impl's tracked ``_chain_head`` directly.
  The typed-service framework chain-bridges it back to the caller
  through the call_scope's ancestor chain (which includes the
  impl_scope by way of the mount, via the bridge from impl_scope to
  the workstation-side service_scope to the frontend's call_scope).
- ``subscribe`` delegates to the hub, which opens fresh isolated
  per-subscription scopes, bridges the subscriber ref in from the
  active scope at the dispatch (the call_scope), and tears them down
  forcefully when the subscription ends — so the impl_scope doesn't
  accumulate stale subscriber bridges from long-departed frontends
  (and subscriptions don't pin the impl_scope either). The
  ``seed=[head]`` push and the subscription append happen with no
  ``await`` in between (inside the hub), preserving the old
  ``watch_state`` no-dup-no-miss guarantee with the head riding the
  stream instead of the return value.
- ``send`` resolves the pre-bridged inbox callback ref via
  ``self._inbox_callback_ref.resolve(scope=self._scope)`` and calls
  ``notify`` through that proxy.

## Lifecycle

The impl is constructed once per supervisor cycle and lives until
the cycle ends:

1. Supervisor opens ``impl_scope`` + ``impl_tg`` (see chunk D).
2. Bootstraps the workstation_inbox callback (chunk D's
   ``_bootstrap_agent_mcp``) and bridges its
   ``ServiceRef[WorkstationSessionEventInbox]`` into ``impl_scope``.
3. Constructs ``AgentMcpRunningSessionImpl(scope=impl_scope,
   workstation=..., inbox_callback_ref=..., initial_head=...,
   impl_tg=impl_tg)``.
4. Mounts it on ``impl_scope`` and registers with the workstation
   under ``agent_mcp:{session_id}``.

When the supervisor cycle ends, ``impl_tg`` cancels all
subscription workers, ``impl_scope`` drains and releases the
bridges, and the workstation drops the ``agent_mcp:{session_id}``
registration.
"""

from __future__ import annotations

import logging
from typing import Any

import anyio.abc
from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import (
    FxtrScope,
    ServiceRef,
    get_active_scope,
)
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.subscription_types import Subscriber, UnsubscribeHandle
from fxtr2.services.subscription_util import SubscriberHub
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method
from fxtr_workstation.protocols import WorkstationServiceProto, WorkstationSessionEventInbox

from .agent_log_types import WsAgentLogEventChain
from .agent_mcp_proto import (
    AGENT_MCP_RUNNING_SESSION_TYPE_ID,
    AgentMcpRunningSessionProto,
)

_log = logging.getLogger(__name__)


class AgentMcpRunningSessionImpl(TypedServiceImpl[AgentMcpRunningSessionProto]):
    """Impl behind the workstation-exposed ``agent_mcp:{session_id}``
    service. See the module docstring for the full design.
    """

    def __init__(
        self,
        *,
        scope: FxtrScope,
        workstation_ref: ServiceRef[WorkstationServiceProto],
        inbox_callback_ref: ServiceRef[WorkstationSessionEventInbox],
        initial_head: EntityRef[WsAgentLogEventChain] | None,
        impl_tg: anyio.abc.TaskGroup,
    ) -> None:
        """Construct the impl.

        Args:
            scope: The impl scope. Long-lived (cycle-scoped). All
                bridges (listener refs, inbox callback) live here;
                ``get_state`` returns refs that are loadable here.
            workstation_ref: ``ServiceRef[WorkstationServiceProto]``
                for the MCP's workstation. Held for future expansion
                — the impl methods don't currently dispatch through
                it, but storing it as a ref (rather than a
                pre-resolved proxy bound to ``scope``) lets future
                callers open transient resolution scopes per the
                ``log_walker`` pattern, avoiding long-lived service
                bridges accumulating on ``scope``.
            inbox_callback_ref: ``ServiceRef[WorkstationSessionEventInbox]``
                for the session's reserved ``workstation_inbox`` callback.
                Caller (chunk D's bootstrap) bridges this into
                ``scope`` before passing it in, so resolving it from
                ``scope`` works.
            initial_head: The chain head as of impl construction —
                typically loaded from the persistence cap on session
                resubscribe (or ``None`` for a fresh session). Read
                by ``get_state`` until ``update_head`` replaces it,
                and seeded into each subscription so ``subscribe``
                delivers it as a first event.
            impl_tg: Task group for the hub's per-subscription
                workers. Cycle-scoped (cancels on cycle end).
        """
        self._scope = scope
        self._workstation_ref = workstation_ref
        self._inbox_callback_ref = inbox_callback_ref
        self._chain_head: EntityRef[WsAgentLogEventChain] | None = initial_head
        # Coalesce mode: subscribers want the latest head, not every
        # intermediate one (skip-ahead) — older heads stay reachable
        # via ``head.prev`` walks. The impl retains the latest head
        # itself (``_chain_head``); each ``subscribe`` seeds it, so no
        # hub-side retained value is needed.
        self._hub = SubscriberHub[EntityRef[WsAgentLogEventChain]](
            task_group=impl_tg,
            mode="coalesce",
        )

    def _target_type_annotation(self) -> Any:
        return AgentMcpRunningSessionProto

    def _service_type_id(self) -> FxtrID:
        return AGENT_MCP_RUNNING_SESSION_TYPE_ID

    # --- public (called by the log builder, not over /rpc) ----------

    @property
    def chain_head(self) -> EntityRef[WsAgentLogEventChain] | None:
        """Current chain head. Read-only view for callers that want to
        plug it into :func:`log_builder.extend_chain` as ``prev_head``."""
        return self._chain_head

    def update_head(self, new_head: EntityRef[WsAgentLogEventChain]) -> None:
        """Replace ``_chain_head`` with ``new_head`` and publish it to
        every subscriber.

        Sync — no awaits. The log builder calls this after a
        successful pin (or unconditionally when persistence is
        disabled). The hub's coalesce-mode delivery wakes each
        subscription's worker with the latest head (skip-ahead).
        """
        self._chain_head = new_head
        self._hub.publish(new_head, origin_scope=self._scope)

    # --- typed-service methods -------------------------------------

    @service_method
    async def get_state(self) -> EntityRef[WsAgentLogEventChain] | None:
        """Return the current chain head, or ``None`` for a fresh
        session.

        Reachability rationale (per scope discipline): the returned
        ``EntityRef``'s ``entity_id`` is loadable in ``self._scope``
        — either because it was the ``initial_head`` (caller passed
        a scope-loadable ref) or because it was minted by
        ``log_builder.extend_chain`` storing into ``self._scope``.
        The framework chain-bridges the ref through the dispatch
        ``call_scope`` back to the caller.
        """
        return self._chain_head

    @service_method
    async def subscribe(
        self, subscriber: ServiceRef[Subscriber[EntityRef[WsAgentLogEventChain]]]
    ) -> ServiceRef[UnsubscribeHandle]:
        """Subscribe ``subscriber`` to chain-head updates, delivering
        the current head (when one exists) as the first event.

        Delegates to the impl's coalesce-mode
        :class:`~fxtr2.services.subscription_util.SubscriberHub`,
        seeding the subscription with the impl's retained latest head
        (``seed=[head]``, ``seed_origin=self._scope`` — heads are
        stored there). The seed push and the registration share the
        hub's no-await window, so they are atomic with respect to
        concurrent ``update_head`` calls (no lost or duplicated head;
        a racing newer head simply supersedes the seed before the
        first delivery, coalesce-mode semantics). A fresh session
        (no head yet) delivers nothing until the first extension.

        See :meth:`AgentMcpRunningSessionProto.subscribe` for the
        caller-facing contract.
        """
        head = self._chain_head
        return await self._hub.subscribe(
            subscriber,
            get_active_scope(),
            seed=[head] if head is not None else None,
            seed_origin=self._scope,
        )

    @service_method
    async def send(self, payload: FxtrServiceData) -> None:
        """Route ``payload`` through the reserved ``workstation_inbox``
        callback.

        Resolves the pre-bridged inbox callback ref against the
        active **call_scope** (the per-call scope the typed-service
        framework opens to receive arguments) and dispatches
        ``notify``. The call_scope is a child of ``self._scope``, so
        the inbox ref — bridged into ``self._scope`` at bootstrap —
        is reachable via the parent chain; the proxy's home scope
        is the call_scope, so the wire encoder can find any entity
        refs the caller embedded in ``payload`` (those refs were
        bridged into the call_scope at decode time, not into
        ``self._scope``). Resolving against ``self._scope`` instead
        would leave the encoder unable to look up the entity since
        ``self._scope`` cannot see into its own child scopes.

        The workstation fires a ``CallbackEvent`` for
        ``workstation_inbox``, the MCP's ``/client`` reader queues
        an ``_EventJob``, and the worker eventually builds a
        :class:`CallbackNotificationLogEvent(slot_key="workstation_inbox",
        ...)` for the chain.
        """
        call_scope = get_active_scope()
        proxy = await self._inbox_callback_ref.resolve(scope=call_scope)
        await proxy.notify(payload)
