"""SQLite database operations using raw SQL and sqlite3.

This module contains all the SQL strings and sync database operations
for the sqlite_store module. Operations take a sqlite3.Connection and
perform focused database operations using raw SQL.

Transaction Philosophy:
- Each function manages its own transaction using `with transaction(conn, txn_mode):`
- Keep transactions as small and granular as possible
- Python work (deciding what to enqueue, processing results) happens outside transactions
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sqlite3
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from fxtr2.data.abstract_arena import DepKind
    from fxtr2.data.entity_core import FxtrID

import anyio.to_thread

from fxtr2.data.sqlite_store import sqlite_index_ops
from fxtr2.data.sqlite_store.sqlite_util import (
    ConnectionWorkerPool,
    ConnectionWorkerPoolGroup,
    transaction,
)

logger = logging.getLogger(__name__)

# =============================================================================
# Constants
# =============================================================================

# Database user version for migrations
DB_USER_VERSION = 6

# Garbage session constants (internal id=0, public_id=zero UUID)
GARBAGE_SESSION_ID = 0
GARBAGE_SESSION_UUID = uuid.UUID(int=0)

# When an IN clause has more than this many values, use json_each() with a
# single JSON parameter instead of individual ? placeholders to avoid hitting
# SQLite's SQLITE_MAX_VARIABLE_NUMBER limit.
_JSON_EACH_THRESHOLD = 10

# =============================================================================
# SQL Schema Definitions
# =============================================================================

# Table creation SQL for DB version 6 (segment-file store)
SQL_CREATE_TABLES = [
    # Entity tables
    """CREATE TABLE IF NOT EXISTS entity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id BLOB UNIQUE,
    storage_location VARCHAR(10) NOT NULL,
    seg_id INTEGER,
    seg_offset INTEGER,
    seg_length INTEGER
)""",
    """CREATE TABLE IF NOT EXISTS entity_dep (
    requirer_id INTEGER NOT NULL REFERENCES entity(id),
    required_id INTEGER NOT NULL REFERENCES entity(id),
    kind VARCHAR(20) NOT NULL,
    UNIQUE (requirer_id, required_id, kind)
)""",
    """CREATE INDEX IF NOT EXISTS ix_entity_dep_requirer ON entity_dep(requirer_id)""",
    # Note: stored_entity_data table removed in v4 - data now stored in segment files
    # Namespace tables
    """CREATE TABLE IF NOT EXISTS namespace (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR NOT NULL,
    kind VARCHAR NOT NULL,
    public_id BLOB(16) NOT NULL UNIQUE
)""",
    """CREATE INDEX IF NOT EXISTS ix_namespace_name_kind ON namespace(name, kind)""",
    """CREATE TABLE IF NOT EXISTS namespace_entity_ownership (
    namespace_id INTEGER NOT NULL REFERENCES namespace(id),
    entity_id INTEGER NOT NULL REFERENCES entity(id),
    UNIQUE (namespace_id, entity_id)
)""",
    """CREATE TABLE IF NOT EXISTS namespace_entity_backref (
    requirer_id INTEGER NOT NULL REFERENCES entity(id),
    required_id INTEGER NOT NULL REFERENCES entity(id),
    namespace_id INTEGER NOT NULL REFERENCES namespace(id),
    kind VARCHAR(20) NOT NULL,
    UNIQUE (requirer_id, required_id, namespace_id, kind)
)""",
    """CREATE INDEX IF NOT EXISTS ix_namespace_entity_backref_required_ns
    ON namespace_entity_backref(required_id, namespace_id)""",
    """CREATE TABLE IF NOT EXISTS namespace_pin (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace_id INTEGER NOT NULL REFERENCES namespace(id),
    entity_id INTEGER NOT NULL REFERENCES entity(id),
    name VARCHAR NOT NULL,
    UNIQUE (namespace_id, name)
)""",
    """CREATE INDEX IF NOT EXISTS ix_namespace_pin_entity
    ON namespace_pin(entity_id, namespace_id)""",
    # Session tables
    """CREATE TABLE IF NOT EXISTS session (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id BLOB(16) NOT NULL UNIQUE,
    created_at DATETIME NOT NULL,
    last_seen_at DATETIME NOT NULL,
    is_garbage BOOLEAN NOT NULL DEFAULT 0,
    is_collecting_garbage BOOLEAN NOT NULL DEFAULT 0
)""",
    """CREATE TABLE IF NOT EXISTS arena (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id BLOB(16) NOT NULL UNIQUE,
    namespace_id INTEGER NOT NULL REFERENCES namespace(id),
    session_id INTEGER NOT NULL REFERENCES session(id)
)""",
    """CREATE INDEX IF NOT EXISTS ix_arena_session ON arena(session_id)""",
    """CREATE INDEX IF NOT EXISTS ix_arena_namespace ON arena(namespace_id)""",
    """CREATE TABLE IF NOT EXISTS arena_membership (
    arena_id INTEGER NOT NULL REFERENCES arena(id),
    entity_id INTEGER NOT NULL REFERENCES entity(id),
    UNIQUE (arena_id, entity_id)
)""",
    """CREATE INDEX IF NOT EXISTS ix_arena_membership_entity ON arena_membership(entity_id)""",
    # Index registry table (namespace indexing)
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
)""",
]

# Additional tables for segment-file storage (v6).
# For fresh databases, the entity table already includes seg_id/seg_offset/seg_length
# columns (defined inline in SQL_CREATE_TABLES). The ALTER TABLE forms here are used
# only by the v5→v6 migration.
SQL_CREATE_SEGMENT_TABLES = [
    """CREATE TABLE IF NOT EXISTS segment_metadata (
    segment_id INTEGER PRIMARY KEY,
    status VARCHAR NOT NULL DEFAULT 'active',
    write_cursor INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL
)""",
]


# =============================================================================
# Database Setup
# =============================================================================


def _connection_setup(conn: sqlite3.Connection) -> None:
    """Per-connection setup callback for connection pools.

    This is called for each new connection created by the pool.
    """
    # Enable foreign key constraints
    conn.execute("PRAGMA foreign_keys = ON")
    # Note: sqlite3 doesn't support disabling double-quoted strings via config
    # Wait up to 5 seconds for a write lock before returning SQLITE_BUSY.
    # The previous 500ms value caused intermittent failures when multiple
    # processes (e.g. subprocess-based CLI calls in tests) competed for
    # the same database file. 5 seconds is conservative but safe — in
    # production the single-process server rarely contends, and in tests
    # the extra headroom avoids flaky failures.
    conn.execute("PRAGMA busy_timeout = 5000")


def _connection_teardown(conn: sqlite3.Connection) -> None:
    """Per-connection teardown callback for connection pools.

    This is called just before each connection is closed. Runs PRAGMA optimize
    to update query statistics if needed.

    From SQLite docs: "Applications should be prepared to run PRAGMA optimize
    once per database connection just prior to closing the connection."
    See: https://www.sqlite.org/pragma.html#pragma_optimize
    """
    try:
        conn.execute("PRAGMA optimize")
    except sqlite3.OperationalError:
        logger.debug("[conn %x] PRAGMA optimize skipped (database locked)", id(conn))


