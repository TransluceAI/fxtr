"""Tests for fxtr2/data/sqlite_store/sqlite_namespace.py."""

from __future__ import annotations

import logging
import typing
import uuid
from typing import Any

import pytest

from fxtr2.data.entity_core import (
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawStrongFxtrID,
    RawWeakFxtrID,
)
from fxtr2.data.memory_arena import MemoryArena, MemoryStore
from fxtr2.data.sqlite_store import sqlite_ops
from fxtr2.data.sqlite_store.sqlite_namespace import (
    SQLiteFxtrSession,
    SQLiteNamespaceArena,
    SQLiteNamespaceBackend,
)
from fxtr2.data.sqlite_store.sqlite_ops import GARBAGE_SESSION_ID
from fxtr2.leases.local_lease import UnconditionalLocalLifetime
from tests.conftest import _LeasedArena

logger = logging.getLogger(__name__)

# =============================================================================
# Database Initialization
# =============================================================================


class TestDatabaseInitialization:
    """Tests for database initialization via setup_database_and_create_worker_pool_group."""

    def test_setup_creates_tables(self, tmp_db_path_str: str):
        """setup_database_and_create_worker_pool_group() creates all required tables."""
        import sqlite3

        pool_group = sqlite_ops.setup_database_and_create_worker_pool_group(tmp_db_path_str)
        try:
            # Verify tables exist by querying
            conn = sqlite3.connect(tmp_db_path_str, isolation_level=None)
            try:
                # Check session table exists
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='session'"
                )
                assert cursor.fetchone() is not None

                # Check entity table exists
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='entity'"
                )
                assert cursor.fetchone() is not None

                # Check namespace table exists
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='namespace'"
                )
                assert cursor.fetchone() is not None
            finally:
                conn.close()
        finally:
            pool_group.close()

    def test_setup_idempotent(self, tmp_db_path_str: str):
        """Calling setup_database_and_create_worker_pool_group() twice is safe."""
        pool_group1 = sqlite_ops.setup_database_and_create_worker_pool_group(tmp_db_path_str)
        pool_group1.close()
        # Second call should not raise
        pool_group2 = sqlite_ops.setup_database_and_create_worker_pool_group(tmp_db_path_str)
        pool_group2.close()

    def test_setup_creates_garbage_session(self, tmp_db_path_str: str):
        """Session 0 (garbage session) is created."""
        import sqlite3

        pool_group = sqlite_ops.setup_database_and_create_worker_pool_group(tmp_db_path_str)
        try:
            conn = sqlite3.connect(tmp_db_path_str, isolation_level=None)
            try:
                cursor = conn.execute("SELECT id, is_garbage FROM session WHERE id = 0")
                row = cursor.fetchone()
                assert row is not None
                assert row[0] == GARBAGE_SESSION_ID
                assert row[1] == 1  # is_garbage (SQLite stores as 1)
            finally:
                conn.close()
        finally:
            pool_group.close()

    def test_database_user_version(self, tmp_db_path_str: str):
        """User version pragma is set."""
        import sqlite3

        pool_group = sqlite_ops.setup_database_and_create_worker_pool_group(tmp_db_path_str)
        try:
            conn = sqlite3.connect(tmp_db_path_str, isolation_level=None)
            try:
                cursor = conn.execute("PRAGMA user_version")
                row = cursor.fetchone()
                assert row is not None
                assert row[0] == sqlite_ops.DB_USER_VERSION
            finally:
                conn.close()
        finally:
            pool_group.close()

    def test_memory_db_not_supported(self):
        """Memory databases are not supported (segment files require filesystem)."""
        # Plain :memory: is rejected
        with pytest.raises(ValueError, match="must be a plain file path"):
            sqlite_ops.setup_database_and_create_worker_pool_group(":memory:")

        # file: URIs are also rejected
        with pytest.raises(ValueError, match="must be a plain file path"):
            sqlite_ops.setup_database_and_create_worker_pool_group("file::memory:?cache=shared")

        with pytest.raises(ValueError, match="must be a plain file path"):
            sqlite_ops.setup_database_and_create_worker_pool_group(
                "file:testmemdb?mode=memory&cache=shared"
            )


# =============================================================================
# Segment Files
# =============================================================================


class TestSegmentFiles:
    """Tests for segment file infrastructure created on fresh databases."""

    def test_fresh_db_creates_shards_directory(self, tmp_db_path_str: str):
        """A fresh database creates a .data2.shards/ directory with an initial segment file."""
        import os

        pool_group = sqlite_ops.setup_database_and_create_worker_pool_group(tmp_db_path_str)
        try:
            shards_dir = f"{tmp_db_path_str}.data2.shards"
            seg_path = os.path.join(shards_dir, "seg_00000000.bin")
            assert os.path.isdir(shards_dir), f"Expected shards dir at {shards_dir}"
            assert os.path.isfile(seg_path), f"Expected initial segment file at {seg_path}"
        finally:
            pool_group.close()

    def test_fresh_db_has_segment_metadata(self, tmp_db_path_str: str):
        """A fresh database has a segment_metadata row for the active segment."""
        import sqlite3

        pool_group = sqlite_ops.setup_database_and_create_worker_pool_group(tmp_db_path_str)
        try:
            conn = sqlite3.connect(tmp_db_path_str, isolation_level=None)
            try:
                cursor = conn.execute(
                    "SELECT segment_id, status, write_cursor FROM segment_metadata"
                )
                row = cursor.fetchone()
                assert row is not None
                assert row[0] == 0  # segment_id
                assert row[1] == "active"  # status
                assert row[2] == 0  # write_cursor (empty)
            finally:
                conn.close()
        finally:
            pool_group.close()

    def test_reopening_db(self, tmp_db_path_str: str):
        """A segment-file database can be closed and reopened."""
        pool_group1 = sqlite_ops.setup_database_and_create_worker_pool_group(tmp_db_path_str)
        pool_group1.close()

        # Second open should work and reuse the shards directory
        pool_group2 = sqlite_ops.setup_database_and_create_worker_pool_group(tmp_db_path_str)
        pool_group2.close()


# =============================================================================
# SQLiteFxtrSession Lifecycle
# =============================================================================


class TestSQLiteFxtrSessionLifecycle:
    """Tests for SQLiteFxtrSession lifecycle management."""

    @pytest.mark.anyio
    async def test_session_open_creates_context(self, tmp_db_path_str: str):
        """SQLiteFxtrSession.open() works as context manager."""
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            assert session is not None
            assert isinstance(session, SQLiteFxtrSession)

    @pytest.mark.anyio
    async def test_session_is_active(self, tmp_db_path_str: str):
        """is_active() returns True for active session."""
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            assert session.is_active()

    @pytest.mark.anyio
    async def test_session_not_active_after_close(self, tmp_db_path_str: str):
        """Session is not active after close."""
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            assert session.is_active()
        # After context exit, session should be closed
        assert not session.is_active()

    @pytest.mark.anyio
    async def test_session_has_internal_id(self, tmp_db_path_str: str):
        """Session gets an internal ID after start."""
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            assert session._internal_sid is not None
            assert session._internal_sid > 0  # 0 is garbage session


# =============================================================================
# Namespace Operations
# =============================================================================


