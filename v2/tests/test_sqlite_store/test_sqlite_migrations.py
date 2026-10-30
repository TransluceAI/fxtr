"""Tests for database schema migrations."""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fxtr2.data.sqlite_store import sqlite_ops
from fxtr2.data.sqlite_store.sqlite_migrations import (
    DB_USER_VERSION,
    migrate_v2_to_v3,
    migrate_v5_to_v6,
    run_migrations,
)


class TestDatabaseMigrations:
    """Tests for database schema migrations."""

    def test_migrate_v2_to_v3(self, tmp_path: Path):
        """Migration from v2 to v3 schema adds the expected indices."""
        db_path = tmp_path / "test_migration.sqlite"

        # Create v2 schema (without the new indices)
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            self._create_v2_schema(conn)
            conn.execute("PRAGMA user_version = 2")

            # Verify indices don't exist yet
            indices_before = self._get_index_names(conn)
            assert "ix_arena_session" not in indices_before
            assert "ix_arena_namespace" not in indices_before
            assert "ix_arena_membership_entity" not in indices_before
            assert "ix_namespace_pin_entity" not in indices_before
            assert "ix_namespace_name_kind" not in indices_before

            # Run migration
            migrate_v2_to_v3(conn)

            # Verify version updated
            cursor = conn.execute("PRAGMA user_version")
            assert cursor.fetchone()[0] == 3

            # Verify indices now exist
            indices_after = self._get_index_names(conn)
            assert "ix_arena_session" in indices_after
            assert "ix_arena_namespace" in indices_after
            assert "ix_arena_membership_entity" in indices_after
            assert "ix_namespace_pin_entity" in indices_after
            assert "ix_namespace_name_kind" in indices_after
        finally:
            conn.close()

    def test_migrate_v2_to_v3_via_setup(self, tmp_path: Path):
        """Migration runs v2->v3, then fails on v3->v4 (which requires manual migration)."""
        db_path = tmp_path / "test_migration.sqlite"

        # Create v2 schema
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            self._create_v2_schema(conn)
            self._insert_v2_test_data(conn)
            conn.execute("PRAGMA user_version = 2")
        finally:
            conn.close()

        # Open via setup_database_and_create_worker_pool_group - should run v2->v3, then fail on v3->v4
        with pytest.raises(ValueError, match="Cannot automatically migrate from v3 to v4"):
            sqlite_ops.setup_database_and_create_worker_pool_group(str(db_path))

    def test_fresh_database_gets_current_version(self, tmp_db_path_str: str):
        """Fresh database is created with current version and all indices."""
        pool_group = sqlite_ops.setup_database_and_create_worker_pool_group(tmp_db_path_str)
        try:
            conn = sqlite3.connect(tmp_db_path_str, isolation_level=None)
            try:
                # Check version
                cursor = conn.execute("PRAGMA user_version")
                assert cursor.fetchone()[0] == DB_USER_VERSION

                # Check indices exist
                indices = self._get_index_names(conn)
                assert "ix_arena_session" in indices
                assert "ix_arena_namespace" in indices
                assert "ix_arena_membership_entity" in indices
                assert "ix_namespace_pin_entity" in indices
                assert "ix_namespace_name_kind" in indices
            finally:
                conn.close()
        finally:
            pool_group.close()

    def test_future_version_raises_error(self, tmp_path: Path):
        """Database from future version raises ValueError."""
        db_path = tmp_path / "future.sqlite"

        # Create database with future version
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            conn.execute(f"PRAGMA user_version = {DB_USER_VERSION + 1}")
        finally:
            conn.close()

        with pytest.raises(ValueError, match="newer than supported"):
            sqlite_ops.setup_database_and_create_worker_pool_group(str(db_path))

    def test_run_migrations_no_path_raises_error(self, tmp_path: Path):
        """Missing migration raises ValueError."""
        db_path = tmp_path / "test.sqlite"
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            # Try to migrate from version 1 (which we don't support)
            with pytest.raises(ValueError, match="No migration path from version 1"):
                run_migrations(conn, current_version=1, target_version=3)
        finally:
            conn.close()

    def test_migrate_v3_to_v4_raises_error(self, tmp_path: Path):
        """v3->v4 migration is not automatic and raises an error with instructions."""
        db_path = tmp_path / "test_v3.sqlite"

        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            self._create_v2_schema(conn)
            conn.execute("PRAGMA user_version = 2")
            migrate_v2_to_v3(conn)
        finally:
            conn.close()

        conn = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        finally:
            conn.close()

        with pytest.raises(ValueError, match="Cannot automatically migrate from v3 to v4"):
            sqlite_ops.setup_database_and_create_worker_pool_group(str(db_path))

    # -------------------------------------------------------------------------
    # Helper methods
    # -------------------------------------------------------------------------

    def _get_index_names(self, conn: sqlite3.Connection) -> set[str]:
        """Get all index names in the database."""
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        )
        return {row[0] for row in cursor.fetchall()}

    def _create_v2_schema(self, conn: sqlite3.Connection) -> None:
        """Create v2 database schema (without the v3 indices)."""
        # Entity tables
        conn.execute(
            """
            CREATE TABLE entity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_id BLOB NOT NULL UNIQUE,
                storage_location VARCHAR(10) NOT NULL
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE entity_dep (
                requirer_id INTEGER NOT NULL REFERENCES entity(id),
                required_id INTEGER NOT NULL REFERENCES entity(id),
                kind VARCHAR(20) NOT NULL,
                UNIQUE (requirer_id, required_id, kind)
            )
        """
        )
        conn.execute("CREATE INDEX ix_entity_dep_requirer ON entity_dep(requirer_id)")
        conn.execute(
            """
            CREATE TABLE stored_entity_data (
                entity_id INTEGER PRIMARY KEY REFERENCES entity(id),
                data BLOB NOT NULL
            )
        """
        )

        # Namespace tables
        conn.execute(
            """
            CREATE TABLE namespace (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR NOT NULL,
                kind VARCHAR NOT NULL,
                public_id BLOB(16) NOT NULL UNIQUE
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE namespace_entity_ownership (
                namespace_id INTEGER NOT NULL REFERENCES namespace(id),
                entity_id INTEGER NOT NULL REFERENCES entity(id),
                UNIQUE (namespace_id, entity_id)
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE namespace_entity_backref (
                requirer_id INTEGER NOT NULL REFERENCES entity(id),
                required_id INTEGER NOT NULL REFERENCES entity(id),
                namespace_id INTEGER NOT NULL REFERENCES namespace(id),
                kind VARCHAR(20) NOT NULL,
                UNIQUE (requirer_id, required_id, namespace_id, kind)
            )
        """
        )
        conn.execute(
            """
            CREATE INDEX ix_namespace_entity_backref_required_ns
            ON namespace_entity_backref(required_id, namespace_id)
        """
        )
        conn.execute(
            """
            CREATE TABLE namespace_pin (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                namespace_id INTEGER NOT NULL REFERENCES namespace(id),
                entity_id INTEGER NOT NULL REFERENCES entity(id),
                name VARCHAR NOT NULL,
                UNIQUE (namespace_id, name)
            )
        """
        )

        # Session tables
        conn.execute(
            """
            CREATE TABLE session (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_id BLOB(16) NOT NULL UNIQUE,
                created_at DATETIME NOT NULL,
                last_seen_at DATETIME NOT NULL,
                is_garbage BOOLEAN NOT NULL DEFAULT 0,
                is_collecting_garbage BOOLEAN NOT NULL DEFAULT 0
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE arena (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_id BLOB(16) NOT NULL UNIQUE,
                namespace_id INTEGER NOT NULL REFERENCES namespace(id),
                session_id INTEGER NOT NULL REFERENCES session(id)
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE arena_membership (
                arena_id INTEGER NOT NULL REFERENCES arena(id),
                entity_id INTEGER NOT NULL REFERENCES entity(id),
                UNIQUE (arena_id, entity_id)
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE entity_reservations (
                session_id INTEGER NOT NULL REFERENCES session(id),
                entity_id INTEGER NOT NULL REFERENCES entity(id),
                count INTEGER NOT NULL DEFAULT 1,
                UNIQUE (session_id, entity_id)
            )
        """
        )

    def _insert_v2_test_data(self, conn: sqlite3.Connection) -> None:
        """Insert test data for v2 schema (garbage session required for setup)."""
        now = datetime.now(timezone.utc)
        garbage_uuid = uuid.UUID(int=0)

        # Create garbage session (required by setup_database_and_create_worker_pool_group)
        conn.execute(
            """
            INSERT INTO session (id, public_id, created_at, last_seen_at, is_garbage, is_collecting_garbage)
            VALUES (0, ?, ?, ?, 1, 0)
            """,
            (garbage_uuid.bytes, now.isoformat(), now.isoformat()),
        )