def _setup_database_schema(db_path: str) -> tuple[Any, sqlite3.Connection]:
    """Initialize database schema, create segment infrastructure, and return setup state.

    Args:
        db_path: Path to the SQLite database file (must be a file path, not URI).

    Returns:
        Tuple of (segment_manager, setup_conn). The caller is responsible
        for closing these resources.

    Raises:
        ValueError: If the database version is newer than the code supports, or if
            the db_path is not a valid file path.
    """
    from fxtr2.data.sqlite_store.segment_io import SegmentManager, truncate_to_cursor

    # Validate that db_path is a plain file path (not a URI or :memory:)
    if db_path == ":memory:" or db_path.startswith("file:"):
        raise ValueError(
            f"Database path '{db_path}' must be a plain file path, not a URI. "
            "In-memory databases are not supported (segment files require a filesystem path)."
        )

    segments_dir = f"{db_path}.data2.shards"

    # Open SQLite connection
    setup_conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    _connection_setup(setup_conn)

    try:
        # Enable WAL mode
        setup_conn.execute("PRAGMA journal_mode = WAL")

        # Check database version
        cursor = setup_conn.execute("PRAGMA user_version")
        row = cursor.fetchone()
        current_version = row[0] if row else 0

        if current_version > DB_USER_VERSION:
            raise ValueError(
                f"Database version {current_version} is newer than supported version "
                f"{DB_USER_VERSION}. Please upgrade your code."
            )
        elif current_version == 0:
            # Fresh database: create schema with segment support
            with transaction(setup_conn, "IMMEDIATE"):
                for statement in SQL_CREATE_TABLES:
                    setup_conn.execute(statement)
                for statement in SQL_CREATE_SEGMENT_TABLES:
                    setup_conn.execute(statement)

                # Create segments directory and initial segment file
                os.makedirs(segments_dir, exist_ok=True)
                seg_path = os.path.join(segments_dir, "seg_00000000.bin")
                seg_fd = os.open(seg_path, os.O_CREAT | os.O_RDWR, 0o644)
                os.close(seg_fd)

                # Insert initial segment_metadata row
                now = datetime.now(timezone.utc)
                setup_conn.execute(
                    "INSERT INTO segment_metadata "
                    "(segment_id, status, write_cursor, created_at) "
                    "VALUES (0, 'active', 0, ?)",
                    (now.isoformat(),),
                )

                setup_conn.execute(f"PRAGMA user_version = {DB_USER_VERSION}")
        elif current_version < DB_USER_VERSION:
            # Run migrations
            from fxtr2.data.sqlite_store.sqlite_migrations import run_migrations

            run_migrations(setup_conn, current_version, DB_USER_VERSION)

        # Create garbage session
        with transaction(setup_conn, "IMMEDIATE"):
            cursor = setup_conn.execute(
                "SELECT id FROM session WHERE id = ? AND public_id = ?",
                (GARBAGE_SESSION_ID, GARBAGE_SESSION_UUID.bytes),
            )
            if cursor.fetchone() is None:
                now = datetime.now(timezone.utc)
                setup_conn.execute(
                    """
                    INSERT OR IGNORE INTO session
                        (id, public_id, created_at, last_seen_at, is_garbage, is_collecting_garbage)
                    VALUES (?, ?, ?, ?, 1, 0)
                    """,
                    (
                        GARBAGE_SESSION_ID,
                        GARBAGE_SESSION_UUID.bytes,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )

        # Crash recovery: truncate active segment to committed write_cursor
        cursor = setup_conn.execute(
            "SELECT segment_id, write_cursor FROM segment_metadata WHERE status = 'active'"
        )
        active_row = cursor.fetchone()
        if active_row is not None:
            active_seg_id, write_cursor = active_row
            seg_path = os.path.join(segments_dir, f"seg_{active_seg_id:08d}.bin")
            if os.path.exists(seg_path):
                truncate_to_cursor(seg_path, write_cursor)

        # Create SegmentManager for reading segment files
        segment_manager = SegmentManager(segments_dir)

    except BaseException:
        setup_conn.close()
        raise

    return segment_manager, setup_conn


def setup_database_and_create_worker_pool_group(
    db_path: str,
    write_pool_size: int = 1,
    read_pool_size: int = 5,
) -> ConnectionWorkerPoolGroup:
    """Initialize database schema, create segment infrastructure, and return a pool group.

    Creates ConnectionWorkerPool instances (persistent worker threads) for
    read and write operations. Worker threads must be started by the caller
    after creation (e.g. via pool.start_all_workers() in a task group).

    CRITICAL: Only call this function ONCE per database path, or use GLOBAL_POOL_GROUP_MANAGER
    (in sqlite_namespace.py) to ensure singleton pool groups.

    Args:
        db_path: Path to the SQLite database file (must be a plain file path, not URI).
        write_pool_size: Max connections for the write pool (default 1).
        read_pool_size: Max connections for the read pool (default 5).

    Returns:
        A ConnectionWorkerPoolGroup with separate read and write worker pools
        and a SegmentManager for entity data.
    """
    # Initialize schema and segment infrastructure
    segment_manager, setup_conn = _setup_database_schema(db_path)
    setup_conn.close()

    segments_dir = f"{db_path}.data2.shards"

    # Create worker pools
    write_pool = ConnectionWorkerPool(
        db_path=db_path,
        max_connections=write_pool_size,
        connection_setup=_connection_setup,
        connection_teardown=_connection_teardown,
    )
    read_pool = ConnectionWorkerPool(
        db_path=db_path,
        max_connections=read_pool_size,
        connection_setup=_connection_setup,
        connection_teardown=_connection_teardown,
    )

    return ConnectionWorkerPoolGroup(
        write_pool=write_pool,
        read_pool=read_pool,
        segment_manager=segment_manager,
        segments_dir=segments_dir,
    )


async def async_setup_database_and_create_worker_pool_group(
    db_path: str,
) -> ConnectionWorkerPoolGroup:
    """Async wrapper for setup_database_and_create_worker_pool_group."""
    return await anyio.to_thread.run_sync(
        setup_database_and_create_worker_pool_group,
        db_path,
    )


def setup_readonly_pool_group(
    db_path: str,
    read_pool_size: int = 5,
) -> ConnectionWorkerPoolGroup:
    """Open a database in read-only mode and return a pool group with no write pool.

    This validates that the database exists and has the expected schema version,
    but performs NO writes: no migrations, no garbage session creation, no crash
    recovery truncation.

    Connections are opened with SQLite ``mode=ro`` so that any accidental write
    attempt will be rejected by the database engine itself.

    Args:
        db_path: Path to the SQLite database file (must already exist).
        read_pool_size: Max connections for the read pool (default 5).

    Returns:
        A ConnectionWorkerPoolGroup with ``write_pool=None``.

    Raises:
        FileNotFoundError: If the database file does not exist.
        ValueError: If the database schema version does not match.
    """
    from fxtr2.data.sqlite_store.segment_io import SegmentManager

    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database file not found: {db_path}")

    # Open a temporary connection to validate the schema version
    uri = f"file:{db_path}?mode=ro"
    check_conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False)
    try:
        cursor = check_conn.execute("PRAGMA user_version")
        current_version = (cursor.fetchone() or (0,))[0]
        if current_version != DB_USER_VERSION:
            raise ValueError(
                f"Database version {current_version} does not match expected version "
                f"{DB_USER_VERSION}. Cannot open in read-only mode "
                f"(migrations require read-write access)."
            )
    finally:
        check_conn.close()

    segments_dir = f"{db_path}.data2.shards"
    segment_manager = SegmentManager(segments_dir)

    read_pool = ConnectionWorkerPool(
        db_path=uri,
        max_connections=read_pool_size,
        connection_setup=_connection_setup,
        # Skip PRAGMA optimize on teardown — it may attempt to write
        connection_teardown=None,
        uri=True,
    )

    return ConnectionWorkerPoolGroup(
        write_pool=None,
        read_pool=read_pool,
        segment_manager=segment_manager,
        segments_dir=segments_dir,
    )


async def async_setup_readonly_pool_group(
    db_path: str,
) -> ConnectionWorkerPoolGroup:
    """Async wrapper for setup_readonly_pool_group."""
    return await anyio.to_thread.run_sync(
        setup_readonly_pool_group,
        db_path,
    )


# =============================================================================
# Session Operations
# =============================================================================


def insert_session(conn: sqlite3.Connection, public_id: bytes, now: datetime) -> int:
    """Insert a new session and return its internal ID.

    Args:
        conn: sqlite3 connection.
        public_id: The session's public UUID as bytes.
        now: The current timestamp.

    Returns:
        The internal session ID.
    """
    with transaction(conn, "IMMEDIATE"):
        cursor = conn.execute(
            """
            INSERT INTO session (public_id, created_at, last_seen_at, is_garbage, is_collecting_garbage)
            VALUES (?, ?, ?, 0, 0)
            """,
            (public_id, now.isoformat(), now.isoformat()),
        )
        internal_id = cursor.lastrowid
        assert internal_id is not None
    return internal_id


def mark_session_garbage(conn: sqlite3.Connection, session_id: int, now: datetime) -> None:
    """Mark a session as garbage.

    Also marks any BUILDING index registrations owned by this session as
    GARBAGE so they will be cleaned up by the GC worker.

    Args:
        conn: sqlite3 connection.
        session_id: The internal session ID.
        now: The current timestamp.
    """
    with transaction(conn, "IMMEDIATE"):
        conn.execute(
            "UPDATE session SET is_garbage = 1, last_seen_at = ? WHERE id = ?",
            (now.isoformat(), session_id),
        )
        sqlite_index_ops._mark_building_registrations_garbage_for_session(conn, session_id)


