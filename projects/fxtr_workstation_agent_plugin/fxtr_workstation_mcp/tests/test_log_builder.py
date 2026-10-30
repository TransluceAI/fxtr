"""Unit tests for :mod:`fxtr_workstation_mcp.log_builder`.

Builds typed event-log entities from synthetic wire-form inputs +
mock workstation proxy + mock persistence cap. Verifies:

- :func:`build_script_execution_event` for each kind of queued op
  (export_entity / export_service / forget / schedule_invoke /
  create_callback / forget_callback_notification / present_to_user)
  plus the success / script-raised / workstation-rejected paths.
- :func:`build_invoke_result_event` / :func:`build_invoke_error_event` /
  :func:`build_callback_notification_event`.
- :func:`extend_chain` with and without a persistence cap, verifying
  the head's ``prev`` chain + pin invocation.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from frozendict import frozendict
from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.data.entity_core import FxtrID, RawStrongFxtrID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef

from fxtr_workstation_mcp.agent_log_types import (
    OP_KIND_CREATE_CALLBACK,
    OP_KIND_EXPORT_ENTITY,
    OP_KIND_EXPORT_SERVICE,
    OP_KIND_FORGET,
    OP_KIND_FORGET_CALLBACK_NOTIFICATION,
    OP_KIND_PRESENT_TO_USER,
    OP_KIND_SCHEDULE_INVOKE,
    InvokeErrorLogEvent,
    InvokeResultLogEvent,
    OwnedCallbackPlaceholder,
    ScriptExecutionLogEvent,
    WorkstationServicePlaceholder,
    WsAgentLogEventChain,
)
from fxtr_workstation_mcp.log_builder import (
    build_callback_notification_event,
    build_invoke_error_event,
    build_invoke_result_event,
    build_script_execution_event,
    extend_chain,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
async def scope() -> AsyncIterator[FxtrScope]:
    """Active scope for the test body."""
    async with FxtrScope(name="log_builder_test") as s:
        with s.as_active_scope():
            yield s


# --- Mocks ------------------------------------------------------------


class _MockProxy:
    def __init__(self, type_id: FxtrID, ctx_id: FxtrID) -> None:
        self._type_id = type_id
        self._ctx_id = ctx_id

    async def service_type_id(self) -> FxtrID:
        return self._type_id

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        if name == "$fxtr.ctx":
            return self._fxtr_ctx
        raise AttributeError(name)

    async def _fxtr_ctx(self) -> FxtrID:
        return self._ctx_id


class _MockServiceRef:
    def __init__(self, proxy: _MockProxy) -> None:
        self._proxy = proxy

    def replace_service_type(self, _type: Any) -> "_MockServiceRef":
        return self

    async def resolve(self, scope: FxtrScope | None = None) -> _MockProxy:  # noqa: ARG002
        return self._proxy


class _MockWorkstation:
    def __init__(self) -> None:
        self._entities: dict[str, FxtrID] = {}
        self._services: dict[str, tuple[FxtrID, FxtrID]] = {}

    def register_entity(self, short_id: str, fid: FxtrID) -> None:
        self._entities[short_id] = fid

    def register_service(self, short_id: str, type_id: FxtrID, ctx_id: FxtrID) -> None:
        self._services[short_id] = (type_id, ctx_id)

    async def get_entity(self, short_id: str) -> FxtrID:
        return self._entities[short_id]

    async def get_service(self, short_id: str) -> ServiceRef[Any]:
        type_id, ctx_id = self._services[short_id]
        return _MockServiceRef(_MockProxy(type_id, ctx_id))  # type: ignore[return-value]


class _MockWorkstationRef:
    """Duck-typed stand-in for
    ``ServiceRef[WorkstationServiceProto]`` that the log builder's
    walker resolves into a per-lookup temp scope. Returns the same
    :class:`_MockWorkstation` proxy on every resolve; see
    ``test_log_walker.py``'s comment for why this bypass is OK in
    the wire-form-parsing unit tests."""

    def __init__(self, proxy: _MockWorkstation) -> None:
        self._proxy = proxy

    async def resolve(self, scope: FxtrScope) -> _MockWorkstation:  # noqa: ARG002
        return self._proxy


class _MockPersistence:
    """Records :func:`pin` invocations so tests can assert them."""

    def __init__(self) -> None:
        self.pins: list[tuple[str, FxtrID, bool]] = []

    async def pin(self, key: str, entity: FxtrID, *, overwrite: bool = False) -> None:
        self.pins.append((key, entity, overwrite))


def _no_slot_keys(slot_n: int) -> str | None:
    _ = slot_n
    return None


def _alloc_id(scope: FxtrScope, name: str) -> FxtrID:
    fid = FxtrID.builtin(f"test_log_builder_{name}_{uuid.uuid4().hex}")
    scope.track_builtin(fid)
    return fid


# --- build_script_execution_event: success path ----------------------


async def test_script_execution_success_with_diverse_ops(scope: FxtrScope) -> None:
    """A script that did one op of every kind round-trips through the
    log builder with each op materializing as a typed :class:`ScriptOpRecord`."""
    ws = _MockWorkstation()
    # Pre-register the entities + services the walker will look up.
    exported_entity_fid = _alloc_id(scope, "exported_entity")
    arg_entity_fid = _alloc_id(scope, "arg_entity")
    svc_type = _alloc_id(scope, "svc_type")
    svc_ctx = _alloc_id(scope, "svc_ctx")
    exported_svc_type = _alloc_id(scope, "exp_svc_type")
    exported_svc_ctx = _alloc_id(scope, "exp_svc_ctx")
    ws.register_entity("ent_s1", exported_entity_fid)
    ws.register_entity("ent_s2", arg_entity_fid)
    ws.register_service("svc_s5", svc_type, svc_ctx)
    ws.register_service("svc_s9", exported_svc_type, exported_svc_ctx)

    runner_queued_ops: list[dict[str, Any]] = [
        {"kind": OP_KIND_EXPORT_ENTITY, "slot_key": "exp_e"},
        {"kind": OP_KIND_EXPORT_SERVICE, "slot_key": "exp_s", "short_id": "svc_s9"},
        {"kind": OP_KIND_FORGET, "slot_key": "old"},
        {
            "kind": OP_KIND_SCHEDULE_INVOKE,
            "slot_key": "r",
            "service_short_id": "svc_s5",
            "method_name": "echo",
            "args": [42, ["entity", "ent_s2"]],
            "kwargs": {"flag": True},
            "entity_arg_handles": [],
        },
        {"kind": OP_KIND_CREATE_CALLBACK, "slot_key": "cb"},
        {
            "kind": OP_KIND_FORGET_CALLBACK_NOTIFICATION,
            "slot_key": "cb",
            "notif_index": 7,
        },
        {
            "kind": OP_KIND_PRESENT_TO_USER,
            "payload": {"msg": "hello", "ent": ["entity", "ent_s2"]},
        },
    ]
    export_entity_short_ids = {"exp_e": "ent_s1"}

    ref = await build_script_execution_event(
        scope=scope,
        script_src="...",
        preamble_src="# preamble\n",
        stdout="hi\n",
        stderr="",
        error=None,
        rejected=None,
        committed_seq=42,
        runner_queued_ops=runner_queued_ops,
        export_entity_short_ids=export_entity_short_ids,
        workstation_ref=_MockWorkstationRef(ws),  # type: ignore[arg-type]
        slot_key_for_slot_n=_no_slot_keys,
    )

    loaded = await ref.load()
    assert isinstance(loaded, ScriptExecutionLogEvent)
    assert loaded.committed_seq == 42
    assert loaded.error is None
    assert loaded.rejected is None
    assert len(loaded.ops) == 7

    # Op 0: export_entity → has a populated `entity` ref pointing at
    # the workstation entity.
    op_ee = loaded.ops[0]
    assert op_ee.kind == OP_KIND_EXPORT_ENTITY
    assert op_ee.slot_key == "exp_e"
    assert op_ee.entity is not None
    assert op_ee.entity.entity_id == exported_entity_fid

    # Op 1: export_service → `service` ref pointing at a stored
    # WorkstationServicePlaceholder.
    op_es = loaded.ops[1]
    assert op_es.kind == OP_KIND_EXPORT_SERVICE
    assert op_es.slot_key == "exp_s"
    assert op_es.service is not None
    loaded_svc = await op_es.service.load()
    assert loaded_svc.short_id == "svc_s9"
    assert loaded_svc.service_type_id == exported_svc_type

    # Op 2: forget.
    assert loaded.ops[2].kind == OP_KIND_FORGET
    assert loaded.ops[2].slot_key == "old"

    # Op 3: schedule_invoke with mixed args.
    op_si = loaded.ops[3]
    assert op_si.kind == OP_KIND_SCHEDULE_INVOKE
    assert op_si.slot_key == "r"
    assert op_si.service_short_id == "svc_s5"
    assert op_si.method_name == "echo"
    assert op_si.args == (42, RawWeakFxtrID(inner_id=arg_entity_fid))
    assert op_si.kwargs == frozendict({"flag": True})

    # Op 4: create_callback.
    assert loaded.ops[4].kind == OP_KIND_CREATE_CALLBACK
    assert loaded.ops[4].slot_key == "cb"

    # Op 5: forget_callback_notification.
    op_fcn = loaded.ops[5]
    assert op_fcn.kind == OP_KIND_FORGET_CALLBACK_NOTIFICATION
    assert op_fcn.notif_index == 7

    # Op 6: present_to_user.
    op_ptu = loaded.ops[6]
    assert op_ptu.kind == OP_KIND_PRESENT_TO_USER
    assert isinstance(op_ptu.payload, frozendict)
    assert op_ptu.payload["msg"] == "hello"
    assert op_ptu.payload["ent"] == RawWeakFxtrID(inner_id=arg_entity_fid)


# --- build_script_execution_event: error / rejected paths ------------


async def test_script_execution_script_raised(scope: FxtrScope) -> None:
    """error set, rejected=None, ops=()."""
    ws = _MockWorkstation()
    ref = await build_script_execution_event(
        scope=scope,
        script_src="raise ValueError",
        preamble_src="",
        stdout="",
        stderr="Traceback...",
        error="ValueError: boom",
        rejected=None,
        committed_seq=None,
        # Even if queued_ops is non-empty, the builder skips them on
        # error since the mutation didn't commit.
        runner_queued_ops=[{"kind": OP_KIND_FORGET, "slot_key": "x"}],
        export_entity_short_ids={},
        workstation_ref=_MockWorkstationRef(ws),  # type: ignore[arg-type]
        slot_key_for_slot_n=_no_slot_keys,
    )
    loaded = await ref.load()
    assert loaded.error == "ValueError: boom"
    assert loaded.rejected is None
    assert loaded.committed_seq is None
    assert loaded.ops == ()


async def test_script_execution_workstation_rejected(scope: FxtrScope) -> None:
    """rejected set, error=None, ops=()."""
    ws = _MockWorkstation()
    ref = await build_script_execution_event(
        scope=scope,
        script_src="...",
        preamble_src="",
        stdout="",
        stderr="",
        error=None,
        rejected="conflict: slot 'r' already populated",
        committed_seq=None,
        runner_queued_ops=[],
        export_entity_short_ids={},
        workstation_ref=_MockWorkstationRef(ws),  # type: ignore[arg-type]
        slot_key_for_slot_n=_no_slot_keys,
    )
    loaded = await ref.load()
    assert loaded.rejected == "conflict: slot 'r' already populated"
    assert loaded.error is None
    assert loaded.ops == ()


async def test_script_execution_no_op(scope: FxtrScope) -> None:
    """Successful empty script: ops=(), no error/rejected, committed_seq set."""
    ws = _MockWorkstation()
    ref = await build_script_execution_event(
        scope=scope,
        script_src="pass\n",
        preamble_src="",
        stdout="",
        stderr="",
        error=None,
        rejected=None,
        committed_seq=0,
        runner_queued_ops=[],
        export_entity_short_ids={},
        workstation_ref=_MockWorkstationRef(ws),  # type: ignore[arg-type]
        slot_key_for_slot_n=_no_slot_keys,
    )
    loaded = await ref.load()
    assert loaded.ops == ()
    assert loaded.committed_seq == 0


# --- Inbound-event builders ------------------------------------------


async def test_build_invoke_result_event(scope: FxtrScope) -> None:
    """Wire payload walks through; resulting event has slot_key + seq."""
    ws = _MockWorkstation()
    ent = _alloc_id(scope, "invoke_result_ent")
    ws.register_entity("ent_s3", ent)
    ref = await build_invoke_result_event(
        scope=scope,
        slot_key="r",
        slot_n=1,
        seq=5,
        payload={"out": ["entity", "ent_s3"]},
        workstation_ref=_MockWorkstationRef(ws),  # type: ignore[arg-type]
        slot_key_for_slot_n=_no_slot_keys,
    )
    loaded = await ref.load()
    assert isinstance(loaded, InvokeResultLogEvent)
    assert loaded.slot_key == "r"
    assert loaded.slot_n == 1
    assert loaded.seq == 5
    assert loaded.payload == frozendict({"out": RawWeakFxtrID(inner_id=ent)})


async def test_build_invoke_error_event_is_sync_safe(scope: FxtrScope) -> None:
    """error events don't walk payloads — sync construction is fine."""
    ref = build_invoke_error_event(scope=scope, slot_key="r", slot_n=2, seq=6, message="boom")
    loaded = await ref.load()
    assert isinstance(loaded, InvokeErrorLogEvent)
    assert loaded.message == "boom"


