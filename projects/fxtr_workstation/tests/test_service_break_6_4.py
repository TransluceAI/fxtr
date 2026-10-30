"""Service-break notifications (6.4).

When the grant backing a tracked service's bridge is revoked — here by
crashing the registering peer's ``/rpc`` connection — the workstation
detects the broken keep-alive lease and:

- pushes an out-of-band, sequence-less :class:`ServiceBrokenNotification`
  to every session subscription currently holding the service,
- flips the visible ``broken`` field on the ``services`` resource record,
- keeps the service *known* (its short_id stays resolvable while tracked)
  but unusable (invokes fail).

Graceful unregister produces a normal removal, not a break. Brokenness
is re-derived from current state, so a subscription that arrives after
the break still learns about it.
"""

from __future__ import annotations

import anyio
import pytest
from fxtr_workstation import WorkstationServer
from fxtr_workstation.client_protocol import (
    AckResponse,
    InvokeErrorEvent,
    MutateSessionRequest,
    MutationCommittedResponse,
    ResourceChangesEvent,
    ResourceSnapshotEvent,
    ServerFrame,
    ServiceBrokenNotification,
    SessionEventFrame,
    SubscribeRequest,
    SubscribeSessionRequest,
)
from fxtr_workstation.protocols import WorkstationServiceProto

from ._fixtures import EchoImpl
from ._session_helpers import create_and_subscribe
from ._ws_client import ClientWS, connect_client, connect_rpc

DEADLOCK_TIMEOUT = 10.0


async def _resolve_workstation(session, scope):  # type: ignore[no-untyped-def]
    return await session.remote_service.replace_service_type(WorkstationServiceProto).resolve(scope)


async def _drain_until(client: ClientWS, predicate, *, timeout: float = 3.0) -> list[ServerFrame]:
    seen: list[ServerFrame] = []
    with anyio.fail_after(timeout):
        while not predicate(seen):
            seen.append(await client.receive())
    return seen


def _has_broken_notif(frames, sub_id: int, short_id: str) -> bool:
    return any(
        isinstance(f, ServiceBrokenNotification)
        and f.subscription_id == sub_id
        and f.service_short_id == short_id
        for f in frames
    )


def _has_broken_resource_delta(frames, short_id: str) -> bool:
    return any(
        isinstance(f, ResourceChangesEvent)
        and any(rec.get("short_id") == short_id and rec.get("broken") is True for rec in f.updated)
        for f in frames
    )


async def _register_and_track_then_crash(
    ws: WorkstationServer, client: ClientWS, sub_id: int
) -> str:
    """Register an echo service from a peer, track it in the session, then
    crash the peer connection. Returns the service short_id."""
    short_id: str | None = None
    with pytest.raises(BaseException):  # noqa: PT012, B017
        async with connect_rpc(ws.rpc_url) as peer:
            workstation = await _resolve_workstation(peer, peer.scope)
            echo_ref = await peer.scope.mount(EchoImpl())
            short_id, _rcb = await workstation.register_service("the-echo", echo_ref)
            await client.send(
                MutateSessionRequest(
                    request_id=10,
                    subscription_id=sub_id,
                    ack_seq=0,
                    update_id=1,
                    patch=[],
                    ops=[{"kind": "track_service", "short_id": short_id}],  # type: ignore[list-item]
                )
            )
            committed = await client.receive()
            assert isinstance(committed, MutationCommittedResponse)
            # Crash the peer ungracefully: its grant on the echo service
            # is revoked, breaking the workstation's keep-alive lease.
            raise RuntimeError("crash peer")
    assert short_id is not None
    return short_id


async def test_tracked_service_break_notifies_session_and_marks_resource() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                await client.send(SubscribeRequest(request_id=100, resource="services"))
                assert (await client.receive()) == AckResponse(request_id=100)
                assert isinstance(await client.receive(), ResourceSnapshotEvent)

                async with create_and_subscribe(client) as (_sid, sub_id):
                    short_id = await _register_and_track_then_crash(ws, client, sub_id)

                    seen = await _drain_until(
                        client,
                        lambda s: _has_broken_notif(s, sub_id, short_id)
                        and _has_broken_resource_delta(s, short_id),
                    )
                    assert _has_broken_notif(seen, sub_id, short_id)
                    assert _has_broken_resource_delta(seen, short_id)

                    # The service stays *known* (short_id resolvable) but
                    # is flagged broken on the record.
                    service_key = ws.state.short_ids.lookup_active_service(short_id)
                    assert service_key is not None
                    assert ws.state.service_records[service_key].broken is True


