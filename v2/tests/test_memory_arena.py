"""Tests for fxtr2/data/memory_arena.py."""

from __future__ import annotations

import pytest

from fxtr2.data.entity_core import (
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawStrongFxtrID,
    RawWeakFxtrID,
)
from fxtr2.data.memory_arena import MemoryArena, MemoryStore

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
        arena = store.new_empty_arena()
        assert arena is not None
        assert isinstance(arena, MemoryArena)
        arena.close()

    def test_memory_store_multiple_arenas(self):
        """Can create multiple arenas from same store."""
        store = MemoryStore()
        arena1 = store.new_empty_arena()
        arena2 = store.new_empty_arena()
        assert arena1 is not arena2
        assert arena1._key != arena2._key
        arena1.close()
        arena2.close()


# =============================================================================
# MemoryArena Basic Operations
# =============================================================================


class TestMemoryArenaBasics:
    """Tests for MemoryArena basic operations."""

    def test_arena_is_tracked_empty(self, memory_arena: MemoryArena, ground_context: FxtrID):
        """New arena tracks the ground node (.)."""
        # Ground node is tracked by default
        assert memory_arena.is_tracked(ground_context)
        # Random builtin is not tracked
        assert not memory_arena.is_tracked(FxtrID.builtin("not_tracked"))

    def test_arena_track_builtin(self, memory_arena: MemoryArena):
        """track_builtin() tracks '.' and '!' nodes."""
        here = FxtrID.from_tag_and_payload(".", b"")
        builtin = FxtrID.builtin("test_type")

        # Track them
        memory_arena.track_builtin(here)
        memory_arena.track_builtin(builtin)

        assert memory_arena.is_tracked(here)
        assert memory_arena.is_tracked(builtin)

    def test_arena_close(self, memory_store: MemoryStore):
        """Closed arena behavior."""
        arena = memory_store.new_empty_arena()
        ground = FxtrID.from_tag_and_payload(".", b"")
        assert arena.is_tracked(ground)
        arena.close()
        # After close, nothing is tracked
        assert not arena.is_tracked(ground)


# =============================================================================
# Storing Ground Entities (d-nodes)
# =============================================================================


class TestStoreGroundEntities:
    """Tests for storing ground (d-node) entities."""

    def test_store_simple_entity(self, memory_arena: MemoryArena, ground_context: FxtrID):
        """Store entity with basic fields."""
        # Track the type builtin first
        type_id = FxtrID.builtin("test")
        memory_arena.track_builtin(type_id)

        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "$fxtr.type": RawWeakFxtrID(inner_id=type_id),
            "name": "test",
            "value": 42,
        }
        node_id = memory_arena.store(entity)
        assert node_id is not None
        assert memory_arena.is_tracked(node_id)

    def test_store_returns_d_node(self, memory_arena: MemoryArena, ground_context: FxtrID):
        """Stored ground entity returns d-node ID."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        node_id = memory_arena.store(entity)
        assert node_id.get_tag() == "d"

    def test_store_same_data_same_id(self, memory_arena: MemoryArena, ground_context: FxtrID):
        """Identical entities get same ID (content-addressed)."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        id1 = memory_arena.store(entity)
        id2 = memory_arena.store(entity)
        assert id1 == id2

    def test_store_different_data_different_id(
        self, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """Different entities get different IDs."""
        entity1: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test1",
        }
        entity2: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test2",
        }
        id1 = memory_arena.store(entity1)
        id2 = memory_arena.store(entity2)
        assert id1 != id2

    def test_store_nested_entities(self, memory_arena: MemoryArena, ground_context: FxtrID):
        """Entity containing refs to other entities."""
        # First store a child entity
        child: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "child_data": "child",
        }
        child_id = memory_arena.store(child)

        # Store parent referencing child
        parent: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "child_ref": RawStrongFxtrID(inner_id=child_id),
        }
        parent_id = memory_arena.store(parent)
        assert memory_arena.is_tracked(parent_id)
        assert memory_arena.is_tracked(child_id)

    def test_store_requires_tracked_refs(self, memory_arena: MemoryArena, ground_context: FxtrID):
        """Cannot store entity with untracked refs."""
        # Try to store entity referencing untracked node
        untracked_id = FxtrID.builtin("untracked")
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "ref": RawWeakFxtrID(inner_id=untracked_id),
        }
        with pytest.raises(ValueError, match="not tracked"):
            memory_arena.store(entity)