class TestNamespaceOperations:
    """Tests for namespace operations."""

    @pytest.mark.anyio
    async def test_create_user_namespace(self, tmp_db_path_str: str):
        """create_or_retrieve_user_namespace() creates new namespace."""
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            assert ns is not None
            assert isinstance(ns, SQLiteNamespaceBackend)

    @pytest.mark.anyio
    async def test_retrieve_existing_namespace(self, tmp_db_path_str: str):
        """Returns existing namespace by name."""
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns1 = await session.create_or_retrieve_user_namespace("test_namespace")
            ns2 = await session.create_or_retrieve_user_namespace("test_namespace")
            # Should get same namespace (same internal ID)
            assert ns1._namespace_internal_id == ns2._namespace_internal_id

    @pytest.mark.anyio
    async def test_namespace_has_uuid(self, tmp_db_path_str: str):
        """Namespace gets unique UUID."""
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            assert ns._namespace_id is not None
            assert isinstance(ns._namespace_id, uuid.UUID)

    @pytest.mark.anyio
    async def test_multiple_namespaces(self, tmp_db_path_str: str):
        """Can create multiple namespaces."""
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
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
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """pin_entity_from_arena() copies and pins."""
        # Store entity in memory arena
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "test_entity",
            "value": 42,
        }
        entity_id = _legacy_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "test_pin",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

    @pytest.mark.anyio
    async def test_pin_duplicate_name_raises(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """Duplicate pin name raises KeyError when overwrite=False."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = _legacy_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "test_pin",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )
            with pytest.raises(KeyError):
                await ns.pin_entity_from_arena(
                    "test_pin",
                    entity_id,
                    _legacy_arena._arena,
                    _legacy_arena._lease.clone(),
                )

    @pytest.mark.anyio
    async def test_pin_overwrite_replaces_existing(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """pin_entity_from_arena with overwrite=True replaces existing pin."""
        entity1: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "original",
        }
        entity1_id = _legacy_arena.store(entity1)

        entity2: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "replacement",
        }
        entity2_id = _legacy_arena.store(entity2)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "test_pin",
                entity1_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            # Overwrite with entity2
            await ns.pin_entity_from_arena(
                "test_pin",
                entity2_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
                overwrite=True,
            )

            # Retrieve should return entity2
            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            assert retrieved_id == entity2_id
            loaded = await typing.cast(Any, arena.load(session.root_lease.clone(), retrieved_id))
            assert loaded["data"] == "replacement"

    @pytest.mark.anyio
    async def test_pin_overwrite_with_same_entity(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """pin_entity_from_arena with overwrite=True works even for same entity."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = _legacy_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "test_pin",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            # Overwrite with same entity should succeed
            await ns.pin_entity_from_arena(
                "test_pin",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
                overwrite=True,
            )

            # Retrieve should still work
            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            assert retrieved_id == entity_id

    @pytest.mark.anyio
    async def test_pin_overwrite_false_is_default(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """Explicit overwrite=False behaves same as default."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = _legacy_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "test_pin",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )
            with pytest.raises(KeyError):
                await ns.pin_entity_from_arena(
                    "test_pin",
                    entity_id,
                    _legacy_arena._arena,
                    _legacy_arena._lease.clone(),
                    overwrite=False,
                )

    @pytest.mark.anyio
    async def test_unpin_entity(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """unpin_entity() removes pin."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = _legacy_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "test_pin",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )
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
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """retrieve_pinned() returns entity ID and arena."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "test_entity",
            "value": 42,
        }
        entity_id = _legacy_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "test_pin",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            assert retrieved_id == entity_id
            assert arena is not None
            assert isinstance(arena, SQLiteNamespaceArena)

    @pytest.mark.anyio
    async def test_retrieve_nonexistent_raises(self, tmp_db_path_str: str):
        """Retrieving unknown pin raises."""
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            with pytest.raises(KeyError):
                await ns.retrieve_pinned("nonexistent_pin")

    @pytest.mark.anyio
    async def test_retrieved_entity_loadable(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """Retrieved entity can be loaded."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "test_entity",
            "value": 42,
        }
        entity_id = _legacy_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "test_pin",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            loaded = await arena.load(session.root_lease.clone(), retrieved_id)
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
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """is_tracked() works for known entities."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = _legacy_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "test_pin",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            assert arena.is_tracked(session.root_lease.clone(), retrieved_id)
            # Note: unlike MemoryArena, SQLiteNamespaceArena doesn't track ground context
            # by default - it tracks only entities in the arena

    @pytest.mark.anyio
    async def test_arena_load(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """load() returns entity data."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "key": "value",
        }
        entity_id = _legacy_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "test_pin",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            loaded = await arena.load(session.root_lease.clone(), retrieved_id)
            loaded = typing.cast(Any, loaded)
            assert loaded["key"] == "value"

    @pytest.mark.anyio
    async def test_arena_load_tracks_children(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """Loading entity tracks child entities."""
        # Create a child entity
        child: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "child": True,
        }
        child_id = _legacy_arena.store(child)

        # Create parent referencing child
        parent: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "child_ref": RawStrongFxtrID(inner_id=child_id),
        }
        parent_id = _legacy_arena.store(parent)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "test_pin",
                parent_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            loaded = await arena.load(session.root_lease.clone(), retrieved_id)
            loaded = typing.cast(Any, loaded)

            # Child should be tracked after loading parent
            child_ref = loaded["child_ref"]
            assert arena.is_tracked(session.root_lease.clone(), child_ref.inner_id)

    @pytest.mark.anyio
    async def test_arena_unwrap_provenance_on_d_node(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """unwrap_provenance() on d-node returns None."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = _legacy_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "test_pin",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            unwrapped = arena.unwrap_provenance(session.root_lease.clone(), retrieved_id)
            assert unwrapped is None  # d-nodes don't unwrap

    @pytest.mark.anyio
    async def test_arena_abandon(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """abandon() removes from tracked set."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = _legacy_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "test_pin",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            assert arena.is_tracked(session.root_lease.clone(), retrieved_id)

            arena.abandon(session.root_lease.clone(), retrieved_id)
            assert not arena.is_tracked(session.root_lease.clone(), retrieved_id)


# =============================================================================
# Persistence
# =============================================================================


class TestPersistence:
    """Tests for data persistence across sessions."""

    @pytest.mark.anyio
    async def test_data_persists_across_sessions(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """Close and reopen, data still there."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "persistent": "data",
        }
        entity_id = _legacy_arena.store(entity)

        # First session: pin entity
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "test_pin",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

        # Second session: retrieve entity
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            loaded = await arena.load(session.root_lease.clone(), retrieved_id)
            loaded = typing.cast(Any, loaded)
            assert loaded["persistent"] == "data"

    @pytest.mark.anyio
    async def test_namespace_persists(self, tmp_db_path_str: str):
        """Namespaces persist across sessions."""
        ns_id1 = None

        # First session: create namespace
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("persistent_namespace")
            ns_id1 = ns._namespace_id

        # Second session: retrieve namespace
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("persistent_namespace")
            assert ns._namespace_id == ns_id1


# =============================================================================
# Node Type Handling
# =============================================================================


class TestNodeTypeHandling:
    """Tests for handling different node types."""

    @pytest.mark.anyio
    async def test_d_node_storage(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """d-nodes stored with CBOR data."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "type": "d-node",
        }
        entity_id = _legacy_arena.store(entity)
        assert entity_id.get_tag() == "d"

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "test_pin",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            loaded = await arena.load(session.root_lease.clone(), retrieved_id)
            loaded = typing.cast(Any, loaded)
            assert loaded["type"] == "d-node"

    @pytest.mark.anyio
    async def test_builtin_node_handling(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """Builtin nodes are handled correctly."""
        # Track a builtin
        test_type = FxtrID.builtin("test_type")
        _legacy_arena.track_builtin(test_type)

        # Create entity referencing builtin
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "$fxtr.type": RawWeakFxtrID(inner_id=test_type),
            "data": "test",
        }
        entity_id = _legacy_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "test_pin",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            loaded = await arena.load(session.root_lease.clone(), retrieved_id)
            loaded = typing.cast(Any, loaded)

            # Type ref should be preserved
            type_ref = loaded.get("$fxtr.type")
            assert type_ref is not None
            assert type_ref.inner_id == test_type

    @pytest.mark.anyio
    async def test_u_node_storage_and_unwrap(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """u-nodes stored with wrapped reference, unwrap works, load raises."""
        # Create d-node
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "inner": "data",
        }
        d_node_id = _legacy_arena.store(entity)

        # Create u-node wrapping it
        u_node_id = _legacy_arena.create_u_node(d_node_id)
        assert u_node_id.get_tag() == "u"

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "test_pin",
                u_node_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            retrieved_id, arena = await ns.retrieve_pinned("test_pin")

            # Check unwrap works
            unwrapped = arena.unwrap_provenance(session.root_lease.clone(), retrieved_id)
            assert unwrapped == d_node_id

            # Loading u-node directly raises NotImplementedError
            with pytest.raises(NotImplementedError, match="does not support loading 'u' nodes"):
                await arena.load(session.root_lease.clone(), retrieved_id)

            # But loading the unwrapped d-node works
            assert unwrapped is not None
            loaded = await arena.load(session.root_lease.clone(), unwrapped)
            loaded = typing.cast(Any, loaded)
            assert loaded["inner"] == "data"


# =============================================================================
# Error Cases
# =============================================================================


class TestMultipleRefsToSameChild:
    """Tests for entities with multiple references to the same child."""

    @pytest.mark.anyio
    async def test_entity_with_multiple_refs_to_same_child(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """Entity with multiple refs (2 weak, 1 strong) to same child."""
        # Create a child entity
        child: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "child_data": "shared_child",
        }
        child_id = _legacy_arena.store(child)

        # Create parent with 3 refs to the same child: 2 weak, 1 strong
        parent: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "weak_ref1": RawWeakFxtrID(inner_id=child_id),
            "weak_ref2": RawWeakFxtrID(inner_id=child_id),
            "strong_ref": RawStrongFxtrID(inner_id=child_id),
        }
        parent_id = _legacy_arena.store(parent)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "test_pin",
                parent_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            # Retrieve and load
            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            loaded = await arena.load(session.root_lease.clone(), retrieved_id)
            loaded = typing.cast(Any, loaded)
            assert loaded["weak_ref1"].inner_id == child_id
            assert loaded["weak_ref2"].inner_id == child_id
            assert loaded["strong_ref"].inner_id == child_id
        # Session closes here, SQLite arena is cleaned up


