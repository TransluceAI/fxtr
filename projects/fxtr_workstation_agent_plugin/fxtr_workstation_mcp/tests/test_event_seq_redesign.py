"""Unit tests for the event-seq redesign (introduced in stage
3-post-2, completed in stage 4).

Covers:

- ``_handle_state_update`` updates the mirror (state +
  ``last_state_seq`` / ``last_state_ack_seq`` + tracker pin sets +
  slot_counter), trims ``_replayable_events`` by ``ev.ack_seq``,
  and rebuilds ``_unflushed_state_changes`` via
  ``_apply_state_effect`` (the same code path as initial delivery).
- The seq-regression hard-error case.
- The non-state event arm of ``_do_event_job`` advances
  ``_last_processed_seq`` inline; hard-error checks fire for
  within-cycle re-delivery + past-horizon delivery.
- Foreign-overwrite + slot-lookup-failure: redelivery is
  bit-identical to initial delivery, so a lookup failure during
  the replay raises ``ProtocolError`` (same as on arrival), and
  the supervisor tears down.
- Notification-dedupe via ``_notified_seqs``: subscribe-time
  replay and the rebuild loop don't re-surface events the agent
  already saw.

All tests use the real ``_apply_state_effect`` (stage 4's
implementation). Earlier revisions of this file monkey-patched a
stage-4-shaped stub because the real handler hadn't landed yet.
"""

from __future__ import annotations

import pytest
from fxtr_workstation.client_protocol import (
    InvokeResultEvent,
    SessionEventFrame,
    StateUpdateEvent,
)

from fxtr_workstation_mcp.ws_connection import (
    ProtocolError,
    WorkstationConnection,
    _EventJob,
)

pytestmark = pytest.mark.anyio


def _make_connection() -> WorkstationConnection:
    conn = WorkstationConnection(
        rpc_url="ws://unused/rpc",
        client_url="ws://unused/client",
    )
    conn._active_subscription_id = 1
    return conn


def _state_update(
    *,
    seq: int,
    ack_seq: int = 0,
    state: dict | None = None,
    slot_counter: int = 0,
) -> StateUpdateEvent:
    """Build a StateUpdateEvent. Defaults the state to carry both
    top-level fields so the connection's init-prologue detector
    (``workstation_state_needs_init``) doesn't fire and produce a
    pending change the test isn't expecting.

    If a test explicitly wants the init prologue to fire, pass
    ``state={}``.
    """
    return StateUpdateEvent(
        seq=seq,
        ack_seq=ack_seq,
        state=state or {"data_by_key": {}, "callback_notifications": {}},
        tracked_entities=[],
        tracked_services=[],
        slot_counter=slot_counter,
    )


# --- _handle_state_update ----------------------------------------------


async def test_handle_state_update_initial_apply() -> None:
    """First state_update on a fresh connection: just applies."""
    conn = _make_connection()
    await conn._handle_state_update(
        _state_update(seq=4, ack_seq=0, state={"data_by_key": {"k": ["exported_entity", "e1"]}})
    )
    assert conn._mirror.last_state_seq == 4
    assert conn._mirror.last_state_ack_seq == 0
    assert conn._mirror.data_by_key == {"k": ["exported_entity", "e1"]}
    # Helper does NOT advance _last_processed_seq — the caller's job.
    assert conn._last_processed_seq == 0


async def test_handle_state_update_seq_regression_raises() -> None:
    """A state_update with seq <= mirror.last_state_seq is a
    workstation protocol violation."""
    conn = _make_connection()
    await conn._handle_state_update(_state_update(seq=10))
    with pytest.raises(ProtocolError, match="regressed"):
        await conn._handle_state_update(_state_update(seq=10))
    with pytest.raises(ProtocolError, match="regressed"):
        await conn._handle_state_update(_state_update(seq=5))