# =============================================================================
# Loading Ground Entities (d-nodes)
# =============================================================================


class TestLoadGroundEntities:
    """Tests for loading ground (d-node) entities."""

    @pytest.mark.anyio
    async def test_load_simple_entity(self, memory_arena: MemoryArena, ground_context: FxtrID):
        """Load stored entity returns same data."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "test",
            "value": 42,
        }
        node_id = memory_arena.store(entity)
        loaded = await memory_arena.load(node_id)

        assert loaded["name"] == "test"
        assert loaded["value"] == 42

    @pytest.mark.anyio
    async def test_load_tracks_children(self, memory_arena: MemoryArena, ground_context: FxtrID):
        """Loading entity tracks all its child refs."""
        # Store a child
        child: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "child_data": "child",
        }
        child_id = memory_arena.store(child)

        # Store parent
        parent: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "child_ref": RawStrongFxtrID(inner_id=child_id),
        }
        parent_id = memory_arena.store(parent)

        # Create a new arena to test load tracking
        new_arena = memory_arena._store.new_empty_arena()
        try:
            # Copy just the parent to new arena
            new_arena._track_from_store(parent_id)
            assert new_arena.is_tracked(parent_id)
            assert not new_arena.is_tracked(child_id)  # Child not tracked yet

            # Load parent - should track child
            await new_arena.load(parent_id)
            assert new_arena.is_tracked(child_id)  # Now tracked
        finally:
            new_arena.close()

    @pytest.mark.anyio
    async def test_load_untracked_raises(self, memory_arena: MemoryArena):
        """Loading untracked entity raises."""
        untracked_id = FxtrID.builtin("untracked")
        with pytest.raises(ValueError, match="not tracked"):
            await memory_arena.load(untracked_id)

    @pytest.mark.anyio
    async def test_load_roundtrip(self, memory_arena: MemoryArena, ground_context: FxtrID):
        """store -> load roundtrips data correctly."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "int_val": 42,
            "str_val": "hello",
            "list_val": [1, 2, 3],
            "dict_val": {"a": 1, "b": 2},
            "bool_val": True,
            "bytes_val": b"bytes",
        }
        node_id = memory_arena.store(entity)
        loaded = await memory_arena.load(node_id)

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
    async def test_store_n_node(self, local_fxtr_arena: MemoryArena):
        """Entity with c-node context creates n-node."""
        # Track builtins first
        ctx_type = FxtrID.builtin("ctx")
        test_type = FxtrID.builtin("test")
        local_fxtr_arena.track_builtin(ctx_type)
        local_fxtr_arena.track_builtin(test_type)
        ground = FxtrID.from_tag_and_payload(".", b"")

        # Create context data manually (not using DataclassEntity to avoid needing all the setup)
        context_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
            "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
            "value": RawWeakFxtrID(inner_id=ground),
            "ctx_key": "test_ctx",
            "metadata": {"version": 1},
        }
        c_node_id = local_fxtr_arena.create_c_node(context_data)

        # Wrap the test type builtin as a u-node so it can be a child of an n-node
        # (n-nodes require all children to be provenance nodes that unwrap)
        u_test_type = local_fxtr_arena.create_u_node(test_type)

        # Store an entity with c-node context
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=c_node_id),
            "$fxtr.type": RawWeakFxtrID(inner_id=u_test_type),
            "data": "provenance data",
        }
        n_node_id = local_fxtr_arena.store(entity)
        assert n_node_id.get_tag() == "n"

    @pytest.mark.anyio
    async def test_n_node_unwraps_to_d_node(self, local_fxtr_arena: MemoryArena):
        """n-node unwraps to corresponding d-node."""
        # Track builtins first
        ctx_type = FxtrID.builtin("ctx")
        test_type = FxtrID.builtin("test")
        local_fxtr_arena.track_builtin(ctx_type)
        local_fxtr_arena.track_builtin(test_type)
        ground = FxtrID.from_tag_and_payload(".", b"")

        # Create context data manually
        context_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
            "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
            "value": RawWeakFxtrID(inner_id=ground),
            "ctx_key": "test_ctx",
            "metadata": {"version": 1},
        }
        c_node_id = local_fxtr_arena.create_c_node(context_data)

        # Wrap the test type builtin as a u-node
        u_test_type = local_fxtr_arena.create_u_node(test_type)

        # Store an entity with c-node context
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=c_node_id),
            "$fxtr.type": RawWeakFxtrID(inner_id=u_test_type),
            "data": "provenance data",
        }
        n_node_id = local_fxtr_arena.store(entity)

        # Unwrap the n-node
        d_node_id = local_fxtr_arena.unwrap_provenance(n_node_id)
        assert d_node_id is not None
        assert d_node_id.get_tag() == "d"


