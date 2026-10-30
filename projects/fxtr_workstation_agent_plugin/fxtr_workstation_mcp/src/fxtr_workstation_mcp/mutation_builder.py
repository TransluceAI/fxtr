"""Build a :class:`MutateSessionRequest` from pending changes + a
script's queued ops.

Pure-function module — takes the current :class:`SessionMirror`,
the connection's ``_pending_changes`` log, the runner's
queued-ops list, and the worker-built ``handle → short_id`` dict
of registered entities, and returns:

- a :class:`MutateSessionRequest` carrying the JSON Patch + tracker
  diff + workstation op-list + ack bookkeeping; or
- ``None`` if the mutation would be a no-op (no patch + no tracker
  diff + no ops). The caller short-circuits the wire entirely in
  that case.

Translation strategy: walk the unified
``pending_changes + runner_queued_ops`` sequence in order,
simulating each change against a running copy of the workstation's
pre-state. Each change emits one or more JSON Patch ops (with lazy
``add /<container> {}`` precursors for any intermediate object
that doesn't yet exist in the running state). The final running
state is the ``post_state`` we hand back to the caller, who plugs
it into the mirror on commit.

Three kinds of ``PendingChange``:

- ``op="initialize"`` → emits ``add /data_by_key {}`` and
  ``add /callback_notifications {}``. Always synthesized when the
  workstation's view lacks these top-level fields (fresh session,
  post-subscribe wipe).
- ``op="set"`` → emits ``add`` (path doesn't exist) or ``replace``
  (path exists). Lazy-creates intermediate containers.
- ``op="remove"`` → emits ``remove``.

Script ops:

- ``KIND_EXPORT_ENTITY`` / ``KIND_EXPORT_SERVICE`` /
  ``KIND_SCHEDULE_INVOKE`` / ``KIND_SCHEDULE_CALLBACK_INVOKE`` /
  ``KIND_CREATE_CALLBACK`` → write to
  ``data_by_key`` as add/replace; produce a corresponding wire
  op (``TrackEntityOp`` via the tracker diff for export ops,
  ``InvokeOp`` / ``CreateCallbackOp`` for slot-claiming ops).
- ``KIND_FORGET`` → ``remove /data_by_key/<slot_key>``. If the
  running value was ``["callback_open", N]``, *also* emits a
  ``CloseCallbackOp(slot=N)`` workstation op.
- ``KIND_FORGET_CALLBACK_NOTIFICATION`` →
  ``remove /callback_notifications/<slot_key>/<notif_index>``.

Tracker diff: ``desired_tracker_set`` walks the post-state's
``data_by_key`` and ``callback_notifications`` and diffs against
the workstation's last-reported pin sets.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from fxtr_workstation.client_protocol import (
    CloseCallbackOp,
    CreateCallbackOp,
    CallbackInvokeOp,
    InvokeOp,
    MutateSessionRequest,
    MutationOp,
    TrackEntityOp,
    TrackServiceOp,
    UntrackEntityOp,
    UntrackServiceOp,
)

from fxtr_ws_mcp_sandbox_bridge.protocols import slot_handle

from .session_state import (
    CALLBACK_OPEN_TAG,
    KIND_CREATE_CALLBACK,
    KIND_EXPORT_ENTITY,
    KIND_EXPORT_SERVICE,
    KIND_FORGET,
    KIND_FORGET_CALLBACK_NOTIFICATION,
    KIND_PRESENT_TO_USER,
    KIND_SCHEDULE_CALLBACK_INVOKE,
    KIND_SCHEDULE_INVOKE,
    RESERVED_SLOT_KEYS,
    ForgetMissingKey,
    PendingChange,
    SessionMirror,
    SlotKeyAlreadyExists,
    SlotKeyReserved,
    desired_tracker_set,
)


@dataclass(frozen=True)
class BuildResult:
    """Result of :func:`build_mutate_request`.

    Carries the wire :class:`MutateSessionRequest`, the full
    ``post_state`` after the patch applies (so the caller plugs
    it into the mirror without re-applying), desired tracker sets,
    and the new ``_slot_counter`` value after this mutation's
    slot-claiming ops (invokes + create_callbacks) consume their
    slots.

    ``substituted_runner_queued_ops`` is the post-placeholder-
    substitution form of ``runner_queued_ops`` — each op's args /
    kwargs / payload has ``["entity_handle", h]`` → ``["entity",
    short_id]`` and ``["callback_handle", k]`` → ``["callback",
    N]`` rewritten. Used by chunk-E's log writer (which expects
    wire-final shapes for the substitution walker).
    ``slot_allocations`` mirrors the internal allocation map so
    callers can reason about the slot ↔ slot_key mapping for the
    invoke / create_callback ops in this mutation.
    """

    request: MutateSessionRequest
    post_state: dict[str, Any]
    desired_entities: set[str]
    desired_services: set[str]
    new_slot_counter: int
    substituted_runner_queued_ops: list[dict[str, Any]]
    slot_allocations: dict[tuple[str, str], int]


def build_mutate_request(
    *,
    mirror: SessionMirror,
    pending_changes: list[PendingChange],
    runner_queued_ops: list[dict[str, Any]],
    entity_short_ids: dict[str, str],
    slot_counter: int,
    ack_seq: int,
    request_id: int,
    subscription_id: int,
    update_id: int,
    allow_reserved_slot_keys: bool = False,
) -> BuildResult | None:
    """Translate ``pending_changes`` + ``runner_queued_ops`` into
    a ``MutateSessionRequest`` plus the post-state needed to
    update the mirror on commit.

    Returns ``None`` if the result would be a no-op (empty patch
    + empty tracker diff + no workstation ops). An empty
    pending-changes log + empty script yields ``None``; if either
    contributes, we send a wire mutation so the workstation
    absorbs the pending effects.

    Raises :class:`session_state.ForgetMissingKey` if any
    ``forget`` op references a missing data_by_key slot, or any
    ``forget_callback_notification`` references a missing
    notification — caller surfaces as a tool error before sending.
    Neither the mirror nor the pending-changes log is modified.

    ``slot_counter`` is the connection's cached
    ``_slot_counter`` (the workstation's last-known slot_counter
    value). Each ``ScheduleInvokeOp`` and ``CreateCallbackOp``
    claims the next monotonic slot in script order
    (``slot_counter + 1, slot_counter + 2, …``). On commit, the
    connection updates ``_slot_counter`` to
    ``BuildResult.new_slot_counter``.

    ``ack_seq`` is the value we send to the workstation — typically
    the connection's ``_last_processed_seq``. The mutation's
    tracker diff is computed against ``mirror.ws_tracked_*`` (the
    workstation's last-reported pin set), but ack semantics are
    decoupled from the mirror's ``last_state_seq``.

    ``allow_reserved_slot_keys`` defaults to ``False`` — ops
    targeting a slot_key in :data:`RESERVED_SLOT_KEYS` raise
    :class:`SlotKeyReserved`. The supervisor's
    ``_bootstrap_workstation_inbox`` path sets this to ``True`` so
    its synthetic ``create_callback("workstation_inbox")`` op can
    succeed; agent-script callers always leave it ``False``.
    """
    slot_allocations = _allocate_slots(runner_queued_ops, slot_counter)
    new_slot_counter = slot_counter + len(slot_allocations)

    # Running state, simulated by walking changes in order.
    state: dict[str, Any] = copy.deepcopy(mirror.state) if isinstance(mirror.state, dict) else {}
    patch: list[dict[str, Any]] = []

    # Phase 1: pending changes (init prologue + event-driven entries).
    for change in pending_changes:
        _apply_pending_change(change, state, patch)

    # Phase 2: script ops. Track auto-emitted CloseCallbackOps.
    close_callback_slots: list[int] = []
    for raw_op in runner_queued_ops:
        _apply_script_op(
            raw_op,
            state=state,
            patch=patch,
            slot_allocations=slot_allocations,
            entity_short_ids=entity_short_ids,
            close_callback_slots=close_callback_slots,
            allow_reserved_slot_keys=allow_reserved_slot_keys,
        )

    # Tracker diff against the running post-state.
    post_data_by_key = state.get("data_by_key", {})
    post_callback_notifications = state.get("callback_notifications", {})
    desired_entities, desired_services = desired_tracker_set(
        post_data_by_key, post_callback_notifications
    )
    track_ops = _build_tracker_diff(
        current_entities=mirror.ws_tracked_entities,
        current_services=mirror.ws_tracked_services,
        desired_entities=desired_entities,
        desired_services=desired_services,
    )
    slot_ops = _build_slot_ops(
        runner_queued_ops=runner_queued_ops,
        slot_allocations=slot_allocations,
        entity_short_ids=entity_short_ids,
    )
    close_ops: list[MutationOp] = [CloseCallbackOp(slot=n) for n in close_callback_slots]
    if not patch and not track_ops and not slot_ops and not close_ops:
        return None
    # Build the substituted form of runner_queued_ops for chunk-E's
    # log writer. Each op's args / kwargs / payload has placeholders
    # rewritten to wire-final shapes. ``_build_slot_ops`` does the
    # same substitution internally for invoke ops; we redo it here
    # because it doesn't return the per-op dicts (only the
    # MutationOp instances).
    substituted_ops = [
        substitute_op_for_log(op, entity_short_ids, slot_allocations) for op in runner_queued_ops
    ]
    request = MutateSessionRequest(
        request_id=request_id,
        subscription_id=subscription_id,
        ack_seq=ack_seq,
        update_id=update_id,
        patch=patch,
        ops=[*track_ops, *slot_ops, *close_ops],
    )
    return BuildResult(
        request=request,
        post_state=state,
        desired_entities=desired_entities,
        desired_services=desired_services,
        new_slot_counter=new_slot_counter,
        substituted_runner_queued_ops=substituted_ops,
        slot_allocations=slot_allocations,
    )


def substitute_op_for_log(
    op: dict[str, Any],
    entity_short_ids: dict[str, str],
    slot_allocations: dict[tuple[str, str], int],
) -> dict[str, Any]:
    """Return a copy of ``op`` with ``args`` / ``kwargs`` / ``payload``
    fields walked through :func:`substitute_placeholders`.

    Kinds without payload-bearing fields pass through unchanged.

    Public so the connection's "no-op script" path can pre-substitute
    placeholders for log-intent pushes when ``build_mutate_request``
    returned ``None`` (e.g. a script that only called
    ``present_to_user`` — MCP-side only, no wire mutation, but the
    op still belongs on the chain).
    """
    kind = op.get("kind")
    if kind == KIND_SCHEDULE_INVOKE:
        return {
            **op,
            "args": [
                substitute_placeholders(a, entity_short_ids, slot_allocations) for a in op["args"]
            ],
            "kwargs": {
                k: substitute_placeholders(v, entity_short_ids, slot_allocations)
                for k, v in op["kwargs"].items()
            },
        }
    if kind == KIND_SCHEDULE_CALLBACK_INVOKE:
        return {
            **op,
            # The target rides the same placeholder grammar as args:
            # ``["callback_handle", k]`` → ``["callback", N]``.
            "callback": substitute_placeholders(op["callback"], entity_short_ids, slot_allocations),
            "args": [
                substitute_placeholders(a, entity_short_ids, slot_allocations) for a in op["args"]
            ],
            "kwargs": {
                k: substitute_placeholders(v, entity_short_ids, slot_allocations)
                for k, v in op["kwargs"].items()
            },
        }
    if kind == KIND_PRESENT_TO_USER:
        return {
            **op,
            "payload": substitute_placeholders(op["payload"], entity_short_ids, slot_allocations),
        }
    return op


# --- Allocate workstation slots in script order ---------------------


def _allocate_slots(
    runner_queued_ops: list[dict[str, Any]],
    slot_counter: int,
) -> dict[tuple[str, str], int]:
    """Walk ``runner_queued_ops`` in order; for each
    ``schedule_invoke`` / ``schedule_callback_invoke`` /
    ``create_callback`` (all slot-claiming), claim
    ``slot_counter + i`` (1-based).
    Returns ``(kind, slot_key) → slot_n``.

    Both kinds share the same monotonic slot space — the
    workstation enforces that slot numbers in one mutation are
    strictly increasing across all slot-claiming ops, so we
    allocate in script order and emit the wire ops list in
    matching order downstream (see :func:`_build_slot_ops`).
    """
    out: dict[tuple[str, str], int] = {}
    i = 0
    for raw_op in runner_queued_ops:
        kind = raw_op.get("kind")
        if kind in (KIND_SCHEDULE_INVOKE, KIND_SCHEDULE_CALLBACK_INVOKE, KIND_CREATE_CALLBACK):
            i += 1
            out[(kind, raw_op["slot_key"])] = slot_counter + i
    return out


# --- Patch synthesis: pending changes -------------------------------


def _apply_pending_change(
    change: PendingChange,
    state: dict[str, Any],
    patch: list[dict[str, Any]],
) -> None:
    """Translate one :class:`PendingChange` into JSON Patch ops,
    mutating ``state`` in place to keep it in sync with the
    accumulated patch."""
    if change.op == "initialize":
        # Always creates both top-level objects the MCP writes
        # to. By construction this entry is only enqueued when
        # the workstation's state lacks them
        # (``workstation_state_needs_init``), so the ``add`` is
        # always correct.
        for key in ("data_by_key", "callback_notifications"):
            if key not in state:
                patch.append({"op": "add", "path": f"/{key}", "value": {}})
                state[key] = {}
        return
    if change.op == "set":
        _emit_set(state, patch, change.path, change.value)
        return
    if change.op == "remove":
        _emit_remove(state, patch, change.path)
        return
    raise ValueError(f"unknown PendingChange op: {change.op!r}")


def _emit_set(
    state: dict[str, Any],
    patch: list[dict[str, Any]],
    path: tuple[str, ...],
    value: Any,
) -> None:
    """Emit JSON Patch ops to set ``state[path] = value``.

    Walks ``path`` segments; for each intermediate container
    that doesn't yet exist in ``state``, emits
    ``add /<prefix> {}`` first. Then emits ``add`` or ``replace``
    for the leaf based on whether it currently exists.
    """
    # Walk intermediate containers.
    cur: dict[str, Any] = state
    for i in range(len(path) - 1):
        seg = path[i]
        if seg not in cur:
            container_path = "/" + "/".join(_escape_json_pointer(s) for s in path[: i + 1])
            patch.append({"op": "add", "path": container_path, "value": {}})
            cur[seg] = {}
        cur = cur[seg]
    # Leaf.
    leaf = path[-1]
    leaf_path = "/" + "/".join(_escape_json_pointer(s) for s in path)
    op = "replace" if leaf in cur else "add"
    patch.append({"op": op, "path": leaf_path, "value": value})
    cur[leaf] = value


def _emit_remove(
    state: dict[str, Any],
    patch: list[dict[str, Any]],
    path: tuple[str, ...],
) -> None:
    """Emit ``remove /<path>`` and update ``state``. Raises
    :class:`ForgetMissingKey` if the leaf isn't present."""
    cur: dict[str, Any] = state
    for seg in path[:-1]:
        if seg not in cur:
            raise ForgetMissingKey(":".join(path))
        cur = cur[seg]
    leaf = path[-1]
    if leaf not in cur:
        raise ForgetMissingKey(":".join(path))
    leaf_path = "/" + "/".join(_escape_json_pointer(s) for s in path)
    patch.append({"op": "remove", "path": leaf_path})
    del cur[leaf]


def _escape_json_pointer(s: str) -> str:
    """RFC-6901 token escape: ``~`` → ``~0``, ``/`` → ``~1``."""
    return s.replace("~", "~0").replace("/", "~1")


# --- Patch synthesis: script ops ------------------------------------


_KINDS_WRITING_DATA_BY_KEY = frozenset(
    {
        KIND_EXPORT_ENTITY,
        KIND_EXPORT_SERVICE,
        KIND_SCHEDULE_INVOKE,
        KIND_SCHEDULE_CALLBACK_INVOKE,
        KIND_CREATE_CALLBACK,
    }
)


def _apply_script_op(
    raw_op: dict[str, Any],
    *,
    state: dict[str, Any],
    patch: list[dict[str, Any]],
    slot_allocations: dict[tuple[str, str], int],
    entity_short_ids: dict[str, str],
    close_callback_slots: list[int],
    allow_reserved_slot_keys: bool = False,
) -> None:
    """Translate one queued sandbox op into JSON Patch ops +
    auto-emitted ``CloseCallbackOp`` slots, mutating ``state``
    in place.

    Raises :class:`SlotKeyAlreadyExists` if a write-to-``data_by_key``
    op (export_entity / export_service / schedule_invoke /
    create_callback) targets a slot_key that's already populated in
    the running state. The sandbox's ``used_slot_keys`` set blocks
    in-script reuse; this check covers the cross-script case where
    a slot's prior value (from a committed earlier script or
    workstation-side foreign mutation) is still around. The agent
    must ``forget(slot_key)`` in a prior script before reusing.
    """
    kind = raw_op.get("kind")
    if kind == KIND_PRESENT_TO_USER:
        # MCP-side only — no JSON Patch, no workstation op. The log
        # writer picks it up from ``runner_queued_ops`` and builds
        # a ``PresentToUserOpRecord`` on the
        # ``ScriptExecutionLogEvent``.
        return
    if kind == KIND_FORGET_CALLBACK_NOTIFICATION:
        slot_key: str = raw_op["slot_key"]
        if not allow_reserved_slot_keys and slot_key in RESERVED_SLOT_KEYS:
            assert isinstance(kind, str)
            raise SlotKeyReserved(slot_key, kind)
        notif_idx_str = str(raw_op["notif_index"])
        _emit_remove(state, patch, ("callback_notifications", slot_key, notif_idx_str))
        return
    slot_key = raw_op["slot_key"]
    if not allow_reserved_slot_keys and slot_key in RESERVED_SLOT_KEYS:
        assert isinstance(kind, str)
        raise SlotKeyReserved(slot_key, kind)
    if kind in _KINDS_WRITING_DATA_BY_KEY:
        existing = state.get("data_by_key", {}).get(slot_key)
        if existing is not None:
            assert isinstance(kind, str)
            raise SlotKeyAlreadyExists(slot_key, kind, existing)
    if kind == KIND_EXPORT_ENTITY:
        value: Any = ["exported_entity", entity_short_ids[slot_handle(slot_key)]]
        _emit_set(state, patch, ("data_by_key", slot_key), value)
    elif kind == KIND_EXPORT_SERVICE:
        _emit_set(
            state,
            patch,
            ("data_by_key", slot_key),
            ["exported_service", raw_op["short_id"]],
        )
    elif kind == KIND_FORGET:
        # forget on a callback_open slot also closes the callback.
        running_value = state.get("data_by_key", {}).get(slot_key)
        if (
            isinstance(running_value, list)
            and len(running_value) == 2
            and running_value[0] == CALLBACK_OPEN_TAG
            and isinstance(running_value[1], int)
        ):
            close_callback_slots.append(running_value[1])
        _emit_remove(state, patch, ("data_by_key", slot_key))
    elif kind in (KIND_SCHEDULE_INVOKE, KIND_SCHEDULE_CALLBACK_INVOKE):
        assert isinstance(kind, str)
        slot_n = slot_allocations[(kind, slot_key)]
        _emit_set(state, patch, ("data_by_key", slot_key), ["invoke_pending", slot_n])
    elif kind == KIND_CREATE_CALLBACK:
        slot_n = slot_allocations[(KIND_CREATE_CALLBACK, slot_key)]
        _emit_set(state, patch, ("data_by_key", slot_key), [CALLBACK_OPEN_TAG, slot_n])
    else:
        raise ValueError(f"unknown queued-op kind: {kind!r}")


# --- Workstation op list (invokes / callback creates) ---------------


def _build_slot_ops(
    *,
    runner_queued_ops: list[dict[str, Any]],
    slot_allocations: dict[tuple[str, str], int],
    entity_short_ids: dict[str, str],
) -> list[MutationOp]:
    """Emit slot-claiming wire ops (``InvokeOp`` /
    ``CallbackInvokeOp`` / ``CreateCallbackOp``) in the runner's
    script order.

    Order matters: the workstation requires strict-monotonic
    slot numbers across all slot-claiming ops in one mutation,
    and we allocated in script order via :func:`_allocate_slots`.

    For each ``InvokeOp``, substitutes ``["entity_handle",
    <handle>]`` and ``["callback_handle", <slot_key>]``
    placeholders in args with the wire-final ``["entity",
    <short_id>]`` / ``["callback", <slot_n>]`` forms.
    """
    ops: list[MutationOp] = []
    for raw_op in runner_queued_ops:
        kind = raw_op.get("kind")
        if kind == KIND_SCHEDULE_INVOKE:
            slot_key = raw_op["slot_key"]
            slot_n = slot_allocations[(KIND_SCHEDULE_INVOKE, slot_key)]
            args = [
                substitute_placeholders(a, entity_short_ids, slot_allocations)
                for a in raw_op["args"]
            ]
            kwargs = {
                k: substitute_placeholders(v, entity_short_ids, slot_allocations)
                for k, v in raw_op["kwargs"].items()
            }
            ops.append(
                InvokeOp(
                    slot=slot_n,
                    service_short_id=raw_op["service_short_id"],
                    method_name=raw_op["method_name"],
                    args=args,
                    kwargs=kwargs,
                )
            )
        elif kind == KIND_SCHEDULE_CALLBACK_INVOKE:
            slot_key = raw_op["slot_key"]
            slot_n = slot_allocations[(KIND_SCHEDULE_CALLBACK_INVOKE, slot_key)]
            # Resolve the target through the same placeholder
            # substitution the args use: ``["callback_handle", k]``
            # (same-script create) becomes ``["callback", N]``; a
            # cross-script ``["callback", N]`` passes through.
            target = substitute_placeholders(raw_op["callback"], entity_short_ids, slot_allocations)
            if not (
                isinstance(target, list)
                and len(target) == 2
                and target[0] == "callback"
                and isinstance(target[1], int)
            ):
                raise ValueError(
                    f"schedule_callback_invoke target did not resolve to a "
                    f"callback slot: {target!r}"
                )
            args = [
                substitute_placeholders(a, entity_short_ids, slot_allocations)
                for a in raw_op["args"]
            ]
            kwargs = {
                k: substitute_placeholders(v, entity_short_ids, slot_allocations)
                for k, v in raw_op["kwargs"].items()
            }
            ops.append(
                CallbackInvokeOp(
                    slot=slot_n,
                    callback_slot=target[1],
                    method_name=raw_op["method_name"],
                    args=args,
                    kwargs=kwargs,
                )
            )
        elif kind == KIND_CREATE_CALLBACK:
            slot_key = raw_op["slot_key"]
            slot_n = slot_allocations[(KIND_CREATE_CALLBACK, slot_key)]
            ops.append(CreateCallbackOp(slot=slot_n))
    return ops


def substitute_placeholders(
    value: Any,
    entity_short_ids: dict[str, str],
    slot_allocations: dict[tuple[str, str], int],
) -> Any:
    """Walk a wire-escape-encoded value, rewriting:

    - ``["entity_handle", <handle>]`` → ``["entity",
      <real_workstation_short_id>]``. The sandbox emits the
      placeholder tag so the workstation short_id lookup happens
      host-side after ``expose_entity`` over /rpc.
    - ``["callback_handle", <slot_key>]`` (stage 6) →
      ``["callback", <slot_n>]``. The sandbox emits this
      placeholder when a same-script-created callback is passed
      into a ``schedule_invoke`` arg; the host substitutes the
      slot number it just allocated for the matching
      ``CreateCallbackOp``.

    Walks the wire escape grammar (matches the codec in
    ``sandbox_bridge.builtins``):

    - 2-element list with a string tag: a tagged form. We rewrite
      ``entity_handle`` / ``callback_handle``; all other
      recognized tags (``entity`` / ``service`` / ``bytes`` /
      ``callback``) pass through. An unknown tag is forward-compat:
      pass through too. The host doesn't validate against the
      workstation's tag vocabulary here — that's the workstation's
      job at op-validation time.
    - 1-element list whose element is itself a list: a
      literal-list-escape (``[[…]]``). Recurse into the inner
      items, re-wrap.
    - dict: walk values; keys are strings by contract.
    - scalar (``str`` / ``int`` / ``float`` / ``bool`` / ``None``):
      pass through.

    A list of any other shape (e.g. bare ``[a, b, c]`` or empty
    ``[]``) is a malformed wire payload — error rather than
    treat as a literal list, because letting it through would
    silently hide a sandbox bug.
    """
    if isinstance(value, list):
        if len(value) == 2 and isinstance(value[0], str):
            tag = value[0]
            if tag == "entity_handle":
                handle = value[1]
                if not isinstance(handle, str):
                    raise ValueError(f"entity_handle payload must be a string, got {handle!r}")
                return ["entity", entity_short_ids[handle]]
            if tag == "callback_handle":
                slot_key = value[1]
                if not isinstance(slot_key, str):
                    raise ValueError(f"callback_handle payload must be a string, got {slot_key!r}")
                return ["callback", slot_allocations[(KIND_CREATE_CALLBACK, slot_key)]]
            return value
        if len(value) == 1 and isinstance(value[0], list):
            inner = value[0]
            return [[substitute_placeholders(v, entity_short_ids, slot_allocations) for v in inner]]
        raise ValueError(
            f"malformed wire-escape list: {value!r} (must be a tagged "
            "2-element form or a 1-element literal-list-escape)"
        )
    if isinstance(value, dict):
        return {
            k: substitute_placeholders(v, entity_short_ids, slot_allocations)
            for k, v in value.items()
        }
    return value


def _build_tracker_diff(
    *,
    current_entities: set[str],
    current_services: set[str],
    desired_entities: set[str],
    desired_services: set[str],
) -> list[MutationOp]:
    """Emit ``track_*`` / ``untrack_*`` ops to converge the
    workstation's pin set to ``desired_*``."""
    ops: list[MutationOp] = []
    # Untrack first, then track — the workstation validates the dry
    # run, so order doesn't strictly matter, but doing untracks
    # first is closer to a human-readable mental model.
    for short_id in sorted(current_entities - desired_entities):
        ops.append(UntrackEntityOp(short_id=short_id))
    for short_id in sorted(current_services - desired_services):
        ops.append(UntrackServiceOp(short_id=short_id))
    for short_id in sorted(desired_entities - current_entities):
        ops.append(TrackEntityOp(short_id=short_id))
    for short_id in sorted(desired_services - current_services):
        ops.append(TrackServiceOp(short_id=short_id))
    return ops