async def test_handle_state_update_trims_replayable_events_by_ack_seq() -> None:
    """Events with seq <= new ack_seq drop out of
    _replayable_events (and consequently out of the derived
    _unflushed_state_changes). Surviving events get re-applied
    via _apply_state_effect — same code path as initial delivery."""
    conn = _make_connection()
    # Initial state with two pending slots.
    await conn._handle_state_update(
        _state_update(
            seq=10,
            ack_seq=0,
            state={
                "data_by_key": {
                    "a": ["invoke_pending", 1],
                    "b": ["invoke_pending", 2],
                }
            },
        )
    )
    # Two non-state events arrive — _apply_state_effect appends to
    # replayable + writes the derived dict.
    await conn._apply_state_effect(InvokeResultEvent(seq=11, slot=1, payload="RA"))
    await conn._apply_state_effect(InvokeResultEvent(seq=12, slot=2, payload="RB"))
    assert conn._mirror.data_by_key["a"] == ["invoke_result", "RA"]
    assert conn._mirror.data_by_key["b"] == ["invoke_result", "RB"]

    # New state_update: ack_seq=11 → drops event seq=11 (key "a"),
    # keeps event seq=12 (key "b"). The new state reflects "a"
    # promoted (workstation absorbed) and "b" still pending.
    await conn._handle_state_update(
        _state_update(
            seq=13,
            ack_seq=11,
            state={
                "data_by_key": {
                    "a": ["invoke_result", "RA"],
                    "b": ["invoke_pending", 2],
                }
            },
        )
    )

    # Trimmed: only ev_b survives.
    assert [e.seq for e in conn._replayable_events] == [12]
    # Derived pending_changes: one set for "b". (The workstation
    # view already has data_by_key, so no init prologue is
    # synthesized.)
    set_changes = [
        c for c in conn._pending_changes if c.op == "set" and c.path[:1] == ("data_by_key",)
    ]
    assert [c.path[1] for c in set_changes] == ["b"]
    assert set_changes[0].seq == 12
    # Mirror: local view layers "b"'s promotion on top of the
    # workstation view.
    assert conn._mirror.data_by_key == {
        "a": ["invoke_result", "RA"],
        "b": ["invoke_result", "RB"],
    }


async def test_handle_state_update_rebuild_hard_fails_on_foreign_overwrite() -> None:
    """If a foreign mutator's state_update overwrote a slot_key we
    had a local promotion for, the rebuild's slot lookup fails
    against the new state — ProtocolError, same as if the failure
    happened at initial delivery. Supervisor tears down."""
    conn = _make_connection()
    # Initial state: slot 5 is pending under key "x".
    await conn._handle_state_update(
        _state_update(
            seq=10,
            ack_seq=0,
            state={"data_by_key": {"x": ["invoke_pending", 5]}},
        )
    )
    # Non-state event arrives at seq=12 — local promotion of "x".
    await conn._apply_state_effect(InvokeResultEvent(seq=12, slot=5, payload="local"))
    assert conn._mirror.data_by_key["x"] == ["invoke_result", "local"]

    # Foreign state_update at seq=15 (ack_seq=10, doesn't subsume
    # ev) overwrote key "x" with their own value. The rebuild
    # tries to replay our event against the new mirror — slot 5
    # lookup fails — ProtocolError.
    with pytest.raises(ProtocolError, match="no matching invoke_pending entry"):
        await conn._handle_state_update(
            _state_update(
                seq=15,
                ack_seq=10,
                state={"data_by_key": {"x": ["exported_entity", "foreign_value"]}},
            )
        )


async def test_handle_state_update_advances_slot_counter() -> None:
    """slot_counter is monotonic — each state_update with a higher
    value advances it."""
    conn = _make_connection()
    await conn._handle_state_update(_state_update(seq=2, slot_counter=5))
    assert conn._slot_counter == 5
    await conn._handle_state_update(_state_update(seq=3, slot_counter=8))
    assert conn._slot_counter == 8
    # A state_update can carry an unchanged slot_counter (no slot
    # ops in this mutation) — _slot_counter stays at the max.
    await conn._handle_state_update(_state_update(seq=4, slot_counter=8))
    assert conn._slot_counter == 8


async def test_handle_state_update_does_not_advance_last_processed_seq() -> None:
    """Single-responsibility check: ``_handle_state_update`` never
    touches ``_last_processed_seq``. The caller is responsible for
    advancing inline (foreign events / subscribe) or deferring via
    ``_AckCommitJob`` (self-commit)."""
    conn = _make_connection()
    conn._last_processed_seq = 7  # pretend we were here
    await conn._handle_state_update(_state_update(seq=20))
    assert conn._mirror.last_state_seq == 20  # mirror advanced
    assert conn._last_processed_seq == 7  # but seq did not


# --- Non-state event arm of _do_event_job -----------------------------


