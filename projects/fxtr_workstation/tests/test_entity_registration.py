"""Entity registration flows: copy_entity / get_entity / by-name +
the ``expose_entity`` helper. Mirror of test_service_registration.py
for entities."""

from __future__ import annotations

import anyio
import pytest
from fxtr2.data.entity_ref_scope import EntityRef
from fxtr_workstation import WorkstationServer, expose_entity
from fxtr_workstation.protocols import ReleaseCallbackProto, WorkstationServiceProto

from ._fixtures import EchoImpl, Note
from ._ws_client import connect_rpc

DEADLOCK_TIMEOUT = 10.0


async def _resolve_workstation(session, scope):  # type: ignore[no-untyped-def]
    return await session.remote_service.replace_service_type(WorkstationServiceProto).resolve(scope)


async def _resolve_release_cb(release_cb_ref, scope):  # type: ignore[no-untyped-def]
    return await release_cb_ref.replace_service_type(ReleaseCallbackProto).resolve(scope)


async def test_copy_entity_round_trip_across_connections() -> None:
    """A copies a Note in; B retrieves the FxtrID by short_id and by
    external name and successfully ``load()``s the entity."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_rpc(ws.rpc_url) as session_a, connect_rpc(ws.rpc_url) as session_b:
                ws_a = await _resolve_workstation(session_a, session_a.scope)
                ws_b = await _resolve_workstation(session_b, session_b.scope)

                note = Note(title="hello", body="world")
                note_ref = note.store(session_a.scope)
                short_id, rcb = await ws_a.copy_entity("note-1", note_ref.entity_id)
                assert isinstance(short_id, str)

                # B retrieves by short id, loads the entity through
                # the inheritance bridge from session_b's user_scope.
                retrieved_id = await ws_b.get_entity(short_id)
                loaded = await EntityRef(retrieved_id, Note).load(session_b.scope)
                assert loaded == note

                # B retrieves by external name as well.
                via_name_id = await ws_b.get_entity_by_external_name("note-1")
                assert via_name_id == retrieved_id

                # Clean up explicitly so session_a's close doesn't hit
                # the auto-release-on-drop cycle.
                rcb_obj = await _resolve_release_cb(rcb, session_a.scope)
                await rcb_obj.release()


async def test_expose_entity_helper_round_trip() -> None:
    """``expose_entity`` is the recommended client-side helper."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_rpc(ws.rpc_url) as session_a, connect_rpc(ws.rpc_url) as session_b:
                ws_a_ref = session_a.remote_service.replace_service_type(WorkstationServiceProto)
                ws_b = await _resolve_workstation(session_b, session_b.scope)

                note = Note(title="via", body="helper")
                note_ref = note.store(session_a.scope)

                async with expose_entity(
                    ws_a_ref, "the-note", note_ref.entity_id, session_a.scope
                ) as short_id:
                    # B can retrieve and load.
                    retrieved_id = await ws_b.get_entity(short_id)
                    loaded = await EntityRef(retrieved_id, Note).load(session_b.scope)
                    assert loaded == note

                # After exit: lookups fail.
                with pytest.raises(Exception):
                    await ws_b.get_entity(short_id)
                with pytest.raises(Exception):
                    await ws_b.get_entity_by_external_name("the-note")


async def test_duplicate_entity_external_name_is_rejected() -> None:
    """Entities have their own external-name namespace, with the same
    exclusivity rule as services."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_rpc(ws.rpc_url) as session_a, connect_rpc(ws.rpc_url) as session_b:
                ws_a = await _resolve_workstation(session_a, session_a.scope)
                ws_b = await _resolve_workstation(session_b, session_b.scope)

                note_a = Note(title="a", body="a-body").store(session_a.scope)
                note_b = Note(title="b", body="b-body").store(session_b.scope)

                _short_id_a, rcb_a = await ws_a.copy_entity("shared-name", note_a.entity_id)

                with pytest.raises(Exception):
                    await ws_b.copy_entity("shared-name", note_b.entity_id)

                rcb_a_obj = await _resolve_release_cb(rcb_a, session_a.scope)
                await rcb_a_obj.release()

                # After release, B can claim it.
                _short_id_b, rcb_b = await ws_b.copy_entity("shared-name", note_b.entity_id)
                rcb_b_obj = await _resolve_release_cb(rcb_b, session_b.scope)
                await rcb_b_obj.release()


async def test_entity_short_id_independent_of_service_short_id() -> None:
    """Service and entity short-ID namespaces don't share identity:
    a service named ``s0`` and an entity with a ``d…``-prefixed short
    id never collide. This test just sanity-checks that the entity
    short id format isn't the ``s{N}`` one."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_rpc(ws.rpc_url) as session_a:
                ws_a_ref = session_a.remote_service.replace_service_type(WorkstationServiceProto)
                note = Note(title="t", body="b").store(session_a.scope)

                async with expose_entity(
                    ws_a_ref, "n", note.entity_id, session_a.scope
                ) as short_id:
                    assert isinstance(short_id, str)
                    # ``Note.store_into`` produces a ``d``-tagged ID,
                    # so the short ID should start with ``d``.
                    assert short_id.startswith("d")


