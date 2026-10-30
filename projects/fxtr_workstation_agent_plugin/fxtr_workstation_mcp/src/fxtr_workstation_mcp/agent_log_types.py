"""Typed event-log entity definitions for the agent MCP.

The MCP keeps a persistent, typed record of everything it does — script
executions, callback notifications it received, invoke results it
received, user messages — as a head-only linked list of
:class:`WsAgentLogEventChain` entities. The chain is pinned to a
workstation persistence cap (``agent_mcp_store`` by default) on every
extension, keyed by session UUID, so reconnects start from the prior
head rather than a blank slate.

Entities here are stored host-side (in the MCP process); the
:class:`AgentMcpRunningSessionImpl` mounts them in its impl_scope.
Frontends (separate ``/rpc`` peers) walk the chain by following
``WsAgentLogEventChain.prev``; they reach the typed payload fields through
the standard ``EntityRef.load()`` path.

## Substitution forms

A wire-form workstation payload carries entity / service / callback
refs as bracketed short_ids — ``["entity", "d:abc"]`` /
``["service", "s7"]`` / ``["callback", 4]``. The log walker
(``log_walker.py``) substitutes those leaves so the persistent record
holds typed Python objects rather than opaque short_ids:

- Entity short_id → ``EntityRef[UnknownEntity]`` (resolved via
  ``workstation_proxy.get_entity``). Round-trips through ``load()``.
- Service short_id → ``EntityRef[WorkstationServicePlaceholder]``.
  The placeholder is a stored :class:`DataclassEntity` whose three
  fields capture the service's identity (``service_type_id``,
  ``service_ctx``) and the live workstation short_id.
- Owned-callback slot N → ``EntityRef[OwnedCallbackPlaceholder]``.
  ``slot_n`` is captured plus ``slot_key`` for human readability
  (best-effort; ``"<unknown>"`` for closed-and-forgotten slots).

Content-addressing means an identical service / callback appearing
multiple times in the same chain (or across chains) maps to one stored
placeholder entity.

## Module shape

Foundational types:

- :data:`WS_AGENT_NS` — stable namespace UUID for every type defined
  here.
- :class:`WorkstationServicePlaceholder` /
  :class:`OwnedCallbackPlaceholder` — substitution targets.
- :class:`ScriptPreamble` — content-addressed preamble source so
  identical preambles share storage across many script runs.

Log spine + event payloads:

- :class:`WsAgentLogEventChain` — the head-only linked-list spine.
- :class:`ScriptExecutionLogEvent` — one script's outcome (success,
  error, or rejection), with its per-op record list.
- :class:`InvokeResultLogEvent` / :class:`InvokeErrorLogEvent` /
  :class:`CallbackNotificationLogEvent` — incoming workstation events
  the MCP processed.
- :class:`ScriptOpRecord` — single flat :class:`DataclassFxtrSubpart`
  with a ``kind`` discriminator and per-kind nullable fields. One
  record per script-side op.

Module-level constants ``OP_KIND_*`` are the canonical string values
for ``ScriptOpRecord.kind``; consumers should compare against these
rather than typing string literals.

Naming note: each event entity uses the ``LogEvent`` suffix
specifically to avoid name collisions with the workstation's
wire-protocol types (``InvokeResultEvent`` / ``InvokeErrorEvent`` /
``CallbackEvent`` live in :mod:`fxtr_workstation.client_protocol`).
The ``Log`` infix makes the agent-side persistent-log distinction
explicit at the import site.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Literal

from fxtr2.conversion.converters.mashumaro_entities import (
    DataclassEntity,
    DataclassFxtrSubpart,
    EntityRef,
)
from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FrozenFxtrCbor, FxtrID

#: Stable namespace UUID for fxtr_workstation_mcp's typed event-log
#: entities. Distinct from :data:`fxtr_ws_mcp_sandbox_bridge.protocols.WS_MCP_NS`
#: so log entity type ids are independent of sandbox-bridge type ids.
WS_AGENT_NS = uuid.UUID("8b3a7e1d-c4f0-46d2-9a51-7e8c1f3b2a09")


# --- Substitution-target entities -------------------------------------


@dataclasses.dataclass(frozen=True)
class WorkstationServicePlaceholder(
    DataclassEntity,
    fxtr_type=system_entities.define_type(WS_AGENT_NS, "workstation_service_placeholder"),
):
    """Stored substitute for a ``["service", short_id]`` wire-form leaf.

    The log walker resolves the service ref via ``get_service(short_id)``,
    invokes ``$fxtr.type`` + ``$fxtr.ctx`` framework methods to capture
    identity, and stores one of these. The resulting :class:`EntityRef`
    is what lands in the typed log payload.

    ``short_id`` is the workstation's run-lifetime allocation; it stays
    stable while the workstation is alive but is forgotten on
    workstation restart. Persistent storage of the log across restart
    will therefore reference services whose short_ids may no longer
    resolve; ``service_type_id`` / ``service_ctx`` are the durable
    identity.

    Content-addressed via mashumaro: two refs to a service with
    matching ``(service_type_id, service_ctx, short_id)`` collapse to
    one stored entity.
    """

    short_id: str
    service_type_id: FxtrID
    service_ctx: FxtrID


@dataclasses.dataclass(frozen=True)
class OwnedCallbackPlaceholder(
    DataclassEntity,
    fxtr_type=system_entities.define_type(WS_AGENT_NS, "owned_callback_placeholder"),
):
    """Stored substitute for a ``["callback", N]`` wire-form leaf.

    Wire ``["callback", N]`` references a workstation-side callback
    the MCP's session owns (created via a ``CreateCallbackOp``).
    ``slot_n`` is the workstation slot number; ``slot_key`` is the
    agent-facing slot string (e.g. ``"workstation_inbox"``).

    ``slot_key`` is reliably looked up via
    ``mirror.pending_slot_index[N]`` while the callback is still
    open (the index is kept past first-fire for callback slots —
    only invoke slots drop on promote). The literal sentinel
    ``"<unknown>"`` covers the pathological case where the slot has
    already been closed and forgotten by the time we walk a payload
    referencing it; in that case the walker logs a warning.
    """

    slot_n: int
    slot_key: str


# --- Preamble (content-addressed for storage reuse) -------------------


@dataclasses.dataclass(frozen=True)
class ScriptPreamble(
    DataclassEntity,
    fxtr_type=system_entities.define_type(WS_AGENT_NS, "script_preamble"),
):
    """The source of the working directory's ``preamble.py``, captured
    when a script runs.

    Stored separately from :class:`ScriptExecutionLogEvent` so identical
    preambles (the common case — preambles rarely change between
    runs) share content-addressed storage. The preamble's source is
    what's exec'd by the sandbox runner before each script.
    """

    source: str


# --- ScriptOpRecord — flat-shape discriminated subpart ----------------

#: ``ScriptOpRecord.kind`` discriminator values. Mirrors the sandbox-
#: bridge ``QueuedOp.kind`` strings so the host-side walker can
#: translate directly from the wire-form queued op dicts.
OP_KIND_EXPORT_ENTITY = "export_entity"
OP_KIND_EXPORT_SERVICE = "export_service"
OP_KIND_FORGET = "forget"
OP_KIND_SCHEDULE_INVOKE = "schedule_invoke"
OP_KIND_SCHEDULE_CALLBACK_INVOKE = "schedule_callback_invoke"
OP_KIND_CREATE_CALLBACK = "create_callback"
OP_KIND_FORGET_CALLBACK_NOTIFICATION = "forget_callback_notification"
OP_KIND_PRESENT_TO_USER = "present_to_user"

#: Literal type for :attr:`ScriptOpRecord.kind`. Pyright-checks
#: callsites that build :class:`ScriptOpRecord` against the canonical
#: set; the runtime check is implicit via mashumaro's literal
#: validation.
OpKind = Literal[
    "export_entity",
    "export_service",
    "forget",
    "schedule_invoke",
    "schedule_callback_invoke",
    "create_callback",
    "forget_callback_notification",
    "present_to_user",
]


@dataclasses.dataclass(frozen=True)
class ScriptOpRecord(DataclassFxtrSubpart):
    """One per-op record inside a :class:`ScriptExecutionLogEvent`.

    Flat-shape dataclass with a string discriminator (``kind``). Each
    per-kind extra field defaults to ``None``; callers populate only
    the fields relevant to the op kind. The trade-off vs. distinct
    per-kind subclasses: a slightly wider record with some ``None``
    fields, but simple mashumaro round-trip with no custom
    discriminator wiring on :class:`DataclassFxtrSubpart` (which has
    none built-in — only :class:`DataclassEntity` does).

    Per-kind field mapping:

    - ``export_entity``: ``slot_key``, ``entity``.
    - ``export_service``: ``slot_key``, ``service``.
    - ``forget``: ``slot_key``.
    - ``schedule_invoke``: ``slot_key``, ``service_short_id``,
      ``method_name``, ``args``, ``kwargs``.
    - ``schedule_callback_invoke``: ``slot_key``, ``callback`` (the
      walked owned-callback target — an
      ``EntityRef[OwnedCallbackPlaceholder]`` leaf), ``method_name``,
      ``args``, ``kwargs``.
    - ``create_callback``: ``slot_key``.
    - ``forget_callback_notification``: ``slot_key``, ``notif_index``.
    - ``present_to_user``: ``payload``.

    ``args`` / ``kwargs`` / ``payload`` carry the walker's
    substituted output — :data:`FrozenFxtrCbor` trees with typed
    ``EntityRef`` leaves rather than wire-form bracketed shapes.
    """

    kind: OpKind
    slot_key: str | None = None
    entity: EntityRef[UnknownEntity] | None = None
    service: EntityRef[WorkstationServicePlaceholder] | None = None
    service_short_id: str | None = None
    callback: FrozenFxtrCbor = None
    method_name: str | None = None
    args: FrozenFxtrCbor = None
    kwargs: FrozenFxtrCbor = None
    notif_index: int | None = None
    payload: FrozenFxtrCbor = None


# --- Event payload entities -------------------------------------------


@dataclasses.dataclass(frozen=True)
class ScriptExecutionLogEvent(
    DataclassEntity,
    fxtr_type=system_entities.define_type(WS_AGENT_NS, "log_script_execution"),
):
    """One script's outcome.

    Three terminal shapes:

    - **Success**: ``error is None``, ``rejected is None``, ``ops``
      is the recorded op list, ``committed_seq`` is the workstation's
      assigned seq.
    - **Script-raised**: ``error`` is the **full** formatted
      traceback — exactly the string the sandbox runner produces
      from ``traceback.format_exception``. Persisted unmodified on
      the chain so frontend watchers can show operators the whole
      stack. The agent's tool response sees a **separate
      compressed** two-frame summary (see
      :class:`ws_connection.ScriptOutcome` and
      :func:`ws_connection._compress_script_traceback`); the same
      full string is also stashed in
      :class:`exception_log.ExceptionLog` for the agent to fetch on
      demand via ``get_full_exception(exc_id)``. ``ops`` is empty —
      script semantics are atomic, no commit happened.
    - **Workstation-rejected**: ``rejected`` is the workstation's
      rejection reason. ``ops`` is empty — the mutation didn't
      commit.

    The ``ops`` tuple is empty on both error paths; informative
    detail lives in ``stderr`` / ``error`` / ``rejected``.

    No-op scripts (empty queued_ops, no error) still produce an
    event — frontends can choose to filter these. Keeps the chain a
    faithful record of every ``run_script`` call.
    """

    script_src: str
    preamble: EntityRef[ScriptPreamble]
    stdout: str
    stderr: str
    error: str | None
    rejected: str | None
    ops: tuple[ScriptOpRecord, ...]
    committed_seq: int | None


@dataclasses.dataclass(frozen=True)
class InvokeResultLogEvent(
    DataclassEntity,
    fxtr_type=system_entities.define_type(WS_AGENT_NS, "log_invoke_result"),
):
    """An ``InvokeResultEvent`` the MCP received from the workstation,
    captured for the persistent log (seq + walked payload).

    Distinguished from a callback notification by the absence of a
    ``notif_index`` and by the fact that an invoke slot resolves
    once (the slot's ``data_by_key`` value flips from
    ``["invoke_pending", N]`` to ``["invoke_result", ...]`` on
    arrival).
    """

    slot_key: str
    slot_n: int
    seq: int
    payload: FrozenFxtrCbor


@dataclasses.dataclass(frozen=True)
class InvokeErrorLogEvent(
    DataclassEntity,
    fxtr_type=system_entities.define_type(WS_AGENT_NS, "log_invoke_error"),
):
    """The workstation reported an error for the invoke at this slot.

    The slot's ``data_by_key`` value is now ``["invoke_error", msg]``;
    ``message`` is the string the workstation surfaced.
    """

    slot_key: str
    slot_n: int
    seq: int
    message: str


@dataclasses.dataclass(frozen=True)
class CallbackNotificationLogEvent(
    DataclassEntity,
    fxtr_type=system_entities.define_type(WS_AGENT_NS, "log_callback_notification"),
):
    """One workstation ``CallbackEvent`` the MCP received on an open
    callback slot.

    Multiple notifications per slot are normal (callbacks fire
    repeatedly). The ``seq`` is the originating event's session seq
    — also the ``notif_index`` the agent retrieves the payload by
    via ``retrieve_callback_notification``.

    Messages sent through ``AgentMcpRunningSessionProto.send`` flow
    through the reserved ``workstation_inbox`` callback, so they
    land here as ``CallbackNotificationLogEvent(slot_key="workstation_inbox",
    ...)``. Frontends distinguish user messages from regular callback
    events by inspecting ``slot_key``.
    """

    slot_key: str
    slot_n: int
    seq: int
    payload: FrozenFxtrCbor


# --- Log spine --------------------------------------------------------


#: Type alias for the union of event payload entity types the chain
#: spine can reference via :attr:`WsAgentLogEventChain.event`. Used as
#: the ``EntityRef[...]`` parameter; mashumaro discriminates on
#: ``$fxtr.type`` automatically.
WsAgentLogEvent = (
    ScriptExecutionLogEvent
    | InvokeResultLogEvent
    | InvokeErrorLogEvent
    | CallbackNotificationLogEvent
)


@dataclasses.dataclass(frozen=True)
class WsAgentLogEventChain(
    DataclassEntity,
    fxtr_type=system_entities.define_type(WS_AGENT_NS, "ws_agent_log_chain"),
):
    """Head-only linked-list spine of agent events.

    Each node references the previous node + the typed event payload.
    The persistence cap stores only the current head's id under key
    ``session_id``; transitive ancestors stay reachable because the
    head's stored cbor references them via :attr:`prev`.

    ``timestamp`` is the host's unix-seconds clock at the time of
    chain extension. The workstation seq inside the wrapped event
    (where applicable) is the authoritative ordering — the timestamp
    is an informational annotation for human-readable rendering.
    """

    prev: EntityRef[WsAgentLogEventChain] | None
    event: EntityRef[WsAgentLogEvent]
    timestamp: float