async def test_build_callback_notification_event(scope: FxtrScope) -> None:
    """Callback payload walks through; resulting event has slot_key
    ``"workstation_inbox"`` (the user-message convention)."""
    ws = _MockWorkstation()
    type_id = _alloc_id(scope, "cb_payload_svc_type")
    ctx_id = _alloc_id(scope, "cb_payload_svc_ctx")
    ws.register_service("svc_s7", type_id, ctx_id)
    ref = await build_callback_notification_event(
        scope=scope,
        slot_key="workstation_inbox",
        slot_n=3,
        seq=7,
        payload={"echo": ["service", "svc_s7"]},
        workstation_ref=_MockWorkstationRef(ws),  # type: ignore[arg-type]
        slot_key_for_slot_n=_no_slot_keys,
    )
    loaded = await ref.load()
    assert loaded.slot_key == "workstation_inbox"
    placeholder_id = loaded.payload["echo"]  # type: ignore[index]
    assert isinstance(placeholder_id, RawStrongFxtrID)


# --- extend_chain -----------------------------------------------------


async def test_extend_chain_first_head_no_persistence(scope: FxtrScope) -> None:
    """No prev, no persistence cap — chain just stores a new head."""
    event_ref = build_invoke_error_event(scope=scope, slot_key="r", slot_n=1, seq=1, message="m")
    updates: list[EntityRef[WsAgentLogEventChain]] = []
    head = await extend_chain(
        scope=scope,
        prev_head=None,
        event_ref=event_ref,
        persistence=None,
        session_id="session-abc",
        update_head=updates.append,
        timestamp=100.0,
    )
    loaded = await head.load()
    assert loaded.prev is None
    assert loaded.timestamp == 100.0
    assert updates == [head]