# =============================================================================
# Error Cases
# =============================================================================


class TestErrorCases:
    """Tests for error conditions."""

    @pytest.mark.anyio
    async def test_load_untracked_entity_raises(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """Loading untracked entity raises ValueError."""
        # Pin an entity so we get an arena
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = _legacy_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "test_pin",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )
            _, arena = await ns.retrieve_pinned("test_pin")

            # Try to load an entity that isn't tracked
            unknown_id = FxtrID.from_tag_and_payload("d", b"unknown_entity_data")
            with pytest.raises(ValueError, match="not tracked"):
                await arena.load(session.root_lease.clone(), unknown_id)


# =============================================================================
# Integration Tests
# =============================================================================


class TestIntegration:
    """Integration tests for complete workflows."""

    @pytest.mark.anyio
    async def test_full_workflow(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """Create in memory -> copy to sqlite -> retrieve -> load."""
        # Create entity with children in memory
        child: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "type": "child",
            "value": 100,
        }
        child_id = _legacy_arena.store(child)

        parent: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "type": "parent",
            "child": RawStrongFxtrID(inner_id=child_id),
        }
        parent_id = _legacy_arena.store(parent)

        # Copy to SQLite
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "parent",
                parent_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

        # Retrieve in new session
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            retrieved_id, arena = await ns.retrieve_pinned("parent")

            # Load parent
            parent_loaded = await arena.load(session.root_lease.clone(), retrieved_id)
            parent_loaded = typing.cast(Any, parent_loaded)
            assert parent_loaded["type"] == "parent"

            # Load child
            child_ref = parent_loaded["child"]
            child_loaded = await arena.load(session.root_lease.clone(), child_ref.inner_id)
            child_loaded = typing.cast(Any, child_loaded)
            assert child_loaded["type"] == "child"
            assert child_loaded["value"] == 100

    @pytest.mark.anyio
    async def test_multiple_pins_same_entity(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """Same entity pinned under multiple names."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "shared",
        }
        entity_id = _legacy_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "pin1",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )
            await ns.pin_entity_from_arena(
                "pin2",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            # Both pins return same entity
            id1, arena1 = await ns.retrieve_pinned("pin1")
            id2, arena2 = await ns.retrieve_pinned("pin2")
            assert id1 == id2 == entity_id

    @pytest.mark.anyio
    async def test_pin_entity_with_deep_deps(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
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
            current_id = _legacy_arena.store(entity)

        root_id = current_id
        assert root_id is not None

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "deep_chain",
                root_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            # Verify all levels can be loaded
            retrieved_id, arena = await ns.retrieve_pinned("deep_chain")
            current = await arena.load(session.root_lease.clone(), retrieved_id)
            current = typing.cast(Any, current)
            for i in range(9, -1, -1):
                assert current["level"] == i
                if i > 0:
                    next_ref = current["next"]
                    current = await arena.load(session.root_lease.clone(), next_ref.inner_id)
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
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
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
        child1_id = _legacy_arena.store(child1)

        child2: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "child2",
        }
        child2_id = _legacy_arena.store(child2)

        parent: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "parent",
            "child1": RawStrongFxtrID(inner_id=child1_id),
            "child2": RawStrongFxtrID(inner_id=child2_id),
        }
        parent_id = _legacy_arena.store(parent)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            # Step 1: Pin to namespace A - this copies entities to entity tables
            ns_a = await session.create_or_retrieve_user_namespace("namespace_a")
            await ns_a.pin_entity_from_arena(
                "parent",
                parent_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            # Verify entities are in namespace A
            retrieved_id, arena_a = await ns_a.retrieve_pinned("parent")
            assert retrieved_id == parent_id
            loaded = await arena_a.load(session.root_lease.clone(), retrieved_id)
            loaded = typing.cast(Any, loaded)
            assert loaded["name"] == "parent"

            # Step 2: Create namespace B and copy the parent from namespace A's arena
            ns_b = await session.create_or_retrieve_user_namespace("namespace_b")

            # Copy from arena_a (which has the parent tracked) to namespace B
            # This exercises TrackDeps: parent and children are in entity tables
            # but not in namespace B
            await ns_b.pin_entity_from_arena(
                "parent_copy",
                parent_id,
                arena_a,
                session.root_lease.clone(),
            )

            # Step 3: Verify all entities are correctly in namespace B
            retrieved_id_b, arena_b = await ns_b.retrieve_pinned("parent_copy")
            assert retrieved_id_b == parent_id

            loaded_b = await arena_b.load(session.root_lease.clone(), retrieved_id_b)
            loaded_b = typing.cast(Any, loaded_b)
            assert loaded_b["name"] == "parent"

            # Verify children are accessible
            child1_ref = loaded_b["child1"]
            child1_loaded = await arena_b.load(session.root_lease.clone(), child1_ref.inner_id)
            child1_loaded = typing.cast(Any, child1_loaded)
            assert child1_loaded["name"] == "child1"

            child2_ref = loaded_b["child2"]
            child2_loaded = await arena_b.load(session.root_lease.clone(), child2_ref.inner_id)
            child2_loaded = typing.cast(Any, child2_loaded)
            assert child2_loaded["name"] == "child2"

    @pytest.mark.anyio
    async def test_copy_deep_hierarchy_between_namespaces(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
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
        leaf_id = _legacy_arena.store(leaf)

        level3: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "level": "level3",
            "child": RawStrongFxtrID(inner_id=leaf_id),
        }
        level3_id = _legacy_arena.store(level3)

        level2: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "level": "level2",
            "child": RawStrongFxtrID(inner_id=level3_id),
        }
        level2_id = _legacy_arena.store(level2)

        level1: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "level": "level1",
            "child": RawStrongFxtrID(inner_id=level2_id),
        }
        level1_id = _legacy_arena.store(level1)

        root: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "level": "root",
            "child": RawStrongFxtrID(inner_id=level1_id),
        }
        root_id = _legacy_arena.store(root)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            # Pin to namespace A
            ns_a = await session.create_or_retrieve_user_namespace("namespace_a")
            await ns_a.pin_entity_from_arena(
                "root",
                root_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            # Retrieve from A to get an arena
            _, arena_a = await ns_a.retrieve_pinned("root")

            # Copy to namespace B
            ns_b = await session.create_or_retrieve_user_namespace("namespace_b")
            await ns_b.pin_entity_from_arena(
                "root_copy",
                root_id,
                arena_a,
                session.root_lease.clone(),
            )

            # Verify entire chain is accessible in namespace B
            retrieved_id, arena_b = await ns_b.retrieve_pinned("root_copy")
            current = await arena_b.load(session.root_lease.clone(), retrieved_id)
            current = typing.cast(Any, current)
            expected_levels = ["root", "level1", "level2", "level3", "leaf"]

            for expected_level in expected_levels:
                assert current["level"] == expected_level
                if "child" in current:
                    child_ref = current["child"]
                    current = await arena_b.load(session.root_lease.clone(), child_ref.inner_id)
                    current = typing.cast(Any, current)

    @pytest.mark.anyio
    async def test_copy_diamond_dependency_between_namespaces(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
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
        shared_id = _legacy_arena.store(shared)

        child1: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "child1",
            "dep": RawStrongFxtrID(inner_id=shared_id),
        }
        child1_id = _legacy_arena.store(child1)

        child2: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "child2",
            "dep": RawStrongFxtrID(inner_id=shared_id),
        }
        child2_id = _legacy_arena.store(child2)

        parent: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "parent",
            "left": RawStrongFxtrID(inner_id=child1_id),
            "right": RawStrongFxtrID(inner_id=child2_id),
        }
        parent_id = _legacy_arena.store(parent)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            # Pin to namespace A
            ns_a = await session.create_or_retrieve_user_namespace("namespace_a")
            await ns_a.pin_entity_from_arena(
                "parent",
                parent_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            # Retrieve from A
            _, arena_a = await ns_a.retrieve_pinned("parent")

            # Copy to namespace B
            ns_b = await session.create_or_retrieve_user_namespace("namespace_b")
            await ns_b.pin_entity_from_arena(
                "parent_copy",
                parent_id,
                arena_a,
                session.root_lease.clone(),
            )

            # Verify structure in namespace B
            retrieved_id, arena_b = await ns_b.retrieve_pinned("parent_copy")
            parent_loaded = await arena_b.load(session.root_lease.clone(), retrieved_id)
            parent_loaded = typing.cast(Any, parent_loaded)
            assert parent_loaded["name"] == "parent"

            # Load children
            child1_loaded = await arena_b.load(
                session.root_lease.clone(), parent_loaded["left"].inner_id
            )
            child1_loaded = typing.cast(Any, child1_loaded)
            child2_loaded = await arena_b.load(
                session.root_lease.clone(), parent_loaded["right"].inner_id
            )
            child2_loaded = typing.cast(Any, child2_loaded)
            assert child1_loaded["name"] == "child1"
            assert child2_loaded["name"] == "child2"

            # Both children should reference the same shared entity
            shared_from_child1 = await arena_b.load(
                session.root_lease.clone(), child1_loaded["dep"].inner_id
            )
            shared_from_child2 = await arena_b.load(
                session.root_lease.clone(), child2_loaded["dep"].inner_id
            )
            shared_from_child1 = typing.cast(Any, shared_from_child1)
            shared_from_child2 = typing.cast(Any, shared_from_child2)
            assert shared_from_child1["name"] == "shared"
            assert shared_from_child2["name"] == "shared"
            assert child1_loaded["dep"].inner_id == child2_loaded["dep"].inner_id


# =============================================================================
# Bulk Copy Internals Tests
# =============================================================================


class TestBulkCopyInternals:
    """Targeted tests for the batched bulk copy internals.

    These tests verify the discover sweep, bulk commit, race handling,
    precomp caching, topological ordering, and CTE namespace membership
    features of the bulk copy pipeline.
    """

    @pytest.mark.anyio
    async def test_discover_sweep_all_preexisting(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """Discover sweep with all entities already in DB returns empty save queue."""
        child: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "child",
        }
        child_id = _legacy_arena.store(child)

        parent: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "parent",
            "child": RawStrongFxtrID(inner_id=child_id),
        }
        parent_id = _legacy_arena.store(parent)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            # Pin to namespace A — writes all entities to entity table
            ns_a = await session.create_or_retrieve_user_namespace("namespace_a")
            await ns_a.pin_entity_from_arena(
                "parent",
                parent_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            # Create namespace B and run discover sweep
            ns_b = await session.create_or_retrieve_user_namespace("namespace_b")
            save_queue, preexisting = await ns_b._discover_sweep_for_copy(
                parent_id, _legacy_arena._arena, _legacy_arena._lease.clone()
            )

            # All entities preexist — save queue should be empty
            assert len(save_queue) == 0
            # Root entity should be in preexisting (DFS stops at preexisting)
            assert parent_id in preexisting

    @pytest.mark.anyio
    async def test_discover_sweep_all_new(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """Discover sweep with no entities in DB puts all in save queue."""
        child: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "child",
        }
        child_id = _legacy_arena.store(child)

        parent: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "parent",
            "child": RawStrongFxtrID(inner_id=child_id),
        }
        parent_id = _legacy_arena.store(parent)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            # Don't pin anything — entity table is empty
            ns = await session.create_or_retrieve_user_namespace("test_ns")
            save_queue, preexisting = await ns._discover_sweep_for_copy(
                parent_id, _legacy_arena._arena, _legacy_arena._lease.clone()
            )

            # Nothing preexisting
            assert len(preexisting) == 0

            # All entities should be in save queue (ground, child, parent)
            save_queue_ids = {item.fxtr_id for item in save_queue}
            assert ground_context in save_queue_ids
            assert child_id in save_queue_ids
            assert parent_id in save_queue_ids

            # Topologically ordered: deps before parents
            positions = {item.fxtr_id: i for i, item in enumerate(save_queue)}
            assert positions[child_id] < positions[parent_id]
            assert positions[ground_context] < positions[child_id]
            assert positions[ground_context] < positions[parent_id]

    @pytest.mark.anyio
    async def test_discover_sweep_mixed(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """Discover sweep correctly partitions preexisting and new entities."""
        # Build: parent -> child -> shared (where shared is already in DB)
        shared: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "shared",
        }
        shared_id = _legacy_arena.store(shared)

        child: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "child",
            "dep": RawStrongFxtrID(inner_id=shared_id),
        }
        child_id = _legacy_arena.store(child)

        parent: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "parent",
            "child": RawStrongFxtrID(inner_id=child_id),
        }
        parent_id = _legacy_arena.store(parent)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            # Pin shared to namespace A — only shared (+ ground) in entity table
            ns_a = await session.create_or_retrieve_user_namespace("namespace_a")
            await ns_a.pin_entity_from_arena(
                "shared",
                shared_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            # Discover sweep on namespace B for parent
            ns_b = await session.create_or_retrieve_user_namespace("namespace_b")
            save_queue, preexisting = await ns_b._discover_sweep_for_copy(
                parent_id, _legacy_arena._arena, _legacy_arena._lease.clone()
            )

            # shared and ground context should be preexisting
            assert shared_id in preexisting
            assert ground_context in preexisting

            # child and parent should be in save queue
            save_queue_ids = {item.fxtr_id for item in save_queue}
            assert child_id in save_queue_ids
            assert parent_id in save_queue_ids

            # shared and ground should NOT be in save queue
            assert shared_id not in save_queue_ids
            assert ground_context not in save_queue_ids

            # Topological: child before parent
            positions = {item.fxtr_id: i for i, item in enumerate(save_queue)}
            assert positions[child_id] < positions[parent_id]

    @pytest.mark.anyio
    async def test_discover_sweep_topological_order(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """Diamond dependency: save_queue has correct topological order.

        Diamond graph:
            A
           / \\
          B   C
           \\ /
            D

        D must come before B and C, which must come before A.
        """
        d_entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "D",
        }
        d_id = _legacy_arena.store(d_entity)

        b_entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "B",
            "dep": RawStrongFxtrID(inner_id=d_id),
        }
        b_id = _legacy_arena.store(b_entity)

        c_entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "C",
            "dep": RawStrongFxtrID(inner_id=d_id),
        }
        c_id = _legacy_arena.store(c_entity)

        a_entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "A",
            "left": RawStrongFxtrID(inner_id=b_id),
            "right": RawStrongFxtrID(inner_id=c_id),
        }
        a_id = _legacy_arena.store(a_entity)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")
            save_queue, preexisting = await ns._discover_sweep_for_copy(
                a_id, _legacy_arena._arena, _legacy_arena._lease.clone()
            )

            assert len(preexisting) == 0

            positions = {item.fxtr_id: i for i, item in enumerate(save_queue)}
            # D must come before B and C
            assert positions[d_id] < positions[b_id]
            assert positions[d_id] < positions[c_id]
            # B and C must come before A
            assert positions[b_id] < positions[a_id]
            assert positions[c_id] < positions[a_id]
            # Ground context must come before everything
            assert positions[ground_context] < positions[d_id]

    @pytest.mark.anyio
    async def test_bulk_commit_race_handling(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """bulk_copy_commit handles raced entities correctly.

        When an entity in the save queue was committed by another session
        between discover and commit, the race check should detect the existing
        entity and reuse the winner's internal ID instead of inserting a duplicate.
        """
        child: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "child",
        }
        child_id = _legacy_arena.store(child)

        parent: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "parent",
            "child": RawStrongFxtrID(inner_id=child_id),
        }
        parent_id = _legacy_arena.store(parent)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")

            # Phase 0: Discover sweep (nothing in DB yet)
            save_queue, preexisting = await ns._discover_sweep_for_copy(
                parent_id, _legacy_arena._arena, _legacy_arena._lease.clone()
            )
            assert len(preexisting) == 0

            # Simulate race: another namespace pins child (writes child + ground to entity table)
            ns_other = await session.create_or_retrieve_user_namespace("other_ns")
            await ns_other.pin_entity_from_arena(
                "child",
                child_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            # Single-phase commit (raced entities handled gracefully)
            arena_uuid = uuid.uuid4()
            pool_group = session._pool_group
            preexisting_bytes = {fid._raw_data_id: iid for fid, iid in preexisting.items()}
            resolved, _arena_id = await pool_group.write_pool.run_sync_with_connection(
                sqlite_ops.bulk_copy_commit,
                ns._namespace_internal_id,
                arena_uuid.bytes,
                session._internal_sid,
                parent_id._raw_data_id,
                preexisting_bytes,
                save_queue,
                pool_group.segment_manager,
                pool_group.segments_dir,
            )

            # All entities should be resolved
            assert parent_id._raw_data_id in resolved
            assert child_id._raw_data_id in resolved
            assert ground_context._raw_data_id in resolved

    @pytest.mark.anyio
    async def test_bulk_commit_precomp_cache(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """Precomp maps are correct for a chain using the cached-precomp path.

        Chain: A -> B -> C (where -> means "depends on")
        After bulk copy, each entity's closed precomp map should contain entries
        for itself and all its transitive dependencies.
        """
        from fxtr2.data.sqlite_store import entity_blob

        c_entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "C",
        }
        c_id = _legacy_arena.store(c_entity)

        b_entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "B",
            "dep": RawStrongFxtrID(inner_id=c_id),
        }
        b_id = _legacy_arena.store(b_entity)

        a_entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "A",
            "dep": RawStrongFxtrID(inner_id=b_id),
        }
        a_id = _legacy_arena.store(a_entity)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")
            # Pin A (copies whole chain via the cached-precomp bulk path)
            await ns.pin_entity_from_arena(
                "a",
                a_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            # Get internal IDs and segment locations for all entities
            pool_group = session._pool_group

            def get_internal_ids_and_locs(conn):
                ids = {}
                locs = {}
                for fxtr_id in [ground_context, c_id, b_id, a_id]:
                    row = conn.execute(
                        "SELECT id, seg_id, seg_offset, seg_length FROM entity WHERE public_id = ?",
                        (fxtr_id._raw_data_id,),
                    ).fetchone()
                    assert row is not None, f"Entity {fxtr_id} not found"
                    ids[fxtr_id] = row[0]
                    locs[row[0]] = (row[1], row[2], row[3])
                return ids, locs

            internal_ids, seg_locs = await pool_group.read_pool.run_sync_with_connection(
                get_internal_ids_and_locs,
            )

            # Read precomp maps from segment files and verify closure
            sm = pool_group.segment_manager
            # Each entity's precomp should contain itself
            for fxtr_id in [c_id, b_id, a_id]:
                iid = internal_ids[fxtr_id]
                seg_id, seg_offset, seg_length = seg_locs[iid]
                blob = sm.read_record(seg_id, seg_offset, seg_length)
                _deps, precomp_map = entity_blob.decode_metadata(blob)
                assert iid in precomp_map

            # A's precomp should contain all transitive deps (closed map)
            a_iid = internal_ids[a_id]
            seg_id, seg_offset, seg_length = seg_locs[a_iid]
            blob = sm.read_record(seg_id, seg_offset, seg_length)
            _deps, a_precomp = entity_blob.decode_metadata(blob)
            for fxtr_id in [ground_context, c_id, b_id, a_id]:
                iid = internal_ids[fxtr_id]
                assert (
                    iid in a_precomp
                ), f"Entity {fxtr_id} (iid={iid}) missing from A's precomp map"

            # B's precomp should contain B, C, and ground (but not A)
            b_iid = internal_ids[b_id]
            seg_id, seg_offset, seg_length = seg_locs[b_iid]
            blob = sm.read_record(seg_id, seg_offset, seg_length)
            _deps, b_precomp = entity_blob.decode_metadata(blob)
            assert internal_ids[b_id] in b_precomp
            assert internal_ids[c_id] in b_precomp
            assert internal_ids[ground_context] in b_precomp
            assert internal_ids[a_id] not in b_precomp

    @pytest.mark.anyio
    async def test_cte_namespace_add(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """CTE recursively adds all transitive deps to namespace.

        Entities exist in entity table (from namespace A) but not in namespace B.
        The CTE should add all transitively reachable entities to namespace B.
        """
        # Build: root -> mid -> leaf
        leaf: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "leaf",
        }
        leaf_id = _legacy_arena.store(leaf)

        mid: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "mid",
            "dep": RawStrongFxtrID(inner_id=leaf_id),
        }
        mid_id = _legacy_arena.store(mid)

        root: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "name": "root",
            "dep": RawStrongFxtrID(inner_id=mid_id),
        }
        root_id = _legacy_arena.store(root)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            # Pin to namespace A — all entities in entity table + namespace A
            ns_a = await session.create_or_retrieve_user_namespace("namespace_a")
            await ns_a.pin_entity_from_arena(
                "root",
                root_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            pool_group = session._pool_group

            # Get internal IDs
            def get_internal_ids(conn):
                ids = {}
                for fxtr_id in [ground_context, leaf_id, mid_id, root_id]:
                    row = conn.execute(
                        "SELECT id FROM entity WHERE public_id = ?",
                        (fxtr_id._raw_data_id,),
                    ).fetchone()
                    assert row is not None
                    ids[fxtr_id] = row[0]
                return ids

            internal_ids = await pool_group.read_pool.run_sync_with_connection(
                get_internal_ids,
            )

            # Create namespace B and use preexisting-only fast path
            ns_b = await session.create_or_retrieve_user_namespace("namespace_b")
            arena_uuid = uuid.uuid4()
            root_iid = internal_ids[root_id]
            arena_id = await pool_group.write_pool.run_sync_with_connection(
                sqlite_ops.bulk_copy_commit_preexisting_only,
                ns_b._namespace_internal_id,
                arena_uuid.bytes,
                session._internal_sid,
                root_iid,
            )

            # Verify all entities were added to namespace B
            def check_namespace_membership(conn):
                ns_b_id = ns_b._namespace_internal_id
                results = {}
                for fxtr_id, iid in internal_ids.items():
                    row = conn.execute(
                        "SELECT 1 FROM namespace_entity_ownership "
                        "WHERE namespace_id = ? AND entity_id = ?",
                        (ns_b_id, iid),
                    ).fetchone()
                    results[fxtr_id] = row is not None
                return results

            membership = await pool_group.read_pool.run_sync_with_connection(
                check_namespace_membership,
            )
            for fxtr_id in [ground_context, leaf_id, mid_id, root_id]:
                assert membership[fxtr_id], f"Entity {fxtr_id} not in namespace B"

            # Verify only root entity is in the arena (arena is temporary, only needs root)
            def check_arena_membership(conn):
                rows = conn.execute(
                    "SELECT entity_id FROM arena_membership WHERE arena_id = ?",
                    (arena_id,),
                ).fetchall()
                return {r[0] for r in rows}

            arena_entities = await pool_group.read_pool.run_sync_with_connection(
                check_arena_membership,
            )
            assert arena_entities == {internal_ids[root_id]}


# =============================================================================
# Listing Pins
# =============================================================================


class TestListPins:
    """Tests for list_pins() operation."""

    @pytest.mark.anyio
    async def test_list_pins_empty_namespace(self, tmp_db_path_str: str):
        """list_pins() returns empty dict when namespace has no pins."""
        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            result = await ns.list_pins()
            assert result == {}

    @pytest.mark.anyio
    async def test_list_pins_returns_all_pins(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """list_pins() with empty prefix returns all pins."""
        entity1: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "entity1",
        }
        entity1_id = _legacy_arena.store(entity1)

        entity2: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "entity2",
        }
        entity2_id = _legacy_arena.store(entity2)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "pin_a",
                entity1_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )
            await ns.pin_entity_from_arena(
                "pin_b",
                entity2_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            result = await ns.list_pins()
            assert len(result) == 2
            assert result["pin_a"] == entity1_id
            assert result["pin_b"] == entity2_id

    @pytest.mark.anyio
    async def test_list_pins_with_prefix(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """list_pins() filters by prefix."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = _legacy_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "data/models:encoder",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )
            await ns.pin_entity_from_arena(
                "data/models:decoder",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )
            await ns.pin_entity_from_arena(
                "data/configs:main",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )
            await ns.pin_entity_from_arena(
                "other_pin",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            # Filter by "data/models:" prefix
            result = await ns.list_pins("data/models:")
            assert len(result) == 2
            assert "data/models:encoder" in result
            assert "data/models:decoder" in result
            assert "data/configs:main" not in result
            assert "other_pin" not in result

    @pytest.mark.anyio
    async def test_list_pins_prefix_no_match(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """list_pins() returns empty dict when prefix matches nothing."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = _legacy_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "my_pin",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            result = await ns.list_pins("nonexistent_prefix:")
            assert result == {}

    @pytest.mark.anyio
    async def test_list_pins_prefix_partial_match(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """list_pins() only matches pins that start with the prefix."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = _legacy_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "prefix_one",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )
            await ns.pin_entity_from_arena(
                "prefix_two",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )
            await ns.pin_entity_from_arena(
                "other_prefix_one",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            # "prefix_" should match "prefix_one" and "prefix_two" but not "other_prefix_one"
            result = await ns.list_pins("prefix_")
            assert len(result) == 2
            assert "prefix_one" in result
            assert "prefix_two" in result
            assert "other_prefix_one" not in result

    @pytest.mark.anyio
    async def test_list_pins_different_namespaces(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """list_pins() only returns pins from the current namespace."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = _legacy_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns_a = await session.create_or_retrieve_user_namespace("namespace_a")
            ns_b = await session.create_or_retrieve_user_namespace("namespace_b")

            await ns_a.pin_entity_from_arena(
                "pin_in_a",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )
            await ns_b.pin_entity_from_arena(
                "pin_in_b",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            # Each namespace only sees its own pins
            result_a = await ns_a.list_pins()
            result_b = await ns_b.list_pins()

            assert list(result_a.keys()) == ["pin_in_a"]
            assert list(result_b.keys()) == ["pin_in_b"]

    @pytest.mark.anyio
    async def test_list_pins_after_unpin(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """list_pins() reflects unpinned entities."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = _legacy_arena.store(entity)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_namespace")
            await ns.pin_entity_from_arena(
                "pin_a",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )
            await ns.pin_entity_from_arena(
                "pin_b",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

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

        db_path = str(tmp_path / "test.sqlite")
        session_dir = Path(db_path + "-fxtrsessions")

        async with SQLiteFxtrSession.open(db_path) as session:
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

        db_path = str(tmp_path / "test.sqlite")
        session_dir = Path(db_path + "-fxtrsessions")

        async with SQLiteFxtrSession.open(db_path) as session:
            session_file = session_dir / str(session._external_sid)
            assert session_file.exists()

        # After context manager exits, file should be gone
        assert not session_file.exists()
        # Directory should be cleaned up too (was empty)
        assert not session_dir.exists()

    @pytest.mark.anyio
    async def test_session_dir_not_deleted_if_other_sessions(self, tmp_path, caplog):
        """Session directory persists if other session files exist."""
        caplog.set_level(logging.INFO)
        caplog.set_level(logging.DEBUG, logger="fxtr2.data.sqlite_store.sqlite_namespace")
        from pathlib import Path

        db_path = str(tmp_path / "test.sqlite")
        session_dir = Path(db_path + "-fxtrsessions")

        async with SQLiteFxtrSession.open(db_path) as session1:
            async with SQLiteFxtrSession.open(db_path) as session2:
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

    # Memory DB test removed - v4 does not support memory databases

    @pytest.mark.anyio
    async def test_dead_session_detected_and_marked_garbage(self, tmp_path):
        """Dead session files are detected and marked as garbage."""
        import os
        import sqlite3
        from datetime import datetime, timezone
        from pathlib import Path

        db_path = str(tmp_path / "test.sqlite")
        session_dir = Path(db_path + "-fxtrsessions")

        # Set up the database and insert the dead session BEFORE opening a session
        # This avoids racing with the garbage cleanup worker
        pool_group = sqlite_ops.setup_database_and_create_worker_pool_group(db_path)
        pool_group.close()

        dead_session_uuid = uuid.uuid4()

        # Insert a fake dead session using sqlite3
        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO session (public_id, created_at, last_seen_at, is_garbage) VALUES (?,"
                " ?, ?, 0)",
                (dead_session_uuid.bytes, now, now),
            )
        finally:
            conn.close()

        # Now create the fake session file (not locked by anyone)
        session_dir.mkdir(parents=True, exist_ok=True)
        dead_session_file = session_dir / str(dead_session_uuid)
        dead_session_file.write_text(f"{os.getpid()}\n2025-01-01T00:00:00+00:00\n")

        async with SQLiteFxtrSession.open(db_path) as session:
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
            check_conn = sqlite3.connect(db_path, isolation_level=None)
            try:
                cursor = check_conn.execute(
                    "SELECT is_garbage FROM session WHERE public_id = ?",
                    (dead_session_uuid.bytes,),
                )
                row = cursor.fetchone()
                if row is not None:
                    # Session still exists - must be marked as garbage
                    assert row[0] == 1, "Dead session should be marked as garbage"
                # If row is None, session was fully cleaned up - also acceptable
            finally:
                check_conn.close()

    @pytest.mark.anyio
    async def test_live_session_not_marked_dead(self, tmp_path):
        """Live sessions (with held locks) are not marked as dead."""
        from pathlib import Path

        db_path = str(tmp_path / "test.sqlite")
        session_dir = Path(db_path + "-fxtrsessions")

        async with SQLiteFxtrSession.open(db_path) as session1:
            file1 = session_dir / str(session1._external_sid)

            async with SQLiteFxtrSession.open(db_path) as session2:
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

        db_path = str(tmp_path / "test.sqlite")
        session_dir = Path(db_path + "-fxtrsessions")

        # Create an invalid session file
        session_dir.mkdir(parents=True, exist_ok=True)
        invalid_file = session_dir / "not-a-uuid"
        invalid_file.write_text("invalid content")

        async with SQLiteFxtrSession.open(db_path) as session:
            # Detection should clean up the invalid file
            await session._detect_and_mark_dead_sessions()

            # Invalid file should be deleted
            assert not invalid_file.exists()


# =============================================================================
# SQLiteNamespaceArena Load Flat Tests
# =============================================================================


class TestSQLiteNamespaceArenaLoadFlat:
    """Tests for SQLiteNamespaceArena.load_flat."""

    @pytest.mark.anyio
    async def test_load_flat_d_node(self, tmp_db_path_str: str):
        """load_flat returns raw bytes and deps for d-nodes in SQLite."""

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")

            # Create a memory arena and store an entity
            mem_store = MemoryStore()
            _mem_ull = UnconditionalLocalLifetime("load_flat_d")
            mem_store.register_token(_mem_ull.token, name="load_flat_d")
            mem_lease = _mem_ull.root_lease.take()
            mem_arena = mem_store.new_arena(mem_lease.clone())
            ground = FxtrID.from_tag_and_payload(".", b"")
            mem_arena.track_builtin(mem_lease.clone(), ground)

            entity: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
                "data": "test",
            }
            entity_id = mem_arena.store(mem_lease.clone(), entity)

            # Pin to SQLite namespace
            await ns.pin_entity_from_arena(
                "test_pin",
                entity_id,
                mem_arena,
                mem_lease.clone(),
            )

            # Retrieve and load_flat
            retrieved_id, arena = await ns.retrieve_pinned("test_pin")
            flat_data = await arena.load_flat(session.root_lease.clone(), retrieved_id)

            # Check entity_id
            assert flat_data.entity_id == retrieved_id

            # Check entity_bytes
            assert flat_data.entity_bytes is not None

            # Check deps
            assert len(flat_data.deps) > 0
            dep_ids = {dep_id for dep_id, _ in flat_data.deps}
            assert ground in dep_ids

            arena.close()
            mem_arena.close()
            mem_lease.discard()

    @pytest.mark.anyio
    async def test_load_flat_n_node(self, tmp_db_path_str: str):
        """load_flat returns UNWRAPS_TO dep for n-nodes in SQLite."""
        from fxtr2.data.abstract_arena import DepKind

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")

            # Create memory arena
            mem_store = MemoryStore()
            _mem_ull = UnconditionalLocalLifetime("load_flat_n")
            mem_store.register_token(_mem_ull.token, name="load_flat_n")
            mem_lease = _mem_ull.root_lease.take()
            mem_arena = mem_store.new_arena(mem_lease.clone())
            ground = FxtrID.from_tag_and_payload(".", b"")
            mem_arena.track_builtin(mem_lease.clone(), ground)

            # Create context and n-node
            context_data: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
                "value": RawWeakFxtrID(inner_id=ground),
            }
            c_id = mem_arena.create_c_node(mem_lease.clone(), context_data)

            prov_entity: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=c_id),
                "data": "test",
            }
            n_id = mem_arena.store(mem_lease.clone(), prov_entity)

            # Pin and retrieve
            await ns.pin_entity_from_arena(
                "test_pin",
                n_id,
                mem_arena,
                mem_lease.clone(),
            )
            retrieved_id, arena = await ns.retrieve_pinned("test_pin")

            # Load flat
            flat_data = await arena.load_flat(session.root_lease.clone(), retrieved_id)

            # Check entity_bytes
            assert flat_data.entity_bytes is not None

            # Check UNWRAPS_TO dep
            dep_kinds = {kind for _, kind in flat_data.deps}
            assert DepKind.UNWRAPS_TO in dep_kinds

            arena.close()
            mem_arena.close()
            mem_lease.discard()

    @pytest.mark.anyio
    async def test_load_flat_c_node(self, tmp_db_path_str: str):
        """load_flat returns no bytes and correct deps for c-nodes in SQLite."""
        from fxtr2.data.abstract_arena import DepKind

        async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")

            # Create memory arena
            mem_store = MemoryStore()
            _mem_ull = UnconditionalLocalLifetime("load_flat_c")
            mem_store.register_token(_mem_ull.token, name="load_flat_c")
            mem_lease = _mem_ull.root_lease.take()
            mem_arena = mem_store.new_arena(mem_lease.clone())
            ground = FxtrID.from_tag_and_payload(".", b"")
            mem_arena.track_builtin(mem_lease.clone(), ground)

            # Create c-node
            context_data: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
                "value": RawWeakFxtrID(inner_id=ground),
            }
            c_id = mem_arena.create_c_node(mem_lease.clone(), context_data)

            # Pin and retrieve
            await ns.pin_entity_from_arena(
                "test_pin",
                c_id,
                mem_arena,
                mem_lease.clone(),
            )
            retrieved_id, arena = await ns.retrieve_pinned("test_pin")

            # Load flat
            flat_data = await arena.load_flat(session.root_lease.clone(), retrieved_id)

            # Check no bytes
            assert flat_data.entity_bytes is None

            # Check deps
            dep_kinds = {kind for _, kind in flat_data.deps}
            assert DepKind.CTX_NODE in dep_kinds
            assert DepKind.UNWRAPS_TO in dep_kinds

            arena.close()
            mem_arena.close()
            mem_lease.discard()


