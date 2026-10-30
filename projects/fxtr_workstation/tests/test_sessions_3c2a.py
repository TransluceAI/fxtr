"""Stage 3c.2a tests: refcount + event-counter primitives.

Focused unit tests for the new ``_incref_session_entity`` /
``_decref_session_entity`` primitives, the explicit-pin composite
helpers, and the session-close drain. Plus a small integration check
that ``apply_mutation`` advances ``event_seq`` alongside
``last_state_seq`` (they coincide today; they'll diverge in 3c.2b
when result events land).

The 3a/3b/3d test suites already exercise the explicit pin path
end-to-end through the wire protocol; these tests focus on the
primitives directly.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from uuid import UUID

import anyio
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr_workstation import WorkstationServer
from fxtr_workstation.client_protocol import (
    CreateSessionRequest,
    MutateSessionRequest,
    MutationCommittedResponse,
    SessionCreatedResponse,
    SessionSnapshotEvent,
    SubscribeSessionRequest,
)
from fxtr_workstation.state import (
    TrackedEntityRecord,
    _add_session_entity_tracker,
    _close_session_drain_trackers,
    _decref_session_entity,
    _ensure_entity_record_via_track,
    _incref_session_entity,
    _maybe_release_entity_record,
    _remove_session_entity_tracker,
    provenanced_key_for,
)

from ._fixtures import Note
from ._ws_client import connect_client

DEADLOCK_TIMEOUT = 10.0


@contextlib.contextmanager
def _track_entity_in_workstation(
    ws: WorkstationServer, note_title: str
) -> Iterator[TrackedEntityRecord]:
    """Mint a ``TrackedEntityRecord`` for a fresh Note in the
    workstation, in the per-record scope model. Context manager —
    on exit, runs :func:`_maybe_release_entity_record` so the
    record releases cleanly if the test left it without holders.

    Stores the Note into a scratch scope (child of ``root_scope``)
    just to get a FxtrID + in-memory data, then uses
    ``_ensure_entity_record_via_track`` to mint a per-record scope
    that holds a tracking claim. Drops the scratch scope before
    yielding — the new record's per-record scope is the only
    survivor while the body runs.

    No RPC ``EntityRegistration`` is created — these tests exercise
    the session-side refcount primitives, so the only thing keeping
    the record alive is whatever the test attaches via
    ``_incref_session_entity``. The exit cleanup is a no-op (short-
    circuits inside :func:`_maybe_release_entity_record`) when the
    test still has holders attached at exit; otherwise it drains
    the record's per-record scope so server shutdown sees an empty
    registry.
    """
    scratch = FxtrScope(isolated=True, include=[ws.state.root_scope]).lowlevel_start_lifespanless(
        parent_leases={ws.state.root_scope: ws.state.root_scope.clone_authorization()}
    )
    try:
        note_ref = Note(title=note_title, body="b").store(scratch)
        fid = note_ref.entity_id
        with ws.state.mutation_context():
            tracked = _ensure_entity_record_via_track(ws.state, fid, scratch)
    finally:
        scratch.force_close()
    try:
        yield tracked
    finally:
        with ws.state.mutation_context():
            _maybe_release_entity_record(ws.state, tracked)


# --- Primitive: attach + detach round trip --------------------------


async def test_attach_then_detach_round_trips_refcount() -> None:
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            with _track_entity_in_workstation(ws, "t1") as tracked:
                key = provenanced_key_for(tracked.fxtr_id)

                session = ws.state.create_session({})

                with ws.state.mutation_context():
                    _incref_session_entity(ws.state, session, key)
                assert session.entity_refcounts[key] == 1
                assert session.session_id in tracked.tracking_sessions

                with ws.state.mutation_context():
                    _decref_session_entity(ws.state, session, key)
                assert key not in session.entity_refcounts
                # Record had no other holder → released.
                assert key not in ws.state.entity_records


# --- Primitive: refcount accumulates from multiple sources ----------


async def test_double_attach_sums_refcount() -> None:
    """Two attaches (e.g. one from an explicit pin + one from a
    retained event ref) sum into refcount=2; releases happen on the
    final 1→0 transition."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            with _track_entity_in_workstation(ws, "t2") as tracked:
                key = provenanced_key_for(tracked.fxtr_id)
                session = ws.state.create_session({})

                with ws.state.mutation_context():
                    _incref_session_entity(ws.state, session, key)
                    _incref_session_entity(ws.state, session, key)
                assert session.entity_refcounts[key] == 2
                # Only the first attach added the tracking_sessions edge.
                assert session.session_id in tracked.tracking_sessions

                with ws.state.mutation_context():
                    _decref_session_entity(ws.state, session, key)
                assert session.entity_refcounts[key] == 1
                # Still tracking until the final detach.
                assert session.session_id in tracked.tracking_sessions
                assert key in ws.state.entity_records

                with ws.state.mutation_context():
                    _decref_session_entity(ws.state, session, key)
                assert key not in session.entity_refcounts
                assert key not in ws.state.entity_records


# --- Composite: explicit pin / unpin --------------------------------


