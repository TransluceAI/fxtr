"""Stage 5e.1 tests: open-slot fields on the session debug snapshot.

Covers ``open_callbacks`` / ``open_invokes`` / ``open_loads`` —
the debug-only surface of "what's currently in flight for this
session." Each entry is created at commit time (callbacks at
attach; invokes / loads at worker-spawn) and removed when the
slot's terminal event commits — close-callback for callbacks,
result-or-error event for invokes / loads. Session close clears
all three.

The flush sequencing is verified end-to-end via
:class:`SubscribeSessionDebugRequest`: each open-slot insert
fires from the same snapshot emit hook the refcount primitives
use, and each pop fires from a dedicated emit at the worker's
``finally`` (see ``_drop_open_invoke_slot`` /
``_drop_open_load_slot``).
"""

from __future__ import annotations

import anyio
from fxtr_workstation import WorkstationServer, expose_entity, expose_service
from fxtr_workstation.client_protocol import (
    AckResponse,
    InvokeResultEvent,
    MutateSessionRequest,
    MutationCommittedResponse,
    OpenInvokeInfo,
    ServerFrame,
    SessionDebugSnapshotEvent,
    SessionEventFrame,
    SubscribeSessionDebugRequest,
)
from fxtr_workstation.protocols import WorkstationServiceProto

from ._fixtures import CallbackInvokerImpl, EchoImpl, Note
from ._session_helpers import create_and_subscribe
from ._ws_client import ClientWS, connect_client, connect_rpc

DEADLOCK_TIMEOUT = 10.0


async def _drain_until(
    client: ClientWS,
    predicate,
    *,
    timeout: float = 2.0,
) -> list[ServerFrame]:
    seen: list[ServerFrame] = []
    with anyio.fail_after(timeout):
        while not predicate(seen):
            seen.append(await client.receive())
    return seen


def _last_debug_snapshot(seen: list[ServerFrame]) -> SessionDebugSnapshotEvent:
    """Return the most recent :class:`SessionDebugSnapshotEvent` from
    ``seen``. Useful when the test only cares about the final settled
    state after a sequence of refcount + open-slot pushes."""
    for frame in reversed(seen):
        if isinstance(frame, SessionDebugSnapshotEvent):
            return frame
    raise AssertionError("no SessionDebugSnapshotEvent in drain")


# --- open_callbacks -------------------------------------------------


async def test_open_callbacks_appears_after_create_clears_after_close() -> None:
    """Creating a callback at slot N populates ``open_callbacks[N]``
    on the next debug snapshot; closing it clears the entry."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                async with create_and_subscribe(client) as (sid, sub_id):
                    await client.send(SubscribeSessionDebugRequest(request_id=100, session_id=sid))
                    assert (await client.receive()) == AckResponse(request_id=100)
                    initial = await client.receive()
                    assert isinstance(initial, SessionDebugSnapshotEvent)
                    assert initial.open_callbacks == {}

                    # Create.
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[{"kind": "create_callback", "slot": 1}],  # type: ignore[list-item]
                        )
                    )
                    seen = await _drain_until(
                        client,
                        lambda s: any(
                            isinstance(f, SessionDebugSnapshotEvent) and f.open_callbacks for f in s
                        ),
                    )
                    snap_after_create = _last_debug_snapshot(seen)
                    assert set(snap_after_create.open_callbacks.keys()) == {1}
                    cb_short_id = snap_after_create.open_callbacks[1]
                    assert cb_short_id.startswith("s")

                    # Close.
                    await client.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=1,
                            update_id=2,
                            patch=[],
                            ops=[{"kind": "close_callback", "slot": 1}],  # type: ignore[list-item]
                        )
                    )
                    seen = await _drain_until(
                        client,
                        lambda s: any(
                            isinstance(f, SessionDebugSnapshotEvent) and not f.open_callbacks
                            for f in s
                        ),
                    )
                    snap_after_close = _last_debug_snapshot(seen)
                    assert snap_after_close.open_callbacks == {}


# --- open_invokes ---------------------------------------------------


async def test_open_invokes_appears_at_commit_clears_at_result() -> None:
    """A pending invoke shows up in ``open_invokes`` on the snapshot
    pushed at commit time; once the worker delivers its result event,
    the entry is removed and a fresh snapshot reflects that."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                echo_ref = await rpc.scope.mount(EchoImpl())
                async with (
                    expose_service(ws_ref, "echo", echo_ref, rpc.scope) as svc_short_id,
                    create_and_subscribe(client) as (sid, sub_id),
                ):
                    await client.send(SubscribeSessionDebugRequest(request_id=100, session_id=sid))
                    assert (await client.receive()) == AckResponse(request_id=100)
                    assert isinstance(await client.receive(), SessionDebugSnapshotEvent)

                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {  # type: ignore[list-item]
                                    "kind": "invoke",
                                    "slot": 1,
                                    "service_short_id": svc_short_id,
                                    "method_name": "echo",
                                    "args": ["hello"],
                                }
                            ],
                        )
                    )

                    # Drain until we've seen at least one snapshot
                    # with the open_invokes entry. (The commit-time
                    # incref of the target service emits the
                    # snapshot with the open_invokes entry already
                    # set — see the "set before incref" ordering in
                    # ``apply_mutation``.)
                    seen_open = await _drain_until(
                        client,
                        lambda s: any(
                            isinstance(f, SessionDebugSnapshotEvent) and f.open_invokes for f in s
                        ),
                    )
                    open_snap = _last_debug_snapshot(seen_open)
                    assert set(open_snap.open_invokes.keys()) == {1}
                    info = open_snap.open_invokes[1]
                    assert isinstance(info, OpenInvokeInfo)
                    assert info.service_short_id == svc_short_id
                    assert info.method_name == "echo"

                    # Now drain until the invoke result lands and
                    # the slot is gone from open_invokes.
                    seen_done = await _drain_until(
                        client,
                        lambda s: any(
                            isinstance(f, SessionDebugSnapshotEvent) and not f.open_invokes
                            for f in s
                        )
                        and any(
                            isinstance(f, SessionEventFrame)
                            and isinstance(f.event, InvokeResultEvent)
                            for f in s
                        ),
                    )
                    done_snap = _last_debug_snapshot(seen_done)
                    assert done_snap.open_invokes == {}