# =============================================================================
# Archive Export / Import
# =============================================================================


class TestArchiveExport:
    """Tests for SQLiteNamespaceBackend.export_archive."""

    @pytest.mark.anyio
    async def test_export_notar(
        self, tmp_path: Any, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """export_archive(tar=False) creates sqlite + segment files."""
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "value": "archive_test",
        }
        entity_id = _legacy_arena.store(entity)

        db_path = str(tmp_path / "source.sqlite")
        async with SQLiteFxtrSession.open(db_path) as session:
            ns = await session.create_or_retrieve_user_namespace("myns")
            await ns.pin_entity_from_arena(
                "pin1",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            archive_path = tmp_path / "archive.sqlite"
            await ns.export_archive(archive_path, tar=False)

        assert archive_path.exists()
        shards_path = tmp_path / "archive.sqlite.data2.shards"
        assert shards_path.exists()
        assert shards_path.is_dir()
        assert any(shards_path.iterdir())  # At least one segment file

    @pytest.mark.anyio
    async def test_export_tar(
        self, tmp_path: Any, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """export_archive(tar=True) creates a tarball with sqlite + segment files."""
        import tarfile

        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "value": "archive_test",
        }
        entity_id = _legacy_arena.store(entity)

        db_path = str(tmp_path / "source.sqlite")
        async with SQLiteFxtrSession.open(db_path) as session:
            ns = await session.create_or_retrieve_user_namespace("myns")
            await ns.pin_entity_from_arena(
                "pin1",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            archive_path = tmp_path / "archive.tar"
            await ns.export_archive(archive_path, tar=True)

        assert archive_path.exists()
        with tarfile.open(archive_path, "r:") as tf:
            names = tf.getnames()
            assert "store.sqlite" in names
            seg_names = [n for n in names if n.startswith("store.sqlite.data2.shards/")]
            assert len(seg_names) >= 1

    @pytest.mark.anyio
    async def test_export_tar_gz(
        self, tmp_path: Any, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """export_archive(tar=True, gz=True) creates a gzipped tarball."""
        import tarfile

        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "value": "archive_test",
        }
        entity_id = _legacy_arena.store(entity)

        db_path = str(tmp_path / "source.sqlite")
        async with SQLiteFxtrSession.open(db_path) as session:
            ns = await session.create_or_retrieve_user_namespace("myns")
            await ns.pin_entity_from_arena(
                "pin1",
                entity_id,
                _legacy_arena._arena,
                _legacy_arena._lease.clone(),
            )

            archive_path = tmp_path / "archive.tar.gz"
            await ns.export_archive(archive_path, tar=True, gz=True)

        assert archive_path.exists()
        with tarfile.open(archive_path, "r:gz") as tf:
            names = tf.getnames()
            assert "store.sqlite" in names
            seg_names = [n for n in names if n.startswith("store.sqlite.data2.shards/")]
            assert len(seg_names) >= 1

    @pytest.mark.anyio
    async def test_export_multiple_pins(
        self, tmp_path: Any, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """Export handles multiple pins correctly."""
        import tarfile

        entities = {}
        for i in range(5):
            entity: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                "value": f"entity_{i}",
            }
            entities[f"pin_{i}"] = _legacy_arena.store(entity)

        db_path = str(tmp_path / "source.sqlite")
        archive_path = tmp_path / "archive.tar"

        async with SQLiteFxtrSession.open(db_path) as session:
            ns = await session.create_or_retrieve_user_namespace("myns")
            for key, eid in entities.items():
                await ns.pin_entity_from_arena(
                    key,
                    eid,
                    _legacy_arena._arena,
                    _legacy_arena._lease.clone(),
                )
            await ns.export_archive(archive_path, tar=True)

        # Verify archive has expected structure
        with tarfile.open(archive_path, "r:") as tf:
            names = tf.getnames()
            assert "store.sqlite" in names
            seg_names = [n for n in names if n.startswith("store.sqlite.data2.shards/")]
            assert len(seg_names) >= 1


# =============================================================================
# Indexer Registration Tests
# =============================================================================


class TestIndexerRegistration:
    """Tests for indexer registration and cleanup behavior."""

    @pytest.mark.anyio
    async def test_register_indexer_cleanup_on_failure(
        self, tmp_db_path_str: str, _legacy_arena: _LeasedArena, ground_context: FxtrID
    ):
        """Failed registration is cleaned up, allowing re-registration after fixing the issue."""
        from fxtr2.data.entity_ref_scope import FxtrScope
        from fxtr2.data.store_indexing.indexers import DerefMarker, PathKey, TypedPathValueIndexer
        from fxtr2.data.store_indexing.indexing_types import IndexerColumnType

        # Create a strict indexer that expects entities to have a "name" field
        indexer = TypedPathValueIndexer(
            initial_lookup=DerefMarker(),
            strict=True,
            path=(PathKey(key="name"),),
            var_types=(),
            value_type=IndexerColumnType.STR,
        )

        # Store the indexer in a scope
        async with FxtrScope(name="test_scope") as scope:
            indexer_result = indexer.store(scope=scope)
            indexer_id = indexer_result.entity_id
            indexer_arena = scope._main_arena.copy_to_new_arena(
                scope.clone_authorization(), [indexer_id]
            )

            simple_type = _legacy_arena.track_builtin(FxtrID.builtin("testing.simple_entity"))

            # Create valid entities with "name" field
            valid_entities = []
            for i in range(10):
                entity: FxtrCbor = {
                    "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                    "$fxtr.type": RawWeakFxtrID(inner_id=simple_type),
                    "name": f"entity_{i}",
                }
                valid_entities.append((f"valid_{i}", _legacy_arena.store(entity)))

            # Create an invalid entity missing the "name" field
            invalid_entity: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
                "$fxtr.type": RawWeakFxtrID(inner_id=simple_type),
                "other_field": "value",
            }
            invalid_entity_id = _legacy_arena.store(invalid_entity)

            async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
                ns = await session.create_or_retrieve_user_namespace("test_ns")

                # Pin all valid entities and the invalid one
                for key, eid in valid_entities:
                    await ns.pin_entity_from_arena(
                        key,
                        eid,
                        _legacy_arena._arena,
                        _legacy_arena._lease.clone(),
                    )
                await ns.pin_entity_from_arena(
                    "invalid",
                    invalid_entity_id,
                    _legacy_arena._arena,
                    _legacy_arena._lease.clone(),
                )

                # First registration attempt should fail due to the invalid entity
                with pytest.raises(ValueError, match="not found"):
                    indexer_arena_copy = indexer_arena.copy_to_new_arena(
                        scope.clone_authorization(), [indexer_id]
                    )
                    await ns.register_indexer(
                        indexer_id,
                        indexer_arena_copy,
                        scope.clone_authorization(),
                    )

                # Verify the indexer is not registered
                registrations, indexers_arena = await ns.list_indexers()
                try:
                    assert len(registrations) == 0
                finally:
                    indexers_arena.close()

                # Unpin the invalid entity
                await ns.unpin_entity("invalid")

                # Second registration attempt should succeed if cleanup worked properly
                # If cleanup didn't work, we'd get a RuntimeError about the indexer already being registered
                indexer_arena_copy2 = indexer_arena.copy_to_new_arena(
                    scope.clone_authorization(), [indexer_id]
                )
                await ns.register_indexer(
                    indexer_id,
                    indexer_arena_copy2,
                    scope.clone_authorization(),
                )

                # Verify the indexer is now properly registered
                registrations, indexers_arena = await ns.list_indexers()
                try:
                    assert len(registrations) == 1
                    assert registrations[0][0] == indexer_id
                finally:
                    indexers_arena.close()

            indexer_arena.close()


# =============================================================================
# fetch_entity_segment_locations: too many SQL variables
# =============================================================================


class TestFetchEntitySegmentLocationsSQLVariableLimit:
    """Regression test for sqlite3.OperationalError: too many SQL variables.

    When the number of entity internal IDs exceeds the SQLite variable limit,
    fetch_entity_segment_locations must still work by avoiding an unbounded
    IN (...) placeholder list.
    """

    def test_many_ids_exceeds_variable_limit(self, tmp_db_path_str: str):
        """fetch_entity_segment_locations works even when ID count exceeds the SQL variable limit."""
        import sqlite3 as _sqlite3

        conn = _sqlite3.connect(tmp_db_path_str, isolation_level=None)
        try:
            # Create minimal schema
            conn.execute(
                "CREATE TABLE IF NOT EXISTS entity ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  public_id BLOB UNIQUE,"
                "  storage_location VARCHAR(10) NOT NULL,"
                "  seg_id INTEGER,"
                "  seg_offset INTEGER,"
                "  seg_length INTEGER"
                ")"
            )

            # Insert enough entities to exceed a low variable limit.
            # We use 200 entities and set the limit to 100 to simulate the
            # real-world scenario where a user has thousands of entities.
            n_entities = 200
            for i in range(n_entities):
                conn.execute(
                    "INSERT INTO entity (public_id, storage_location, seg_id, seg_offset, seg_length)"
                    " VALUES (?, 'segment', ?, ?, ?)",
                    (f"ent_{i}".encode(), 1, i * 100, 100),
                )

            all_ids = [row[0] for row in conn.execute("SELECT id FROM entity").fetchall()]
            assert len(all_ids) == n_entities

            # Lower the SQLITE_LIMIT_VARIABLE_NUMBER to 100 so we hit the
            # "too many SQL variables" error with just 200 IDs.
            SQLITE_LIMIT_VARIABLE_NUMBER = 9
            conn.setlimit(SQLITE_LIMIT_VARIABLE_NUMBER, 100)

            result = sqlite_ops.fetch_entity_segment_locations(conn, all_ids)

            assert len(result) == n_entities
            # Verify every entry was returned correctly
            for row_id in all_ids:
                assert row_id in result
                seg_id, seg_offset, seg_length = result[row_id]
                assert seg_id == 1
                assert seg_length == 100
        finally:
            conn.close()
