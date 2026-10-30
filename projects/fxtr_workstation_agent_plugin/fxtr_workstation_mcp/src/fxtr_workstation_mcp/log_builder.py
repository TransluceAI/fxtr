"""Build typed event-log entities from the wire-form inputs the
MCP's worker accumulates per script execution or inbound event.

Two public functions build leaf event entities (one
:class:`DataclassEntity` per event) and one builds + pins a new
chain head:

- :func:`build_script_execution_event` — packages a script's
  outcome (sources, std streams, error/rejected/committed status,
  list of recorded ops) into a :class:`ScriptExecutionLogEvent`. Walks
  each op's payload through :mod:`log_walker` so the stored event
  carries typed leaves.
- :func:`build_invoke_result_event` /
  :func:`build_invoke_error_event` /
  :func:`build_callback_notification_event` — package one inbound
  workstation event into the corresponding log entity. The walker
  runs on the payload (where applicable).
- :func:`extend_chain` — given the prior chain head + a freshly
  built event ref, mint a new :class:`WsAgentLogEventChain` head, pin
  it to the persistence cap (if configured) under the session_id
  key. Returns the new head.

Stored entities land in the *active scope* at call time. The caller
(:class:`AgentMcpRunningSessionImpl`) sets the active scope to its
impl scope before invoking. The log builder makes no assumptions
beyond that.

## Op record contract

The :func:`build_script_execution_event` function expects
``runner_queued_ops`` to be the **post-placeholder-substitution**
wire form — i.e. each op's args / kwargs / payload carry tagged
shapes like ``["entity", "s_e7"]`` / ``["callback", 3]``, not the
sandbox-bridge ``"entity_handle"`` / ``"callback_handle"``
placeholders. The mutation builder's
:func:`mutation_builder.substitute_placeholders` is responsible for
that step; callers run it before invoking the log builder.

``export_entity_short_ids`` is the parallel map (slot_key →
workstation entity short_id) for the export_entity ops — populated
by ``_apply_script_mutation`` after ``expose_entity`` over /rpc
returns each registered entity's short_id.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef
from fxtr_workstation.protocols import PersistenceProto, WorkstationServiceProto

from .agent_log_types import (
    OP_KIND_CREATE_CALLBACK,
    OP_KIND_SCHEDULE_CALLBACK_INVOKE,
    OP_KIND_EXPORT_ENTITY,
    OP_KIND_EXPORT_SERVICE,
    OP_KIND_FORGET,
    OP_KIND_FORGET_CALLBACK_NOTIFICATION,
    OP_KIND_PRESENT_TO_USER,
    OP_KIND_SCHEDULE_INVOKE,
    CallbackNotificationLogEvent,
    InvokeErrorLogEvent,
    InvokeResultLogEvent,
    OpKind,
    ScriptExecutionLogEvent,
    ScriptOpRecord,
    ScriptPreamble,
    WorkstationServicePlaceholder,
    WsAgentLogEvent,
    WsAgentLogEventChain,
)
from .log_walker import WalkerContext, walk_payload, walk_with_context

#: Callable type returning the slot_key (or ``None``) for an
#: owned-callback ``slot_n``. Typically
#: ``mirror.pending_slot_index.get``.
SlotKeyLookup = Callable[[int], str | None]


# --- Script-execution event ------------------------------------------


async def build_script_execution_event(
    *,
    scope: FxtrScope,
    script_src: str,
    preamble_src: str,
    stdout: str,
    stderr: str,
    error: str | None,
    rejected: str | None,
    committed_seq: int | None,
    runner_queued_ops: list[dict[str, Any]],
    export_entity_short_ids: dict[str, str],
    workstation_ref: ServiceRef[WorkstationServiceProto],
    slot_key_for_slot_n: SlotKeyLookup,
    prefilled_entity_short_ids: dict[str, FxtrID] | None = None,
) -> EntityRef[ScriptExecutionLogEvent]:
    """Build + store a :class:`ScriptExecutionLogEvent` into ``scope``.

    ``scope`` is the destination for the preamble, every walked
    placeholder (services / callbacks referenced in op payloads),
    and the resulting :class:`ScriptExecutionLogEvent` itself —
    explicit, per scope discipline (see
    ``v2/notes/scope_discipline.md``). ``workstation_ref`` is
    re-resolved by the walker into a transient child scope for
    each entity / service lookup; it only needs to be registered
    in ``scope`` (or one of its parents).

    The preamble is stored as a separate content-addressed
    :class:`ScriptPreamble` so identical preambles share storage.

    On either error path (``error is not None`` or
    ``rejected is not None``), ``ops`` is the empty tuple — the
    script didn't commit. Informative detail lives in
    ``stderr`` / ``error`` / ``rejected``.

    ``runner_queued_ops`` must already have placeholder substitution
    applied (``["entity_handle", h]`` → ``["entity", short_id]``,
    ``["callback_handle", k]`` → ``["callback", N]``); see the
    module docstring's "Op record contract" section.

    ``export_entity_short_ids`` maps a slot_key (from an
    ``export_entity`` op) to the workstation entity short_id assigned
    by ``expose_entity`` over /rpc.

    ``prefilled_entity_short_ids`` optionally pre-populates the
    walker's ``short_id → FxtrID`` cache. The caller resolves these
    while its per-script ``expose_entity`` registrations are alive
    (the workstation's ``track_from_scope`` then keeps the FxtrIDs
    reachable past the stack exit); the walker uses the cache as
    a short-circuit so it never tries to call
    ``workstation.get_entity(short_id)`` after the workstation has
    released the short_id. See
    :class:`_LogIntent_ScriptExecution.prefilled_entity_short_ids`
    for the bug this guards against.
    """
    preamble = ScriptPreamble(source=preamble_src)
    # Preamble is freshly constructed; store explicitly into ``scope``.
    preamble_ref = preamble.store(scope)

    if error is not None or rejected is not None:
        ops: tuple[ScriptOpRecord, ...] = ()
    else:
        ops = await _build_op_records(
            scope=scope,
            runner_queued_ops=runner_queued_ops,
            export_entity_short_ids=export_entity_short_ids,
            workstation_ref=workstation_ref,
            slot_key_for_slot_n=slot_key_for_slot_n,
            prefilled_entity_short_ids=prefilled_entity_short_ids or {},
        )

    event = ScriptExecutionLogEvent(
        script_src=script_src,
        preamble=preamble_ref,
        stdout=stdout,
        stderr=stderr,
        error=error,
        rejected=rejected,
        ops=ops,
        committed_seq=committed_seq,
    )
    # The event's refs (``preamble_ref``, plus any ``EntityRef`` /
    # ``RawWeakFxtrID`` inside ``ops``' walker output) all live in
    # ``scope`` — preamble because we just stored it; walker output
    # because the walker stored its placeholders into ``scope`` and
    # resolved entity FxtrIDs there.
    return event.store(scope)


async def _build_op_records(
    *,
    scope: FxtrScope,
    runner_queued_ops: list[dict[str, Any]],
    export_entity_short_ids: dict[str, str],
    workstation_ref: ServiceRef[WorkstationServiceProto],
    slot_key_for_slot_n: SlotKeyLookup,
    prefilled_entity_short_ids: dict[str, FxtrID],
) -> tuple[ScriptOpRecord, ...]:
    """Walk each queued op into a :class:`ScriptOpRecord`. Reuses a
    single :class:`WalkerContext` across all ops so duplicate
    entity / service refs across ops dedupe through the walker's
    cache.

    The walker's ``_entity_cache`` is pre-populated from
    ``prefilled_entity_short_ids`` so any ``["entity", short_id]``
    leaf in the queued ops short-circuits to the pre-resolved
    FxtrID (no follow-up ``workstation.get_entity`` RPC needed).
    See :func:`build_script_execution_event`'s docstring for why.
    """
    ctx = WalkerContext(
        scope=scope,
        workstation_ref=workstation_ref,
        slot_key_for_slot_n=slot_key_for_slot_n,
    )
    if prefilled_entity_short_ids:
        ctx.prefill_entity_cache(prefilled_entity_short_ids)
    records: list[ScriptOpRecord] = []
    for raw in runner_queued_ops:
        records.append(
            await _build_one_op_record(
                raw,
                export_entity_short_ids=export_entity_short_ids,
                ctx=ctx,
            )
        )
    return tuple(records)


async def _build_one_op_record(
    raw: dict[str, Any],
    *,
    export_entity_short_ids: dict[str, str],
    ctx: WalkerContext,
) -> ScriptOpRecord:
    """Dispatch one queued op (asdict'd ``QueuedOp``) into a
    :class:`ScriptOpRecord`."""
    kind = raw.get("kind")
    if kind == OP_KIND_EXPORT_ENTITY:
        slot_key = raw["slot_key"]
        short_id = export_entity_short_ids[slot_key]
        fid = await ctx.resolve_entity_short_id(short_id)
        return ScriptOpRecord(
            kind=OP_KIND_EXPORT_ENTITY,
            slot_key=slot_key,
            entity=EntityRef(entity_id=fid, target_type=UnknownEntity),
        )
    if kind == OP_KIND_EXPORT_SERVICE:
        slot_key = raw["slot_key"]
        short_id = raw["short_id"]
        placeholder_id = await ctx.stored_service_placeholder(short_id)
        return ScriptOpRecord(
            kind=OP_KIND_EXPORT_SERVICE,
            slot_key=slot_key,
            service=EntityRef(entity_id=placeholder_id, target_type=WorkstationServicePlaceholder),
        )
    if kind == OP_KIND_FORGET:
        return ScriptOpRecord(kind=OP_KIND_FORGET, slot_key=raw["slot_key"])
    if kind == OP_KIND_CREATE_CALLBACK:
        return ScriptOpRecord(kind=OP_KIND_CREATE_CALLBACK, slot_key=raw["slot_key"])
    if kind == OP_KIND_FORGET_CALLBACK_NOTIFICATION:
        return ScriptOpRecord(
            kind=OP_KIND_FORGET_CALLBACK_NOTIFICATION,
            slot_key=raw["slot_key"],
            notif_index=raw["notif_index"],
        )
    if kind == OP_KIND_SCHEDULE_INVOKE:
        # schedule_invoke's args is a plain list (each element is a
        # wire-form value) and kwargs is a plain dict (string keys,
        # wire-form values). Walk each element + the mapping through
        # the shared walker context so caches span the whole script.
        args_tuple = tuple([await walk_with_context(a, ctx) for a in raw["args"]])
        kwargs_dict = await walk_with_context(raw["kwargs"], ctx)
        return ScriptOpRecord(
            kind=OP_KIND_SCHEDULE_INVOKE,
            slot_key=raw["slot_key"],
            service_short_id=raw["service_short_id"],
            method_name=raw["method_name"],
            args=args_tuple,
            kwargs=kwargs_dict,
        )
    if kind == OP_KIND_SCHEDULE_CALLBACK_INVOKE:
        # Same as schedule_invoke, except the target is an owned
        # callback: the mutation builder pre-substituted it to the
        # wire-final ``["callback", N]`` form, which the walker turns
        # into an ``EntityRef[OwnedCallbackPlaceholder]``.
        callback_walked = await walk_with_context(raw["callback"], ctx)
        args_tuple = tuple([await walk_with_context(a, ctx) for a in raw["args"]])
        kwargs_dict = await walk_with_context(raw["kwargs"], ctx)
        return ScriptOpRecord(
            kind=OP_KIND_SCHEDULE_CALLBACK_INVOKE,
            slot_key=raw["slot_key"],
            callback=callback_walked,
            method_name=raw["method_name"],
            args=args_tuple,
            kwargs=kwargs_dict,
        )
    if kind == OP_KIND_PRESENT_TO_USER:
        payload = await walk_with_context(raw["payload"], ctx)
        return ScriptOpRecord(
            kind=OP_KIND_PRESENT_TO_USER,
            payload=payload,
        )
    raise ValueError(f"log_builder: unknown queued op kind {kind!r}")


# --- Inbound-event entities ------------------------------------------


async def build_invoke_result_event(
    *,
    scope: FxtrScope,
    slot_key: str,
    slot_n: int,
    seq: int,
    payload: Any,
    workstation_ref: ServiceRef[WorkstationServiceProto],
    slot_key_for_slot_n: SlotKeyLookup,
) -> EntityRef[InvokeResultLogEvent]:
    """Walk a wire-form invoke-result payload + build + store the
    log entity into ``scope``.

    Same scope contract as :func:`build_script_execution_event`:
    walker stores placeholders into ``scope``, resolved entity
    FxtrIDs land in ``scope`` (via the workstation proxy's RPC
    return-value handling), and the event itself stores into
    ``scope``.
    """
    walked = await walk_payload(
        payload,
        scope=scope,
        workstation_ref=workstation_ref,
        slot_key_for_slot_n=slot_key_for_slot_n,
    )
    event = InvokeResultLogEvent(slot_key=slot_key, slot_n=slot_n, seq=seq, payload=walked)
    return event.store(scope)


def build_invoke_error_event(
    *,
    scope: FxtrScope,
    slot_key: str,
    slot_n: int,
    seq: int,
    message: str,
) -> EntityRef[InvokeErrorLogEvent]:
    """Build + store the log entity for an
    :class:`InvokeErrorLogEvent` into ``scope``. Sync — error events
    carry only a string message, no payload to walk."""
    event = InvokeErrorLogEvent(slot_key=slot_key, slot_n=slot_n, seq=seq, message=message)
    return event.store(scope)


async def build_callback_notification_event(
    *,
    scope: FxtrScope,
    slot_key: str,
    slot_n: int,
    seq: int,
    payload: Any,
    workstation_ref: ServiceRef[WorkstationServiceProto],
    slot_key_for_slot_n: SlotKeyLookup,
) -> EntityRef[CallbackNotificationLogEvent]:
    """Walk a wire-form callback payload + build + store the log
    entity into ``scope``.

    Same scope contract as :func:`build_invoke_result_event`. User
    messages (``AgentMcpRunningSessionProto.send``) flow through
    the reserved ``workstation_inbox`` callback, so they land here
    with ``slot_key="workstation_inbox"``.
    """
    walked = await walk_payload(
        payload,
        scope=scope,
        workstation_ref=workstation_ref,
        slot_key_for_slot_n=slot_key_for_slot_n,
    )
    event = CallbackNotificationLogEvent(slot_key=slot_key, slot_n=slot_n, seq=seq, payload=walked)
    return event.store(scope)


# --- Chain extension --------------------------------------------------


#: Hook the caller passes to :func:`extend_chain` so the impl is
#: notified of the new head. Sync (the impl's ``update_head``
#: itself doesn't await — it writes to watcher slots and returns).
UpdateHeadHook = Callable[[EntityRef[WsAgentLogEventChain]], None]


async def extend_chain(
    *,
    scope: FxtrScope,
    prev_head: EntityRef[WsAgentLogEventChain] | None,
    event_ref: EntityRef[WsAgentLogEvent],
    persistence: PersistenceProto | None,
    session_id: str,
    update_head: UpdateHeadHook,
    timestamp: float | None = None,
) -> EntityRef[WsAgentLogEventChain]:
    """Mint a new :class:`WsAgentLogEventChain` head into ``scope``
    + pin it.

    ``scope`` is the destination for the new chain head.
    ``prev_head`` and ``event_ref`` must be loadable from ``scope`` —
    typically they were produced by earlier ``build_*`` calls or
    earlier ``extend_chain`` invocations against the same scope.

    Steps:

    1. Construct + store a new :class:`WsAgentLogEventChain`
       referencing ``prev_head`` and ``event_ref``. ``timestamp``
       defaults to ``time.time()`` for human-readable rendering.
    2. If ``persistence`` is non-None: ``pin(session_id,
       new_head_id, overwrite=True)``. Pin failures propagate to
       the caller — by contract, a pin failure pauses the
       supervisor (for an event job) or surfaces as a tool error
       (for a script job).
    3. Call ``update_head(new_head_ref)`` so watchers are notified
       (synchronous — the impl's ``update_head`` just writes to
       watcher slots).

    Returns the new chain head as an :class:`EntityRef`.
    """
    if timestamp is None:
        timestamp = time.time()
    chain = WsAgentLogEventChain(prev=prev_head, event=event_ref, timestamp=timestamp)
    # Caller-supplied invariant: ``prev_head`` and ``event_ref`` are
    # loadable in ``scope``. So storing the new head into ``scope``
    # is well-defined.
    new_head = chain.store(scope)
    if persistence is not None:
        await persistence.pin(session_id, new_head.entity_id, overwrite=True)
    update_head(new_head)
    return new_head


# Re-export the OpKind literal type for callers that want to spell
# the discriminator without importing agent_log_types directly.
__all__ = [
    "build_script_execution_event",
    "build_invoke_result_event",
    "build_invoke_error_event",
    "build_callback_notification_event",
    "extend_chain",
    "OpKind",
    "SlotKeyLookup",
    "UpdateHeadHook",
]
