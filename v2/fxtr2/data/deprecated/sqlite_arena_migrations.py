"""Database migrations for sqlite_arena.

DEPRECATED: This module is deprecated along with sqlite_arena.py.

The new sqlite_store implementation uses the same schema, so these migrations
are still used but should not be modified. See fxtr2.data.sqlite_store for the
current implementation.

---

This module contains migration functions for upgrading the SQLite database schema
between versions. Migrations are run automatically during database initialization
if the database version is older than the current schema version.

Each migration function handles upgrading from one version to the next.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# Import the current table definitions to simplify migration. Note that if these change, we may
# need to copy in the previous table definitions here to enable migrating to older versions than the
# current one. Imports are named with a suffix based on the user version at which they were last
# modified.
from fxtr2.data.deprecated.sqlite_arena import DB_USER_VERSION
from fxtr2.data.deprecated.sqlite_arena import entity_dep_table as entity_dep_table__changed_at_v2
from fxtr2.data.deprecated.sqlite_arena import (
    namespace_entity_backref_table as namespace_entity_backref_table__changed_at_v2,
)

assert DB_USER_VERSION == 2, (
    "when changing the DB_USER_VERSION, check if you need to fix the above imports to still refer"
    " to the same original value"
)

logger = logging.getLogger(__name__)


async def migrate_v1_to_v2(conn: AsyncConnection) -> None:
    """Migrate from schema version 1 to version 2.

    Changes:
    - entity_dep: Change unique constraint from (requirer_id, required_id)
      to (requirer_id, required_id, kind) to allow multiple references to
      the same entity with different link types.
    - namespace_entity_backref: Add 'kind' column and update unique constraint
      to (requirer_id, required_id, namespace_id, kind).

    Uses SQLAlchemy table definitions for creating new tables to ensure
    schema consistency with the current codebase.
    """

    logger.info("Running migration v1 -> v2")

    # --- Migrate entity_dep table ---
    # 1. Drop old indexes (SQLite indexes are database-wide, survive table renames)
    await conn.execute(text("DROP INDEX IF EXISTS ix_entity_dep_requirer"))
    await conn.execute(text("DROP INDEX IF EXISTS uq_entity_dep_key"))

    # 2. Rename old table
    await conn.execute(text("ALTER TABLE entity_dep RENAME TO _entity_dep_v1"))

    # 3. Create new table using v2 schema definition (includes new indexes)
    await conn.run_sync(lambda sync_conn: entity_dep_table__changed_at_v2.create(sync_conn))

    # 3. Copy data from old table
    await conn.execute(
        text(
            """
        INSERT INTO entity_dep (requirer_id, required_id, kind)
        SELECT requirer_id, required_id, kind FROM _entity_dep_v1
        """
        )
    )

    # 4. Drop old table
    await conn.execute(text("DROP TABLE _entity_dep_v1"))

    # --- Migrate namespace_entity_backref table ---
    # 1. Drop old indexes (SQLite indexes are database-wide, survive table renames)
    await conn.execute(text("DROP INDEX IF EXISTS ix_namespace_entity_backref_required_ns"))
    await conn.execute(text("DROP INDEX IF EXISTS uq_namespace_entity_backref_key"))

    # 2. Rename old table
    await conn.execute(
        text("ALTER TABLE namespace_entity_backref RENAME TO _namespace_entity_backref_v1")
    )

    # 3. Create new table using v2 schema definition (includes new indexes)
    await conn.run_sync(
        lambda sync_conn: namespace_entity_backref_table__changed_at_v2.create(sync_conn)
    )

    # 3. Copy data from old table, joining with entity_dep to get the kind.
    #    In v1, entity_dep had unique (requirer_id, required_id), so there's
    #    exactly one kind per pair.
    await conn.execute(
        text(
            """
        INSERT INTO namespace_entity_backref (requirer_id, required_id, namespace_id, kind)
        SELECT neb.requirer_id, neb.required_id, neb.namespace_id, ed.kind
        FROM _namespace_entity_backref_v1 neb
        JOIN entity_dep ed ON neb.requirer_id = ed.requirer_id
                          AND neb.required_id = ed.required_id
        """
        )
    )

    # 4. Drop old table
    await conn.execute(text("DROP TABLE _namespace_entity_backref_v1"))

    # Update version
    await conn.execute(text("PRAGMA user_version = 2"))

    logger.info("Migration v1 -> v2 complete")


# Type alias for migration functions
MigrationFn = Callable[[AsyncConnection], Awaitable[None]]

# Registry of migrations: (from_version, to_version) -> migration function
_MIGRATIONS: dict[tuple[int, int], MigrationFn] = {
    (1, 2): migrate_v1_to_v2,
}


async def run_migrations(conn: AsyncConnection, current_version: int, target_version: int) -> None:
    """Run all migrations needed to get from current_version to target_version.

    Args:
        conn: Database connection (must be in a transaction)
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
        await migration_fn(conn)
        version = next_version

    logger.info(
        f"Successfully migrated database from version {current_version} to {target_version}"
    )