def _non_state_event_job(*, seq: int, slot: int = 1) -> _EventJob:
    return _EventJob(
        frame=SessionEventFrame(
            subscription_id=1,
            event=InvokeResultEvent(seq=seq, slot=slot, payload="ok"),
        )
    )


async def test_non_state_event_within_cycle_redelivery_raises() -> None:
    """An event with seq <= _last_processed_seq is a within-cycle
    re-delivery — the workstation shouldn't ever do this."""
    conn = _make_connection()
    # Apply a state_update + inline-advance to simulate foreign arm.
    await conn._handle_state_update(
        _state_update(seq=5, state={"data_by_key": {"k": ["invoke_pending", 1]}})
    )
    conn._last_processed_seq = 5
    # Process a non-state event at seq=10 — passes both checks.
    await conn._do_event_job(_non_state_event_job(seq=10))
    assert conn._last_processed_seq == 10
    # Re-delivery of the same seq raises.
    with pytest.raises(ProtocolError, match="re-delivered within cycle"):
        await conn._do_event_job(_non_state_event_job(seq=10))


async def test_non_state_event_past_ack_horizon_raises() -> None:
    """An event with seq <= mirror.last_state_ack_seq is past the
    workstation's retention horizon — the workstation should never
    deliver it on the live stream. To exercise this check in
    isolation (without the within-cycle check firing first), we
    leave _last_processed_seq low and let the horizon check fire."""
    conn = _make_connection()
    await conn._handle_state_update(_state_update(seq=11, ack_seq=10))
    # Don't advance _last_processed_seq — leave it at 0. Now an
    # event at seq=8 only triggers the past-horizon check, not
    # the within-cycle one.
    with pytest.raises(ProtocolError, match="past the workstation's ack horizon"):
        await conn._do_event_job(_non_state_event_job(seq=8))


async def test_unhandled_event_kind_raises_not_implemented() -> None:
    """``_apply_state_effect`` raises ``NotImplementedError`` for
    non-state event kinds the MCP doesn't yet handle. This drives
    the worker's bare-dispatch teardown signal — later stages
    fill in the per-kind branches as they wire the corresponding
    agent-facing ops."""
    from fxtr_workstation.client_protocol import LoadResultEvent

    conn = _make_connection()
    await conn._handle_state_update(_state_update(seq=5))
    conn._last_processed_seq = 5
    # LoadResultEvent is still a stub in stage 4. Real handler
    # lands when the agent gains a load-via-session-load builtin.
    job = _EventJob(
        frame=SessionEventFrame(
            subscription_id=1,
            event=LoadResultEvent(seq=10, slot=1, payload=None),
        )
    )
    with pytest.raises(NotImplementedError, match="no handler yet"):
        await conn._do_event_job(job)


# --- Resets ------------------------------------------------------------


async def test_close_session_state_resets_per_session_fields() -> None:
    """``_do_close_session`` and ``_do_subscribe`` wipe every per-
    session field — mirror, replayable events, derived dict,
    slot_counter, last_processed_seq."""
    conn = _make_connection()
    await conn._handle_state_update(
        _state_update(
            seq=4,
            slot_counter=3,
            state={"data_by_key": {"k": ["invoke_pending", 1]}},
        )
    )
    await conn._apply_state_effect(InvokeResultEvent(seq=5, slot=1, payload="x"))
    conn._last_processed_seq = 5
    # Simulate what _do_close_session / _do_subscribe wipe does:
    from fxtr_workstation_mcp.session_state import SessionMirror

    conn._mirror = SessionMirror()
    conn._replayable_events = []
    conn._pending_changes = []
    conn._slot_counter = 0
    conn._last_processed_seq = 0
    # A fresh state_update applies cleanly without regression.
    await conn._handle_state_update(_state_update(seq=99, slot_counter=7))
    assert conn._mirror.last_state_seq == 99
    assert conn._slot_counter == 7
    assert conn._replayable_events == []
    # After applying a fresh state_update that already has both
    # top-level fields, no init prologue is queued.
    assert conn._pending_changes == []


# --- Pending-slot reverse index ----------------------------------------


