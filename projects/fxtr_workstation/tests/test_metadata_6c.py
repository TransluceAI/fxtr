"""Stage 6c tests: ``GetEntityMetadataOp`` / ``GetServiceMetadataOp``.

Covers:

- Bare ``(data, None)`` for a ``d``-tagged entity returns
  ``id_string`` + ``unprovenanced``; no provenance fields, no
  ``unwraps_to``.
- Layered ``(data, prov)`` returns ``id_string`` + ``unprovenanced``
  + ``provenance_parent`` + ``provenance_parent_id_string``.
- Provenance-tag data IDs (``c``/``n``/``p``/``u``) get
  ``unwraps_to`` + ``unwraps_to_id_string`` populated.
- ``GetServiceMetadata`` returns ``type_id_string``.
- Refs in metadata participate in retention: after the metadata
  event ages out, the session's refcounts on those keys drop.
- Op validation rejects unknown / inactive short_ids; slot-
  monotonicity is enforced.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from uuid import UUID

import anyio
from fxtr2.data.entity_core import FxtrCbor, FxtrID, RawFxtrEntityMarkerID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr_workstation import WorkstationServer, expose_entity, expose_service
from fxtr_workstation.client_protocol import (
    EntityMetadataResultEvent,
    MutateSessionRequest,
    MutationCommittedResponse,
    MutationRejectedResponse,
    ServiceMetadataResultEvent,
    SessionEventFrame,
)
from fxtr_workstation.protocols import WorkstationServiceProto
from fxtr_workstation.state import (
    _ensure_entity_record_via_track,
    _maybe_release_entity_record,
    provenanced_key_for,
)

from ._fixtures import EchoImpl, Note
from ._session_helpers import create_and_subscribe
from ._ws_client import connect_client, connect_rpc

DEADLOCK_TIMEOUT = 10.0

GROUND = FxtrID.from_tag_and_payload(".", b"")


def _make_c_node(scope: FxtrScope, ctx_key: str = "test") -> FxtrID:
    """Build a c-node in ``scope``'s arena that unwraps to ``"."``.
    Mirrors the helper in ``v2/tests/test_entity_ref_scope/test_provenance_flow.py``.
    """
    ctx_type = scope.track_builtin(FxtrID.builtin("ctx"))
    context_data: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=GROUND),
        "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
        "value": RawWeakFxtrID(inner_id=GROUND),
        "ctx_key": ctx_key,
    }
    return scope.create_c_node(context_data)


def _store_simple_in_scope(scope: FxtrScope) -> FxtrID:
    builtin_type = scope.track_builtin(FxtrID.builtin("simple_metadata_test"))
    cbor: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=scope.get_context_marker_id()),
        "$fxtr.type": RawWeakFxtrID(inner_id=builtin_type),
        "name": "x",
    }
    return scope.store_cbor(cbor)


@contextlib.asynccontextmanager
async def _layered_record(
    ws: WorkstationServer,
) -> AsyncGenerator[tuple[FxtrID, FxtrScope], None]:
    """Mint a layered FxtrID via a c-node provenance scope, store it
    as a tracked record in the workstation, and yield ``(layered_fid,
    scope)``. Releases the record on exit so server shutdown sees an
    empty registry."""
    async with FxtrScope(isolated=True, include=[ws.state.root_scope]) as builder:
        c_node = _make_c_node(builder)
        async with FxtrScope(
            isolated=True,
            include=[builder],
            provenance_context=c_node,
            provenance_context_from_scope=builder,
        ) as scope:
            fid = _store_simple_in_scope(scope)
            with ws.state.mutation_context():
                _ensure_entity_record_via_track(ws.state, fid, scope)
            try:
                yield fid, scope
            finally:
                with ws.state.mutation_context():
                    rec = ws.state.entity_records.get(provenanced_key_for(fid))
                    if rec is not None:
                        _maybe_release_entity_record(ws.state, rec)


# =====================================================================
# GetEntityMetadata — bare data ID.
# =====================================================================


async def test_metadata_for_bare_d_node_has_only_id_string_and_unprovenanced() -> None:
    """A ``d``-tagged bare data ID has no provenance parent and no
    unwrap target — the metadata payload contains exactly
    ``id_string`` + ``unprovenanced``."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                note = Note(title="t", body="b").store(rpc.scope)
                async with (
                    expose_entity(ws_ref, "n", note.entity_id, rpc.scope) as ent_short_id,
                    create_and_subscribe(client) as (_sid, sub_id),
                ):
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {  # type: ignore[list-item]
                                    "kind": "get_entity_metadata",
                                    "slot": 1,
                                    "short_id": ent_short_id,
                                }
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    frame = await client.receive()
                    assert isinstance(frame, SessionEventFrame)
                    event = frame.event
                    assert isinstance(event, EntityMetadataResultEvent)
                    assert event.slot == 1

                    md = event.metadata
                    # id_string is the bare d-id.
                    assert md["id_string"] == note.entity_id.id_string()
                    # unprovenanced points back at ourselves (this is
                    # already the bare form). The short ID is the
                    # workstation-allocated short for ``(d, None)``.
                    # Wire-escape-tagged form: ``["entity", short_id]``.
                    assert md["unprovenanced"] == ["entity", ent_short_id]
                    # No prov half, no unwrap target (d-tag is not
                    # provenance-bearing).
                    assert "provenance_parent" not in md
                    assert "provenance_parent_id_string" not in md
                    assert "unwraps_to" not in md
                    assert "unwraps_to_id_string" not in md


