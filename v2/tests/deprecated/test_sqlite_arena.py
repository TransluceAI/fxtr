"""Tests for fxtr2/data/sqlite_arena.py."""

from __future__ import annotations

import dataclasses
import logging
import typing
import uuid
from typing import Any

import pytest

from fxtr2.data.deprecated.sqlite_arena import (
    DB_USER_VERSION,
    GARBAGE_SESSION_ID,
    SQLiteFxtrSession,
    SQLiteNamespaceArena,
    SQLiteNamespaceBackend,
    create_fxtr_engine,
    init_database,
)
from fxtr2.data.entity_core import (
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawStrongFxtrID,
    RawWeakFxtrID,
)
from fxtr2.data.memory_arena import MemoryArena, MemoryStore

logger = logging.getLogger(__name__)

# =============================================================================
# Database Initialization
# =============================================================================


class TestDatabaseInitialization:
    """Tests for database initialization."""

    @pytest.mark.anyio
    async def test_init_database_creates_tables(self, tmp_db_url: str):
        """init_database() creates all required tables."""
        engine = create_fxtr_engine(tmp_db_url)
        try:
            await init_database(engine)

            # Verify tables exist by querying
            from sqlalchemy import text

            async with engine.read_txn_engine.begin() as conn:
                # Check session table exists
                result = await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table' AND name='session'")
                )
                assert result.fetchone() is not None

                # Check entity table exists
                result = await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table' AND name='entity'")
                )
                assert result.fetchone() is not None

                # Check namespace table exists
                result = await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table' AND name='namespace'")
                )
                assert result.fetchone() is not None
        finally:
            await engine.root_engine.dispose()

    @pytest.mark.anyio
    async def test_init_database_idempotent(self, tmp_db_url: str):
        """Calling init_database() twice is safe."""
        engine = create_fxtr_engine(tmp_db_url)
        try:
            await init_database(engine)
            await init_database(engine)  # Second call should not raise
        finally:
            await engine.root_engine.dispose()

    @pytest.mark.anyio
    async def test_init_database_creates_garbage_session(self, tmp_db_url: str):
        """Session 0 (garbage session) is created."""
        engine = create_fxtr_engine(tmp_db_url)
        try:
            await init_database(engine)

            from sqlalchemy import text

            async with engine.read_txn_engine.begin() as conn:
                result = await conn.execute(text("SELECT id, is_garbage FROM session WHERE id = 0"))
                row = result.fetchone()
                assert row is not None
                assert row[0] == GARBAGE_SESSION_ID
                assert row[1] == True  # is_garbage
        finally:
            await engine.root_engine.dispose()

    @pytest.mark.anyio
    async def test_database_user_version(self, tmp_db_url: str):
        """User version pragma is set."""
        engine = create_fxtr_engine(tmp_db_url)
        try:
            await init_database(engine)

            from sqlalchemy import text

            async with engine.read_txn_engine.begin() as conn:
                result = await conn.execute(text("PRAGMA user_version"))
                row = result.fetchone()
                assert row is not None
                assert row[0] == DB_USER_VERSION
        finally:
            await engine.root_engine.dispose()


# =============================================================================
# SQLiteFxtrSession Lifecycle
# =============================================================================


class TestSQLiteFxtrSessionLifecycle:
    """Tests for SQLiteFxtrSession lifecycle management."""

    @pytest.mark.anyio
    async def test_session_open_creates_context(self, tmp_db_url: str):
        """SQLiteFxtrSession.open() works as context manager."""
        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            assert session is not None
            assert isinstance(session, SQLiteFxtrSession)

    @pytest.mark.anyio
    async def test_session_is_active(self, tmp_db_url: str):
        """is_active() returns True for active session."""
        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            assert session.is_active()

    @pytest.mark.anyio
    async def test_session_not_active_after_close(self, tmp_db_url: str):
        """Session is not active after close."""
        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            assert session.is_active()
        # After context exit, session should be closed
        assert not session.is_active()

    @pytest.mark.anyio
    async def test_session_has_internal_id(self, tmp_db_url: str):
        """Session gets an internal ID after start."""
        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            assert session._internal_sid is not None
            assert session._internal_sid > 0  # 0 is garbage session


# =============================================================================
# Namespace Operations
# =============================================================================


class TestNamespaceOperations:
    """Tests for namespace operations."""

    @pytest.mark.anyio
    async def test_create_user_namespace(self, tmp_db_url: str):
        """create_or_retrieve_user_namespace() creates new namespace."""
        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            assert ns is not None
            assert isinstance(ns, SQLiteNamespaceBackend)

    @pytest.mark.anyio
    async def test_retrieve_existing_namespace(self, tmp_db_url: str):
        """Returns existing namespace by name."""
        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns1 = await session.create_or_retrieve_user_namespace("test_namespace")
            ns2 = await session.create_or_retrieve_user_namespace("test_namespace")
            # Should get same namespace (same internal ID)
            assert ns1._namespace_internal_id == ns2._namespace_internal_id

    @pytest.mark.anyio
    async def test_namespace_has_uuid(self, tmp_db_url: str):
        """Namespace gets unique UUID."""
        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            assert ns._namespace_id is not None
            assert isinstance(ns._namespace_id, uuid.UUID)

    @pytest.mark.anyio
    async def test_multiple_namespaces(self, tmp_db_url: str):
        """Can create multiple namespaces."""
        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns1 = await session.create_or_retrieve_user_namespace("namespace1")
            ns2 = await session.create_or_retrieve_user_namespace("namespace2")
            assert ns1._namespace_internal_id != ns2._namespace_internal_id
            assert ns1._namespace_id != ns2._namespace_id


# =============================================================================
# Pinning Entities
# =============================================================================


