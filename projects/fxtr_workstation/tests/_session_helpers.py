"""Shared session-protocol helpers for the workstation test suite.

The :func:`create_and_subscribe` async context manager is the canonical
way for a test to spin up a session and subscribe to it. On exit it
closes the session (via a fresh subscription, to avoid touching the
test's primary subscription queue), which releases any session-pinned
RPC services and lets the surrounding ``connect_rpc`` block drain
cleanly.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from typing import Any

from fxtr_workstation.client_protocol import (
    AckResponse,
    CreateSessionRequest,
    ErrorResponse,
    MutateSessionRequest,
    MutationCommittedResponse,
    SessionCreatedResponse,
    SessionSnapshotEvent,
    SubscribeSessionRequest,
)

from ._ws_client import ClientWS

# Reserved request_id / update_id values for the cleanup path. Picked
# high enough that no test is likely to use them for its own ops.
_CLEANUP_SUBSCRIBE_REQUEST_ID = 100_001
_CLEANUP_MUTATE_REQUEST_ID = 100_002
_CLEANUP_UPDATE_ID = 100_003


@contextlib.asynccontextmanager
async def create_and_subscribe(
    client: ClientWS,
    *,
    metadata: dict[str, Any] | None = None,
) -> AsyncGenerator[tuple[str, int]]:
    """Create a session on ``client``, subscribe to it, and on exit
    close the session.

    Yields ``(session_id, subscription_id)``. By convention the
    subscription_id is ``2`` (the request_id of the subscribe call),
    and the create call uses request_id ``1`` — matching the historical
    inline helper.

    Cleanup path: on exit the helper opens a *fresh* subscription on
    the same client to read the current ``last_state.seq`` from a
    clean snapshot, then sends a close mutation on that fresh
    subscription. This avoids racing with whatever events the test
    body may have left unread on the original subscription.

    Cleanup is best-effort: if the session was already closed by the
    test body, the websocket is broken, or any other transient error
    occurs, we silently move on. The point of the cleanup is to
    release session-pinned RPC services so a surrounding
    ``connect_rpc`` block can drain on its own exit; if that part has
    already happened (or no longer applies), no harm done.
    """

    await client.send(CreateSessionRequest(request_id=1, metadata=metadata or {}))
    created = await client.receive()
    assert isinstance(created, SessionCreatedResponse)
    sid = created.session_id

    await client.send(SubscribeSessionRequest(request_id=2, session_id=sid))
    assert (await client.receive()) == AckResponse(request_id=2)
    snap = await client.receive()
    assert isinstance(snap, SessionSnapshotEvent)

    try:
        yield sid, 2
    finally:
        with contextlib.suppress(Exception):
            await _close_session_via_fresh_subscription(client, sid)


async def _close_session_via_fresh_subscription(client: ClientWS, sid: str) -> None:
    """Open a new subscription, read the snapshot's ``last_state.seq``,
    and send a close mutation on the new subscription.

    Tolerates events from any prior subscription showing up in the
    websocket queue: we filter by request_id / subscription_id and
    discard everything else.
    """

    await client.send(
        SubscribeSessionRequest(request_id=_CLEANUP_SUBSCRIBE_REQUEST_ID, session_id=sid)
    )
    last_state_seq: int | None = None
    saw_ack = False
    while True:
        frame = await client.receive()
        if isinstance(frame, ErrorResponse) and frame.request_id == _CLEANUP_SUBSCRIBE_REQUEST_ID:
            # Session is already gone (e.g., the test closed it itself,
            # or the close-mutation observer test consumed the close
            # event and the registry dropped the session).
            return
        if isinstance(frame, AckResponse) and frame.request_id == _CLEANUP_SUBSCRIBE_REQUEST_ID:
            saw_ack = True
            continue
        if (
            isinstance(frame, SessionSnapshotEvent)
            and frame.subscription_id == _CLEANUP_SUBSCRIBE_REQUEST_ID
        ):
            assert saw_ack, "snapshot arrived before ack on cleanup subscribe"
            last_state_seq = frame.last_state.seq
            break
        # Anything else (queued events from the original subscription,
        # foreign session-list updates, etc.): drop on the floor.

    assert last_state_seq is not None

    await client.send(
        MutateSessionRequest(
            request_id=_CLEANUP_MUTATE_REQUEST_ID,
            subscription_id=_CLEANUP_SUBSCRIBE_REQUEST_ID,
            ack_seq=last_state_seq,
            update_id=_CLEANUP_UPDATE_ID,
            patch=[],
            ops=[{"kind": "close"}],  # type: ignore[list-item]
        )
    )
    while True:
        frame = await client.receive()
        if (
            isinstance(frame, MutationCommittedResponse)
            and frame.request_id == _CLEANUP_MUTATE_REQUEST_ID
        ):
            return
        if isinstance(frame, ErrorResponse) and frame.request_id == _CLEANUP_MUTATE_REQUEST_ID:
            return
        # Drop any other frames (foreign-broadcast close events for
        # other subscriptions on this connection, residual queued
        # events, etc.).
