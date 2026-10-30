"""Stage 6a integration tests: provenance-aware tracking.

Covers:

- 6a.1 (record keying):
    - Two layered FxtrIDs sharing a data ID but with distinct
      provenance parents create *two* :class:`TrackedEntityRecord`
      instances with distinct short IDs and distinct
      ``tracking_sessions`` sets.
    - Session refcounts isolate per :class:`ProvenancedKey` —
      pinning ``(data, prov_X)`` does not increment the refcount on
      ``(data, prov_Y)`` or ``(data, None)``.
- 6a.2 (per-record scope):
    - Releasing one of two records that share a data subgraph keeps
      the other's record alive in the workstation, with its
      per-record scope still holding the entity.
    - The transient ``load_scope`` opened by ``load_worker`` drains
      cleanly (verified indirectly: the parent record's scope still
      holds the entity after the load completes).
- 6a.1+6a.3 (wire format):
    - Short IDs follow the ``prefix`` / ``prefix@N`` convention:
      first-seen layered registration claims the bare prefix,
      late-arriving bare claims ``@0``.

Helpers (``_make_c_node``, ``_store_simple_in_prov_scope``) mirror
the patterns in v2's ``tests/test_entity_ref_scope/test_provenance_flow.py``
and produce a real layered FxtrID via a c-node provenance context.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator

import anyio
from fxtr2.data.entity_core import FxtrCbor, FxtrID, RawFxtrEntityMarkerID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr_workstation import WorkstationServer
from fxtr_workstation.state import (
    _ensure_entity_record_via_track,
    _incref_session_entity,
    _maybe_release_entity_record,
    provenanced_key_for,
)

DEADLOCK_TIMEOUT = 10.0

GROUND = FxtrID.from_tag_and_payload(".", b"")


def _make_c_node(scope: FxtrScope, ctx_key: str) -> FxtrID:
    """Build a c-node in ``scope``'s arena that unwraps to ``"."``.

    Mirrors ``_make_c_node`` from
    ``v2/tests/test_entity_ref_scope/test_provenance_flow.py``.
    """
    ctx_type = scope.track_builtin(FxtrID.builtin("ctx"))
    context_data: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=GROUND),
        "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
        "value": RawWeakFxtrID(inner_id=GROUND),
        "ctx_key": ctx_key,
    }
    return scope.create_c_node(context_data)


def _store_simple_in_scope(scope: FxtrScope, *, name: str = "n", value: int = 1) -> FxtrID:
    """Store a tiny d-node-shaped entity in ``scope``. If ``scope``
    has a provenance context, returns a layered FxtrID; otherwise
    returns a bare data ID.
    """
    builtin_type = scope.track_builtin(FxtrID.builtin("test_simple_entity_6a"))
    cbor: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=scope.get_context_marker_id()),
        "$fxtr.type": RawWeakFxtrID(inner_id=builtin_type),
        "name": name,
        "value": value,
    }
    return scope.store_cbor(cbor)


@contextlib.asynccontextmanager
async def _two_layered_with_shared_data(
    ws: WorkstationServer,
) -> AsyncGenerator[tuple[FxtrID, FxtrID, FxtrScope, FxtrScope], None]:
    """Open two provenance-context scopes (children of
    ``ws.state.root_scope``) with distinct c-nodes and store the same
    simple entity in each.

    Yields ``(fid_a, fid_b, scope_a, scope_b)`` where ``fid_a`` and
    ``fid_b`` share their data ID but have distinct provenance
    parents (an n-node from each context). Both layered IDs are
    tracked in their respective scopes — pass each to the
    workstation alongside the corresponding scope.

    The caller must use the yielded scopes while still inside the CM
    body — they drain on exit. As an exit-cleanup pass, the helper
    runs :func:`_maybe_release_entity_record` on any record keyed on
    ``fid_a`` / ``fid_b`` so server shutdown sees an empty registry
    even if the test minted records via the yielded fids/scopes.
    """
    async with FxtrScope(
        isolated=True,
        include=[ws.state.root_scope],
        name="6a/builder",
    ) as builder:
        c_a = _make_c_node(builder, ctx_key="trace_a")
        c_b = _make_c_node(builder, ctx_key="trace_b")
        async with (
            FxtrScope(
                isolated=True,
                include=[builder],
                provenance_context=c_a,
                provenance_context_from_scope=builder,
                name="6a/scope_a",
            ) as scope_a,
            FxtrScope(
                isolated=True,
                include=[builder],
                provenance_context=c_b,
                provenance_context_from_scope=builder,
                name="6a/scope_b",
            ) as scope_b,
        ):
            fid_a = _store_simple_in_scope(scope_a)
            fid_b = _store_simple_in_scope(scope_b)
            # Same data half (FxtrID equality ignores prov).
            assert fid_a == fid_b
            # Different prov parents (n-nodes from distinct c-nodes).
            assert fid_a._provenance_parent is not None
            assert fid_b._provenance_parent is not None
            assert fid_a._provenance_parent != fid_b._provenance_parent
            try:
                yield fid_a, fid_b, scope_a, scope_b
            finally:
                # Release any records the test minted via these fids
                # so server shutdown's auto-close sees an empty
                # registry.
                with ws.state.mutation_context():
                    for fid in (fid_a, fid_b):
                        rec = ws.state.entity_records.get(provenanced_key_for(fid))
                        if rec is not None:
                            _maybe_release_entity_record(ws.state, rec)


# =====================================================================
# 6a.1 — distinct records for (data, prov_X) vs (data, prov_Y).
# =====================================================================


async def test_two_provenance_variants_get_distinct_records() -> None:
    """Same data ID, two different provenance parents → two records,
    two short IDs, two distinct keys in ``state.entity_records``."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with _two_layered_with_shared_data(ws) as (
                fid_a,
                fid_b,
                scope_a,
                scope_b,
            ):
                with ws.state.mutation_context():
                    rec_a = _ensure_entity_record_via_track(ws.state, fid_a, scope_a)
                    rec_b = _ensure_entity_record_via_track(ws.state, fid_b, scope_b)

                # Distinct records, distinct short IDs.
                assert rec_a is not rec_b
                assert rec_a.short_id != rec_b.short_id

                key_a = provenanced_key_for(fid_a)
                key_b = provenanced_key_for(fid_b)
                # Same data half, different prov half.
                assert key_a.data_id == key_b.data_id
                assert key_a.prov_parent != key_b.prov_parent
                assert key_a != key_b

                # Both records present in the registry.
                assert ws.state.entity_records[key_a] is rec_a
                assert ws.state.entity_records[key_b] is rec_b
                # The records carry the layered FxtrIDs.
                assert rec_a.fxtr_id._provenance_parent == fid_a._provenance_parent
                assert rec_b.fxtr_id._provenance_parent == fid_b._provenance_parent
                # Per-record scopes are independent objects.
                assert rec_a.scope is not rec_b.scope


