"""Tests for fxtr2/data/_legacy_arena.py."""

from __future__ import annotations

from typing import Any

import pytest

from fxtr2.data.abstract_arena import LeaseGrantMismatchError
from fxtr2.data.entity_core import (
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawStrongFxtrID,
    RawWeakFxtrID,
)
from fxtr2.data.memory_arena import MemoryArena, MemoryStore
from fxtr2.leases.lease_types import Lease
from fxtr2.leases.local_lease import UnconditionalLocalLifetime
from tests.conftest import _LeasedArena


def _new_arena(store: MemoryStore) -> _LeasedArena:
    """Test helper: create a new arena wrapped as a ``_LeasedArena``.

    Mints a fresh ``UnconditionalLocalLifetime``, registers its token with the
    store, and calls ``store.new_arena(lease)``. The wrapper owns the driving
    lease (discarded on ``close``); the ULL is kept alive implicitly via the
    lease's owner reference. For tests that need raw access, read
    ``leased._arena`` / ``leased._lease``.
    """
    ull = UnconditionalLocalLifetime("test")
    store.register_token(ull.token, name="test")
    lease = ull.root_lease.take()
    arena = store.new_arena(lease.clone())
    return _LeasedArena(arena, lease)


# =============================================================================
# MemoryStore Basics
# =============================================================================


class TestMemoryStoreBasics:
    """Tests for MemoryStore creation and basic operations."""

    def test_memory_store_creation(self):
        """Can create empty MemoryStore."""
        store = MemoryStore()
        assert store is not None

    def test_memory_store_new_empty_arena(self):
        """Creates a working arena."""
        store = MemoryStore()
        leased = _new_arena(store)
        assert leased is not None
        assert isinstance(leased._arena, MemoryArena)
        leased.close()

    def test_memory_store_multiple_arenas(self):
        """Can create multiple arenas from same store."""
        store = MemoryStore()
        leased1 = _new_arena(store)
        leased2 = _new_arena(store)
        assert leased1 is not leased2
        assert leased1.token is not leased2.token
        leased1.close()
        leased2.close()


# =============================================================================
# register_token
# =============================================================================


class TestRegisterToken:
    """Tests for MemoryStore.register_token error cases.

    Happy-path behaviour is exercised implicitly by every other test via the
    ``memory_arena`` fixture and ``_new_arena`` helper. These tests cover the
    explicit error paths.
    """

    def test_duplicate_registration_raises(self):
        """Registering the same token twice raises RuntimeError."""
        store = MemoryStore()
        ull = UnconditionalLocalLifetime("dup-test")
        store.register_token(ull.token, name="dup-test")
        with pytest.raises(RuntimeError, match="already registered"):
            store.register_token(ull.token, name="dup-test")
        # Teardown: drop the root lease so the token drains cleanly.
        ull.root_lease.discard()

    # NOTE: ``test_unknown_parent_raises`` was removed in part6post2: the memory
    # store no longer tracks token parents at all (FxtrScope owns that via its
    # ancestor cap-bridges). Parent-unknown error paths now live there.


# =============================================================================
# MemoryArena Basic Operations
# =============================================================================


class TestMemoryArenaBasics:
    """Tests for MemoryArena basic operations."""

    def test_arena_is_tracked_empty(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ):
        """New arena tracks the ground node (.)."""
        arena, lease = memory_arena
        # Ground node is tracked by default (via the conftest fixture's track_builtin).
        assert arena.is_tracked(lease.clone(), ground_context)
        # Random builtin is not tracked
        assert not arena.is_tracked(lease.clone(), FxtrID.builtin("not_tracked"))

    def test_arena_track_builtin(self, memory_arena: tuple[MemoryArena[Any], Lease[Any]]):
        """track_builtin() tracks '.' and '!' nodes."""
        arena, lease = memory_arena
        here = FxtrID.from_tag_and_payload(".", b"")
        builtin = FxtrID.builtin("test_type")

        # Track them
        arena.track_builtin(lease.clone(), here)
        arena.track_builtin(lease.clone(), builtin)

        assert arena.is_tracked(lease.clone(), here)
        assert arena.is_tracked(lease.clone(), builtin)

    def test_arena_close(self, memory_store: MemoryStore):
        """Closed arena behavior."""
        leased = _new_arena(memory_store)
        ground = FxtrID.from_tag_and_payload(".", b"")
        leased.track_builtin(ground)
        assert leased.is_tracked(ground)
        # Close arena but retain the lease for a post-close check.
        leased._arena.close()
        # After close, nothing is tracked (is_tracked returns False rather than
        # raising, special-cased for closed arenas).
        assert not leased.is_tracked(ground)
        leased._lease.discard()


# =============================================================================
# Storing Ground Entities (d-nodes)
# =============================================================================


class TestStoreGroundEntities:
    """Tests for storing ground (d-node) entities."""

    def test_store_simple_entity(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ):
        """Store entity with basic fields."""
        arena, lease = memory_arena
        # Track the type builtin first
        type_id = FxtrID.builtin("test")
        arena.track_builtin(lease.clone(), type_id)

        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "$fxtr.type": RawWeakFxtrID(inner_id=type_id),
            "name": "test",
            "value": 42,
        }
        node_id = arena.store(lease.clone(), entity)
        assert node_id is not None
        assert arena.is_tracked(lease.clone(), node_id)

    def test_store_returns_d_node(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ):
        """Stored ground entity returns d-node ID."""
        arena, lease = memory_arena
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        node_id = arena.store(lease.clone(), entity)
        assert node_id.get_tag() == "d"

    def test_store_same_data_same_id(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ):
        """Identical entities get same ID (content-addressed)."""
        arena, lease = memory_arena
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        id1 = arena.store(lease.clone(), entity)
        id2 = arena.store(lease.clone(), entity)
        assert id1 == id2

    def test_store_different_data_different_id(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ):
        """Different entities get different IDs."""
        arena, lease = memory_arena
        entity1: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test1",
        }
        entity2: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test2",
        }
        id1 = arena.store(lease.clone(), entity1)
        id2 = arena.store(lease.clone(), entity2)
        assert id1 != id2

    def test_store_nested_entities(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ):
        """Entity containing refs to other entities."""
        arena, lease = memory_arena
        # First store a child entity
        child: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "child_data": "child",
        }
        child_id = arena.store(lease.clone(), child)

        # Store parent referencing child
        parent: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "child_ref": RawStrongFxtrID(inner_id=child_id),
        }
        parent_id = arena.store(lease.clone(), parent)
        assert arena.is_tracked(lease.clone(), parent_id)
        assert arena.is_tracked(lease.clone(), child_id)

    def test_store_requires_tracked_refs(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ):
        """Cannot store entity with untracked refs."""
        arena, lease = memory_arena
        # Try to store entity referencing untracked node
        untracked_id = FxtrID.builtin("untracked")
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "ref": RawWeakFxtrID(inner_id=untracked_id),
        }
        with pytest.raises(ValueError, match="not tracked"):
            arena.store(lease.clone(), entity)


# =============================================================================
# Loading Ground Entities (d-nodes)
# =============================================================================


class TestLoadGroundEntities:
    """Tests for loading ground (d-node) entities."""

    @pytest.mark.anyio
    async def test_load_simple_entity(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ):
        """Load stored entity returns same data."""
        arena, lease = memory_arena
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "test",
            "value": 42,
        }
        node_id = arena.store(lease.clone(), entity)
        loaded = await arena.load(lease.clone(), node_id)

        assert loaded["name"] == "test"
        assert loaded["value"] == 42

    @pytest.mark.anyio
    async def test_load_tracks_children(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ):
        """Loading entity tracks all its child refs."""
        arena, lease = memory_arena
        # Store a child
        child: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "child_data": "child",
        }
        child_id = arena.store(lease.clone(), child)

        # Store parent
        parent: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "child_ref": RawStrongFxtrID(inner_id=child_id),
        }
        parent_id = arena.store(lease.clone(), parent)

        # Create a new arena sharing the same token to test load tracking.
        new_arena: MemoryArena[Any] = arena._store._new_arena_for_token(arena.token)
        try:
            # Copy just the parent to new arena
            new_arena._track_from_store(parent_id)
            assert new_arena.is_tracked(lease.clone(), parent_id)
            assert not new_arena.is_tracked(lease.clone(), child_id)  # Child not tracked yet

            # Load parent - should track child
            await new_arena.load(lease.clone(), parent_id)
            assert new_arena.is_tracked(lease.clone(), child_id)  # Now tracked
        finally:
            new_arena.close()
            # Note: no separate lease to discard — shares the fixture's lease.

    @pytest.mark.anyio
    async def test_load_untracked_raises(self, memory_arena: tuple[MemoryArena[Any], Lease[Any]]):
        """Loading untracked entity raises."""
        arena, lease = memory_arena
        untracked_id = FxtrID.builtin("untracked")
        with pytest.raises(ValueError, match="not tracked"):
            await arena.load(lease.clone(), untracked_id)

    @pytest.mark.anyio
    async def test_load_roundtrip(
        self,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ):
        """store -> load roundtrips data correctly."""
        arena, lease = memory_arena
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "int_val": 42,
            "str_val": "hello",
            "list_val": [1, 2, 3],
            "dict_val": {"a": 1, "b": 2},
            "bool_val": True,
            "bytes_val": b"bytes",
        }
        node_id = arena.store(lease.clone(), entity)
        loaded = await arena.load(lease.clone(), node_id)

        assert loaded["int_val"] == 42
        assert loaded["str_val"] == "hello"
        assert loaded["list_val"] == [1, 2, 3]
        assert loaded["dict_val"] == {"a": 1, "b": 2}
        assert loaded["bool_val"] is True
        assert loaded["bytes_val"] == b"bytes"


# =============================================================================
# Storing Provenance Entities (n-nodes)
# =============================================================================


