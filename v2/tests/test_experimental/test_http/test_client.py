"""Tests for the HTTP client (HttpReadableArena, HttpFxtrReadNamespace)."""

from __future__ import annotations

import dataclasses

import httpx
import pytest

from fxtr2.conversion.converters.mashumaro_entities import ChildRef, DataclassEntity
from fxtr2.data.entity_core import FxtrID, RawFxtrEntityMarkerID, RawStrongFxtrID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.experimental.http.asgi_app import create_fxtr_app
from fxtr2.experimental.http.client import (
    HttpFxtrReadNamespace,
    HttpReadableArena,
    _decode_pin_value,
)
from fxtr2.leases.local_lease import UnconditionalLocalLifetime
from tests.test_experimental.test_http.conftest import make_c_node

# =============================================================================
# Test Entity Definitions
# =============================================================================


@dataclasses.dataclass(frozen=True)
class SimpleEntity(DataclassEntity, fxtr_type=FxtrID.builtin("test_httpc_simple")):
    name: str
    value: int


@dataclasses.dataclass(frozen=True)
class EntityWithRef(DataclassEntity, fxtr_type=FxtrID.builtin("test_httpc_with_ref")):
    name: str
    child: ChildRef[SimpleEntity]


def _track_builtins(scope: FxtrScope) -> None:
    scope.track_builtin(FxtrID.builtin("test_httpc_simple"))
    scope.track_builtin(FxtrID.builtin("test_httpc_with_ref"))


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
async def server_and_client():
    """Create a server scope with entities and an httpx client via ASGITransport."""
    async with FxtrScope(name="server") as server_scope:
        _track_builtins(server_scope)

        child_ref = server_scope.store(SimpleEntity(name="child", value=42))
        parent_ref = server_scope.store(EntityWithRef(name="parent", child=child_ref))

        pins = {
            "my/pin": child_ref.entity_id,
            "parent": parent_ref.entity_id,
        }
        app = create_fxtr_app(server_scope, pins)
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, child_ref, parent_ref


# =============================================================================
# HttpReadableArena tests
# =============================================================================


class TestHttpReadableArena:
    @pytest.mark.anyio
    async def test_load_entity(self, server_and_client):
        client, child_ref, parent_ref = server_and_client
        async with UnconditionalLocalLifetime("http_read_arena") as lt:
            arena = HttpReadableArena(client, lt)
            arena._tracked.add(child_ref.entity_id)

            lv = lt.root_lease.clone()
            cbor = await arena.load(lv, child_ref.entity_id)
            assert isinstance(cbor, dict)
            assert cbor["name"] == "child"
            assert cbor["value"] == 42
            arena.close()

    @pytest.mark.anyio
    async def test_load_tracks_children(self, server_and_client):
        client, child_ref, parent_ref = server_and_client
        async with UnconditionalLocalLifetime("http_read_arena") as lt:
            arena = HttpReadableArena(client, lt)
            arena._tracked.add(parent_ref.entity_id)

            lv = lt.root_lease.clone()
            cbor = await arena.load(lv, parent_ref.entity_id)
            assert isinstance(cbor, dict)

            # The child's ID should now be tracked
            assert arena.is_tracked(lt.root_lease.clone(), child_ref.entity_id)
            arena.close()

    @pytest.mark.anyio
    async def test_load_flat(self, server_and_client):
        client, child_ref, parent_ref = server_and_client
        async with UnconditionalLocalLifetime("http_read_arena") as lt:
            arena = HttpReadableArena(client, lt)
            arena._tracked.add(child_ref.entity_id)

            lv = lt.root_lease.clone()
            flat = await arena.load_flat(lv, child_ref.entity_id)
            assert flat.entity_id == child_ref.entity_id
            assert flat.entity_bytes is not None
            assert len(flat.deps) >= 0
            arena.close()

    @pytest.mark.anyio
    async def test_load_populates_precomp(self, server_and_client):
        """Loading an entity populates the precomp dicts from the server response."""
        client, child_ref, parent_ref = server_and_client
        async with UnconditionalLocalLifetime("http_read_arena") as lt:
            arena = HttpReadableArena(client, lt)
            arena._tracked.add(parent_ref.entity_id)

            lv = lt.root_lease.clone()
            await arena.load(lv, parent_ref.entity_id)

            # The child should be tracked (discovered from CBOR walk)
            assert arena.is_tracked(lt.root_lease.clone(), child_ref.entity_id)
            arena.close()

    @pytest.mark.anyio
    async def test_abandon(self, server_and_client):
        client, child_ref, parent_ref = server_and_client
        async with UnconditionalLocalLifetime("http_read_arena") as lt:
            arena = HttpReadableArena(client, lt)
            arena._tracked.add(child_ref.entity_id)

            assert arena.is_tracked(lt.root_lease.clone(), child_ref.entity_id)
            arena.abandon(lt.root_lease.clone(), child_ref.entity_id)
            assert not arena.is_tracked(lt.root_lease.clone(), child_ref.entity_id)
            arena.close()

    @pytest.mark.anyio
    async def test_copy_to_new_arena(self, server_and_client):
        client, child_ref, parent_ref = server_and_client
        async with UnconditionalLocalLifetime("http_read_arena") as lt:
            arena = HttpReadableArena(client, lt)
            arena._tracked.add(child_ref.entity_id)

            new_arena = arena.copy_to_new_arena(lt.root_lease.clone(), [child_ref.entity_id])
            assert new_arena.is_tracked(lt.root_lease.clone(), child_ref.entity_id)
            assert not new_arena.is_tracked(lt.root_lease.clone(), parent_ref.entity_id)
            arena.close()
            new_arena.close()