async def test_session_refcounts_isolate_per_provenanced_key() -> None:
    """Increfing a session on ``(data, prov_X)`` does not bump the
    refcount on ``(data, prov_Y)`` (or any other variant)."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with _two_layered_with_shared_data(ws) as (
                fid_a,
                fid_b,
                scope_a,
                scope_b,
            ):
                with ws.state.mutation_context():
                    _ensure_entity_record_via_track(ws.state, fid_a, scope_a)
                    _ensure_entity_record_via_track(ws.state, fid_b, scope_b)

                session = ws.state.create_session({})
                key_a = provenanced_key_for(fid_a)
                key_b = provenanced_key_for(fid_b)

                # Incref only key_a.
                with ws.state.mutation_context():
                    _incref_session_entity(ws.state, session, key_a)

                assert session.entity_refcounts[key_a] == 1
                assert key_b not in session.entity_refcounts
                # Tracking sessions reflects only key_a's record.
                assert ws.state.entity_records[key_a].tracking_sessions == {session.session_id}
                assert ws.state.entity_records[key_b].tracking_sessions == set()


# =====================================================================
# 6a.2 — abandon-isolation under shared data.
# =====================================================================


async def test_releasing_one_provenance_variant_keeps_the_other_alive() -> None:
    """Releasing record A (drains its per-record scope) must NOT
    affect record B that shares part of the data subgraph. The v2
    arena's per-scope refcounting keeps the shared data alive while
    B's per-record scope holds a claim."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with _two_layered_with_shared_data(ws) as (
                fid_a,
                fid_b,
                scope_a,
                scope_b,
            ):
                with ws.state.mutation_context():
                    rec_a = _ensure_entity_record_via_track(ws.state, fid_a, scope_a)
                    rec_b = _ensure_entity_record_via_track(ws.state, fid_b, scope_b)

                key_a = provenanced_key_for(fid_a)
                key_b = provenanced_key_for(fid_b)

                # Drop record A's only holder (no registrations, no
                # tracking sessions yet). It releases.
                from fxtr_workstation.state import _maybe_release_entity_record

                with ws.state.mutation_context():
                    _maybe_release_entity_record(ws.state, rec_a)
                assert key_a not in ws.state.entity_records

                # Record B is still alive and its scope still tracks
                # the layered FxtrID (the shared data half is kept
                # alive by the v2 arena's per-scope refcount on B's
                # per-record scope).
                assert key_b in ws.state.entity_records
                assert rec_b.scope.is_tracked(fid_b)


