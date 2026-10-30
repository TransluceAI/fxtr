"""Unit tests for :mod:`fxtr_workstation_mcp.agent_log_types`.

Round-trip tests for each entity type defined in the agent event-log
module — placeholders, the preamble, per-op records, and the chain
spine. No workstation server, no MCP machinery — just mashumaro
storage / load against an isolated :class:`FxtrScope`.

Verifies:

- Each :class:`DataclassEntity` round-trips through ``store()`` /
  ``EntityRef.load()`` and reconstructs all field values.
- :class:`ScriptOpRecord` (a :class:`DataclassFxtrSubpart`) round-
  trips via ``to_dict()`` / ``from_dict()`` with each ``kind``.
- The discriminator-on-``$fxtr.type`` mashumaro mechanism dispatches
  to the right subclass when loading an
  :class:`EntityRef[WsAgentEvent]` union.
- Linked-list ``WsAgentLogEventChain.prev`` walks correctly (``None``
  at head, concrete refs for older entries).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from frozendict import frozendict
from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data.entity_core import FxtrID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import FxtrScope

pytestmark = pytest.mark.anyio


from fxtr_workstation_mcp.agent_log_types import (
    OP_KIND_CREATE_CALLBACK,
    OP_KIND_EXPORT_ENTITY,
    OP_KIND_EXPORT_SERVICE,
    OP_KIND_FORGET,
    OP_KIND_FORGET_CALLBACK_NOTIFICATION,
    OP_KIND_PRESENT_TO_USER,
    OP_KIND_SCHEDULE_INVOKE,
    CallbackNotificationLogEvent,
    InvokeErrorLogEvent,
    InvokeResultLogEvent,
    OwnedCallbackPlaceholder,
    ScriptExecutionLogEvent,
    ScriptOpRecord,
    ScriptPreamble,
    WorkstationServicePlaceholder,
    WsAgentLogEventChain,
)


@pytest.fixture
async def scope() -> AsyncIterator[FxtrScope]:
    """Fresh isolated scope, active for the test body."""
    async with FxtrScope(name="agent_log_types_test") as s:
        with s.as_active_scope():
            yield s


# --- Placeholder + preamble round-trips -------------------------------


async def test_workstation_service_placeholder_round_trip(scope: FxtrScope) -> None:
    """Three required fields all round-trip."""
    # FxtrIDs in fields must be reachable in some scope. Use
    # builtin-tracked IDs for these test fixtures so the storage
    # pipeline accepts them.
    type_id = FxtrID.builtin("test_service_type")
    ctx_id = FxtrID.builtin("test_service_ctx")
    scope.track_builtin(type_id)
    scope.track_builtin(ctx_id)
    placeholder = WorkstationServicePlaceholder(
        short_id="s7",
        service_type_id=type_id,
        service_ctx=ctx_id,
    )
    ref = placeholder.store()
    loaded = await ref.load()
    assert loaded == placeholder


async def test_owned_callback_placeholder_round_trip(scope: FxtrScope) -> None:
    """slot_n + slot_key round-trip."""
    _ = scope
    placeholder = OwnedCallbackPlaceholder(slot_n=4, slot_key="workstation_inbox")
    ref = placeholder.store()
    loaded = await ref.load()
    assert loaded == placeholder


async def test_owned_callback_placeholder_unknown_slot_key(scope: FxtrScope) -> None:
    """The literal ``"<unknown>"`` sentinel string for closed-and-forgotten slots."""
    _ = scope
    placeholder = OwnedCallbackPlaceholder(slot_n=99, slot_key="<unknown>")
    ref = placeholder.store()
    loaded = await ref.load()
    assert loaded.slot_key == "<unknown>"


async def test_script_preamble_round_trip(scope: FxtrScope) -> None:
    """ScriptPreamble holds raw source text."""
    _ = scope
    preamble = ScriptPreamble(source="# preamble\nfrom typing import Any\n")
    ref = preamble.store()
    loaded = await ref.load()
    assert loaded.source == preamble.source


async def test_script_preamble_content_addressed(scope: FxtrScope) -> None:
    """Two identical preambles map to one stored FxtrID (the storage-
    reuse property the user asked for)."""
    _ = scope
    a = ScriptPreamble(source="# preamble\nimport os\n")
    b = ScriptPreamble(source="# preamble\nimport os\n")
    ref_a = a.store()
    ref_b = b.store()
    assert ref_a.entity_id == ref_b.entity_id


# --- ScriptOpRecord (DataclassFxtrSubpart) round-trips ----------------


def _mk_entity_id() -> FxtrID:
    """Synthetic FxtrID for ScriptOpRecord to_dict/from_dict tests.

    These tests don't pass through any scope-validity check (no
    ``.store()`` call), so an untracked id is fine. Tests that *do*
    call ``.store()`` use ``_mk_tracked_id`` instead.
    """
    return FxtrID.from_tag_and_payload("d", uuid.uuid4().bytes)


def _mk_tracked_id(scope: FxtrScope, name: str) -> FxtrID:
    """Allocate + track a builtin FxtrID. Used in scope tests where
    the storage pipeline validates id reachability."""
    fid = FxtrID.builtin(name)
    scope.track_builtin(fid)
    return fid


async def test_script_op_record_export_entity_round_trip() -> None:
    """Flat-shape dataclass round-trips via to_dict/from_dict."""
    entity_id = _mk_entity_id()
    record = ScriptOpRecord(
        kind=OP_KIND_EXPORT_ENTITY,
        slot_key="r",
        entity=EntityRef(entity_id=entity_id, target_type=UnknownEntity),
    )
    raw = record.to_dict()
    restored = ScriptOpRecord.from_dict(raw)
    assert restored.kind == record.kind
    assert restored.slot_key == record.slot_key
    assert restored.entity is not None
    assert restored.entity.entity_id == entity_id


async def test_script_op_record_schedule_invoke_round_trip() -> None:
    """schedule_invoke records carry args + kwargs as FrozenFxtrCbor."""
    args: tuple = (1, "hello", RawWeakFxtrID(inner_id=_mk_entity_id()))
    kwargs: frozendict = frozendict({"flag": True})
    record = ScriptOpRecord(
        kind=OP_KIND_SCHEDULE_INVOKE,
        slot_key="r",
        service_short_id="s5",
        method_name="echo",
        args=args,
        kwargs=kwargs,
    )
    raw = record.to_dict()
    restored = ScriptOpRecord.from_dict(raw)
    assert restored.kind == record.kind
    assert restored.slot_key == record.slot_key
    assert restored.service_short_id == record.service_short_id
    assert restored.method_name == record.method_name
    assert restored.args == record.args
    assert restored.kwargs == record.kwargs


async def test_script_op_record_forget_round_trip() -> None:
    """forget carries only slot_key."""
    record = ScriptOpRecord(kind=OP_KIND_FORGET, slot_key="r")
    raw = record.to_dict()
    restored = ScriptOpRecord.from_dict(raw)
    assert restored == record


async def test_script_op_record_create_callback_round_trip() -> None:
    """create_callback carries only slot_key."""
    record = ScriptOpRecord(kind=OP_KIND_CREATE_CALLBACK, slot_key="cb")
    raw = record.to_dict()
    restored = ScriptOpRecord.from_dict(raw)
    assert restored == record


async def test_script_op_record_forget_callback_notification_round_trip() -> None:
    """forget_callback_notification carries slot_key + notif_index."""
    record = ScriptOpRecord(
        kind=OP_KIND_FORGET_CALLBACK_NOTIFICATION,
        slot_key="cb",
        notif_index=42,
    )
    raw = record.to_dict()
    restored = ScriptOpRecord.from_dict(raw)
    assert restored == record


async def test_script_op_record_export_service_round_trip() -> None:
    """export_service points at a WorkstationServicePlaceholder ref."""
    entity_id = _mk_entity_id()
    record = ScriptOpRecord(
        kind=OP_KIND_EXPORT_SERVICE,
        slot_key="svc",
        service=EntityRef(entity_id=entity_id, target_type=WorkstationServicePlaceholder),
    )
    raw = record.to_dict()
    restored = ScriptOpRecord.from_dict(raw)
    assert restored.kind == record.kind
    assert restored.slot_key == record.slot_key
    assert restored.service is not None
    assert restored.service.entity_id == entity_id


async def test_script_op_record_present_to_user_round_trip() -> None:
    """present_to_user carries a FrozenFxtrCbor payload."""
    payload: frozendict = frozendict({"greeting": "hi", "count": 3})
    record = ScriptOpRecord(
        kind=OP_KIND_PRESENT_TO_USER,
        payload=payload,
    )
    raw = record.to_dict()
    restored = ScriptOpRecord.from_dict(raw)
    assert restored == record


# --- ScriptExecutionLogEvent round-trip ----------------------------------


async def test_script_execution_event_success_round_trip(scope: FxtrScope) -> None:
    """Success path: ops populated, error / rejected both None."""
    preamble = ScriptPreamble(source="# preamble\n")
    preamble_ref = preamble.store()
    entity_id = _mk_tracked_id(scope, "test_event_entity")
    ops = (
        ScriptOpRecord(
            kind=OP_KIND_EXPORT_ENTITY,
            slot_key="r",
            entity=EntityRef(entity_id=entity_id, target_type=UnknownEntity),
        ),
        ScriptOpRecord(kind=OP_KIND_FORGET, slot_key="x"),
    )
    event = ScriptExecutionLogEvent(
        script_src="forget('x')\n",
        preamble=preamble_ref,
        stdout="hello\n",
        stderr="",
        error=None,
        rejected=None,
        ops=ops,
        committed_seq=42,
    )
    ref = event.store()
    loaded = await ref.load()
    assert loaded.script_src == event.script_src
    assert loaded.stdout == event.stdout
    assert loaded.stderr == event.stderr
    assert loaded.error is None
    assert loaded.rejected is None
    assert loaded.committed_seq == 42
    assert len(loaded.ops) == 2
    assert loaded.ops[0].kind == OP_KIND_EXPORT_ENTITY
    assert loaded.ops[1].kind == OP_KIND_FORGET
    loaded_preamble = await loaded.preamble.load()
    assert loaded_preamble.source == preamble.source


async def test_script_execution_event_error_path(scope: FxtrScope) -> None:
    """Script-raised path: error set, ops empty."""
    _ = scope
    preamble = ScriptPreamble(source="")
    event = ScriptExecutionLogEvent(
        script_src="raise ValueError('boom')\n",
        preamble=preamble.store(),
        stdout="",
        stderr="Traceback...\nValueError: boom\n",
        error="ValueError: boom",
        rejected=None,
        ops=(),
        committed_seq=None,
    )
    ref = event.store()
    loaded = await ref.load()
    assert loaded.error == "ValueError: boom"
    assert loaded.ops == ()
    assert loaded.committed_seq is None


async def test_script_execution_event_rejected_path(scope: FxtrScope) -> None:
    """Workstation-rejected path: rejected set, ops empty."""
    _ = scope
    preamble = ScriptPreamble(source="")
    event = ScriptExecutionLogEvent(
        script_src="export_entity('r', some_id)\n",
        preamble=preamble.store(),
        stdout="",
        stderr="",
        error=None,
        rejected="conflict: slot 'r' already populated",
        ops=(),
        committed_seq=None,
    )
    ref = event.store()
    loaded = await ref.load()
    assert loaded.rejected == "conflict: slot 'r' already populated"
    assert loaded.error is None


# --- Invoke / callback event round-trips ------------------------------


async def test_invoke_result_event_round_trip(scope: FxtrScope) -> None:
    """InvokeResultLogEvent: slot_key + slot_n + seq + payload."""
    _ = scope
    payload: frozendict = frozendict({"answer": 42})
    event = InvokeResultLogEvent(slot_key="r", slot_n=1, seq=5, payload=payload)
    ref = event.store()
    loaded = await ref.load()
    assert loaded == event


async def test_invoke_error_event_round_trip(scope: FxtrScope) -> None:
    """InvokeErrorLogEvent: slot_key + slot_n + seq + message."""
    _ = scope
    event = InvokeErrorLogEvent(slot_key="r", slot_n=2, seq=6, message="boom")
    ref = event.store()
    loaded = await ref.load()
    assert loaded == event


async def test_callback_notification_event_round_trip(scope: FxtrScope) -> None:
    """CallbackNotificationLogEvent for an inbox-style payload."""
    _ = scope
    payload: frozendict = frozendict({"msg": "hello from frontend"})
    event = CallbackNotificationLogEvent(
        slot_key="workstation_inbox", slot_n=3, seq=7, payload=payload
    )
    ref = event.store()
    loaded = await ref.load()
    assert loaded == event


# --- Chain spine round-trip + linked-list walk ------------------------


async def test_chain_head_with_none_prev(scope: FxtrScope) -> None:
    """First chain entry: prev=None."""
    _ = scope
    event = InvokeResultLogEvent(slot_key="r", slot_n=1, seq=1, payload=None)
    event_ref = event.store()
    chain = WsAgentLogEventChain(prev=None, event=event_ref, timestamp=1700000000.0)
    chain_ref = chain.store()
    loaded = await chain_ref.load()
    assert loaded.prev is None
    assert loaded.timestamp == 1700000000.0
    loaded_event = await loaded.event.load()
    assert isinstance(loaded_event, InvokeResultLogEvent)
    assert loaded_event.slot_key == "r"


async def test_chain_linked_list_walk(scope: FxtrScope) -> None:
    """Three nodes in a chain; walk prev back to the head and verify
    each event's identity along the way."""
    _ = scope
    event1 = InvokeResultLogEvent(slot_key="a", slot_n=1, seq=1, payload=None)
    event2 = InvokeResultLogEvent(slot_key="b", slot_n=2, seq=2, payload=None)
    event3 = InvokeErrorLogEvent(slot_key="c", slot_n=3, seq=3, message="boom")

    head1 = WsAgentLogEventChain(prev=None, event=event1.store(), timestamp=1.0)
    head1_ref = head1.store()
    head2 = WsAgentLogEventChain(prev=head1_ref, event=event2.store(), timestamp=2.0)
    head2_ref = head2.store()
    head3 = WsAgentLogEventChain(prev=head2_ref, event=event3.store(), timestamp=3.0)
    head3_ref = head3.store()

    # Walk backwards from the latest head.
    cursor: EntityRef[WsAgentLogEventChain] | None = head3_ref
    seen: list[tuple[float, str]] = []
    while cursor is not None:
        loaded = await cursor.load()
        ev = await loaded.event.load()
        # Mashumaro's $fxtr.type discriminator dispatches to the
        # right subclass — we tolerate either invoke shape and
        # extract a common identifier.
        slot_key = getattr(ev, "slot_key")
        seen.append((loaded.timestamp, slot_key))
        cursor = loaded.prev
    # The walk from head3 → head2 → head1 yields timestamps 3, 2, 1.
    assert [t for t, _ in seen] == [3.0, 2.0, 1.0]
    assert [k for _, k in seen] == ["c", "b", "a"]


async def test_chain_event_discriminates_by_fxtr_type(scope: FxtrScope) -> None:
    """The ``$fxtr.type`` discriminator routes each EntityRef[WsAgentEvent]
    to the right concrete subclass on load."""
    _ = scope
    a = InvokeResultLogEvent(slot_key="a", slot_n=1, seq=1, payload=None).store()
    b = InvokeErrorLogEvent(slot_key="b", slot_n=2, seq=2, message="m").store()
    c = CallbackNotificationLogEvent(
        slot_key="c", slot_n=3, seq=3, payload=frozendict({"x": 1})
    ).store()

    loaded_a = await a.load()
    loaded_b = await b.load()
    loaded_c = await c.load()
    assert type(loaded_a).__name__ == "InvokeResultLogEvent"
    assert type(loaded_b).__name__ == "InvokeErrorLogEvent"
    assert type(loaded_c).__name__ == "CallbackNotificationLogEvent"