class TestV5ToV6Migration:
    """Tests for v5 → v6 migration (LMDB to segment files)."""

    def test_migration_with_entities(self, tmp_path: Path):
        """Full migration from v5 to v6: LMDB data migrates to segment files."""
        import lmdb

        from fxtr2.data.sqlite_store import lmdb_blob
        from fxtr2.data.sqlite_store.entity_blob import (
            decode_cbor_bytes,
            decode_fxtr_id_bytes,
            decode_metadata,
        )
        from fxtr2.data.sqlite_store.segment_io import SegmentManager

        db_path = tmp_path / "test.sqlite"

        # Create v5 database with LMDB data
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            _create_v5_schema(conn)

            # Insert two entities: entity 1 (base), entity 2 (depends on 1)
            conn.execute(
                "INSERT INTO entity (id, public_id, storage_location) " "VALUES (1, ?, 'data1/1')",
                (b"entity_one",),
            )
            conn.execute(
                "INSERT INTO entity (id, public_id, storage_location) " "VALUES (2, ?, 'data1/1')",
                (b"entity_two",),
            )
            conn.execute(
                "INSERT INTO entity_dep (requirer_id, required_id, kind) "
                "VALUES (2, 1, 'ctx_node')"
            )

            # Write corresponding LMDB blobs
            lmdb_path = f"{db_path}.data1.mdb"
            lmdb_env = lmdb.open(
                lmdb_path,
                subdir=False,
                map_size=1024 * 1024 * 100,
                writemap=True,
                max_dbs=1,
            )
            lmdb_data_db = lmdb_env.open_db(b"data1/1", create=True)

            with lmdb_env.begin(db=lmdb_data_db, write=True) as txn:
                # Entity 1: base node, no deps
                metadata1 = {
                    "deps": [],
                    "precomp": {1: [b"entity_one", None, None, None]},
                }
                blob1 = lmdb_blob.encode_blob(b"entity_one", b"cbor_data_1", metadata1)
                txn.put(int.to_bytes(1, 8, "little"), blob1)

                # Entity 2: depends on entity 1 via ctx_node
                metadata2 = {
                    "deps": [[1, "ctx_node"]],
                    "precomp": {
                        2: [b"entity_two", None, 1, None],
                        1: [b"entity_one", None, None, None],
                    },
                }
                blob2 = lmdb_blob.encode_blob(b"entity_two", b"cbor_data_2", metadata2)
                txn.put(int.to_bytes(2, 8, "little"), blob2)

            lmdb_env.close()
            conn.execute("PRAGMA user_version = 5")
        finally:
            conn.close()

        # Run migration
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            migrate_v5_to_v6(conn)

            # Verify version
            cursor = conn.execute("PRAGMA user_version")
            assert cursor.fetchone()[0] == 6

            # Verify entity_reservations dropped
            cursor = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='entity_reservations'"
            )
            assert cursor.fetchone() is None

            # Verify segment_metadata exists with correct state
            cursor = conn.execute("SELECT segment_id, status, write_cursor FROM segment_metadata")
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == 0  # segment_id
            assert row[1] == "active"
            assert row[2] > 0  # write_cursor > 0 (we wrote data)

            # Verify entity table has seg columns populated
            cursor = conn.execute(
                "SELECT seg_id, seg_offset, seg_length, storage_location "
                "FROM entity WHERE id = 1"
            )
            row1 = cursor.fetchone()
            assert row1[0] == 0  # seg_id
            assert row1[1] is not None  # seg_offset
            assert row1[2] is not None  # seg_length
            assert row1[3] == "data2/seg"

            cursor = conn.execute(
                "SELECT seg_id, seg_offset, seg_length, storage_location "
                "FROM entity WHERE id = 2"
            )
            row2 = cursor.fetchone()
            assert row2[0] == 0
            assert row2[1] > row1[1]  # entity 2 written after entity 1
            assert row2[3] == "data2/seg"

            # Verify segment file data is readable
            segments_dir = f"{db_path}.data2.shards"
            mgr = SegmentManager(segments_dir)
            try:
                # Read entity 1
                blob1 = mgr.read_record(0, row1[1], row1[2])
                assert decode_fxtr_id_bytes(blob1) == b"entity_one"
                assert decode_cbor_bytes(blob1) == b"cbor_data_1"

                # Read entity 2
                blob2 = mgr.read_record(0, row2[1], row2[2])
                assert decode_fxtr_id_bytes(blob2) == b"entity_two"
                assert decode_cbor_bytes(blob2) == b"cbor_data_2"

                # Verify precomp entries have 5th element (same-segment offsets)
                _deps1, precomp1 = decode_metadata(blob1)
                # Entity 1's own entry: seg_offset is None (self)
                assert len(precomp1[1]) == 5
                assert precomp1[1][4] is None

                _deps2, precomp2 = decode_metadata(blob2)
                assert precomp2[1][4] == row1[1]  # entity 1's offset in the segment
                assert precomp2[2][4] is None  # entity 2's own entry
            finally:
                mgr.close()
        finally:
            conn.close()

    def test_migration_no_entities(self, tmp_path: Path):
        """Migration works on database with no LMDB entities."""
        db_path = tmp_path / "test.sqlite"

        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            _create_v5_schema(conn)
            conn.execute("PRAGMA user_version = 5")
        finally:
            conn.close()

        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            migrate_v5_to_v6(conn)

            cursor = conn.execute("PRAGMA user_version")
            assert cursor.fetchone()[0] == 6

            # Segment directory and file should exist
            segments_dir = f"{db_path}.data2.shards"
            seg_path = os.path.join(segments_dir, "seg_00000000.bin")
            assert os.path.isdir(segments_dir)
            assert os.path.isfile(seg_path)
            assert os.path.getsize(seg_path) == 0  # Empty

            # write_cursor should be 0
            cursor = conn.execute("SELECT write_cursor FROM segment_metadata WHERE segment_id = 0")
            assert cursor.fetchone()[0] == 0
        finally:
            conn.close()

    def test_migration_entity_reservations_dropped(self, tmp_path: Path):
        """Migration drops entity_reservations table even if it has data."""
        db_path = tmp_path / "test.sqlite"

        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            _create_v5_schema(conn)
            # Insert a reservation row to verify it gets dropped
            conn.execute(
                "INSERT INTO entity_reservations (session_id, entity_id, count) " "VALUES (0, 0, 1)"
            )
            conn.execute("PRAGMA user_version = 5")
        finally:
            conn.close()

        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            migrate_v5_to_v6(conn)

            cursor = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='entity_reservations'"
            )
            assert cursor.fetchone() is None
        finally:
            conn.close()