async def test_explicit_pin_unpin_uses_primitives() -> None:
    """``_add_session_entity_tracker`` updates the pin set *and*
    bumps refcount; ``_remove_*`` does the inverse."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            with _track_entity_in_workstation(ws, "t3") as tracked:
                key = provenanced_key_for(tracked.fxtr_id)
                session = ws.state.create_session({})

                with ws.state.mutation_context():
                    _add_session_entity_tracker(ws.state, session, key)
                assert key in session.tracked_entities
                assert session.entity_refcounts[key] == 1
                assert session.session_id in tracked.tracking_sessions

                with ws.state.mutation_context():
                    _remove_session_entity_tracker(ws.state, session, key)
                assert key not in session.tracked_entities
                assert key not in session.entity_refcounts
                assert key not in ws.state.entity_records


async def test_explicit_pin_layered_with_event_ref_contribution() -> None:
    """Explicit pin + a separately-attached "event ref" contribution
    drop independently. After both detach, refcount goes 0 and the
    record releases."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            with _track_entity_in_workstation(ws, "t4") as tracked:
                key = provenanced_key_for(tracked.fxtr_id)
                session = ws.state.create_session({})

                with ws.state.mutation_context():
                    # Explicit pin (refcount 1).
                    _add_session_entity_tracker(ws.state, session, key)
                    # Simulated retained-event ref (refcount 2).
                    _incref_session_entity(ws.state, session, key)
                assert session.entity_refcounts[key] == 2

                # Drop the event-ref contribution: refcount 1; pin still holds.
                with ws.state.mutation_context():
                    _decref_session_entity(ws.state, session, key)
                assert session.entity_refcounts[key] == 1
                assert key in session.tracked_entities
                assert key in ws.state.entity_records

                # Drop the pin: refcount 0; record releases.
                with ws.state.mutation_context():
                    _remove_session_entity_tracker(ws.state, session, key)
                assert key not in session.entity_refcounts
                assert key not in ws.state.entity_records


# --- Close drains mixed contributions -------------------------------


async def test_close_drains_mixed_pin_and_event_refs() -> None:
    """Synthesize an explicit pin on one entity and a retained-event
    ref entry on another; close, and confirm all session bookkeeping
    is cleared and both records are released."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            with (
                _track_entity_in_workstation(ws, "pin-target") as tracked_pin,
                _track_entity_in_workstation(ws, "event-target") as tracked_event,
            ):
                key_pin = provenanced_key_for(tracked_pin.fxtr_id)
                key_event = provenanced_key_for(tracked_event.fxtr_id)

                session = ws.state.create_session({})

                with ws.state.mutation_context():
                    # Explicit pin.
                    _add_session_entity_tracker(ws.state, session, key_pin)
                    # Simulated retained-event ref: refcount bump + event_refs
                    # entry. (3c.2b will produce these via the load-result
                    # delivery path; here we hand-stitch the same shape.)
                    _incref_session_entity(ws.state, session, key_event)
                    session.event_refs[42] = ({key_event}, set())

                # Sanity: only the pinned entity is in tracked_entities;
                # both contribute to refcount.
                assert key_pin in session.tracked_entities
                assert key_event not in session.tracked_entities
                assert session.entity_refcounts[key_pin] == 1
                assert session.entity_refcounts[key_event] == 1
                assert session.session_id in tracked_pin.tracking_sessions
                assert session.session_id in tracked_event.tracking_sessions

                # Close drain.
                with ws.state.mutation_context():
                    _close_session_drain_trackers(ws.state, session)

                # All session-side bookkeeping cleared.
                assert session.entity_refcounts == {}
                assert session.tracked_entities == set()
                assert session.event_refs == {}
                # Both records released (no remaining holders).
                assert key_pin not in ws.state.entity_records
                assert key_event not in ws.state.entity_records


# --- Defensive: detach with no refcount entry is a no-op ------------


async def test_detach_without_refcount_is_noop() -> None:
    """Underflow guard: detaching an entity the session never held
    short-circuits cleanly."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            with _track_entity_in_workstation(ws, "phantom") as tracked:
                key = provenanced_key_for(tracked.fxtr_id)
                session = ws.state.create_session({})

                _decref_session_entity(ws.state, session, key)
                assert key not in session.entity_refcounts
                # Record untouched (record stays in the registry until
                # something clears it; for this test we just confirm
                # the detach didn't crash and didn't mutate session
                # state — the helper's exit cleanup releases the
                # orphan record so server shutdown sees an empty
                # registry).
                assert key in ws.state.entity_records
                assert session.session_id not in tracked.tracking_sessions


# --- event_seq advances with state-update events --------------------


async def test_state_update_advances_event_seq_and_last_state_seq() -> None:
    """Each state-update mutation bumps both ``event_seq`` and
    ``last_state_seq`` to the new seq. They coincide today; they'll
    diverge in 3c.2b when result events bump only ``event_seq``."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_client(ws.client_url) as client:
                await client.send(CreateSessionRequest(request_id=1, metadata={}))
                created = await client.receive()
                assert isinstance(created, SessionCreatedResponse)
                sid = created.session_id

                await client.send(SubscribeSessionRequest(request_id=2, session_id=sid))
                await client.receive()  # ack
                snap = await client.receive()
                assert isinstance(snap, SessionSnapshotEvent)

                await client.send(
                    MutateSessionRequest(
                        request_id=10,
                        subscription_id=2,
                        ack_seq=0,
                        update_id=1,
                        patch=[{"op": "add", "path": "/x", "value": 1}],
                        ops=[],
                    )
                )
                assert isinstance(await client.receive(), MutationCommittedResponse)

                session = ws.state.sessions[UUID(sid)]
                assert session.event_seq == 1
                assert session.last_state_seq == 1

                await client.send(
                    MutateSessionRequest(
                        request_id=11,
                        subscription_id=2,
                        ack_seq=1,
                        update_id=2,
                        patch=[{"op": "replace", "path": "/x", "value": 2}],
                        ops=[],
                    )
                )
                assert isinstance(await client.receive(), MutationCommittedResponse)

                assert session.event_seq == 2
                assert session.last_state_seq == 2
