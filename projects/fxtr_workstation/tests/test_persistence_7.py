"""End-to-end tests for the persistence caps (both modes).

Each test opens one or more :class:`SQLiteFxtrNamespace`s
externally, hands them to :class:`WorkstationServer` via
``persistence_namespaces=[...]``, and exercises the cap through an
RPC peer. The cap path doubles as session coverage — sessions
invoke the same cap via ``InvokeOp``, so the underlying impl is
shared.

Covers both ``mode="no_provenance"`` (provenance dropped on pin,
absent on retrieve) and ``mode="preserve_provenance"`` (pin projects
to a bare prov node via ``_layer_for_storage``; retrieve restores the
layered form via ``embed_as_provenance_parent``).
"""

from __future__ import annotations

import pathlib

import anyio
import pytest
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import EntityRef
from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr_workstation import PersistenceNamespaceConfig, PersistenceProto, WorkstationServer
from fxtr_workstation.protocols import WorkstationServiceProto

from ._fixtures import Note
from ._ws_client import connect_rpc

DEADLOCK_TIMEOUT = 30.0


def _sqlite_url(tmp_path: pathlib.Path, name: str) -> str:
    """Return a plain SQLite path the namespace constructor accepts."""
    return str(tmp_path / f"{name}.sqlite")


async def _resolve_persistence(session, scope, external_name: str):  # type: ignore[no-untyped-def]
    """Helper: resolve the workstation main service, then look up the
    persistence cap by external name and return a typed proxy."""
    ws_ref = session.remote_service.replace_service_type(WorkstationServiceProto)
    workstation = await ws_ref.resolve(scope)
    cap_ref = await workstation.get_service_by_external_name(external_name)
    return await cap_ref.replace_service_type(PersistenceProto).resolve(scope)


async def test_pin_and_retrieve_round_trip(tmp_path: pathlib.Path) -> None:
    """A connected RPC peer pins a Note and retrieves it via the
    persistence cap. The retrieved FxtrID matches the original and
    the entity is loadable via the chain-bridge."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with SQLiteFxtrNamespace(_sqlite_url(tmp_path, "store"), "ns") as ns:
            cfg = PersistenceNamespaceConfig(
                external_name="persistence:default",
                namespace=ns,
            )
            async with WorkstationServer(persistence_namespaces=[cfg]) as ws:
                async with connect_rpc(ws.rpc_url) as session:
                    cap = await _resolve_persistence(session, session.scope, "persistence:default")
                    note = Note(title="hi", body="there").store(session.scope)
                    await cap.pin("note:hi", note.entity_id)

                    # ``list_pins`` reflects the new pin.
                    assert await cap.list_pins() == ["note:hi"]
                    assert await cap.list_pins("note:") == ["note:hi"]
                    assert await cap.list_pins("other:") == []

                    # Retrieve gets us back the same FxtrID and the
                    # entity is loadable.
                    retrieved_id = await cap.retrieve("note:hi")
                    assert isinstance(retrieved_id, FxtrID)
                    assert retrieved_id == note.entity_id
                    loaded = await EntityRef(retrieved_id, Note).load(session.scope)
                    assert loaded == Note(title="hi", body="there")


async def test_unpin_and_try_retrieve(tmp_path: pathlib.Path) -> None:
    """``unpin`` removes the entry; ``try_retrieve`` returns ``None``
    instead of raising when the key is missing."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with SQLiteFxtrNamespace(_sqlite_url(tmp_path, "store"), "ns") as ns:
            cfg = PersistenceNamespaceConfig(external_name="persistence:default", namespace=ns)
            async with WorkstationServer(persistence_namespaces=[cfg]) as ws:
                async with connect_rpc(ws.rpc_url) as session:
                    cap = await _resolve_persistence(session, session.scope, "persistence:default")
                    note = Note(title="x", body="").store(session.scope)
                    await cap.pin("k", note.entity_id)

                    assert await cap.try_retrieve("k") == note.entity_id
                    assert await cap.try_retrieve("missing") is None

                    await cap.unpin("k")
                    assert await cap.list_pins() == []
                    assert await cap.try_retrieve("k") is None


