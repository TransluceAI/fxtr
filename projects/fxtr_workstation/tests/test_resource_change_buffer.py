"""Unit tests for the stage 4a :class:`ResourceChangeBuffer` and
:meth:`WorkstationState.mutation_context` plumbing.

These tests exercise the infrastructure directly: open a mutation
context, manually ``touch`` keys around hand-driven registry
mutations, and verify the dispatched :class:`ResourceChangesEvent`
shape — including the debug-flag projection / suppression filter.

Higher-level end-to-end coverage of the same primitives via the
production mutation entry points lives in
``test_client_protocol.py`` and ``test_snapshot_builders.py``;
these stay as the focused contract for the buffer primitives.
"""

from __future__ import annotations

import anyio
import pytest
from fxtr_workstation import WorkstationServer
from fxtr_workstation.client_protocol import ResourceChangesEvent
from fxtr_workstation.state import ResourceChangeBuffer, ResourceSubscription

DEADLOCK_TIMEOUT = 10.0


async def test_mutation_context_no_touches_emits_nothing() -> None:
    """Opening + closing a mutation context with no touch calls
    must not push any frame to subscribers."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with ws.state.add_client_connection(None) as (record, receive):
                ws.state._register_subscription(
                    record, 1, ResourceSubscription(resource="services")
                )

                with ws.state.mutation_context():
                    pass

                with anyio.move_on_after(0.05) as cs:
                    await receive.receive()
                assert cs.cancelled_caught, "unexpected frame for empty mutation"


async def test_mutation_context_nested_is_a_no_op() -> None:
    """Nested ``mutation_context()`` re-uses the parent buffer; only
    the outermost exit flushes."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            assert ws.state._current_buffer is None
            with ws.state.mutation_context():
                buf_outer = ws.state._current_buffer
                assert isinstance(buf_outer, ResourceChangeBuffer)
                with ws.state.mutation_context():
                    assert ws.state._current_buffer is buf_outer
                # Still the outer buffer after nested exit.
                assert ws.state._current_buffer is buf_outer
            assert ws.state._current_buffer is None


async def test_touch_outside_context_raises() -> None:
    """``touch`` is meaningless without an active buffer — it must
    not silently no-op."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            with pytest.raises(RuntimeError, match="mutation_context"):
                ws.state.touch("services", "s0")


async def test_buffer_dropped_on_exception() -> None:
    """An exception inside the body discards the buffer without
    flushing — partial-state broadcasts are not a thing."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with ws.state.add_client_connection(None) as (record, receive):
                ws.state._register_subscription(
                    record, 1, ResourceSubscription(resource="rpc_connections")
                )

                with pytest.raises(RuntimeError, match="kaboom"):
                    with ws.state.mutation_context():
                        ws.state.touch("rpc_connections", "ignored")
                        raise RuntimeError("kaboom")

                assert ws.state._current_buffer is None

                with anyio.move_on_after(0.05) as cs:
                    await receive.receive()
                assert cs.cancelled_caught, "exception path must not flush a frame"


async def test_buffer_emits_delta_for_added_rpc_connection() -> None:
    """End-to-end-ish: open a mutation_context, manually mutate
    ``rpc_connections``, verify the subscriber receives a
    :class:`ResourceChangesEvent` with the new record in ``added``.
    """
    import datetime
    from uuid import uuid4

    from fxtr2.data.entity_ref_scope import FxtrScope
    from fxtr_workstation.state import RPCConnectionRecord

    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with ws.state.add_client_connection(None) as (record, receive):
                ws.state._register_subscription(
                    record, 7, ResourceSubscription(resource="rpc_connections")
                )

                async with FxtrScope(isolated=True, include=[ws.state.root_scope]) as conn_scope:
                    new_id = uuid4()
                    fake = RPCConnectionRecord(
                        connection_id=new_id,
                        peer_addr="fake:1",
                        connected_at=datetime.datetime.now(datetime.timezone.utc),
                        scope=conn_scope,
                    )
                    with ws.state.mutation_context():
                        # Touch BEFORE the mutation so pre-image is empty.
                        ws.state.touch("rpc_connections", str(new_id))
                        ws.state.rpc_connections[new_id] = fake

                    frame = await receive.receive()
                    assert isinstance(frame, ResourceChangesEvent)
                    assert frame.subscription_id == 7
                    assert len(frame.added) == 1
                    assert frame.added[0]["connection_id"] == str(new_id)
                    assert frame.added[0]["peer_addr"] == "fake:1"
                    assert frame.updated == []
                    assert frame.removed == []

                    # Clean up so the connection's defensive sweep doesn't
                    # fire on a partially-faked record. Touch + remove
                    # under a fresh mutation; we don't assert on the
                    # second frame here — that's covered by other tests.
                    with ws.state.mutation_context():
                        ws.state.touch("rpc_connections", str(new_id))
                        del ws.state.rpc_connections[new_id]
                    # Drain the follow-up frame.
                    with anyio.move_on_after(0.5):
                        await receive.receive()