# =============================================================================
# Context Nodes (c-nodes)
# =============================================================================


class TestContextNodes:
    """Tests for context (c-node) operations."""

    @pytest.mark.anyio
    async def test_create_c_node(self, local_fxtr_arena: MemoryArena):
        """create_c_node() creates c-node from context data."""
        ground = FxtrID.from_tag_and_payload(".", b"")
        ctx_type = FxtrID.builtin("ctx")
        local_fxtr_arena.track_builtin(ctx_type)

        context_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
            "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
            "value": RawWeakFxtrID(inner_id=ground),
            "ctx_key": "test_ctx",
            "metadata": {},
        }
        c_node_id = local_fxtr_arena.create_c_node(context_data)
        assert c_node_id.get_tag() == "c"
        assert local_fxtr_arena.is_tracked(c_node_id)

    @pytest.mark.anyio
    async def test_c_node_requires_value_field(self, local_fxtr_arena: MemoryArena):
        """context_data must have 'value' field."""
        ground = FxtrID.from_tag_and_payload(".", b"")
        context_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
            "ctx_key": "test_ctx",
            "metadata": {},
            # Missing "value" field
        }
        with pytest.raises(ValueError, match="value"):
            local_fxtr_arena.create_c_node(context_data)

    @pytest.mark.anyio
    async def test_c_node_unwraps_to_value(self, local_fxtr_arena: MemoryArena):
        """c-node unwraps to its 'value' field."""
        ground = FxtrID.from_tag_and_payload(".", b"")
        ctx_type = FxtrID.builtin("ctx")
        local_fxtr_arena.track_builtin(ctx_type)

        context_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
            "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
            "value": RawWeakFxtrID(inner_id=ground),
            "ctx_key": "test_ctx",
            "metadata": {},
        }
        c_node_id = local_fxtr_arena.create_c_node(context_data)
        unwrapped = local_fxtr_arena.unwrap_provenance(c_node_id)
        assert unwrapped == ground

    @pytest.mark.anyio
    async def test_c_node_to_context_data(self, local_fxtr_arena: MemoryArena):
        """c_node_to_context_data() returns the d-node."""
        ground = FxtrID.from_tag_and_payload(".", b"")
        ctx_type = FxtrID.builtin("ctx")
        local_fxtr_arena.track_builtin(ctx_type)

        context_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
            "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
            "value": RawWeakFxtrID(inner_id=ground),
            "ctx_key": "test_ctx",
            "metadata": {},
        }
        c_node_id = local_fxtr_arena.create_c_node(context_data)
        d_node_id = local_fxtr_arena.c_node_to_context_data(c_node_id)
        assert d_node_id.get_tag() == "d"
        assert local_fxtr_arena.is_tracked(d_node_id)