async def test_overwrite_semantics(tmp_path: pathlib.Path) -> None:
    """Default ``overwrite=False`` raises on duplicate; ``overwrite=True``
    replaces the existing pin."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with SQLiteFxtrNamespace(_sqlite_url(tmp_path, "store"), "ns") as ns:
            cfg = PersistenceNamespaceConfig(external_name="persistence:default", namespace=ns)
            async with WorkstationServer(persistence_namespaces=[cfg]) as ws:
                async with connect_rpc(ws.rpc_url) as session:
                    cap = await _resolve_persistence(session, session.scope, "persistence:default")
                    note_a = Note(title="a", body="").store(session.scope)
                    note_b = Note(title="b", body="").store(session.scope)

                    await cap.pin("k", note_a.entity_id)
                    # Default = no overwrite → raises across the wire
                    # as a ValueError-ish dispatch error.
                    with pytest.raises(Exception):
                        await cap.pin("k", note_b.entity_id)
                    # Explicit overwrite=True succeeds and the new
                    # FxtrID lands.
                    await cap.pin("k", note_b.entity_id, overwrite=True)
                    assert await cap.retrieve("k") == note_b.entity_id


async def test_restart_durability(tmp_path: pathlib.Path) -> None:
    """Server A pins; server A shuts down; server B opens the same
    SQLite path and retrieves the pinned FxtrID. The second server
    sees the stored content even though the in-memory state was
    fully discarded between them."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        sqlite_url = _sqlite_url(tmp_path, "store")
        # --- Server A: pin ---
        async with SQLiteFxtrNamespace(sqlite_url, "ns") as ns_a:
            cfg = PersistenceNamespaceConfig(external_name="persistence:default", namespace=ns_a)
            async with WorkstationServer(persistence_namespaces=[cfg]) as ws:
                async with connect_rpc(ws.rpc_url) as session:
                    cap = await _resolve_persistence(session, session.scope, "persistence:default")
                    note = Note(title="restart-me", body="payload").store(session.scope)
                    await cap.pin("note:restart", note.entity_id)
                    pinned_id = note.entity_id

        # --- Server B: retrieve ---
        async with SQLiteFxtrNamespace(sqlite_url, "ns") as ns_b:
            cfg = PersistenceNamespaceConfig(external_name="persistence:default", namespace=ns_b)
            async with WorkstationServer(persistence_namespaces=[cfg]) as ws:
                async with connect_rpc(ws.rpc_url) as session:
                    cap = await _resolve_persistence(session, session.scope, "persistence:default")
                    assert await cap.list_pins() == ["note:restart"]
                    retrieved_id = await cap.retrieve("note:restart")
                    # Content-addressed: same FxtrID across processes.
                    assert retrieved_id == pinned_id
                    loaded = await EntityRef(retrieved_id, Note).load(session.scope)
                    assert loaded == Note(title="restart-me", body="payload")


async def test_two_namespace_independence(tmp_path: pathlib.Path) -> None:
    """Two namespaces, two configs, same key in both with different
    entities. Each cap reads its own namespace; ``list_pins`` is
    isolated."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with (
            SQLiteFxtrNamespace(_sqlite_url(tmp_path, "alpha"), "alpha") as ns_a,
            SQLiteFxtrNamespace(_sqlite_url(tmp_path, "beta"), "beta") as ns_b,
        ):
            cfgs = [
                PersistenceNamespaceConfig(external_name="persistence:alpha", namespace=ns_a),
                PersistenceNamespaceConfig(external_name="persistence:beta", namespace=ns_b),
            ]
            async with WorkstationServer(persistence_namespaces=cfgs) as ws:
                async with connect_rpc(ws.rpc_url) as session:
                    cap_a = await _resolve_persistence(session, session.scope, "persistence:alpha")
                    cap_b = await _resolve_persistence(session, session.scope, "persistence:beta")
                    note_a = Note(title="A", body="").store(session.scope)
                    note_b = Note(title="B", body="").store(session.scope)

                    await cap_a.pin("k", note_a.entity_id)
                    await cap_b.pin("k", note_b.entity_id)

                    # Each cap returns the entity it stored — keys are
                    # local to the namespace.
                    assert await cap_a.retrieve("k") == note_a.entity_id
                    assert await cap_b.retrieve("k") == note_b.entity_id

                    # ``list_pins`` is per-namespace.
                    assert await cap_a.list_pins() == ["k"]
                    assert await cap_b.list_pins() == ["k"]

                    # Distinct entities are content-addressed to
                    # distinct FxtrIDs.
                    assert note_a.entity_id != note_b.entity_id


async def test_preserve_prov_round_trip_lifts_bare_to_u(
    tmp_path: pathlib.Path,
) -> None:
    """In preserve-prov mode, pinning a bare data entity lifts it to
    a ``u``-tagged provenance node. Retrieving gives back the
    layered ``(data, u(data))`` form, with the original FxtrID's
    raw_data_id preserved as the data half."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with SQLiteFxtrNamespace(_sqlite_url(tmp_path, "store"), "ns") as ns:
            cfg = PersistenceNamespaceConfig(
                external_name="persistence:prov",
                namespace=ns,
                mode="preserve_provenance",
            )
            async with WorkstationServer(persistence_namespaces=[cfg]) as ws:
                async with connect_rpc(ws.rpc_url) as session:
                    cap = await _resolve_persistence(session, session.scope, "persistence:prov")
                    note = Note(title="x", body="").store(session.scope)
                    bare_id = note.entity_id
                    assert bare_id._provenance_parent is None
                    assert bare_id.get_tag() == "d"

                    await cap.pin("k", bare_id)
                    layered = await cap.retrieve("k")

                    # Retrieve restored the layered form: data half is
                    # the original bare ID; prov half is u(bare).
                    assert layered.get_tag() == "d"
                    assert layered._raw_data_id == bare_id._raw_data_id
                    assert layered._provenance_parent is not None
                    assert layered._provenance_parent.get_tag() == "u"
                    # The entity is still loadable via the layered ID.
                    loaded = await EntityRef(layered, Note).load(session.scope)
                    assert loaded == Note(title="x", body="")