class TestPinningEntities:
    """Tests for entity pinning operations."""

    @pytest.mark.anyio
    async def test_pin_entity_from_arena(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """pin_entity_from_arena() copies and pins."""
        # Store entity in memory arena
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "test_entity",
            "value": 42,
        }
        entity_id = memory_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("test_pin", entity_id, memory_arena)

    @pytest.mark.anyio
    async def test_pin_duplicate_name_raises(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """Duplicate pin name raises KeyError when overwrite=False."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = memory_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("test_pin", entity_id, memory_arena)
            with pytest.raises(KeyError):
                await ns.pin_entity_from_arena("test_pin", entity_id, memory_arena)

    @pytest.mark.anyio
    async def test_pin_overwrite_replaces_existing(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """pin_entity_from_arena with overwrite=True replaces existing pin."""
        entity1: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "original",
        }
        entity1_id = memory_arena.store(entity1)

        entity2: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "replacement",
        }
        entity2_id = memory_arena.store(entity2)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("test_pin", entity1_id, memory_arena)

            # Overwrite with entity2
            await ns.pin_entity_from_arena("test_pin", entity2_id, memory_arena, overwrite=True)

            # Retrieve should return entity2
            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            assert retrieved_id == entity2_id
            loaded = await typing.cast(Any, arena.load(retrieved_id))
            assert loaded["data"] == "replacement"

    @pytest.mark.anyio
    async def test_pin_overwrite_with_same_entity(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """pin_entity_from_arena with overwrite=True works even for same entity."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = memory_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("test_pin", entity_id, memory_arena)

            # Overwrite with same entity should succeed
            await ns.pin_entity_from_arena("test_pin", entity_id, memory_arena, overwrite=True)

            # Retrieve should still work
            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            assert retrieved_id == entity_id

    @pytest.mark.anyio
    async def test_pin_overwrite_false_is_default(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """Explicit overwrite=False behaves same as default."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = memory_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("test_pin", entity_id, memory_arena)
            with pytest.raises(KeyError):
                await ns.pin_entity_from_arena("test_pin", entity_id, memory_arena, overwrite=False)

    @pytest.mark.anyio
    async def test_unpin_entity(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """unpin_entity() removes pin."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = memory_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("test_pin", entity_id, memory_arena)
            await ns.unpin_entity("test_pin")

            # Verify pin is gone by trying to unpin again
            with pytest.raises(KeyError):
                await ns.unpin_entity("test_pin")


# =============================================================================
# Retrieving Pinned Entities
# =============================================================================


class TestRetrievingPinnedEntities:
    """Tests for retrieving pinned entities."""

    @pytest.mark.anyio
    async def test_retrieve_pinned_entity(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """retrieve_pinned() returns entity ID and arena."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "test_entity",
            "value": 42,
        }
        entity_id = memory_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("test_pin", entity_id, memory_arena)

            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            assert retrieved_id == entity_id
            assert arena is not None
            assert isinstance(arena, SQLiteNamespaceArena)

    @pytest.mark.anyio
    async def test_retrieve_nonexistent_raises(self, tmp_db_url: str):
        """Retrieving unknown pin raises."""
        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            with pytest.raises(KeyError):
                await ns.retrieve_pinned("nonexistent_pin")

    @pytest.mark.anyio
    async def test_retrieved_entity_loadable(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """Retrieved entity can be loaded."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "test_entity",
            "value": 42,
        }
        entity_id = memory_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("test_pin", entity_id, memory_arena)

            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            loaded = await arena.load(retrieved_id)
            loaded = typing.cast(Any, loaded)
            assert loaded["name"] == "test_entity"
            assert loaded["value"] == 42


# =============================================================================
# SQLiteNamespaceArena Operations
# =============================================================================


class TestSQLiteNamespaceArenaOperations:
    """Tests for SQLiteNamespaceArena operations."""

    @pytest.mark.anyio
    async def test_arena_is_tracked(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """is_tracked() works for known entities."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = memory_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("test_pin", entity_id, memory_arena)

            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            assert arena.is_tracked(retrieved_id)
            # Note: unlike MemoryArena, SQLiteNamespaceArena doesn't track ground context
            # by default - it tracks only entities in the arena

    @pytest.mark.anyio
    async def test_arena_load(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """load() returns entity data."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "key": "value",
        }
        entity_id = memory_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("test_pin", entity_id, memory_arena)

            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            loaded = await arena.load(retrieved_id)
            loaded = typing.cast(Any, loaded)
            assert loaded["key"] == "value"

    @pytest.mark.anyio
    async def test_arena_load_tracks_children(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """Loading entity tracks child entities."""
        # Create a child entity
        child: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "child": True,
        }
        child_id = memory_arena.store(child)

        # Create parent referencing child
        parent: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "child_ref": RawStrongFxtrID(inner_id=child_id),
        }
        parent_id = memory_arena.store(parent)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("test_pin", parent_id, memory_arena)

            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            loaded = await arena.load(retrieved_id)
            loaded = typing.cast(Any, loaded)

            # Child should be tracked after loading parent
            child_ref = loaded["child_ref"]
            assert arena.is_tracked(child_ref.inner_id)

    @pytest.mark.anyio
    async def test_arena_unwrap_provenance_on_d_node(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """unwrap_provenance() on d-node returns None."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = memory_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("test_pin", entity_id, memory_arena)

            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            unwrapped = arena.unwrap_provenance(retrieved_id)
            assert unwrapped is None  # d-nodes don't unwrap

    @pytest.mark.anyio
    async def test_arena_abandon(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """abandon() removes from tracked set."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = memory_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("test_pin", entity_id, memory_arena)

            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            assert arena.is_tracked(retrieved_id)

            arena.abandon(retrieved_id)
            assert not arena.is_tracked(retrieved_id)


# =============================================================================
# Copy From Arena
# =============================================================================


class TestCopyFromArena:
    """Tests for copy_from_arena operations."""

    @pytest.mark.anyio
    async def test_copy_preserves_data(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """Copied entity has same content."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "original",
            "value": 123,
        }
        entity_id = memory_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("test_pin", entity_id, memory_arena)

            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            loaded = await arena.load(retrieved_id)
            loaded = typing.cast(Any, loaded)

            # Same ID
            assert retrieved_id == entity_id
            # Same data
            assert loaded["name"] == "original"
            assert loaded["value"] == 123

    @pytest.mark.anyio
    async def test_copy_handles_dependencies(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """Dependencies are copied correctly."""
        # Create chain of entities
        leaf: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "level": "leaf",
        }
        leaf_id = memory_arena.store(leaf)

        middle: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "level": "middle",
            "child": RawStrongFxtrID(inner_id=leaf_id),
        }
        middle_id = memory_arena.store(middle)

        root: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "level": "root",
            "child": RawStrongFxtrID(inner_id=middle_id),
        }
        root_id = memory_arena.store(root)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("test_pin", root_id, memory_arena)

            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            loaded = await arena.load(retrieved_id)
            loaded = typing.cast(Any, loaded)

            assert loaded["level"] == "root"

            # Can load children
            middle_ref = loaded["child"]
            middle_loaded = await arena.load(middle_ref.inner_id)
            middle_loaded = typing.cast(Any, middle_loaded)
            assert middle_loaded["level"] == "middle"

            leaf_ref = middle_loaded["child"]
            leaf_loaded = await arena.load(leaf_ref.inner_id)
            leaf_loaded = typing.cast(Any, leaf_loaded)
            assert leaf_loaded["level"] == "leaf"


# =============================================================================
# Persistence
# =============================================================================


class TestPersistence:
    """Tests for data persistence across sessions."""

    @pytest.mark.anyio
    async def test_data_persists_across_sessions(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """Close and reopen, data still there."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "persistent": "data",
        }
        entity_id = memory_arena.store(entity)

        # First session: pin entity
        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("test_pin", entity_id, memory_arena)

        # Second session: retrieve entity
        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            loaded = await arena.load(retrieved_id)
            loaded = typing.cast(Any, loaded)
            assert loaded["persistent"] == "data"

    @pytest.mark.anyio
    async def test_namespace_persists(self, tmp_db_url: str):
        """Namespaces persist across sessions."""
        ns_id1 = None

        # First session: create namespace
        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("persistent_namespace")
            ns_id1 = ns._namespace_id

        # Second session: retrieve namespace
        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("persistent_namespace")
            assert ns._namespace_id == ns_id1


# =============================================================================
# Node Type Handling
# =============================================================================


class TestNodeTypeHandling:
    """Tests for handling different node types."""

    @pytest.mark.anyio
    async def test_d_node_storage(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """d-nodes stored with CBOR data."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "type": "d-node",
        }
        entity_id = memory_arena.store(entity)
        assert entity_id.get_tag() == "d"

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("test_pin", entity_id, memory_arena)

            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            loaded = await arena.load(retrieved_id)
            loaded = typing.cast(Any, loaded)
            assert loaded["type"] == "d-node"

    @pytest.mark.anyio
    async def test_builtin_node_handling(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """Builtin nodes are handled correctly."""
        # Track a builtin
        test_type = FxtrID.builtin("test_type")
        memory_arena.track_builtin(test_type)

        # Create entity referencing builtin
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "$fxtr.type": RawWeakFxtrID(inner_id=test_type),
            "data": "test",
        }
        entity_id = memory_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("test_pin", entity_id, memory_arena)

            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            loaded = await arena.load(retrieved_id)
            loaded = typing.cast(Any, loaded)

            # Type ref should be preserved
            type_ref = loaded.get("$fxtr.type")
            assert type_ref is not None
            assert type_ref.inner_id == test_type

    @pytest.mark.anyio
    async def test_u_node_storage(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """u-nodes stored with wrapped reference."""
        # Create d-node
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "inner": "data",
        }
        d_node_id = memory_arena.store(entity)

        # Create u-node wrapping it
        u_node_id = memory_arena.create_u_node(d_node_id)
        assert u_node_id.get_tag() == "u"

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("test_pin", u_node_id, memory_arena)

            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            loaded = await arena.load(retrieved_id)
            loaded = typing.cast(Any, loaded)
            assert loaded["inner"] == "data"

            # Check unwrap
            unwrapped = arena.unwrap_provenance(retrieved_id)
            assert unwrapped == d_node_id


# =============================================================================
# Error Cases
# =============================================================================


class TestMultipleRefsToSameChild:
    """Tests for entities with multiple references to the same child."""

    @pytest.mark.anyio
    async def test_entity_with_multiple_refs_to_same_child(
        self, tmp_db_url: str, memory_store: MemoryStore, ground_context: FxtrID
    ):
        """Entity with multiple refs (2 weak, 1 strong) to same child."""
        # Create a fresh arena for this test
        memory_arena = memory_store.new_empty_arena()

        # Create a child entity
        child: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "child_data": "shared_child",
        }
        child_id = memory_arena.store(child)

        # Create parent with 3 refs to the same child: 2 weak, 1 strong
        parent: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "weak_ref1": RawWeakFxtrID(inner_id=child_id),
            "weak_ref2": RawWeakFxtrID(inner_id=child_id),
            "strong_ref": RawStrongFxtrID(inner_id=child_id),
        }
        parent_id = memory_arena.store(parent)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("test_pin", parent_id, memory_arena)

            # Retrieve and load
            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            loaded = await arena.load(retrieved_id)
            loaded = typing.cast(Any, loaded)
            assert loaded["weak_ref1"].inner_id == child_id
            assert loaded["weak_ref2"].inner_id == child_id
            assert loaded["strong_ref"].inner_id == child_id
        # Session closes here, SQLite arena is cleaned up

        # Explicitly close the memory arena
        memory_arena.close()


# =============================================================================
# Error Cases
# =============================================================================


class TestErrorCases:
    """Tests for error conditions."""

    @pytest.mark.anyio
    async def test_load_unknown_entity_raises(self, tmp_db_url: str, memory_arena: MemoryArena):
        """Loading unknown entity raises."""
        # Create a fake entity ID
        unknown_id = FxtrID.from_tag_and_payload("d", b"unknown_entity_data")

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            arena = ns.new_empty_arena()
            with pytest.raises((ValueError, KeyError)):
                await arena.load(unknown_id)


# =============================================================================
# Integration Tests
# =============================================================================


class TestIntegration:
    """Integration tests for complete workflows."""

    @pytest.mark.anyio
    async def test_full_workflow(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """Create in memory -> copy to sqlite -> retrieve -> load."""
        # Create entity with children in memory
        child: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "type": "child",
            "value": 100,
        }
        child_id = memory_arena.store(child)

        parent: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "type": "parent",
            "child": RawStrongFxtrID(inner_id=child_id),
        }
        parent_id = memory_arena.store(parent)

        # Copy to SQLite
        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("parent", parent_id, memory_arena)

        # Retrieve in new session
        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            retrieved_id, arena = await ns.retrieve_pinned("parent")

            # Load parent
            parent_loaded = await arena.load(retrieved_id)
            parent_loaded = typing.cast(Any, parent_loaded)
            assert parent_loaded["type"] == "parent"

            # Load child
            child_ref = parent_loaded["child"]
            child_loaded = await arena.load(child_ref.inner_id)
            child_loaded = typing.cast(Any, child_loaded)
            assert child_loaded["type"] == "child"
            assert child_loaded["value"] == 100

    @pytest.mark.anyio
    async def test_multiple_pins_same_entity(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """Same entity pinned under multiple names."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "shared",
        }
        entity_id = memory_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("pin1", entity_id, memory_arena)
            await ns.pin_entity_from_arena("pin2", entity_id, memory_arena)

            # Both pins return same entity
            id1, arena1 = await ns.retrieve_pinned("pin1")
            id2, arena2 = await ns.retrieve_pinned("pin2")
            assert id1 == id2 == entity_id

    @pytest.mark.anyio
    async def test_pin_entity_with_deep_deps(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """Entity with many levels of dependencies."""
        # Build a chain of 10 entities
        current_id = None
        for i in range(10):
            if current_id is None:
                entity: FxtrCbor = {
                    "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                    "level": i,
                }
            else:
                entity = {
                    "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                    "level": i,
                    "next": RawStrongFxtrID(inner_id=current_id),
                }
            current_id = memory_arena.store(entity)

        root_id = current_id
        assert root_id is not None

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("deep_chain", root_id, memory_arena)

            # Verify all levels can be loaded
            retrieved_id, arena = await ns.retrieve_pinned("deep_chain")
            current = await arena.load(retrieved_id)
            current = typing.cast(Any, current)
            for i in range(9, -1, -1):
                assert current["level"] == i
                if i > 0:
                    next_ref = current["next"]
                    current = await arena.load(next_ref.inner_id)
                    current = typing.cast(Any, current)


# =============================================================================
# Cross-Namespace Copy Tests
# =============================================================================


class TestCrossNamespaceCopy:
    """Tests for copying entities between namespaces.

    These tests verify that when entities are already in entity tables (from being
    in namespace A) but not in namespace B, they are correctly copied to namespace B
    along with all their dependencies.
    """

    @pytest.mark.anyio
    async def test_copy_entity_from_one_namespace_to_another(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """Entity in namespace A can be copied to namespace B.

        This tests the TrackDeps worklist item: when an entity is in entity tables
        (from namespace A) but not in the target namespace B, TrackDeps must ensure
        all dependencies are also added to namespace B before adding the entity itself.
        """
        # Create parent with child entities
        child1: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "child1",
        }
        child1_id = memory_arena.store(child1)

        child2: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "child2",
        }
        child2_id = memory_arena.store(child2)

        parent: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "parent",
            "child1": RawStrongFxtrID(inner_id=child1_id),
            "child2": RawStrongFxtrID(inner_id=child2_id),
        }
        parent_id = memory_arena.store(parent)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            # Step 1: Pin to namespace A - this copies entities to entity tables
            ns_a = await session.create_or_retrieve_user_namespace("namespace_a")
            await ns_a.pin_entity_from_arena("parent", parent_id, memory_arena)

            # Verify entities are in namespace A
            retrieved_id, arena_a = await ns_a.retrieve_pinned("parent")
            assert retrieved_id == parent_id
            loaded = await arena_a.load(retrieved_id)
            loaded = typing.cast(Any, loaded)
            assert loaded["name"] == "parent"

            # Step 2: Create namespace B and copy the parent from namespace A's arena
            ns_b = await session.create_or_retrieve_user_namespace("namespace_b")

            # Copy from arena_a (which has the parent tracked) to namespace B
            # This exercises TrackDeps: parent and children are in entity tables
            # but not in namespace B
            await ns_b.pin_entity_from_arena("parent_copy", parent_id, arena_a)

            # Step 3: Verify all entities are correctly in namespace B
            retrieved_id_b, arena_b = await ns_b.retrieve_pinned("parent_copy")
            assert retrieved_id_b == parent_id

            loaded_b = await arena_b.load(retrieved_id_b)
            loaded_b = typing.cast(Any, loaded_b)
            assert loaded_b["name"] == "parent"

            # Verify children are accessible
            child1_ref = loaded_b["child1"]
            child1_loaded = await arena_b.load(child1_ref.inner_id)
            child1_loaded = typing.cast(Any, child1_loaded)
            assert child1_loaded["name"] == "child1"

            child2_ref = loaded_b["child2"]
            child2_loaded = await arena_b.load(child2_ref.inner_id)
            child2_loaded = typing.cast(Any, child2_loaded)
            assert child2_loaded["name"] == "child2"

    @pytest.mark.anyio
    async def test_copy_deep_hierarchy_between_namespaces(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """Deep entity hierarchy copied correctly between namespaces.

        Tests that TrackDeps correctly handles recursive dependencies when
        copying a deep chain from one namespace to another.
        """
        # Build a chain: root -> level1 -> level2 -> level3 -> leaf
        leaf: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "level": "leaf",
        }
        leaf_id = memory_arena.store(leaf)

        level3: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "level": "level3",
            "child": RawStrongFxtrID(inner_id=leaf_id),
        }
        level3_id = memory_arena.store(level3)

        level2: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "level": "level2",
            "child": RawStrongFxtrID(inner_id=level3_id),
        }
        level2_id = memory_arena.store(level2)

        level1: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "level": "level1",
            "child": RawStrongFxtrID(inner_id=level2_id),
        }
        level1_id = memory_arena.store(level1)

        root: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "level": "root",
            "child": RawStrongFxtrID(inner_id=level1_id),
        }
        root_id = memory_arena.store(root)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            # Pin to namespace A
            ns_a = await session.create_or_retrieve_user_namespace("namespace_a")
            await ns_a.pin_entity_from_arena("root", root_id, memory_arena)

            # Retrieve from A to get an arena
            _, arena_a = await ns_a.retrieve_pinned("root")

            # Copy to namespace B
            ns_b = await session.create_or_retrieve_user_namespace("namespace_b")
            await ns_b.pin_entity_from_arena("root_copy", root_id, arena_a)

            # Verify entire chain is accessible in namespace B
            retrieved_id, arena_b = await ns_b.retrieve_pinned("root_copy")
            current = await arena_b.load(retrieved_id)
            current = typing.cast(Any, current)
            expected_levels = ["root", "level1", "level2", "level3", "leaf"]

            for expected_level in expected_levels:
                assert current["level"] == expected_level
                if "child" in current:
                    child_ref = current["child"]
                    current = await arena_b.load(child_ref.inner_id)
                    current = typing.cast(Any, current)

    @pytest.mark.anyio
    async def test_copy_diamond_dependency_between_namespaces(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """Diamond dependency pattern copied correctly between namespaces.

        Tests that TrackDeps handles the case where the same entity is a
        dependency of multiple entities (diamond pattern):

            parent
           /      \\
        child1   child2
           \\      /
            shared

        When copying parent from namespace A to B, shared should only be
        added once to namespace B despite being reached via two paths.
        """
        shared: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "shared",
        }
        shared_id = memory_arena.store(shared)

        child1: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "child1",
            "dep": RawStrongFxtrID(inner_id=shared_id),
        }
        child1_id = memory_arena.store(child1)

        child2: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "child2",
            "dep": RawStrongFxtrID(inner_id=shared_id),
        }
        child2_id = memory_arena.store(child2)

        parent: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "parent",
            "left": RawStrongFxtrID(inner_id=child1_id),
            "right": RawStrongFxtrID(inner_id=child2_id),
        }
        parent_id = memory_arena.store(parent)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            # Pin to namespace A
            ns_a = await session.create_or_retrieve_user_namespace("namespace_a")
            await ns_a.pin_entity_from_arena("parent", parent_id, memory_arena)

            # Retrieve from A
            _, arena_a = await ns_a.retrieve_pinned("parent")

            # Copy to namespace B
            ns_b = await session.create_or_retrieve_user_namespace("namespace_b")
            await ns_b.pin_entity_from_arena("parent_copy", parent_id, arena_a)

            # Verify structure in namespace B
            retrieved_id, arena_b = await ns_b.retrieve_pinned("parent_copy")
            parent_loaded = await arena_b.load(retrieved_id)
            parent_loaded = typing.cast(Any, parent_loaded)
            assert parent_loaded["name"] == "parent"

            # Load children
            child1_loaded = await arena_b.load(parent_loaded["left"].inner_id)
            child1_loaded = typing.cast(Any, child1_loaded)
            child2_loaded = await arena_b.load(parent_loaded["right"].inner_id)
            child2_loaded = typing.cast(Any, child2_loaded)
            assert child1_loaded["name"] == "child1"
            assert child2_loaded["name"] == "child2"

            # Both children should reference the same shared entity
            shared_from_child1 = await arena_b.load(child1_loaded["dep"].inner_id)
            shared_from_child2 = await arena_b.load(child2_loaded["dep"].inner_id)
            shared_from_child1 = typing.cast(Any, shared_from_child1)
            shared_from_child2 = typing.cast(Any, shared_from_child2)
            assert shared_from_child1["name"] == "shared"
            assert shared_from_child2["name"] == "shared"
            assert child1_loaded["dep"].inner_id == child2_loaded["dep"].inner_id