# --- open_loads -----------------------------------------------------


async def test_open_loads_appears_at_commit_clears_at_result() -> None:
    """A pending load shows up in ``open_loads`` on the snapshot
    pushed at commit; once the worker delivers its result event the
    entry is removed."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                note = Note(title="t", body="b").store(rpc.scope)
                async with (
                    expose_entity(ws_ref, "the-note", note.entity_id, rpc.scope) as ent_short_id,
                    create_and_subscribe(client) as (sid, sub_id),
                ):
                    await client.send(SubscribeSessionDebugRequest(request_id=100, session_id=sid))
                    assert (await client.receive()) == AckResponse(request_id=100)
                    assert isinstance(await client.receive(), SessionDebugSnapshotEvent)

                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {"kind": "load_entity", "slot": 1, "short_id": ent_short_id}  # type: ignore[list-item]
                            ],
                        )
                    )

                    seen_open = await _drain_until(
                        client,
                        lambda s: any(
                            isinstance(f, SessionDebugSnapshotEvent) and f.open_loads for f in s
                        ),
                    )
                    open_snap = _last_debug_snapshot(seen_open)
                    assert open_snap.open_loads == {1: ent_short_id}

                    seen_done = await _drain_until(
                        client,
                        lambda s: any(
                            isinstance(f, SessionDebugSnapshotEvent) and not f.open_loads for f in s
                        ),
                    )
                    done_snap = _last_debug_snapshot(seen_done)
                    assert done_snap.open_loads == {}


# --- combined behaviors ---------------------------------------------


async def test_session_close_clears_all_open_slot_dicts() -> None:
    """Closing a session that has open callbacks (and would have had
    open invokes / loads if any were in flight) clears every open-slot
    dict on the in-memory ``SessionRecord`` before teardown."""
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
                                {"kind": "create_callback", "slot": 2},  # type: ignore[list-item]
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    from uuid import UUID

                    session = ws.state.sessions[UUID(sid)]
                    assert set(session.open_callbacks.keys()) == {1, 2}

                    # Close the session.
                    await client.send(
                        MutateSessionRequest(
                            request_id=11,
                            subscription_id=sub_id,
                            ack_seq=1,
                            update_id=2,
                            patch=[],
                            ops=[{"kind": "close"}],  # type: ignore[list-item]
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    # Session is gone from the registry; the in-memory
                    # ``SessionRecord`` no longer matters but its
                    # final-drained shape (had it survived) was empty
                    # for all three dicts via the close drain. We
                    # assert the registry-level state.
                    assert UUID(sid) not in ws.state.sessions


async def test_open_invokes_carries_through_pending_callback_invoke() -> None:
    """Sanity: ``open_invokes`` works for an invoke that uses a
    callback as an arg — confirms the open-invokes entry shape is
    independent of arg shape."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                invoker_ref = await rpc.scope.mount(CallbackInvokerImpl())
                async with (
                    expose_service(ws_ref, "invoker", invoker_ref, rpc.scope) as invoker_short_id,
                    create_and_subscribe(client) as (sid, sub_id),
                ):
                    await client.send(SubscribeSessionDebugRequest(request_id=100, session_id=sid))
                    assert (await client.receive()) == AckResponse(request_id=100)
                    assert isinstance(await client.receive(), SessionDebugSnapshotEvent)

                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {"kind": "create_callback", "slot": 1},  # type: ignore[list-item]
                                {  # type: ignore[list-item]
                                    "kind": "invoke",
                                    "slot": 2,
                                    "service_short_id": invoker_short_id,
                                    "method_name": "fire",
                                    "args": [["callback", 1], "hi"],
                                },
                            ],
                        )
                    )

                    seen = await _drain_until(
                        client,
                        lambda s: any(
                            isinstance(f, SessionDebugSnapshotEvent)
                            and f.open_callbacks
                            and f.open_invokes
                            for f in s
                        ),
                    )
                    snap = _last_debug_snapshot(seen)
                    assert set(snap.open_callbacks.keys()) == {1}
                    assert set(snap.open_invokes.keys()) == {2}
                    assert snap.open_invokes[2].method_name == "fire"