async def test_entity_loadable_after_abandon_via_rpc_token_bridge() -> None:
    """On last-release the workstation drains the entity's per-record
    :class:`FxtrScope`. A consumer that grabbed the FxtrID via
    ``get_entity`` can still ``load()`` it because the RPC layer
    bridged the entity's lifetime token at call return time — the
    consumer's connection holds an independent claim that survives
    the workstation's per-record drain."""
    from fxtr_workstation.state import provenanced_key_for

    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_rpc(ws.rpc_url) as session_a, connect_rpc(ws.rpc_url) as session_b:
                ws_a = await _resolve_workstation(session_a, session_a.scope)
                ws_b = await _resolve_workstation(session_b, session_b.scope)

                note = Note(title="persist", body="me").store(session_a.scope)
                short_id, rcb = await ws_a.copy_entity("persist", note.entity_id)
                retrieved_id = await ws_b.get_entity(short_id)

                # Sanity: the entity has a workstation-side record
                # with its own per-record scope.
                key = provenanced_key_for(note.entity_id)
                assert key in ws.state.entity_records

                # A releases. Last registration → workstation drains
                # the entity's per-record scope.
                rcb_obj = await _resolve_release_cb(rcb, session_a.scope)
                await rcb_obj.release()

                assert (
                    key not in ws.state.entity_records
                ), "record should have been dropped on last release"

                # Workstation lookups fail.
                with pytest.raises(Exception):
                    await ws_b.get_entity(short_id)

                # But B can still load — the RPC layer bridged the
                # token across the wire when ``get_entity`` returned.
                loaded = await EntityRef(retrieved_id, Note).load(session_b.scope)
                assert loaded == Note(title="persist", body="me")


# --- Cross-namespace external_name invariants -------------------------


async def test_external_name_namespace_is_shared_across_kinds() -> None:
    """External names live in a single namespace shared between
    services and entities. Registering an entity with a name already
    bound by an active service registration is rejected, and
    vice versa."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_rpc(ws.rpc_url) as session_a:
                workstation = await _resolve_workstation(session_a, session_a.scope)

                # Register a service first.
                echo_ref = await session_a.scope.mount(EchoImpl())
                _svc_short_id, svc_rcb = await workstation.register_service(
                    "contested-name", echo_ref
                )

                # Now attempting to bind the same name to an entity
                # should fail.
                note = Note(title="t", body="b").store(session_a.scope)
                with pytest.raises(Exception, match="already"):
                    await workstation.copy_entity("contested-name", note.entity_id)

                # Release the service. The name is now free for an
                # entity.
                svc_rcb_obj = await _resolve_release_cb(svc_rcb, session_a.scope)
                await svc_rcb_obj.release()

                _ent_short_id, ent_rcb = await workstation.copy_entity(
                    "contested-name", note.entity_id
                )

                # While the entity holds the name, re-registering as a
                # service is rejected.
                with pytest.raises(Exception, match="already"):
                    await workstation.register_service("contested-name", echo_ref)

                ent_rcb_obj = await _resolve_release_cb(ent_rcb, session_a.scope)
                await ent_rcb_obj.release()


async def test_lookup_short_id_by_external_name_returns_service_id() -> None:
    """``lookup_short_id_by_external_name`` returns the service's
    short_id when the name is bound to a service."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_rpc(ws.rpc_url) as session_a:
                workstation = await _resolve_workstation(session_a, session_a.scope)

                echo_ref = await session_a.scope.mount(EchoImpl())
                svc_short_id, rcb = await workstation.register_service("svc-name", echo_ref)
                looked_up = await workstation.lookup_short_id_by_external_name("svc-name")
                assert looked_up == svc_short_id

                rcb_obj = await _resolve_release_cb(rcb, session_a.scope)
                await rcb_obj.release()

                # After release the name is unknown.
                with pytest.raises(Exception, match="unknown"):
                    await workstation.lookup_short_id_by_external_name("svc-name")


async def test_lookup_short_id_by_external_name_returns_entity_id() -> None:
    """``lookup_short_id_by_external_name`` returns the entity's
    short_id when the name is bound to an entity."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_rpc(ws.rpc_url) as session_a:
                workstation = await _resolve_workstation(session_a, session_a.scope)

                note = Note(title="ttl", body="bdy").store(session_a.scope)
                ent_short_id, rcb = await workstation.copy_entity("ent-name", note.entity_id)
                looked_up = await workstation.lookup_short_id_by_external_name("ent-name")
                assert looked_up == ent_short_id

                rcb_obj = await _resolve_release_cb(rcb, session_a.scope)
                await rcb_obj.release()


async def test_lookup_short_id_by_external_name_raises_for_unknown() -> None:
    """Unknown external names raise."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with WorkstationServer() as ws:
            async with connect_rpc(ws.rpc_url) as session_a:
                workstation = await _resolve_workstation(session_a, session_a.scope)
                with pytest.raises(Exception, match="unknown"):
                    await workstation.lookup_short_id_by_external_name("nope")
