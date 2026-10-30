"""Database migrations for sqlite_store.

This module contains migration functions for upgrading the SQLite database schema
between versions. Migrations are run automatically during database initialization
if the database version is older than the current schema version.

Each migration function handles upgrading from one version to the next.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable

from fxtr2.data.sqlite_store.sqlite_ops import DB_USER_VERSION
from fxtr2.data.sqlite_store.sqlite_util import transaction

# Assert to catch version mismatches during development - update this when adding migrations
assert (
    DB_USER_VERSION == 6
), "When changing DB_USER_VERSION, update this assert and add a new migration function"

logger = logging.getLogger(__name__)


def migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """Migrate from schema version 2 to version 3.

    Changes:
    - Add ix_arena_session index on arena(session_id)
    - Add ix_arena_namespace index on arena(namespace_id)
    - Add ix_arena_membership_entity index on arena_membership(entity_id)
    - Add ix_namespace_pin_entity index on namespace_pin(entity_id, namespace_id)
    - Add ix_namespace_name_kind index on namespace(name, kind)

    These indices improve performance for garbage collection and orphan detection queries.
    """
    if logger.isEnabledFor(logging.INFO):
        logger.info("Running migration v2 -> v3")

    with transaction(conn, "IMMEDIATE"):
        conn.execute("CREATE INDEX IF NOT EXISTS ix_arena_session ON arena(session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_arena_namespace ON arena(namespace_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_arena_membership_entity ON arena_membership(entity_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_namespace_pin_entity "
            "ON namespace_pin(entity_id, namespace_id)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS ix_namespace_name_kind ON namespace(name, kind)")
        conn.execute("PRAGMA user_version = 3")

    if logger.isEnabledFor(logging.INFO):
        logger.info("Migration v2 -> v3 complete")


def migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """Migrate from schema version 3 to version 4.

    Version 4 uses LMDB for entity data storage instead of the stored_entity_data table.
    This migration cannot be performed automatically.

    Raises:
        ValueError: Always raised with instructions for manual migration.
    """
    raise ValueError(
        "Cannot automatically migrate from v3 to v4. The v4 schema uses LMDB for entity data.\n"
        "Migrate by loading the old database with a tool that still understands v3 storage,\n"
        "then copy entities into a new v4 database using high-level fxtr scope APIs."
    )


# Type alias for migration functions
MigrationFn = Callable[[sqlite3.Connection], None]


def migrate_v5_to_v6(conn: sqlite3.Connection) -> None:
    """Migrate from schema version 5 to version 6.

    Changes:
    - Add segment_metadata table for segment file lifecycle tracking
    - Add seg_id/seg_offset/seg_length columns to entity table
    - Drop entity_reservations table (unused legacy table)
    - Migrate entity data from LMDB to append-only segment files
    - Set user_version to 6

    After this migration, all entities have storage_location='data2/seg' and their
    data lives in segment files under {db_path}.data2.shards/. LMDB files are
    deleted after migration completes successfully.
    """
    import os
    from datetime import datetime, timezone

    from fxtr2.data.sqlite_store.entity_blob import encode_blob
    from fxtr2.data.sqlite_store.segment_io import append_record, flush_segment
    from fxtr2.data.sqlite_store.sqlite_ops import SQL_CREATE_SEGMENT_TABLES

    if logger.isEnabledFor(logging.INFO):
        logger.info("Running migration v5 -> v6")

    with transaction(conn, "IMMEDIATE"):
        # 1. Create segment_metadata table
        for stmt in SQL_CREATE_SEGMENT_TABLES:
            conn.execute(stmt)

        # 2. Add seg columns to existing entity table
        conn.execute("ALTER TABLE entity ADD COLUMN seg_id INTEGER")
        conn.execute("ALTER TABLE entity ADD COLUMN seg_offset INTEGER")
        conn.execute("ALTER TABLE entity ADD COLUMN seg_length INTEGER")

        # 3. Drop entity_reservations (legacy table, unused)
        conn.execute("DROP TABLE IF EXISTS entity_reservations")

        # 4. Resolve db_path from connection
        cursor = conn.execute("PRAGMA database_list")
        db_path = None
        for row in cursor.fetchall():
            if row[1] == "main":
                db_path = row[2]
                break
        if not db_path:
            raise ValueError("Could not determine database file path from PRAGMA database_list")

        # 5. Create segments directory and initial segment file
        segments_dir = f"{db_path}.data2.shards"
        os.makedirs(segments_dir, exist_ok=True)
        seg_path = os.path.join(segments_dir, "seg_00000000.bin")

        # 6. Insert segment_metadata row
        now = datetime.now(timezone.utc)
        conn.execute(
            "INSERT INTO segment_metadata "
            "(segment_id, status, write_cursor, created_at) "
            "VALUES (0, 'active', 0, ?)",
            (now.isoformat(),),
        )

        # 7. Check if there are any entities to migrate from LMDB
        cursor = conn.execute("SELECT COUNT(*) FROM entity WHERE storage_location = 'data1/1'")
        entity_count = cursor.fetchone()[0]

        if entity_count > 0:
            # Need to open LMDB and migrate data
            import lmdb
            from tqdm import tqdm

            from fxtr2.data.sqlite_store import lmdb_blob

            # Detect LMDB path (same logic as old _setup_database_schema)
            old_lmdb_dir = f"{db_path}.data1.lmdb"
            if os.path.exists(old_lmdb_dir):
                lmdb_path = old_lmdb_dir
                lmdb_subdir = True
            else:
                lmdb_path = f"{db_path}.data1.mdb"
                lmdb_subdir = False

            lmdb_env = lmdb.open(
                lmdb_path,
                subdir=lmdb_subdir,
                map_size=1024 * 1024 * 1024 * 100,
                readonly=True,
                max_dbs=1,
            )
            lmdb_data_db = lmdb_env.open_db(b"data1/1")

            try:
                # Open segment file for writing
                seg_fd = os.open(seg_path, os.O_CREAT | os.O_RDWR, 0o644)
                try:
                    # Track migrated offsets for same-segment precomp pointers.
                    # Since we iterate in id ASC order (deps have lower IDs than
                    # dependents), every dep is already in this dict when we need it.
                    migrated_offsets: dict[int, int] = {}

                    with lmdb_env.begin(db=lmdb_data_db) as lmdb_txn:
                        # Iterate all entities ordered by id ASC
                        cursor = conn.execute(
                            "SELECT id FROM entity "
                            "WHERE storage_location = 'data1/1' ORDER BY id ASC"
                        )
                        entity_ids = [row[0] for row in cursor.fetchall()]

                        for internal_id in tqdm(
                            entity_ids,
                            desc="Migrating entities to segment files",
                            unit="entity",
                            delay=0.5,
                        ):
                            # Read blob from LMDB
                            lmdb_key = internal_id.to_bytes(8, "little")
                            lmdb_data = lmdb_txn.get(lmdb_key)
                            if lmdb_data is None:
                                if logger.isEnabledFor(logging.WARNING):
                                    logger.warning(
                                        f"Entity {internal_id} has storage_location='data1/1' "
                                        "but no LMDB data found, skipping"
                                    )
                                continue

                            # Decode original blob sections
                            fxtr_id_bytes = lmdb_blob.decode_fxtr_id_bytes(lmdb_data)
                            cbor_bytes = lmdb_blob.decode_cbor_bytes(lmdb_data)
                            deps_list, precomp_map = lmdb_blob.decode_metadata(lmdb_data)

                            # Add 5th element (seg_offset) to precomp entries
                            new_precomp: dict[int, list] = {}
                            for iid, entry in precomp_map.items():
                                new_entry = list(entry)
                                if iid in migrated_offsets:
                                    # Dep already written to this segment
                                    new_entry.append(migrated_offsets[iid])
                                else:
                                    # Entity itself (not yet written) or unknown
                                    new_entry.append(None)
                                new_precomp[iid] = new_entry

                            # Re-encode blob with updated precomp
                            metadata = {
                                "deps": [list(dep) for dep in deps_list],
                                "precomp": new_precomp,
                            }
                            blob = encode_blob(fxtr_id_bytes, cbor_bytes, metadata)

                            # Append to segment file
                            offset, length = append_record(seg_fd, blob)

                            # Record offset for same-segment pointers
                            migrated_offsets[internal_id] = offset

                            # Update entity row
                            conn.execute(
                                "UPDATE entity SET storage_location = 'data2/seg', "
                                "seg_id = 0, seg_offset = ?, seg_length = ? WHERE id = ?",
                                (offset, length, internal_id),
                            )

                    # 9. Flush segment file
                    flush_segment(seg_fd)

                    # 10. Update write_cursor to final position
                    final_cursor = os.lseek(seg_fd, 0, os.SEEK_END)
                    conn.execute(
                        "UPDATE segment_metadata SET write_cursor = ? " "WHERE segment_id = 0",
                        (final_cursor,),
                    )
                finally:
                    os.close(seg_fd)
            finally:
                lmdb_env.close()
        else:
            # No entities to migrate — just create empty segment file
            seg_fd = os.open(seg_path, os.O_CREAT | os.O_RDWR, 0o644)
            os.close(seg_fd)

        # 12. Set version
        conn.execute("PRAGMA user_version = 6")

    # 13. Delete LMDB files now that migration is committed.
    # db_path was resolved inside the transaction above.
    if db_path:
        import shutil

        # .data1.lmdb is the subdir variant (directory with data.mdb + lock.mdb inside)
        lmdb_dir = f"{db_path}.data1.lmdb"
        if os.path.isdir(lmdb_dir):
            shutil.rmtree(lmdb_dir, ignore_errors=True)
            if logger.isEnabledFor(logging.INFO):
                logger.info(f"Deleted LMDB directory: {lmdb_dir}")

        # .data1.mdb is the NOSUBDIR variant (single file + lock file)
        lmdb_file = f"{db_path}.data1.mdb"
        if os.path.isfile(lmdb_file):
            os.remove(lmdb_file)
            if logger.isEnabledFor(logging.INFO):
                logger.info(f"Deleted LMDB file: {lmdb_file}")
        lock_file = lmdb_file + "-lock"
        if os.path.isfile(lock_file):
            os.remove(lock_file)

    if logger.isEnabledFor(logging.INFO):
        logger.info("Migration v5 -> v6 complete")


def migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
    """Migrate from schema version 4 to version 5.

    Changes:
    - Add ns_index_registry table for namespace indexing.
    """
    if logger.isEnabledFor(logging.INFO):
        logger.info("Running migration v4 -> v5")

    with transaction(conn, "IMMEDIATE"):
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ns_index_registry (
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
        conn.execute("PRAGMA user_version = 5")

    if logger.isEnabledFor(logging.INFO):
        logger.info("Migration v4 -> v5 complete")


# Registry of migrations: (from_version, to_version) -> migration function
_MIGRATIONS: dict[tuple[int, int], MigrationFn] = {
    (2, 3): migrate_v2_to_v3,
    (3, 4): migrate_v3_to_v4,
    (4, 5): migrate_v4_to_v5,
    (5, 6): migrate_v5_to_v6,
}


def run_migrations(conn: sqlite3.Connection, current_version: int, target_version: int) -> None:
    """Run all migrations needed to get from current_version to target_version.

    Args:
        conn: Database connection (caller manages the connection lifecycle)
        current_version: Current schema version (from PRAGMA user_version)
        target_version: Target schema version to migrate to

    Raises:
        ValueError: If there's no migration path from current to target version
    """
    if current_version >= target_version:
        return

    version = current_version
    while version < target_version:
        next_version = version + 1
        migration_key = (version, next_version)

        if migration_key not in _MIGRATIONS:
            raise ValueError(
                f"No migration path from version {version} to {next_version}. "
                f"Cannot upgrade database from version {current_version} to {target_version}."
            )

        migration_fn = _MIGRATIONS[migration_key]
        migration_fn(conn)
        version = next_version

    # Run PRAGMA optimize after migrations to update query statistics for new indices
    # See: https://www.sqlite.org/pragma.html#pragma_optimize
    conn.execute("PRAGMA optimize")

    if logger.isEnabledFor(logging.INFO):
        logger.info(
            f"Successfully migrated database from version {current_version} to {target_version}"
        )