async def test_broken_service_is_announced_once_per_subscription() -> None:
    """A single break announces once on a subscription; a fresh
    subscription (the reconnect shape) re-announces from current state."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                async with create_and_subscribe(client) as (sid, sub_id):
                    short_id = await _register_and_track_then_crash(ws, client, sub_id)

                    seen = await _drain_until(
                        client, lambda s: _has_broken_notif(s, sub_id, short_id)
                    )
                    # Exactly one notification for this subscription.
                    n_first = sum(
                        isinstance(f, ServiceBrokenNotification)
                        and f.subscription_id == sub_id
                        and f.service_short_id == short_id
                        for f in seen
                    )
                    assert n_first == 1

                    # A fresh subscription to the same session re-announces
                    # the (still-broken) service after its snapshot.
                    await client.send(SubscribeSessionRequest(request_id=200, session_id=sid))
                    seen2 = await _drain_until(
                        client, lambda s: _has_broken_notif(s, 200, short_id)
                    )
                    assert _has_broken_notif(seen2, 200, short_id)


async def test_invoke_on_broken_service_errors() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                async with create_and_subscribe(client) as (_sid, sub_id):
                    short_id = await _register_and_track_then_crash(ws, client, sub_id)
                    await _drain_until(client, lambda s: _has_broken_notif(s, sub_id, short_id))

                    # Invoke on the broken (but still known) service: the
                    # bridged handle's lease is broken, so dispatch fails
                    # and the worker emits an InvokeErrorEvent.
                    await client.send(
                        MutateSessionRequest(
                            request_id=20,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=2,
                            patch=[],
                            ops=[
                                {  # type: ignore[list-item]
                                    "kind": "invoke",
                                    "slot": 1,
                                    "service_short_id": short_id,
                                    "method_name": "echo",
                                    "args": ["hi"],
                                }
                            ],
                        )
                    )
                    seen = await _drain_until(
                        client,
                        lambda s: any(
                            isinstance(f, SessionEventFrame)
                            and isinstance(f.event, InvokeErrorEvent)
                            and f.event.slot == 1
                            for f in s
                        ),
                    )
                    assert any(
                        isinstance(f, SessionEventFrame)
                        and isinstance(f.event, InvokeErrorEvent)
                        and f.event.slot == 1
                        for f in seen
                    )


async def test_graceful_unregister_does_not_mark_broken() -> None:
    """An orderly unregister removes the service from the resource list
    without ever flipping ``broken`` — graceful teardown is not a break."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                await client.send(SubscribeRequest(request_id=100, resource="services"))
                assert (await client.receive()) == AckResponse(request_id=100)
                assert isinstance(await client.receive(), ResourceSnapshotEvent)

                async with connect_rpc(ws.rpc_url) as peer:
                    workstation = await _resolve_workstation(peer, peer.scope)
                    echo_ref = await peer.scope.mount(EchoImpl())
                    short_id, rcb_ref = await workstation.register_service("the-echo", echo_ref)
                    # Wait for the add delta so we have a clean baseline.
                    await _drain_until(
                        client,
                        lambda s: any(
                            isinstance(f, ResourceChangesEvent)
                            and any(rec.get("short_id") == short_id for rec in f.added)
                            for f in s
                        ),
                    )
                    # Graceful release.
                    from fxtr_workstation.protocols import ReleaseCallbackProto

                    rcb = await rcb_ref.replace_service_type(ReleaseCallbackProto).resolve(
                        peer.scope
                    )
                    await rcb.release()

                    seen = await _drain_until(
                        client,
                        lambda s: any(
                            isinstance(f, ResourceChangesEvent) and short_id in f.removed for f in s
                        ),
                    )
                    # Never flipped broken; never a break notification.
                    assert not _has_broken_resource_delta(seen, short_id)
                    assert not any(isinstance(f, ServiceBrokenNotification) for f in seen)