# =============================================================================
# HttpFxtrReadNamespace tests
# =============================================================================


class TestHttpFxtrReadNamespace:
    @pytest.mark.anyio
    async def test_retrieve_pin(self, server_and_client):
        client, child_ref, parent_ref = server_and_client

        async with HttpFxtrReadNamespace(client) as ns:
            _track_builtins(ns.scope)
            fxtr_id = await ns.retrieve("my/pin")
            assert fxtr_id == child_ref.entity_id

    @pytest.mark.anyio
    async def test_retrieve_typed(self, server_and_client):
        client, child_ref, parent_ref = server_and_client

        async with HttpFxtrReadNamespace(client) as ns:
            _track_builtins(ns.scope)
            ref = await ns.retrieve("my/pin", SimpleEntity)
            assert ref.entity_id == child_ref.entity_id
            loaded = await ref.load()
            assert loaded.name == "child"
            assert loaded.value == 42

    @pytest.mark.anyio
    async def test_try_retrieve_not_found(self, server_and_client):
        client, child_ref, parent_ref = server_and_client

        async with HttpFxtrReadNamespace(client) as ns:
            result = await ns.try_retrieve("nonexistent")
            assert result is None

    @pytest.mark.anyio
    async def test_retrieve_not_found_raises(self, server_and_client):
        client, child_ref, parent_ref = server_and_client

        async with HttpFxtrReadNamespace(client) as ns:
            with pytest.raises(KeyError):
                await ns.retrieve("nonexistent")

    @pytest.mark.anyio
    async def test_list_pins(self, server_and_client):
        client, child_ref, parent_ref = server_and_client

        async with HttpFxtrReadNamespace(client) as ns:
            pins = await ns.list_pins()
            assert "my/pin" in pins
            assert "parent" in pins
            assert pins["my/pin"] == child_ref.entity_id

    @pytest.mark.anyio
    async def test_list_pins_with_prefix(self, server_and_client):
        client, child_ref, parent_ref = server_and_client

        async with HttpFxtrReadNamespace(client) as ns:
            pins = await ns.list_pins(prefix="my/")
            assert "my/pin" in pins
            assert "parent" not in pins


# =============================================================================
# Provenance node tests
# =============================================================================


@pytest.fixture
async def provenance_server():
    """Create a server with provenance nodes (u, c, p) and a d-node referencing them."""
    async with FxtrScope(name="prov_server") as scope:
        _track_builtins(scope)
        arena = scope._main_arena
        dot_id = scope.get_context_marker_id()

        # Base d-node
        d_ref = scope.store(SimpleEntity(name="base", value=1))
        d_id = d_ref.entity_id

        # u-node wrapping the d-node
        u_id = arena.create_u_node(scope.clone_authorization(), d_id)

        # c-node: needs context data entity with a "value" field
        ctx_data = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(dot_id),
            "value": RawStrongFxtrID(d_id),
        }
        c_id = arena.create_c_node(scope.clone_authorization(), ctx_data)

        # p-node: needs pathroot data entity with a "value" field
        pathroot_data = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(dot_id),
            "value": RawStrongFxtrID(d_id),
        }
        p_id = arena.create_p_node(scope.clone_authorization(), pathroot_data)

        # d-node that references the u-node (so we can test case (b))
        holder_type_id = scope.track_builtin(FxtrID.builtin("test_prov_holder"))
        holder_cbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(dot_id),
            "$fxtr.type": RawWeakFxtrID(holder_type_id),
            "ref_to_u": RawWeakFxtrID(u_id),
            "ref_to_c": RawWeakFxtrID(c_id),
            "ref_to_p": RawWeakFxtrID(p_id),
        }
        holder_id = scope.store_cbor(holder_cbor)

        pins = {
            "d_node": d_id,
            "u_node": u_id,
            "c_node": c_id,
            "p_node": p_id,
            "holder": holder_id,
        }
        app = create_fxtr_app(scope, pins)
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, d_id, u_id, c_id, p_id, holder_id


