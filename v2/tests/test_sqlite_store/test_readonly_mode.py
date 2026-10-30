"""Tests for read-only mode in SQLiteFxtrSession and SQLiteNamespaceBackend."""

from __future__ import annotations

import sqlite3
import typing
from pathlib import Path
from typing import Any

import pytest

from fxtr2.data.entity_core import FxtrCbor, FxtrID, RawFxtrEntityMarkerID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import FxtrScope, get_active_scope
from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr2.data.memory_arena import MemoryArena
from fxtr2.data.sqlite_store.sqlite_namespace import SQLiteFxtrSession
from fxtr2.data.store_indexing.indexers import (
    DerefMarker,
    PathKey,
    TypedPathValueIndexer,
    VarMarker,
)
from fxtr2.data.store_indexing.indexing_types import IndexerColumnType, QueryView
from fxtr2.leases.lease_types import Lease


@pytest.fixture
async def populated_db(
    tmp_db_path_str: str,
    memory_arena: tuple[MemoryArena[Any], Lease[Any]],
    ground_context: FxtrID,
) -> tuple[str, FxtrID]:
    """Create a DB with a namespace and a pinned entity, return (db_path, entity_id)."""
    arena, lease = memory_arena
    entity: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
        "name": "test_entity",
        "value": 42,
    }
    entity_id = arena.store(lease.clone(), entity)

    async with SQLiteFxtrSession.open(tmp_db_path_str) as session:
        ns = await session.create_or_retrieve_user_namespace("test_ns")
        await ns.pin_entity_from_arena(
            "my_pin",
            entity_id,
            arena,
            lease.clone(),
        )
        await ns.pin_entity_from_arena(
            "other_pin",
            entity_id,
            arena,
            lease.clone(),
            overwrite=True,
        )

    return tmp_db_path_str, entity_id


class TestReadOnlySessionLifecycle:
    """Tests for read-only session startup and lifecycle."""

    @pytest.mark.anyio
    async def test_readonly_session_opens(self, populated_db: tuple[str, FxtrID]):
        """A read-only session opens successfully on an existing DB."""
        db_path, _ = populated_db
        async with SQLiteFxtrSession.open(db_path, read_only=True) as session:
            assert session.is_active()
            assert session._read_only

    @pytest.mark.anyio
    async def test_readonly_session_is_not_active_after_close(
        self, populated_db: tuple[str, FxtrID]
    ):
        """A read-only session reports not active after close."""
        db_path, _ = populated_db
        async with SQLiteFxtrSession.open(db_path, read_only=True) as session:
            pass
        assert not session.is_active()

    @pytest.mark.anyio
    async def test_readonly_session_no_session_row(self, populated_db: tuple[str, FxtrID]):
        """A read-only session does not insert a row into the session table."""
        db_path, _ = populated_db

        # Count sessions before
        conn = sqlite3.connect(db_path, isolation_level=None)
        before = conn.execute("SELECT COUNT(*) FROM session").fetchone()[0]
        conn.close()

        async with SQLiteFxtrSession.open(db_path, read_only=True) as session:
            # Count sessions during
            conn = sqlite3.connect(db_path, isolation_level=None)
            during = conn.execute("SELECT COUNT(*) FROM session").fetchone()[0]
            conn.close()
            assert during == before, "Read-only session should not insert a session row"

    @pytest.mark.anyio
    async def test_readonly_session_no_session_file(self, populated_db: tuple[str, FxtrID]):
        """A read-only session does not create a session file."""
        db_path, _ = populated_db
        session_dir = Path(db_path + "-fxtrsessions")

        async with SQLiteFxtrSession.open(db_path, read_only=True) as session:
            if session_dir.exists():
                # Our session UUID should not appear in the session files
                our_uuid = str(session._external_sid)
                session_files = [f.name for f in session_dir.iterdir() if f.is_file()]
                assert our_uuid not in session_files