async def test_preserve_prov_layered_input_round_trips(
    tmp_path: pathlib.Path,
) -> None:
    """Pinning an already-layered FxtrID extracts the prov half and
    pins that. Retrieving restores the same layered ID."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with SQLiteFxtrNamespace(_sqlite_url(tmp_path, "store"), "ns") as ns:
            cfg = PersistenceNamespaceConfig(
                external_name="persistence:prov",
                namespace=ns,
                mode="preserve_provenance",
            )
            async with WorkstationServer(persistence_namespaces=[cfg]) as ws:
                async with connect_rpc(ws.rpc_url) as session:
                    cap = await _resolve_persistence(session, session.scope, "persistence:prov")
                    note = Note(title="layered", body="").store(session.scope)
                    # First pin lifts to u; first retrieve gives layered.
                    await cap.pin("k", note.entity_id)
                    layered_first = await cap.retrieve("k")
                    assert layered_first._provenance_parent is not None

                    # Re-pin the layered FxtrID under a different key.
                    # The cap should extract the prov half (= u(d)) and
                    # pin that. The retrieved layered ID should match.
                    await cap.pin("k2", layered_first)
                    layered_again = await cap.retrieve("k2")
                    assert layered_again == layered_first


async def test_preserve_prov_bare_prov_tag_input_is_u_wrapped(
    tmp_path: pathlib.Path,
) -> None:
    """A bare prov-tag input (no ``_provenance_parent``) is treated
    like any other bare id: wrapped to ``u(node)`` on pin. The branch
    is purely on the layering, never on the tag — so a bare ``u``-tag
    node round-trips as ``(u(d), u(u(d)))``."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with SQLiteFxtrNamespace(_sqlite_url(tmp_path, "store"), "ns") as ns:
            cfg = PersistenceNamespaceConfig(
                external_name="persistence:prov",
                namespace=ns,
                mode="preserve_provenance",
            )
            async with WorkstationServer(persistence_namespaces=[cfg]) as ws:
                async with connect_rpc(ws.rpc_url) as session:
                    cap = await _resolve_persistence(session, session.scope, "persistence:prov")
                    # Round-trip a Note to get a layered (d, u(d))
                    # FxtrID. Extract the bare prov half — that's a
                    # ``u``-tag ID with no ``_provenance_parent``.
                    note = Note(title="x", body="").store(session.scope)
                    await cap.pin("k", note.entity_id)
                    layered = await cap.retrieve("k")
                    bare_prov = layered._provenance_parent
                    assert bare_prov is not None
                    assert bare_prov.get_tag() == "u"
                    assert bare_prov._provenance_parent is None

                    # Pinning the bare prov u(d) wraps it as u(u(d)) and
                    # pins that; retrieve restores the layered form
                    # (data half = u(d), prov half = u(u(d))).
                    await cap.pin("k2", bare_prov)
                    layered2 = await cap.retrieve("k2")
                    assert layered2._raw_data_id == bare_prov._raw_data_id
                    assert layered2._provenance_parent is not None
                    assert layered2._provenance_parent.get_tag() == "u"
                    # The outer wrap is a distinct id from u(d).
                    assert layered2._provenance_parent._raw_data_id != bare_prov._raw_data_id