def mark_session_garbage_by_public_id(conn: sqlite3.Connection, public_id: bytes) -> int:
    """Mark a session as garbage by its public ID.

    Args:
        conn: sqlite3 connection.
        public_id: The session's public UUID as bytes.

    Returns:
        The number of rows affected (0 if session not found, 1 if marked).
    """
    with transaction(conn, "IMMEDIATE"):
        cursor = conn.execute("UPDATE session SET is_garbage = 1 WHERE public_id = ?", (public_id,))
        return cursor.rowcount


def set_session_collecting_garbage(
    conn: sqlite3.Connection, session_id: int, is_collecting: bool
) -> None:
    """Set whether a session is collecting garbage.

    Args:
        conn: sqlite3 connection.
        session_id: The internal session ID.
        is_collecting: True if the session is collecting garbage, False otherwise.
    """
    with transaction(conn, "IMMEDIATE"):
        conn.execute(
            "UPDATE session SET is_collecting_garbage = ? WHERE id = ?",
            (1 if is_collecting else 0, session_id),
        )


def find_session_collecting_garbage(conn: sqlite3.Connection) -> int | None:
    """Find any session that is currently collecting garbage.

    Args:
        conn: sqlite3 connection.

    Returns:
        The session ID if one is collecting garbage, None otherwise.
    """
    with transaction(conn, "DEFERRED"):
        cursor = conn.execute("SELECT id FROM session WHERE is_collecting_garbage = 1 LIMIT 1")
        row = cursor.fetchone()
        return row[0] if row else None