async def test_pending_slot_index_populated_from_state() -> None:
    """``SessionMirror.pending_slot_index`` is populated by
    ``apply_workstation_view`` from any ``["invoke_pending", N]``
    entries in the new data_by_key."""
    conn = _make_connection()
    await conn._handle_state_update(
        _state_update(
            seq=10,
            state={
                "data_by_key": {
                    "a": ["invoke_pending", 5],
                    "b": ["exported_entity", "e1"],
                    "c": ["invoke_pending", 7],
                    "d": ["invoke_result", "old"],  # not pending
                }
            },
        )
    )
    assert conn._mirror.pending_slot_index == {5: "a", 7: "c"}


async def test_pending_slot_index_dropped_on_apply_state_effect() -> None:
    """When ``_apply_state_effect`` promotes a slot from pending
    to result (or error), the per-slot pending index entry drops."""
    conn = _make_connection()
    await conn._handle_state_update(
        _state_update(
            seq=10,
            state={
                "data_by_key": {
                    "a": ["invoke_pending", 5],
                    "b": ["invoke_pending", 6],
                }
            },
        )
    )
    assert conn._mirror.pending_slot_index == {5: "a", 6: "b"}
    # Real handler promotes slot 5 → drops index[5].
    await conn._apply_state_effect(InvokeResultEvent(seq=11, slot=5, payload="RA"))
    assert conn._mirror.pending_slot_index == {6: "b"}
    assert conn._mirror.data_by_key["a"] == ["invoke_result", "RA"]
    # Same for InvokeErrorEvent on slot 6.
    from fxtr_workstation.client_protocol import InvokeErrorEvent

    await conn._apply_state_effect(InvokeErrorEvent(seq=12, slot=6, message="boom"))
    assert conn._mirror.pending_slot_index == {}
    assert conn._mirror.data_by_key["b"] == ["invoke_error", "boom"]


async def test_apply_state_effect_no_pending_slot_raises() -> None:
    """If a result event arrives for a slot the mirror doesn't
    know about (e.g. foreign mutator overwrote the slot_key, or
    workstation bug), raise ProtocolError."""
    conn = _make_connection()
    await conn._handle_state_update(_state_update(seq=10, state={"data_by_key": {}}))
    with pytest.raises(ProtocolError, match="no matching invoke_pending entry"):
        await conn._apply_state_effect(InvokeResultEvent(seq=11, slot=5, payload="x"))


async def test_apply_state_effect_invoke_error() -> None:
    """InvokeErrorEvent writes ``["invoke_error", message]``."""
    conn = _make_connection()
    from fxtr_workstation.client_protocol import InvokeErrorEvent

    await conn._handle_state_update(
        _state_update(seq=10, state={"data_by_key": {"k": ["invoke_pending", 1]}})
    )
    await conn._apply_state_effect(InvokeErrorEvent(seq=11, slot=1, message="boom"))
    assert conn._mirror.data_by_key["k"] == ["invoke_error", "boom"]
    # Pending change recorded for the slot promotion.
    set_for_k = [
        c for c in conn._pending_changes if c.op == "set" and c.path == ("data_by_key", "k")
    ]
    assert len(set_for_k) == 1
    assert set_for_k[0].value == ["invoke_error", "boom"]
    assert 1 not in conn._mirror.pending_slot_index


# --- Notification dedupe -----------------------------------------------


async def test_notification_dedupe_skips_replayed_events() -> None:
    """``_notify_for_event`` records each seq in ``_notified_seqs``
    on first delivery and suppresses re-delivery. The dedupe set
    persists across reconnect-time state wipes so re-delivered
    events from ``snapshot.subsequent_events`` don't
    double-notify."""
    from fxtr_workstation_mcp.session_state import SessionMirror

    conn = _make_connection()
    # Initial state with one pending slot.
    await conn._handle_state_update(
        _state_update(seq=10, state={"data_by_key": {"k": ["invoke_pending", 1]}})
    )
    conn._last_processed_seq = 10
    # Process an event at seq=15. _notify_for_event runs; seq=15
    # recorded.
    await conn._do_event_job(_non_state_event_job(seq=15))
    assert conn._notified_seqs == {15}

    # Simulate a reconnect: _do_subscribe wipes per-session state
    # but deliberately does NOT touch _notified_seqs. Then the
    # snapshot's last_state (still at seq=10) is applied + the
    # snapshot's subsequent_events redeliver seq=15.
    conn._mirror = SessionMirror()
    conn._replayable_events = []
    conn._pending_changes = []
    conn._last_processed_seq = 0
    conn._slot_counter = 0
    # _notified_seqs intentionally not wiped.
    await conn._handle_state_update(
        _state_update(seq=10, state={"data_by_key": {"k": ["invoke_pending", 1]}})
    )
    # Subscribe inline-advances _last_processed_seq.
    conn._last_processed_seq = max(conn._last_processed_seq, 10)
    # Redeliver the event. Passes the within-cycle check
    # (15 > 10) but the dedupe layer suppresses the notification.
    await conn._do_event_job(_non_state_event_job(seq=15))
    assert conn._notified_seqs == {15}  # still only one entry


