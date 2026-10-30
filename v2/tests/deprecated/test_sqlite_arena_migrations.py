"""Tests for database schema migrations."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from fxtr2.data.deprecated.sqlite_arena import DB_USER_VERSION, create_fxtr_engine, init_database


class TestDatabaseMigrations:
    """Tests for database schema migrations."""

    @pytest.mark.anyio
    async def test_migrate_v1_to_v2(self, tmp_path):
        """Migration from v1 to v2 schema works correctly."""
        db_path = tmp_path / "test_migration.sqlite"
        db_url = f"sqlite+aiosqlite:///{db_path}"

        engine = create_fxtr_engine(db_url)
        try:
            # Create v1 schema and test data
            async with engine.write_txn_engine.begin() as conn:
                await conn.execute(text("PRAGMA foreign_keys = ON"))
                await self._create_v1_schema(conn)
                await self._insert_v1_test_data(conn)
                await conn.execute(text("PRAGMA user_version = 1"))

            # Run migration
            await init_database(engine)

            # Verify
            async with engine.read_txn_engine.begin() as conn:
                # Check version
                result = await conn.execute(text("PRAGMA user_version"))
                assert result.fetchone()[0] == 2

                # Verify entity_dep data preserved
                result = await conn.execute(
                    text("SELECT requirer_id, required_id, kind FROM entity_dep")
                )
                assert result.fetchall() == [(1, 2, "WEAK_REF")]

                # Verify namespace_entity_backref has kind and data preserved
                result = await conn.execute(text("PRAGMA table_info(namespace_entity_backref)"))
                columns = {row[1] for row in result.fetchall()}
                assert "kind" in columns

                result = await conn.execute(
                    text(
                        "SELECT requirer_id, required_id, namespace_id, kind "
                        "FROM namespace_entity_backref"
                    )
                )
                assert result.fetchall() == [(1, 2, 1, "WEAK_REF")]
        finally:
            await engine.root_engine.dispose()

    @pytest.mark.anyio
    async def test_fresh_database_gets_current_version(self, tmp_db_url: str):
        """Fresh database is created with current version."""
        engine = create_fxtr_engine(tmp_db_url)
        try:
            await init_database(engine)
            async with engine.read_txn_engine.begin() as conn:
                result = await conn.execute(text("PRAGMA user_version"))
                assert result.fetchone()[0] == DB_USER_VERSION
        finally:
            await engine.root_engine.dispose()

    @pytest.mark.anyio
    async def test_future_version_raises_error(self, tmp_path):
        """Database from future version raises ValueError."""
        db_path = tmp_path / "future.sqlite"
        db_url = f"sqlite+aiosqlite:///{db_path}"

        engine = create_fxtr_engine(db_url)
        try:
            async with engine.write_txn_engine.begin() as conn:
                await conn.execute(text(f"PRAGMA user_version = {DB_USER_VERSION + 1}"))

            with pytest.raises(ValueError, match="newer than supported"):
                await init_database(engine)
        finally:
            await engine.root_engine.dispose()

    # -------------------------------------------------------------------------
    # Helper methods
    # -------------------------------------------------------------------------

    async def _create_v1_schema(self, conn) -> None:
        """Create v1 database schema."""
        await conn.execute(
            text(
                """
            CREATE TABLE session (
                id INTEGER PRIMARY KEY,
                public_id BLOB NOT NULL UNIQUE,
                created_at DATETIME NOT NULL,
                last_seen_at DATETIME NOT NULL,
                is_garbage BOOLEAN NOT NULL DEFAULT FALSE
            )
            """
            )
        )
        await conn.execute(
            text(
                """
            CREATE TABLE entity (
                id INTEGER PRIMARY KEY,
                public_id BLOB NOT NULL UNIQUE,
                storage_location VARCHAR(20) NOT NULL
            )
            """
            )
        )
        await conn.execute(
            text(
                """
            CREATE TABLE stored_entity_data (
                entity_id INTEGER PRIMARY KEY REFERENCES entity(id),
                data BLOB NOT NULL
            )
            """
            )
        )
        # V1: entity_dep unique constraint on (requirer_id, required_id) only
        await conn.execute(
            text(
                """
            CREATE TABLE entity_dep (
                requirer_id INTEGER NOT NULL REFERENCES entity(id),
                required_id INTEGER NOT NULL REFERENCES entity(id),
                kind VARCHAR(20) NOT NULL,
                UNIQUE(requirer_id, required_id)
            )
            """
            )
        )
        await conn.execute(text("CREATE INDEX ix_entity_dep_requirer ON entity_dep(requirer_id)"))
        await conn.execute(
            text(
                """
            CREATE TABLE namespace (
                id INTEGER PRIMARY KEY,
                public_id BLOB NOT NULL UNIQUE,
                name VARCHAR(255) NOT NULL UNIQUE
            )
            """
            )
        )
        await conn.execute(
            text(
                """
            CREATE TABLE namespace_entity_ownership (
                namespace_id INTEGER NOT NULL REFERENCES namespace(id),
                entity_id INTEGER NOT NULL REFERENCES entity(id),
                UNIQUE(namespace_id, entity_id)
            )
            """
            )
        )
        # V1: namespace_entity_backref has no kind column
        await conn.execute(
            text(
                """
            CREATE TABLE namespace_entity_backref (
                requirer_id INTEGER NOT NULL REFERENCES entity(id),
                required_id INTEGER NOT NULL REFERENCES entity(id),
                namespace_id INTEGER NOT NULL REFERENCES namespace(id),
                UNIQUE(requirer_id, required_id, namespace_id)
            )
            """
            )
        )
        await conn.execute(
            text(
                """
            CREATE INDEX ix_namespace_entity_backref_required_ns
            ON namespace_entity_backref(required_id, namespace_id)
            """
            )
        )
        await conn.execute(
            text(
                """
            CREATE TABLE namespace_pin (
                namespace_id INTEGER NOT NULL REFERENCES namespace(id),
                name VARCHAR(255) NOT NULL,
                entity_id INTEGER NOT NULL REFERENCES entity(id),
                UNIQUE(namespace_id, name)
            )
            """
            )
        )
        await conn.execute(
            text(
                """
            CREATE TABLE entity_reservation (
                session_id INTEGER NOT NULL REFERENCES session(id),
                entity_id INTEGER NOT NULL REFERENCES entity(id),
                UNIQUE(session_id, entity_id)
            )
            """
            )
        )
        await conn.execute(
            text(
                """
            CREATE TABLE arena_membership (
                arena_id INTEGER NOT NULL,
                entity_id INTEGER NOT NULL REFERENCES entity(id),
                UNIQUE(arena_id, entity_id)
            )
            """
            )
        )

    async def _insert_v1_test_data(self, conn) -> None:
        """Insert test data for v1 schema."""
        now = datetime.now(timezone.utc)

        # Garbage session
        await conn.execute(
            text(
                """
            INSERT INTO session (id, public_id, created_at, last_seen_at, is_garbage)
            VALUES (0, :pub_id, :now, :now, TRUE)
            """
            ),
            {"pub_id": bytes(16), "now": now.isoformat()},
        )

        # Namespace
        await conn.execute(
            text("INSERT INTO namespace (id, public_id, name) VALUES (1, :pub_id, 'test_ns')"),
            {"pub_id": uuid.uuid4().bytes},
        )

        # Two entities
        await conn.execute(
            text(
                "INSERT INTO entity (id, public_id, storage_location) "
                "VALUES (1, :pub1, 'HERE'), (2, :pub2, 'HERE')"
            ),
            {"pub1": b"entity1_public_id___", "pub2": b"entity2_public_id___"},
        )

        # Dependency and backref
        await conn.execute(
            text(
                "INSERT INTO entity_dep (requirer_id, required_id, kind) VALUES (1, 2, 'WEAK_REF')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO namespace_entity_ownership (namespace_id, entity_id) "
                "VALUES (1, 1), (1, 2)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO namespace_entity_backref (requirer_id, required_id, namespace_id) "
                "VALUES (1, 2, 1)"
            )
        )