def _create_v5_schema(conn: sqlite3.Connection) -> None:
    """Create a minimal v5 database schema for migration testing."""
    conn.execute(
        """CREATE TABLE entity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id BLOB UNIQUE,
        storage_location VARCHAR(10) NOT NULL
    )"""
    )
    conn.execute(
        """CREATE TABLE entity_dep (
        requirer_id INTEGER NOT NULL REFERENCES entity(id),
        required_id INTEGER NOT NULL REFERENCES entity(id),
        kind VARCHAR(20) NOT NULL,
        UNIQUE (requirer_id, required_id, kind)
    )"""
    )
    conn.execute("CREATE INDEX ix_entity_dep_requirer ON entity_dep(requirer_id)")
    conn.execute(
        """CREATE TABLE entity_reservations (
        session_id INTEGER NOT NULL,
        entity_id INTEGER NOT NULL,
        count INTEGER NOT NULL DEFAULT 1,
        UNIQUE (session_id, entity_id)
    )"""
    )
    conn.execute(
        """CREATE TABLE namespace (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name VARCHAR NOT NULL,
        kind VARCHAR NOT NULL,
        public_id BLOB(16) NOT NULL UNIQUE
    )"""
    )
    conn.execute("CREATE INDEX ix_namespace_name_kind ON namespace(name, kind)")
    conn.execute(
        """CREATE TABLE namespace_entity_ownership (
        namespace_id INTEGER NOT NULL REFERENCES namespace(id),
        entity_id INTEGER NOT NULL REFERENCES entity(id),
        UNIQUE (namespace_id, entity_id)
    )"""
    )
    conn.execute(
        """CREATE TABLE namespace_entity_backref (
        requirer_id INTEGER NOT NULL REFERENCES entity(id),
        required_id INTEGER NOT NULL REFERENCES entity(id),
        namespace_id INTEGER NOT NULL REFERENCES namespace(id),
        kind VARCHAR(20) NOT NULL,
        UNIQUE (requirer_id, required_id, namespace_id, kind)
    )"""
    )
    conn.execute(
        """CREATE INDEX ix_namespace_entity_backref_required_ns
        ON namespace_entity_backref(required_id, namespace_id)"""
    )
    conn.execute(
        """CREATE TABLE namespace_pin (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        namespace_id INTEGER NOT NULL REFERENCES namespace(id),
        entity_id INTEGER NOT NULL REFERENCES entity(id),
        name VARCHAR NOT NULL,
        UNIQUE (namespace_id, name)
    )"""
    )
    conn.execute("CREATE INDEX ix_namespace_pin_entity ON namespace_pin(entity_id, namespace_id)")
    conn.execute(
        """CREATE TABLE session (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id BLOB(16) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL,
        last_seen_at DATETIME NOT NULL,
        is_garbage BOOLEAN NOT NULL DEFAULT 0,
        is_collecting_garbage BOOLEAN NOT NULL DEFAULT 0
    )"""
    )
    conn.execute(
        """CREATE TABLE arena (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id BLOB(16) NOT NULL UNIQUE,
        namespace_id INTEGER NOT NULL REFERENCES namespace(id),
        session_id INTEGER NOT NULL REFERENCES session(id)
    )"""
    )
    conn.execute("CREATE INDEX ix_arena_session ON arena(session_id)")
    conn.execute("CREATE INDEX ix_arena_namespace ON arena(namespace_id)")
    conn.execute(
        """CREATE TABLE arena_membership (
        arena_id INTEGER NOT NULL REFERENCES arena(id),
        entity_id INTEGER NOT NULL REFERENCES entity(id),
        UNIQUE (arena_id, entity_id)
    )"""
    )
    conn.execute("CREATE INDEX ix_arena_membership_entity ON arena_membership(entity_id)")
    conn.execute(
        """CREATE TABLE ns_index_registry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        namespace_id INTEGER NOT NULL REFERENCES namespace(id),
        indexer_entity_id INTEGER NOT NULL REFERENCES entity(id),
        key_prefix VARCHAR NOT NULL DEFAULT '',
        state VARCHAR NOT NULL CHECK(state IN ('BUILDING', 'INDEXED', 'GARBAGE')),
        building_session_id INTEGER REFERENCES session(id),
        key_types VARCHAR NOT NULL DEFAULT '',
        value_type VARCHAR NOT NULL DEFAULT '',
        UNIQUE(namespace_id, indexer_entity_id, key_prefix)
    )"""
    )