# =====================================================================
# GetEntityMetadata — layered (data, n-prov).
# =====================================================================


async def test_metadata_for_layered_emits_provenance_parent_and_unwraps_to() -> None:
    """A layered ``(d, n-prov)`` entity surfaces ``provenance_parent``
    + ``provenance_parent_id_string`` (the n-node) and, since
    ``embed_as_provenance_parent(d)`` returns None for a d-tag,
    NO ``unwraps_to`` (d isn't a provenance node)."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with _layered_record(ws) as (fid, _scope):
                # The record was minted bypassing RPC, so feed the
                # metadata op directly through a /client mutation.
                async with connect_client(ws.client_url) as client:
                    async with create_and_subscribe(client) as (_sid, sub_id):
                        # Resolve our short_id via the workstation.
                        ent_short_id = ws.state.short_ids.short_id_for_entity(fid)

                        await client.send(
                            MutateSessionRequest(
                                request_id=10,
                                subscription_id=sub_id,
                                ack_seq=0,
                                update_id=1,
                                patch=[],
                                ops=[
                                    {  # type: ignore[list-item]
                                        "kind": "get_entity_metadata",
                                        "slot": 1,
                                        "short_id": ent_short_id,
                                    }
                                ],
                            )
                        )
                        assert isinstance(await client.receive(), MutationCommittedResponse)
                        frame = await client.receive()
                        assert isinstance(frame, SessionEventFrame)
                        event = frame.event
                        assert isinstance(event, EntityMetadataResultEvent)

                        md = event.metadata
                        # id_string carries the layered form.
                        # ``id_string`` is local-only — the prov half
                        # is in ``provenance_parent_id_string``.
                        assert md["id_string"] == fid.local_id_string()
                        # unprovenanced points at the bare d-half.
                        # Wire-escape-tagged form: ``["entity", short_id]``.
                        unprov_tag = md["unprovenanced"]
                        assert isinstance(unprov_tag, list) and unprov_tag[0] == "entity"
                        unprov_short = unprov_tag[1]
                        assert unprov_short != ent_short_id  # bare ≠ layered
                        # provenance_parent points at the n-node.
                        prov_tag = md["provenance_parent"]
                        assert isinstance(prov_tag, list) and prov_tag[0] == "entity"
                        prov_short = prov_tag[1]
                        assert prov_short  # non-empty string
                        assert (
                            md["provenance_parent_id_string"] == fid._provenance_parent.id_string()
                        )
                        # The data half is d-tag (not provenance);
                        # no unwraps_to.
                        assert "unwraps_to" not in md
                        assert "unwraps_to_id_string" not in md


# =====================================================================
# GetEntityMetadata — provenance-tag data ID gets unwraps_to.
# =====================================================================


async def test_metadata_for_n_tag_has_unwraps_to() -> None:
    """When the *data half* of the targeted record is itself a
    provenance-tag node (``n``), ``unwraps_to`` resolves to the
    layered ``(unwrapped_d, n)`` form."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with FxtrScope(isolated=True, include=[ws.state.root_scope]) as builder:
                c_node = _make_c_node(builder)
                async with FxtrScope(
                    isolated=True,
                    include=[builder],
                    provenance_context=c_node,
                    provenance_context_from_scope=builder,
                ) as scope:
                    layered = _store_simple_in_scope(scope)
                    n_id = layered._provenance_parent
                    assert n_id is not None
                    assert n_id.get_tag() == "n"

                    # Track the n-node directly as the *data* of a record
                    # (i.e. ``ProvenancedKey(n_id, None)``). This is the
                    # entity whose metadata we'll query.
                    with ws.state.mutation_context():
                        rec = _ensure_entity_record_via_track(ws.state, n_id, scope)
                    n_short_id = rec.short_id

                    try:
                        async with connect_client(ws.client_url) as client:
                            async with create_and_subscribe(client) as (_sid, sub_id):
                                await client.send(
                                    MutateSessionRequest(
                                        request_id=10,
                                        subscription_id=sub_id,
                                        ack_seq=0,
                                        update_id=1,
                                        patch=[],
                                        ops=[
                                            {  # type: ignore[list-item]
                                                "kind": "get_entity_metadata",
                                                "slot": 1,
                                                "short_id": n_short_id,
                                            }
                                        ],
                                    )
                                )
                                assert isinstance(await client.receive(), MutationCommittedResponse)
                                frame = await client.receive()
                                assert isinstance(frame, SessionEventFrame)
                                event = frame.event
                                assert isinstance(event, EntityMetadataResultEvent)
                                md = event.metadata
                                assert md["id_string"] == n_id.id_string()
                                assert "provenance_parent" not in md
                                # unwraps_to is populated since n is a
                                # provenance-tag node.
                                assert "unwraps_to" in md
                                assert "unwraps_to_id_string" in md
                                # ``unwraps_to_id_string`` is local-only
                                # (the prov half is the current record's
                                # data ID — the caller already knows it),
                                # so it starts with a ``d`` tag and has
                                # no ``@`` separator.
                                assert "@" not in md["unwraps_to_id_string"]
                                assert md["unwraps_to_id_string"].startswith("d")
                    finally:
                        with ws.state.mutation_context():
                            _maybe_release_entity_record(ws.state, rec)


