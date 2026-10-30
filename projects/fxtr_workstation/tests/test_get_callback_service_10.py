"""Stage 10: tests for ``GetCallbackServiceRequest``.

Covers the dedicated single-shot request that resolves a session's
callback slot to its workstation send-side service short_id. Used by
the agent MCP plugin to discover the ``workstation_inbox`` callback's
short_id without subscribing to the session debug stream — but the
request itself is session-agnostic and any caller with an active
``SessionSubscription`` can use it.

Scenarios:

- Success: create a callback at a slot, fetch its short_id, verify it
  equals ``session.open_callbacks[slot].service_short_id`` server-side.
- Error: subscription_id refers to a non-session subscription
  (resource subscription).
- Error: subscription_id is unknown on the calling connection.
- Error: slot is unknown in ``session.open_callbacks``.
"""

from __future__ import annotations

from uuid import UUID

import anyio
from fxtr_workstation import WorkstationServer
from fxtr_workstation.client_protocol import (
    AckResponse,
    ErrorResponse,
    GetCallbackServiceRequest,
    GetCallbackServiceResponse,
    MutateSessionRequest,
    MutationCommittedResponse,
    SubscribeRequest,
)

from ._session_helpers import create_and_subscribe
from ._ws_client import connect_client

DEADLOCK_TIMEOUT = 10.0


async def test_get_callback_service_success() -> None:
    """Create a callback at slot 1, fetch its short_id, verify the
    response matches ``session.open_callbacks[1].service_short_id``."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                async with create_and_subscribe(client) as (sid, sub_id):
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {"kind": "create_callback", "slot": 1},  # type: ignore[list-item]
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)

                    # Server-side ground truth.
                    session = ws.state.sessions[UUID(sid)]
                    expected_short_id = session.open_callbacks[1].service_short_id

                    await client.send(
                        GetCallbackServiceRequest(request_id=20, subscription_id=sub_id, slot=1)
                    )
                    frame = await client.receive()
                    assert isinstance(frame, GetCallbackServiceResponse)
                    assert frame.request_id == 20
                    assert frame.subscription_id == sub_id
                    assert frame.slot == 1
                    assert frame.service_short_id == expected_short_id


async def test_get_callback_service_unknown_slot() -> None:
    """Querying a slot that doesn't exist in ``open_callbacks``
    returns an :class:`ErrorResponse`."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                async with create_and_subscribe(client) as (_sid, sub_id):
                    await client.send(
                        GetCallbackServiceRequest(request_id=30, subscription_id=sub_id, slot=999)
                    )
                    frame = await client.receive()
                    assert isinstance(frame, ErrorResponse)
                    assert frame.request_id == 30
                    assert "no open callback at slot 999" in frame.message


async def test_get_callback_service_unknown_subscription_id() -> None:
    """Querying with a subscription_id not bound on this connection
    returns an :class:`ErrorResponse`."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                # No subscription has been opened yet.
                await client.send(
                    GetCallbackServiceRequest(request_id=40, subscription_id=999, slot=1)
                )
                frame = await client.receive()
                assert isinstance(frame, ErrorResponse)
                assert frame.request_id == 40
                assert "no active session subscription" in frame.message


async def test_get_callback_service_subscription_is_not_session() -> None:
    """Calling with a resource subscription_id (not a
    :class:`SessionSubscription`) returns :class:`ErrorResponse`."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                # Subscribe to a resource so subscription_id 7 is bound,
                # but it's a ResourceSubscription, not a SessionSubscription.
                await client.send(SubscribeRequest(request_id=7, resource="sessions"))
                # Drain the ack + snapshot.
                while True:
                    frame = await client.receive()
                    if isinstance(frame, AckResponse) and frame.request_id == 7:
                        break
                # And the snapshot (next frame after ack).
                _ = await client.receive()

                await client.send(
                    GetCallbackServiceRequest(request_id=50, subscription_id=7, slot=1)
                )
                frame = await client.receive()
                assert isinstance(frame, ErrorResponse)
                assert frame.request_id == 50
                assert "no active session subscription" in frame.message