# =====================================================================
# 6a.2 — load worker transient scope cleanup.
# =====================================================================


async def test_load_worker_transient_scope_drains_cleanly() -> None:
    """End-to-end load via the wire — confirms that after the result
    lands the parent record's scope still holds the entity (the
    transient ``load_scope`` opened inside ``load_worker`` drained
    on the worker's ``finally`` block; nothing leaked back to the
    parent)."""
    from fxtr_workstation import expose_entity
    from fxtr_workstation.client_protocol import (
        LoadResultEvent,
        MutateSessionRequest,
        MutationCommittedResponse,
        SessionEventFrame,
    )
    from fxtr_workstation.protocols import WorkstationServiceProto

    from ._fixtures import Note
    from ._session_helpers import create_and_subscribe
    from ._ws_client import connect_client, connect_rpc

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
                                    "kind": "load_entity",
                                    "slot": 1,
                                    "short_id": ent_short_id,
                                }
                            ],
                        )
                    )
                    assert isinstance(await client.receive(), MutationCommittedResponse)
                    frame = await client.receive()
                    assert isinstance(frame, SessionEventFrame)
                    assert isinstance(frame.event, LoadResultEvent)

                    # Parent record's scope still holds the layered FxtrID.
                    parent_key = provenanced_key_for(note.entity_id)
                    parent_record = ws.state.entity_records[parent_key]
                    assert parent_record.scope.is_tracked(note.entity_id)


# =====================================================================
# 6a.3 — wire-format short-id assignment via the registry.
# =====================================================================


async def test_short_id_format_layered_then_bare_via_helper() -> None:
    """End-to-end check of the short-ID rules through
    :func:`_ensure_entity_record_via_track`:

    - ``(data, prov_X)`` first claims the bare prefix.
    - Subsequent ``(data, None)`` claims ``prefix@0``.
    - A second prov variant claims ``prefix@2`` (skips @1).
    """
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with FxtrScope(
                isolated=True,
                include=[ws.state.root_scope],
                name="6a/builder",
            ) as builder:
                c_a = _make_c_node(builder, ctx_key="trace_a")
                c_b = _make_c_node(builder, ctx_key="trace_b")
                async with (
                    FxtrScope(
                        isolated=True,
                        include=[builder],
                        provenance_context=c_a,
                        provenance_context_from_scope=builder,
                    ) as scope_a,
                    FxtrScope(
                        isolated=True,
                        include=[builder],
                        provenance_context=c_b,
                        provenance_context_from_scope=builder,
                    ) as scope_b,
                ):
                    fid_a = _store_simple_in_scope(scope_a)
                    fid_b = _store_simple_in_scope(scope_b)
                    # Verify the data half is shared.
                    assert fid_a == fid_b
                    bare_data = FxtrID(raw_data_id=fid_a._raw_data_id)

                    # First-seen layered → bare prefix.
                    with ws.state.mutation_context():
                        rec_layered_a = _ensure_entity_record_via_track(ws.state, fid_a, scope_a)
                    sid_layered_a = rec_layered_a.short_id
                    assert "@" not in sid_layered_a

                    # Late-arriving (data, None) → @0. The bare data
                    # half is tracked in ``scope_a`` (the n-node store
                    # in scope_a populates the unwraps_to chain to the
                    # corresponding d-node, which becomes a tracked
                    # entry in the same scope's liveset).
                    with ws.state.mutation_context():
                        rec_bare = _ensure_entity_record_via_track(ws.state, bare_data, scope_a)
                    assert rec_bare.short_id == f"{sid_layered_a}@0"

                    # Second prov variant → @2 (skips @1).
                    with ws.state.mutation_context():
                        rec_layered_b = _ensure_entity_record_via_track(ws.state, fid_b, scope_b)
                    assert rec_layered_b.short_id == f"{sid_layered_a}@2"

                    # Cleanup: release the three minted records so
                    # server shutdown sees an empty registry.
                    with ws.state.mutation_context():
                        _maybe_release_entity_record(ws.state, rec_layered_a)
                        _maybe_release_entity_record(ws.state, rec_bare)
                        _maybe_release_entity_record(ws.state, rec_layered_b)