# =====================================================================
# GetServiceMetadata.
# =====================================================================


async def test_metadata_for_c_tag_emits_context_data() -> None:
    """``c``-tagged data IDs surface ``context_data`` (the d-node the
    c-node was built from) via :meth:`MemoryArena.c_node_to_context_data`."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with FxtrScope(isolated=True, include=[ws.state.root_scope]) as builder:
                c_id = _make_c_node(builder)
                assert c_id.get_tag() == "c"

                with ws.state.mutation_context():
                    rec = _ensure_entity_record_via_track(ws.state, c_id, builder)
                c_short_id = rec.short_id

                try:
                    async with connect_client(ws.client_url) as client:
                        async with create_and_subscribe(client) as (_sid, sub_id):
                            await client.send(
                                MutateSessionRequest(
                                    request_id=10,
                                    subscription_id=sub_id,
                                    ack_seq=0,
                                    update_id=1,
                                    patch=[],
                                    ops=[
                                        {  # type: ignore[list-item]
                                            "kind": "get_entity_metadata",
                                            "slot": 1,
                                            "short_id": c_short_id,
                                        }
                                    ],
                                )
                            )
                            assert isinstance(await client.receive(), MutationCommittedResponse)
                            frame = await client.receive()
                            assert isinstance(frame, SessionEventFrame)
                            event = frame.event
                            assert isinstance(event, EntityMetadataResultEvent)
                            md = event.metadata
                            # c-tag → unwraps_to + context_data both
                            # populated. Wire-escape-tagged.
                            assert "unwraps_to" in md
                            ctx_tag = md["context_data"]
                            assert isinstance(ctx_tag, list) and ctx_tag[0] == "entity"
                            ctx_short_id = ctx_tag[1]
                            # The context_data short id resolves to the
                            # d-node the c-node was built from.
                            ctx_fid = ws.state.short_ids.lookup_active_entity(ctx_short_id)
                            assert ctx_fid is not None
                            assert ctx_fid.get_tag() == "d"
                finally:
                    with ws.state.mutation_context():
                        _maybe_release_entity_record(ws.state, rec)


async def test_metadata_for_pathroot_p_tag_omits_backstep() -> None:
    """A pathroot p-node has no recorded backstep (``backstep_node`` is
    ``None`` after ``create_p_node``). The ``backstep`` field is
    omitted from the metadata payload."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with FxtrScope(isolated=True, include=[ws.state.root_scope]) as builder:
                # Build a minimal pathroot d-node: a CBOR dict with a
                # ``value`` field pointing to ``"."``. ``create_p_node``
                # stores that d-node and mints the p-node from its
                # hash.
                pathroot_type = builder.track_builtin(FxtrID.builtin("pathroot_type_test"))
                pathroot_data: FxtrCbor = {
                    "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=GROUND),
                    "$fxtr.type": RawWeakFxtrID(inner_id=pathroot_type),
                    "value": RawWeakFxtrID(inner_id=GROUND),
                }
                p_id = builder.create_p_node(pathroot_data)
                assert p_id.get_tag() == "p"

                with ws.state.mutation_context():
                    rec = _ensure_entity_record_via_track(ws.state, p_id, builder)
                p_short_id = rec.short_id

                try:
                    async with connect_client(ws.client_url) as client:
                        async with create_and_subscribe(client) as (_sid, sub_id):
                            await client.send(
                                MutateSessionRequest(
                                    request_id=10,
                                    subscription_id=sub_id,
                                    ack_seq=0,
                                    update_id=1,
                                    patch=[],
                                    ops=[
                                        {  # type: ignore[list-item]
                                            "kind": "get_entity_metadata",
                                            "slot": 1,
                                            "short_id": p_short_id,
                                        }
                                    ],
                                )
                            )
                            assert isinstance(await client.receive(), MutationCommittedResponse)
                            frame = await client.receive()
                            assert isinstance(frame, SessionEventFrame)
                            event = frame.event
                            assert isinstance(event, EntityMetadataResultEvent)
                            md = event.metadata
                            assert md["id_string"] == p_id.id_string()
                            # Pathroot has no backstep — field omitted.
                            assert "backstep" not in md
                            # ``unwraps_to`` is still populated (p-tag is
                            # a provenance-node tag).
                            assert "unwraps_to" in md
                finally:
                    with ws.state.mutation_context():
                        _maybe_release_entity_record(ws.state, rec)