class TestProvenanceViaDirectPin:
    """Case (a): provenance nodes are directly pinned and retrieved."""

    @pytest.mark.anyio
    async def test_u_node_unwrap(self, provenance_server):
        client, d_id, u_id, c_id, p_id, holder_id = provenance_server
        async with HttpFxtrReadNamespace(client) as ns:
            retrieved_u = await ns.retrieve("u_node")
            assert retrieved_u == u_id

            # unwrap_provenance should work via precomp
            unwrapped = ns.scope._main_arena.unwrap_provenance(ns.scope.clone_authorization(), u_id)
            assert unwrapped == d_id

    @pytest.mark.anyio
    async def test_c_node_unwrap_and_ctx(self, provenance_server):
        client, d_id, u_id, c_id, p_id, holder_id = provenance_server
        async with HttpFxtrReadNamespace(client) as ns:
            await ns.retrieve("c_node")

            unwrapped = ns.scope._main_arena.unwrap_provenance(ns.scope.clone_authorization(), c_id)
            assert unwrapped == d_id

            ctx_data_id = ns.scope._main_arena.c_node_to_context_data(
                ns.scope.clone_authorization(), c_id
            )
            assert ctx_data_id is not None

    @pytest.mark.anyio
    async def test_p_node_unwrap_and_pathroot(self, provenance_server):
        client, d_id, u_id, c_id, p_id, holder_id = provenance_server
        async with HttpFxtrReadNamespace(client) as ns:
            await ns.retrieve("p_node")

            unwrapped = ns.scope._main_arena.unwrap_provenance(ns.scope.clone_authorization(), p_id)
            assert unwrapped == d_id

            pathroot_id = ns.scope._main_arena.p_node_to_pathroot(
                ns.scope.clone_authorization(), p_id
            )
            assert pathroot_id is not None


class TestProvenanceViaParentLoad:
    """Case (b): provenance nodes discovered by loading a d-node that references them."""

    @pytest.mark.anyio
    async def test_load_holder_then_unwrap_u(self, provenance_server):
        client, d_id, u_id, c_id, p_id, holder_id = provenance_server
        async with HttpFxtrReadNamespace(client) as ns:
            # Retrieve and load the holder d-node
            holder_fxtr_id = await ns.retrieve("holder")
            assert holder_fxtr_id == holder_id

            cbor = await ns.scope.load_cbor(holder_fxtr_id)
            assert isinstance(cbor, dict)

            # u_id should now be tracked with precomp from the /data/ response
            assert ns.scope.is_tracked(u_id)
            unwrapped = ns.scope._main_arena.unwrap_provenance(ns.scope.clone_authorization(), u_id)
            assert unwrapped == d_id

    @pytest.mark.anyio
    async def test_load_holder_then_unwrap_c(self, provenance_server):
        client, d_id, u_id, c_id, p_id, holder_id = provenance_server
        async with HttpFxtrReadNamespace(client) as ns:
            holder_fxtr_id = await ns.retrieve("holder")
            await ns.scope.load_cbor(holder_fxtr_id)

            assert ns.scope.is_tracked(c_id)
            unwrapped = ns.scope._main_arena.unwrap_provenance(ns.scope.clone_authorization(), c_id)
            assert unwrapped == d_id

    @pytest.mark.anyio
    async def test_load_holder_then_unwrap_p(self, provenance_server):
        client, d_id, u_id, c_id, p_id, holder_id = provenance_server
        async with HttpFxtrReadNamespace(client) as ns:
            holder_fxtr_id = await ns.retrieve("holder")
            await ns.scope.load_cbor(holder_fxtr_id)

            assert ns.scope.is_tracked(p_id)
            unwrapped = ns.scope._main_arena.unwrap_provenance(ns.scope.clone_authorization(), p_id)
            assert unwrapped == d_id