# =============================================================================
# Path Nodes (p-nodes)
# =============================================================================


class TestPathNodes:
    """Tests for path (p-node) operations."""

    @pytest.mark.anyio
    async def test_create_p_node(self, local_fxtr_arena: MemoryArena, ground_context: FxtrID):
        """create_p_node() creates p-node from pathroot data."""
        # Track builtin first
        pathroot_type = FxtrID.builtin("pathroot")
        local_fxtr_arena.track_builtin(pathroot_type)

        # First create a root entity to wrap
        root_entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "root",
        }
        root_id = local_fxtr_arena.store(root_entity)

        # Create pathroot data
        pathroot_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "$fxtr.type": RawWeakFxtrID(inner_id=pathroot_type),
            "value": RawWeakFxtrID(inner_id=root_id),
            "ctx_key": "test_path",
            "metadata": {},
        }
        p_node_id = local_fxtr_arena.create_p_node(pathroot_data)
        assert p_node_id.get_tag() == "p"
        assert local_fxtr_arena.is_tracked(p_node_id)

    @pytest.mark.anyio
    async def test_p_node_unwraps_to_root(
        self, local_fxtr_arena: MemoryArena, ground_context: FxtrID
    ):
        """p-node with empty path unwraps to root."""
        # Track builtin first
        pathroot_type = FxtrID.builtin("pathroot")
        local_fxtr_arena.track_builtin(pathroot_type)

        # Create root entity
        root_entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "root",
        }
        root_id = local_fxtr_arena.store(root_entity)

        # Create pathroot data
        pathroot_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "$fxtr.type": RawWeakFxtrID(inner_id=pathroot_type),
            "value": RawWeakFxtrID(inner_id=root_id),
            "ctx_key": "test_path",
            "metadata": {},
        }
        p_node_id = local_fxtr_arena.create_p_node(pathroot_data)
        unwrapped = local_fxtr_arena.unwrap_provenance(p_node_id)
        assert unwrapped == root_id

    @pytest.mark.anyio
    async def test_p_node_to_pathroot(self, local_fxtr_arena: MemoryArena, ground_context: FxtrID):
        """p_node_to_pathroot() returns the pathroot d-node."""
        # Track builtin first
        pathroot_type = FxtrID.builtin("pathroot")
        local_fxtr_arena.track_builtin(pathroot_type)

        # Create root entity
        root_entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "root",
        }
        root_id = local_fxtr_arena.store(root_entity)

        # Create pathroot data
        pathroot_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "$fxtr.type": RawWeakFxtrID(inner_id=pathroot_type),
            "value": RawWeakFxtrID(inner_id=root_id),
            "ctx_key": "test_path",
            "metadata": {},
        }
        p_node_id = local_fxtr_arena.create_p_node(pathroot_data)
        pathroot_id = local_fxtr_arena.p_node_to_pathroot(p_node_id)
        assert pathroot_id.get_tag() == "d"
        assert local_fxtr_arena.is_tracked(pathroot_id)

    @pytest.mark.anyio
    async def test_load_p_node_creates_children(
        self, local_fxtr_arena: MemoryArena, ground_context: FxtrID
    ):
        """Loading p-node creates p-node children with extended paths."""
        # Track builtin first
        pathroot_type = FxtrID.builtin("pathroot")
        local_fxtr_arena.track_builtin(pathroot_type)

        # Create an entity with child refs
        child1: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "child": "1",
        }
        child1_id = local_fxtr_arena.store(child1)

        child2: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "child": "2",
        }
        child2_id = local_fxtr_arena.store(child2)

        root_entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "children": [
                RawStrongFxtrID(inner_id=child1_id),
                RawStrongFxtrID(inner_id=child2_id),
            ],
        }
        root_id = local_fxtr_arena.store(root_entity)

        # Create pathroot
        pathroot_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "$fxtr.type": RawWeakFxtrID(inner_id=pathroot_type),
            "value": RawWeakFxtrID(inner_id=root_id),
            "ctx_key": "test_path",
            "metadata": {},
        }
        p_node_id = local_fxtr_arena.create_p_node(pathroot_data)

        # Load p-node - should create p-node children
        loaded = await local_fxtr_arena.load(p_node_id)
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

    def test_create_u_node(self, memory_arena: MemoryArena, ground_context: FxtrID):
        """create_u_node() creates u-node wrapping data node."""
        # Store a data node
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        d_node_id = memory_arena.store(entity)

        # Create u-node
        u_node_id = memory_arena.create_u_node(d_node_id)
        assert u_node_id.get_tag() == "u"
        assert memory_arena.is_tracked(u_node_id)

    def test_u_node_unwraps(self, memory_arena: MemoryArena, ground_context: FxtrID):
        """u-node unwraps to wrapped node."""
        # Store a data node
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        d_node_id = memory_arena.store(entity)

        # Create u-node
        u_node_id = memory_arena.create_u_node(d_node_id)
        unwrapped = memory_arena.unwrap_provenance(u_node_id)
        assert unwrapped == d_node_id

    @pytest.mark.anyio
    async def test_load_u_node(self, memory_arena: MemoryArena, ground_context: FxtrID):
        """Loading u-node wraps children as u-nodes."""
        # Create entity with child ref
        child: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "child": "data",
        }
        child_id = memory_arena.store(child)

        root_entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "child_ref": RawStrongFxtrID(inner_id=child_id),
        }
        root_id = memory_arena.store(root_entity)

        # Create u-node
        u_node_id = memory_arena.create_u_node(root_id)

        # Load u-node
        loaded = await memory_arena.load(u_node_id)
        child_ref = loaded["child_ref"]
        assert isinstance(child_ref, (RawStrongFxtrID, RawWeakFxtrID))
        assert child_ref.inner_id.get_tag() == "u"


