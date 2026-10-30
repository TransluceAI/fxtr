"""Callback-inbox subscription connection-state handling in the MCP
`/client` reader (6.4post1).

The workstation emits a seq'd :class:`CallbackSubscriptionEvent` when a
callback inbox's ``subscribe_to`` subscription transitions
connected/broken. Unlike the sequence-less ``ServiceBrokenNotification``
(stage 6.4), this rides the normal non-state event path
(``_apply_state_effect``): the MCP tracks the per-slot connection state
in the mirror and surfaces only the **break** direction to the agent as
a best-effort ``subscription_broken`` channel notice, deduped by seq via
``_notified_seqs`` (so reconnect / replay doesn't re-spam).
"""

from __future__ import annotations

import pytest
from fxtr_workstation.client_protocol import CallbackSubscriptionEvent

from fxtr_workstation_mcp.ws_connection import WorkstationConnection

pytestmark = pytest.mark.anyio


def _make_connection() -> WorkstationConnection:
    conn = WorkstationConnection(
        rpc_url="ws://unused/rpc",
        client_url="ws://unused/client",
    )
    conn._active_subscription_id = 1
    return conn


def _with_open_inbox(conn: WorkstationConnection, slot: int, slot_key: str) -> None:
    """Seed the mirror with an open callback inbox at ``slot`` so the
    connection-state branch can resolve ``slot -> slot_key``."""
    conn._mirror.data_by_key = {slot_key: ["callback_open", slot]}
    conn._mirror.pending_slot_index = {slot: slot_key}


async def test_broken_transition_notifies_agent_with_reason() -> None:
    conn = _make_connection()
    notifications: list[tuple[str, dict[str, str]]] = []

    async def capture(content: str, attrs: dict[str, str]) -> None:
        notifications.append((content, attrs))

    conn._notify_channel = capture  # type: ignore[assignment]
    _with_open_inbox(conn, slot=5, slot_key="cb")

    await conn._apply_state_effect(
        CallbackSubscriptionEvent(seq=7, slot=5, connected=False, reason="rpc connection dropped")
    )

    assert len(notifications) == 1
    content, attrs = notifications[0]
    assert attrs == {"kind": "subscription_broken", "slot_key": "cb"}
    assert "cb" in content
    assert "rpc connection dropped" in content
    # Mirror reflects the broken state.
    assert conn._mirror.callback_subscription_states == {"cb": "broken"}


async def test_connected_transition_updates_state_without_notifying() -> None:
    conn = _make_connection()
    notifications: list[tuple[str, dict[str, str]]] = []

    async def capture(content: str, attrs: dict[str, str]) -> None:
        notifications.append((content, attrs))

    conn._notify_channel = capture  # type: ignore[assignment]
    _with_open_inbox(conn, slot=5, slot_key="cb")

    await conn._apply_state_effect(
        CallbackSubscriptionEvent(seq=7, slot=5, connected=True, reason=None)
    )

    # Connected is non-actionable: state updated, no channel frame.
    assert notifications == []
    assert conn._mirror.callback_subscription_states == {"cb": "connected"}


async def test_broken_then_reconnect_reduces_to_connected() -> None:
    conn = _make_connection()
    conn._notify_channel = None  # type: ignore[assignment]
    _with_open_inbox(conn, slot=5, slot_key="cb")

    await conn._apply_state_effect(
        CallbackSubscriptionEvent(seq=7, slot=5, connected=False, reason="boom")
    )
    assert conn._mirror.callback_subscription_states["cb"] == "broken"

    # A later connected transition (resubscribe) flips it back — latest
    # event wins.
    await conn._apply_state_effect(
        CallbackSubscriptionEvent(seq=9, slot=5, connected=True, reason=None)
    )
    assert conn._mirror.callback_subscription_states["cb"] == "connected"


async def test_broken_notification_deduped_by_seq_on_replay() -> None:
    """Re-applying the same break event (the reconnect / state-update
    rebuild shape) does not re-notify the agent — dedup rides the
    existing ``_notified_seqs`` machinery."""
    conn = _make_connection()
    notifications: list[tuple[str, dict[str, str]]] = []

    async def capture(content: str, attrs: dict[str, str]) -> None:
        notifications.append((content, attrs))

    conn._notify_channel = capture  # type: ignore[assignment]
    _with_open_inbox(conn, slot=5, slot_key="cb")

    ev = CallbackSubscriptionEvent(seq=7, slot=5, connected=False, reason="boom")
    await conn._apply_state_effect(ev)
    await conn._apply_state_effect(ev)  # replay

    assert len(notifications) == 1


async def test_unknown_slot_dropped_without_crash() -> None:
    """A connection-state transition whose inbox slot is no longer in
    the mirror (closed / forgotten) is dropped — informational, so it
    must not raise (unlike payload-bearing events) and must not notify."""
    conn = _make_connection()
    notifications: list[tuple[str, dict[str, str]]] = []

    async def capture(content: str, attrs: dict[str, str]) -> None:
        notifications.append((content, attrs))

    conn._notify_channel = capture  # type: ignore[assignment]
    # No open inbox seeded → pending_slot_index has no entry for slot 5.

    await conn._apply_state_effect(
        CallbackSubscriptionEvent(seq=7, slot=5, connected=False, reason="boom")
    )

    assert notifications == []
    assert conn._mirror.callback_subscription_states == {}