def find_and_delete_orphan_garbage_session(conn: sqlite3.Connection) -> int | None:
    """Find and delete an orphan garbage session (not session 0) that has no arenas.

    This combines the find and delete in a single transaction to match the
    original transaction structure.

    Args:
        conn: sqlite3 connection.

    Returns:
        The deleted session ID if one was found and deleted, None otherwise.
    """
    with transaction(conn, "IMMEDIATE"):
        cursor = conn.execute(
            """
            SELECT id FROM session
            WHERE is_garbage = 1 AND id != ?
            AND NOT EXISTS (SELECT 1 FROM arena WHERE arena.session_id = session.id)
            LIMIT 1
            """,
            (GARBAGE_SESSION_ID,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        session_id = row[0]
        conn.execute("DELETE FROM session WHERE id = ?", (session_id,))
        return session_id


# =============================================================================
# Namespace Operations
# =============================================================================


def get_or_create_user_namespace(conn: sqlite3.Connection, name: str) -> tuple[int, uuid.UUID]:
    """Get or create a user namespace.

    Args:
        conn: sqlite3 connection.
        name: The namespace name.

    Returns:
        Tuple of (internal_id, public_uuid).
    """
    with transaction(conn, "IMMEDIATE"):
        cursor = conn.execute(
            "SELECT id, public_id FROM namespace WHERE name = ? AND kind = 'user'",
            (name,),
        )
        row = cursor.fetchone()

        if row is not None:
            return row[0], uuid.UUID(bytes=row[1])

        # Namespace doesn't exist, create it
        namespace_uuid = uuid.uuid4()
        cursor = conn.execute(
            "INSERT INTO namespace (name, kind, public_id) VALUES (?, 'user', ?)",
            (name, namespace_uuid.bytes),
        )
        internal_id = cursor.lastrowid
        assert internal_id is not None
        return internal_id, namespace_uuid


def get_user_namespace(conn: sqlite3.Connection, name: str) -> tuple[int, uuid.UUID]:
    """Look up a user namespace by name (read-only, no creation).

    Args:
        conn: sqlite3 connection.
        name: The namespace name.

    Returns:
        Tuple of (internal_id, public_uuid).

    Raises:
        KeyError: If the namespace does not exist.
    """
    cursor = conn.execute(
        "SELECT id, public_id FROM namespace WHERE name = ? AND kind = 'user'",
        (name,),
    )
    row = cursor.fetchone()
    if row is None:
        raise KeyError(f"Namespace '{name}' not found")
    return row[0], uuid.UUID(bytes=bytes(row[1]))


# =============================================================================
# Pin Operations
# =============================================================================


def list_pins_with_prefix(
    conn: sqlite3.Connection, namespace_id: int, prefix: str
) -> list[tuple[str, bytes]]:
    """List pins in a namespace matching a prefix.

    Args:
        conn: sqlite3 connection.
        namespace_id: The namespace internal ID.
        prefix: Prefix to filter by (empty string for all pins).

    Returns:
        List of (name, entity_public_id) tuples.
    """
    with transaction(conn, "DEFERRED"):
        if prefix:
            cursor = conn.execute(
                """
                SELECT np.name, e.public_id
                FROM namespace_pin np
                JOIN entity e ON np.entity_id = e.id
                WHERE np.namespace_id = ? AND np.name LIKE ? || '%' ESCAPE '\\'
                """,
                (
                    namespace_id,
                    prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"),
                ),
            )
        else:
            cursor = conn.execute(
                """
                SELECT np.name, e.public_id
                FROM namespace_pin np
                JOIN entity e ON np.entity_id = e.id
                WHERE np.namespace_id = ?
                """,
                (namespace_id,),
            )
        return cast(list[tuple[str, bytes]], cursor.fetchall())


def check_entities_exist(
    conn: sqlite3.Connection,
    namespace_id: int,
    public_ids: list[bytes],
) -> set[bytes]:
    """Check which entity public IDs exist in a namespace.

    Args:
        conn: sqlite3 connection.
        namespace_id: The namespace internal ID.
        public_ids: Entity public IDs to check.

    Returns:
        Set of public IDs that exist in the namespace.
    """
    if not public_ids:
        return set()

    found: set[bytes] = set()
    with transaction(conn, "DEFERRED"):
        chunk_size = 500
        for i in range(0, len(public_ids), chunk_size):
            chunk = public_ids[i : i + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            cursor = conn.execute(
                f"""
                SELECT e.public_id
                FROM entity e
                JOIN namespace_entity_ownership neo ON e.id = neo.entity_id
                WHERE neo.namespace_id = ? AND e.public_id IN ({placeholders})
                """,
                [namespace_id, *chunk],
            )
            found.update(bytes(row[0]) for row in cursor)

    return found


def get_transitive_closure(
    conn: sqlite3.Connection,
    namespace_id: int,
    root_public_ids: list[bytes],
) -> list[tuple[bytes, int]]:
    """Walk entity_dep to find the transitive closure of root entities.

    Returns (public_id, seg_length) pairs in topological order (deps before
    dependents). Only includes entities owned by the given namespace.

    Uses a recursive CTE to find reachable internal IDs, then fetches dep
    edges within the closure for a topological sort in Python.
    """
    if not root_public_ids:
        return []

    with transaction(conn, "DEFERRED"):
        # Resolve root public IDs to internal IDs
        placeholders = ",".join("?" for _ in root_public_ids)
        cursor = conn.execute(
            f"""
            SELECT e.id
            FROM entity e
            JOIN namespace_entity_ownership neo ON e.id = neo.entity_id
            WHERE neo.namespace_id = ? AND e.public_id IN ({placeholders})
            """,
            [namespace_id, *root_public_ids],
        )
        root_internal_ids = [row[0] for row in cursor]

        if not root_internal_ids:
            return []

        # Recursive CTE to find all transitively-reachable entities
        seed_placeholders = ",".join("?" for _ in root_internal_ids)
        conn.execute(
            f"""
            CREATE TEMP TABLE _closure_ids AS
            WITH RECURSIVE reachable(entity_id) AS (
                SELECT id FROM entity WHERE id IN ({seed_placeholders})
                UNION
                SELECT ed.required_id
                FROM entity_dep ed
                JOIN reachable ON ed.requirer_id = reachable.entity_id
            )
            SELECT entity_id FROM reachable
            WHERE EXISTS (
                SELECT 1 FROM namespace_entity_ownership
                WHERE namespace_id = ? AND entity_id = reachable.entity_id
            )
            """,
            [*root_internal_ids, namespace_id],
        )

        # Fetch entity info: internal_id, public_id, seg_length
        cursor = conn.execute(
            """
            SELECT e.id, e.public_id, COALESCE(e.seg_length, 0)
            FROM _closure_ids c
            JOIN entity e ON c.entity_id = e.id
            """
        )
        entities: dict[int, tuple[bytes, int]] = {}
        for iid, pub_id, seg_len in cursor:
            entities[iid] = (bytes(pub_id), seg_len)

        # Fetch dep edges within the closure for topological sorting
        cursor = conn.execute(
            """
            SELECT ed.requirer_id, ed.required_id
            FROM entity_dep ed
            WHERE ed.requirer_id IN (SELECT entity_id FROM _closure_ids)
              AND ed.required_id IN (SELECT entity_id FROM _closure_ids)
            """
        )
        # Forward adjacency: required_id -> list of requirer_ids that depend on it
        dependents: dict[int, list[int]] = {iid: [] for iid in entities}
        in_degree: dict[int, int] = {iid: 0 for iid in entities}
        for requirer, required in cursor:
            if requirer in entities and required in entities:
                dependents[required].append(requirer)
                in_degree[requirer] += 1

        conn.execute("DROP TABLE _closure_ids")

    # Kahn's algorithm for topological sort (deps before dependents)
    queue = deque(iid for iid, deg in in_degree.items() if deg == 0)
    result: list[tuple[bytes, int]] = []
    visited: set[int] = set()

    while queue:
        node = queue.popleft()
        visited.add(node)
        result.append(entities[node])
        for dep in dependents[node]:
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                queue.append(dep)

    # Include any remaining nodes (cycles or disconnected) at the end
    for iid in entities:
        if iid not in visited:
            result.append(entities[iid])

    return result


def unpin_and_maybe_rebind(conn: sqlite3.Connection, namespace_id: int, name: str) -> bool:
    """Remove a pin, clean up index data, and rebind the entity to garbage if orphaned.

    Args:
        conn: sqlite3 connection.
        namespace_id: The namespace internal ID.
        name: The pin name.

    Returns:
        True if garbage was created (entity was rebound to garbage arena),
        False otherwise.

    Raises:
        KeyError: If no pin with that name exists.
    """
    with transaction(conn, "IMMEDIATE"):
        # Look up the pin to get the entity ID
        cursor = conn.execute(
            "SELECT entity_id FROM namespace_pin WHERE namespace_id = ? AND name = ?",
            (namespace_id, name),
        )
        row = cursor.fetchone()
        if row is None:
            raise KeyError(f"No pin named {name!r} in namespace")
        entity_id: int = row[0]

        # Remove the pin first, then clean up index entries only if the entity
        # is no longer pinned under any key covered by an INDEXED registration.
        conn.execute(
            "DELETE FROM namespace_pin WHERE namespace_id = ? AND name = ?",
            (namespace_id, name),
        )

        sqlite_index_ops._delete_index_entries_for_entity_if_unneeded(
            conn,
            namespace_id,
            entity_id,
        )

        # If entity is now orphaned, move it to a garbage arena
        return _rebind_to_garbage_if_needed(conn, namespace_id, entity_id)


def create_or_update_pin(
    conn: sqlite3.Connection,
    namespace_id: int,
    entity_public_id: bytes,
    name: str,
    overwrite: bool,
    index_entries: dict[int, list[tuple]] | None = None,
) -> tuple[int, bool]:
    """Look up an entity in namespace and create or update a pin for it.

    Always verifies that every current INDEXED registration's indexer is
    covered by *index_entries* keys (CAS check).  When *index_entries* is
    provided, also writes the computed index rows and processed markers, and
    cleans up old entity index data on overwrite.

    If the CAS check fails, ``sqlite_index_ops.IndexMismatchError`` is raised
    and the transaction is rolled back so the caller can retry.

    Args:
        conn: sqlite3 connection.
        namespace_id: The namespace internal ID.
        entity_public_id: The entity's public ID bytes to pin.
        name: The pin name.
        overwrite: If True, overwrite existing pin. If False, raise on conflict.
        index_entries: ``{indexer_entity_iid: [(key_val_0, ..., value), ...]}``
            for each INDEXED registration covering this key.  ``None`` means
            no index work and asserts no INDEXED registrations exist.

    Returns:
        Tuple of (entity_internal_id, garbage_created):
        - entity_internal_id: The pinned entity's internal ID.
        - garbage_created: True if garbage was created (old entity was rebound).

    Raises:
        RuntimeError: If entity not found in namespace.
        KeyError: If pin exists and overwrite=False.
        sqlite_index_ops.IndexMismatchError: If INDEXED registrations changed.
    """
    with transaction(conn, "IMMEDIATE"):
        # Get the ground-truth set of INDEXED registrations for this key.
        # If any indexer is not covered by index_entries, we must retry.
        covered_iids = frozenset(index_entries.keys()) if index_entries else frozenset()
        live_regs = sqlite_index_ops._get_indexed_registrations_for_key(
            conn,
            namespace_id,
            name,
        )
        live_indexer_iids = frozenset(r[1] for r in live_regs)
        if not live_indexer_iids.issubset(covered_iids):
            raise sqlite_index_ops.IndexMismatchError(live_regs)

        # Look up entity by public_id and verify it's in the namespace
        cursor = conn.execute(
            """
            SELECT e.id
            FROM entity e
            JOIN namespace_entity_ownership neo ON e.id = neo.entity_id
            WHERE e.public_id = ? AND neo.namespace_id = ?
            """,
            (entity_public_id, namespace_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("Entity not found in namespace")
        entity_id: int = row[0]

        # Check if pin already exists
        cursor = conn.execute(
            "SELECT id, entity_id FROM namespace_pin WHERE namespace_id = ? AND name = ?",
            (namespace_id, name),
        )
        existing_pin = cursor.fetchone()

        old_entity_id: int | None = None
        if existing_pin is not None:
            if not overwrite:
                raise KeyError(f"Pin {name!r} already exists in namespace")

            # Update existing pin
            pin_id, old_entity_id = existing_pin
            conn.execute(
                "UPDATE namespace_pin SET entity_id = ? WHERE id = ?",
                (entity_id, pin_id),
            )

            # Clean up old entity's index entries if it's no longer pinned
            # under any key covered by an INDEXED registration
            if old_entity_id != entity_id:
                assert old_entity_id is not None
                sqlite_index_ops._delete_index_entries_for_entity_if_unneeded(
                    conn,
                    namespace_id,
                    old_entity_id,
                )
        else:
            # Create new pin
            conn.execute(
                "INSERT INTO namespace_pin (namespace_id, entity_id, name) VALUES (?, ?, ?)",
                (namespace_id, entity_id, name),
            )

        # Write index entries only for indexers that are actually INDEXED right now
        for indexer_iid in live_indexer_iids:
            rows = index_entries.get(indexer_iid, []) if index_entries else []
            if rows:
                sqlite_index_ops._write_index_rows(conn, namespace_id, indexer_iid, entity_id, rows)
            sqlite_index_ops._write_processed_marker(conn, namespace_id, indexer_iid, entity_id)

        # Handle garbage for overwritten entity
        garbage_created = False
        if old_entity_id is not None and old_entity_id != entity_id:
            garbage_created = _rebind_to_garbage_if_needed(conn, namespace_id, old_entity_id)

        return entity_id, garbage_created


# =============================================================================
# Arena Operations
# =============================================================================


def reassign_arena_to_garbage_session(conn: sqlite3.Connection, arena_id: int) -> None:
    """Reassign an arena to the garbage session (session 0).

    Args:
        conn: sqlite3 connection.
        arena_id: The arena internal ID.
    """
    with transaction(conn, "IMMEDIATE"):
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"reassign_arena_to_garbage_session executing update")
        conn.execute(
            "UPDATE arena SET session_id = ? WHERE id = ?",
            (GARBAGE_SESSION_ID, arena_id),
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"reassign_arena_to_garbage_session finished update")


# =============================================================================
# Private Helpers (called within transactions, do not start their own)
# =============================================================================


def _is_entity_orphaned_in_namespace(
    conn: sqlite3.Connection, namespace_id: int, entity_id: int
) -> bool:
    """Check if an entity is orphaned in a namespace.

    An entity is orphaned if it has no reason to be in the namespace:
    - Not required by anything else in the namespace (no backrefs)
    - Not pinned
    - Not in any arena associated with the namespace
    - Not used as an indexer in any non-GARBAGE index registration

    MUST be called within an existing transaction.

    Args:
        conn: sqlite3 connection (must be in a transaction).
        namespace_id: The namespace internal ID.
        entity_id: The entity internal ID.

    Returns:
        True if the entity is orphaned, False otherwise.
    """
    cursor = conn.execute(
        """
        SELECT EXISTS(
            SELECT 1 FROM namespace_entity_backref
            WHERE required_id = ? AND namespace_id = ?
        ) OR EXISTS(
            SELECT 1 FROM namespace_pin
            WHERE entity_id = ? AND namespace_id = ?
        ) OR EXISTS(
            SELECT 1 FROM arena_membership am
            JOIN arena a ON am.arena_id = a.id
            WHERE am.entity_id = ? AND a.namespace_id = ?
        ) OR EXISTS(
            SELECT 1 FROM ns_index_registry
            WHERE indexer_entity_id = ? AND namespace_id = ? AND state != 'GARBAGE'
        )
        """,
        (
            entity_id,
            namespace_id,
            entity_id,
            namespace_id,
            entity_id,
            namespace_id,
            entity_id,
            namespace_id,
        ),
    )
    row = cursor.fetchone()
    # If any of the conditions is true, entity is NOT orphaned
    assert row is not None
    return not bool(row[0])


def _rebind_to_garbage_if_needed(
    conn: sqlite3.Connection, namespace_id: int, entity_id: int
) -> bool:
    """Rebind an orphaned entity to a garbage arena if needed.

    Ensures invariants hold by tracking this entity in a garbage arena if it
    is unowned. The database maintains the invariant that every entity in a
    namespace is there because it is required by something else, pinned, or
    tracked in an arena.

    MUST be called within an existing transaction.

    Args:
        conn: sqlite3 connection (must be in a transaction).
        namespace_id: The namespace internal ID.
        entity_id: The entity internal ID.

    Returns:
        True if garbage was created (entity was added to a garbage arena),
        False if entity was not orphaned.
    """
    if not _is_entity_orphaned_in_namespace(conn, namespace_id, entity_id):
        return False

    # Entity is orphaned - find or create a garbage arena in this namespace
    cursor = conn.execute(
        "SELECT id FROM arena WHERE namespace_id = ? AND session_id = ? LIMIT 1",
        (namespace_id, GARBAGE_SESSION_ID),
    )
    row = cursor.fetchone()

    if row is not None:
        garbage_arena_id = row[0]
    else:
        # Create a new garbage arena for this namespace
        cursor = conn.execute(
            "INSERT INTO arena (public_id, namespace_id, session_id) VALUES (?, ?, ?)",
            (uuid.uuid4().bytes, namespace_id, GARBAGE_SESSION_ID),
        )
        garbage_arena_id = cursor.lastrowid
        assert garbage_arena_id is not None
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Created garbage arena %s for namespace %s",
                garbage_arena_id,
                namespace_id,
            )

    # Add entity to the garbage arena
    conn.execute(
        "INSERT INTO arena_membership (arena_id, entity_id) VALUES (?, ?)",
        (garbage_arena_id, entity_id),
    )
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "Rebound orphaned entity %s to garbage arena %s in namespace %s",
            entity_id,
            garbage_arena_id,
            namespace_id,
        )
    return True


def _create_arena_with_entities(
    conn: sqlite3.Connection,
    namespace_id: int,
    session_id: int,
    entity_ids: list[int],
    arena_public_id: bytes,
) -> int:
    """Create a new arena and add entities to it.

    MUST be called within an existing transaction.

    Args:
        conn: sqlite3 connection (must be in a transaction).
        namespace_id: The namespace internal ID.
        session_id: The session internal ID that owns the arena.
        entity_ids: List of entity internal IDs to add to the arena.
        arena_public_id: The public UUID bytes for the new arena.

    Returns:
        The arena internal ID.
    """
    cursor = conn.execute(
        "INSERT INTO arena (public_id, namespace_id, session_id) VALUES (?, ?, ?)",
        (arena_public_id, namespace_id, session_id),
    )
    arena_id = cursor.lastrowid
    assert arena_id is not None

    for entity_id in entity_ids:
        conn.execute(
            "INSERT INTO arena_membership (arena_id, entity_id) VALUES (?, ?)",
            (arena_id, entity_id),
        )

    return arena_id

    # Note: Entity reservation operations (_add_entity_reservation, _release_entity_reservation)
    # have been removed. Entity-table GC never runs while sessions are open, so reservations
    # are unnecessary during the bulk copy workflow.


# =============================================================================
# Garbage Collection Operations
# =============================================================================


def check_and_claim_gc(conn: sqlite3.Connection, session_id: int) -> bool:
    """Check if there's garbage and no other collector, claim if so.

    Args:
        conn: sqlite3 connection.
        session_id: The session internal ID that wants to collect garbage.

    Returns:
        True if we claimed GC (caller should run GC loop).
        False if another session is collecting or no garbage exists.
    """
    with transaction(conn, "IMMEDIATE"):
        # Check if another session is already collecting garbage
        cursor = conn.execute("SELECT id FROM session WHERE is_collecting_garbage = 1 LIMIT 1")
        if cursor.fetchone() is not None:
            return False

        # Check if there's any garbage to collect:
        # - Garbage arenas (arenas in garbage sessions)
        # - Garbage sessions (except session 0)
        # - GARBAGE index registrations
        cursor = conn.execute(
            """
            SELECT EXISTS(
                SELECT 1 FROM arena a
                JOIN session s ON a.session_id = s.id
                WHERE s.is_garbage = 1
            ) OR EXISTS(
                SELECT 1 FROM session
                WHERE is_garbage = 1 AND id != ?
            ) OR EXISTS(
                SELECT 1 FROM ns_index_registry
                WHERE state = 'GARBAGE'
            )
            """,
            (GARBAGE_SESSION_ID,),
        )
        row = cursor.fetchone()
        has_garbage = bool(row[0]) if row else False

        if not has_garbage:
            return False

        # Claim garbage collection for this session
        conn.execute(
            "UPDATE session SET is_collecting_garbage = 1 WHERE id = ?",
            (session_id,),
        )
        return True


def release_gc_claim(conn: sqlite3.Connection, session_id: int) -> None:
    """Release garbage collection claim.

    Args:
        conn: sqlite3 connection.
        session_id: The session internal ID that was collecting garbage.
    """
    with transaction(conn, "IMMEDIATE"):
        conn.execute(
            "UPDATE session SET is_collecting_garbage = 0 WHERE id = ?",
            (session_id,),
        )


def run_one_gc_step(conn: sqlite3.Connection) -> tuple[bool, bool]:
    """Run one step of garbage collection.

    Args:
        conn: sqlite3 connection.

    Returns:
        Tuple of (work_done, garbage_created) where:
        - work_done: True if any GC work was done, False if no garbage found.
        - garbage_created: True if new garbage was created (entity rebound to garbage arena).
    """
    with transaction(conn, "IMMEDIATE"):
        # Phase 0: Clean up GARBAGE index registrations
        idx_work_done, indexer_iid, idx_ns_id = (
            sqlite_index_ops._gc_cleanup_one_garbage_registration(conn)
        )
        if idx_work_done:
            assert indexer_iid is not None and idx_ns_id is not None
            garbage_created = _rebind_to_garbage_if_needed(conn, idx_ns_id, indexer_iid)
            return True, garbage_created
        # Step 1: Find any garbage arena
        cursor = conn.execute(
            """
            SELECT a.id, a.namespace_id
            FROM arena a
            JOIN session s ON a.session_id = s.id
            WHERE s.is_garbage = 1
            LIMIT 1
            """
        )
        row = cursor.fetchone()

        if row is not None:
            arena_id, namespace_id = row

            # Check if arena has any entities
            cursor = conn.execute(
                "SELECT entity_id FROM arena_membership WHERE arena_id = ? LIMIT 1",
                (arena_id,),
            )
            entity_row = cursor.fetchone()

            if entity_row is None:
                # Arena is empty - delete it
                conn.execute("DELETE FROM arena WHERE id = ?", (arena_id,))
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("Garbage cleanup: deleted empty garbage arena %s", arena_id)
                return True, False

            # Arena has entities - process one
            entity_id: int = entity_row[0]

            # Remove entity from this arena
            conn.execute(
                "DELETE FROM arena_membership WHERE arena_id = ? AND entity_id = ?",
                (arena_id, entity_id),
            )

            # Check if entity is now orphaned in the namespace
            if _is_entity_orphaned_in_namespace(conn, namespace_id, entity_id):
                # Entity is orphaned - get its deps before removing it
                cursor = conn.execute(
                    "SELECT required_id FROM entity_dep WHERE requirer_id = ?",
                    (entity_id,),
                )
                dep_ids = [r[0] for r in cursor.fetchall()]

                # Remove entity's backrefs (where this entity is the requirer)
                conn.execute(
                    """
                    DELETE FROM namespace_entity_backref
                    WHERE requirer_id = ? AND namespace_id = ?
                    """,
                    (entity_id, namespace_id),
                )

                # Remove entity from namespace ownership
                conn.execute(
                    """
                    DELETE FROM namespace_entity_ownership
                    WHERE namespace_id = ? AND entity_id = ?
                    """,
                    (namespace_id, entity_id),
                )

                # Rebind any deps that are now orphaned to a garbage arena
                garbage_created = False
                for dep_id in dep_ids:
                    if _rebind_to_garbage_if_needed(conn, namespace_id, cast(int, dep_id)):
                        garbage_created = True

                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "Garbage cleanup: removed entity %s from namespace %s",
                        entity_id,
                        namespace_id,
                    )
                return True, garbage_created
            else:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "Garbage cleanup: removed entity %s from garbage arena %s (still needed"
                        " elsewhere)",
                        entity_id,
                        arena_id,
                    )
                return True, False

        # Step 2: No garbage arenas - clean up orphan garbage sessions (not session 0)
        cursor = conn.execute(
            """
            SELECT id FROM session
            WHERE is_garbage = 1 AND id != ?
            AND NOT EXISTS (SELECT 1 FROM arena WHERE arena.session_id = session.id)
            LIMIT 1
            """,
            (GARBAGE_SESSION_ID,),
        )
        row = cursor.fetchone()

        if row is not None:
            orphan_session_id = row[0]
            conn.execute("DELETE FROM session WHERE id = ?", (orphan_session_id,))
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Garbage cleanup: deleted orphan garbage session %s", orphan_session_id
                )
            return True, False

        # No garbage found
        return False, False