# =============================================================================
# Unwrapping Provenance
# =============================================================================


class TestUnwrapProvenance:
    """Tests for unwrap_provenance operations."""

    def test_unwrap_d_node_returns_none(self, memory_arena: MemoryArena, ground_context: FxtrID):
        """Ground d-nodes return None on unwrap."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        d_node_id = memory_arena.store(entity)
        assert memory_arena.unwrap_provenance(d_node_id) is None

    def test_unwrap_here_node_returns_none(self, memory_arena: MemoryArena, ground_context: FxtrID):
        """'.' returns None on unwrap."""
        assert memory_arena.unwrap_provenance(ground_context) is None

    def test_unwrap_builtin_returns_none(self, memory_arena: MemoryArena):
        """'!' builtin returns None on unwrap."""
        builtin = FxtrID.builtin("test")
        memory_arena.track_builtin(builtin)
        assert memory_arena.unwrap_provenance(builtin) is None


# =============================================================================
# Arena Abandonment
# =============================================================================


class TestArenaAbandonment:
    """Tests for arena abandon operations."""

    def test_abandon_removes_from_tracked(self, memory_arena: MemoryArena, ground_context: FxtrID):
        """Abandoned node is no longer tracked."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        node_id = memory_arena.store(entity)
        assert memory_arena.is_tracked(node_id)

        memory_arena.abandon(node_id)
        assert not memory_arena.is_tracked(node_id)

    def test_abandon_untracked_raises(self, memory_arena: MemoryArena):
        """Cannot abandon untracked node."""
        untracked = FxtrID.builtin("not_tracked")
        with pytest.raises(ValueError, match="not tracked"):
            memory_arena.abandon(untracked)