# =============================================================================
# Listing Pins
# =============================================================================


class TestListPins:
    """Tests for list_pins() operation."""

    @pytest.mark.anyio
    async def test_list_pins_empty_namespace(self, tmp_db_url: str):
        """list_pins() returns empty dict when namespace has no pins."""
        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            result = await ns.list_pins()
            assert result == {}

    @pytest.mark.anyio
    async def test_list_pins_returns_all_pins(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """list_pins() with empty prefix returns all pins."""
        entity1: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "entity1",
        }
        entity1_id = memory_arena.store(entity1)

        entity2: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "entity2",
        }
        entity2_id = memory_arena.store(entity2)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("pin_a", entity1_id, memory_arena)
            await ns.pin_entity_from_arena("pin_b", entity2_id, memory_arena)

            result = await ns.list_pins()
            assert len(result) == 2
            assert result["pin_a"] == entity1_id
            assert result["pin_b"] == entity2_id

    @pytest.mark.anyio
    async def test_list_pins_with_prefix(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """list_pins() filters by prefix."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = memory_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("data/models:encoder", entity_id, memory_arena)
            await ns.pin_entity_from_arena("data/models:decoder", entity_id, memory_arena)
            await ns.pin_entity_from_arena("data/configs:main", entity_id, memory_arena)
            await ns.pin_entity_from_arena("other_pin", entity_id, memory_arena)

            # Filter by "data/models:" prefix
            result = await ns.list_pins("data/models:")
            assert len(result) == 2
            assert "data/models:encoder" in result
            assert "data/models:decoder" in result
            assert "data/configs:main" not in result
            assert "other_pin" not in result

    @pytest.mark.anyio
    async def test_list_pins_prefix_no_match(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """list_pins() returns empty dict when prefix matches nothing."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = memory_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("my_pin", entity_id, memory_arena)

            result = await ns.list_pins("nonexistent_prefix:")
            assert result == {}

    @pytest.mark.anyio
    async def test_list_pins_prefix_partial_match(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """list_pins() only matches pins that start with the prefix."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = memory_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("prefix_one", entity_id, memory_arena)
            await ns.pin_entity_from_arena("prefix_two", entity_id, memory_arena)
            await ns.pin_entity_from_arena("other_prefix_one", entity_id, memory_arena)

            # "prefix_" should match "prefix_one" and "prefix_two" but not "other_prefix_one"
            result = await ns.list_pins("prefix_")
            assert len(result) == 2
            assert "prefix_one" in result
            assert "prefix_two" in result
            assert "other_prefix_one" not in result

    @pytest.mark.anyio
    async def test_list_pins_different_namespaces(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """list_pins() only returns pins from the current namespace."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = memory_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns_a = await session.create_or_retrieve_user_namespace("namespace_a")
            ns_b = await session.create_or_retrieve_user_namespace("namespace_b")

            await ns_a.pin_entity_from_arena("pin_in_a", entity_id, memory_arena)
            await ns_b.pin_entity_from_arena("pin_in_b", entity_id, memory_arena)

            # Each namespace only sees its own pins
            result_a = await ns_a.list_pins()
            result_b = await ns_b.list_pins()

            assert list(result_a.keys()) == ["pin_in_a"]
            assert list(result_b.keys()) == ["pin_in_b"]

    @pytest.mark.anyio
    async def test_list_pins_after_unpin(
        self, tmp_db_url: str, memory_arena: MemoryArena, ground_context: FxtrID
    ):
        """list_pins() reflects unpinned entities."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = memory_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena("pin_a", entity_id, memory_arena)
            await ns.pin_entity_from_arena("pin_b", entity_id, memory_arena)

            # Initially has both pins
            result = await ns.list_pins()
            assert len(result) == 2

            # Unpin one
            await ns.unpin_entity("pin_a")

            # Now only has one pin
            result = await ns.list_pins()
            assert len(result) == 1
            assert "pin_b" in result
            assert "pin_a" not in result


# =============================================================================
# Store/Abandon Stress Tests
# =============================================================================


@dataclasses.dataclass
class StressTestFixture:
    """Shared fixture data for store/abandon stress tests."""

    memory_arena: MemoryArena
    n_node_id: FxtrID
    d_node_id: FxtrID
    session: SQLiteFxtrSession
    arena: SQLiteNamespaceArena

    async def run_gc_until_done(self) -> int:
        """Run garbage collection until no more garbage is found.

        Returns the number of GC iterations that found garbage.
        """
        count = 0
        while await self.session._clean_up_garbage():
            count += 1
        return count


@pytest.fixture
async def stress_fixture(
    tmp_db_url: str, memory_store: MemoryStore, ground_context: FxtrID
) -> typing.AsyncIterator[StressTestFixture]:
    """Fixture providing memory arena with n/d nodes and SQLite session with GC disabled."""
    # Create memory arena with n-node and d-node
    memory_arena = memory_store.new_empty_arena()

    # Set up builtins for creating the n-node
    ctx_type = FxtrID.builtin("ctx")
    memory_arena.track_builtin(ctx_type)

    # Create context data for a c-node (which makes stored entities become n-nodes)
    context_data: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
        "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
        "value": RawWeakFxtrID(inner_id=ground_context),
        "ctx_key": "test_ctx",
        "metadata": {},
    }
    c_node_id = memory_arena.create_c_node(context_data)

    # Store an entity with c-node context -> creates an n-node
    n_entity: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=c_node_id),
        "data": "test_n_node_data",
    }
    n_node_id = memory_arena.store(n_entity)
    assert n_node_id.get_tag() == "n", f"Expected n-node, got {n_node_id.get_tag()}"

    # Unwrap to get the d-node
    d_node_id = memory_arena.unwrap_provenance(n_node_id)
    assert d_node_id is not None
    assert d_node_id.get_tag() == "d", f"Expected d-node, got {d_node_id.get_tag()}"

    # Create SQLite session with GC disabled
    engine = create_fxtr_engine(tmp_db_url)
    await init_database(engine)

    async with SQLiteFxtrSession.attach_to_engine(
        engine, _should_start_garbage_worker=False
    ) as session:
        ns = await session.create_or_retrieve_user_namespace("test_namespace")
        arena = ns.new_empty_arena()

        yield StressTestFixture(
            memory_arena=memory_arena,
            n_node_id=n_node_id,
            d_node_id=d_node_id,
            session=session,
            arena=arena,
        )

    await engine.root_engine.dispose()


class TestStoreAbandonStress:
    """Stress tests for store/abandon sequences on SQLiteNamespaceArena.

    These tests exercise various orderings of copy_from_arena (store) and abandon
    operations, with controlled timing of pending tasks and garbage collection,
    to isolate and reproduce specific bugs in the tracking logic.

    Key control mechanisms:
    - `_delay_pending_tasks`: Delays pending async tasks until context exits
    - `_should_start_garbage_worker=False`: Disables automatic garbage collection
    - `_clean_up_garbage()`: Manually triggers garbage collection steps
    """

    # -------------------------------------------------------------------------
    # Segment 1: store n, wait, abandon n, wait, re-store n
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    async def test_segment1_store_wait_abandon_wait_restore__gc_after_pending(
        self, stress_fixture: StressTestFixture
    ):
        """Segment 1: store n, wait, abandon n, wait, re-store n.

        GC timing: after pending tasks complete.
        """
        f = stress_fixture

        # Initial state
        assert not f.arena.is_tracked(f.n_node_id)
        assert not f.arena.is_tracked(f.d_node_id)

        # store n
        await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.n_node_id)

        # wait
        await f.arena._wait_for_all_pending_tasks()
        assert f.arena.is_tracked(f.n_node_id)

        # abandon n
        f.arena.abandon(f.n_node_id)
        assert not f.arena.is_tracked(f.n_node_id)

        # wait
        await f.arena._wait_for_all_pending_tasks()
        assert not f.arena.is_tracked(f.n_node_id)

        # GC after pending tasks
        await f.run_gc_until_done()

        # re-store n
        await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.n_node_id)

        await f.arena._wait_for_all_pending_tasks()

    @pytest.mark.anyio
    async def test_segment1_store_wait_abandon_wait_restore__gc_before_pending(
        self, stress_fixture: StressTestFixture
    ):
        """Segment 1: store n, wait, abandon n, wait, re-store n.

        GC timing: before pending tasks complete (using _delay_pending_tasks).
        """
        f = stress_fixture

        # store n
        await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
        await f.arena._wait_for_all_pending_tasks()

        # abandon n, but delay pending tasks
        with f.arena._delay_pending_tasks("abandon delay"):
            f.arena.abandon(f.n_node_id)
            assert not f.arena.is_tracked(f.n_node_id)

            # GC before abandon's pending tasks complete
            await f.run_gc_until_done()

        # Now pending tasks complete (context exit)
        await f.arena._wait_for_all_pending_tasks()

        # re-store n
        await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.n_node_id)

        await f.arena._wait_for_all_pending_tasks()

    # -------------------------------------------------------------------------
    # Segment 2: store n, abandon n (no wait between), wait, re-store n
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    async def test_segment2_store_abandon_wait_restore__gc_after_pending(
        self, stress_fixture: StressTestFixture
    ):
        """Segment 2: store n, abandon n (no wait between), wait, re-store n.

        GC timing: after pending tasks complete.
        """
        f = stress_fixture

        # store n
        await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.n_node_id)

        # abandon n immediately (no wait)
        f.arena.abandon(f.n_node_id)
        assert not f.arena.is_tracked(f.n_node_id)

        # wait
        await f.arena._wait_for_all_pending_tasks()

        # GC after pending tasks
        await f.run_gc_until_done()

        # re-store n
        await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.n_node_id)

        await f.arena._wait_for_all_pending_tasks()

    @pytest.mark.anyio
    async def test_segment2_store_abandon_wait_restore__gc_before_abandon_pending(
        self, stress_fixture: StressTestFixture
    ):
        """Segment 2: store n, abandon n, GC before abandon's pending tasks complete.

        GC timing: after abandon but before abandon's pending tasks complete.
        Uses _delay_pending_tasks on the abandon operation.
        """
        f = stress_fixture

        # store n and wait for it to complete
        await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
        await f.arena._wait_for_all_pending_tasks()
        assert f.arena.is_tracked(f.n_node_id)

        # abandon n, but delay the abandon's pending tasks
        with f.arena._delay_pending_tasks("abandon delay"):
            f.arena.abandon(f.n_node_id)
            assert not f.arena.is_tracked(f.n_node_id)

            # GC while abandon's pending tasks are delayed
            await f.run_gc_until_done()

        # Now abandon's pending tasks complete (context exit)
        await f.arena._wait_for_all_pending_tasks()

        # GC again
        await f.run_gc_until_done()

        # re-store n
        await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.n_node_id)

        await f.arena._wait_for_all_pending_tasks()

    # -------------------------------------------------------------------------
    # Segment 3: Multiple store/abandon cycles without waits, then wait
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    async def test_segment3_multiple_store_abandon_then_wait__gc_after_pending(
        self, stress_fixture: StressTestFixture
    ):
        """Segment 3: store n, abandon n, store n, abandon n, wait, re-store n.

        GC timing: after pending tasks complete.
        """
        f = stress_fixture

        # store n
        await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.n_node_id)

        # abandon n
        f.arena.abandon(f.n_node_id)
        assert not f.arena.is_tracked(f.n_node_id)

        # store n again
        await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.n_node_id)

        # abandon n again
        f.arena.abandon(f.n_node_id)
        assert not f.arena.is_tracked(f.n_node_id)

        # wait
        await f.arena._wait_for_all_pending_tasks()

        # GC after pending tasks
        await f.run_gc_until_done()

        # re-store n
        await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.n_node_id)

        await f.arena._wait_for_all_pending_tasks()

    @pytest.mark.anyio
    async def test_segment3_multiple_store_abandon_then_wait__gc_between_cycles(
        self, stress_fixture: StressTestFixture
    ):
        """Segment 3: store n, abandon n, store n, abandon n, wait, re-store n.

        GC timing: between store/abandon cycles (with delayed pending tasks).
        """
        f = stress_fixture

        # First cycle: store n, abandon n
        await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
        f.arena.abandon(f.n_node_id)

        # GC between cycles (pending tasks still running)
        await f.run_gc_until_done()

        # Second cycle: store n, abandon n
        await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
        f.arena.abandon(f.n_node_id)

        # wait
        await f.arena._wait_for_all_pending_tasks()

        # GC after pending tasks
        await f.run_gc_until_done()

        # re-store n
        await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.n_node_id)

        await f.arena._wait_for_all_pending_tasks()

    # -------------------------------------------------------------------------
    # Segment 4: store n and d, abandon both in order (n then d)
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    async def test_segment4_store_n_d_abandon_n_d__gc_after_pending(
        self, stress_fixture: StressTestFixture
    ):
        """Segment 4: store n, store d, abandon n, abandon d, wait, re-store both.

        GC timing: after pending tasks complete.
        """
        f = stress_fixture

        # store n
        await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.n_node_id)
        assert not f.arena.is_tracked(f.d_node_id)

        # store d
        await f.arena.copy_from_arena(f.d_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.n_node_id)
        assert f.arena.is_tracked(f.d_node_id)

        # abandon n
        f.arena.abandon(f.n_node_id)
        assert not f.arena.is_tracked(f.n_node_id)
        assert f.arena.is_tracked(f.d_node_id)

        # abandon d
        f.arena.abandon(f.d_node_id)
        assert not f.arena.is_tracked(f.n_node_id)
        assert not f.arena.is_tracked(f.d_node_id)

        # wait
        await f.arena._wait_for_all_pending_tasks()

        # GC after pending tasks
        await f.run_gc_until_done()

        # re-store n
        await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.n_node_id)

        # re-store d
        await f.arena.copy_from_arena(f.d_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.d_node_id)

        await f.arena._wait_for_all_pending_tasks()

    # -------------------------------------------------------------------------
    # Segment 5: store n and d, abandon both in reverse order (d then n)
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    async def test_segment5_store_n_d_abandon_d_n__gc_after_pending(
        self, stress_fixture: StressTestFixture
    ):
        """Segment 5: store n, store d, abandon d, abandon n, wait, re-store both.

        GC timing: after pending tasks complete.
        """
        f = stress_fixture

        # store n
        await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
        # store d
        await f.arena.copy_from_arena(f.d_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.n_node_id)
        assert f.arena.is_tracked(f.d_node_id)

        # abandon d first
        f.arena.abandon(f.d_node_id)
        assert f.arena.is_tracked(f.n_node_id)
        assert not f.arena.is_tracked(f.d_node_id)

        # abandon n
        f.arena.abandon(f.n_node_id)
        assert not f.arena.is_tracked(f.n_node_id)
        assert not f.arena.is_tracked(f.d_node_id)

        # wait
        await f.arena._wait_for_all_pending_tasks()

        # GC after pending tasks
        await f.run_gc_until_done()

        # re-store n
        await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.n_node_id)

        # re-store d
        await f.arena.copy_from_arena(f.d_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.d_node_id)

        await f.arena._wait_for_all_pending_tasks()

    # -------------------------------------------------------------------------
    # Segment 6: store d then n, abandon both (d then n)
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    async def test_segment6_store_d_n_abandon_d_n__gc_after_pending(
        self, stress_fixture: StressTestFixture
    ):
        """Segment 6: store d, store n, abandon d, abandon n, wait, re-store both.

        GC timing: after pending tasks complete.
        """
        f = stress_fixture

        # store d first
        await f.arena.copy_from_arena(f.d_node_id, f.memory_arena)
        assert not f.arena.is_tracked(f.n_node_id)
        assert f.arena.is_tracked(f.d_node_id)

        # store n
        await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.n_node_id)
        assert f.arena.is_tracked(f.d_node_id)

        # abandon d
        f.arena.abandon(f.d_node_id)
        assert f.arena.is_tracked(f.n_node_id)
        assert not f.arena.is_tracked(f.d_node_id)

        # abandon n
        f.arena.abandon(f.n_node_id)
        assert not f.arena.is_tracked(f.n_node_id)
        assert not f.arena.is_tracked(f.d_node_id)

        # wait
        await f.arena._wait_for_all_pending_tasks()

        # GC after pending tasks
        await f.run_gc_until_done()

        # re-store d
        await f.arena.copy_from_arena(f.d_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.d_node_id)

        # re-store n
        await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.n_node_id)

        await f.arena._wait_for_all_pending_tasks()

    # -------------------------------------------------------------------------
    # Segment 7: store d then n, abandon both (n then d)
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    async def test_segment7_store_d_n_abandon_n_d__gc_after_pending(
        self, stress_fixture: StressTestFixture
    ):
        """Segment 7: store d, store n, abandon n, abandon d, wait, re-store both.

        GC timing: after pending tasks complete.
        """
        f = stress_fixture

        # store d first
        await f.arena.copy_from_arena(f.d_node_id, f.memory_arena)
        # store n
        await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.n_node_id)
        assert f.arena.is_tracked(f.d_node_id)

        # abandon n first
        f.arena.abandon(f.n_node_id)
        assert not f.arena.is_tracked(f.n_node_id)
        assert f.arena.is_tracked(f.d_node_id)

        # abandon d
        f.arena.abandon(f.d_node_id)
        assert not f.arena.is_tracked(f.n_node_id)
        assert not f.arena.is_tracked(f.d_node_id)

        # wait
        await f.arena._wait_for_all_pending_tasks()

        # GC after pending tasks
        await f.run_gc_until_done()

        # re-store d
        await f.arena.copy_from_arena(f.d_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.d_node_id)

        # re-store n
        await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.n_node_id)

        await f.arena._wait_for_all_pending_tasks()

    # -------------------------------------------------------------------------
    # Loop tests: repeated store/abandon cycles
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    async def test_loop_store_abandon_n_only__gc_after_each_cycle(
        self, stress_fixture: StressTestFixture
    ):
        """Repeatedly store and abandon n-node with GC after each cycle.

        This tests the original failure pattern where repeated store/abandon
        cycles eventually lead to "Entity not found in namespace after copy".
        """
        f = stress_fixture

        for i in range(10):
            # store n
            await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
            assert f.arena.is_tracked(f.n_node_id), f"Cycle {i}: n should be tracked after store"

            # abandon n
            f.arena.abandon(f.n_node_id)
            assert not f.arena.is_tracked(
                f.n_node_id
            ), f"Cycle {i}: n should not be tracked after abandon"

            # wait for pending tasks
            await f.arena._wait_for_all_pending_tasks()

            # GC after each cycle
            await f.run_gc_until_done()

    @pytest.mark.anyio
    async def test_loop_store_abandon_n_only__gc_after_all_cycles(
        self, stress_fixture: StressTestFixture
    ):
        """Repeatedly store and abandon n-node, GC only at the end.

        This tests whether accumulated pending tasks or deferred GC causes issues.
        """
        f = stress_fixture

        for i in range(10):
            # store n
            await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
            assert f.arena.is_tracked(f.n_node_id), f"Cycle {i}: n should be tracked after store"

            # abandon n
            f.arena.abandon(f.n_node_id)
            assert not f.arena.is_tracked(
                f.n_node_id
            ), f"Cycle {i}: n should not be tracked after abandon"

            # wait for pending tasks each cycle
            await f.arena._wait_for_all_pending_tasks()

        # GC only at the end
        await f.run_gc_until_done()

        # Final re-store should work
        await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.n_node_id)
        await f.arena._wait_for_all_pending_tasks()

    @pytest.mark.anyio
    async def test_loop_store_abandon_n_only__no_wait_between_cycles(
        self, stress_fixture: StressTestFixture
    ):
        """Repeatedly store and abandon n-node without waiting between cycles.

        This tests whether rapid store/abandon without waiting causes issues.
        """
        f = stress_fixture

        for i in range(10):
            # store n (no wait after)
            await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
            assert f.arena.is_tracked(f.n_node_id), f"Cycle {i}: n should be tracked after store"

            # abandon n (no wait after)
            f.arena.abandon(f.n_node_id)
            assert not f.arena.is_tracked(
                f.n_node_id
            ), f"Cycle {i}: n should not be tracked after abandon"

        # Wait and GC only at the end
        await f.arena._wait_for_all_pending_tasks()
        await f.run_gc_until_done()

        # Final re-store should work
        await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.n_node_id)
        await f.arena._wait_for_all_pending_tasks()

    @pytest.mark.anyio
    async def test_loop_store_abandon_n_only__gc_between_cycles_no_wait(
        self, stress_fixture: StressTestFixture
    ):
        """Repeatedly store and abandon n-node with GC between but no wait.

        This tests whether GC interleaved with pending tasks causes issues.
        """
        f = stress_fixture

        for i in range(10):
            # store n
            await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
            assert f.arena.is_tracked(f.n_node_id), f"Cycle {i}: n should be tracked after store"

            # abandon n
            f.arena.abandon(f.n_node_id)
            assert not f.arena.is_tracked(
                f.n_node_id
            ), f"Cycle {i}: n should not be tracked after abandon"

            # GC without waiting for pending tasks first
            await f.run_gc_until_done()

        # Wait at the end
        await f.arena._wait_for_all_pending_tasks()

        # Final re-store should work
        await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.n_node_id)
        await f.arena._wait_for_all_pending_tasks()

    @pytest.mark.anyio
    async def test_loop_store_abandon_n_only__delayed_pending_gc_interleaved(
        self, stress_fixture: StressTestFixture
    ):
        """Store/abandon with delayed pending tasks and GC interleaved.

        This tests whether GC running while abandon's pending tasks are delayed
        causes issues.
        """
        f = stress_fixture

        for i in range(5):
            # store n and wait
            await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
            await f.arena._wait_for_all_pending_tasks()
            assert f.arena.is_tracked(f.n_node_id), f"Cycle {i}: n should be tracked after store"

            # abandon n with delayed pending tasks, run GC while delayed
            with f.arena._delay_pending_tasks(f"abandon delay {i}"):
                f.arena.abandon(f.n_node_id)
                assert not f.arena.is_tracked(
                    f.n_node_id
                ), f"Cycle {i}: n should not be tracked after abandon"

                # GC while abandon's pending tasks are delayed
                await f.run_gc_until_done()

            # Now pending tasks complete
            await f.arena._wait_for_all_pending_tasks()

        # Final re-store should work
        await f.arena.copy_from_arena(f.n_node_id, f.memory_arena)
        assert f.arena.is_tracked(f.n_node_id)
        await f.arena._wait_for_all_pending_tasks()

    # -------------------------------------------------------------------------
    # Tests with async garbage worker enabled (for race conditions)
    # -------------------------------------------------------------------------

    @pytest.mark.anyio
    async def test_loop_store_abandon_n_only__with_async_gc_worker(
        self, tmp_db_url: str, memory_store: MemoryStore, ground_context: FxtrID, caplog
    ):
        """Repeatedly store and abandon n-node with async GC worker enabled.

        This tests the original failure pattern with non-deterministic GC timing.
        The garbage worker runs in the background, potentially racing with
        store/abandon operations.
        """
        import logging

        caplog.set_level(logging.INFO)
        caplog.set_level(logging.DEBUG, logger="fxtr2.data.sqlite_arena")

        # Create memory arena with n-node and d-node (same as fixture but separate)
        memory_arena = memory_store.new_empty_arena()
        ctx_type = FxtrID.builtin("ctx")
        memory_arena.track_builtin(ctx_type)

        context_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
            "value": RawWeakFxtrID(inner_id=ground_context),
            "ctx_key": "test_ctx",
            "metadata": {},
        }
        c_node_id = memory_arena.create_c_node(context_data)

        n_entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=c_node_id),
            "data": "test_n_node_data",
        }
        n_node_id = memory_arena.store(n_entity)

        # Use normal session with GC worker enabled (default)
        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            arena = ns.new_empty_arena()

            for i in range(20):
                # store n
                await arena.copy_from_arena(n_node_id, memory_arena)
                assert arena.is_tracked(n_node_id), f"Cycle {i}: n should be tracked after store"

                # abandon n
                arena.abandon(n_node_id)
                assert not arena.is_tracked(
                    n_node_id
                ), f"Cycle {i}: n should not be tracked after abandon"

                # wait for pending tasks
                await arena._wait_for_all_pending_tasks()

            # Final re-store should work
            await arena.copy_from_arena(n_node_id, memory_arena)
            assert arena.is_tracked(n_node_id)
            await arena._wait_for_all_pending_tasks()

    @pytest.mark.anyio
    async def test_loop_store_abandon_n_only__with_async_gc_worker_no_wait(
        self, tmp_db_url: str, memory_store: MemoryStore, ground_context: FxtrID
    ):
        """Repeatedly store and abandon n-node with async GC, no wait between cycles.

        This maximizes the chance of race conditions by not waiting for pending
        tasks between cycles.
        """
        # Create memory arena with n-node and d-node
        memory_arena = memory_store.new_empty_arena()
        ctx_type = FxtrID.builtin("ctx")
        memory_arena.track_builtin(ctx_type)

        context_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
            "value": RawWeakFxtrID(inner_id=ground_context),
            "ctx_key": "test_ctx",
            "metadata": {},
        }
        c_node_id = memory_arena.create_c_node(context_data)

        n_entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=c_node_id),
            "data": "test_n_node_data",
        }
        n_node_id = memory_arena.store(n_entity)

        # Use normal session with GC worker enabled (default)
        async with SQLiteFxtrSession.open(tmp_db_url) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            arena = ns.new_empty_arena()

            for i in range(20):
                # store n (no wait)
                await arena.copy_from_arena(n_node_id, memory_arena)
                assert arena.is_tracked(n_node_id), f"Cycle {i}: n should be tracked after store"

                # abandon n (no wait)
                arena.abandon(n_node_id)
                assert not arena.is_tracked(
                    n_node_id
                ), f"Cycle {i}: n should not be tracked after abandon"

            # Wait only at the end
            await arena._wait_for_all_pending_tasks()

            # Final re-store should work
            await arena.copy_from_arena(n_node_id, memory_arena)
            assert arena.is_tracked(n_node_id)
            await arena._wait_for_all_pending_tasks()


# =============================================================================
# Session File Crash Recovery Tests
# =============================================================================


class TestSessionFileCrashRecovery:
    """Tests for session file crash recovery mechanism.

    These tests verify that session files are created, locked, and cleaned up
    correctly, and that dead sessions from crashed processes can be detected
    and marked as garbage.
    """

    @pytest.mark.anyio
    async def test_session_file_created_on_start(self, tmp_path):
        """Session file is created when session starts."""
        from pathlib import Path

        db_path = tmp_path / "test.sqlite"
        session_dir = Path(str(db_path) + "-fxtrsessions")
        db_url = f"sqlite+aiosqlite:///{db_path}"

        async with SQLiteFxtrSession.open(db_url) as session:
            # Session file directory should exist
            assert session_dir.exists()
            # Session file should exist with our session UUID
            session_file = session_dir / str(session._external_sid)
            assert session_file.exists()
            # File should contain PID and timestamp
            content = session_file.read_text()
            lines = content.strip().split("\n")
            assert len(lines) == 2
            import os

            assert lines[0] == str(os.getpid())
            # Second line should be ISO timestamp
            from datetime import datetime

            datetime.fromisoformat(lines[1])  # Will raise if invalid

    @pytest.mark.anyio
    async def test_session_file_deleted_on_normal_close(self, tmp_path):
        """Session file is deleted when session closes normally."""
        from pathlib import Path

        db_path = tmp_path / "test.sqlite"
        session_dir = Path(str(db_path) + "-fxtrsessions")
        db_url = f"sqlite+aiosqlite:///{db_path}"

        async with SQLiteFxtrSession.open(db_url) as session:
            session_file = session_dir / str(session._external_sid)
            assert session_file.exists()

        # After context manager exits, file should be gone
        assert not session_file.exists()
        # Directory should be cleaned up too (was empty)
        assert not session_dir.exists()

    @pytest.mark.anyio
    async def test_session_dir_not_deleted_if_other_sessions(self, tmp_path):
        """Session directory persists if other session files exist."""
        from pathlib import Path

        db_path = tmp_path / "test.sqlite"
        session_dir = Path(str(db_path) + "-fxtrsessions")
        db_url = f"sqlite+aiosqlite:///{db_path}"

        async with SQLiteFxtrSession.open(db_url) as session1:
            async with SQLiteFxtrSession.open(db_url) as session2:
                file1 = session_dir / str(session1._external_sid)
                file2 = session_dir / str(session2._external_sid)
                assert file1.exists()
                assert file2.exists()

            # session2 exited, its file should be gone
            assert not file2.exists()
            # But directory still exists (session1's file is there)
            assert session_dir.exists()

        # Now both are gone, directory should be cleaned
        assert not file1.exists()
        assert not session_dir.exists()

    @pytest.mark.anyio
    async def test_memory_db_skips_session_files(self):
        """In-memory databases don't create session files."""
        db_url = "sqlite+aiosqlite:///:memory:"

        async with SQLiteFxtrSession.open(db_url) as session:
            # These should return None for in-memory databases
            assert session._session_files_dir_path() is None
            assert session._session_file_path() is None

    @pytest.mark.anyio
    async def test_dead_session_detected_and_marked_garbage(self, tmp_path):
        """Dead session files are detected and marked as garbage."""
        import os
        from datetime import datetime, timezone
        from pathlib import Path

        from sqlalchemy import select

        from fxtr2.data.deprecated.sqlite_arena import (
            create_fxtr_engine,
            init_database,
            session_table,
        )

        db_path = tmp_path / "test.sqlite"
        session_dir = Path(str(db_path) + "-fxtrsessions")
        db_url = f"sqlite+aiosqlite:///{db_path}"

        # Set up the database and insert the dead session BEFORE opening a session
        # This avoids racing with the garbage cleanup worker
        engine = create_fxtr_engine(db_url)
        await init_database(engine)

        dead_session_uuid = uuid.uuid4()

        async with engine.write_txn_engine.begin() as conn:
            await conn.execute(
                session_table.insert().values(
                    public_id=dead_session_uuid.bytes,
                    created_at=datetime.now(timezone.utc),
                    last_seen_at=datetime.now(timezone.utc),
                    is_garbage=False,  # Not marked as garbage yet
                )
            )

        await engine.root_engine.dispose()

        # Now create the fake session file (not locked by anyone)
        session_dir.mkdir(parents=True, exist_ok=True)
        dead_session_file = session_dir / str(dead_session_uuid)
        dead_session_file.write_text(f"{os.getpid()}\n2025-01-01T00:00:00+00:00\n")

        async with SQLiteFxtrSession.open(db_url) as session:
            # The garbage worker will detect the dead session file (unlocked),
            # mark the session as garbage in the DB, delete the file, and then
            # eventually clean up the session record entirely.
            import anyio

            # Give the background worker a moment to run
            await anyio.sleep(0.2)

            # The dead session file should be deleted (proves detection worked)
            assert not dead_session_file.exists()

            # The dead session should either be:
            # - Deleted from DB entirely, OR
            # - Still in DB but marked as garbage
            # It should NOT be present and non-garbage
            async with session._async_engine_group.read_txn_engine.begin() as conn:
                result = await conn.execute(
                    select(session_table.c.is_garbage).where(
                        session_table.c.public_id == dead_session_uuid.bytes
                    )
                )
                row = result.fetchone()
                if row is not None:
                    # Session still exists - must be marked as garbage
                    assert row[0] is True, "Dead session should be marked as garbage"
                # If row is None, session was fully cleaned up - also acceptable

    @pytest.mark.anyio
    async def test_live_session_not_marked_dead(self, tmp_path):
        """Live sessions (with held locks) are not marked as dead."""
        from pathlib import Path

        db_path = tmp_path / "test.sqlite"
        session_dir = Path(str(db_path) + "-fxtrsessions")
        db_url = f"sqlite+aiosqlite:///{db_path}"

        async with SQLiteFxtrSession.open(db_url) as session1:
            file1 = session_dir / str(session1._external_sid)

            async with SQLiteFxtrSession.open(db_url) as session2:
                file2 = session_dir / str(session2._external_sid)

                # Session2 checks for dead sessions - session1 should NOT be detected
                found_dead = await session2._detect_and_mark_dead_sessions()
                # No dead sessions found (session1 is alive)
                assert found_dead is False
                # Session1's file should still exist
                assert file1.exists()

    @pytest.mark.anyio
    async def test_invalid_session_file_cleaned_up(self, tmp_path):
        """Invalid session files (not UUIDs) are cleaned up."""
        from pathlib import Path

        db_path = tmp_path / "test.sqlite"
        session_dir = Path(str(db_path) + "-fxtrsessions")
        db_url = f"sqlite+aiosqlite:///{db_path}"

        # Create an invalid session file
        session_dir.mkdir(parents=True, exist_ok=True)
        invalid_file = session_dir / "not-a-uuid"
        invalid_file.write_text("invalid content")

        async with SQLiteFxtrSession.open(db_url) as session:
            # Detection should clean up the invalid file
            await session._detect_and_mark_dead_sessions()

            # Invalid file should be deleted
            assert not invalid_file.exists()