async def test_buffer_collapses_add_then_remove_to_no_op() -> None:
    """If a record is touched-and-added then touched-and-removed in
    the same buffer, no frame is emitted (pre==None, post==None).
    """
    import datetime
    from uuid import uuid4

    from fxtr2.data.entity_ref_scope import FxtrScope
    from fxtr_workstation.state import RPCConnectionRecord

    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with ws.state.add_client_connection(None) as (record, receive):
                ws.state._register_subscription(
                    record, 1, ResourceSubscription(resource="rpc_connections")
                )

                async with FxtrScope(isolated=True, include=[ws.state.root_scope]) as conn_scope:
                    new_id = uuid4()
                    fake = RPCConnectionRecord(
                        connection_id=new_id,
                        peer_addr="fake:2",
                        connected_at=datetime.datetime.now(datetime.timezone.utc),
                        scope=conn_scope,
                    )
                    with ws.state.mutation_context():
                        ws.state.touch("rpc_connections", str(new_id))
                        ws.state.rpc_connections[new_id] = fake
                        ws.state.touch("rpc_connections", str(new_id))
                        del ws.state.rpc_connections[new_id]

                with anyio.move_on_after(0.05) as cs:
                    await receive.receive()
                assert cs.cancelled_caught, "add+remove in same buffer must collapse"


async def test_initial_snapshot_is_projected_per_subscription_debug_flag() -> None:
    """The initial ``ResourceSnapshotEvent`` sent on subscribe is
    projected against the subscription's ``debug`` flag, just like
    follow-up ``ResourceChangesEvent`` frames."""
    from fxtr2.data.entity_ref_scope import FxtrScope
    from fxtr_workstation.state import _ensure_entity_record_via_track, _maybe_release_entity_record

    from ._fixtures import Note

    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            # Pre-mint an entity record so the snapshot is non-empty.
            scratch = FxtrScope(
                isolated=True, include=[ws.state.root_scope]
            ).lowlevel_start_lifespanless(
                parent_leases={ws.state.root_scope: ws.state.root_scope.clone_authorization()}
            )
            try:
                note_ref = Note(title="t", body="b").store(scratch)
                fid = note_ref.entity_id
                with ws.state.mutation_context():
                    tracked = _ensure_entity_record_via_track(ws.state, fid, scratch)
            finally:
                scratch.force_close()

            from fxtr_workstation.client_protocol import ResourceSnapshotEvent, SubscribeRequest

            from ._ws_client import connect_client

            async with connect_client(ws.client_url) as client:
                await client.send(SubscribeRequest(request_id=1, resource="entities", debug=False))
                from fxtr_workstation.client_protocol import AckResponse

                assert (await client.receive()) == AckResponse(request_id=1)
                snap = await client.receive()
                assert isinstance(snap, ResourceSnapshotEvent)
                (entry,) = snap.data
                # Debug-only fields stripped.
                assert "pinned_by_sessions" not in entry
                assert "registering_connection_ids" not in entry
                # Visible field present.
                assert entry["fxtr_id"] == fid.id_string()

                # Same subscriber but with debug=True.
                await client.send(SubscribeRequest(request_id=2, resource="entities", debug=True))
                assert (await client.receive()) == AckResponse(request_id=2)
                snap = await client.receive()
                assert isinstance(snap, ResourceSnapshotEvent)
                (entry,) = snap.data
                assert entry["pinned_by_sessions"] == []
                assert entry["registering_connection_ids"] == []

            # Cleanup the synthesized record so server shutdown sees
            # an empty registry.
            with ws.state.mutation_context():
                _maybe_release_entity_record(ws.state, tracked)


async def test_debug_subscriber_sees_debug_only_fields() -> None:
    """A subscription with ``debug=True`` receives the full record
    shape; a non-debug subscription gets the same record minus the
    fields in :data:`_DEBUG_ONLY_FIELDS`."""
    import datetime
    from uuid import uuid4

    from fxtr2.data.entity_ref_scope import FxtrScope
    from fxtr_workstation.state import RPCConnectionRecord

    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with ws.state.add_client_connection(None) as (record, receive):
                ws.state._register_subscription(
                    record, 1, ResourceSubscription(resource="rpc_connections", debug=False)
                )
                ws.state._register_subscription(
                    record, 2, ResourceSubscription(resource="rpc_connections", debug=True)
                )

                async with FxtrScope(isolated=True, include=[ws.state.root_scope]) as conn_scope:
                    new_id = uuid4()
                    fake = RPCConnectionRecord(
                        connection_id=new_id,
                        peer_addr="fake:debug",
                        connected_at=datetime.datetime.now(datetime.timezone.utc),
                        scope=conn_scope,
                    )
                    with ws.state.mutation_context():
                        ws.state.touch("rpc_connections", str(new_id))
                        ws.state.rpc_connections[new_id] = fake

                    # Both subscribers get a frame, but with different
                    # record shapes.
                    frames: dict[int, ResourceChangesEvent] = {}
                    for _ in range(2):
                        f = await receive.receive()
                        assert isinstance(f, ResourceChangesEvent)
                        frames[f.subscription_id] = f
                    non_debug = frames[1]
                    debug = frames[2]

                    assert "service_registration_short_ids" not in non_debug.added[0]
                    assert "entity_registration_short_ids" not in non_debug.added[0]
                    # Visible fields preserved.
                    assert non_debug.added[0]["peer_addr"] == "fake:debug"

                    assert debug.added[0]["service_registration_short_ids"] == []
                    assert debug.added[0]["entity_registration_short_ids"] == []
                    assert debug.added[0]["peer_addr"] == "fake:debug"

                    # Cleanup.
                    with ws.state.mutation_context():
                        ws.state.touch("rpc_connections", str(new_id))
                        del ws.state.rpc_connections[new_id]
                    # Drain the two follow-up `removed` frames.
                    with anyio.move_on_after(0.5):
                        await receive.receive()
                    with anyio.move_on_after(0.5):
                        await receive.receive()