class TestStoreProvenanceEntities:
    """Tests for storing provenance (n-node) entities."""

    @pytest.mark.anyio
    async def test_store_n_node(self, _legacy_arena: _LeasedArena):
        """Entity with c-node context creates n-node."""
        # Track builtins first
        ctx_type = FxtrID.builtin("ctx")
        test_type = FxtrID.builtin("test")
        _legacy_arena.track_builtin(ctx_type)
        _legacy_arena.track_builtin(test_type)
        ground = FxtrID.from_tag_and_payload(".", b"")

        # Create context data manually (not using DataclassEntity to avoid needing all the setup)
        context_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
            "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
            "value": RawWeakFxtrID(inner_id=ground),
            "ctx_key": "test_ctx",
            "metadata": {"version": 1},
        }
        c_node_id = _legacy_arena.create_c_node(context_data)

        # Wrap the test type builtin as a u-node so it can be a child of an n-node
        # (n-nodes require all children to be provenance nodes that unwrap)
        u_test_type = _legacy_arena.create_u_node(test_type)

        # Store an entity with c-node context
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=c_node_id),
            "$fxtr.type": RawWeakFxtrID(inner_id=u_test_type),
            "data": "provenance data",
        }
        n_node_id = _legacy_arena.store(entity)
        assert n_node_id.get_tag() == "n"

    @pytest.mark.anyio
    async def test_n_node_unwraps_to_d_node(self, _legacy_arena: _LeasedArena):
        """n-node unwraps to corresponding d-node."""
        # Track builtins first
        ctx_type = FxtrID.builtin("ctx")
        test_type = FxtrID.builtin("test")
        _legacy_arena.track_builtin(ctx_type)
        _legacy_arena.track_builtin(test_type)
        ground = FxtrID.from_tag_and_payload(".", b"")

        # Create context data manually
        context_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
            "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
            "value": RawWeakFxtrID(inner_id=ground),
            "ctx_key": "test_ctx",
            "metadata": {"version": 1},
        }
        c_node_id = _legacy_arena.create_c_node(context_data)

        # Wrap the test type builtin as a u-node
        u_test_type = _legacy_arena.create_u_node(test_type)

        # Store an entity with c-node context
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=c_node_id),
            "$fxtr.type": RawWeakFxtrID(inner_id=u_test_type),
            "data": "provenance data",
        }
        n_node_id = _legacy_arena.store(entity)

        # Unwrap the n-node
        d_node_id = _legacy_arena.unwrap_provenance(n_node_id)
        assert d_node_id is not None
        assert d_node_id.get_tag() == "d"


# =============================================================================
# Context Nodes (c-nodes)
# =============================================================================


class TestContextNodes:
    """Tests for context (c-node) operations."""

    @pytest.mark.anyio
    async def test_create_c_node(self, _legacy_arena: _LeasedArena):
        """create_c_node() creates c-node from context data."""
        ground = FxtrID.from_tag_and_payload(".", b"")
        ctx_type = FxtrID.builtin("ctx")
        _legacy_arena.track_builtin(ctx_type)

        context_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
            "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
            "value": RawWeakFxtrID(inner_id=ground),
            "ctx_key": "test_ctx",
            "metadata": {},
        }
        c_node_id = _legacy_arena.create_c_node(context_data)
        assert c_node_id.get_tag() == "c"
        assert _legacy_arena.is_tracked(c_node_id)

    @pytest.mark.anyio
    async def test_c_node_requires_value_field(self, _legacy_arena: _LeasedArena):
        """context_data must have 'value' field."""
        ground = FxtrID.from_tag_and_payload(".", b"")
        context_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
            "ctx_key": "test_ctx",
            "metadata": {},
            # Missing "value" field
        }
        with pytest.raises(ValueError, match="value"):
            _legacy_arena.create_c_node(context_data)

    @pytest.mark.anyio
    async def test_c_node_unwraps_to_value(self, _legacy_arena: _LeasedArena):
        """c-node unwraps to its 'value' field."""
        ground = FxtrID.from_tag_and_payload(".", b"")
        ctx_type = FxtrID.builtin("ctx")
        _legacy_arena.track_builtin(ctx_type)

        context_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
            "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
            "value": RawWeakFxtrID(inner_id=ground),
            "ctx_key": "test_ctx",
            "metadata": {},
        }
        c_node_id = _legacy_arena.create_c_node(context_data)
        unwrapped = _legacy_arena.unwrap_provenance(c_node_id)
        assert unwrapped == ground

    @pytest.mark.anyio
    async def test_c_node_to_context_data(self, _legacy_arena: _LeasedArena):
        """c_node_to_context_data() returns the d-node."""
        ground = FxtrID.from_tag_and_payload(".", b"")
        ctx_type = FxtrID.builtin("ctx")
        _legacy_arena.track_builtin(ctx_type)

        context_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
            "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
            "value": RawWeakFxtrID(inner_id=ground),
            "ctx_key": "test_ctx",
            "metadata": {},
        }
        c_node_id = _legacy_arena.create_c_node(context_data)
        d_node_id = _legacy_arena.c_node_to_context_data(c_node_id)
        assert d_node_id.get_tag() == "d"
        assert _legacy_arena.is_tracked(d_node_id)


# =============================================================================
# Path Nodes (p-nodes)
# =============================================================================


class TestPathNodes:
    """Tests for path (p-node) operations."""

    @pytest.mark.anyio
    async def test_create_p_node(self, _legacy_arena: _LeasedArena, ground_context: FxtrID):
        """create_p_node() creates p-node from pathroot data."""
        # Track builtin first
        pathroot_type = FxtrID.builtin("pathroot")
        _legacy_arena.track_builtin(pathroot_type)

        # First create a root entity to wrap
        root_entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "root",
        }
        root_id = _legacy_arena.store(root_entity)

        # Create pathroot data
        pathroot_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "$fxtr.type": RawWeakFxtrID(inner_id=pathroot_type),
            "value": RawWeakFxtrID(inner_id=root_id),
            "ctx_key": "test_path",
            "metadata": {},
        }
        p_node_id = _legacy_arena.create_p_node(pathroot_data)
        assert p_node_id.get_tag() == "p"
        assert _legacy_arena.is_tracked(p_node_id)

    @pytest.mark.anyio
    async def test_p_node_unwraps_to_root(
        self, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """p-node with empty path unwraps to root."""
        # Track builtin first
        pathroot_type = FxtrID.builtin("pathroot")
        _legacy_arena.track_builtin(pathroot_type)

        # Create root entity
        root_entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "root",
        }
        root_id = _legacy_arena.store(root_entity)

        # Create pathroot data
        pathroot_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "$fxtr.type": RawWeakFxtrID(inner_id=pathroot_type),
            "value": RawWeakFxtrID(inner_id=root_id),
            "ctx_key": "test_path",
            "metadata": {},
        }
        p_node_id = _legacy_arena.create_p_node(pathroot_data)
        unwrapped = _legacy_arena.unwrap_provenance(p_node_id)
        assert unwrapped == root_id

    @pytest.mark.anyio
    async def test_p_node_to_pathroot(self, _legacy_arena: _LeasedArena, ground_context: FxtrID):
        """p_node_to_pathroot() returns the pathroot d-node."""
        # Track builtin first
        pathroot_type = FxtrID.builtin("pathroot")
        _legacy_arena.track_builtin(pathroot_type)

        # Create root entity
        root_entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "root",
        }
        root_id = _legacy_arena.store(root_entity)

        # Create pathroot data
        pathroot_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "$fxtr.type": RawWeakFxtrID(inner_id=pathroot_type),
            "value": RawWeakFxtrID(inner_id=root_id),
            "ctx_key": "test_path",
            "metadata": {},
        }
        p_node_id = _legacy_arena.create_p_node(pathroot_data)
        pathroot_id = _legacy_arena.p_node_to_pathroot(p_node_id)
        assert pathroot_id.get_tag() == "d"
        assert _legacy_arena.is_tracked(pathroot_id)

    @pytest.mark.anyio
    async def test_load_p_node_creates_children(
        self, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """Loading p-node creates p-node children with extended paths."""
        # Track builtin first
        pathroot_type = FxtrID.builtin("pathroot")
        _legacy_arena.track_builtin(pathroot_type)

        # Create an entity with child refs
        child1: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "child": "1",
        }
        child1_id = _legacy_arena.store(child1)

        child2: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "child": "2",
        }
        child2_id = _legacy_arena.store(child2)

        root_entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "children": [
                RawStrongFxtrID(inner_id=child1_id),
                RawStrongFxtrID(inner_id=child2_id),
            ],
        }
        root_id = _legacy_arena.store(root_entity)

        # Create pathroot
        pathroot_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "$fxtr.type": RawWeakFxtrID(inner_id=pathroot_type),
            "value": RawWeakFxtrID(inner_id=root_id),
            "ctx_key": "test_path",
            "metadata": {},
        }
        p_node_id = _legacy_arena.create_p_node(pathroot_data)

        # Load p-node - should create p-node children
        loaded = await _legacy_arena.load(p_node_id)
        assert "children" in loaded
        # Children should be p-nodes
        for child_ref in loaded["children"]:
            assert isinstance(child_ref, (RawStrongFxtrID, RawWeakFxtrID))
            assert child_ref.inner_id.get_tag() == "p"


# =============================================================================
# Untracked Nodes (u-nodes)
# =============================================================================


class TestUntrackedNodes:
    """Tests for untracked (u-node) operations."""

    def test_create_u_node(self, _legacy_arena: _LeasedArena, ground_context: FxtrID):
        """create_u_node() creates u-node wrapping data node."""
        # Store a data node
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        d_node_id = _legacy_arena.store(entity)

        # Create u-node
        u_node_id = _legacy_arena.create_u_node(d_node_id)
        assert u_node_id.get_tag() == "u"
        assert _legacy_arena.is_tracked(u_node_id)

    def test_u_node_unwraps(self, _legacy_arena: _LeasedArena, ground_context: FxtrID):
        """u-node unwraps to wrapped node."""
        # Store a data node
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        d_node_id = _legacy_arena.store(entity)

        # Create u-node
        u_node_id = _legacy_arena.create_u_node(d_node_id)
        unwrapped = _legacy_arena.unwrap_provenance(u_node_id)
        assert unwrapped == d_node_id

    @pytest.mark.anyio
    async def test_load_u_node(self, _legacy_arena: _LeasedArena, ground_context: FxtrID):
        """Loading u-node wraps children as u-nodes."""
        # Create entity with child ref
        child: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "child": "data",
        }
        child_id = _legacy_arena.store(child)

        root_entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "child_ref": RawStrongFxtrID(inner_id=child_id),
        }
        root_id = _legacy_arena.store(root_entity)

        # Create u-node
        u_node_id = _legacy_arena.create_u_node(root_id)

        # Load u-node
        loaded = await _legacy_arena.load(u_node_id)
        child_ref = loaded["child_ref"]
        assert isinstance(child_ref, (RawStrongFxtrID, RawWeakFxtrID))
        assert child_ref.inner_id.get_tag() == "u"


# =============================================================================
# Unwrapping Provenance
# =============================================================================


class TestUnwrapProvenance:
    """Tests for unwrap_provenance operations."""

    def test_unwrap_d_node_returns_none(self, _legacy_arena: _LeasedArena, ground_context: FxtrID):
        """Ground d-nodes return None on unwrap."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        d_node_id = _legacy_arena.store(entity)
        assert _legacy_arena.unwrap_provenance(d_node_id) is None

    def test_unwrap_here_node_returns_none(
        self, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """'.' returns None on unwrap."""
        assert _legacy_arena.unwrap_provenance(ground_context) is None

    def test_unwrap_builtin_returns_none(self, _legacy_arena: _LeasedArena):
        """'!' builtin returns None on unwrap."""
        builtin = FxtrID.builtin("test")
        _legacy_arena.track_builtin(builtin)
        assert _legacy_arena.unwrap_provenance(builtin) is None


# =============================================================================
# Arena Abandonment
# =============================================================================


class TestArenaAbandonment:
    """Tests for arena abandon operations."""

    def test_abandon_removes_from_tracked(
        self, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """Abandoned node is no longer tracked."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        node_id = _legacy_arena.store(entity)
        assert _legacy_arena.is_tracked(node_id)

        _legacy_arena.abandon(node_id)
        assert not _legacy_arena.is_tracked(node_id)

    def test_abandon_untracked_raises(self, _legacy_arena: _LeasedArena):
        """Cannot abandon untracked node."""
        untracked = FxtrID.builtin("not_tracked")
        with pytest.raises(ValueError, match="not tracked"):
            _legacy_arena.abandon(untracked)