async def test_extend_chain_pins_when_persistence_configured(
    scope: FxtrScope,
) -> None:
    """With a persistence cap, the head's id is pinned under session_id."""
    event_ref = build_invoke_error_event(scope=scope, slot_key="r", slot_n=1, seq=1, message="m")
    persistence = _MockPersistence()
    head = await extend_chain(
        scope=scope,
        prev_head=None,
        event_ref=event_ref,
        persistence=persistence,  # type: ignore[arg-type]
        session_id="session-xyz",
        update_head=lambda _h: None,
        timestamp=100.0,
    )
    assert len(persistence.pins) == 1
    pinned_key, pinned_id, overwrite = persistence.pins[0]
    assert pinned_key == "session-xyz"
    assert pinned_id == head.entity_id
    assert overwrite is True


async def test_extend_chain_linked_list_via_prev(scope: FxtrScope) -> None:
    """Three successive extends produce a chain walkable via prev."""
    e1 = build_invoke_error_event(scope=scope, slot_key="a", slot_n=1, seq=1, message="m")
    e2 = build_invoke_error_event(scope=scope, slot_key="b", slot_n=2, seq=2, message="m")
    e3 = build_invoke_error_event(scope=scope, slot_key="c", slot_n=3, seq=3, message="m")

    head1 = await extend_chain(
        scope=scope,
        prev_head=None,
        event_ref=e1,
        persistence=None,
        session_id="s",
        update_head=lambda _h: None,
        timestamp=1.0,
    )
    head2 = await extend_chain(
        scope=scope,
        prev_head=head1,
        event_ref=e2,
        persistence=None,
        session_id="s",
        update_head=lambda _h: None,
        timestamp=2.0,
    )
    head3 = await extend_chain(
        scope=scope,
        prev_head=head2,
        event_ref=e3,
        persistence=None,
        session_id="s",
        update_head=lambda _h: None,
        timestamp=3.0,
    )

    # Walk back from head3.
    loaded3 = await head3.load()
    assert loaded3.prev is not None
    loaded2 = await loaded3.prev.load()
    assert loaded2.prev is not None
    loaded1 = await loaded2.prev.load()
    assert loaded1.prev is None
    assert (loaded3.timestamp, loaded2.timestamp, loaded1.timestamp) == (3.0, 2.0, 1.0)


async def test_extend_chain_update_head_called_after_pin(scope: FxtrScope) -> None:
    """Order: pin first, then update_head. Sequenced so a watcher
    never sees a head that hasn't been durably persisted yet."""
    event_ref = build_invoke_error_event(scope=scope, slot_key="r", slot_n=1, seq=1, message="m")
    observed_order: list[str] = []

    class _OrderingPersistence:
        async def pin(self, key: str, entity: FxtrID, *, overwrite: bool = False) -> None:
            _ = (key, entity, overwrite)
            observed_order.append("pin")

    def _update(h: EntityRef[WsAgentLogEventChain]) -> None:
        _ = h
        observed_order.append("update_head")

    await extend_chain(
        scope=scope,
        prev_head=None,
        event_ref=event_ref,
        persistence=_OrderingPersistence(),  # type: ignore[arg-type]
        session_id="s",
        update_head=_update,
        timestamp=1.0,
    )
    assert observed_order == ["pin", "update_head"]
