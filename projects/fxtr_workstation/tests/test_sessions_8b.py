"""Stage 8b tests: out-of-band direct entity load.

End-to-end via the ``/client`` wire. The direct-load request is not
tied to a session; it just returns the escaped CBOR payload for an
already-active entity short_id, with no session refcount side effects
and no records minted for transitive children (their short_ids are
allocated inactive in the run-lifetime allocator).
"""

from __future__ import annotations

from uuid import UUID

import anyio
from fxtr_workstation import WorkstationServer, expose_entity
from fxtr_workstation.client_protocol import (
    LoadEntityErrorResponse,
    LoadEntityRequest,
    LoadEntityResponse,
)
from fxtr_workstation.protocols import WorkstationServiceProto
from fxtr_workstation.state import provenanced_key_for

from ._fixtures import Note
from ._session_helpers import create_and_subscribe
from ._ws_client import connect_client, connect_rpc

DEADLOCK_TIMEOUT = 10.0


# --- Happy path ----------------------------------------------------


async def test_direct_load_returns_payload_no_session_side_effects() -> None:
    """Direct-load an active entity. The reply carries the escaped
    CBOR; the requesting session's refcount dicts are *not* touched
    even though the session is active on the same connection."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                note = Note(title="hello", body="world").store(rpc.scope)
                async with (
                    expose_entity(ws_ref, "n", note.entity_id, rpc.scope) as ent_short_id,
                    create_and_subscribe(client) as (sid, _sub_id),
                ):
                    session = ws.state.sessions[UUID(sid)]
                    refcounts_before = dict(session.entity_refcounts)

                    await client.send(LoadEntityRequest(request_id=42, short_id=ent_short_id))
                    reply = await client.receive()
                    assert isinstance(reply, LoadEntityResponse)
                    assert reply.request_id == 42

                    payload = reply.payload
                    assert isinstance(payload, dict)
                    assert payload["title"] == "hello"
                    assert payload["body"] == "world"

                    # Session was untouched.
                    assert dict(session.entity_refcounts) == refcounts_before


async def test_direct_load_children_are_inactive_short_ids() -> None:
    """Children FxtrIDs in the loaded payload (e.g. the ``$fxtr.type``
    marker) appear as ``["entity", short_id]`` tags. The short_ids are
    allocated in the run-lifetime allocator but the keys are *not*
    marked active — ``lookup_active_entity`` returns ``None`` and no
    ``TrackedEntityRecord`` is minted."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                note = Note(title="t", body="b").store(rpc.scope)
                async with expose_entity(ws_ref, "n", note.entity_id, rpc.scope) as ent_short_id:
                    await client.send(LoadEntityRequest(request_id=1, short_id=ent_short_id))
                    reply = await client.receive()
                    assert isinstance(reply, LoadEntityResponse)

                    type_tag = reply.payload["$fxtr.type"]
                    assert isinstance(type_tag, list) and len(type_tag) == 2
                    type_short_id = type_tag[1]

                    # Inactive: lookup_active_entity returns None.
                    assert ws.state.short_ids.lookup_active_entity(type_short_id) is None
                    # But the mapping IS in the run-lifetime allocator —
                    # lookup_known_entity resolves to the ProvenancedKey.
                    known = ws.state.short_ids.lookup_known_entity(type_short_id)
                    assert known is not None
                    # No TrackedEntityRecord was minted for the child.
                    assert known not in ws.state.entity_records


# --- Failure modes -------------------------------------------------


async def test_direct_load_unknown_short_id_errors() -> None:
    """A short_id that doesn't resolve to any active entity replies
    with :class:`LoadEntityErrorResponse`."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                await client.send(LoadEntityRequest(request_id=7, short_id="nope-not-real"))
                reply = await client.receive()
                assert isinstance(reply, LoadEntityErrorResponse)
                assert reply.request_id == 7
                assert "unknown or inactive" in reply.message


# --- Concurrency ---------------------------------------------------


async def test_concurrent_direct_loads_respond_independently() -> None:
    """Two direct loads on the same connection respond with their own
    request_ids in some order; both succeed."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                note = Note(title="t", body="b").store(rpc.scope)
                async with expose_entity(ws_ref, "n", note.entity_id, rpc.scope) as ent_short_id:
                    await client.send(LoadEntityRequest(request_id=100, short_id=ent_short_id))
                    await client.send(LoadEntityRequest(request_id=101, short_id=ent_short_id))

                    seen: set[int] = set()
                    while len(seen) < 2:
                        reply = await client.receive()
                        assert isinstance(reply, LoadEntityResponse)
                        seen.add(reply.request_id)
                    assert seen == {100, 101}


# --- Cross-session sharing -----------------------------------------


async def test_direct_load_entity_pinned_by_other_session() -> None:
    """Direct-load on a connection that has no session pinning the
    target. The entity stays alive via another holder (the RPC-side
    expose_entity registration), and the direct load returns the
    payload."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as bare_client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                note = Note(title="cross", body="conn").store(rpc.scope)
                async with expose_entity(ws_ref, "n", note.entity_id, rpc.scope) as ent_short_id:
                    # bare_client has no session/subscription. Directly
                    # load the entity registered by the rpc connection.
                    await bare_client.send(LoadEntityRequest(request_id=1, short_id=ent_short_id))
                    reply = await bare_client.receive()
                    assert isinstance(reply, LoadEntityResponse)
                    assert reply.payload["title"] == "cross"

                    # Workstation entity registry shows the parent as
                    # active and tracked (via the RPC registration).
                    parent_key = provenanced_key_for(note.entity_id)
                    assert parent_key in ws.state.entity_records