# =============================================================================
# Arena Copying
# =============================================================================


class TestArenaCopying:
    """Tests for arena copying operations."""

    def test_copy_to_new_arena(self, _legacy_arena: _LeasedArena, ground_context: FxtrID):
        """copy_to_new_arena() creates independent arena (same token)."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        node_id = _legacy_arena.store(entity)

        new_arena = _legacy_arena.copy_to_new_arena([node_id])
        try:
            assert new_arena.is_tracked(_legacy_arena._lease.clone(), node_id)
            assert new_arena is not _legacy_arena._arena
        finally:
            new_arena.close()

    def test_copied_arena_independent(self, _legacy_arena: _LeasedArena, ground_context: FxtrID):
        """Changes to original don't affect copy."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        node_id = _legacy_arena.store(entity)

        new_arena = _legacy_arena.copy_to_new_arena([node_id])
        try:
            # Abandon from original
            _legacy_arena.abandon(node_id)
            assert not _legacy_arena.is_tracked(node_id)
            # Copy still has it
            assert new_arena.is_tracked(_legacy_arena._lease.clone(), node_id)
        finally:
            new_arena.close()

    @pytest.mark.anyio
    async def test_copy_from_arena(self, memory_store: MemoryStore, ground_context: FxtrID):
        """copy_from_arena() imports entities from another arena."""
        arena1 = _new_arena(memory_store)
        arena1.track_builtin(ground_context)
        arena2 = _new_arena(memory_store)
        arena2.track_builtin(ground_context)
        try:
            entity: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                "data": "test",
            }
            node_id = arena1.store(entity)
            assert arena1.is_tracked(node_id)
            assert not arena2.is_tracked(node_id)

            await arena2.copy_from_arena(node_id, arena1)
            assert arena2.is_tracked(node_id)
        finally:
            arena1.close()
            arena2.close()


# =============================================================================
# Track From Memory Arena
# =============================================================================


class TestTrackFromMemoryArena:
    """Tests for MemoryStore.track_from_memory_arena (sync DAG walk)."""

    def test_basic_track(self, memory_store: MemoryStore, ground_context: FxtrID):
        """Basic entity can be tracked from one arena to another."""
        arena1 = _new_arena(memory_store)
        arena2 = _new_arena(memory_store)
        arena1.track_builtin(ground_context)
        arena2.track_builtin(ground_context)
        try:
            entity: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                "data": "test",
            }
            node_id = arena1.store(entity)
            assert arena1.is_tracked(node_id)
            assert not arena2.is_tracked(node_id)

            arena2.track_from_memory_arena(node_id, arena1)
            assert arena2.is_tracked(node_id)
        finally:
            arena1.close()
            arena2.close()

    def test_track_with_deps(self, memory_store: MemoryStore, ground_context: FxtrID):
        """Tracking an entity also claims its dependencies."""
        arena1 = _new_arena(memory_store)
        arena2 = _new_arena(memory_store)
        arena1.track_builtin(ground_context)
        arena2.track_builtin(ground_context)
        try:
            # Store a child entity, then a parent that references it.
            child: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                "child_data": "hello",
            }
            child_id = arena1.store(child)

            parent: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                "ref": RawStrongFxtrID(inner_id=child_id),
            }
            parent_id = arena1.store(parent)

            # Track parent into arena2 — should also claim child.
            arena2.track_from_memory_arena(parent_id, arena1)
            assert arena2.is_tracked(parent_id)

            # Child should be owned by arena2's token now.
            child_node = memory_store._entity_states[child_id]
            assert arena2.token in child_node.owned_tokens
        finally:
            arena1.close()
            arena2.close()

    def test_track_survives_source_token_invalidation(
        self, memory_store: MemoryStore, ground_context: FxtrID
    ):
        """After tracking, entity survives invalidation of the source token."""
        arena1 = _new_arena(memory_store)
        arena2 = _new_arena(memory_store)
        arena1.track_builtin(ground_context)
        arena2.track_builtin(ground_context)
        try:
            entity: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                "data": "persist",
            }
            node_id = arena1.store(entity)
            arena2.track_from_memory_arena(node_id, arena1)

            # Close source arena and discard its lease (equivalent to old
            # invalidate_token); the ULL ends and runs _on_token_end cleanup.
            arena1.close()

            # Node should still exist (claimed by arena2's token).
            assert node_id in memory_store._entity_states
            assert arena2.is_tracked(node_id)
        finally:
            arena2.close()

    def test_track_already_claimed_is_noop(self, memory_store: MemoryStore, ground_context: FxtrID):
        """Tracking a node that's already claimed by the target token is a no-op."""
        # Two arenas sharing the same token (same grant).
        arena1 = _new_arena(memory_store)
        arena2_raw: MemoryArena[Any] = memory_store._new_arena_for_token(arena1._arena.token)
        arena2 = _LeasedArena(arena2_raw, arena1._lease.clone())
        arena1.track_builtin(ground_context)
        # arena2 already sees the ground builtin (same liveset-token).
        try:
            entity: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                "data": "shared",
            }
            node_id = arena1.store(entity)
            # Same token — already has case 3 access.
            arena2.track_from_memory_arena(node_id, arena1)
            assert arena2.is_tracked(node_id)
        finally:
            arena2_raw.close()
            arena2._lease.discard()
            arena1.close()

    @pytest.mark.anyio
    async def test_track_adopts_dataless_node_via_claim_holding_source(
        self, memory_store: MemoryStore, ground_context: FxtrID
    ):
        """A sync walk must adopt a data-less node through the *claim* its
        source token holds, not just through entries whose source token is
        itself among ``dest_inheritance_bridges``.

        Shape (mirrors the workstation guest incident): token ``holder``
        inherits entity ``e`` from a *foreign* arena (data never loaded —
        ``e`` is a data-less d-node whose only inheritance entry sources at
        the foreign arena's token ``Y``). ``holder`` stores a bundle
        referencing ``e``. Token ``dest`` (think: per-call scope derived
        from ``holder``) tracks the bundle from ``holder`` with
        ``dest_inheritance_bridges={holder: ...}`` — exactly what the
        marker-inherit path passes.

        The walk reaches ``e``; ``Y`` is not in ``dest``'s bridges, so the
        old strategy (2) missed and strategy (3) demanded in-memory data →
        ``RuntimeError: ... no in-memory data``. But the transfer is
        structurally justified without data or I/O: ``holder`` has a claim
        on ``e`` and ``dest`` keeps ``holder`` alive through its bridge, so
        ``e`` can be adopted as a same-store inheritance entry targeting
        ``holder`` (later loads recurse through ``holder``'s claim).
        """
        from fxtr2.leases.lease_bridges import IndependentLocalBridge

        # Foreign side: a separate store holding e's bytes.
        foreign_store = MemoryStore()
        foreign = _new_arena(foreign_store)
        foreign.track_builtin(ground_context)
        e_id = foreign.store(
            {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                "payload": "never loaded into the consumer store",
            }
        )

        holder = _new_arena(memory_store)
        holder.track_builtin(ground_context)
        dest = _new_arena(memory_store)
        dest.track_builtin(ground_context)
        dest_bridge = IndependentLocalBridge(lease=holder._lease.clone())
        try:
            # holder inherits e lazily from the foreign arena; its data
            # stays unloaded in memory_store.
            holder._arena.lazily_attach_from_arena(
                holder._lease.clone(),
                e_id,
                foreign._arena,
                IndependentLocalBridge(lease=foreign._lease.clone()),
            )
            e_node = memory_store._entity_states[e_id]
            assert e_node.data is None
            assert holder.token in e_node.inheritance_bridges

            bundle_id = holder.store(
                {
                    "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                    "ref": RawStrongFxtrID(inner_id=e_id),
                }
            )

            # The sync walk from holder into dest. Pre-fix this raised
            # RuntimeError("... has no in-memory data ...") when it reached e.
            dest._arena.track_from_memory_arena(
                dest._lease.clone(),
                holder._arena,
                holder._lease.clone(),
                bundle_id,
                dest_inheritance_bridges={holder.token: dest_bridge},
            )

            # The adoption is lazy: holder (the walk source) is dest-bridged
            # and holds the claim on the bundle, so the walk stops right at
            # the root — dest reaches the bundle through holder, and e is
            # untouched until a load walks into it.
            assert e_node.data is None
            bundle_node = memory_store._entity_states[bundle_id]
            assert (
                dest.token in bundle_node.owned_tokens
                or dest.token in bundle_node.inheritance_bridges
            )

            # Loading the bundle from dest cascades adoption of e...
            flat_bundle = await dest.load_flat(bundle_id)
            assert flat_bundle.entity_id == bundle_id

            # ...and e is then loadable from dest too (async, through
            # holder's foreign entry), materializing the bytes.
            flat = await memory_store.bare_load_flat(dest._lease.clone(), e_id)
            assert flat.entity_id == e_id
            assert flat.entity_bytes is not None
        finally:
            dest_bridge.discard_at_rest()
            dest.close()
            holder.close()
            foreign.close()
            foreign_store.close()

    @pytest.mark.anyio
    async def test_track_adopts_dataless_node_via_non_source_claim_holder(
        self, memory_store: MemoryStore, ground_context: FxtrID
    ):
        """Adoption must consider *any* dest-bridged token holding a claim on
        the node — not only the walk's source token.

        Shape (distinguishes the general rule from a narrower
        "walk-source-only" rule): tokens ``a`` and ``b`` independently
        inherit ``e`` from the same foreign arena. ``b`` stores a bundle
        referencing ``e``. ``c`` tracks the bundle *from b* but its
        ``dest_inheritance_bridges`` only contain ``a`` (think: c is a child
        of a but not of b — moving data out of a temporary sibling scope).

        ``e``'s entries all source at the foreign token (not bridged into
        c), so the old strategy (2) misses; ``b`` is the walk source but is
        not in c's bridges, so a walk-source-only adoption rule would miss
        too. The structurally-justified path is ``a``: it holds a claim on
        ``e`` and c's bridge keeps ``a`` alive, so ``e`` is adoptable as a
        same-store entry targeting ``a``.
        """
        from fxtr2.leases.lease_bridges import IndependentLocalBridge

        foreign_store = MemoryStore()
        foreign = _new_arena(foreign_store)
        foreign.track_builtin(ground_context)
        e_id = foreign.store(
            {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                "payload": "never loaded into the consumer store",
            }
        )

        a = _new_arena(memory_store)
        a.track_builtin(ground_context)
        b = _new_arena(memory_store)
        b.track_builtin(ground_context)
        c = _new_arena(memory_store)
        c.track_builtin(ground_context)
        c_bridge_to_a = IndependentLocalBridge(lease=a._lease.clone())
        try:
            # a and b each lazily inherit e from the foreign arena.
            for consumer in (a, b):
                consumer._arena.lazily_attach_from_arena(
                    consumer._lease.clone(),
                    e_id,
                    foreign._arena,
                    IndependentLocalBridge(lease=foreign._lease.clone()),
                )
            e_node = memory_store._entity_states[e_id]
            assert e_node.data is None
            assert a.token in e_node.inheritance_bridges
            assert b.token in e_node.inheritance_bridges

            bundle_id = b.store(
                {
                    "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                    "ref": RawStrongFxtrID(inner_id=e_id),
                }
            )

            # Walk from b into c, with only a bridged on the dest side.
            c._arena.track_from_memory_arena(
                c._lease.clone(),
                b._arena,
                b._lease.clone(),
                bundle_id,
                dest_inheritance_bridges={a.token: c_bridge_to_a},
            )

            assert e_node.data is None
            c_entry = e_node.inheritance_bridges.get(c.token)
            assert c_entry is not None
            assert c_entry.source_token() is a.token

            flat = await memory_store.bare_load_flat(c._lease.clone(), e_id)
            assert flat.entity_id == e_id
            assert flat.entity_bytes is not None
        finally:
            c_bridge_to_a.discard_at_rest()
            c.close()
            b.close()
            a.close()
            foreign.close()
            foreign_store.close()

    def test_track_child_parent_tokens(self, memory_store: MemoryStore, ground_context: FxtrID):
        """Tracking from a child-token arena into the parent-token arena.

        NOTE (part6post2): The "child of T1" framing is historical; the memory store
        no longer tracks token parents. Step C of the simplification plan will
        rewrite this test in terms of explicit dest-side bridges.
        """
        parent_arena = _new_arena(memory_store)
        child_arena = _new_arena(memory_store)
        parent_arena.track_builtin(ground_context)
        child_arena.track_builtin(ground_context)
        try:
            entity: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                "data": "from child",
            }
            node_id = child_arena.store(entity)

            # Track from child arena to parent arena — extends claim to parent token.
            parent_arena.track_from_memory_arena(node_id, child_arena)
            assert parent_arena.is_tracked(node_id)

            # Should survive child token ending.
            child_arena.close()
            assert node_id in memory_store._entity_states
        finally:
            parent_arena.close()

    def test_track_error_on_external_only_node(self, memory_store: MemoryStore):
        """Errors when hitting a node with no in-memory data and no dest-adoptable bridge."""
        # Manually fabricate a node with no data and only token1 in owned_tokens,
        # bypassing the public attach API.
        arena1 = _new_arena(memory_store)
        arena2 = _new_arena(memory_store)
        try:
            from fxtr2.data.memory_arena import MemoryStoreNode

            node_id = FxtrID.from_tag_and_payload("d", b"fake_external")
            token1 = arena1.token
            token2 = arena2.token
            node = MemoryStoreNode(
                my_id=node_id,
                data=None,
                unwraps_to=None,
                inheritance_bridges={},
                owned_tokens={token1},
                wrapped_by=set(),
            )
            memory_store._entity_states[node_id] = node
            memory_store._token_data[token1].node_associations.add(node_id)

            # token2 has no access to the node and no dest-adoptable bridge.
            arena1._arena._liveset().add(node_id)
            with pytest.raises(RuntimeError, match="would require async load"):
                memory_store.bare_track_from_memory_arena(
                    arena2._lease.clone(),
                    arena1._lease.clone(),
                    node_id,
                    dest_inheritance_bridges={},
                )
        finally:
            arena1.close()
            arena2.close()

    def test_track_different_store_raises(self):
        """Raises when source and target are in different stores."""
        store1 = MemoryStore()
        store2 = MemoryStore()
        arena1 = _new_arena(store1)
        arena2 = _new_arena(store2)
        try:
            # The arena-level method checks same-store first (before validating
            # leases on either side) and raises ValueError.
            with pytest.raises(ValueError, match="same-store"):
                arena2.track_from_memory_arena(FxtrID.from_tag_and_payload("d", b"x"), arena1)
        finally:
            arena1.close()
            arena2.close()
            store1.close()
            store2.close()

    def test_track_untracked_source_raises(self, memory_store: MemoryStore, ground_context: FxtrID):
        """Raises when source arena doesn't track the entity (liveset check)."""
        arena1 = _new_arena(memory_store)
        arena2 = _new_arena(memory_store)
        arena1.track_builtin(ground_context)
        arena2.track_builtin(ground_context)
        try:
            entity: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                "data": "test",
            }
            node_id = arena1.store(entity)
            arena1.abandon(node_id)

            # Even though the store still has arena1's token claiming the node
            # (abandon only modifies the arena's liveset), the arena-level
            # ``track_from_memory_arena`` surfaces the user error that the
            # source arena doesn't know about the node.
            with pytest.raises(ValueError, match="not tracked in the source arena"):
                arena2.track_from_memory_arena(node_id, arena1)
        finally:
            arena1.close()
            arena2.close()