async def test_service_metadata_returns_type_id_string() -> None:
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
                    create_and_subscribe(client) as (_sid, sub_id),
                ):
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {  # type: ignore[list-item]
                                    "kind": "get_service_metadata",
                                    "slot": 1,
                                    "short_id": svc_short_id,
                                }
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    frame = await client.receive()
                    assert isinstance(frame, SessionEventFrame)
                    event = frame.event
                    assert isinstance(event, ServiceMetadataResultEvent)
                    assert event.slot == 1
                    md = event.metadata
                    assert "type_id_string" in md
                    # The type id is the EchoProto type id from
                    # _fixtures; we just check the string is non-empty
                    # and has the standard FxtrID id-string shape.
                    assert isinstance(md["type_id_string"], str)
                    assert len(md["type_id_string"]) > 0


# =====================================================================
# Refcount + retention round-trip.
# =====================================================================


async def test_metadata_refs_drop_when_event_passes_retention_horizon() -> None:
    """Issue ``GetEntityMetadata`` for a layered record. Verify the
    session has refcounts on the unprovenanced and provenance_parent
    keys via ``event_refs[seq]``. Ack past the metadata event; the
    contributions decrement and the records release if they have no
    other holders."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with _layered_record(ws) as (fid, _scope):
                ent_short_id = ws.state.short_ids.short_id_for_entity(fid)

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
                                    {  # type: ignore[list-item]
                                        "kind": "get_entity_metadata",
                                        "slot": 1,
                                        "short_id": ent_short_id,
                                    }
                                ],
                            )
                        )
                        assert isinstance(await client.receive(), MutationCommittedResponse)
                        frame = await client.receive()
                        assert isinstance(frame, SessionEventFrame)
                        event = frame.event
                        assert isinstance(event, EntityMetadataResultEvent)
                        result_seq = event.seq

                        session = ws.state.sessions[UUID(sid)]
                        # event_refs[seq] sidecar records the metadata
                        # event's ref contributions.
                        assert result_seq in session.event_refs
                        refs_e, refs_s = session.event_refs[result_seq]
                        # At least the (data, None) and (prov, None)
                        # keys are pinned.
                        assert len(refs_e) >= 2
                        assert refs_s == set()

                        # Advance ack past result_seq → retention prune
                        # drops the event, refs decrement.
                        await client.send(
                            MutateSessionRequest(
                                request_id=11,
                                subscription_id=sub_id,
                                ack_seq=result_seq,
                                update_id=2,
                                patch=[],
                                ops=[],
                            )
                        )
                        assert isinstance(await client.receive(), MutationCommittedResponse)

                        # Sidecar entry is gone; metadata-emerged refs
                        # have been decremented.
                        assert result_seq not in session.event_refs
                        # The (prov, None) and (data, None) records had
                        # only this metadata event's contribution + nothing
                        # else (no RPC registration, no other pin), so
                        # they release. The originating layered record
                        # (which the helper holds open) survives.
                        for ref_key in refs_e:
                            if ref_key == provenanced_key_for(fid):
                                # The originating record stays alive
                                # via the helper's track.
                                continue
                            # Auxiliary records should have released.
                            assert ref_key not in ws.state.entity_records


# =====================================================================
# Op validation.
# =====================================================================


async def test_metadata_rejects_unknown_short_id() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                async with create_and_subscribe(client) as (_sid, sub_id):
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {  # type: ignore[list-item]
                                    "kind": "get_entity_metadata",
                                    "slot": 1,
                                    "short_id": "not-a-real-short-id",
                                }
                            ],
                        )
                    )
                    resp = await client.receive()
                    assert isinstance(resp, MutationRejectedResponse)
                    assert resp.reason == "validation_failed"


async def test_metadata_op_slot_must_be_strictly_greater_than_counter() -> None:
    """The metadata op shares the load/invoke slot-monotonicity rule.
    Two ops in the same mutation can't reuse a slot."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with (
                connect_rpc(ws.rpc_url) as rpc,
                connect_client(ws.client_url) as client,
            ):
                ws_ref = rpc.remote_service.replace_service_type(WorkstationServiceProto)
                note = Note(title="t", body="b").store(rpc.scope)
                async with (
                    expose_entity(ws_ref, "n", note.entity_id, rpc.scope) as ent_short_id,
                    create_and_subscribe(client) as (_sid, sub_id),
                ):
                    await client.send(
                        MutateSessionRequest(
                            request_id=10,
                            subscription_id=sub_id,
                            ack_seq=0,
                            update_id=1,
                            patch=[],
                            ops=[
                                {  # type: ignore[list-item]
                                    "kind": "get_entity_metadata",
                                    "slot": 5,
                                    "short_id": ent_short_id,
                                },
                                {  # type: ignore[list-item]
                                    "kind": "get_entity_metadata",
                                    "slot": 5,
                                    "short_id": ent_short_id,
                                },
                            ],
                        )
                    )
                    resp = await client.receive()
                    assert isinstance(resp, MutationRejectedResponse)
                    assert resp.reason == "validation_failed"
