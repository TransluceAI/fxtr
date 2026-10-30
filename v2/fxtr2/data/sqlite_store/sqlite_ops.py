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

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import cast
from urllib.parse import parse_qs, urlparse

import anyio.to_thread
import cbor2

from fxtr2.data import entity_core
from fxtr2.data.arena_util import visit_and_project_ids_in_canonical_cbor_order
from fxtr2.data.entity_core import (
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawStrongFxtrID,
    RawWeakFxtrID,
)
from fxtr2.data.sqlite_store.sqlite_util import ConnectionPool, ConnectionPoolGroup, transaction

logger = logging.getLogger(__name__)

# =============================================================================
# Constants
# =============================================================================

# Database user version for migrations
DB_USER_VERSION = 2

# Garbage session constants (internal id=0, public_id=zero UUID)
GARBAGE_SESSION_ID = 0
GARBAGE_SESSION_UUID = uuid.UUID(int=0)

# =============================================================================
# SQL Schema Definitions
# =============================================================================

# Table creation SQL - must match the SQLAlchemy schema in sqlite_arena.py
SQL_CREATE_TABLES = [
    # Entity tables
    """CREATE TABLE IF NOT EXISTS entity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id BLOB NOT NULL UNIQUE,
    storage_location VARCHAR(10) NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS entity_dep (
    requirer_id INTEGER NOT NULL REFERENCES entity(id),
    required_id INTEGER NOT NULL REFERENCES entity(id),
    kind VARCHAR(20) NOT NULL,
    UNIQUE (requirer_id, required_id, kind)
)""",
    """CREATE INDEX IF NOT EXISTS ix_entity_dep_requirer ON entity_dep(requirer_id)""",
    """CREATE TABLE IF NOT EXISTS stored_entity_data (
    entity_id INTEGER PRIMARY KEY REFERENCES entity(id),
    data BLOB NOT NULL
)""",
    # Namespace tables
    """CREATE TABLE IF NOT EXISTS namespace (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR NOT NULL,
    kind VARCHAR NOT NULL,
    public_id BLOB(16) NOT NULL UNIQUE
)""",
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
    """CREATE TABLE IF NOT EXISTS arena_membership (
    arena_id INTEGER NOT NULL REFERENCES arena(id),
    entity_id INTEGER NOT NULL REFERENCES entity(id),
    UNIQUE (arena_id, entity_id)
)""",
    """CREATE TABLE IF NOT EXISTS entity_reservations (
    session_id INTEGER NOT NULL REFERENCES session(id),
    entity_id INTEGER NOT NULL REFERENCES entity(id),
    count INTEGER NOT NULL DEFAULT 1,
    UNIQUE (session_id, entity_id)
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
    # Set busy timeout to avoid intermittent SQLITE_BUSY errors
    conn.execute("PRAGMA busy_timeout = 500")


def _is_shared_cache_supported() -> bool:
    """Check if the SQLite build supports shared cache mode.

    Returns:
        True if shared cache is supported, False otherwise.
    """
    # Check compile options for OMIT_SHARED_CACHE
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        cursor = conn.execute("PRAGMA compile_options")
        for row in cursor:
            if row[0] == "OMIT_SHARED_CACHE":
                return False
        return True
    finally:
        conn.close()


def _parse_sqlite_uri(db_path: str) -> tuple[bool, bool, str | None]:
    """Parse a SQLite URI to detect memory database configuration.

    SQLite URI format: file:name?query_params
    For named shared-cache memory DBs: file:memdb1?mode=memory&cache=shared

    Args:
        db_path: The database path or URI.

    Returns:
        Tuple of (is_memory, has_shared_cache, db_name) where:
        - is_memory: True if this is an in-memory database
        - has_shared_cache: True if cache=shared is set
        - db_name: The database name (for named memory DBs), or None
    """
    # Handle plain :memory: case
    if db_path == ":memory:":
        return True, False, None

    # Handle file: URIs
    if not db_path.startswith("file:"):
        return False, False, None

    # Parse the URI - SQLite uses file: scheme
    # urlparse handles file:name?params format
    parsed = urlparse(db_path)

    # The path contains the database name (e.g., "memdb1" from "file:memdb1?...")
    # Note: for "file::memory:?cache=shared", path would be ":memory:"
    db_name = parsed.path if parsed.path else None

    # Parse query parameters
    query_params = parse_qs(parsed.query)

    # Check for mode=memory parameter
    is_mode_memory = "memory" in query_params.get("mode", [])

    # Check for :memory: in path (legacy format like file::memory:?cache=shared)
    is_path_memory = db_name == ":memory:" if db_name else False

    is_memory = is_mode_memory or is_path_memory

    # Check for cache=shared
    has_shared_cache = "shared" in query_params.get("cache", [])

    return is_memory, has_shared_cache, db_name


def _is_memory_db(db_path: str) -> bool:
    """Check if the database path is for an in-memory database."""
    is_memory, _, _ = _parse_sqlite_uri(db_path)
    return is_memory


def _is_valid_named_shared_memory_db(db_path: str) -> bool:
    """Check if the database path is a valid named shared-cache memory database.

    Valid format: file:name?mode=memory&cache=shared
    where 'name' is a non-empty identifier (not :memory:).

    Args:
        db_path: The database path or URI.

    Returns:
        True if this is a valid named shared-cache memory database.
    """
    is_memory, has_shared_cache, db_name = _parse_sqlite_uri(db_path)

    # Must be a memory database with shared cache
    if not is_memory or not has_shared_cache:
        return False

    # Must have a proper name (not :memory: or empty)
    if not db_name or db_name == ":memory:":
        return False

    return True


def setup_database_and_create_pool_group(
    db_path: str,
    write_pool_size: int = 1,
    read_pool_size: int = 5,
) -> ConnectionPoolGroup:
    """Initialize database schema and return a ConnectionPoolGroup.

    This is the main entry point for creating a database connection pool.
    It:
    1. Creates a temporary connection for setup
    2. Sets PRAGMA journal_mode = WAL (for file-based DBs)
    3. Creates tables if needed (checks user_version)
    4. Creates the garbage session (session 0) if needed
    5. Creates and returns a ConnectionPoolGroup

    Each ConnectionPool's per-connection setup handles PRAGMA foreign_keys = ON.

    Args:
        db_path: Path to the SQLite database file, or a named shared-cache memory URI
            like "file:memdb1?mode=memory&cache=shared". Plain ":memory:" and
            "file::memory:?cache=shared" are NOT supported because they don't
            reliably share data between connections.
        write_pool_size: Maximum concurrent write connections (default 1).
        read_pool_size: Maximum concurrent read connections (default 5).

    Returns:
        A ConnectionPoolGroup with separate read and write pools.

    Raises:
        ValueError: If the database version is newer than the code supports,
            or if a non-named-shared-cache memory database is requested.
    """
    # Check for unsupported memory database formats
    is_memory = _is_memory_db(db_path)
    if is_memory:
        if not _is_valid_named_shared_memory_db(db_path):
            raise ValueError(
                f"Memory database path '{db_path}' is not supported. "
                "Use the format 'file:name?mode=memory&cache=shared' where 'name' is a "
                "unique identifier (e.g., 'file:memdb1?mode=memory&cache=shared'). "
                "Plain ':memory:' and 'file::memory:?cache=shared' are not supported "
                "because they don't reliably share data between connections."
            )

    # Create setup connection with manual transaction control
    # For memory databases with URI format, we need to explicitly use URI mode
    use_uri = db_path.startswith("file:")
    setup_conn = sqlite3.connect(
        db_path, isolation_level=None, check_same_thread=False, uri=use_uri
    )
    _connection_setup(setup_conn)

    try:
        # Set WAL mode for file-based databases (not applicable to memory DBs)
        if not is_memory:
            setup_conn.execute("PRAGMA journal_mode = WAL")

        # Check current database version
        cursor = setup_conn.execute("PRAGMA user_version")
        row = cursor.fetchone()
        current_version = row[0] if row else 0

        if current_version > DB_USER_VERSION:
            raise ValueError(
                f"Database version {current_version} is newer than supported version "
                f"{DB_USER_VERSION}. Please upgrade your code."
            )
        elif current_version == 0:
            # Fresh database: create all tables and set version
            with transaction(setup_conn, "IMMEDIATE"):
                for statement in SQL_CREATE_TABLES:
                    setup_conn.execute(statement)
                setup_conn.execute(f"PRAGMA user_version = {DB_USER_VERSION}")
        elif current_version < DB_USER_VERSION:
            # Older database: need migrations. Not supported right now
            raise ValueError(
                f"Database version {current_version} is older than the latest supported version "
                f"{DB_USER_VERSION}. Migrations are not supported right now."
            )
        # else: current_version == DB_USER_VERSION, nothing to do

        # Create garbage session if needed
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

    except BaseException:
        setup_conn.close()
        raise

    # Create connection pools
    # Both pools use read-write connections; reads go through read_pool by convention
    write_pool = ConnectionPool(
        db_path=db_path,
        max_connections=write_pool_size,
        connection_setup=_connection_setup,
        is_memory_db=is_memory,
    )
    read_pool = ConnectionPool(
        db_path=db_path,
        max_connections=read_pool_size,
        connection_setup=_connection_setup,
        is_memory_db=is_memory,
    )

    # For memory databases, we need to keep at least one connection alive to preserve the data.
    if is_memory:
        return ConnectionPoolGroup(
            write_pool=write_pool, read_pool=read_pool, memory_keep_alive_connection=setup_conn
        )
    else:
        setup_conn.close()
        return ConnectionPoolGroup(write_pool=write_pool, read_pool=read_pool)


async def async_setup_database_and_create_pool_group(
    db_path: str,
    write_pool_size: int = 1,
    read_pool_size: int = 5,
) -> ConnectionPoolGroup:
    """Async wrapper for setup_database_and_create_pool_group.

    Runs the database setup in a background thread to avoid blocking the event loop.

    Args:
        db_path: Path to the SQLite database file, or a shared-cache memory URI.
        write_pool_size: Maximum concurrent write connections (default 1).
        read_pool_size: Maximum concurrent read connections (default 5).

    Returns:
        A ConnectionPoolGroup with separate read and write pools.

    Raises:
        ValueError: If the database version is newer than the code supports,
            or if a non-shared-cache memory database is requested.
    """
    return await anyio.to_thread.run_sync(
        setup_database_and_create_pool_group,
        db_path,
        write_pool_size,
        read_pool_size,
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
        conn.execute("DELETE FROM entity_reservations WHERE session_id = ?", (session_id,))
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


def unpin_and_maybe_rebind(conn: sqlite3.Connection, namespace_id: int, name: str) -> bool:
    """Remove a pin and rebind the entity to garbage if orphaned.

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

        # Remove the pin
        conn.execute(
            "DELETE FROM namespace_pin WHERE namespace_id = ? AND name = ?",
            (namespace_id, name),
        )

        # If entity is now orphaned, move it to a garbage arena
        return _rebind_to_garbage_if_needed(conn, namespace_id, entity_id)


def create_or_update_pin(
    conn: sqlite3.Connection,
    namespace_id: int,
    entity_public_id: bytes,
    name: str,
    overwrite: bool,
) -> tuple[int, bool]:
    """Look up an entity in namespace and create or update a pin for it.

    Args:
        conn: sqlite3 connection.
        namespace_id: The namespace internal ID.
        entity_public_id: The entity's public ID bytes to pin.
        name: The pin name.
        overwrite: If True, overwrite existing pin. If False, raise on conflict.

    Returns:
        Tuple of (entity_internal_id, garbage_created):
        - entity_internal_id: The pinned entity's internal ID.
        - garbage_created: True if garbage was created (old entity was rebound).

    Raises:
        RuntimeError: If entity not found in namespace.
        KeyError: If pin exists and overwrite=False.
    """
    with transaction(conn, "IMMEDIATE"):
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

        if existing_pin is not None:
            if not overwrite:
                raise KeyError(f"Pin {name!r} already exists in namespace")

            # Update existing pin
            pin_id, old_entity_id = existing_pin
            conn.execute(
                "UPDATE namespace_pin SET entity_id = ? WHERE id = ?",
                (entity_id, pin_id),
            )

            # Check if old entity is now orphaned
            garbage_created = _rebind_to_garbage_if_needed(conn, namespace_id, old_entity_id)
            return entity_id, garbage_created
        else:
            # Create new pin
            conn.execute(
                "INSERT INTO namespace_pin (namespace_id, entity_id, name) VALUES (?, ?, ?)",
                (namespace_id, entity_id, name),
            )
            return entity_id, False


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
        logger.debug(f"reassign_arena_to_garbage_session executing update")
        conn.execute(
            "UPDATE arena SET session_id = ? WHERE id = ?",
            (GARBAGE_SESSION_ID, arena_id),
        )
        logger.debug(f"reassign_arena_to_garbage_session finished update")


def retrieve_pinned_and_create_arena(
    conn: sqlite3.Connection,
    namespace_id: int,
    session_id: int,
    pin_name: str,
    arena_public_id: bytes,
) -> tuple[int, bytes, int]:
    """Look up a pin and create an arena with the pinned entity.

    Args:
        conn: sqlite3 connection.
        namespace_id: The namespace internal ID.
        session_id: The session internal ID that will own the arena.
        pin_name: The name of the pin to look up.
        arena_public_id: The public UUID bytes for the new arena.

    Returns:
        Tuple of (entity_internal_id, entity_public_id, arena_internal_id).

    Raises:
        KeyError: If no pin with that name exists.
    """
    with transaction(conn, "IMMEDIATE"):
        # Look up the pin
        cursor = conn.execute(
            """
            SELECT np.entity_id, e.public_id
            FROM namespace_pin np
            JOIN entity e ON np.entity_id = e.id
            WHERE np.namespace_id = ? AND np.name = ?
            """,
            (namespace_id, pin_name),
        )
        logger.debug(f"retrieve_pinned_and_create_arena executed first query")
        row = cursor.fetchone()
        logger.debug(f"retrieve_pinned_and_create_arena got first result: %s", row)
        if row is None:
            raise KeyError(f"No pin named {pin_name!r} in namespace")

        entity_internal_id: int = row[0]
        entity_public_id: bytes = row[1]

        # Create arena with the entity
        arena_id = _create_arena_with_entities(
            conn, namespace_id, session_id, [entity_internal_id], arena_public_id
        )

        return entity_internal_id, entity_public_id, arena_id


def lookup_in_namespace_and_create_arena(
    conn: sqlite3.Connection,
    namespace_id: int,
    session_id: int,
    entity_public_id: bytes,
    arena_public_id: bytes,
) -> tuple[int, int] | None:
    """Look up an entity in namespace and create an arena if found.

    Args:
        conn: sqlite3 connection.
        namespace_id: The namespace internal ID.
        session_id: The session internal ID that will own the arena.
        entity_public_id: The entity's public ID bytes.
        arena_public_id: The public UUID bytes for the new arena.

    Returns:
        Tuple of (entity_internal_id, arena_internal_id) if found, None if not found.
    """
    with transaction(conn, "IMMEDIATE"):
        # Look up entity by public_id and check if it's in this namespace
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
            return None

        entity_internal_id: int = row[0]

        # Create arena with the entity
        arena_id = _create_arena_with_entities(
            conn, namespace_id, session_id, [entity_internal_id], arena_public_id
        )

        return entity_internal_id, arena_id


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
        )
        """,
        (entity_id, namespace_id, entity_id, namespace_id, entity_id, namespace_id),
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


# =============================================================================
# Entity Reservation Operations
# =============================================================================


def _add_entity_reservation(conn: sqlite3.Connection, session_id: int, entity_id: int) -> None:
    """Add a reservation for an entity.

    Reservations protect entities from garbage collection during copy operations.
    Multiple reservations can be held for the same entity (count is incremented).

    MUST be called within an existing transaction (internal helper).

    Args:
        conn: sqlite3 connection (must be in a transaction).
        session_id: The session internal ID holding the reservation.
        entity_id: The internal ID of the entity to reserve.
    """
    # Use INSERT ... ON CONFLICT to either insert or increment count
    conn.execute(
        """
        INSERT INTO entity_reservations (session_id, entity_id, count)
        VALUES (?, ?, 1)
        ON CONFLICT (session_id, entity_id) DO UPDATE SET count = count + 1
        """,
        (session_id, entity_id),
    )


def _release_entity_reservation(conn: sqlite3.Connection, session_id: int, entity_id: int) -> None:
    """Release a reservation for an entity.

    Decrements the reservation count, and removes the reservation row if count reaches 0.

    MUST be called within an existing transaction (internal helper).

    Args:
        conn: sqlite3 connection (must be in a transaction).
        session_id: The session internal ID holding the reservation.
        entity_id: The internal ID of the entity to release.
    """
    # Decrement count
    conn.execute(
        """
        UPDATE entity_reservations SET count = count - 1
        WHERE session_id = ? AND entity_id = ?
        """,
        (session_id, entity_id),
    )
    # Delete if count <= 0
    conn.execute(
        """
        DELETE FROM entity_reservations
        WHERE session_id = ? AND entity_id = ? AND count <= 0
        """,
        (session_id, entity_id),
    )


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
        cursor = conn.execute(
            """
            SELECT EXISTS(
                SELECT 1 FROM arena a
                JOIN session s ON a.session_id = s.id
                WHERE s.is_garbage = 1
            ) OR EXISTS(
                SELECT 1 FROM session
                WHERE is_garbage = 1 AND id != ?
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

                logger.debug(
                    "Garbage cleanup: removed entity %s from namespace %s",
                    entity_id,
                    namespace_id,
                )
                return True, garbage_created
            else:
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
            # Manually delete entity reservations before deleting the session
            conn.execute(
                "DELETE FROM entity_reservations WHERE session_id = ?",
                (orphan_session_id,),
            )
            conn.execute("DELETE FROM session WHERE id = ?", (orphan_session_id,))
            logger.debug("Garbage cleanup: deleted orphan garbage session %s", orphan_session_id)
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


def verify_in_namespace_and_add_to_arena(
    conn: sqlite3.Connection,
    namespace_id: int,
    arena_id: int,
    entity_id: int,
) -> None:
    """Verify entity is in namespace, then add to arena.

    Args:
        conn: sqlite3 connection.
        namespace_id: The namespace internal ID.
        arena_id: The arena internal ID.
        entity_id: The entity internal ID.

    Raises:
        RuntimeError: If entity not in namespace.
    """
    with transaction(conn, "IMMEDIATE"):
        # Verify entity is in namespace
        cursor = conn.execute(
            """
            SELECT 1 FROM namespace_entity_ownership
            WHERE namespace_id = ? AND entity_id = ?
            """,
            (namespace_id, entity_id),
        )
        if cursor.fetchone() is None:
            raise RuntimeError(f"Entity {entity_id} is not in namespace {namespace_id}")

        # Add to arena
        conn.execute(
            "INSERT INTO arena_membership (arena_id, entity_id) VALUES (?, ?)",
            (arena_id, entity_id),
        )


def remove_from_arena_and_maybe_rebind(
    conn: sqlite3.Connection,
    namespace_id: int,
    arena_id: int,
    entity_id: int,
) -> bool:
    """Remove entity from arena and rebind to garbage if orphaned.

    Args:
        conn: sqlite3 connection.
        namespace_id: The namespace internal ID.
        arena_id: The arena internal ID.
        entity_id: The entity internal ID.

    Returns:
        True if garbage was created (entity was rebound to garbage arena),
        False otherwise.
    """
    with transaction(conn, "IMMEDIATE"):
        # Remove from arena
        conn.execute(
            "DELETE FROM arena_membership WHERE arena_id = ? AND entity_id = ?",
            (arena_id, entity_id),
        )

        # Rebind to garbage if orphaned
        return _rebind_to_garbage_if_needed(conn, namespace_id, entity_id)


def _add_entity_to_arena_if_not_present(
    conn: sqlite3.Connection,
    arena_id: int,
    entity_id: int,
) -> None:
    """Add entity to arena if not already present.

    MUST be called within an existing transaction (internal helper).

    Args:
        conn: sqlite3 connection (must be in a transaction).
        arena_id: The arena internal ID.
        entity_id: The entity internal ID.
    """
    conn.execute(
        """
        INSERT INTO arena_membership (arena_id, entity_id)
        VALUES (?, ?)
        ON CONFLICT (arena_id, entity_id) DO NOTHING
        """,
        (arena_id, entity_id),
    )


# =============================================================================
# Namespace Operations (helpers for copy worklist)
# =============================================================================


def _add_entity_to_namespace_if_needed(
    conn: sqlite3.Connection,
    namespace_id: int,
    entity_id: int,
) -> None:
    """Add entity to namespace tables if not already present.

    Checks namespace_entity_ownership, and if not present:
    - Inserts into namespace_entity_ownership
    - Copies deps from entity_dep into namespace_entity_backref

    MUST be called within an existing transaction (internal helper).

    Args:
        conn: sqlite3 connection (must be in a transaction).
        namespace_id: The namespace internal ID.
        entity_id: The entity internal ID.

    Raises:
        RuntimeError: If any dependency is not in the namespace.
    """
    # Check if already in namespace
    cursor = conn.execute(
        """
        SELECT 1 FROM namespace_entity_ownership
        WHERE namespace_id = ? AND entity_id = ?
        """,
        (namespace_id, entity_id),
    )
    if cursor.fetchone() is not None:
        return  # Already in namespace

    # Add to ownership
    conn.execute(
        "INSERT INTO namespace_entity_ownership (namespace_id, entity_id) VALUES (?, ?)",
        (namespace_id, entity_id),
    )

    # Copy deps to backrefs
    cursor = conn.execute(
        "SELECT required_id, kind FROM entity_dep WHERE requirer_id = ?",
        (entity_id,),
    )
    for row in cursor.fetchall():
        required_id, kind = row[0], row[1]

        # Ensure the required_id is actually in the namespace
        check_cursor = conn.execute(
            """
            SELECT 1 FROM namespace_entity_ownership
            WHERE namespace_id = ? AND entity_id = ?
            """,
            (namespace_id, required_id),
        )
        if check_cursor.fetchone() is None:
            raise RuntimeError(
                f"Internal invariant violation: child ID {required_id} is not in namespace"
                f" {namespace_id}"
            )

        # Insert a back reference
        conn.execute(
            """
            INSERT INTO namespace_entity_backref (namespace_id, requirer_id, required_id, kind)
            VALUES (?, ?, ?, ?)
            """,
            (namespace_id, entity_id, required_id, kind),
        )


# =============================================================================
# Copy Worklist Operations
# =============================================================================


def copy_discover(
    conn: sqlite3.Connection,
    session_id: int,
    public_id: bytes,
) -> tuple[int | None, bool]:
    """Discover phase: check if entity exists in entity tables.

    Args:
        conn: sqlite3 connection.
        session_id: The session internal ID (for reservations).
        public_id: The entity's public ID bytes.

    Returns:
        Tuple of (internal_id, found):
        - If found: (internal_id, True) - reservation added, caller should enqueue TrackDeps.
        - If not found: (None, False) - caller should fetch from source arena and enqueue Save.
    """
    with transaction(conn, "IMMEDIATE"):
        cursor = conn.execute(
            "SELECT id FROM entity WHERE public_id = ?",
            (public_id,),
        )
        row = cursor.fetchone()

        if row is not None:
            internal_id: int = row[0]
            _add_entity_reservation(conn, session_id, internal_id)
            return internal_id, True

        return None, False


def copy_track_deps(
    conn: sqlite3.Connection,
    session_id: int,
    namespace_id: int,
    temp_arena_id: int,
    entity_id: int,
) -> tuple[bool, list[int]]:
    """TrackDeps phase: check namespace membership, get deps if not in namespace.

    Args:
        conn: sqlite3 connection.
        session_id: The session internal ID (for reservations).
        namespace_id: The namespace internal ID.
        temp_arena_id: The temporary arena internal ID.
        entity_id: The entity internal ID.

    Returns:
        Tuple of (already_in_namespace, dep_internal_ids):
        - If already in namespace: (True, []) - added to arena, released reservation.
        - Otherwise: (False, dep_ids) - loaded dep IDs, added reservations for deps.
    """
    with transaction(conn, "IMMEDIATE"):
        # Check if already in namespace
        cursor = conn.execute(
            """
            SELECT 1 FROM namespace_entity_ownership
            WHERE namespace_id = ? AND entity_id = ?
            """,
            (namespace_id, entity_id),
        )
        if cursor.fetchone() is not None:
            # Already in namespace, add to arena (with ON CONFLICT DO NOTHING)
            _add_entity_to_arena_if_not_present(conn, temp_arena_id, entity_id)
            # Release the reservation
            _release_entity_reservation(conn, session_id, entity_id)
            return True, []

        # Load deps from entity_dep table (just internal IDs)
        cursor = conn.execute(
            "SELECT required_id FROM entity_dep WHERE requirer_id = ?",
            (entity_id,),
        )
        dep_internal_ids: list[int] = [cast(int, row[0]) for row in cursor.fetchall()]

        # Reserve all deps
        for dep_internal_id in dep_internal_ids:
            _add_entity_reservation(conn, session_id, dep_internal_id)

        return False, dep_internal_ids


def copy_save(
    conn: sqlite3.Connection,
    session_id: int,
    public_id: bytes,
    storage_location: str,
    cbor_bytes: bytes | None,
    deps: list[tuple[bytes, str]],
) -> int:
    """Save phase: insert entity into entity tables.

    Args:
        conn: sqlite3 connection.
        session_id: The session internal ID (for reservations).
        public_id: The entity's public ID bytes.
        storage_location: Either "here" or "none".
        cbor_bytes: The CBOR data to store, or None for non-content nodes.
        deps: List of (dep_public_id, kind) tuples.

    Returns:
        The entity's internal ID.
    """
    with transaction(conn, "IMMEDIATE"):
        # Re-check: concurrent process might have saved it
        cursor = conn.execute(
            "SELECT id FROM entity WHERE public_id = ?",
            (public_id,),
        )
        row = cursor.fetchone()

        if row is not None:
            internal_id = row[0]
        else:
            # Insert entity
            cursor = conn.execute(
                "INSERT INTO entity (public_id, storage_location) VALUES (?, ?)",
                (public_id, storage_location),
            )
            internal_id = cursor.lastrowid
            assert internal_id is not None

            # Insert entity data if applicable
            if cbor_bytes is not None:
                conn.execute(
                    "INSERT INTO stored_entity_data (entity_id, data) VALUES (?, ?)",
                    (internal_id, cbor_bytes),
                )

            # Insert deps - need to look up internal IDs from public IDs
            for dep_public_id, dep_kind in deps:
                cursor = conn.execute(
                    "SELECT id FROM entity WHERE public_id = ?",
                    (dep_public_id,),
                )
                dep_row = cursor.fetchone()
                assert dep_row is not None, f"Dep with public_id not found in entity table"
                dep_internal_id = dep_row[0]

                conn.execute(
                    "INSERT INTO entity_dep (requirer_id, required_id, kind) VALUES (?, ?, ?)",
                    (internal_id, dep_internal_id, dep_kind),
                )

        # Add reservation
        _add_entity_reservation(conn, session_id, internal_id)

        return internal_id


def copy_track(
    conn: sqlite3.Connection,
    session_id: int,
    namespace_id: int,
    arena_id: int,
    entity_id: int,
) -> None:
    """Track phase: add entity to namespace and arena.

    Args:
        conn: sqlite3 connection.
        session_id: The session internal ID (for reservations).
        namespace_id: The namespace internal ID.
        arena_id: The arena internal ID.
        entity_id: The entity internal ID.
    """
    with transaction(conn, "IMMEDIATE"):
        _add_entity_to_namespace_if_needed(conn, namespace_id, entity_id)
        # Add to arena
        conn.execute(
            "INSERT INTO arena_membership (arena_id, entity_id) VALUES (?, ?)",
            (arena_id, entity_id),
        )
        # Release reservation
        _release_entity_reservation(conn, session_id, entity_id)


# =============================================================================
# Precomputed Data Loading
# =============================================================================


def load_precomp_data_for_entities(
    conn: sqlite3.Connection,
    initial_entity_ids: list[int],
    sync_dep_kinds: list[str],
) -> tuple[dict[int, bytes], dict[int, list[tuple[int, str]]]]:
    """Load precomputed data for entities and their transitive sync deps.

    Iteratively expands frontier to include all transitively-reachable entities
    via the specified dep kinds.

    Args:
        conn: sqlite3 connection.
        initial_entity_ids: List of entity internal IDs to start from.
        sync_dep_kinds: List of dep kind strings to follow (e.g., ["ctx_node", "backstep", "unwraps_to"]).

    Returns:
        Tuple of (id_to_public_id, id_to_deps) where:
        - id_to_public_id: mapping from internal ID to public ID bytes
        - id_to_deps: mapping from internal ID to list of (required_id, kind) tuples
    """
    with transaction(conn, "DEFERRED"):
        id_to_public_id: dict[int, bytes] = {}
        id_to_deps: dict[int, list[tuple[int, str]]] = {}
        frontier: set[int] = set(initial_entity_ids)

        while frontier:
            # Convert frontier to JSON array for constant SQL
            frontier_list = list(frontier)
            frontier_json = json.dumps(frontier_list)
            sync_dep_kinds_json = json.dumps(sync_dep_kinds)

            # Step 1: Fetch public IDs for internal IDs we haven't seen yet
            cursor = conn.execute(
                """SELECT e.id, e.public_id FROM entity e
                   WHERE e.id IN (SELECT value FROM json_each(?))""",
                (frontier_json,),
            )
            for row in cursor.fetchall():
                internal_id = cast(int, row[0])
                pub_id = cast(bytes, row[1])
                id_to_public_id[internal_id] = bytes(pub_id)
                if internal_id not in id_to_deps:
                    id_to_deps[internal_id] = []

            # Step 2: Fetch deps for the frontier
            cursor = conn.execute(
                """SELECT ed.requirer_id, ed.required_id, ed.kind
                   FROM entity_dep ed
                   WHERE ed.requirer_id IN (SELECT value FROM json_each(?))
                     AND ed.kind IN (SELECT value FROM json_each(?))""",
                (frontier_json, sync_dep_kinds_json),
            )
            dep_rows = cursor.fetchall()

            # Clear frontier, will be repopulated with new internal IDs we discover
            frontier = set()

            for row in dep_rows:
                requirer_id = cast(int, row[0])
                required_id = cast(int, row[1])
                kind = cast(str, row[2])
                # Store the dep
                if requirer_id not in id_to_deps:
                    id_to_deps[requirer_id] = []
                id_to_deps[requirer_id].append((required_id, kind))

                # If we haven't seen this required_id, add to frontier for next iteration
                if required_id not in id_to_public_id:
                    frontier.add(required_id)

        return id_to_public_id, id_to_deps


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
# Entity Load Operation (internal helper)
# =============================================================================


def _load_cbor_and_create_children(
    conn: sqlite3.Connection,
    fxtr_id: FxtrID,
    internal_id: int,
    namespace_id: int,
) -> FxtrCbor:
    """Load entity data, creating child entities in entity tables for p/u nodes.

    For d/n nodes, simply fetches and deserializes the stored CBOR.
    For p/u nodes, loads the underlying data and creates wrapped child entities.

    Does NOT handle namespace or arena membership - caller does that.

    MUST be called within an existing transaction (internal helper).

    Args:
        conn: sqlite3 connection (must be in a transaction).
        fxtr_id: The FxtrID of the entity to load.
        internal_id: The internal ID of the entity.
        namespace_id: The namespace internal ID (needed for child lookups).

    Returns:
        The (possibly transformed) FxtrCbor data.
    """
    tag = fxtr_id.get_tag()

    if tag in ("d", "n"):
        # Simple case: fetch from stored_entity_data and deserialize
        cursor = conn.execute(
            "SELECT data FROM stored_entity_data WHERE entity_id = ?",
            (internal_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise ValueError(f"No stored data for entity {fxtr_id}")
        return entity_core.fxtr_cbor_loads(bytes(row[0]))

    elif tag == "p":
        # P-node: load underlying data, wrap children as new p-nodes
        # Get deps: CTX_NODE and UNWRAPS_TO
        cursor = conn.execute(
            "SELECT kind, required_id FROM entity_dep WHERE requirer_id = ?",
            (internal_id,),
        )
        deps = {cast(str, row[0]): cast(int, row[1]) for row in cursor.fetchall()}
        ctx_node_internal = deps.get("ctx_node")
        unwraps_to_internal = deps.get("unwraps_to")
        assert ctx_node_internal is not None
        assert unwraps_to_internal is not None

        # Get the unwraps_to FxtrID
        cursor = conn.execute(
            "SELECT public_id FROM entity WHERE id = ?",
            (unwraps_to_internal,),
        )
        row = cursor.fetchone()
        assert row is not None
        unwraps_to_fxtr_id = FxtrID(raw_data_id=bytes(row[0]))

        # Recursively load the underlying data
        underlying_data = _load_cbor_and_create_children(
            conn, unwraps_to_fxtr_id, unwraps_to_internal, namespace_id
        )

        # Extract path from current p-node
        _, payload = fxtr_id.split_tag_and_payload()
        current_path = cbor2.loads(payload)
        assert isinstance(current_path, list)

        # Counter for child index
        child_counter = 0

        def transform_p_child(
            typed_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
        ) -> FxtrID:
            nonlocal child_counter
            child_fxtr_id = typed_id.inner_id
            new_path = current_path + [child_counter]
            child_counter += 1
            new_p_id = FxtrID.from_tag_and_payload("p", cbor2.dumps(new_path))

            # Check if new p-node already exists
            cursor = conn.execute(
                "SELECT id FROM entity WHERE public_id = ?",
                (new_p_id._raw_data_id,),
            )
            existing = cursor.fetchone()

            if existing is None:
                # Get internal ID of the child we're wrapping
                cursor = conn.execute(
                    "SELECT id FROM entity WHERE public_id = ?",
                    (child_fxtr_id._raw_data_id,),
                )
                child_row = cursor.fetchone()
                assert child_row is not None
                child_internal = cast(int, child_row[0])

                # Create new p-node entity
                cursor = conn.execute(
                    "INSERT INTO entity (public_id, storage_location) VALUES (?, 'none')",
                    (new_p_id._raw_data_id,),
                )
                new_internal = cursor.lastrowid
                assert new_internal is not None

                # Insert deps: CTX_NODE, UNWRAPS_TO, and optionally BACKSTEP
                conn.execute(
                    "INSERT INTO entity_dep (requirer_id, required_id, kind) VALUES (?, ?,"
                    " 'ctx_node')",
                    (new_internal, ctx_node_internal),
                )
                conn.execute(
                    "INSERT INTO entity_dep (requirer_id, required_id, kind) VALUES (?, ?,"
                    " 'unwraps_to')",
                    (new_internal, child_internal),
                )
                if isinstance(typed_id, RawFxtrEntityMarkerID):
                    # Marker node: add BACKSTEP to parent
                    conn.execute(
                        "INSERT INTO entity_dep (requirer_id, required_id, kind) VALUES (?, ?,"
                        " 'backstep')",
                        (new_internal, internal_id),
                    )

            return new_p_id

        return visit_and_project_ids_in_canonical_cbor_order(underlying_data, transform_p_child)

    elif tag == "u":
        # U-node: load underlying data, wrap children as new u-nodes
        # Get UNWRAPS_TO dep
        cursor = conn.execute(
            "SELECT required_id FROM entity_dep WHERE requirer_id = ? AND kind = 'unwraps_to'",
            (internal_id,),
        )
        row = cursor.fetchone()
        assert row is not None
        unwraps_to_internal = cast(int, row[0])

        # Get the unwraps_to FxtrID
        cursor = conn.execute(
            "SELECT public_id FROM entity WHERE id = ?",
            (unwraps_to_internal,),
        )
        row = cursor.fetchone()
        assert row is not None
        unwraps_to_fxtr_id = FxtrID(raw_data_id=bytes(row[0]))

        # Recursively load the underlying data
        underlying_data = _load_cbor_and_create_children(
            conn, unwraps_to_fxtr_id, unwraps_to_internal, namespace_id
        )

        def transform_u_child(
            typed_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
        ) -> FxtrID:
            child_fxtr_id = typed_id.inner_id
            new_u_id = FxtrID.from_tag_and_payload("u", child_fxtr_id._raw_data_id)

            # Check if new u-node already exists
            cursor = conn.execute(
                "SELECT id FROM entity WHERE public_id = ?",
                (new_u_id._raw_data_id,),
            )
            existing = cursor.fetchone()

            if existing is None:
                # Get internal ID of the child we're wrapping
                cursor = conn.execute(
                    "SELECT id FROM entity WHERE public_id = ?",
                    (child_fxtr_id._raw_data_id,),
                )
                child_row = cursor.fetchone()
                assert child_row is not None
                child_internal = cast(int, child_row[0])

                # Create new u-node entity
                cursor = conn.execute(
                    "INSERT INTO entity (public_id, storage_location) VALUES (?, 'none')",
                    (new_u_id._raw_data_id,),
                )
                new_internal = cursor.lastrowid
                assert new_internal is not None

                # Insert UNWRAPS_TO dep
                conn.execute(
                    "INSERT INTO entity_dep (requirer_id, required_id, kind) VALUES (?, ?,"
                    " 'unwraps_to')",
                    (new_internal, child_internal),
                )

            return new_u_id

        return visit_and_project_ids_in_canonical_cbor_order(underlying_data, transform_u_child)

    else:
        raise ValueError(f"Cannot load contentless node type: {tag}")


def load_and_add_children_to_arena(
    conn: sqlite3.Connection,
    fxtr_id: FxtrID,
    internal_id: int,
    namespace_id: int,
    arena_id: int,
) -> tuple[FxtrCbor, list[int]]:
    """Load entity data and add all children to namespace and arena.

    Args:
        conn: sqlite3 connection.
        fxtr_id: The FxtrID of the entity to load.
        internal_id: The internal ID of the entity.
        namespace_id: The namespace internal ID.
        arena_id: The arena internal ID.

    Returns:
        Tuple of (data, child_internal_ids) where:
        - data: The loaded (possibly transformed) FxtrCbor data
        - child_internal_ids: List of internal IDs of children to load precomp data for
    """
    # Import locally
    from fxtr2.data.arena_util import visit_ids

    with transaction(conn, "IMMEDIATE"):
        # Load data (creates child entities in entity tables for p/u nodes)
        data = _load_cbor_and_create_children(conn, fxtr_id, internal_id, namespace_id)

        # Extract child IDs from returned data
        child_ids: list[FxtrID] = []
        visit_ids(data, lambda tid: child_ids.append(tid.inner_id))

        # Add children to namespace and arena
        child_internal_ids: list[int] = []
        for child_id in child_ids:
            # Look up internal ID
            cursor = conn.execute(
                "SELECT id FROM entity WHERE public_id = ?",
                (child_id._raw_data_id,),
            )
            row = cursor.fetchone()
            assert row is not None, f"Child {child_id} not found in entity table"
            child_internal = cast(int, row[0])
            child_internal_ids.append(child_internal)

            # Add to namespace if needed
            _add_entity_to_namespace_if_needed(conn, namespace_id, child_internal)

            # Add to arena membership (with ON CONFLICT DO NOTHING)
            _add_entity_to_arena_if_not_present(conn, arena_id, child_internal)

        return data, child_internal_ids


def lookup_and_add_to_arena(
    conn: sqlite3.Connection,
    entity_public_id: bytes,
    namespace_id: int,
    arena_id: int,
) -> int | None:
    """Look up an entity in namespace and add to arena if found.

    Args:
        conn: sqlite3 connection.
        entity_public_id: The entity's public ID bytes.
        namespace_id: The namespace internal ID.
        arena_id: The arena internal ID.

    Returns:
        The entity's internal ID if found and added, None if not found.
    """
    with transaction(conn, "IMMEDIATE"):
        # Look up entity by public_id and check if it's in this namespace
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
            return None

        entity_internal_id: int = cast(int, row[0])

        # Add to arena
        _add_entity_to_arena_if_not_present(conn, arena_id, entity_internal_id)

        return entity_internal_id