# =============================================================================
# Arena Copying
# =============================================================================


class TestArenaCopying:
    """Tests for arena copying operations."""

    def test_copy_to_new_arena(self, memory_arena: MemoryArena, ground_context: FxtrID):
        """copy_to_new_arena() creates independent arena."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        node_id = memory_arena.store(entity)

        new_arena = memory_arena.copy_to_new_arena([node_id])
        try:
            assert new_arena.is_tracked(node_id)
            assert new_arena is not memory_arena
        finally:
            new_arena.close()

    def test_copied_arena_independent(self, memory_arena: MemoryArena, ground_context: FxtrID):
        """Changes to original don't affect copy."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        node_id = memory_arena.store(entity)

        new_arena = memory_arena.copy_to_new_arena([node_id])
        try:
            # Abandon from original
            memory_arena.abandon(node_id)
            assert not memory_arena.is_tracked(node_id)
            # Copy still has it
            assert new_arena.is_tracked(node_id)
        finally:
            new_arena.close()

    @pytest.mark.anyio
    async def test_copy_from_arena(self, memory_store: MemoryStore, ground_context: FxtrID):
        """copy_from_arena() imports entities from another arena."""
        arena1 = memory_store.new_empty_arena()
        arena2 = memory_store.new_empty_arena()
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
# Error Cases
# =============================================================================