async def test_preserve_prov_rejects_non_prov_tag_payload_on_retrieve(
    tmp_path: pathlib.Path,
) -> None:
    """Manually pin a bare data-tag FxtrID via the namespace's own
    API (bypassing the cap), then retrieve through the preserve-prov
    cap. The cap rejects: its invariant is that every pin is a
    provenance-tag node."""
    from fxtr2.data.entity_ref_scope import FxtrScope

    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with SQLiteFxtrNamespace(_sqlite_url(tmp_path, "store"), "ns") as ns:
            # Pin a bare d-tagged entity directly via the namespace
            # API (bypassing any cap). Use a scope that includes the
            # namespace's scope so storage / pin both work.
            async with FxtrScope(include=[ns.scope]) as ns_scope:
                bare_note = Note(title="raw", body="").store(ns_scope)
                bare_id = bare_note.entity_id
                assert bare_id.get_tag() == "d"
                assert bare_id._provenance_parent is None
                await ns.pin("raw_d", bare_id, scope=ns_scope)

            cfg = PersistenceNamespaceConfig(
                external_name="persistence:prov",
                namespace=ns,
                mode="preserve_provenance",
            )
            async with WorkstationServer(persistence_namespaces=[cfg]) as ws:
                async with connect_rpc(ws.rpc_url) as session:
                    cap = await _resolve_persistence(session, session.scope, "persistence:prov")
                    with pytest.raises(Exception, match="resolves|provenance node|d'-tag"):
                        await cap.retrieve("raw_d")


async def test_preserve_prov_restart_durability(tmp_path: pathlib.Path) -> None:
    """Server A pins (via preserve-prov cap, lifting to u-node);
    server B opens the same SQLite path and retrieves; the layered
    form round-trips across restart."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        sqlite_url = _sqlite_url(tmp_path, "store")
        # --- Server A: pin ---
        async with SQLiteFxtrNamespace(sqlite_url, "ns") as ns_a:
            cfg = PersistenceNamespaceConfig(
                external_name="persistence:prov",
                namespace=ns_a,
                mode="preserve_provenance",
            )
            async with WorkstationServer(persistence_namespaces=[cfg]) as ws:
                async with connect_rpc(ws.rpc_url) as session:
                    cap = await _resolve_persistence(session, session.scope, "persistence:prov")
                    note = Note(title="durable", body="x").store(session.scope)
                    await cap.pin("note:durable", note.entity_id)

        # --- Server B: retrieve ---
        async with SQLiteFxtrNamespace(sqlite_url, "ns") as ns_b:
            cfg = PersistenceNamespaceConfig(
                external_name="persistence:prov",
                namespace=ns_b,
                mode="preserve_provenance",
            )
            async with WorkstationServer(persistence_namespaces=[cfg]) as ws:
                async with connect_rpc(ws.rpc_url) as session:
                    cap = await _resolve_persistence(session, session.scope, "persistence:prov")
                    layered = await cap.retrieve("note:durable")
                    assert layered._provenance_parent is not None
                    assert layered._provenance_parent.get_tag() == "u"
                    loaded = await EntityRef(layered, Note).load(session.scope)
                    assert loaded == Note(title="durable", body="x")


async def test_duplicate_external_name_rejected_at_construction(
    tmp_path: pathlib.Path,
) -> None:
    """Two configs sharing an external name fail validation at
    :class:`WorkstationServer` construction time, before any
    bring-up work happens."""
    async with SQLiteFxtrNamespace(_sqlite_url(tmp_path, "a"), "a") as ns_a:
        async with SQLiteFxtrNamespace(_sqlite_url(tmp_path, "b"), "b") as ns_b:
            cfgs = [
                PersistenceNamespaceConfig(external_name="persistence:dup", namespace=ns_a),
                PersistenceNamespaceConfig(external_name="persistence:dup", namespace=ns_b),
            ]
            with pytest.raises(ValueError, match="duplicate external name"):
                WorkstationServer(persistence_namespaces=cfgs)


async def test_invalid_mode_rejected_at_construction(
    tmp_path: pathlib.Path,
) -> None:
    """An unknown ``mode`` value fails validation at construction
    time, before the cap worker is started. The static ``Literal``
    annotation should catch this at type-check time, but a caller
    constructing the config from runtime data (e.g. config-file
    deserialization) can still hand us a bogus value — we want a
    clear error, not a deferred startup failure."""
    async with SQLiteFxtrNamespace(_sqlite_url(tmp_path, "store"), "ns") as ns:
        bogus_cfg = PersistenceNamespaceConfig(
            external_name="persistence:bogus",
            namespace=ns,
            mode="bogus_mode",  # type: ignore[arg-type]
        )
        with pytest.raises(ValueError, match="unknown mode"):
            WorkstationServer(persistence_namespaces=[bogus_cfg])