async def test_notification_dedupe_pruned_on_state_update() -> None:
    """When a state_update arrives whose ack_seq subsumes some
    previously-notified seqs, those drop from ``_notified_seqs``.
    Bounds growth and prevents resurrection-then-redundant-notify
    in pathological cases."""
    conn = _make_connection()
    await conn._handle_state_update(
        _state_update(seq=10, state={"data_by_key": {"k": ["invoke_pending", 1]}})
    )
    # Pretend we notified about events 11, 12, 15.
    conn._notified_seqs = {11, 12, 15}
    # New state_update with ack_seq=12 subsumes 11 and 12 (those
    # events are now past the workstation's retention horizon and
    # won't be re-delivered).
    await conn._handle_state_update(_state_update(seq=20, ack_seq=12))
    assert conn._notified_seqs == {15}


async def test_notification_dedupe_pruned_on_self_commit() -> None:
    """Same pruning happens on self-commit (which also advances
    the ack horizon)."""
    from fxtr_workstation_mcp.mutation_builder import BuildResult
    from fxtr_workstation.client_protocol import (
        MutateSessionRequest,
        MutationCommittedResponse,
    )

    conn = _make_connection()
    await conn._handle_state_update(_state_update(seq=10))
    conn._notified_seqs = {11, 12, 15}
    build_result = BuildResult(
        request=MutateSessionRequest(
            request_id=1,
            subscription_id=1,
            ack_seq=12,
            update_id=1,
            patch=[],
            ops=[],
        ),
        post_state={"data_by_key": {}, "callback_notifications": {}},
        desired_entities=set(),
        desired_services=set(),
        new_slot_counter=0,
        substituted_runner_queued_ops=[],
        slot_allocations={},
    )
    response = MutationCommittedResponse(
        request_id=1,
        subscription_id=1,
        seq=20,
        update_id=1,
    )
    conn._apply_local_commit(build_result, response)
    assert conn._notified_seqs == {15}


# --- _apply_local_commit doesn't synthesize -----------------------------


async def test_apply_local_commit_direct_clears_replayable() -> None:
    """``_apply_local_commit`` clears ``_replayable_events`` and
    ``_unflushed_state_changes`` directly (no rebuild loop) since
    by construction our own ack_seq covers every event."""
    from fxtr_workstation_mcp.mutation_builder import BuildResult
    from fxtr_workstation.client_protocol import (
        MutateSessionRequest,
        MutationCommittedResponse,
    )

    conn = _make_connection()
    await conn._handle_state_update(
        _state_update(seq=10, state={"data_by_key": {"k": ["invoke_pending", 1]}})
    )
    # Two locally-absorbed events.
    await conn._apply_state_effect(InvokeResultEvent(seq=11, slot=1, payload="x"))
    conn._last_processed_seq = 11
    # Self-commit sends ack_seq=11 (= _last_processed_seq).
    build_result = BuildResult(
        request=MutateSessionRequest(
            request_id=1,
            subscription_id=1,
            ack_seq=11,
            update_id=1,
            patch=[],
            ops=[],
        ),
        post_state={
            "data_by_key": {"k": ["invoke_result", "x"]},
            "callback_notifications": {},
        },
        desired_entities=set(),
        desired_services=set(),
        new_slot_counter=0,
        substituted_runner_queued_ops=[],
        slot_allocations={},
    )
    response = MutationCommittedResponse(
        request_id=1,
        subscription_id=1,
        seq=12,
        update_id=1,
    )
    conn._apply_local_commit(build_result, response)
    # Mirror reset to the post-commit view.
    assert conn._mirror.last_state_seq == 12
    assert conn._mirror.last_state_ack_seq == 11
    assert conn._mirror.data_by_key == {"k": ["invoke_result", "x"]}
    # Replayable + pending cleared directly.
    assert conn._replayable_events == []
    assert conn._pending_changes == []