# =============================================================================
# Arena Operations
# =============================================================================


def create_arena(
    conn: sqlite3.Connection, public_id: bytes, namespace_id: int, session_id: int
) -> int:
    """Create an empty arena and return its internal ID.

    Args:
        conn: sqlite3 connection.
        public_id: The arena's public UUID as bytes.
        namespace_id: The namespace internal ID.
        session_id: The session internal ID that owns the arena.

    Returns:
        The arena internal ID.
    """
    with transaction(conn, "IMMEDIATE"):
        cursor = conn.execute(
            "INSERT INTO arena (public_id, namespace_id, session_id) VALUES (?, ?, ?)",
            (public_id, namespace_id, session_id),
        )
        arena_id = cursor.lastrowid
        assert arena_id is not None
        return arena_id


def create_arena_with_entities(
    conn: sqlite3.Connection,
    public_id: bytes,
    namespace_id: int,
    session_id: int,
    entity_ids: list[int],
) -> int:
    """Create an arena with initial entity memberships.

    Args:
        conn: sqlite3 connection.
        public_id: The arena's public UUID as bytes.
        namespace_id: The namespace internal ID.
        session_id: The session internal ID that owns the arena.
        entity_ids: List of entity internal IDs to add to the arena.

    Returns:
        The arena internal ID.
    """
    with transaction(conn, "IMMEDIATE"):
        return _create_arena_with_entities(conn, namespace_id, session_id, entity_ids, public_id)