class TestProvenanceViaDeepFetch:
    """Case (c): provenance nodes discovered via _deep_fetch on the scope.

    _deep_fetch returns a new arena containing the node. The node is adopted
    into the scope's store (so a second _deep_fetch would find it cached),
    but the returned arena is the one to use for provenance operations.
    """

    @pytest.mark.anyio
    async def test_deep_fetch_u_node(self, provenance_server):
        """Deep-fetching a u-node should populate precomp so unwrap works."""
        client, d_id, u_id, c_id, p_id, holder_id = provenance_server
        async with HttpFxtrReadNamespace(client) as ns:
            arena = await ns.scope._deep_fetch(u_id)
            assert arena is not None

            # The returned arena should support unwrap_provenance
            unwrapped = arena.unwrap_provenance(ns.scope.clone_authorization(), u_id)
            assert unwrapped == d_id
            arena.close()

    @pytest.mark.anyio
    async def test_deep_fetch_c_node(self, provenance_server):
        """Deep-fetching a c-node should populate precomp so unwrap and ctx_node work."""
        client, d_id, u_id, c_id, p_id, holder_id = provenance_server
        async with HttpFxtrReadNamespace(client) as ns:
            arena = await ns.scope._deep_fetch(c_id)
            assert arena is not None

            unwrapped = arena.unwrap_provenance(ns.scope.clone_authorization(), c_id)
            assert unwrapped == d_id

            ctx_data_id = arena.c_node_to_context_data(ns.scope.clone_authorization(), c_id)
            assert ctx_data_id is not None
            arena.close()

    @pytest.mark.anyio
    async def test_deep_fetch_p_node(self, provenance_server):
        """Deep-fetching a p-node should populate precomp so unwrap and pathroot work."""
        client, d_id, u_id, c_id, p_id, holder_id = provenance_server
        async with HttpFxtrReadNamespace(client) as ns:
            arena = await ns.scope._deep_fetch(p_id)
            assert arena is not None

            unwrapped = arena.unwrap_provenance(ns.scope.clone_authorization(), p_id)
            assert unwrapped == d_id

            pathroot_id = arena.p_node_to_pathroot(ns.scope.clone_authorization(), p_id)
            assert pathroot_id is not None
            arena.close()


# =============================================================================
# Layered provenance round-trip — the provenanced wire
# =============================================================================


@pytest.fixture
async def provenanced_server():
    """A server exposing a genuinely-provenanced (``n``-node) layered pin, served
    with ``provenanced=True`` so ``/pins`` + ``/pin`` carry the layered form."""
    async with FxtrScope(name="prov_builder") as builder:
        c_node = make_c_node(builder)
        async with FxtrScope(
            provenance_context=c_node,
            provenance_context_from_scope=builder,
            name="prov_src",
        ) as scope:
            _track_builtins(scope)
            child_ref = scope.store(SimpleEntity(name="child", value=42))
            parent_ref = scope.store(EntityWithRef(name="parent", child=child_ref))
            layered = parent_ref.entity_id
            assert layered._provenance_parent is not None
            assert layered._provenance_parent.get_tag() == "n"
            n_node = FxtrID(raw_data_id=layered._provenance_parent._raw_data_id)

            app = create_fxtr_app(scope, {"parent": layered}, provenanced=True)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                yield client, layered, n_node


class TestLayeredPinRoundTrip:
    @pytest.mark.anyio
    async def test_retrieve_reconstructs_layered_id(self, provenanced_server):
        client, layered, n_node = provenanced_server
        async with HttpFxtrReadNamespace(client) as ns:
            fxtr_id = await ns.retrieve("parent")
            assert isinstance(fxtr_id, FxtrID)
            assert fxtr_id._raw_data_id == layered._raw_data_id
            assert fxtr_id._provenance_parent == n_node

    @pytest.mark.anyio
    async def test_retrieve_typed_loads_and_keeps_nested_provenance(self, provenanced_server):
        client, layered, n_node = provenanced_server
        async with HttpFxtrReadNamespace(client) as ns:
            ref = await ns.retrieve("parent", EntityWithRef)
            assert ref.entity_id._provenance_parent == n_node
            parent = await ref.load()
            assert parent.name == "parent"
            # The child ref inside the loaded provenanced parent is itself layered.
            assert parent.child.entity_id._provenance_parent is not None

    @pytest.mark.anyio
    async def test_list_pins_reconstructs_layered_id(self, provenanced_server):
        client, layered, n_node = provenanced_server
        async with HttpFxtrReadNamespace(client) as ns:
            pins = await ns.list_pins()
            assert pins["parent"]._raw_data_id == layered._raw_data_id
            assert pins["parent"]._provenance_parent == n_node


def test_decode_pin_value_strict_shape():
    data = FxtrID.from_tag_and_payload("d", b"\x01" * 32)
    prov = FxtrID.from_tag_and_payload("n", b"\x02" * 32)
    assert _decode_pin_value(data.id_string()) == data
    assert _decode_pin_value(data.id_string())._provenance_parent is None
    layered = _decode_pin_value([data.id_string(), prov.id_string()])
    assert layered == data and layered._provenance_parent == prov
    # A 3-element value is neither a bare string nor a [data, prov] pair → error.
    with pytest.raises(ValueError):
        _decode_pin_value(["a", "b", "c"])
    # The '@'-form in a half is rejected (allow_provenance=False).
    with pytest.raises(ValueError):
        _decode_pin_value([f"{data.id_string()}@{prov.id_string()}", prov.id_string()])