async def test_debug_only_field_change_suppressed_for_non_debug() -> None:
    """An ``updated`` whose only diff is a debug-only field reaches
    debug subscribers but is suppressed for non-debug subscribers.

    The mutation under test pins an entity via
    :func:`_incref_session_entity`, which changes
    ``pinned_by_sessions`` (debug-only) on the entities resource but
    nothing visible. The non-debug subscriber should observe no
    delta frame; the debug subscriber should observe one with the
    record in ``updated``.
    """
    from fxtr2.data.entity_ref_scope import FxtrScope
    from fxtr_workstation.state import (
        _ensure_entity_record_via_track,
        _incref_session_entity,
        provenanced_key_for,
    )

    from ._fixtures import Note

    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            # Pre-mint an entity record (no RPC peer needed).
            scratch = FxtrScope(
                isolated=True, include=[ws.state.root_scope]
            ).lowlevel_start_lifespanless(
                parent_leases={ws.state.root_scope: ws.state.root_scope.clone_authorization()}
            )
            try:
                note_ref = Note(title="t", body="b").store(scratch)
                fid = note_ref.entity_id
                with ws.state.mutation_context():
                    _ensure_entity_record_via_track(ws.state, fid, scratch)
            finally:
                scratch.force_close()

            async with ws.state.add_client_connection(None) as (record, receive):
                ws.state._register_subscription(
                    record, 1, ResourceSubscription(resource="entities", debug=False)
                )
                ws.state._register_subscription(
                    record, 2, ResourceSubscription(resource="entities", debug=True)
                )

                # Pin via a session.
                session = ws.state.create_session({})
                with ws.state.mutation_context():
                    _incref_session_entity(ws.state, session, provenanced_key_for(fid))

                # Debug subscriber receives an ``updated`` with the
                # pin reflected; non-debug subscriber receives nothing
                # (the only diff is a debug-only field).
                frame = await receive.receive()
                assert isinstance(frame, ResourceChangesEvent)
                assert frame.subscription_id == 2  # debug sub
                assert len(frame.updated) == 1
                assert frame.updated[0]["pinned_by_sessions"] == [str(session.session_id)]

                # Non-debug subscriber should not have received anything.
                with anyio.move_on_after(0.05) as cs:
                    await receive.receive()
                assert (
                    cs.cancelled_caught
                ), "non-debug subscriber should not see a debug-only field change"


async def test_flush_only_dispatches_to_matching_resource() -> None:
    """A change to ``rpc_connections`` doesn't fan out to ``services``
    subscribers."""
    import datetime
    from uuid import uuid4

    from fxtr2.data.entity_ref_scope import FxtrScope
    from fxtr_workstation.state import RPCConnectionRecord

    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with ws.state.add_client_connection(None) as (record, receive):
                ws.state._register_subscription(
                    record, 1, ResourceSubscription(resource="services")
                )
                ws.state._register_subscription(
                    record, 2, ResourceSubscription(resource="rpc_connections")
                )

                async with FxtrScope(isolated=True, include=[ws.state.root_scope]) as conn_scope:
                    new_id = uuid4()
                    fake = RPCConnectionRecord(
                        connection_id=new_id,
                        peer_addr="fake:3",
                        connected_at=datetime.datetime.now(datetime.timezone.utc),
                        scope=conn_scope,
                    )
                    with ws.state.mutation_context():
                        ws.state.touch("rpc_connections", str(new_id))
                        ws.state.rpc_connections[new_id] = fake

                    frame = await receive.receive()
                    assert isinstance(frame, ResourceChangesEvent)
                    assert frame.subscription_id == 2  # only rpc_connections sub

                    with anyio.move_on_after(0.05) as cs:
                        await receive.receive()
                    assert (
                        cs.cancelled_caught
                    ), "services subscriber should not have received a frame"

                    # Clean up the fake record under a fresh context.
                    with ws.state.mutation_context():
                        ws.state.touch("rpc_connections", str(new_id))
                        del ws.state.rpc_connections[new_id]
                    with anyio.move_on_after(0.5):
                        await receive.receive()