class TestReadOnlyReads:
    """Tests for read operations in read-only mode."""

    @pytest.mark.anyio
    async def test_retrieve_pinned_entity(self, populated_db: tuple[str, FxtrID]):
        """retrieve_pinned() works in read-only mode."""
        db_path, entity_id = populated_db

        async with SQLiteFxtrSession.open(db_path, read_only=True) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")
            retrieved_id, arena = await ns.retrieve_pinned("my_pin")
            assert retrieved_id == entity_id

            loaded = await typing.cast(Any, arena.load(session.root_lease.clone(), retrieved_id))
            assert loaded["name"] == "test_entity"
            assert loaded["value"] == 42
            arena.close()

    @pytest.mark.anyio
    async def test_list_pins(self, populated_db: tuple[str, FxtrID]):
        """list_pins() works in read-only mode."""
        db_path, entity_id = populated_db

        async with SQLiteFxtrSession.open(db_path, read_only=True) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")
            pins = await ns.list_pins()
            assert set(pins.keys()) == {"my_pin", "other_pin"}
            assert pins["my_pin"] == entity_id

    @pytest.mark.anyio
    async def test_try_lookup(self, populated_db: tuple[str, FxtrID]):
        """try_lookup() works in read-only mode."""
        db_path, entity_id = populated_db

        async with SQLiteFxtrSession.open(db_path, read_only=True) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")
            arena = await ns.try_lookup(entity_id)
            assert arena is not None
            arena.close()

    @pytest.mark.anyio
    async def test_retrieve_nonexistent_raises(self, populated_db: tuple[str, FxtrID]):
        """retrieve_pinned() raises KeyError for nonexistent keys in read-only mode."""
        db_path, _ = populated_db

        async with SQLiteFxtrSession.open(db_path, read_only=True) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")
            with pytest.raises(KeyError):
                await ns.retrieve_pinned("nonexistent")


class TestReadOnlyWriteGuards:
    """Tests that write operations raise RuntimeError in read-only mode."""

    @pytest.mark.anyio
    async def test_pin_raises(
        self,
        populated_db: tuple[str, FxtrID],
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ):
        """pin_entity_from_arena() raises in read-only mode."""
        db_path, _ = populated_db
        arena, lease = memory_arena
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "new",
        }
        entity_id = arena.store(lease.clone(), entity)

        async with SQLiteFxtrSession.open(db_path, read_only=True) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")
            with pytest.raises(RuntimeError, match="read-only"):
                await ns.pin_entity_from_arena(
                    "new_pin",
                    entity_id,
                    arena,
                    lease.clone(),
                )

    @pytest.mark.anyio
    async def test_unpin_raises(self, populated_db: tuple[str, FxtrID]):
        """unpin_entity() raises in read-only mode."""
        db_path, _ = populated_db

        async with SQLiteFxtrSession.open(db_path, read_only=True) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")
            with pytest.raises(RuntimeError, match="read-only"):
                await ns.unpin_entity("my_pin")

    @pytest.mark.anyio
    async def test_register_indexer_raises(self, populated_db: tuple[str, FxtrID]):
        """register_indexer() raises in read-only mode."""
        db_path, entity_id = populated_db

        async with SQLiteFxtrSession.open(db_path, read_only=True) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")
            # Retrieve a pinned entity to use as a fake indexer
            fid, arena = await ns.retrieve_pinned("my_pin")
            with pytest.raises(RuntimeError, match="read-only"):
                await ns.register_indexer(
                    fid,
                    arena,
                    session.root_lease.clone(),
                )
            arena.close()

    @pytest.mark.anyio
    async def test_unregister_indexer_raises(self, populated_db: tuple[str, FxtrID]):
        """unregister_indexer() raises in read-only mode."""
        db_path, entity_id = populated_db

        async with SQLiteFxtrSession.open(db_path, read_only=True) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")
            with pytest.raises(RuntimeError, match="read-only"):
                await ns.unregister_indexer(entity_id)

    @pytest.mark.anyio
    async def test_export_archive_raises(self, populated_db: tuple[str, FxtrID], tmp_path: Path):
        """export_archive() raises in read-only mode."""
        db_path, _ = populated_db

        async with SQLiteFxtrSession.open(db_path, read_only=True) as session:
            ns = await session.create_or_retrieve_user_namespace("test_ns")
            with pytest.raises(RuntimeError, match="read-only"):
                await ns.export_archive(tmp_path / "archive.tar")


