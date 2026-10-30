"""Local mirror of the active workstation session's state.

The mirror is the MCP's **local view** of the active session,
which equals the workstation view (raw state dict carried by
the most-recent state_update) plus the replayed effects of any
non-state events with ``seq > last_state_ack_seq`` that the MCP
has already applied locally. The locally-absorbed effects are
also recorded as :class:`PendingChange` entries on
:attr:`WorkstationConnection._pending_changes`; the mutation
builder consumes them to flush the workstation's view forward
on the next mutation.

Updated only on the worker task: at subscribe-time (after wiping
local state), on every foreign state_update via
``_do_event_job``, and on every self-commit via
``_apply_local_commit``.

Module exposes:

- :class:`SessionMirror` — dataclass holding the raw workstation
  ``state`` dict, ``data_by_key`` + ``callback_notifications``
  (the *local view*), workstation-reported tracker sets, and the
  derived ``pending_slot_index`` reverse-lookup.
  ``apply_workstation_view`` is the single primitive that
  replaces the mirror's contents from a new workstation view.
- :class:`PendingChange` — one queued intent to mutate the
  workstation's session state; translated to a JSON Patch op at
  mutation-build time. Replaces the older split between
  ``UnflushedChange`` (data_by_key effects) and
  ``UnflushedNotification`` (callback_notifications effects).
- :func:`workstation_state_needs_init` — predicate used by the
  connection to decide whether to enqueue a synthetic
  ``"initialize"`` :class:`PendingChange` (creates the empty
  top-level fields the MCP writes to).
- :func:`desired_tracker_set` — pure function: given
  ``data_by_key`` (and optionally ``callback_notifications``),
  return the entity / service short_ids the session should be
  tracking.

Reads from this module are sync and side-effect-free; all
in-place mutation of ``SessionMirror`` happens on the worker
task.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


# --- Op-kind constants -----------------------------------------------
#
# String tags carried in the runner's queued-ops list (asdict'd from
# ``fxtr_ws_mcp_sandbox_bridge.protocols.{ExportEntityOp,
# ExportServiceOp, ForgetOp, ScheduleInvokeOp, ScheduleCallbackInvokeOp,
# CreateCallbackOp, ForgetCallbackNotificationOp}``). Kept in this
# module so the host code can dispatch without importing the bridge
# protocol's dataclasses everywhere.
KIND_EXPORT_ENTITY = "export_entity"
KIND_EXPORT_SERVICE = "export_service"
KIND_FORGET = "forget"
KIND_SCHEDULE_INVOKE = "schedule_invoke"
#: ``schedule_invoke`` whose *target* is an owned callback (the
#: ``ScheduleCallbackInvokeOp`` queued-op variant); emitted on the
#: wire as a workstation ``CallbackInvokeOp``. Slot-claiming, same
#: ``data_by_key`` lifecycle as :data:`KIND_SCHEDULE_INVOKE`.
KIND_SCHEDULE_CALLBACK_INVOKE = "schedule_callback_invoke"
KIND_CREATE_CALLBACK = "create_callback"
KIND_FORGET_CALLBACK_NOTIFICATION = "forget_callback_notification"
#: ``present_to_user`` is MCP-side only — no JSON Patch, no wire op,
#: never reaches the workstation. The mutation builder ignores it
#: when emitting wire ops but the log writer picks it up as a
#: ``PresentToUserOpRecord`` entry on the committed script's
#: ``ScriptExecutionLogEvent.ops`` list.
KIND_PRESENT_TO_USER = "present_to_user"

#: Wire-form tag used inside ``data_by_key`` for an open callback
#: slot. Mirrors ``invoke_pending`` in shape but never
#: auto-promotes — the slot stays as ``["callback_open", N]``
#: until the agent explicitly ``forget(slot_key)``s the slot
#: (which auto-emits ``CloseCallbackOp``).
CALLBACK_OPEN_TAG = "callback_open"

#: Slot_keys reserved for MCP-internal use. Scripts that target one
#: of these get a tool-error before the mutation is built.
#:
#: - ``workstation_inbox``: backs the
#:   :class:`AgentMcpRunningSessionImpl.send` path. The MCP creates
#:   this callback during session bootstrap; agent code must not
#:   write to it (an overwrite would lose the slot's
#:   ``pending_slot_index`` entry and orphan the inbox).
RESERVED_SLOT_KEYS: frozenset[str] = frozenset({"workstation_inbox"})


# --- The mirror ------------------------------------------------------


@dataclass(frozen=True)
class PendingChange:
    """One queued intent to mutate the workstation's session state.

    Translated to a JSON Patch op at mutation-build time.

    ``op``:

    - ``"initialize"`` — synthetic prologue that adds the empty
      top-level state objects the MCP writes to (``data_by_key``
      and ``callback_notifications``). Enqueued by the
      connection when ``apply_workstation_view`` lands an empty
      session state; cleared (along with everything else) on
      self-commit. ``path`` and ``value`` are unused.
    - ``"set"`` — set the value at ``path`` (emitted as JSON
      Patch ``add`` or ``replace`` depending on running state).
      The mutation builder lazy-creates any *sub-container* on
      the path that isn't yet present (e.g.
      ``/callback_notifications/<slot_key>`` when the first
      notif for a slot_key arrives) — but the top-level fields
      are always assumed to exist by the time a ``set`` runs,
      because ``"initialize"`` already created them.
    - ``"remove"`` — remove the value at ``path``.

    ``path`` is a tuple of JSON-pointer segments. Examples:
    ``("data_by_key", "cb")``,
    ``("callback_notifications", "cb", "47")``.

    ``seq`` is the source non-state event's seq for entries
    derived from :meth:`_apply_state_effect`; ``None`` for the
    synthetic ``"initialize"`` entry. Used by
    :meth:`_handle_state_update` to drop entries past the
    workstation's ack horizon.
    """

    op: Literal["initialize", "set", "remove"]
    path: tuple[str, ...] = ()
    value: Any = None
    seq: int | None = None


def workstation_state_needs_init(state: Any) -> bool:
    """Whether the workstation's session ``state`` lacks the
    top-level fields the MCP writes to.

    Returns ``True`` when ``data_by_key`` or
    ``callback_notifications`` is absent. The connection uses
    this to decide whether ``apply_workstation_view`` should
    enqueue a synthetic ``PendingChange(op="initialize")``
    prologue.
    """
    if not isinstance(state, dict):
        return True
    return "data_by_key" not in state or "callback_notifications" not in state


@dataclass
class SessionMirror:
    """The MCP's local mirror of the active session's state.

    Fields are mutated in place by the worker. ``data_by_key`` is
    the **local view**: the workstation's
    ``state["data_by_key"]`` (as carried by the last applied
    state_update) plus reapplied effects of any non-state events
    the connection is holding in ``_replayable_events`` (seq >
    ``last_state_ack_seq``). ``ws_tracked_entities`` /
    ``ws_tracked_services`` mirror the workstation's last-
    reported pin sets (diagnostic only — we derive our own
    ``desired_tracker_set`` from ``data_by_key``).

    ``last_state_seq`` / ``last_state_ack_seq`` track the
    most-recently applied state_update's seq + ack_seq. Used for
    the seq-regression check in ``_handle_state_update`` and to
    bound which events stay in ``_replayable_events`` (those with
    seq > ``last_state_ack_seq``). Both ``0`` for a freshly-
    constructed mirror (the no-prior-state branch).

    ``pending_slot_index`` is a derived reverse-lookup index:
    maps each ``slot: int`` to the ``slot_key: str`` whose value
    in ``data_by_key`` is ``["invoke_pending", slot]`` /
    ``["callback_open", slot]``. Lets ``_apply_state_effect`` find
    the slot_key for an arriving result/callback event in O(1).
    Maintained by ``apply_workstation_view`` (rebuild from scratch)
    and by the per-kind dispatch in ``_apply_state_effect`` (drop
    entry on promote for invoke; keep the entry for
    ``callback_open`` because callbacks fire repeatedly). Slot
    numbers are unique across kinds per the workstation's
    slot-monotonicity rule.

    ``callback_notifications`` (stage 6) is the local view of the
    session's ``callback_notifications`` top-level state field —
    a sibling of ``data_by_key`` that the MCP introduces to
    persist :class:`CallbackEvent` payloads across reconnects and
    let the agent retrieve them via
    ``retrieve_callback_notification``. Outer keys are
    agent-facing slot_keys; inner keys are stringified
    ``notif_index`` ints (= the originating ``CallbackEvent.seq``,
    monotonically increasing across the session lifetime); inner
    values are wire-escaped payloads.
    """

    state: dict[str, Any] = field(default_factory=dict)
    data_by_key: dict[str, list[Any]] = field(default_factory=dict)
    callback_notifications: dict[str, dict[str, list[Any]]] = field(default_factory=dict)
    ws_tracked_entities: set[str] = field(default_factory=set)
    ws_tracked_services: set[str] = field(default_factory=set)
    last_state_seq: int = 0
    last_state_ack_seq: int = 0
    pending_slot_index: dict[int, str] = field(default_factory=dict)
    #: Local view of each callback inbox slot's ``subscribe_to``
    #: connection state, keyed by ``slot_key`` → ``"connected"`` /
    #: ``"broken"``. Reduced from the seq'd ``CallbackSubscriptionEvent``
    #: stream (latest wins); a slot_key absent here is "idle" (never
    #: subscribed). Not part of the workstation's persistent state — it
    #: is reset on ``apply_workstation_view`` and rebuilt by replaying
    #: the surviving non-state events, so a transition acked past the
    #: retention horizon is not recovered on a cold attach.
    callback_subscription_states: dict[str, str] = field(default_factory=dict)

    def apply_workstation_view(
        self,
        *,
        state: dict[str, Any],
        last_state_seq: int,
        last_state_ack_seq: int,
        tracked_entities: list[str],
        tracked_services: list[str],
    ) -> None:
        """Replace mirror contents with the workstation's view at
        ``last_state_seq``. After this, ``data_by_key`` and
        ``callback_notifications`` reflect only the workstation
        view; the caller is responsible for re-applying any
        remaining non-state event effects on top to restore the
        local view (see ``_handle_state_update`` rebuild loop)."""
        self.state = state
        raw = state.get("data_by_key", {}) if isinstance(state, dict) else {}
        # Shallow-copy the outer dict so downstream mutations don't
        # reach back into ``state``. The values are list-tagged
        # immutable shapes; no need for deep.
        self.data_by_key = dict(raw)
        raw_notifs = state.get("callback_notifications", {}) if isinstance(state, dict) else {}
        # Two-level dict; copy both levels so we can mutate locally
        # without reaching back into ``state``.
        self.callback_notifications = {
            slot_key: dict(per_slot) for slot_key, per_slot in raw_notifs.items()
        }
        self.ws_tracked_entities = set(tracked_entities)
        self.ws_tracked_services = set(tracked_services)
        self.last_state_seq = last_state_seq
        self.last_state_ack_seq = last_state_ack_seq
        # Rebuild pending_slot_index from the fresh data_by_key.
        self.pending_slot_index = {}
        for slot_key, value in self.data_by_key.items():
            if _is_pending_marker(value):
                self.pending_slot_index[value[1]] = slot_key
        # Connection state is not carried in the workstation view; the
        # caller re-derives it by replaying surviving non-state events.
        self.callback_subscription_states = {}


def _is_pending_marker(value: Any) -> bool:
    """Whether ``value`` is a recognized slot marker that
    contributes to :attr:`SessionMirror.pending_slot_index`.

    Two shapes today: ``["invoke_pending", slot]`` (waiting on an
    async invoke result) and ``["callback_open", slot]`` (a
    callback that can fire repeatedly). The index lookup is
    uniform across both; the caller's per-kind branch decides
    whether to drop the index entry on promote (invoke: yes;
    callback: no).
    """
    return (
        isinstance(value, list)
        and len(value) == 2
        and value[0] in ("invoke_pending", CALLBACK_OPEN_TAG)
        and isinstance(value[1], int)
    )


# --- Errors ----------------------------------------------------------


class ForgetMissingKey(LookupError):
    """A queued ``forget`` op referenced a key that isn't currently
    in ``data_by_key`` (or already removed by an earlier op in the
    same script). Callers surface this as a tool error before sending
    the mutation; the workstation would reject with
    ``validation_failed`` either way, but catching it locally yields
    a cleaner message."""

    def __init__(self, slot_key: str) -> None:
        super().__init__(slot_key)
        self.slot_key = slot_key


class SlotKeyReserved(ValueError):
    """A queued script op targeted a slot_key that's reserved for
    MCP-internal use (see :data:`RESERVED_SLOT_KEYS`).

    Caller (:meth:`_apply_script_mutation`) catches this and surfaces
    a tool-error string before sending the mutation. The script
    didn't commit — the agent retries with a different slot_key.
    """

    def __init__(self, slot_key: str, kind: str) -> None:
        super().__init__(slot_key)
        self.slot_key = slot_key
        self.kind = kind


class SlotKeyAlreadyExists(ValueError):
    """A queued script op (``export_entity`` / ``export_service`` /
    ``schedule_invoke`` / ``create_callback``) tried to write to a
    ``data_by_key`` slot that's already populated.

    Silently overwriting is dangerous for callbacks specifically: a
    second ``create_callback("foo")`` against an existing
    ``["callback_open", N]`` value leaks the old slot's workstation-side
    callback (still receiving events) but loses our local
    ``pending_slot_index`` entry. When the orphaned slot's
    :class:`CallbackEvent` arrives, the MCP can't reverse-map to a
    slot_key and raises :class:`ProtocolError`. The check is uniform
    across kinds so the rule is "explicit cleanup before reuse"
    rather than per-kind special cases.

    Caller surfaces this as a tool error before sending the mutation;
    the agent fixes it with a prior ``forget(slot_key)`` script."""

    def __init__(self, slot_key: str, kind: str, running_value: Any) -> None:
        super().__init__(slot_key)
        self.slot_key = slot_key
        self.kind = kind
        self.running_value = running_value


# --- Pure helpers ----------------------------------------------------


def desired_tracker_set(
    data_by_key: dict[str, list[Any]],
    callback_notifications: dict[str, dict[str, list[Any]]] | None = None,
) -> tuple[set[str], set[str]]:
    """Compute the set of entity / service short_ids the session
    should track, given a ``data_by_key`` snapshot and (stage 6)
    a ``callback_notifications`` snapshot.

    Walks each value:

    - ``["exported_entity", id]`` → tracks ``id`` as an entity.
    - ``["exported_service", id]`` → tracks ``id`` as a service.
    - ``["invoke_result", payload]`` → walks ``payload`` recursively
      for entity / service tags (stage 4 lands these in
      ``data_by_key``; stage 3a's walk is forward-compat).
    - ``["callback_open", N]`` → no tracker contribution; the
      callback's send-side service is pinned by the workstation's
      session ``open_callbacks`` table independently of our
      tracker.

    Stage 6: the ``callback_notifications`` arg (top-level state
    field) is walked the same way as ``data_by_key`` values —
    each stored payload is a wire-escaped value whose entity /
    service tags contribute to the tracker. This is what keeps
    entity refs surfaced in callback payloads pinned past the
    workstation's event-log retention horizon for the originating
    :class:`CallbackEvent`.
    """
    entities: set[str] = set()
    services: set[str] = set()
    for value in data_by_key.values():
        _walk_for_tracker_set(value, entities, services)
    if callback_notifications:
        for per_slot in callback_notifications.values():
            for payload in per_slot.values():
                _walk_for_tracker_set(payload, entities, services)
    return entities, services


def _walk_for_tracker_set(
    value: Any,
    entities: set[str],
    services: set[str],
) -> None:
    if isinstance(value, list):
        # Two-element tagged form: dispatch on tag.
        if len(value) == 2 and isinstance(value[0], str):
            tag, payload = value[0], value[1]
            if tag == "exported_entity" and isinstance(payload, str):
                entities.add(payload)
                return
            if tag == "exported_service" and isinstance(payload, str):
                services.add(payload)
                return
            if tag == "entity" and isinstance(payload, str):
                # Stage-4 invoke-result payloads carry entity refs as
                # ``["entity", short_id]`` (list-based escape). Pull
                # them into the tracker set so retained results keep
                # their refs alive.
                entities.add(payload)
                return
            if tag == "service" and isinstance(payload, str):
                services.add(payload)
                return
            if tag == "invoke_result":
                _walk_for_tracker_set(payload, entities, services)
                return
            if tag in ("invoke_pending", "bytes", CALLBACK_OPEN_TAG, "callback"):
                # No tracker contribution. ``CALLBACK_OPEN_TAG`` /
                # ``callback`` refer to workstation-internal
                # callback services, which the session's
                # ``open_callbacks`` table pins independently of
                # our tracker.
                return
            # Fall through: not a recognized tag. Recurse defensively.
        # Single-element wrapper for literal lists: ``[[...]]``.
        if len(value) == 1 and isinstance(value[0], list):
            for item in value[0]:
                _walk_for_tracker_set(item, entities, services)
            return
        # Other lists: recurse element-by-element. Should be rare.
        for item in value:
            _walk_for_tracker_set(item, entities, services)
        return
    if isinstance(value, dict):
        for v in value.values():
            _walk_for_tracker_set(v, entities, services)