# =============================================================================
# Bulk Copy Operations (v2 — batched transactions)
# =============================================================================


@dataclasses.dataclass
class SaveItem:
    """An entity that needs to be saved to the entity tables during bulk copy."""

    fxtr_id: FxtrID
    public_id_bytes: bytes
    cbor_bytes: bytes | None
    deps: list[tuple[FxtrID, DepKind]]
    deps_as_bytes: list[tuple[bytes, str]]  # (public_id_bytes, kind_str)


def discover_entity_by_public_id(
    conn: sqlite3.Connection,
    public_id: bytes,
) -> int | None:
    """Check if a committed entity exists in the entity table. Read-only.

    Args:
        conn: sqlite3 connection.
        public_id: The entity's public ID bytes.

    Returns:
        The entity's internal ID if found (committed), or None if not found.
    """
    cursor = conn.execute(
        "SELECT id FROM entity WHERE public_id = ?",
        (public_id,),
    )
    row = cursor.fetchone()
    return row[0] if row is not None else None


def bulk_copy_commit(
    conn: sqlite3.Connection,
    namespace_id: int,
    arena_public_id: bytes,
    session_id: int,
    root_public_id_bytes: bytes,
    preexisting: dict[bytes, int],
    save_queue: list[SaveItem],
    segment_manager: Any,
    segments_dir: str,
    max_segment_size: int = 512 * 1024 * 1024,
) -> tuple[dict[bytes, int], int]:
    """Execute the big commit: save entities to segment files, add to namespace/arena.

    Single IMMEDIATE transaction that allocates entity IDs on the fly, writes blobs
    to the active segment file, and uses a recursive CTE to bulk-add reachable
    entities to the namespace and arena.

    Crash safety: segment data is flushed to disk (fdatasync/F_FULLFSYNC) before the
    SQLite transaction commits. If a crash occurs between flush and commit, startup
    crash recovery truncates the segment to the committed write_cursor.

    Args:
        conn: sqlite3 connection.
        namespace_id: The namespace internal ID.
        arena_public_id: The arena's public UUID as bytes.
        session_id: The session internal ID.
        root_public_id_bytes: The root entity's public ID bytes (to resolve root internal_id).
        preexisting: Map of public_id_bytes -> internal_id for preexisting entities.
        save_queue: Topologically ordered list of SaveItem objects (deps first).
        segment_manager: SegmentManager instance for reading existing blobs.
        segments_dir: Path to the segments directory.
        max_segment_size: Soft limit for segment rotation (default 512 MB).

    Returns:
        Tuple of (resolved, arena_id) where resolved is the map of
        public_id_bytes -> internal_id for all entities.
    """
    import fcntl

    from fxtr2.data.sqlite_store.entity_blob import (
        build_closed_precomp_map_for_segments,
        encode_blob,
    )
    from fxtr2.data.sqlite_store.segment_io import append_record, flush_segment

    # Acquire write lock (released when fd is closed)
    lock_path = os.path.join(segments_dir, "write.lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        with transaction(conn, "IMMEDIATE"):
            # Read active segment info
            cursor = conn.execute(
                "SELECT segment_id, write_cursor FROM segment_metadata "
                "WHERE status = 'active' LIMIT 1"
            )
            row = cursor.fetchone()
            assert row is not None, "No active segment found"
            active_seg_id: int = row[0]
            write_cursor: int = row[1]

            # Open segment file and seek to write_cursor
            seg_path = segment_manager.segment_path(active_seg_id)
            seg_fd = os.open(seg_path, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                os.lseek(seg_fd, write_cursor, os.SEEK_SET)

                # Create arena
                cursor = conn.execute(
                    "INSERT INTO arena (public_id, namespace_id, session_id) VALUES (?, ?, ?)",
                    (arena_public_id, namespace_id, session_id),
                )
                arena_id = cursor.lastrowid
                assert arena_id is not None

                # Pre-fetch segment locations for preexisting deps
                preexisting_seg_locs: dict[int, tuple[int, int, int]] = {}
                preexisting_ids = list(preexisting.values())
                if preexisting_ids:
                    if len(preexisting_ids) <= _JSON_EACH_THRESHOLD:
                        placeholders = ",".join("?" * len(preexisting_ids))
                        cursor = conn.execute(
                            f"SELECT id, seg_id, seg_offset, seg_length FROM entity "
                            f"WHERE id IN ({placeholders})",
                            preexisting_ids,
                        )
                    else:
                        cursor = conn.execute(
                            "SELECT id, seg_id, seg_offset, seg_length FROM entity"
                            " WHERE id IN (SELECT value FROM json_each(?))",
                            (json.dumps(preexisting_ids),),
                        )
                    for r in cursor.fetchall():
                        if r[1] is not None:
                            preexisting_seg_locs[r[0]] = (r[1], r[2], r[3])

                resolved: dict[bytes, int] = dict(preexisting)
                precomp_cache: dict[int, dict[int, list]] = {}
                # Track (internal_id -> offset) for same-batch/segment dep resolution
                batch_offsets: dict[int, int] = {}

                def read_blob(dep_internal_id: int) -> tuple[bytes, bool]:
                    """Read a dep's blob and report whether it's in the same segment.

                    Returns (blob_bytes, same_segment). same_segment is True when the
                    dep lives in active_seg_id, letting the caller know inherited
                    precomp offsets are valid.

                    For same-batch deps, this reads from the active segment file via a
                    separate O_RDONLY fd (through segment_manager) while we're writing
                    to the same file through the O_RDWR seg_fd. This works because
                    os.pread() sees the current file contents via the OS page cache —
                    unlike LMDB, there's no MVCC snapshot isolation between fds.
                    """
                    # Same-batch: already written to this segment
                    if dep_internal_id in batch_offsets:
                        dep_offset = batch_offsets[dep_internal_id]
                        blob = segment_manager.read_record_at(active_seg_id, dep_offset)
                        return blob, True
                    # Preexisting: known segment location
                    if dep_internal_id in preexisting_seg_locs:
                        sid, off, length = preexisting_seg_locs[dep_internal_id]
                        blob = segment_manager.read_record(sid, off, length)
                        return blob, sid == active_seg_id
                    raise ValueError(
                        f"Dependency with internal_id {dep_internal_id} "
                        "not found in batch or preexisting segments"
                    )

                # Process save queue
                for item in save_queue:
                    # Race check: has someone else committed this entity?
                    cursor = conn.execute(
                        "SELECT id FROM entity WHERE public_id = ? AND public_id IS NOT NULL",
                        (item.public_id_bytes,),
                    )
                    row = cursor.fetchone()

                    if row is not None:
                        # Raced — use winner's ID
                        winner_id = row[0]
                        resolved[item.public_id_bytes] = winner_id
                        # Fetch segment location so read_blob can find this dep later
                        if winner_id not in preexisting_seg_locs:
                            loc_row = conn.execute(
                                "SELECT seg_id, seg_offset, seg_length FROM entity WHERE id = ?",
                                (winner_id,),
                            ).fetchone()
                            if loc_row is not None and loc_row[0] is not None:
                                preexisting_seg_locs[winner_id] = (
                                    loc_row[0],
                                    loc_row[1],
                                    loc_row[2],
                                )
                        continue

                    # Allocate ID on the fly
                    cursor = conn.execute(
                        "INSERT INTO entity (public_id, storage_location) "
                        "VALUES (?, 'data2/seg')",
                        (item.public_id_bytes,),
                    )
                    internal_id = cursor.lastrowid
                    assert internal_id is not None

                    # Resolve dep internal IDs
                    deps_internal: list[tuple[int, str]] = [
                        (resolved[dep_pub_id], kind) for dep_pub_id, kind in item.deps_as_bytes
                    ]

                    # Build own precomp entry (5 elements with seg_offset placeholder)
                    unwraps_to = None
                    ctx_node = None
                    backstep = None
                    for dep_iid, dep_kind in deps_internal:
                        if dep_kind == "unwraps_to":
                            unwraps_to = dep_iid
                        elif dep_kind == "ctx_node":
                            ctx_node = dep_iid
                        elif dep_kind == "backstep":
                            backstep = dep_iid

                    # 5th element: seg_offset will be filled after append
                    own_precomp_entry: list = [
                        item.public_id_bytes,
                        unwraps_to,
                        ctx_node,
                        backstep,
                        None,
                    ]

                    # Build closed precomp map (read_blob handles cross-segment offset clearing)
                    precomp_map = build_closed_precomp_map_for_segments(
                        internal_id,
                        own_precomp_entry,
                        deps_internal,
                        precomp_cache,
                        read_blob,
                    )

                    # Fill in same-batch offsets for deps written earlier in this commit
                    for iid, entry in precomp_map.items():
                        if len(entry) >= 5 and iid in batch_offsets:
                            entry[4] = batch_offsets[iid]

                    # Build metadata and encode blob
                    metadata = {
                        "deps": [[iid, kind] for iid, kind in deps_internal],
                        "precomp": precomp_map,
                    }
                    blob = encode_blob(item.public_id_bytes, item.cbor_bytes, metadata)

                    # Append to segment file
                    offset, length = append_record(seg_fd, blob)

                    # Update own precomp entry's seg_offset now that we know it.
                    # Note: this mutates the list in precomp_map (same object), but the
                    # blob was already encoded above with seg_offset=None for the own
                    # entry. The persisted blob always has None for the entity's own
                    # seg_offset. This in-memory update only benefits later entities in
                    # the same batch that reference this one via precomp_cache.
                    own_precomp_entry[4] = offset

                    # Fill in seg_id/seg_offset/seg_length on the entity row
                    conn.execute(
                        "UPDATE entity SET seg_id = ?, seg_offset = ?, seg_length = ? "
                        "WHERE id = ?",
                        (active_seg_id, offset, length, internal_id),
                    )

                    # Insert entity_dep rows
                    if deps_internal:
                        conn.executemany(
                            "INSERT INTO entity_dep "
                            "(requirer_id, required_id, kind) VALUES (?, ?, ?)",
                            [
                                (internal_id, dep_iid, dep_kind)
                                for dep_iid, dep_kind in deps_internal
                            ],
                        )

                    resolved[item.public_id_bytes] = internal_id
                    precomp_cache[internal_id] = precomp_map
                    batch_offsets[internal_id] = offset

                # Flush segment to disk before SQLite commits
                flush_segment(seg_fd)

                # Update write_cursor
                new_cursor = os.lseek(seg_fd, 0, os.SEEK_CUR)
                conn.execute(
                    "UPDATE segment_metadata SET write_cursor = ? WHERE segment_id = ?",
                    (new_cursor, active_seg_id),
                )

                # Segment rotation check
                if new_cursor > max_segment_size:
                    conn.execute(
                        "UPDATE segment_metadata SET status = 'sealed' " "WHERE segment_id = ?",
                        (active_seg_id,),
                    )
                    new_seg_id = active_seg_id + 1
                    now = datetime.now(timezone.utc)
                    conn.execute(
                        "INSERT INTO segment_metadata "
                        "(segment_id, status, write_cursor, created_at) "
                        "VALUES (?, 'active', 0, ?)",
                        (new_seg_id, now.isoformat()),
                    )
                    new_seg_path = segment_manager.segment_path(new_seg_id)
                    new_fd = os.open(new_seg_path, os.O_CREAT | os.O_RDWR, 0o644)
                    os.close(new_fd)

            finally:
                os.close(seg_fd)

            # CTE for namespace + arena membership
            root_iid = resolved[root_public_id_bytes]
            _bulk_add_namespace_and_arena_via_cte(conn, namespace_id, arena_id, root_iid)

            # SQLite COMMIT happens here (transaction context manager exit)

    finally:
        os.close(lock_fd)

    return resolved, arena_id


def bulk_copy_commit_preexisting_only(
    conn: sqlite3.Connection,
    namespace_id: int,
    arena_public_id: bytes,
    session_id: int,
    root_internal_id: int,
) -> int:
    """Fast path: all entities preexist, just create arena and add to namespace.

    Creates an arena and uses the recursive CTE to add all reachable entities
    to the namespace and arena.

    Args:
        conn: sqlite3 connection.
        namespace_id: The namespace internal ID.
        arena_public_id: The arena's public UUID as bytes.
        session_id: The session internal ID.
        root_internal_id: The root entity's internal ID.

    Returns:
        The arena internal ID.
    """
    with transaction(conn, "IMMEDIATE"):
        # Create arena
        cursor = conn.execute(
            "INSERT INTO arena (public_id, namespace_id, session_id) VALUES (?, ?, ?)",
            (arena_public_id, namespace_id, session_id),
        )
        arena_id = cursor.lastrowid
        assert arena_id is not None

        # Use CTE to add to namespace and arena
        _bulk_add_namespace_and_arena_via_cte(conn, namespace_id, arena_id, root_internal_id)

    return arena_id


def _bulk_add_namespace_and_arena_via_cte(
    conn: sqlite3.Connection,
    namespace_id: int,
    arena_id: int,
    root_internal_id: int,
) -> None:
    """Use recursive CTE to add all reachable entities to namespace, and root to arena.

    Walks entity_dep from the root entity, finds all transitively-reachable entities,
    and bulk-adds any not yet in the namespace. Only adds the root entity to the arena
    (the arena is temporary and only needs to hold the root).

    MUST be called within an existing transaction (internal helper).

    Args:
        conn: sqlite3 connection (must be in a transaction).
        namespace_id: The namespace internal ID.
        arena_id: The arena internal ID.
        root_internal_id: The root entity's internal ID.
    """
    # Find reachable entities not yet in namespace (CTE inlined into temp table creation)
    conn.execute(
        """
        CREATE TEMP TABLE _bulk_ns_add AS
        WITH RECURSIVE reachable(entity_id) AS (
            SELECT ?
            UNION
            SELECT ed.required_id
            FROM entity_dep ed
            JOIN reachable ON ed.requirer_id = reachable.entity_id
        )
        SELECT entity_id FROM reachable
        WHERE NOT EXISTS (
            SELECT 1 FROM namespace_entity_ownership
            WHERE namespace_id = ? AND entity_id = reachable.entity_id
        )
        """,
        (root_internal_id, namespace_id),
    )

    # Bulk add to namespace_entity_ownership
    conn.execute(
        """
        INSERT INTO namespace_entity_ownership (namespace_id, entity_id)
        SELECT ?, entity_id FROM _bulk_ns_add
        """,
        (namespace_id,),
    )

    # Bulk add backrefs for newly-added namespace entities
    conn.execute(
        """
        INSERT OR IGNORE INTO namespace_entity_backref
            (namespace_id, requirer_id, required_id, kind)
        SELECT ?, ed.requirer_id, ed.required_id, ed.kind
        FROM entity_dep ed
        WHERE ed.requirer_id IN (SELECT entity_id FROM _bulk_ns_add)
        """,
        (namespace_id,),
    )

    # Add only the root entity to the arena (arena is temporary, only needs root)
    conn.execute(
        "INSERT INTO arena_membership (arena_id, entity_id) VALUES (?, ?)",
        (arena_id, root_internal_id),
    )

    # Cleanup temp table
    conn.execute("DROP TABLE _bulk_ns_add")


# =============================================================================
# Precomputed Data Loading (REMOVED IN V4)
# =============================================================================

# Note: load_precomp_data_for_entities has been removed in v4.
# Entity data and precomputed metadata are now read from segment files
# in SQLiteNamespaceArena (see _load_into_tracked method).

# =============================================================================
# Backref Queries
# =============================================================================


def get_backrefs_for_entity(
    conn: sqlite3.Connection,
    namespace_id: int,
    entity_public_id: bytes,
) -> tuple[int | None, list[tuple[bytes, str]], list[str], list[tuple[bytes, bytes, bool]]]:
    """Get all backrefs for an entity.

    Args:
        conn: sqlite3 connection.
        namespace_id: The namespace internal ID.
        entity_public_id: The entity's public ID bytes.

    Returns:
        Tuple of (entity_internal_id, entity_backrefs, pin_names, arena_backrefs) where:
        - entity_internal_id: The entity's internal ID, or None if not in namespace
        - entity_backrefs: list of (requirer_public_id, dep_kind)
        - pin_names: list of pin names
        - arena_backrefs: list of (arena_public_id, session_public_id, is_garbage)
    """
    with transaction(conn, "DEFERRED"):
        # First, look up the entity's internal ID and verify it's in this namespace
        cursor = conn.execute(
            """
            SELECT e.id
            FROM entity e
            JOIN namespace_entity_ownership neo ON e.id = neo.entity_id
            WHERE e.public_id = ? AND neo.namespace_id = ?
            """,
            (entity_public_id, namespace_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None, [], [], []
        entity_internal_id: int = cast(int, row[0])

        # 1. Entity backrefs: find entities in this namespace that reference this entity
        cursor = conn.execute(
            """
            SELECT e.public_id, neb.kind
            FROM namespace_entity_backref neb
            JOIN entity e ON neb.requirer_id = e.id
            WHERE neb.required_id = ? AND neb.namespace_id = ?
            """,
            (entity_internal_id, namespace_id),
        )
        entity_backrefs: list[tuple[bytes, str]] = [
            (bytes(cast(bytes, row[0])), cast(str, row[1])) for row in cursor.fetchall()
        ]

        # 2. Pin backrefs: find pins that reference this entity
        cursor = conn.execute(
            """
            SELECT name FROM namespace_pin
            WHERE entity_id = ? AND namespace_id = ?
            """,
            (entity_internal_id, namespace_id),
        )
        pin_names: list[str] = [cast(str, row[0]) for row in cursor.fetchall()]

        # 3. Arena backrefs: find all arenas (across all sessions) containing this entity
        cursor = conn.execute(
            """
            SELECT a.public_id, s.public_id, s.is_garbage
            FROM arena_membership am
            JOIN arena a ON am.arena_id = a.id
            JOIN session s ON a.session_id = s.id
            WHERE am.entity_id = ? AND a.namespace_id = ?
            """,
            (entity_internal_id, namespace_id),
        )
        arena_backrefs: list[tuple[bytes, bytes, bool]] = [
            (bytes(cast(bytes, row[0])), bytes(cast(bytes, row[1])), bool(row[2]))
            for row in cursor.fetchall()
        ]

        return entity_internal_id, entity_backrefs, pin_names, arena_backrefs


# =============================================================================
# Read-only Arena Operations (no DB arena creation)
# =============================================================================


def retrieve_pinned_entity(
    conn: sqlite3.Connection,
    namespace_id: int,
    pin_name: str,
) -> tuple[int, bytes, int, int, int]:
    """Look up a pin and return the entity's internal ID, public ID, and segment location.

    Read-only operation — does not create a DB arena.

    Args:
        conn: sqlite3 connection.
        namespace_id: The namespace internal ID.
        pin_name: The name of the pin to look up.

    Returns:
        Tuple of (entity_internal_id, entity_public_id, seg_id, seg_offset, seg_length).

    Raises:
        KeyError: If no pin with that name exists.
    """
    with transaction(conn, "DEFERRED"):
        cursor = conn.execute(
            """
            SELECT np.entity_id, e.public_id, e.seg_id, e.seg_offset, e.seg_length
            FROM namespace_pin np
            JOIN entity e ON np.entity_id = e.id
            WHERE np.namespace_id = ? AND np.name = ?
            """,
            (namespace_id, pin_name),
        )
        row = cursor.fetchone()
        if row is None:
            raise KeyError(f"No pin named {pin_name!r} in namespace")

        return (
            cast(int, row[0]),
            bytes(row[1]),
            cast(int, row[2]),
            cast(int, row[3]),
            cast(int, row[4]),
        )


def lookup_entity_in_namespace(
    conn: sqlite3.Connection,
    namespace_id: int,
    entity_public_id: bytes,
) -> tuple[int, int, int, int] | None:
    """Look up an entity in namespace by public ID.

    Read-only operation — does not create a DB arena.

    Args:
        conn: sqlite3 connection.
        namespace_id: The namespace internal ID.
        entity_public_id: The entity's public ID bytes.

    Returns:
        Tuple of (internal_id, seg_id, seg_offset, seg_length) if found, None otherwise.
    """
    with transaction(conn, "DEFERRED"):
        cursor = conn.execute(
            """
            SELECT e.id, e.seg_id, e.seg_offset, e.seg_length
            FROM entity e
            JOIN namespace_entity_ownership neo ON e.id = neo.entity_id
            WHERE e.public_id = ? AND neo.namespace_id = ?
            """,
            (entity_public_id, namespace_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return cast(int, row[0]), cast(int, row[1]), cast(int, row[2]), cast(int, row[3])


def fetch_entity_segment_locations(
    conn: sqlite3.Connection,
    internal_ids: list[int],
) -> dict[int, tuple[int, int, int]]:
    """Batch-fetch segment locations for a list of entity internal IDs.

    Args:
        conn: sqlite3 connection.
        internal_ids: List of entity internal IDs.

    Returns:
        Dict mapping internal_id -> (seg_id, seg_offset, seg_length).
    """
    if not internal_ids:
        return {}

    with transaction(conn, "DEFERRED"):
        if len(internal_ids) <= _JSON_EACH_THRESHOLD:
            placeholders = ",".join("?" for _ in internal_ids)
            cursor = conn.execute(
                f"SELECT id, seg_id, seg_offset, seg_length FROM entity WHERE id IN ({placeholders})",
                internal_ids,
            )
        else:
            cursor = conn.execute(
                "SELECT id, seg_id, seg_offset, seg_length FROM entity"
                " WHERE id IN (SELECT value FROM json_each(?))",
                (json.dumps(internal_ids),),
            )
        return {
            cast(int, row[0]): (cast(int, row[1]), cast(int, row[2]), cast(int, row[3]))
            for row in cursor
        }


# Note: load_entity_cbor_and_children and load_entity_flat have been removed in v4.
# Entity data is now read from segment files in SQLiteNamespaceArena.