class TestReadOnlyErrorCases:
    """Tests for read-only mode error conditions."""

    @pytest.mark.anyio
    async def test_readonly_missing_db_raises(self, tmp_db_path_str: str):
        """Opening a read-only session on a nonexistent DB raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            async with SQLiteFxtrSession.open(tmp_db_path_str, read_only=True):
                pass

    @pytest.mark.anyio
    async def test_readonly_wrong_version_raises(self, tmp_db_path_str: str):
        """Opening a read-only session on a DB with wrong version raises ValueError."""
        # Create a DB with a different user_version
        conn = sqlite3.connect(tmp_db_path_str, isolation_level=None)
        conn.execute("PRAGMA user_version = 99")
        conn.close()

        with pytest.raises(ValueError, match="version"):
            async with SQLiteFxtrSession.open(tmp_db_path_str, read_only=True):
                pass

    @pytest.mark.anyio
    async def test_readonly_nonexistent_namespace_raises(self, populated_db: tuple[str, FxtrID]):
        """create_or_retrieve_user_namespace raises KeyError for nonexistent namespace in read-only mode."""
        db_path, _ = populated_db

        async with SQLiteFxtrSession.open(db_path, read_only=True) as session:
            with pytest.raises(KeyError, match="does_not_exist"):
                await session.create_or_retrieve_user_namespace("does_not_exist")


class TestReadOnlyConcurrency:
    """Tests for read-only mode alongside read-write sessions."""

    @pytest.mark.anyio
    async def test_readonly_alongside_readwrite(
        self,
        tmp_db_path_str: str,
        memory_arena: tuple[MemoryArena[Any], Lease[Any]],
        ground_context: FxtrID,
    ):
        """A read-only session can coexist with a read-write session."""
        arena, lease = memory_arena
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground_context),
            "data": "test",
        }
        entity_id = arena.store(lease.clone(), entity)

        async with SQLiteFxtrSession.open(tmp_db_path_str) as rw_session:
            ns_rw = await rw_session.create_or_retrieve_user_namespace("test_ns")
            await ns_rw.pin_entity_from_arena(
                "pin1",
                entity_id,
                arena,
                lease.clone(),
            )

            # Open a read-only session on the same DB
            async with SQLiteFxtrSession.open(tmp_db_path_str, read_only=True) as ro_session:
                ns_ro = await ro_session.create_or_retrieve_user_namespace("test_ns")
                pins = await ns_ro.list_pins()
                assert "pin1" in pins


_SCORE_INDEXER = TypedPathValueIndexer(
    initial_lookup=DerefMarker(),
    strict=False,
    path=(PathKey(key="items"), VarMarker(), PathKey(key="score")),
    var_types=(IndexerColumnType.INT,),
    value_type=IndexerColumnType.FLOAT,
)


def _typed_entity(scope: FxtrScope, **fields: object) -> FxtrID:
    """Build and store a typed CBOR entity (needed for indexing)."""
    ctx = RawFxtrEntityMarkerID(inner_id=FxtrID.from_tag_and_payload(".", b""))
    cbor: FxtrCbor = {  # type: ignore[assignment]
        "$fxtr.ctx": ctx,
        "$fxtr.type": RawWeakFxtrID(
            inner_id=scope.track_builtin(FxtrID.builtin("testing.simple_entity"))
        ),
        **fields,
    }
    return scope.store_cbor(cbor)


class TestReadOnlyIndexQueries:
    """Tests for index query operations (sql, iter_matches) in read-only mode."""

    @pytest.mark.anyio
    async def test_sql_query(self, tmp_db_path_str: str):
        """sql() queries work in read-only mode against a pre-indexed store."""
        # Populate in read-write mode: register indexer and pin entities
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns") as ns:
            await ns.register_indexer(_SCORE_INDEXER)

            e1_id = _typed_entity(get_active_scope(), items=[{"score": 4.5}, {"score": 2.1}])
            await ns.pin("e1", e1_id)

            e2_id = _typed_entity(get_active_scope(), items=[{"score": 7.0}])
            await ns.pin("e2", e2_id)

        # Query in read-only mode
        async with SQLiteFxtrNamespace(tmp_db_path_str, "test_ns", read_only=True) as ns:
            rows = await ns.sql(
                "SELECT i, score, pin_name FROM v ORDER BY score",
                views=dict(
                    v=QueryView(score=_SCORE_INDEXER.bind(("i",), "1=1")),
                ),
            )
            assert len(rows) == 3
            scores = [r["score"] for r in rows]
            assert scores == sorted(scores)
            pin_names = {r["pin_name"] for r in rows}
            assert pin_names == {"e1", "e2"}