class TestErrorCases:
    """Tests for error conditions."""

    def test_store_non_dict_raises(self, memory_arena: MemoryArena):
        """Storing non-dict raises ValueError."""
        with pytest.raises((ValueError, AssertionError)):
            memory_arena.store([1, 2, 3])  # type: ignore

    def test_store_missing_ctx_raises(self, memory_arena: MemoryArena):
        """Missing $fxtr.ctx raises ValueError."""
        with pytest.raises((ValueError, AssertionError)):
            memory_arena.store({"data": "test"})

    @pytest.mark.anyio
    async def test_load_contentless_raises(self, memory_arena: MemoryArena, ground_context: FxtrID):
        """Loading contentless node (., !) raises."""
        with pytest.raises(ValueError, match="contentless"):
            await memory_arena.load(ground_context)

        builtin = FxtrID.builtin("test")
        memory_arena.track_builtin(builtin)
        with pytest.raises(ValueError, match="contentless"):
            await memory_arena.load(builtin)


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
        arena = memory_store.new_empty_arena()

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

    def test_empty_entity(self, memory_arena: MemoryArena, ground_context: FxtrID):
        """Entity with only $fxtr.ctx."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
        }
        node_id = memory_arena.store(entity)
        assert node_id.get_tag() == "d"

    def test_large_entity(self, memory_arena: MemoryArena, ground_context: FxtrID):
        """Entity with many fields."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
        }
        for i in range(100):
            entity[f"field_{i}"] = f"value_{i}"
        node_id = memory_arena.store(entity)
        assert memory_arena.is_tracked(node_id)

    @pytest.mark.anyio
    async def test_deeply_nested_entity(self, memory_arena: MemoryArena, ground_context: FxtrID):
        """Entity with deep nesting."""
        # Build a deeply nested structure
        nested = {"level": 10}
        for i in range(9, -1, -1):
            nested = {"level": i, "inner": nested}

        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "nested": nested,
        }
        node_id = memory_arena.store(entity)
        loaded = await memory_arena.load(node_id)
        assert loaded["nested"]["level"] == 0

    def test_entity_with_all_primitive_types(
        self, memory_arena: MemoryArena, ground_context: FxtrID
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
        node_id = memory_arena.store(entity)
        assert memory_arena.is_tracked(node_id)

    @pytest.mark.anyio
    async def test_entity_with_bytes_field(self, memory_arena: MemoryArena, ground_context: FxtrID):
        """Binary data in entity."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "binary": b"\x00\x01\x02\xff",
        }
        node_id = memory_arena.store(entity)
        loaded = await memory_arena.load(node_id)
        assert loaded["binary"] == b"\x00\x01\x02\xff"


# =============================================================================
# Integration: Store/Load/Unwrap Flow
# =============================================================================


class TestIntegrationFlow:
    """Integration tests for complete workflows."""

    @pytest.mark.anyio
    async def test_full_provenance_flow(self, local_fxtr_arena: MemoryArena):
        """Create context, store n-node, unwrap, verify."""
        ground = FxtrID.from_tag_and_payload(".", b"")

        # Track builtins first
        ctx_type = FxtrID.builtin("ctx")
        test_type = FxtrID.builtin("test")
        local_fxtr_arena.track_builtin(ctx_type)
        local_fxtr_arena.track_builtin(test_type)

        # Create context data
        context_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
            "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
            "value": RawWeakFxtrID(inner_id=ground),
            "ctx_key": "full_flow",
            "metadata": {"test": True},
        }
        c_node_id = local_fxtr_arena.create_c_node(context_data)

        # Wrap the test type builtin as a u-node for use in n-node
        u_test_type = local_fxtr_arena.create_u_node(test_type)

        # Store n-node with context
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=c_node_id),
            "$fxtr.type": RawWeakFxtrID(inner_id=u_test_type),
            "name": "provenance_entity",
            "value": 123,
        }
        n_node_id = local_fxtr_arena.store(entity)

        # Verify it's an n-node
        assert n_node_id.get_tag() == "n"

        # Unwrap to d-node
        d_node_id = local_fxtr_arena.unwrap_provenance(n_node_id)
        assert d_node_id is not None
        assert d_node_id.get_tag() == "d"

        # Load both and verify data matches
        n_loaded = await local_fxtr_arena.load(n_node_id)
        d_loaded = await local_fxtr_arena.load(d_node_id)

        assert n_loaded["name"] == "provenance_entity"
        assert d_loaded["name"] == "provenance_entity"

    @pytest.mark.anyio
    async def test_p_node_path_traversal(
        self, local_fxtr_arena: MemoryArena, ground_context: FxtrID
    ):
        """Create p-node tree, traverse via loads."""
        # Track builtin first
        pathroot_type = FxtrID.builtin("pathroot")
        local_fxtr_arena.track_builtin(pathroot_type)

        # Create a tree of entities
        leaf1: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "type": "leaf",
            "value": 1,
        }
        leaf1_id = local_fxtr_arena.store(leaf1)

        leaf2: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "type": "leaf",
            "value": 2,
        }
        leaf2_id = local_fxtr_arena.store(leaf2)

        branch: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "type": "branch",
            "left": RawStrongFxtrID(inner_id=leaf1_id),
            "right": RawStrongFxtrID(inner_id=leaf2_id),
        }
        branch_id = local_fxtr_arena.store(branch)

        root: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "type": "root",
            "child": RawStrongFxtrID(inner_id=branch_id),
        }
        root_id = local_fxtr_arena.store(root)

        # Create p-node for root
        pathroot_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "$fxtr.type": RawWeakFxtrID(inner_id=pathroot_type),
            "value": RawWeakFxtrID(inner_id=root_id),
            "ctx_key": "tree_path",
            "metadata": {},
        }
        p_root_id = local_fxtr_arena.create_p_node(pathroot_data)

        # Load root p-node
        p_root_loaded = await local_fxtr_arena.load(p_root_id)
        assert p_root_loaded["type"] == "root"

        # Navigate to child
        p_child_ref = p_root_loaded["child"]
        assert isinstance(p_child_ref, (RawStrongFxtrID, RawWeakFxtrID))
        p_child_id = p_child_ref.inner_id
        assert p_child_id.get_tag() == "p"

        # Load child p-node
        p_child_loaded = await local_fxtr_arena.load(p_child_id)
        assert p_child_loaded["type"] == "branch"