# =============================================================================
# Error Cases
# =============================================================================


class TestErrorCases:
    """Tests for error conditions."""

    def test_store_non_dict_raises(self, _legacy_arena: _LeasedArena):
        """Storing non-dict raises ValueError."""
        with pytest.raises((ValueError, AssertionError)):
            _legacy_arena.store([1, 2, 3])  # type: ignore

    def test_store_missing_ctx_raises(self, _legacy_arena: _LeasedArena):
        """Missing $fxtr.ctx raises ValueError."""
        with pytest.raises((ValueError, AssertionError)):
            _legacy_arena.store({"data": "test"})

    @pytest.mark.anyio
    async def test_load_contentless_raises(
        self, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """Loading contentless node (., !) raises."""
        with pytest.raises(ValueError, match="contentless"):
            await _legacy_arena.load(ground_context)

        builtin = FxtrID.builtin("test")
        _legacy_arena.track_builtin(builtin)
        with pytest.raises(ValueError, match="contentless"):
            await _legacy_arena.load(builtin)


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases."""

    @pytest.mark.anyio
    async def test_entity_with_multiple_refs_to_same_child(
        self, memory_store: MemoryStore, ground_context: FxtrID
    ):
        """Entity with multiple refs (2 weak, 1 strong) to same child."""
        # Create a fresh arena for this test
        arena = _new_arena(memory_store)
        arena.track_builtin(ground_context)

        # Create a child entity
        child: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "child_data": "shared_child",
        }
        child_id = arena.store(child)

        # Create parent with 3 refs to the same child: 2 weak, 1 strong
        parent: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "weak_ref1": RawWeakFxtrID(inner_id=child_id),
            "weak_ref2": RawWeakFxtrID(inner_id=child_id),
            "strong_ref": RawStrongFxtrID(inner_id=child_id),
        }
        parent_id = arena.store(parent)

        # Load it back
        loaded = await arena.load(parent_id)
        assert loaded["weak_ref1"].inner_id == child_id
        assert loaded["weak_ref2"].inner_id == child_id
        assert loaded["strong_ref"].inner_id == child_id

        # Explicitly close the arena
        arena.close()

    def test_empty_entity(self, _legacy_arena: _LeasedArena, ground_context: FxtrID):
        """Entity with only $fxtr.ctx."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
        }
        node_id = _legacy_arena.store(entity)
        assert node_id.get_tag() == "d"

    def test_large_entity(self, _legacy_arena: _LeasedArena, ground_context: FxtrID):
        """Entity with many fields."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
        }
        for i in range(100):
            entity[f"field_{i}"] = f"value_{i}"
        node_id = _legacy_arena.store(entity)
        assert _legacy_arena.is_tracked(node_id)

    @pytest.mark.anyio
    async def test_deeply_nested_entity(self, _legacy_arena: _LeasedArena, ground_context: FxtrID):
        """Entity with deep nesting."""
        # Build a deeply nested structure
        nested = {"level": 10}
        for i in range(9, -1, -1):
            nested = {"level": i, "inner": nested}

        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "nested": nested,
        }
        node_id = _legacy_arena.store(entity)
        loaded = await _legacy_arena.load(node_id)
        assert loaded["nested"]["level"] == 0

    def test_entity_with_all_primitive_types(
        self, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """All CBOR primitive types."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "none": None,
            "bool_true": True,
            "bool_false": False,
            "int_pos": 42,
            "int_neg": -42,
            "float": 3.14,
            "str": "hello",
            "bytes": b"world",
            "list": [1, 2, 3],
            "dict": {"a": 1},
        }
        node_id = _legacy_arena.store(entity)
        assert _legacy_arena.is_tracked(node_id)

    @pytest.mark.anyio
    async def test_entity_with_bytes_field(
        self, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """Binary data in entity."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "binary": b"\x00\x01\x02\xff",
        }
        node_id = _legacy_arena.store(entity)
        loaded = await _legacy_arena.load(node_id)
        assert loaded["binary"] == b"\x00\x01\x02\xff"

    @pytest.mark.anyio
    async def test_shared_dependency_referenced_twice(self):
        """
        Reproduces previous bug where loading an entity that references the same dependency twice
        caused InheritanceParentInvariantError.

        The issue: when loading C (which references A twice), the first reference to A
        triggered abandonment of the claim from subarena_c (since A was already loaded
        from subarena_b). The second reference then failed because A is no longer tracked
        in subarena_c. Fixed by ensuring we only process each child once.

        Ported to the lease-backed API: X1's lease doubles as the key lease for the
        ``lazily_attach_from_arena`` calls (Y == Z, so the default converter is used).
        """
        ground_context = FxtrID.from_tag_and_payload(".", b"")

        store = MemoryStore()
        # Token T1 simulates the "source" scope
        x1 = _new_arena(store)
        x1.track_builtin(ground_context)

        # Token T2 simulates the "consumer" scope. (No ancestry needed: in the
        # new model the lazy_attach bridge below holds Lease[T1] explicitly.)
        x2 = _new_arena(store)
        x2.track_builtin(ground_context)

        try:
            # Create entity A in X1
            entity_a: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                "name": "A",
            }
            id_a = x1.store(entity_a)

            # Create entity B that points to A
            entity_b: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                "name": "B",
                "ref": RawStrongFxtrID(inner_id=id_a),
            }
            id_b = x1.store(entity_b)

            # Create entity C that points to A TWICE
            entity_c: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                "name": "C",
                "ref1": RawStrongFxtrID(inner_id=id_a),
                "ref2": RawStrongFxtrID(inner_id=id_a),
            }
            id_c = x1.store(entity_c)

            # Fork subarenas containing just B and C respectively.
            subarena_b = x1.copy_to_new_arena([id_b])
            subarena_c = x1.copy_to_new_arena([id_c])

            # Lazily attach B into X2 via subarena_b. Source brand == T1;
            # key lease is T1's own lease; an IndependentLocalBridge is the
            # case-1 shape (Y is reachable via a stored Lease[Y]).
            from fxtr2.leases.lease_bridges import IndependentLocalBridge

            x2._arena.lazily_attach_from_arena(
                x2._lease.clone(),
                id_b,
                subarena_b,
                IndependentLocalBridge(lease=x1._lease.clone()),
            )

            # Load B in X2
            loaded_b = await x2.load(id_b)
            assert loaded_b["name"] == "B"

            # Lazily attach C into X2 via subarena_c
            x2._arena.lazily_attach_from_arena(
                x2._lease.clone(),
                id_c,
                subarena_c,
                IndependentLocalBridge(lease=x1._lease.clone()),
            )

            # Load C in X2 - bug originally triggered here when processing A twice
            loaded_c = await x2.load(id_c)
            assert loaded_c["name"] == "C"

            # Load A in X2
            loaded_a = await x2.load(id_a)
            assert loaded_a["name"] == "A"

        finally:
            subarena_b.close()
            subarena_c.close()
            x2.close()
            x1.close()
            store.close()


# =============================================================================
# Integration: Store/Load/Unwrap Flow
# =============================================================================


class TestIntegrationFlow:
    """Integration tests for complete workflows."""

    @pytest.mark.anyio
    async def test_full_provenance_flow(self, _legacy_arena: _LeasedArena):
        """Create context, store n-node, unwrap, verify."""
        ground = FxtrID.from_tag_and_payload(".", b"")

        # Track builtins first
        ctx_type = FxtrID.builtin("ctx")
        test_type = FxtrID.builtin("test")
        _legacy_arena.track_builtin(ctx_type)
        _legacy_arena.track_builtin(test_type)

        # Create context data
        context_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
            "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
            "value": RawWeakFxtrID(inner_id=ground),
            "ctx_key": "full_flow",
            "metadata": {"test": True},
        }
        c_node_id = _legacy_arena.create_c_node(context_data)

        # Wrap the test type builtin as a u-node for use in n-node
        u_test_type = _legacy_arena.create_u_node(test_type)

        # Store n-node with context
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=c_node_id),
            "$fxtr.type": RawWeakFxtrID(inner_id=u_test_type),
            "name": "provenance_entity",
            "value": 123,
        }
        n_node_id = _legacy_arena.store(entity)

        # Verify it's an n-node
        assert n_node_id.get_tag() == "n"

        # Unwrap to d-node
        d_node_id = _legacy_arena.unwrap_provenance(n_node_id)
        assert d_node_id is not None
        assert d_node_id.get_tag() == "d"

        # Load both and verify data matches
        n_loaded = await _legacy_arena.load(n_node_id)
        d_loaded = await _legacy_arena.load(d_node_id)

        assert n_loaded["name"] == "provenance_entity"
        assert d_loaded["name"] == "provenance_entity"

    @pytest.mark.anyio
    async def test_p_node_path_traversal(self, _legacy_arena: _LeasedArena, ground_context: FxtrID):
        """Create p-node tree, traverse via loads."""
        # Track builtin first
        pathroot_type = FxtrID.builtin("pathroot")
        _legacy_arena.track_builtin(pathroot_type)

        # Create a tree of entities
        leaf1: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "type": "leaf",
            "value": 1,
        }
        leaf1_id = _legacy_arena.store(leaf1)

        leaf2: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "type": "leaf",
            "value": 2,
        }
        leaf2_id = _legacy_arena.store(leaf2)

        branch: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "type": "branch",
            "left": RawStrongFxtrID(inner_id=leaf1_id),
            "right": RawStrongFxtrID(inner_id=leaf2_id),
        }
        branch_id = _legacy_arena.store(branch)

        root: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "type": "root",
            "child": RawStrongFxtrID(inner_id=branch_id),
        }
        root_id = _legacy_arena.store(root)

        # Create p-node for root
        pathroot_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "$fxtr.type": RawWeakFxtrID(inner_id=pathroot_type),
            "value": RawWeakFxtrID(inner_id=root_id),
            "ctx_key": "tree_path",
            "metadata": {},
        }
        p_root_id = _legacy_arena.create_p_node(pathroot_data)

        # Load root p-node
        p_root_loaded = await _legacy_arena.load(p_root_id)
        assert p_root_loaded["type"] == "root"

        # Navigate to child
        p_child_ref = p_root_loaded["child"]
        assert isinstance(p_child_ref, (RawStrongFxtrID, RawWeakFxtrID))
        p_child_id = p_child_ref.inner_id
        assert p_child_id.get_tag() == "p"

        # Load child p-node
        p_child_loaded = await _legacy_arena.load(p_child_id)
        assert p_child_loaded["type"] == "branch"


# =============================================================================
# Load Flat Tests
# =============================================================================


class TestLoadFlat:
    """Tests for load_flat functionality."""

    @pytest.mark.anyio
    async def test_load_flat_d_node(self, _legacy_arena: _LeasedArena, ground_context: FxtrID):
        """load_flat returns raw bytes and deps for d-nodes."""
        # Store a simple d-node
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = _legacy_arena.store(entity)

        # Load flat
        from fxtr2.data.abstract_arena import DepKind

        flat_data = await _legacy_arena.load_flat(entity_id)

        # Check entity_id
        assert flat_data.entity_id == entity_id

        # Check entity_bytes is not None
        assert flat_data.entity_bytes is not None

        # Check deps contains the marker
        dep_ids = {dep_id for dep_id, _ in flat_data.deps}
        assert ground_context in dep_ids

        # Verify WEAK_REF for marker
        marker_deps = [
            (dep_id, kind) for dep_id, kind in flat_data.deps if dep_id == ground_context
        ]
        assert len(marker_deps) == 1
        assert marker_deps[0][1] == DepKind.WEAK_REF

    @pytest.mark.anyio
    async def test_load_flat_n_node(self, _legacy_arena: _LeasedArena, ground_context: FxtrID):
        """load_flat returns raw bytes and deps including UNWRAPS_TO for n-nodes."""
        from fxtr2.data.abstract_arena import DepKind

        # Create a context entity with ground context
        context_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "value": RawWeakFxtrID(inner_id=ground_context),
        }
        c_id = _legacy_arena.create_c_node(context_data)

        # Store n-node with provenance
        prov_entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=c_id),
            "data": "test",
        }
        n_id = _legacy_arena.store(prov_entity)
        assert n_id.get_tag() == "n"

        # Load flat
        flat_data = await _legacy_arena.load_flat(n_id)

        # Check entity_bytes
        assert flat_data.entity_bytes is not None

        # Check deps includes UNWRAPS_TO
        dep_kinds = {kind for _, kind in flat_data.deps}
        assert DepKind.UNWRAPS_TO in dep_kinds

    @pytest.mark.anyio
    async def test_load_flat_c_node(self, _legacy_arena: _LeasedArena, ground_context: FxtrID):
        """load_flat returns no bytes and correct deps for c-nodes."""
        from fxtr2.data.abstract_arena import DepKind

        # Create a context entity
        context_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "value": RawWeakFxtrID(inner_id=ground_context),
        }
        c_id = _legacy_arena.create_c_node(context_data)

        # Load flat
        flat_data = await _legacy_arena.load_flat(c_id)

        # Check no bytes
        assert flat_data.entity_bytes is None

        # Check deps
        dep_kinds = {kind for _, kind in flat_data.deps}
        assert DepKind.CTX_NODE in dep_kinds
        assert DepKind.UNWRAPS_TO in dep_kinds

    @pytest.mark.anyio
    async def test_load_flat_builtin_node(self, _legacy_arena: _LeasedArena):
        """load_flat returns no bytes and no deps for builtin nodes."""
        here = FxtrID.from_tag_and_payload(".", b"")
        _legacy_arena.track_builtin(here)

        flat_data = await _legacy_arena.load_flat(here)

        assert flat_data.entity_bytes is None
        assert flat_data.deps == []

    @pytest.mark.anyio
    async def test_load_flat_tracks_deps(self, _legacy_arena: _LeasedArena, ground_context: FxtrID):
        """load_flat tracks all deps in the arena."""
        # Store entity with a child
        child_entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "child",
        }
        child_id = _legacy_arena.store(child_entity)

        parent_entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "child": RawStrongFxtrID(inner_id=child_id),
        }
        parent_id = _legacy_arena.store(parent_entity)

        # Create a new arena sharing the same token.
        new_arena_raw: MemoryArena[Any] = _legacy_arena._arena._store._new_arena_for_token(
            _legacy_arena._arena.token
        )
        new_arena_raw._track_from_store(parent_id)
        new_arena = _LeasedArena(new_arena_raw, _legacy_arena._lease.clone())

        # Child is not initially tracked in new arena
        assert not new_arena.is_tracked(child_id)

        # load_flat should track child
        await new_arena.load_flat(parent_id)

        # Now child should be tracked
        assert new_arena.is_tracked(child_id)

        new_arena_raw.close()
        new_arena._lease.discard()

    @pytest.mark.anyio
    async def test_bare_store_flat_roundtrip(
        self, memory_store: MemoryStore, ground_context: FxtrID
    ):
        """bare_store_flat and bare_load_flat roundtrip correctly."""
        # Create an arena and store an entity
        arena = _new_arena(memory_store)
        arena.track_builtin(ground_context)
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": [1, 2, 3],
        }
        entity_id = arena.store(entity)

        # Load flat data (store-level, lease-first)
        flat_data = await memory_store.bare_load_flat(arena._lease.clone(), entity_id)

        # Store it in a new store using bare_store_flat
        new_store = MemoryStore()
        new_arena = _new_arena(new_store)
        new_arena.track_builtin(ground_context)

        # Track all deps first
        for dep_id, _ in flat_data.deps:
            if dep_id != ground_context:
                new_arena.track_builtin(dep_id)

        stored_id = new_store.bare_store_flat(new_arena._lease.clone(), flat_data)
        assert stored_id == entity_id

        # Verify we can load it back
        new_arena._arena._track_from_store(stored_id)
        loaded_data = await new_arena.load(stored_id)
        assert loaded_data == entity

        arena.close()
        new_arena.close()


# =============================================================================
# Regression: shared inheritance arena corruption on diamond deps
# =============================================================================


class TestSharedArenaCorruption:
    """Regression test for a bug where _adopt_inheritance_arena_claim corrupted
    a shared inheritance arena.

    When two nodes share a dep (diamond pattern: A→{B,C}, B→{C}), loading A
    adopts B and C with the same arena. Loading B then re-discovers C as a dep
    and tries to adopt it again with the same arena/token. The DOMINATED branch
    would abandon(C) from the arena — but that arena is also C's existing
    inheritance arena, so the abandon corrupts it. A subsequent load of C fails
    because its inheritance arena no longer tracks it.
    """

    @pytest.mark.anyio
    async def test_diamond_dep_lazy_attach_then_load(self):
        """Loading diamond deps through lazily_attach should not corrupt shared claims."""
        ground = FxtrID.from_tag_and_payload(".", b"")

        # --- Source store: create entities C (leaf), B→{C}, A→{B, C} ---
        source_store = MemoryStore()
        source_arena = _new_arena(source_store)
        source_arena.track_builtin(ground)

        c_id = source_arena.store(
            {"$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground), "data": "leaf_c"}
        )
        b_id = source_arena.store(
            {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
                "ref_c": RawStrongFxtrID(inner_id=c_id),
            }
        )
        a_id = source_arena.store(
            {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
                "ref_b": RawStrongFxtrID(inner_id=b_id),
                "ref_c": RawStrongFxtrID(inner_id=c_id),
            }
        )

        # --- Dest store: lazily attach A, then load A → B → C ---
        dest_store = MemoryStore()
        dest_arena = _new_arena(dest_store)

        # Lazily attach A. The source arena's token lives on source_store, so
        # we hold an IndependentLocalBridge on source_arena's own lease (case 1).
        from fxtr2.leases.lease_bridges import IndependentLocalBridge

        dest_arena._arena.lazily_attach_from_arena(
            dest_arena._lease.clone(),
            a_id,
            source_arena._arena,
            IndependentLocalBridge(lease=source_arena._lease.clone()),
        )

        # Load A: _ensure_own(A) loads A from its arena, discovers B and C,
        # adopts both with the same arena object.
        await dest_store.bare_load_flat(dest_arena._lease.clone(), a_id)

        # Load B: _ensure_own(B) loads B from the shared arena, discovers C,
        # re-adopts C with the same arena+token → DOMINATED.
        await dest_store.bare_load_flat(dest_arena._lease.clone(), b_id)

        # Load C: _ensure_own(C) tries to load from its (now corrupted) arena.
        flat_c = await dest_store.bare_load_flat(dest_arena._lease.clone(), c_id)
        assert flat_c.entity_id == c_id

        # Cleanup: dest first (its inherited arenas live in source store)
        dest_arena.close()
        dest_store.close()
        source_arena.close()
        source_store.close()


# =============================================================================
# AttachedLocalLifetime converter integration
# =============================================================================


class _RebrandedReadableArena:
    """Test helper: presents a ReadableArena whose token/grant are externally supplied.

    Used to simulate a foreign arena whose lifetime brand is an
    ``AttachedLocalLifetime`` output token. Validates and consumes each incoming
    lease against the supplied expected grant; forwards the underlying operation
    to a real ``MemoryArena`` using a stored backing lease (kept alive for the
    wrapper's lifetime).
    """

    def __init__(
        self,
        output_token,
        expected_grant,
        backing_arena: MemoryArena[Any],
        backing_lease: Lease[Any],
    ):
        self._output_token = output_token
        self._expected_grant = expected_grant
        self._backing = backing_arena
        self._backing_lease = backing_lease

    @property
    def token(self):
        return self._output_token

    def _consume(self, lease):
        if lease.grant != self._expected_grant:
            raise RuntimeError(f"Expected grant {self._expected_grant!r}, got {lease.grant!r}")
        with lease.take():
            pass

    def is_tracked(self, lease, node_id):
        self._consume(lease)
        return self._backing.is_tracked(self._backing_lease.clone(), node_id)

    async def load(self, lease, node_id):
        self._consume(lease)
        return await self._backing.load(self._backing_lease.clone(), node_id)

    async def load_flat(self, lease, node_id):
        self._consume(lease)
        return await self._backing.load_flat(self._backing_lease.clone(), node_id)

    def unwrap_provenance(self, lease, node_id):
        self._consume(lease)
        return self._backing.unwrap_provenance(self._backing_lease.clone(), node_id)

    def c_node_to_context_data(self, lease, c_node_id):
        self._consume(lease)
        return self._backing.c_node_to_context_data(self._backing_lease.clone(), c_node_id)

    def p_node_to_pathroot(self, lease, p_node_id):
        self._consume(lease)
        return self._backing.p_node_to_pathroot(self._backing_lease.clone(), p_node_id)

    def step_back_p_ctx(self, lease, p_node_id):
        self._consume(lease)
        return self._backing.step_back_p_ctx(self._backing_lease.clone(), p_node_id)

    def abandon(self, lease, node_id):
        self._consume(lease)
        self._backing.abandon(self._backing_lease.clone(), node_id)

    def copy_to_new_arena(self, lease, node_ids):
        self._consume(lease)
        new_backing = self._backing.copy_to_new_arena(self._backing_lease.clone(), list(node_ids))
        return _RebrandedReadableArena(
            self._output_token,
            self._expected_grant,
            new_backing,
            self._backing_lease.clone(),
        )

    def close(self):
        self._backing.close()
        self._backing_lease.discard()


class TestDerivedLocalLifetimeConverter:
    """Covers ``lazily_attach_from_arena`` with a non-default ``LifetimeConverter``.

    Uses ``DerivedLocalLifetime.exchange_forward`` wrapped in a 2-arg adapter so it
    satisfies the ``(key_lease, base_lease) -> out_lease`` shape required by
    :class:`LifetimeConverter`. The derived lifetime's inputs are ``{key_token,
    consumer_token}``: ``key_token`` is a separate unconditional lifetime the test
    controls (acting as the "key" gate), and ``consumer_token`` is the consumer
    memory store's own token (acting as the "base" requirement). Every converter
    invocation receives a fresh key-lease clone and a fresh consumer-lease clone
    and produces an output lease that the foreign arena validates.
    """

    @pytest.mark.anyio
    async def test_derived_forward_bridges_load(self):
        """Attach a foreign arena via ``derived.exchange_forward`` and load through it.

        Flow:
          1. Consumer store mints a token (``consumer_token``); the source store
             is separate.
          2. Test mints a separate ``key_ull`` for key-gating.
          3. ``DerivedLocalLifetime`` binds output token Y to inputs
             ``{key_ull.token, consumer_token}``.
          4. A MemoryArena in the source store holds the entity we want to
             attach; we wrap it so its external brand/grant match Y.
          5. Attach with ``key_lease = key_ull.root_lease.clone()`` and
             ``converter`` = a 2-arg adapter that routes ``(key, base)`` into
             ``derived.exchange_forward({key_token: key, consumer_token: base})``.
          6. Load the entity through the consumer — succeeds; every hop calls
             the converter with fresh (key_clone, consumer_lease_clone) →
             fresh Lease[Y] → wrapper validates and forwards to the backing arena.
        """
        from fxtr2.leases.local_lease import DerivedLocalLifetime, UnconditionalLocalLifetime

        ground = FxtrID.from_tag_and_payload(".", b"")

        # --- Source side: real MemoryArena containing the entity. ---
        source_store = MemoryStore()
        source_arena = _new_arena(source_store)
        source_arena.track_builtin(ground)
        entity_id = source_arena.store(
            {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
                "value": "derived-bridge",
            }
        )

        # --- Consumer side: register the consumer token first. ---
        consumer_store = MemoryStore()
        consumer_leased = _new_arena(consumer_store)
        consumer_leased.track_builtin(ground)
        consumer_token = consumer_leased._lease.subject

        # --- Separate key ULL + Derived lifetime: inputs = {key, consumer}. ---
        key_ull = UnconditionalLocalLifetime("der-test-key")
        derived = DerivedLocalLifetime("der-test", inputs={key_ull.token, consumer_token})

        # Build a 2-arg adapter matching LifetimeConverter's (key, base) -> out shape.
        def converter(key: Lease[Any], base: Lease[Any]) -> Lease[Any]:
            return derived.exchange_forward({key_ull.token: key, consumer_token: base})

        # Snapshot the output grant (stable across exchange_forward calls whose
        # input grants are each pairs of clones of stable roots).
        sample = converter(key_ull.root_lease.clone(), consumer_leased._lease.clone())
        output_grant = sample.grant
        sample.discard()

        # Wrap the source arena so its token/grant externally present as Y.
        wrapped_source = _RebrandedReadableArena(
            output_token=derived.token,
            expected_grant=output_grant,
            backing_arena=source_arena._arena,
            backing_lease=source_arena._lease.clone(),
        )

        # --- Attach via a DependentLocalBridge wrapping the converter. ---
        from fxtr2.leases.lease_bridges import DependentLocalBridge

        def dependent_converter(inputs: dict[Any, Lease[Any]], base: Lease[Any]) -> Lease[Any]:
            key = inputs.pop(key_ull.token)
            for v in inputs.values():
                v.discard()
            return converter(key, base)

        bridge = DependentLocalBridge(
            leases={key_ull.token: key_ull.root_lease.clone()},
            converter=dependent_converter,
        )
        consumer_store.bare_lazily_attach_from_arena(
            consumer_leased._lease.clone(),
            entity_id,
            wrapped_source,
            bridge,
        )

        # --- Load: consumer reaches entity through the bridge. ---
        flat = await consumer_store.bare_load_flat(consumer_leased._lease.clone(), entity_id)
        assert flat.entity_id == entity_id
        assert flat.entity_bytes is not None

        # Second load: case-3 should now be established; no further converter
        # hop needed. We can still load successfully.
        flat2 = await consumer_store.bare_load_flat(consumer_leased._lease.clone(), entity_id)
        assert flat2.entity_bytes == flat.entity_bytes

        # --- Cleanup. Revoke the derived first so any output leases still held
        # by the memory store's foreign-lease path are broken; then drop the key
        # ULL and close the consumer / source. ---
        wrapped_source.close()
        consumer_leased.close()
        consumer_store.close()
        source_arena.close()
        source_store.close()
        derived.revoke("test teardown")
        key_ull.revoke("test teardown")


# =============================================================================
# Sync-Dep Closure invariant
# =============================================================================


class TestSyncDepClosure:
    """Tests covering the Sync-Dep Closure invariant on ``MemoryStore``.

    Invariant: if a token T has access (owned or inherited) to a node N, then
    T has access to every sync-discoverable dep of N — recursively. Sync-
    discoverable deps are ``unwraps_to`` (any provenance node), ``data.ctx_node``
    (c/p), and ``data.backstep_node`` (p). See the ``MemoryStore`` class
    docstring for the full statement.
    """

    def _make_n_node(
        self, arena: _LeasedArena, ground_context: FxtrID, name: str
    ) -> tuple[FxtrID, FxtrID]:
        """Helper: build an n-node by creating a c-node (whose value is the
        ground builtin so ``c.unwraps_to`` is ``.``) + storing a CBOR with the
        c-node as marker and only non-ID values. Returns ``(n_node_id, d_node_id)``."""
        ctx_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "value": RawWeakFxtrID(inner_id=ground_context),
            "ctx_key": name,
        }
        c_node_id = arena.create_c_node(ctx_data)
        prov_entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=c_node_id),
            "name": name,
        }
        n_id = arena.store(prov_entity)
        d_id = arena._arena._store._entity_states[n_id].unwraps_to
        assert d_id is not None
        return n_id, d_id

    @pytest.mark.anyio
    @pytest.mark.anyio
    async def test_a_copy_n_node_unwrap_reachability(
        self, memory_store: MemoryStore, ground_context: FxtrID
    ):
        """Regression for the worklist-driven copy path's analog of the
        original Stage-2 n-node dedup bug. T1 stores an n-node. T2 calls
        ``bare_copy_from_arena`` to copy the n-node DAG into its own claims;
        the worklist saves deps before parents (LIFO), so the d-form is
        owned by T2 before the n-node's flat-dedup hits. Post-copy,
        ``unwrap_provenance`` under T2 returns the d-node and the d-node is
        reachable to T2.
        """
        arena1 = _new_arena(memory_store)
        arena2 = _new_arena(memory_store)
        arena1.track_builtin(ground_context)
        arena2.track_builtin(ground_context)
        try:
            n_id, d_id = self._make_n_node(arena1, ground_context, "A")

            # T2 copies the n-node DAG from arena1 into its own claims.
            await memory_store.bare_copy_from_arena(
                arena2._lease.clone(),
                arena1._lease.clone(),
                arena1._arena,
                n_id,
            )

            # Both n-node and d-node are now reachable to arena2.
            n_node = memory_store._entity_states[n_id]
            d_node = memory_store._entity_states[d_id]
            assert arena2.token in n_node.owned_tokens
            assert memory_store._node_has_access(d_node, arena2.token)
        finally:
            arena1.close()
            arena2.close()

    @pytest.mark.anyio
    async def test_b_inheritance_attach_c_node(
        self, memory_store: MemoryStore, ground_context: FxtrID
    ):
        """Bug-2 repro for c-nodes: T2 inherits an existing-with-data c-node;
        the cascade must give T2 access to ``data.ctx_node`` and ``unwraps_to``
        immediately, before any sync read self-heals."""
        from fxtr2.leases.lease_bridges import IndependentLocalBridge

        arena1 = _new_arena(memory_store)
        arena2 = _new_arena(memory_store)
        arena1.track_builtin(ground_context)
        arena2.track_builtin(ground_context)
        try:
            # T1 builds a c-node (data populated by ``create_c_node``).
            inner_d: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                "name": "v",
            }
            inner_d_id = arena1.store(inner_d)
            ctx_data: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                "value": RawWeakFxtrID(inner_id=inner_d_id),
            }
            c_node_id = arena1.create_c_node(ctx_data)

            c_node = memory_store._entity_states[c_node_id]
            ctx_node_id = c_node.data.ctx_node  # type: ignore[union-attr]
            assert ctx_node_id is not None
            unwraps_to_id = c_node.unwraps_to
            assert unwraps_to_id is not None

            # T2 inherits the c-node via lazy attach (sentinel mode).
            sub = arena1.copy_to_new_arena([c_node_id])
            arena2._arena.lazily_attach_from_arena(
                arena2._lease.clone(),
                c_node_id,
                sub,
                IndependentLocalBridge(lease=arena1._lease.clone()),
            )

            # The cascade must already have inheritance entries for ctx_node and
            # unwraps_to under T2 — no sync read needed first.
            ctx_node = memory_store._entity_states[ctx_node_id]
            unwrap_node = memory_store._entity_states[unwraps_to_id]
            assert memory_store._node_has_access(ctx_node, arena2.token)
            assert memory_store._node_has_access(unwrap_node, arena2.token)

            sub.close()
        finally:
            arena1.close()
            arena2.close()

    @pytest.mark.anyio
    async def test_c_inheritance_attach_p_node(
        self, memory_store: MemoryStore, ground_context: FxtrID
    ):
        """Bug-2 repro for p-nodes: inheritance attach must cascade to
        ``ctx_node`` (pathroot) and ``unwraps_to`` immediately."""
        from fxtr2.leases.lease_bridges import IndependentLocalBridge

        arena1 = _new_arena(memory_store)
        arena2 = _new_arena(memory_store)
        arena1.track_builtin(ground_context)
        arena2.track_builtin(ground_context)
        try:
            # Build a root p-node in T1.
            inner_d: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                "name": "p-val",
            }
            inner_d_id = arena1.store(inner_d)
            pathroot_data: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                "value": RawWeakFxtrID(inner_id=inner_d_id),
            }
            p_node_id = arena1.create_p_node(pathroot_data)

            p_node = memory_store._entity_states[p_node_id]
            pathroot_id = p_node.data.ctx_node  # type: ignore[union-attr]
            unwraps_to_id = p_node.unwraps_to
            assert pathroot_id is not None
            assert unwraps_to_id is not None

            # T2 inherits the p-node.
            sub = arena1.copy_to_new_arena([p_node_id])
            arena2._arena.lazily_attach_from_arena(
                arena2._lease.clone(),
                p_node_id,
                sub,
                IndependentLocalBridge(lease=arena1._lease.clone()),
            )

            # Cascade must have populated inheritance for pathroot + unwraps_to.
            pathroot_node = memory_store._entity_states[pathroot_id]
            unwrap_node = memory_store._entity_states[unwraps_to_id]
            assert memory_store._node_has_access(pathroot_node, arena2.token)
            assert memory_store._node_has_access(unwrap_node, arena2.token)

            sub.close()
        finally:
            arena1.close()
            arena2.close()

    def test_d_track_from_memory_arena_across_c_node(
        self, memory_store: MemoryStore, ground_context: FxtrID
    ):
        """``track_from_memory_arena`` must give dest_token access to all sync
        deps of a c-node it owns — exercising the data-path cascade."""
        arena1 = _new_arena(memory_store)
        arena2 = _new_arena(memory_store)
        arena1.track_builtin(ground_context)
        arena2.track_builtin(ground_context)
        try:
            inner_d: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                "name": "v",
            }
            inner_d_id = arena1.store(inner_d)
            ctx_data: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                "value": RawWeakFxtrID(inner_id=inner_d_id),
            }
            c_node_id = arena1.create_c_node(ctx_data)
            c_node = memory_store._entity_states[c_node_id]
            ctx_node_id = c_node.data.ctx_node  # type: ignore[union-attr]
            unwraps_to_id = c_node.unwraps_to
            assert ctx_node_id is not None
            assert unwraps_to_id is not None

            # Track c-node from arena1 to arena2 (data path — arena1 owns c-node).
            arena2.track_from_memory_arena(c_node_id, arena1)
            assert arena2.is_tracked(c_node_id)

            # Sync deps must be reachable to arena2's token.
            ctx_node = memory_store._entity_states[ctx_node_id]
            unwrap_node = memory_store._entity_states[unwraps_to_id]
            assert memory_store._node_has_access(ctx_node, arena2.token)
            assert memory_store._node_has_access(unwrap_node, arena2.token)
        finally:
            arena1.close()
            arena2.close()

    def test_e_n_node_dedup_regression(self, memory_store: MemoryStore, ground_context: FxtrID):
        """Stage-2 original repro, kept as a regression. T1 stores an n-node;
        T2 re-stores via ``bare_store`` (CBOR path); post-store
        ``unwrap_provenance`` must succeed for T2."""
        arena1 = _new_arena(memory_store)
        arena2 = _new_arena(memory_store)
        arena1.track_builtin(ground_context)
        arena2.track_builtin(ground_context)
        try:
            n_id, d_id = self._make_n_node(arena1, ground_context, "regression")

            # Track c-node into arena2 so it has access to the marker before
            # re-storing the same n-node CBOR.
            cbor = arena1._arena._store._entity_states[n_id].data.entity_bytes  # type: ignore[union-attr]
            assert cbor is not None
            from fxtr2.data import entity_core

            n_cbor = entity_core.fxtr_cbor_loads(cbor)
            # Walk required IDs and lift them into arena2 via track_from_memory_arena.
            from fxtr2.data.arena_util import visit_ids

            referenced: set[FxtrID] = set()
            visit_ids(n_cbor, lambda c: referenced.add(c.inner_id))
            for ref_id in referenced:
                if not arena2.is_tracked(ref_id):
                    arena2.track_from_memory_arena(ref_id, arena1)

            stored_id = arena2.store(n_cbor)
            assert stored_id == n_id

            # T2 must be able to unwrap_provenance and end up with d-node tracked.
            unwrapped = arena2.unwrap_provenance(n_id)
            assert unwrapped == d_id
            assert arena2.is_tracked(d_id)
        finally:
            arena1.close()
            arena2.close()

    @pytest.mark.anyio
    async def test_f_idempotency(self, memory_store: MemoryStore, ground_context: FxtrID):
        """Granting access to a node a token already has is a no-op — no
        duplicate entries, no extra bridge claims."""
        from fxtr2.leases.lease_bridges import IndependentLocalBridge

        arena1 = _new_arena(memory_store)
        arena2 = _new_arena(memory_store)
        arena1.track_builtin(ground_context)
        arena2.track_builtin(ground_context)
        try:
            n_id, d_id = self._make_n_node(arena1, ground_context, "idem")

            # First inheritance attach.
            sub = arena1.copy_to_new_arena([n_id])
            arena2._arena.lazily_attach_from_arena(
                arena2._lease.clone(),
                n_id,
                sub,
                IndependentLocalBridge(lease=arena1._lease.clone()),
            )

            # Snapshot.
            n_node = memory_store._entity_states[n_id]
            d_node = memory_store._entity_states[d_id]
            assert arena2.token in n_node.inheritance_bridges
            assert arena2.token in d_node.inheritance_bridges
            entries_before = (
                len(n_node.inheritance_bridges),
                len(d_node.inheritance_bridges),
                len(n_node.owned_tokens),
                len(d_node.owned_tokens),
            )

            # Re-store via bare_store: hits the dedup path. The cascade must be
            # idempotent — no duplicate entries.
            arena2.store(
                {
                    "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                    "data": "irrelevant",
                }
            )

            entries_after = (
                len(n_node.inheritance_bridges),
                len(d_node.inheritance_bridges),
                len(n_node.owned_tokens),
                len(d_node.owned_tokens),
            )
            # No new inheritance entries should have been added (dedup).
            assert entries_before[:2] == entries_after[:2]
            sub.close()
        finally:
            arena1.close()
            arena2.close()

    @pytest.mark.anyio
    async def test_g_diamond_shape(self, memory_store: MemoryStore, ground_context: FxtrID):
        """Diamond: a c-node N has ``ctx_node = X`` and ``unwraps_to = X``-related
        chain. The cascade must dedup and not double-claim the bridge."""
        from fxtr2.leases.lease_bridges import IndependentLocalBridge

        arena1 = _new_arena(memory_store)
        arena2 = _new_arena(memory_store)
        arena1.track_builtin(ground_context)
        arena2.track_builtin(ground_context)
        try:
            # Build a c-node where the wrapped value happens to be the SAME
            # d-node that is also the c-node's ctx_node — guaranteed identical
            # because we'd normally have to specifically engineer it. Here we
            # just rely on the cascade being idempotent across structurally
            # distinct deps that happen to share a bridge record.
            n_id, d_id = self._make_n_node(arena1, ground_context, "diamond")

            sub = arena1.copy_to_new_arena([n_id])
            arena2._arena.lazily_attach_from_arena(
                arena2._lease.clone(),
                n_id,
                sub,
                IndependentLocalBridge(lease=arena1._lease.clone()),
            )

            # Bridge claim count for (arena2.token, arena1.token) reflects
            # exactly the set of nodes arena2 inherits via this bridge — no
            # duplicates.
            bridge_record = memory_store._token_data[arena2.token].bridges[arena1.token]
            # Each claimed node should be unique.
            assert len(bridge_record.claimed_ids) == len(set(bridge_record.claimed_ids))
            # Both n_id and d_id should be claimed exactly once.
            assert n_id in bridge_record.claimed_ids
            assert d_id in bridge_record.claimed_ids
            sub.close()
        finally:
            arena1.close()
            arena2.close()

    @pytest.mark.anyio
    async def test_h_p_node_backstep_marker_position_only(
        self, memory_store: MemoryStore, ground_context: FxtrID
    ):
        """Regression on the projector invariant that bounds the backstep
        cascade depth: ``backstep_node`` is set only on marker-position
        p-children. Build a multi-level p-node DAG, load through two levels,
        assert ``backstep_node is not None`` exactly for the marker-position
        children."""
        arena = _new_arena(memory_store)
        arena.track_builtin(ground_context)
        try:
            # D1 = {"x": ref(X), "$fxtr.ctx": "."} — innermost d-node.
            x: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                "leaf": True,
            }
            x_id = arena.store(x)
            d1: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                "x": RawWeakFxtrID(inner_id=x_id),
            }
            d1_id = arena.store(d1)

            # D0 = {"value": ref(D1), "$fxtr.ctx": "."} — root pathroot.
            d0: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                "value": RawWeakFxtrID(inner_id=d1_id),
            }
            p0_id = arena.create_p_node(d0)

            # Load p0: produces marker-position child (B0, backstep=p0,
            # unwraps_to="." marker target) and content-position child (Cv,
            # backstep=None, unwraps_to=d1).
            await arena.load(p0_id)

            # Walk the store post-load and identify p-children of p0 by their
            # ``data.backstep_node`` (or by ``data.ctx_node`` matching p0's
            # ctx_node — non-marker children share the pathroot).
            p0_node = memory_store._entity_states[p0_id]
            assert p0_node.data is not None
            assert p0_node.data.backstep_node is None  # root p-node has no backstep
            p0_ctx = p0_node.data.ctx_node

            p_children_of_p0 = [
                nid
                for nid, node in memory_store._entity_states.items()
                if nid.get_tag() == "p"
                and node.data is not None
                and (node.data.backstep_node == p0_id or node.data.ctx_node == p0_ctx)
                and nid != p0_id
            ]
            with_backstep = [
                nid
                for nid in p_children_of_p0
                if memory_store._entity_states[nid].data.backstep_node == p0_id  # type: ignore[union-attr]
            ]
            without_backstep = [
                nid
                for nid in p_children_of_p0
                if memory_store._entity_states[nid].data.backstep_node is None  # type: ignore[union-attr]
            ]
            assert len(with_backstep) == 1, (
                f"Expected exactly one marker-position p-child of p0 with "
                f"backstep, got {len(with_backstep)}"
            )
            assert (
                len(without_backstep) >= 1
            ), "Expected at least one content-position p-child without backstep"

            # The content-position child Cv unwraps to d1; load it and verify
            # the same pattern at the next level.
            cv_id = without_backstep[0]
            await arena.load(cv_id)
            cv_node = memory_store._entity_states[cv_id]
            assert cv_node.data is not None
            assert cv_node.data.backstep_node is None

            # Find Cv's marker-position grandchild (backstep == cv_id).
            cv_marker_children = [
                nid
                for nid, node in memory_store._entity_states.items()
                if nid.get_tag() == "p"
                and node.data is not None
                and node.data.backstep_node == cv_id
            ]
            assert (
                len(cv_marker_children) == 1
            ), "Expected exactly one marker-position p-child of cv with backstep"
            # Confirm the bound: the grandchild's backstep is Cv, and Cv's
            # backstep is None. Backstep chain length = 1.
            grandchild_id = cv_marker_children[0]
            grandchild_node = memory_store._entity_states[grandchild_id]
            assert grandchild_node.data is not None
            assert grandchild_node.data.backstep_node == cv_id
        finally:
            arena.close()


# =============================================================================
# Re-store onto a dataless adopted node
# =============================================================================


class TestRestoreOntoDatalessAdopted:
    """A content-dedup'd prov-form store must repair a dataless unwrap chain.

    Loading an n-node through a foreign inheritance entry (``_ensure_own``
    foreign branch) populates the n via ``_bare_store_flat_impl`` but adopts
    its UNWRAPS_TO dep (the d-form) as a *dataless* inheritance entry — the
    d bytes intentionally don't materialize locally on the load path. A
    subsequent content-addressed ``store`` of the same prov-form cbor dedups
    onto the populated n, but must still leave the token in the same end
    state as a fresh store: owning both the n and its d-form, with the
    d-form bytes saved locally (re-derived from the cbor in hand).

    Regression test: this previously failed with ``_add_owned_token: cannot
    grant ownership of <d-fid> with unpopulated NodeData`` (first seen as a
    wasm-guest dispatch failure when a guest re-``store()``\\ d an entity
    whose prov form had been loaded at connect time).
    """

    @pytest.mark.anyio
    async def test_restore_prov_form_after_foreign_load(self):
        from fxtr2.leases.lease_bridges import IndependentLocalBridge

        ground = FxtrID.from_tag_and_payload(".", b"")
        ctx_type = FxtrID.builtin("ctx")
        test_type = FxtrID.builtin("test")

        # --- Source side ("agent"): store an entity in provenance form. ---
        source_store = MemoryStore()
        source = _new_arena(source_store)
        consumer_store = MemoryStore()
        consumer = _new_arena(consumer_store)
        try:
            source.track_builtin(ground)
            source.track_builtin(ctx_type)
            source.track_builtin(test_type)

            context_data: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
                "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
                "value": RawWeakFxtrID(inner_id=ground),
                "ctx_key": "repro_ctx",
            }
            c_node_id = source.create_c_node(context_data)
            u_test_type = source.create_u_node(test_type)
            prov_entity: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=c_node_id),
                "$fxtr.type": RawWeakFxtrID(inner_id=u_test_type),
                "data": "prov data",
            }
            n_id = source.store(prov_entity)
            assert n_id.get_tag() == "n"
            d_id = source.unwrap_provenance(n_id)
            assert d_id is not None and d_id.get_tag() == "d"

            # --- Consumer side ("guest", separate store): adopt the n-node
            # through a foreign bridge, then load it. The load populates the
            # n locally but leaves its unwrapped d-form as a dataless
            # inheritance entry (bytes still live only source-side). ---
            consumer._arena.lazily_attach_from_arena(
                consumer._lease.clone(),
                n_id,
                source._arena,
                IndependentLocalBridge(lease=source._lease.clone()),
            )
            loaded = await consumer.load(n_id)
            assert loaded["data"] == "prov data"

            # Confirm the precondition this test is about: locally the n has
            # data, the d does not.
            assert consumer_store._entity_states[n_id].data is not None
            assert consumer_store._entity_states[d_id].data is None

            # --- Re-store the identical prov-form cbor on the consumer.
            # Content addressing dedups onto the already-tracked n; this
            # should simply succeed (the consumer demonstrably has the full
            # closure — it just loaded the entity). ---
            restored_id = consumer.store(prov_entity)
            assert restored_id == n_id

            # End-state parity with a fresh store: the consumer token owns
            # both forms and the d-form bytes are now saved locally.
            consumer_token = consumer.token
            n_node = consumer_store._entity_states[n_id]
            d_node = consumer_store._entity_states[d_id]
            assert consumer_token in n_node.owned_tokens
            assert consumer_token in d_node.owned_tokens
            assert d_node.data is not None
            assert d_node.data.entity_bytes is not None

            # Both forms are loadable afterwards.
            reloaded = await consumer.load(n_id)
            assert reloaded["data"] == "prov data"
            consumer._arena._track_from_store(d_id)
            loaded_d = await consumer.load(d_id)
            assert loaded_d["data"] == "prov data"
        finally:
            consumer.close()
            consumer_store.close()
            source.close()
            source_store.close()


# =============================================================================
# Wrong-brand lease handling
# =============================================================================


@pytest.mark.anyio
async def test_load_wrong_brand_lease_is_consumed(memory_store: MemoryStore):
    """``load`` consumes (discards) a wrong-brand lease when it rejects it.

    A lease whose subject is not the arena's token is rejected with
    ``LeaseGrantMismatchError`` (the precheck fires before the node id is even
    looked up, so no stored entity is needed) AND the lease is discarded — load
    is contracted to consume the lease it is handed, including on the reject
    path. Regression test for a leak that, for session leases, pinned the
    session lifetime and hung teardown (see git history / stage-14 notes).
    """
    ull_a = UnconditionalLocalLifetime("arena-a")
    memory_store.register_token(ull_a.token, name="arena-a")
    lease_a = ull_a.root_lease.take()
    arena = memory_store.new_arena(lease_a.clone())

    # A second, valid lease whose subject (token B) is NOT this arena's token.
    ull_b = UnconditionalLocalLifetime("other")
    memory_store.register_token(ull_b.token, name="other")
    wrong_lease = ull_b.root_lease.take()

    fake_id = FxtrID.from_tag_and_payload("d", b"\x00" * 32)
    try:
        # Rejection (raise) is correct; the bug is that the lease isn't consumed.
        with pytest.raises(LeaseGrantMismatchError):
            await arena.load(wrong_lease, fake_id)
        assert (
            not wrong_lease.is_active()
        ), "load() did not consume the wrong-brand lease on its reject path"
    finally:
        # Always tidy up so the test never leaks regardless of pass/xfail.
        if wrong_lease.is_active():
            wrong_lease.discard()
        arena.close()
        lease_a.discard()
