"""Entity store backed by a SQLite database.

DEPRECATED: This module is deprecated in favor of fxtr2.data.sqlite_store.

Use fxtr2.data.sqlite_store instead. This legacy SQLAlchemy-based implementation
is kept for reference only. The new APSW-based implementation is 7-10x faster.

See plans/phase2/stage8_sqlite_speedup.md for details on the new implementation.

---

Original description:

Uses `aiosqlite` and SQLAlchemy Core.

Basically everything in the database has an internal int ID (used for tracking relationships and foreign keys etc) and a user-facing ID (which is usually either a UUID or a content-hash identifier).

Database has a user version for future migrations

* Types of thing, which each have an explicit internal int ID
  * Namespace
  * Entity
  * Session
  * Arena
  * Namespace pin
* Tables, viewed as essentially key-value maps (unique constraint across key tuple)
  * (entities)
    * Entity
      * key: entity
      * Unique user-facing id: bytes (Fxtr id)
      * Storage location enum (currently either “none” or “here”)
    * Entity deps
      * key
        * requirer entity
        * required entity
      * string kind (one of “strong_ref”, “weak_ref”, “ctx_node”, “backstep”, “unwraps_to”)
      * (indexed for fast select on requirer==...)
      * Invariant: the dependencies of X are a deterministic function of the actual entity data (as determined by the application)
    * Stored entity data
      * Key: entity
      * Data: blob
  * (namespaces)
    * Namespace
      * key: Namespace
      * name string
      * kind string
      * unique userid uuid
    * Namespace entity ownership
      * key
        * owner Namespace
        * owned Entity
      * (no data, just a set)
      * Invariants:
        * Entity X is in namespace N if and only if at least one of the following is true:
          * Some entity Y is in namespace N and Y requires X
          * X is pinned to N
          * X is in some arena A which is associated with N
    * Namespace entity backrefs
      * key
        * requirer entity
        * required entity
        * namespace
      * (indexed for fast select on required==... AND namespace==...)
      * Invariant: (X, Y, N) is in this backrefs table if and only if: X depends on Y (in entity dep table) and X is in namespace N
    * Namespace pin
      * key: namespace pin
      * parent namespace
      * pinned entity
      * name string (unique per namespace)
  * (sessions)
    * Session
      * key: session
        * Note: Session 0 is always a garbage session, with a zero uuid
      * unique user id uuid
      * created timestamp
      * last seen timestamp
      * is_garbage bool
      * is_collecting_garbage bool
    * Arena
      * key: arena
      * unique user id: uuid
      * namespace
      * session
    * Arena memberships
      * key
        * owner arena
        * owned entity
      * (no data, just a set)
* How operations should work
  * Starting a session:
    * Generate a new UUID
    * In a transaction: add this session to the DB and get its internal ID
    * This is then stored on the SQLiteSharedConnectionManager
  * Stopping a session:
    * When the SQLiteSharedConnectionManager closes or is finalized:
      * In a transaction: Mark the session as garbage
      * Then, if being closed normally (not a finalizer), try to clean up the garbage
  * Cleaning up garbage
    * A loop that can run as long as there is a garbage session. Possibly has some “politeness” mechanism to
    * If a garbage session (other than the zero garbage session) has no arenas: remove the session
    * If a garbage session has an empty arena: remove the arena
    * For a nonempty garbage arena:
      * In a transaction:
        * Find an entity in the arena, call it X
        * Remove X from this arena.
        * If X no longer has any reason to be in the namespace (not pinned, not in any other arena, and not depended on by any other node in the namespace):
          * Remove X from the namespace.
          * Add all of the other entities that X requires to the arena. (This ensures that the invariant is satisfied, in case those other entities were only in the namespace due to X.)
  * Pinning an entity (in a session, copying from some non-sqlite arena in memory)
    * Transaction
      * Create a new arena for the session to track pending deps
    * Recursively, add to the arena, starting from the root (possibly with a stack-push optimization to prevent stack size blowup)
      * Transaction (retryable, deferred)
        * Check if the entity is in the namespace
        * If so, add it to the new arena
      * If the node was added to the arena, return. Otherwise:
      * Collect all dependencies of the entity
      * Transaction (retryable, deferred)
        * Check if any of the dependencies are in the namespace.
        * For any dependency that is, add it to the new arena
        * For any that is not, mark its fxtr ID
      * Take the dependencies that are not in the namespace and recursively add them to the arena using this method
      * Transaction (retryable, deferred)
        * Check if the entity data is already in the entities table.
        * If so:
          * Make sure it has an entry in this namespace, adding it if necessary. If adding it for the first time, also add any backrefs as necessary to maintain the invariant.
          * Add it to the arena.
      * If the node was added to the arena, return. Otherwise:
      * Serialize the node to bytes and figure out all of the relationships that will have to be tracked to add this node
      * Transaction (immediate)
        * Add this entity to the entities table, including its data and all of its requirements
        * Add an entry to this namespace. Also add any backrefs.
        * Add it to the arena
      * Return once this is complete
    * Transaction
      * Add a namespace pin with the given name pointing at the root node, which must be in the namespace (because it is in this arena)
    * Transaction, run whenever the pinning function exits even if it had an exception
      * Reassign the arena to the garbage session (session id 0)
  * Unpinning an entity
    * Transaction
      * Remove the pin entry
      * If that entity has no reason to be in the namespace (not pinned, not in any other arena, and not depended on by any other node in the namespace): move it to a garbage arena
  * Working with Python user arenas
    * Data
      * Python user arenas maintain a sqlite session ID and arena ID (both internal and external IDs), along with cached Python metadata for nodes in the arena that need to be synchronously accessible
      * Specifically arenas store:
        * Their sqlite session and arena IDs
        * A Python set of entities that are live
        * A Python map from (live entity, sync access type) to entity ID
        * A Python list of pending async operations, where we can already compute the result in Python, but need to propagate the changes back to the backend
    * Directly looking up an entity by ID or pin in a new arena
      * Transaction
        * Make sure the entity exists in the namespace / look up the entity by pin
        * Create a new arena in the session
        * Assign the nodes to the arena
      * Wrap the arena ID in a Python arena object
    * Abandoning an entity X in an arena
      * Immediately:
        * Remove the entity from the Python liveset
        * Schedule the deferred op
      * Deferred as a pending async op:
        * If no other cached access in the Python liveset points to it, remove its cached downpointers
        * Transaction
          * Remove X from arena
          * If X no longer has any reason to be in the namespace (not pinned, not in any other arena, and not depended on by any other node in the namespace):
            * Find any arena in the garbage session, or create a new one if necessary
            * Add X to this arena, thus maintaining the invariant
            * (the “any reason to be in the namespace” check should be indexed to make sure it is fast)
    * Performing a “sync” ID step op
      * Immediately:
        * Put the new ID into the arena
        * Schedule the deferred op
      * Deferred as a pending async op
        * Transaction
          * Make sure the requested entity is live and correct (it should be, we are keeping it alive via whatever parent was in the arena before)
          * Add it to the arena
    * Loading
      * Handle all pending async ops in order
      * Load the data for this node from the store however needed (maybe in its own transaction)
      * Transaction
        * Add all children of this node to the arena (already depended on so they are already in the namespace)
      * Transaction (read-only, also this is immutable)
        * Compute the transitive subgraph of other nodes reachable by sync ops from each of the children

TODO: We will also want some sort of signalling mechanism, perhaps via files and inotify, to allow
writers to inform readers at various meaningful stages:
- when a pin is created in a namespace
- when a pin is removed from a namespace
- when a garbage arena is cleared
"""

from __future__ import annotations

import contextlib
import dataclasses
import enum
import logging
import os
import sqlite3
import uuid
import weakref
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Generator,
    Iterable,
    Self,
    override,
)

import anyio
import anyio.abc
import sqlalchemy
import sqlalchemy.event
from sqlalchemy import Boolean, Column, DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    delete,
    exists,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from fxtr2.data import entity_core
from fxtr2.data.abstract_arena import (
    CopyTargetArena,
    EntityBackref,
    ObservableStore,
    OtherBackref,
    PersistentPinningBackend,
    ReadableArena,
)
from fxtr2.data.arena_util import (
    DepKind,
    async_visit_and_project_ids_in_canonical_cbor_order,
    fetch_data_and_deps_from_arena,
    visit_ids,
)
from fxtr2.data.entity_core import (
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawStrongFxtrID,
    RawWeakFxtrID,
)
from fxtr2.data.file_util.lock import (
    LockNotAcquiredError,
    async_file_lock_acquire,
    sync_file_lock_acquire_nonblocking,
)
from fxtr2.util import anyio_util

logger = logging.getLogger(__name__)


# =============================================================================
# Enums
# =============================================================================


class StorageLocation(enum.Enum):
    """Where the entity data is stored."""

    NONE = "none"
    HERE = "here"


# =============================================================================
# Worklist items for copy operation
# =============================================================================


@dataclasses.dataclass
class _DiscoverWork:
    """Work item to discover an entity and determine what needs to be done."""

    entity_id: FxtrID


@dataclasses.dataclass
class _SaveWork:
    """Work item to save an entity to entity tables."""

    entity_id: FxtrID
    cbor_bytes: bytes | None
    deps: list[tuple[FxtrID, DepKind]]


@dataclasses.dataclass
class _TrackWork:
    """Work item to add an entity to namespace and arena, assuming dependencies are there."""

    internal_id: int


@dataclasses.dataclass
class _TrackDepsWork:
    """Work item to ensure an entity and all its dependencies are added to namespace."""

    internal_id: int


# =============================================================================
# SQLAlchemy Core Schema
# =============================================================================

# Naming convention for constraints (helps with migrations)
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)

# -----------------------------------------------------------------------------
# Entity tables
# -----------------------------------------------------------------------------

entity_table = Table(
    "entity",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("public_id", LargeBinary, nullable=False, unique=True),  # FxtrID bytes
    Column(
        "storage_location",
        SAEnum(StorageLocation, name="storage_location_enum"),
        nullable=False,
    ),
)

entity_dep_table = Table(
    "entity_dep",
    metadata,
    Column(
        "requirer_id",
        Integer,
        ForeignKey("entity.id"),
        nullable=False,
    ),
    Column(
        "required_id",
        Integer,
        ForeignKey("entity.id"),
        nullable=False,
    ),
    Column("kind", SAEnum(DepKind, name="dep_kind_enum"), nullable=False),
    UniqueConstraint("requirer_id", "required_id", "kind", name="uq_entity_dep_key"),
    Index("ix_entity_dep_requirer", "requirer_id"),
)

stored_entity_data_table = Table(
    "stored_entity_data",
    metadata,
    Column(
        "entity_id",
        Integer,
        ForeignKey("entity.id"),
        primary_key=True,
    ),
    Column("data", LargeBinary, nullable=False),
)

# -----------------------------------------------------------------------------
# Namespace tables
# -----------------------------------------------------------------------------

namespace_table = Table(
    "namespace",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String, nullable=False),
    Column("kind", String, nullable=False),
    Column("public_id", LargeBinary(16), nullable=False, unique=True),  # UUID as bytes
)

namespace_entity_ownership_table = Table(
    "namespace_entity_ownership",
    metadata,
    Column(
        "namespace_id",
        Integer,
        ForeignKey("namespace.id"),
        nullable=False,
    ),
    Column(
        "entity_id",
        Integer,
        ForeignKey("entity.id"),
        nullable=False,
    ),
    UniqueConstraint("namespace_id", "entity_id", name="uq_namespace_entity_ownership_key"),
)

namespace_entity_backref_table = Table(
    "namespace_entity_backref",
    metadata,
    Column(
        "requirer_id",
        Integer,
        ForeignKey("entity.id"),
        nullable=False,
    ),
    Column(
        "required_id",
        Integer,
        ForeignKey("entity.id"),
        nullable=False,
    ),
    Column(
        "namespace_id",
        Integer,
        ForeignKey("namespace.id"),
        nullable=False,
    ),
    Column(
        "kind",
        SAEnum(DepKind),
        nullable=False,
    ),
    UniqueConstraint(
        "requirer_id", "required_id", "namespace_id", "kind", name="uq_namespace_entity_backref_key"
    ),
    # Index for fast lookup: "find all entities in namespace N that reference entity Y"
    Index("ix_namespace_entity_backref_required_ns", "required_id", "namespace_id"),
)

namespace_pin_table = Table(
    "namespace_pin",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "namespace_id",
        Integer,
        ForeignKey("namespace.id"),
        nullable=False,
    ),
    Column(
        "entity_id",
        Integer,
        ForeignKey("entity.id"),
        nullable=False,
    ),
    Column("name", String, nullable=False),
    UniqueConstraint("namespace_id", "name", name="uq_namespace_pin_name"),
)

# -----------------------------------------------------------------------------
# Session tables
# -----------------------------------------------------------------------------

session_table = Table(
    "session",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("public_id", LargeBinary(16), nullable=False, unique=True),  # UUID as bytes
    Column("created_at", DateTime, nullable=False),
    Column("last_seen_at", DateTime, nullable=False),
    Column("is_garbage", Boolean, nullable=False, default=False),
    Column("is_collecting_garbage", Boolean, nullable=False, default=False),
)

arena_table = Table(
    "arena",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("public_id", LargeBinary(16), nullable=False, unique=True),  # UUID as bytes
    Column(
        "namespace_id",
        Integer,
        ForeignKey("namespace.id"),
        nullable=False,
    ),
    Column(
        "session_id",
        Integer,
        ForeignKey("session.id"),
        nullable=False,
    ),
)

arena_membership_table = Table(
    "arena_membership",
    metadata,
    Column(
        "arena_id",
        Integer,
        ForeignKey("arena.id"),
        nullable=False,
    ),
    Column(
        "entity_id",
        Integer,
        ForeignKey("entity.id"),
        nullable=False,
    ),
    UniqueConstraint("arena_id", "entity_id", name="uq_arena_membership_key"),
)

# Entity reservations - protects entities during copy operations
# When a session is garbage collected, its reservations are manually cleaned up
# before deleting the session.
entity_reservations_table = Table(
    "entity_reservations",
    metadata,
    Column(
        "session_id",
        Integer,
        ForeignKey("session.id"),
        nullable=False,
    ),
    Column(
        "entity_id",
        Integer,
        ForeignKey("entity.id"),
        nullable=False,
    ),
    Column("count", Integer, nullable=False, default=1),
    UniqueConstraint("session_id", "entity_id", name="uq_entity_reservations_key"),
)


# =============================================================================
# Database Initialization
# =============================================================================

# Database user version for migrations
DB_USER_VERSION = 2

# Garbage session constants (internal id=0, public_id=zero UUID)
GARBAGE_SESSION_ID = 0
GARBAGE_SESSION_UUID = uuid.UUID(int=0)


async def init_database(engine: AsyncEngineGroup) -> None:
    """Initialize the database schema and create the garbage session.

    This function:
    1. Checks the current database version
    2. For fresh databases (version 0): creates all tables and sets version
    3. For older databases: runs migrations to bring schema up to date
    4. Creates the garbage session (session 0) if it doesn't exist

    Args:
        engine: The async SQLAlchemy engine connected to the database.

    Raises:
        ValueError: If the database version is newer than the code supports,
            or if there's no migration path available.
    """
    async with engine.write_txn_engine.begin() as conn:

        # seems somewhat suboptimal but not sure how else to execute these pragmas outside of a
        # transaction in autocommit=False mode.

        def _sync_setup(conn: sqlalchemy.Connection) -> None:
            conn.execute(text("COMMIT"))
            conn.execute(text("PRAGMA foreign_keys = ON"))
            conn.execute(text("PRAGMA journal_mode = WAL"))
            conn.execute(text("BEGIN"))

        await conn.run_sync(_sync_setup)

    async with engine.write_txn_engine.begin() as conn:
        # Check current database version
        result = await conn.execute(text("PRAGMA user_version"))
        row = result.fetchone()
        current_version = row[0] if row else 0

        if current_version > DB_USER_VERSION:
            raise ValueError(
                f"Database version {current_version} is newer than supported version "
                f"{DB_USER_VERSION}. Please upgrade your code."
            )
        elif current_version == 0:
            # Fresh database: create all tables and set version
            await conn.run_sync(metadata.create_all)
            await conn.execute(text(f"PRAGMA user_version = {DB_USER_VERSION}"))
        elif current_version < DB_USER_VERSION:
            # Older database: run migrations
            from fxtr2.data.deprecated.sqlite_arena_migrations import run_migrations

            await run_migrations(conn, current_version, DB_USER_VERSION)
        # else: current_version == DB_USER_VERSION, nothing to do

        # Check if garbage session exists, create if not
        result = await conn.execute(
            select(session_table.c.id).where(
                (session_table.c.id == GARBAGE_SESSION_ID)
                & (session_table.c.public_id == GARBAGE_SESSION_UUID.bytes)
            )
        )
        if result.fetchone() is None:
            # Create the garbage session with internal id=0 and public_id=zero UUID
            now = datetime.now(timezone.utc)
            await conn.execute(
                session_table.insert().values(
                    id=GARBAGE_SESSION_ID,
                    public_id=GARBAGE_SESSION_UUID.bytes,
                    created_at=now,
                    last_seen_at=now,
                    is_garbage=True,
                )
            )


@dataclasses.dataclass(frozen=True)
class AsyncEngineGroup:
    """A collection of async engines for a fxtr database."""

    root_engine: AsyncEngine
    read_txn_engine: AsyncEngine
    write_txn_engine: AsyncEngine


def create_fxtr_engine(database_url: str, **kwargs: Any) -> AsyncEngineGroup:
    """Create an async SQLAlchemy engine for a fxtr database.

    Args:
        database_url: SQLite database URL (e.g., "sqlite+aiosqlite:///path/to/db.sqlite")
        **kwargs: Additional arguments passed to create_async_engine.

    Returns:
        A set of async engines configured for the fxtr database.
    """

    kwargs = dict(kwargs)
    # connect_args = {"autocommit": False}
    # connect_args.update(kwargs.pop("connect_args", {}))
    # root_engine = create_async_engine(database_url, connect_args=connect_args, **kwargs)

    # For in-memory databases, SQLAlchemy uses StaticPool which doesn't accept pool_size
    is_memory_db = ":memory:" in database_url
    engine_kwargs: dict[str, Any] = {
        "pool_logging_name": "fxtr2.data.sqlite_arena.create_fxtr_engine",
        **kwargs,
    }
    if not is_memory_db:
        engine_kwargs["pool_size"] = kwargs.pop("pool_size", 2)

    root_engine = create_async_engine(database_url, **engine_kwargs)
    pool_logger = root_engine.sync_engine.pool.logger
    if isinstance(pool_logger, logging.Logger):
        pool_logger.addFilter(SQLAlchemyPoolNoiseFilter())
    else:
        pool_logger.logger.addFilter(SQLAlchemyPoolNoiseFilter())

    read_txn_engine = root_engine.execution_options()
    write_txn_engine = root_engine.execution_options()

    @sqlalchemy.event.listens_for(root_engine.sync_engine, "connect")
    def _do_connect(dbapi_connection, connection_record):
        # disable aiosqlite's emitting of the BEGIN statement entirely.
        dbapi_connection.isolation_level = None

    @sqlalchemy.event.listens_for(read_txn_engine.sync_engine, "begin")
    def _do_begin_read(conn):
        # emit our own BEGIN.  aiosqlite still emits COMMIT/ROLLBACK correctly
        conn.exec_driver_sql("BEGIN DEFERRED")

    @sqlalchemy.event.listens_for(write_txn_engine.sync_engine, "begin")
    def _do_begin_write(conn):
        # emit our own BEGIN.  aiosqlite still emits COMMIT/ROLLBACK correctly
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    # @sqlalchemy.event.listens_for(root_engine.sync_engine, 'handle_error')
    # def _receive_handle_error(exception_context):
    #     if isinstance(exception_context.original_exception, anyio.get_cancelled_exc_class()):
    #         print("! Caught and ignoring cancellation")
    #         exception_context.is_disconnect = False

    return AsyncEngineGroup(root_engine, read_txn_engine, write_txn_engine)


class SQLAlchemyPoolNoiseFilter(logging.Filter):
    """
    Filters out expected/benign SQLAlchemy connection pool errors that occur
    during task cancellation, such as:
    - CancelledError when terminating connections
    - "no active connection" errors during reset/rollback
    """

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        # Filter: "Exception terminating connection" with CancelledError
        if "Exception terminating connection" in msg:
            if record.exc_info:
                exc_type = record.exc_info[0]
                if exc_type is not None and issubclass(exc_type, anyio.get_cancelled_exc_class()):
                    logger.debug(
                        "Caught and ignoring error in SQLAlchemy pool (cancellation exception terminating a connection)",
                        exc_info=record.exc_info,
                    )
                    return False

        # Filter: "Exception during reset or similar" with "no active connection"
        if "Exception during reset or similar" in msg:
            if record.exc_info:
                exc_value = record.exc_info[1]
                if isinstance(
                    exc_value, sqlite3.OperationalError
                ) and "no active connection" in str(exc_value):
                    logger.debug(
                        "Caught and ignoring error in SQLAlchemy pool (no active connection during reset or similar)",
                        exc_info=record.exc_info,
                    )
                    return False

        return True


type InternalEntityID = int


class SQLiteFxtrSession:
    """A session for a fxtr sqlite db."""

    def __init__(
        self,
        async_engine_group: AsyncEngineGroup,
        background_task_group: anyio.abc.TaskGroup,
        _should_start_garbage_worker: bool = True,
    ):
        """Create a new session.

        Args:
            async_engine: The async engine to use for the session.
            background_task_group: A task group that is guaranteed to be running for the full
                lifetime of the session.
            _should_start_garbage_worker: If True, a garbage worker will be spawned to clean up garbage.
        """
        self._async_engine_group = async_engine_group
        self._background_task_group = background_task_group
        self._external_sid: uuid.UUID = uuid.uuid4()
        # If internal ID is not set, session has not been started yet
        # If internal ID is set and finalizer is live, session is active
        # If internal ID is set and finalizer is None, session is garbage
        self._internal_sid: int | None = None
        self._finalizer: weakref.finalize | None = None
        self._should_close_evt: anyio.Event = anyio.Event()
        self._closed_evt: anyio.Event = anyio.Event()
        # The garbage event is set whenever we may have more garbage to clean up.
        self._garbage_evt: anyio.Event | None = None
        self._should_start_garbage_worker = _should_start_garbage_worker

    def __repr__(self) -> str:
        return f"<SQLiteFxtrSession {self._external_sid} ({self._internal_sid})>"

    def _session_files_dir_path(self) -> Path | None:
        """Get the path to the session files directory.

        Returns None for in-memory databases.
        """
        # Extract database path from engine URL
        db_path = self._async_engine_group.root_engine.url.database
        if db_path is None or db_path == ":memory:" or db_path == "":
            return None
        return Path(db_path + "-fxtrsessions")

    def _session_file_path(self) -> Path | None:
        """Get the path to this session's file.

        Returns None for in-memory databases.
        """
        dir_path = self._session_files_dir_path()
        if dir_path is None:
            return None
        return dir_path / str(self._external_sid)

    @contextlib.asynccontextmanager
    async def _hold_session_file(self) -> AsyncIterator[None]:
        """Hold a lock on the session file for the lifetime of the session.

        Creates the session file with PID and timestamp, acquires an exclusive
        flock on it, and holds that lock until the context manager exits.

        For in-memory databases, this is a no-op.
        """
        session_file = self._session_file_path()
        if session_file is None:
            yield
            return

        # Create directory and file
        session_dir = self._session_files_dir_path()
        assert session_dir is not None
        session_dir.mkdir(parents=True, exist_ok=True)

        # Write session info (PID and timestamp)
        now = datetime.now(timezone.utc)
        session_file.write_text(f"{os.getpid()}\n{now.isoformat()}\n")

        # Acquire exclusive lock and hold it
        async with async_file_lock_acquire(session_file, create_if_not_exists=False):
            yield

    async def start(self) -> None:
        """Start the session, allocating it an ID. Enables future operations to be performed."""
        if self._internal_sid is not None:
            raise RuntimeError("Session already started")
        assert self._finalizer is None

        # Set up background task to manage lifecycle.
        engine = self._async_engine_group.write_txn_engine
        external_sid = self._external_sid
        should_close_evt = self._should_close_evt
        closed_evt = self._closed_evt

        async def _lifecycle_handler(task_status: anyio.abc.TaskStatus[int]) -> None:
            logger.debug("Starting session lifecycle handler for %s", external_sid)
            session_file = self._session_file_path()

            async with contextlib.AsyncExitStack() as exit_stack:
                # Conditionally hold session file lock for the session lifetime
                if session_file is not None:
                    await exit_stack.enter_async_context(self._hold_session_file())
                    logger.debug(
                        "Session %s acquired lock on session file %s", external_sid, session_file
                    )

                # Insert session into DB and get internal ID
                now = datetime.now(timezone.utc)
                async with engine.begin() as conn:
                    result = await conn.execute(
                        session_table.insert()
                        .values(
                            public_id=external_sid.bytes,
                            created_at=now,
                            last_seen_at=now,
                            is_garbage=False,
                        )
                        .returning(session_table.c.id)
                    )
                    row = result.fetchone()
                    assert row is not None
                    internal_sid = row[0]
                try:
                    logger.debug(
                        "Session lifecycle handler for %s started with internal ID %s",
                        external_sid,
                        internal_sid,
                    )
                    task_status.started(internal_sid)
                    await should_close_evt.wait()
                    logger.debug(
                        "Session lifecycle handler for %s received should close event",
                        external_sid,
                    )
                finally:
                    logger.debug(
                        "Session lifecycle handler for %s tearing down session", external_sid
                    )
                    with anyio.fail_after(10.0, shield=True):
                        should_close_evt.set()
                        if self._garbage_evt is not None:
                            self._garbage_evt.set()  # Wake up the garbage worker if it is waiting
                        async with engine.begin() as conn:
                            now = datetime.now(timezone.utc)
                            await conn.execute(
                                session_table.update()
                                .where(session_table.c.id == internal_sid)
                                .values(is_garbage=True, last_seen_at=now)
                            )
                        # Delete session file after marking as garbage (lock still held)
                        if session_file is not None:
                            try:
                                session_file.unlink(missing_ok=True)
                                logger.debug(
                                    "Session %s deleted session file %s",
                                    external_sid,
                                    session_file,
                                )
                                # Try to remove empty directory
                                session_dir = self._session_files_dir_path()
                                if session_dir is not None:
                                    try:
                                        session_dir.rmdir()
                                        logger.debug(
                                            "Session %s removed empty session dir %s",
                                            external_sid,
                                            session_dir,
                                        )
                                    except OSError:
                                        pass  # Directory not empty or other error
                            except OSError as e:
                                logger.warning(
                                    "Session %s failed to delete session file %s: %s",
                                    external_sid,
                                    session_file,
                                    e,
                                )
                        closed_evt.set()
                    logger.debug("Session lifecycle handler for %s completed", external_sid)

        self._internal_sid = await self._background_task_group.start(_lifecycle_handler)
        if self._should_start_garbage_worker:
            self._spawn_cleanup_worker()
        self._finalizer = weakref.finalize(self, should_close_evt.set)

    async def aclose(self):
        """Close the session and clean up the session state in the database."""
        if self._internal_sid is None:
            raise RuntimeError("Session not started")
        if self._finalizer is None:
            return
        if self._garbage_evt is not None:
            self._garbage_evt.set()
        self._should_close_evt.set()
        self._finalizer.detach()
        self._finalizer = None
        await self._closed_evt.wait()

    def close(self):
        """Close the session immediately, scheduling database cleanup."""
        if self._internal_sid is None:
            raise RuntimeError("Session not started")
        if self._finalizer is None:
            return
        if self._garbage_evt is not None:
            self._garbage_evt.set()
        self._finalizer()
        self._finalizer = None

    def is_active(self) -> bool:
        """Check if the session is active."""
        return self._internal_sid is not None and not self._should_close_evt.is_set()

    @classmethod
    @contextlib.asynccontextmanager
    async def attach_to_engine(
        cls, async_engine: AsyncEngineGroup, _should_start_garbage_worker: bool = True
    ) -> AsyncIterator[SQLiteFxtrSession]:
        """Create a new session with an existing engine."""
        async with anyio_util.create_task_group(collapse_exc_group=True) as tg:
            session = cls(
                async_engine, tg, _should_start_garbage_worker=_should_start_garbage_worker
            )
            await session.start()
            try:
                yield session
            finally:
                await session.aclose()

    @classmethod
    @contextlib.asynccontextmanager
    async def open(cls, url: str, **kwargs: Any) -> AsyncIterator[SQLiteFxtrSession]:
        """Set up a session with a database URL, initializing the database if necessary.

        This creates an engine, initializes the database schema, and manages a session
        lifecycle. A background task group handles any cleanup tasks scheduled by
        finalizers.

        Args:
            url: SQLite database URL (e.g., "sqlite+aiosqlite:///path/to/db.sqlite")
            **kwargs: Additional arguments passed to create_fxtr_engine.
        """
        engine = create_fxtr_engine(url, **kwargs)
        try:
            # Initialize the database schema
            await init_database(engine)

            # Use a task group to manage cleanup tasks from finalizers
            async with cls.attach_to_engine(engine) as session:
                yield session
        finally:
            await engine.root_engine.dispose()

    async def create_or_retrieve_user_namespace(self, name: str) -> SQLiteNamespaceBackend:
        """Create or retrieve a user namespace.

        Allocates a new namespace with the given name and "user" kind if it one not exist, and
        returns the namespace. Generates a new UUID if needed.

        Args:
            name: The name of the namespace.

        Returns:
            A SQLiteNamespaceBackend object.
        """
        if not self.is_active():
            raise RuntimeError("Session not active")
        async with self._async_engine_group.write_txn_engine.begin() as conn:
            # Check if namespace with this name and kind="user" already exists
            result = await conn.execute(
                select(namespace_table.c.id, namespace_table.c.public_id).where(
                    (namespace_table.c.name == name) & (namespace_table.c.kind == "user")
                )
            )
            row = result.fetchone()

            if row is not None:
                # Namespace exists, retrieve its IDs
                namespace_internal_id = row[0]
                namespace_uuid = uuid.UUID(bytes=row[1])
            else:
                # Namespace doesn't exist, create it with a new UUID
                namespace_uuid = uuid.uuid4()
                result = await conn.execute(
                    namespace_table.insert()
                    .values(
                        name=name,
                        kind="user",
                        public_id=namespace_uuid.bytes,
                    )
                    .returning(namespace_table.c.id)
                )
                row = result.fetchone()
                assert row is not None
                namespace_internal_id = row[0]

        return SQLiteNamespaceBackend(self, namespace_uuid, namespace_internal_id)

    async def _is_entity_orphaned_in_namespace(
        self,
        conn: AsyncConnection,
        namespace_id: int,
        entity_id: InternalEntityID,
    ) -> bool:
        """Check if an entity is orphaned in a namespace.

        An entity is orphaned if it has no reason to be in the namespace: not required by anything
        else in the namespace, not pinned, not in any arena associated with the namespace.

        Assumes that we are already running inside a transaction.
        """
        is_required = exists().where(
            (namespace_entity_backref_table.c.required_id == entity_id)
            & (namespace_entity_backref_table.c.namespace_id == namespace_id)
        )
        is_pinned = exists().where(
            (namespace_pin_table.c.entity_id == entity_id)
            & (namespace_pin_table.c.namespace_id == namespace_id)
        )
        in_arena = exists().where(
            (arena_membership_table.c.entity_id == entity_id)
            & (arena_table.c.id == arena_membership_table.c.arena_id)
            & (arena_table.c.namespace_id == namespace_id)
        )
        result = await conn.execute(select(or_(is_required, is_pinned, in_arena)))
        return not result.scalar()

    async def _rebind_to_garbage_if_needed(
        self,
        conn: AsyncConnection,
        namespace_internal_id: int,
        entity_internal_id: InternalEntityID,
    ):
        """Ensures invariants hold by tracking this entity in a garbage arena if it is unowned.

        The database maintains the invariant that every entity in a namespace is in that namespace
        either because it is required by something else in the namespace, pinned by a name, or
        tracked in an arena. This method ensures that invariant holds for a particular entity that
        is already in the namespace but may not be required. It does this by detecting if this
        entity is currently orphaned, and if so, tracking it in a garbage arena to allow it to be
        cleaned up safely later.

        Assumes that we are already running inside a transaction.
        """
        if not await self._is_entity_orphaned_in_namespace(
            conn, namespace_internal_id, entity_internal_id
        ):
            return

        # Entity is orphaned - find or create a garbage arena in this namespace
        result = await conn.execute(
            select(arena_table.c.id)
            .where(
                (arena_table.c.namespace_id == namespace_internal_id)
                & (arena_table.c.session_id == GARBAGE_SESSION_ID)
            )
            .limit(1)
        )
        row = result.fetchone()
        if row is not None:
            garbage_arena_id = row[0]
        else:
            # Create a new garbage arena for this namespace
            result = await conn.execute(
                arena_table.insert()
                .values(
                    public_id=uuid.uuid4().bytes,
                    namespace_id=namespace_internal_id,
                    session_id=GARBAGE_SESSION_ID,
                )
                .returning(arena_table.c.id)
            )
            row = result.fetchone()
            assert row is not None
            garbage_arena_id = row[0]
            logger.debug(
                "Created garbage arena %s for namespace %s",
                garbage_arena_id,
                namespace_internal_id,
            )

        # Add entity to the garbage arena
        await conn.execute(
            arena_membership_table.insert().values(
                arena_id=garbage_arena_id,
                entity_id=entity_internal_id,
            )
        )
        if self._garbage_evt is not None:
            self._garbage_evt.set()
        logger.debug(
            "Rebound orphaned entity %s to garbage arena %s in namespace %s",
            entity_internal_id,
            garbage_arena_id,
            namespace_internal_id,
        )

    async def _detect_and_mark_dead_sessions(self) -> bool:
        """Detect and mark dead sessions based on session file locking.

        Scans the session files directory for files that are NOT our own session,
        and attempts a non-blocking flock on each. If we can acquire the lock,
        that session's process is dead - we mark it as garbage in the DB and
        delete the file.

        Returns True if any dead sessions were found and marked, False otherwise.
        """
        session_dir = self._session_files_dir_path()
        if session_dir is None:
            return False  # In-memory mode

        if not session_dir.exists():
            return False  # No session files directory

        our_session_id = str(self._external_sid)
        found_dead = False

        try:
            for session_file in session_dir.iterdir():
                if not session_file.is_file():
                    continue

                # Skip our own session file
                if session_file.name == our_session_id:
                    continue

                # Try to acquire non-blocking lock
                try:
                    with sync_file_lock_acquire_nonblocking(session_file):
                        # We acquired the lock - this session is dead
                        logger.info(
                            "Detected dead session %s (file: %s)",
                            session_file.name,
                            session_file,
                        )

                        # Parse session UUID from filename and mark as garbage in DB
                        try:
                            dead_session_uuid = uuid.UUID(session_file.name)
                        except ValueError:
                            logger.warning(
                                "Invalid session file name (not a UUID): %s",
                                session_file.name,
                            )
                            # Delete the invalid file anyway
                            session_file.unlink(missing_ok=True)
                            continue

                        # Mark session as garbage in DB
                        async with self._async_engine_group.write_txn_engine.begin() as conn:
                            result = await conn.execute(
                                session_table.update()
                                .where(session_table.c.public_id == dead_session_uuid.bytes)
                                .values(is_garbage=True)
                            )
                            if result.rowcount > 0:
                                logger.info("Marked dead session %s as garbage", dead_session_uuid)
                            else:
                                logger.debug(
                                    "Dead session %s not found in DB (may have been cleaned up)",
                                    dead_session_uuid,
                                )

                        # Delete the session file (while still holding lock)
                        session_file.unlink(missing_ok=True)
                        logger.debug("Deleted session file %s", session_file)
                        found_dead = True

                except LockNotAcquiredError:
                    # Session is still alive, skip it
                    pass
                except FileNotFoundError:
                    # File was deleted by another process, skip it
                    pass

            # Try to remove empty directory
            if found_dead:
                try:
                    session_dir.rmdir()
                    logger.debug("Removed empty session files directory %s", session_dir)
                except OSError:
                    pass  # Directory not empty or other error

        except OSError as e:
            logger.warning(
                "Error scanning session files directory %s: %s",
                session_dir,
                e,
            )

        return found_dead

    async def _clean_up_garbage(self) -> bool:
        """Run one step of garbage cleanup.

        Garbage cleanup cleans up garbage in garbage sessions:
        - Namespace ownership tracked by arenas in garbage sessions.
        - Empty arenas in garbage sessions.
        - Garbage sessions with no arenas that are not the persistent 0 session.

        Returns True if we found garbage to clean up, False if we did not find any garbage.
        """
        async with self._async_engine_group.write_txn_engine.begin() as conn:
            # Step 1: Find any garbage arena
            result = await conn.execute(
                select(arena_table.c.id, arena_table.c.namespace_id)
                .select_from(
                    arena_table.join(
                        session_table,
                        arena_table.c.session_id == session_table.c.id,
                    )
                )
                .where(session_table.c.is_garbage == True)  # noqa: E712
                .limit(1)
            )
            row = result.fetchone()

            if row is not None:
                arena_id, namespace_id = row

                # Check if arena has any entities
                result = await conn.execute(
                    select(arena_membership_table.c.entity_id)
                    .where(arena_membership_table.c.arena_id == arena_id)
                    .limit(1)
                )
                entity_row = result.fetchone()

                if entity_row is None:
                    # Arena is empty - delete it
                    await conn.execute(delete(arena_table).where(arena_table.c.id == arena_id))
                    logger.debug("Garbage cleanup: deleted empty garbage arena %s", arena_id)
                    return True

                # Arena has entities - process one
                entity_id: int = entity_row[0]

                # Remove entity from this arena
                await conn.execute(
                    delete(arena_membership_table).where(
                        (arena_membership_table.c.arena_id == arena_id)
                        & (arena_membership_table.c.entity_id == entity_id)
                    )
                )

                # Check if entity is now orphaned in the namespace
                if await self._is_entity_orphaned_in_namespace(conn, namespace_id, entity_id):
                    # Entity is orphaned - get its deps before removing it
                    result = await conn.execute(
                        select(entity_dep_table.c.required_id).where(
                            entity_dep_table.c.requirer_id == entity_id
                        )
                    )
                    dep_ids = [r[0] for r in result.fetchall()]

                    # Remove entity's backrefs (where this entity is the requirer)
                    await conn.execute(
                        delete(namespace_entity_backref_table).where(
                            (namespace_entity_backref_table.c.requirer_id == entity_id)
                            & (namespace_entity_backref_table.c.namespace_id == namespace_id)
                        )
                    )

                    # Remove entity from namespace ownership
                    await conn.execute(
                        delete(namespace_entity_ownership_table).where(
                            (namespace_entity_ownership_table.c.namespace_id == namespace_id)
                            & (namespace_entity_ownership_table.c.entity_id == entity_id)
                        )
                    )

                    # Rebind any deps that are now orphaned to a garbage arena
                    for dep_id in dep_ids:
                        await self._rebind_to_garbage_if_needed(conn, namespace_id, dep_id)

                    logger.debug(
                        "Garbage cleanup: removed entity %s from namespace %s",
                        entity_id,
                        namespace_id,
                    )
                else:
                    logger.debug(
                        "Garbage cleanup: removed entity %s from garbage arena %s (still needed"
                        " elsewhere)",
                        entity_id,
                        arena_id,
                    )

                return True

            # Step 2: No garbage arenas - clean up orphan garbage sessions (not session 0)
            result = await conn.execute(
                select(session_table.c.id)
                .where(
                    (session_table.c.is_garbage == True)  # noqa: E712
                    & (session_table.c.id != GARBAGE_SESSION_ID)
                )
                .where(~exists().where(arena_table.c.session_id == session_table.c.id))
                .limit(1)
            )
            row = result.fetchone()

            if row is not None:
                orphan_session_id = row[0]
                # Manually delete entity reservations before deleting the session
                await conn.execute(
                    delete(entity_reservations_table).where(
                        entity_reservations_table.c.session_id == orphan_session_id
                    )
                )
                await conn.execute(
                    delete(session_table).where(session_table.c.id == orphan_session_id)
                )
                logger.debug(
                    "Garbage cleanup: deleted orphan garbage session %s", orphan_session_id
                )
                return True

            # No garbage found
            return False

    def _spawn_cleanup_worker(self):
        """Spawns a worker that runs cleanup continuously while there is garbage to clean up.

        The garbage worker is a long-running background task. It repeatedly runs the following loop:
        - Check if there is a session with is_collecting_garbage=True. If so, finish loop iteration.
        - Check if there is garbage: either an empty garbage session that is not session 0, or any
          garbage session that has an arena. If not, finish loop iteration.
        - Set is_collecting_garbage=True for this session, and enter a try/finally block that will
          set is_collecting_garbage=False on exit.
        - Inner loop: repeatedly call _clean_up_garbage until it returns False.
        - At the end of the outer loop iteration, initialize _garbage_evt to a new event, then
          sleep until signaled by the _garbage_evt, and try again.
        """
        if self._garbage_evt is not None:
            raise RuntimeError("Garbage cleanup worker already started")
        self._garbage_evt = anyio.Event()
        self._garbage_evt.set()

        async def _cleanup_worker():
            logger.debug("Garbage cleanup worker started for session %s", self._external_sid)
            try:
                while self.is_active():
                    try:
                        # First, detect and mark any dead sessions from crashed processes
                        try:
                            await self._detect_and_mark_dead_sessions()
                        except Exception:
                            logger.warning(
                                "Error detecting dead sessions for %s",
                                self._external_sid,
                                exc_info=True,
                            )

                        async with self._async_engine_group.write_txn_engine.begin() as conn:
                            # Check if another session is already collecting garbage
                            result = await conn.execute(
                                select(session_table.c.id).where(
                                    session_table.c.is_collecting_garbage == True  # noqa: E712
                                )
                            )
                            if result.fetchone() is not None:
                                logger.debug(
                                    "Garbage worker: Another session is collecting garbage,"
                                    " skipping this iteration"
                                )
                                has_garbage = False
                                # Skip to sleep/wait
                            else:
                                # Check if there's any garbage to collect
                                has_garbage_arena = exists().where(
                                    (arena_table.c.session_id == session_table.c.id)
                                    & (session_table.c.is_garbage == True)  # noqa: E712
                                )
                                has_garbage_session = (
                                    session_table.c.is_garbage == True  # noqa: E712
                                ) & (session_table.c.id != GARBAGE_SESSION_ID)
                                result = await conn.execute(
                                    select(
                                        exists().where(or_(has_garbage_arena, has_garbage_session))
                                    )
                                )
                                has_garbage = result.scalar()

                                if has_garbage:
                                    # Claim garbage collection for this session
                                    await conn.execute(
                                        session_table.update()
                                        .where(session_table.c.id == self._internal_sid)
                                        .values(is_collecting_garbage=True)
                                    )
                                else:
                                    logger.debug(
                                        "Garbage worker: No garbage to collect, skipping this"
                                        " iteration"
                                    )

                        # If we claimed garbage collection, run the inner loop
                        if has_garbage:

                            try:
                                logger.debug(
                                    "Session %s starting garbage collection", self._external_sid
                                )
                                while self.is_active():
                                    cleaned = await self._clean_up_garbage()
                                    if not cleaned:
                                        break
                                logger.debug(
                                    "Session %s finished garbage collection", self._external_sid
                                )
                            finally:
                                # Release garbage collection claim
                                with anyio.fail_after(10.0, shield=True):
                                    async with (
                                        self._async_engine_group.write_txn_engine.begin() as conn
                                    ):
                                        await conn.execute(
                                            session_table.update()
                                            .where(session_table.c.id == self._internal_sid)
                                            .values(is_collecting_garbage=False)
                                        )

                    except Exception:
                        logger.warning(
                            "Ignoring error in garbage cleanup worker for session %s",
                            self._external_sid,
                            exc_info=True,
                        )

                    if not self.is_active():
                        logger.debug(
                            "Garbage cleanup worker for session %s exiting at end of collection"
                            " pass because session is no longer active",
                            self._external_sid,
                        )
                        break

                    # Reset the garbage event and wait for new garbage or timeout
                    self._garbage_evt = anyio.Event()
                    await self._garbage_evt.wait()
                    logger.debug(
                        "Garbage cleanup worker for session %s awoken by garbage event",
                        self._external_sid,
                    )
            finally:
                self._garbage_evt = None
                logger.debug("Garbage cleanup worker exiting for session %s", self._external_sid)

        self._background_task_group.start_soon(_cleanup_worker)

    async def _dispose_arena_in_db(self, arena_internal_id: int):
        """Dispose of the arena in the database."""
        if not self.is_active():
            logger.debug(
                "Session %s is not active, skipping disposal of arena %s",
                self._external_sid,
                arena_internal_id,
            )
            return
        logger.debug("Disposing of arena %s in the database", arena_internal_id)
        async with self._async_engine_group.write_txn_engine.begin() as conn:
            # Arenas are disposed of in the DB by re-assigning them to the garbage session
            await conn.execute(
                arena_table.update()
                .where(arena_table.c.id == arena_internal_id)
                .values(session_id=0)
            )
        if self._garbage_evt is not None:
            self._garbage_evt.set()
        logger.debug("Disposed of arena %s in the database", arena_internal_id)

    async def _add_entity_reservation(
        self, conn: AsyncConnection, entity_internal_id: InternalEntityID
    ) -> None:
        """Add a reservation for an entity.

        Reservations protect entities from garbage collection during copy operations.
        Multiple reservations can be held for the same entity (count is incremented).

        Must be called within an active transaction.

        Args:
            conn: The database connection (must be in a transaction).
            entity_internal_id: The internal ID of the entity to reserve.
        """
        assert self._internal_sid is not None
        # Use INSERT ... ON CONFLICT to either insert or increment count
        stmt = sqlite_insert(entity_reservations_table).values(
            session_id=self._internal_sid,
            entity_id=entity_internal_id,
            count=1,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["session_id", "entity_id"],
            set_={"count": entity_reservations_table.c.count + 1},
        )
        await conn.execute(stmt)

    async def _release_entity_reservation(
        self, conn: AsyncConnection, entity_internal_id: InternalEntityID
    ) -> None:
        """Release a reservation for an entity.

        Decrements the reservation count, and removes the reservation row if count reaches 0.

        Must be called within an active transaction.

        Args:
            conn: The database connection (must be in a transaction).
            entity_internal_id: The internal ID of the entity to release.
        """
        assert self._internal_sid is not None
        # Decrement count
        await conn.execute(
            entity_reservations_table.update()
            .where(
                (entity_reservations_table.c.session_id == self._internal_sid)
                & (entity_reservations_table.c.entity_id == entity_internal_id)
            )
            .values(count=entity_reservations_table.c.count - 1)
        )
        # Delete if count <= 0
        await conn.execute(
            entity_reservations_table.delete().where(
                (entity_reservations_table.c.session_id == self._internal_sid)
                & (entity_reservations_table.c.entity_id == entity_internal_id)
                & (entity_reservations_table.c.count <= 0)
            )
        )

    async def _load_cbor_and_save_children(
        self,
        conn: AsyncConnection,
        fxtr_id: FxtrID,
        internal_id: int,
        namespace_internal_id: int,
    ) -> FxtrCbor:
        """Load entity data, creating child entities in entity tables for p/u nodes.

        For d/n nodes, simply fetches and deserializes the stored CBOR.
        For p/u nodes, loads the underlying data and creates wrapped child entities.

        Does NOT handle namespace or arena membership - caller does that.

        Args:
            conn: The database connection (must be in a transaction).
            fxtr_id: The FxtrID of the entity to load.
            internal_id: The internal ID of the entity.
            namespace_internal_id: The namespace internal ID (needed for child lookups).

        Returns:
            The (possibly transformed) FxtrCbor data.
        """
        import cbor2

        tag = fxtr_id.get_tag()

        if tag in ("d", "n"):
            # Simple case: fetch from stored_entity_data and deserialize
            result = await conn.execute(
                select(stored_entity_data_table.c.data).where(
                    stored_entity_data_table.c.entity_id == internal_id
                )
            )
            row = result.fetchone()
            if row is None:
                raise ValueError(f"No stored data for entity {fxtr_id}")
            return entity_core.fxtr_cbor_loads(row[0])

        elif tag == "p":
            # P-node: load underlying data, wrap children as new p-nodes
            # Get deps: CTX_NODE and UNWRAPS_TO
            result = await conn.execute(
                select(entity_dep_table.c.required_id, entity_dep_table.c.kind).where(
                    entity_dep_table.c.requirer_id == internal_id
                )
            )
            deps = {row[1]: row[0] for row in result.fetchall()}
            ctx_node_internal = deps.get(DepKind.CTX_NODE)
            unwraps_to_internal = deps.get(DepKind.UNWRAPS_TO)
            assert ctx_node_internal is not None
            assert unwraps_to_internal is not None

            # Get the unwraps_to FxtrID
            result = await conn.execute(
                select(entity_table.c.public_id).where(entity_table.c.id == unwraps_to_internal)
            )
            row = result.fetchone()
            assert row is not None
            unwraps_to_fxtr_id = FxtrID(raw_data_id=row[0])

            # Recursively load the underlying data
            underlying_data = await self._load_cbor_and_save_children(
                conn, unwraps_to_fxtr_id, unwraps_to_internal, namespace_internal_id
            )

            # Extract path from current p-node
            _, payload = fxtr_id.split_tag_and_payload()
            current_path = cbor2.loads(payload)
            assert isinstance(current_path, list)

            # Counter for child index
            child_counter = 0

            async def transform_p_child(
                typed_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
            ) -> FxtrID:
                nonlocal child_counter
                child_fxtr_id = typed_id.inner_id
                new_path = current_path + [child_counter]
                child_counter += 1
                new_p_id = FxtrID.from_tag_and_payload("p", cbor2.dumps(new_path))

                # Check if new p-node already exists
                result = await conn.execute(
                    select(entity_table.c.id).where(
                        entity_table.c.public_id == new_p_id._raw_data_id
                    )
                )
                existing = result.fetchone()

                if existing is None:
                    # Get internal ID of the child we're wrapping
                    result = await conn.execute(
                        select(entity_table.c.id).where(
                            entity_table.c.public_id == child_fxtr_id._raw_data_id
                        )
                    )
                    child_row = result.fetchone()
                    assert child_row is not None
                    child_internal = child_row[0]

                    # Create new p-node entity
                    result = await conn.execute(
                        entity_table.insert()
                        .values(
                            public_id=new_p_id._raw_data_id,
                            storage_location=StorageLocation.NONE,
                        )
                        .returning(entity_table.c.id)
                    )
                    new_row = result.fetchone()
                    assert new_row is not None
                    new_internal = new_row[0]

                    # Insert deps: CTX_NODE, UNWRAPS_TO, and optionally BACKSTEP
                    await conn.execute(
                        entity_dep_table.insert().values(
                            requirer_id=new_internal,
                            required_id=ctx_node_internal,
                            kind=DepKind.CTX_NODE,
                        )
                    )
                    await conn.execute(
                        entity_dep_table.insert().values(
                            requirer_id=new_internal,
                            required_id=child_internal,
                            kind=DepKind.UNWRAPS_TO,
                        )
                    )
                    if isinstance(typed_id, RawFxtrEntityMarkerID):
                        # Marker node: add BACKSTEP to parent
                        await conn.execute(
                            entity_dep_table.insert().values(
                                requirer_id=new_internal,
                                required_id=internal_id,
                                kind=DepKind.BACKSTEP,
                            )
                        )

                return new_p_id

            return await async_visit_and_project_ids_in_canonical_cbor_order(
                underlying_data, transform_p_child
            )

        elif tag == "u":
            # U-node: load underlying data, wrap children as new u-nodes
            # Get UNWRAPS_TO dep
            result = await conn.execute(
                select(entity_dep_table.c.required_id).where(
                    (entity_dep_table.c.requirer_id == internal_id)
                    & (entity_dep_table.c.kind == DepKind.UNWRAPS_TO)
                )
            )
            row = result.fetchone()
            assert row is not None
            unwraps_to_internal = row[0]

            # Get the unwraps_to FxtrID
            result = await conn.execute(
                select(entity_table.c.public_id).where(entity_table.c.id == unwraps_to_internal)
            )
            row = result.fetchone()
            assert row is not None
            unwraps_to_fxtr_id = FxtrID(raw_data_id=row[0])

            # Recursively load the underlying data
            underlying_data = await self._load_cbor_and_save_children(
                conn, unwraps_to_fxtr_id, unwraps_to_internal, namespace_internal_id
            )

            async def transform_u_child(
                typed_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
            ) -> FxtrID:
                child_fxtr_id = typed_id.inner_id
                new_u_id = FxtrID.from_tag_and_payload("u", child_fxtr_id._raw_data_id)

                # Check if new u-node already exists
                result = await conn.execute(
                    select(entity_table.c.id).where(
                        entity_table.c.public_id == new_u_id._raw_data_id
                    )
                )
                existing = result.fetchone()

                if existing is None:
                    # Get internal ID of the child we're wrapping
                    result = await conn.execute(
                        select(entity_table.c.id).where(
                            entity_table.c.public_id == child_fxtr_id._raw_data_id
                        )
                    )
                    child_row = result.fetchone()
                    assert child_row is not None
                    child_internal = child_row[0]

                    # Create new u-node entity
                    result = await conn.execute(
                        entity_table.insert()
                        .values(
                            public_id=new_u_id._raw_data_id,
                            storage_location=StorageLocation.NONE,
                        )
                        .returning(entity_table.c.id)
                    )
                    new_row = result.fetchone()
                    assert new_row is not None
                    new_internal = new_row[0]

                    # Insert UNWRAPS_TO dep
                    await conn.execute(
                        entity_dep_table.insert().values(
                            requirer_id=new_internal,
                            required_id=child_internal,
                            kind=DepKind.UNWRAPS_TO,
                        )
                    )

                return new_u_id

            return await async_visit_and_project_ids_in_canonical_cbor_order(
                underlying_data, transform_u_child
            )

        else:
            raise ValueError(f"Cannot load contentless node type: {tag}")


class SQLiteNamespaceBackend(PersistentPinningBackend, ObservableStore):
    """Pinning backend backed by a SQLite database.

    Each SQLiteNamespaceBackend object combines a SQLiteFxtrSession with a namespace
    identifier. It then handles the individual operations within the namespace.

    Operations in a given SQLiteNamespaceBackend should act like the entities reachable from that
    namespace are the only ones that exist. In particular, `try_lookup` and `backrefs` should only
    return entities that are reachable from the namespace.
    """

    def __init__(
        self, session: SQLiteFxtrSession, namespace_id: uuid.UUID, namespace_internal_id: int
    ):
        self._session = session
        self._namespace_id = namespace_id
        self._namespace_internal_id = namespace_internal_id

    def new_empty_arena(self) -> SQLiteNamespaceArena:
        """Create a new empty arena in this namespace."""
        return SQLiteNamespaceArena._create_empty_in_session(self._session, self, uuid.uuid4())

    @override
    async def pin_entity_from_arena(
        self, key: str, entity_id: FxtrID, source: ReadableArena, *, overwrite: bool = False
    ):
        """Pin an entity from an arena into this backend."""
        if not self._session.is_active():
            raise RuntimeError("Session not active")

        # Create a temporary arena to hold the entity during the pinning process
        temp_arena_internal_id = await self._build_tmp_arena_copying_from_external_arena(
            entity_id, source.copy_to_new_arena([entity_id])
        )
        try:
            # Create the pin entry - the entity is now in the namespace via the temp arena
            async with self._session._async_engine_group.write_txn_engine.begin() as conn:
                # Look up the internal entity ID and verify it's in the namespace
                result = await conn.execute(
                    select(entity_table.c.id)
                    .select_from(
                        entity_table.join(
                            namespace_entity_ownership_table,
                            entity_table.c.id == namespace_entity_ownership_table.c.entity_id,
                        )
                    )
                    .where(
                        (entity_table.c.public_id == entity_id._raw_data_id)
                        & (
                            namespace_entity_ownership_table.c.namespace_id
                            == self._namespace_internal_id
                        )
                    )
                )
                row = result.fetchone()
                if row is None:
                    raise RuntimeError(f"Entity {entity_id} not found in namespace after copy")
                entity_internal_id: int = row[0]

                # Check if pin already exists
                result = await conn.execute(
                    select(namespace_pin_table.c.id, namespace_pin_table.c.entity_id).where(
                        (namespace_pin_table.c.namespace_id == self._namespace_internal_id)
                        & (namespace_pin_table.c.name == key)
                    )
                )
                existing_pin = result.fetchone()

                if existing_pin is not None:
                    if not overwrite:
                        raise KeyError(f"Pin {key!r} already exists in namespace")
                    # Update existing pin and handle potential orphaning of old entity
                    old_entity_internal_id: int = existing_pin[1]
                    await conn.execute(
                        namespace_pin_table.update()
                        .where(namespace_pin_table.c.id == existing_pin[0])
                        .values(entity_id=entity_internal_id)
                    )
                    # Check if old entity is now orphaned
                    await self._session._rebind_to_garbage_if_needed(
                        conn, self._namespace_internal_id, old_entity_internal_id
                    )
                else:
                    # Create new pin
                    await conn.execute(
                        namespace_pin_table.insert().values(
                            namespace_id=self._namespace_internal_id,
                            entity_id=entity_internal_id,
                            name=key,
                        )
                    )
        finally:
            # Abandon the temporary arena - entity stays in namespace because it's now pinned
            await self._session._dispose_arena_in_db(temp_arena_internal_id)

    @override
    async def unpin_entity(self, key: str):
        """Unpin an entity from this namespace."""
        if not self._session.is_active():
            raise RuntimeError("Session not active")

        async with self._session._async_engine_group.write_txn_engine.begin() as conn:
            # Look up the pin to get the entity ID before deleting
            result = await conn.execute(
                select(namespace_pin_table.c.entity_id).where(
                    (namespace_pin_table.c.namespace_id == self._namespace_internal_id)
                    & (namespace_pin_table.c.name == key)
                )
            )
            row = result.fetchone()
            if row is None:
                raise KeyError(f"No pin named {key!r} in namespace")
            entity_internal_id: int = row[0]

            # Remove the pin entry
            await conn.execute(
                delete(namespace_pin_table).where(
                    (namespace_pin_table.c.namespace_id == self._namespace_internal_id)
                    & (namespace_pin_table.c.name == key)
                )
            )

            # If entity is now orphaned, move it to a garbage arena
            await self._session._rebind_to_garbage_if_needed(
                conn, self._namespace_internal_id, entity_internal_id
            )

    async def _create_arena_with_entities(
        self,
        conn: AsyncConnection,
        entity_internal_ids: list[int],
    ) -> tuple[uuid.UUID, int]:
        """Create a new arena and add entities to it.

        Must be called within an active transaction. Returns (arena_uuid, arena_internal_id).
        """
        assert self._session._internal_sid is not None
        arena_uuid = uuid.uuid4()
        result = await conn.execute(
            arena_table.insert()
            .values(
                public_id=arena_uuid.bytes,
                namespace_id=self._namespace_internal_id,
                session_id=self._session._internal_sid,
            )
            .returning(arena_table.c.id)
        )
        row = result.fetchone()
        assert row is not None
        arena_internal_id: int = row[0]

        # Add entities to the arena
        for entity_internal_id in entity_internal_ids:
            await conn.execute(
                arena_membership_table.insert().values(
                    arena_id=arena_internal_id,
                    entity_id=entity_internal_id,
                )
            )

        return arena_uuid, arena_internal_id

    async def _add_entity_to_namespace_if_needed(
        self, conn: AsyncConnection, internal_id: int
    ) -> None:
        """Add entity to namespace tables if not already present.

        Checks namespace_entity_ownership, and if not present:
        - Inserts into namespace_entity_ownership
        - Copies deps from entity_dep into namespace_entity_backref

        Must be called within an active transaction.
        """
        # Check if already in namespace
        result = await conn.execute(
            select(namespace_entity_ownership_table.c.entity_id).where(
                (namespace_entity_ownership_table.c.namespace_id == self._namespace_internal_id)
                & (namespace_entity_ownership_table.c.entity_id == internal_id)
            )
        )
        if result.fetchone() is not None:
            return  # Already in namespace

        # Add to ownership
        await conn.execute(
            namespace_entity_ownership_table.insert().values(
                namespace_id=self._namespace_internal_id,
                entity_id=internal_id,
            )
        )

        # Copy deps to backrefs
        result = await conn.execute(
            select(entity_dep_table.c.required_id, entity_dep_table.c.kind).where(
                entity_dep_table.c.requirer_id == internal_id
            )
        )
        for row in result.fetchall():
            required_id, kind = row[0], row[1]
            # Ensure the required_id is actually in the namespace.
            check_result = await conn.execute(
                select(namespace_entity_ownership_table.c.entity_id).where(
                    (namespace_entity_ownership_table.c.namespace_id == self._namespace_internal_id)
                    & (namespace_entity_ownership_table.c.entity_id == required_id)
                )
            )
            if check_result.fetchone() is None:
                raise RuntimeError(
                    f"Internal invariant violation: child ID {required_id} is not in namespace"
                    f" {self._namespace_internal_id}"
                )
            # Insert a back reference.
            await conn.execute(
                namespace_entity_backref_table.insert().values(
                    namespace_id=self._namespace_internal_id,
                    requirer_id=internal_id,
                    required_id=required_id,
                    kind=kind,
                )
            )

    async def _build_tmp_arena_copying_from_external_arena(
        self, entity_id: FxtrID, source: ReadableArena
    ) -> int:
        """Build a new temporary arena that contains an entity copied from an external arena.

        Builds a new temporary arena that contains the given entity from a source arena. The arena
        may also end up containing some additional entities that are transitively reachable from the
        entity, but this is not guaranteed.

        Uses a four-stage worklist algorithm:
        - Discover: Check if entity exists in database, and enqueue enough additional work to get
          that entity and all its deps into the namespace.
        - TrackDeps: For an entity that exists in the database but may not be in the namespace,
          enqueue enough additional work to get that entity and all its deps into the namespace.
        - Save: For an entity that does not exist in the database, but whose children exist in the
          database, add it to the entity tables and enqueue adding it to the namespace.
        - Track: Add this entity to namespace and arena.

        The temporary arena should usually be closed and discarded after use.

        TODO(daniel): better exception handling cleanup? also probably the entity reservations
        should be attached to the arena so that we can release them better when things crash.

        Args:
            entity_id: The entity to copy into the temporary arena.
            source: The arena to copy the entity from. This arena will be taken over by the
                operation and closed afterward.

        Returns the internal ID of the new arena.
        """
        if not self._session.is_active():
            raise RuntimeError("Session not active")

        # Create temporary arena
        async with self._session._async_engine_group.write_txn_engine.begin() as conn:
            assert self._session._internal_sid is not None
            arena_uuid = uuid.uuid4()
            result = await conn.execute(
                arena_table.insert()
                .values(
                    public_id=arena_uuid.bytes,
                    namespace_id=self._namespace_internal_id,
                    session_id=self._session._internal_sid,
                )
                .returning(arena_table.c.id)
            )
            row = result.fetchone()
            assert row is not None
            temp_arena_id: int = row[0]

        # Track state. Since we do a depth-first processing order and there are no cycles, if we
        # ever see something we have already seen before that implies we have already finished
        # processing it.
        already_seen_in_discover: set[FxtrID] = set()
        already_seen_in_track: set[InternalEntityID] = set()
        # Maps FxtrID -> internal ID for entities we've saved or found
        fxtr_to_internal: dict[FxtrID, int] = {}

        worklist: list[_DiscoverWork | _SaveWork | _TrackWork | _TrackDepsWork] = [
            _DiscoverWork(entity_id=entity_id)
        ]
        logger.debug(
            f"Building temporary arena to transfer entity {entity_id.summary()} into namespace"
            f" {self._namespace_internal_id}"
        )

        while worklist:
            item = worklist.pop()

            if isinstance(item, _DiscoverWork):
                fxtr_id = item.entity_id

                if fxtr_id in already_seen_in_discover:
                    logger.debug(f"  (Discover({fxtr_id.summary()}): already processed, skipping)")
                    continue
                already_seen_in_discover.add(fxtr_id)

                # Case 1: In entity tables - reserve and enqueue TrackDeps
                async with self._session._async_engine_group.write_txn_engine.begin() as conn:
                    result = await conn.execute(
                        select(entity_table.c.id).where(
                            entity_table.c.public_id == fxtr_id._raw_data_id
                        )
                    )
                    row = result.fetchone()
                    if row is not None:
                        internal_id: int = row[0]
                        fxtr_to_internal[fxtr_id] = internal_id
                        await self._session._add_entity_reservation(conn, internal_id)
                        worklist.append(_TrackDepsWork(internal_id=internal_id))
                        logger.debug(
                            f"  Discover({fxtr_id.summary()}): in entity tables as {internal_id}"
                        )
                        logger.debug(f"    -> enqueue TrackDeps({internal_id})")
                        continue

                # Case 2: Not in entity tables - fetch from source
                cbor_bytes, deps = await fetch_data_and_deps_from_arena(fxtr_id, source)

                # Enqueue: save(self) first (popped after deps), then discover(deps)
                worklist.append(_SaveWork(entity_id=fxtr_id, cbor_bytes=cbor_bytes, deps=deps))
                for dep_id, _ in deps:
                    worklist.append(_DiscoverWork(entity_id=dep_id))

                logger.debug(f"  Discover({fxtr_id.summary()}): not found")
                logger.debug(
                    f"    -> push Discover({[dep_id.summary() for dep_id, _ in deps[::-1]]}),"
                    f" Save({fxtr_id.summary()})"
                )

            elif isinstance(item, _TrackDepsWork):
                internal_id = item.internal_id

                if internal_id in already_seen_in_track:
                    logger.debug(f"  (TrackDeps({internal_id}): already processed, skipping)")
                    continue
                already_seen_in_track.add(internal_id)

                async with self._session._async_engine_group.write_txn_engine.begin() as conn:
                    # Check if already in namespace - if so, add to arena and release reservation
                    result = await conn.execute(
                        select(namespace_entity_ownership_table.c.entity_id).where(
                            (
                                namespace_entity_ownership_table.c.namespace_id
                                == self._namespace_internal_id
                            )
                            & (namespace_entity_ownership_table.c.entity_id == internal_id)
                        )
                    )
                    if result.fetchone() is not None:
                        # Already in namespace, add to arena (use ON CONFLICT DO NOTHING)
                        stmt = sqlite_insert(arena_membership_table).values(
                            arena_id=temp_arena_id,
                            entity_id=internal_id,
                        )
                        stmt = stmt.on_conflict_do_nothing()
                        await conn.execute(stmt)
                        # Release the reservation
                        await self._session._release_entity_reservation(conn, internal_id)
                        logger.debug(
                            f"  TrackDeps({internal_id}): already in namespace, added to arena"
                        )
                        continue

                    # Load deps from entity_dep table (just internal IDs)
                    result = await conn.execute(
                        select(entity_dep_table.c.required_id).where(
                            entity_dep_table.c.requirer_id == internal_id
                        )
                    )
                    dep_internal_ids = [row[0] for row in result.fetchall()]

                    # Reserve all deps
                    for dep_internal_id in dep_internal_ids:
                        await self._session._add_entity_reservation(conn, dep_internal_id)

                # Enqueue Track(self) first (will be popped last due to stack ordering)
                worklist.append(_TrackWork(internal_id=internal_id))
                # Then enqueue TrackDeps for all deps (will be processed before self)
                for dep_internal_id in dep_internal_ids:
                    worklist.append(_TrackDepsWork(internal_id=dep_internal_id))

                logger.debug(
                    f"  TrackDeps({internal_id}): {len(dep_internal_ids)} deps to track first"
                )
                if dep_internal_ids:
                    logger.debug(
                        f"    -> push TrackDeps({dep_internal_ids[::-1]}), Track({internal_id})"
                    )

            elif isinstance(item, _SaveWork):
                fxtr_id = item.entity_id
                cbor_bytes = item.cbor_bytes
                deps = item.deps

                async with self._session._async_engine_group.write_txn_engine.begin() as conn:
                    # Re-check: concurrent process might have saved it
                    result = await conn.execute(
                        select(entity_table.c.id).where(
                            entity_table.c.public_id == fxtr_id._raw_data_id
                        )
                    )
                    row = result.fetchone()

                    if row is not None:
                        internal_id = row[0]
                    else:
                        # Determine storage location
                        tag = fxtr_id.get_tag()
                        if tag in ("d", "n"):
                            storage_loc = StorageLocation.HERE
                        else:
                            storage_loc = StorageLocation.NONE

                        # Insert entity
                        result = await conn.execute(
                            entity_table.insert()
                            .values(
                                public_id=fxtr_id._raw_data_id,
                                storage_location=storage_loc,
                            )
                            .returning(entity_table.c.id)
                        )
                        row = result.fetchone()
                        assert row is not None
                        internal_id = row[0]

                        # Insert entity data if applicable
                        if cbor_bytes is not None:
                            await conn.execute(
                                stored_entity_data_table.insert().values(
                                    entity_id=internal_id,
                                    data=cbor_bytes,
                                )
                            )

                        # Insert deps
                        for dep_id, dep_kind in deps:
                            dep_internal_id = fxtr_to_internal.get(dep_id)
                            if dep_internal_id is None:
                                # Look it up
                                result = await conn.execute(
                                    select(entity_table.c.id).where(
                                        entity_table.c.public_id == dep_id._raw_data_id
                                    )
                                )
                                dep_row = result.fetchone()
                                assert (
                                    dep_row is not None
                                ), f"Dep {dep_id} not found in entity table"
                                dep_internal_id = dep_row[0]
                                fxtr_to_internal[dep_id] = dep_internal_id

                            await conn.execute(
                                entity_dep_table.insert().values(
                                    requirer_id=internal_id,
                                    required_id=dep_internal_id,
                                    kind=dep_kind,
                                )
                            )

                    fxtr_to_internal[fxtr_id] = internal_id
                    # Add reservation
                    await self._session._add_entity_reservation(conn, internal_id)

                worklist.append(_TrackWork(internal_id=internal_id))
                logger.debug(
                    f"  Save({fxtr_id.summary()}): saved to entity tables as {internal_id}"
                )
                logger.debug(f"    -> enqueue Track({internal_id})")

            elif isinstance(item, _TrackWork):
                internal_id = item.internal_id

                async with self._session._async_engine_group.write_txn_engine.begin() as conn:
                    await self._add_entity_to_namespace_if_needed(conn, internal_id)
                    # Add to arena
                    await conn.execute(
                        arena_membership_table.insert().values(
                            arena_id=temp_arena_id,
                            entity_id=internal_id,
                        )
                    )
                    # Release reservation
                    await self._session._release_entity_reservation(conn, internal_id)

                logger.debug(f"  Track({internal_id}): added to namespace and arena")

        source.close()
        return temp_arena_id

    @override
    async def retrieve_pinned(self, key: str) -> tuple[FxtrID, SQLiteNamespaceArena]:
        """Get a pinned entity by key."""
        if not self._session.is_active():
            raise RuntimeError("Session not active")

        async with self._session._async_engine_group.write_txn_engine.begin() as conn:
            # Look up the pin by name
            result = await conn.execute(
                select(namespace_pin_table.c.entity_id, entity_table.c.public_id)
                .select_from(
                    namespace_pin_table.join(
                        entity_table,
                        namespace_pin_table.c.entity_id == entity_table.c.id,
                    )
                )
                .where(
                    (namespace_pin_table.c.namespace_id == self._namespace_internal_id)
                    & (namespace_pin_table.c.name == key)
                )
            )
            row = result.fetchone()
            if row is None:
                raise KeyError(f"No pin named {key!r} in namespace")
            entity_internal_id: int = row[0]
            entity_public_id: bytes = row[1]

            arena_uuid, arena_internal_id = await self._create_arena_with_entities(
                conn, [entity_internal_id]
            )

        fxtr_id = FxtrID(raw_data_id=bytes(entity_public_id))
        arena = await SQLiteNamespaceArena._from_existing_db_arena(
            self._session,
            self,
            arena_uuid,
            arena_internal_id,
            [entity_internal_id],
        )
        return fxtr_id, arena

    @override
    async def list_pins(self, prefix: str = "") -> dict[str, FxtrID]:
        """List pins in this namespace matching the given prefix.

        Args:
            prefix: Only return pins whose keys start with this prefix. Empty string (default)
                returns all pins.

        Returns:
            A dict mapping pin name to entity ID for all matching pins.
        """
        if not self._session.is_active():
            raise RuntimeError("Session not active")

        async with self._session._async_engine_group.read_txn_engine.begin() as conn:
            # Build the query
            query = (
                select(namespace_pin_table.c.name, entity_table.c.public_id)
                .select_from(
                    namespace_pin_table.join(
                        entity_table,
                        namespace_pin_table.c.entity_id == entity_table.c.id,
                    )
                )
                .where(namespace_pin_table.c.namespace_id == self._namespace_internal_id)
            )

            # Add prefix filter if specified
            if prefix:
                query = query.where(namespace_pin_table.c.name.startswith(prefix))

            result = await conn.execute(query)
            rows = result.fetchall()

        return {row[0]: FxtrID(raw_data_id=bytes(row[1])) for row in rows}

    @override
    async def try_lookup(self, node_id: FxtrID) -> SQLiteNamespaceArena | None:
        """Try to look up an entity in the namespace."""
        if not self._session.is_active():
            raise RuntimeError("Session not active")

        async with self._session._async_engine_group.write_txn_engine.begin() as conn:
            # Look up entity by public_id and check if it's in this namespace
            result = await conn.execute(
                select(entity_table.c.id)
                .select_from(
                    entity_table.join(
                        namespace_entity_ownership_table,
                        entity_table.c.id == namespace_entity_ownership_table.c.entity_id,
                    )
                )
                .where(
                    (entity_table.c.public_id == node_id._raw_data_id)
                    & (
                        namespace_entity_ownership_table.c.namespace_id
                        == self._namespace_internal_id
                    )
                )
            )
            row = result.fetchone()
            if row is None:
                return None
            entity_internal_id: int = row[0]

            arena_uuid, arena_internal_id = await self._create_arena_with_entities(
                conn, [entity_internal_id]
            )

        return await SQLiteNamespaceArena._from_existing_db_arena(
            self._session,
            self,
            arena_uuid,
            arena_internal_id,
            [entity_internal_id],
        )

    @override
    async def backrefs(self, node_id: FxtrID) -> list[EntityBackref | OtherBackref]:
        """Get backrefs to a particular entity."""
        if not self._session.is_active():
            raise RuntimeError("Session not active")

        backrefs: list[EntityBackref | OtherBackref] = []

        async with self._session._async_engine_group.read_txn_engine.begin() as conn:
            # First, look up the entity's internal ID and verify it's in this namespace
            result = await conn.execute(
                select(entity_table.c.id)
                .select_from(
                    entity_table.join(
                        namespace_entity_ownership_table,
                        entity_table.c.id == namespace_entity_ownership_table.c.entity_id,
                    )
                )
                .where(
                    (entity_table.c.public_id == node_id._raw_data_id)
                    & (
                        namespace_entity_ownership_table.c.namespace_id
                        == self._namespace_internal_id
                    )
                )
            )
            row = result.fetchone()
            if row is None:
                return []  # Entity not in namespace
            entity_internal_id: int = row[0]

            # 1. Entity backrefs: find entities in this namespace that reference this entity
            result = await conn.execute(
                select(
                    entity_table.c.public_id,
                    entity_dep_table.c.kind,
                )
                .select_from(
                    namespace_entity_backref_table.join(
                        entity_table,
                        namespace_entity_backref_table.c.requirer_id == entity_table.c.id,
                    ).join(
                        entity_dep_table,
                        (
                            entity_dep_table.c.requirer_id
                            == namespace_entity_backref_table.c.requirer_id
                        )
                        & (
                            entity_dep_table.c.required_id
                            == namespace_entity_backref_table.c.required_id
                        ),
                    )
                )
                .where(
                    (namespace_entity_backref_table.c.required_id == entity_internal_id)
                    & (namespace_entity_backref_table.c.namespace_id == self._namespace_internal_id)
                )
            )
            for requirer_public_id, dep_kind in result.fetchall():
                requirer_fxtr_id = FxtrID(raw_data_id=bytes(requirer_public_id))
                # Map DepKind to EntityBackref kind
                match dep_kind:
                    case DepKind.STRONG_REF:
                        backrefs.append(EntityBackref(referrer=requirer_fxtr_id, kind="strong_ref"))
                    case DepKind.WEAK_REF:
                        backrefs.append(EntityBackref(referrer=requirer_fxtr_id, kind="weak_ref"))
                    case DepKind.CTX_NODE:
                        backrefs.append(
                            EntityBackref(referrer=requirer_fxtr_id, kind="context_data")
                        )
                    case DepKind.UNWRAPS_TO:
                        backrefs.append(EntityBackref(referrer=requirer_fxtr_id, kind="unwrap"))
                    case DepKind.BACKSTEP:
                        # Skip backstep deps (like memory_arena does)
                        pass

            # 2. Pin backrefs: find pins that reference this entity
            result = await conn.execute(
                select(namespace_pin_table.c.name).where(
                    (namespace_pin_table.c.entity_id == entity_internal_id)
                    & (namespace_pin_table.c.namespace_id == self._namespace_internal_id)
                )
            )
            for (pin_name,) in result.fetchall():
                backrefs.append(OtherBackref(kind="pin", data={"name": pin_name}))

            # 3. Arena backrefs: find all arenas (across all sessions) containing this entity
            result = await conn.execute(
                select(
                    arena_table.c.public_id,
                    session_table.c.public_id,
                    session_table.c.is_garbage,
                )
                .select_from(
                    arena_membership_table.join(
                        arena_table,
                        arena_membership_table.c.arena_id == arena_table.c.id,
                    ).join(
                        session_table,
                        arena_table.c.session_id == session_table.c.id,
                    )
                )
                .where(
                    (arena_membership_table.c.entity_id == entity_internal_id)
                    & (arena_table.c.namespace_id == self._namespace_internal_id)
                )
            )
            for arena_public_id, session_public_id, is_garbage in result.fetchall():
                backrefs.append(
                    OtherBackref(
                        kind="sqlite_arena",
                        data={
                            "arena_id": uuid.UUID(bytes=bytes(arena_public_id)),
                            "session_id": uuid.UUID(bytes=bytes(session_public_id)),
                            "is_garbage": is_garbage,
                        },
                    )
                )

        return backrefs


class SQLiteNamespaceArena(CopyTargetArena):
    """A mostly-read-only arena backed by a SQLite database.

    Arenas maintain:
    - Reference to the session and namespace
    - Internal and external arena IDs
    - A Python set of tracked entity IDs (for synchronous is_tracked checks)
    - Preloaded sync-accessible data (unwrap targets, etc.)
    - An event that must be set before the arena is allowed to perform any more async operations.

    Internal invariants:
    - At all function boundaries, and at all async boundaries, the state of _db_tracked must match
      the actual tracked entities according to the database.
    - Whenever self._pending_tasks_completed_evt is set, as long as the arena is not closed:
        - _db_tracked must match _tracked
        - _arena_internal_id must be set, and _finalizer must be set
        - _precomp_* dicts may not be modified
    - For every node in _tracked:
        - All _precomp_* dicts must be populated so that all synchronous ops on those nodes can be
          evaluated synchronously without touching the database. This includes _precomp_value_count.
    - When the arena is closed, no further actions may be performed. This includes pending tasks
      that were scheduled before the arena was closed. It is allowed for errors to preemptively
      close the arena.
    - Pending tasks MUST NOT error; they should handle their errors internally and close the arena
      if necessary.
    """

    def __init__(
        self,
        session: SQLiteFxtrSession,
        namespace: SQLiteNamespaceBackend,
        arena_id: uuid.UUID,
    ):
        self._session = session
        self._namespace = namespace
        self._arena_id = arena_id
        self._arena_internal_id: int | None = None
        # _tracked represents the set of entities that are tracked from the perspective of the
        # Python program (uses FxtrID for the public API).
        self._tracked: set[FxtrID] = set()
        self._closed = False
        self._close_reason: Exception | None = None
        self._pending_tasks_completed_evt = anyio.Event()
        self._pending_tasks_counter = 0
        # Finalizer for cleanup
        self._finalizer: weakref.finalize | None = None
        # _db_tracked reflects what is actually tracked in the DB (uses internal IDs); it should
        # match _tracked once all of the pending async ops have completed.
        self._db_tracked: set[InternalEntityID] = set()
        # Preloaded data mirroring some of DB state. Must exist for all nodes in _tracked and all
        # nodes that are values in the below dicts (e.g. they are transitively closed). Also, if
        # a given item is in _fxtr_id_to_internal, we assume we have already populated
        # values for it in the other precomp_* dicts, and do not need to re-fetch them.
        self._fxtr_id_to_internal: dict[FxtrID, InternalEntityID] = {}
        self._internal_id_to_fxtr: dict[InternalEntityID, FxtrID] = {}
        self._precomp_unwraps_to: dict[InternalEntityID, InternalEntityID] = {}
        self._precomp_ctx_node: dict[InternalEntityID, InternalEntityID] = {}
        self._precomp_step_back_p_ctx: dict[InternalEntityID, InternalEntityID] = {}
        # _precomp_value_count[v] is the number of times v appears as the _value_ in any of the
        # above precomp_* dicts. It has the same existence invariant as the above.
        self._precomp_value_count: dict[InternalEntityID, int] = {}

    def is_active(self) -> bool:
        """Check if the arena is active."""
        return (not self._closed) and self._session.is_active()

    def ensure_active(self) -> None:
        """Ensure the arena is active."""
        if not self.is_active():
            if self._close_reason is not None:
                raise RuntimeError(
                    f"Arena is closed due to internal error!"
                ) from self._close_reason
            else:
                raise RuntimeError("Arena is closed")

    def _finish_init_with_internal_id(self, internal_id: int) -> None:
        """Complete arena initialization by setting internal ID and finalizer.

        Must be called exactly once during arena setup, after the DB arena is created. Does NOT yet
        trigger the ready event; caller must also set this event themselves once they are ready for
        the arena to begin processing pending tasks.
        """
        assert self._arena_internal_id is None, "Already initialized"
        self._arena_internal_id = internal_id
        self._finalizer = weakref.finalize(
            self,
            type(self)._schedule_dispose_for_finalization,
            self._session,
            internal_id,
        )

    @classmethod
    def _create_empty_in_session(
        cls,
        session: SQLiteFxtrSession,
        namespace: SQLiteNamespaceBackend,
        arena_id: uuid.UUID,
    ) -> Self:
        """Create a new empty arena in the session.

        Synchronous because it does not have any tracked nodes to begin with, so the only thing you
        can do with it is copy into it.

        Works by deferring the setting of _arena_internal_id.
        """
        arena = cls(session, namespace, arena_id)
        initial_ready_evt = arena._pending_tasks_completed_evt

        async def _init_arena_in_db():
            try:
                assert session._internal_sid is not None
                async with session._async_engine_group.write_txn_engine.begin() as conn:
                    result = await conn.execute(
                        arena_table.insert()
                        .values(
                            public_id=arena_id.bytes,
                            namespace_id=namespace._namespace_internal_id,
                            session_id=session._internal_sid,
                        )
                        .returning(arena_table.c.id)
                    )
                    row = result.fetchone()
                    assert row is not None
                    internal_arena_id = row[0]

                arena._finish_init_with_internal_id(internal_arena_id)
                logger.debug(
                    "Created empty arena %s (internal %s) in namespace %s",
                    arena_id,
                    internal_arena_id,
                    namespace._namespace_internal_id,
                )
            except Exception as e:
                arena._close_reason = e
                arena._closed = True
                logger.error("Failed to create arena %s in DB: %s", arena_id, e, exc_info=True)
            except BaseException as e:
                arena._closed = True
                logger.error("Failed to create arena %s in DB: %s", arena_id, e, exc_info=True)
                raise
            finally:
                initial_ready_evt.set()

        session._background_task_group.start_soon(_init_arena_in_db)
        return arena

    @classmethod
    async def _from_existing_db_arena(
        cls,
        session: SQLiteFxtrSession,
        namespace: SQLiteNamespaceBackend,
        arena_id: uuid.UUID,
        arena_internal_id: int,
        known_tracked: Iterable[InternalEntityID],
    ) -> Self:
        """Create a Python arena from an existing database arena.

        Assumes that all args are correctly populated.

        Args:
            session: The session where the arena is located.
            namespace: The namespace where the arena is located.
            arena_id: The ID of the arena.
            arena_internal_id: The internal ID of the arena.
            known_tracked: The set of internal entity IDs that are already tracked in the arena.

        Returns:
            A Python arena tracking the database arena, with necessary precomputed data loaded.
        """
        arena = cls(session, namespace, arena_id)
        initial_ready_evt = arena._pending_tasks_completed_evt
        arena._finish_init_with_internal_id(arena_internal_id)
        known_tracked_list = list(known_tracked)
        arena._db_tracked.update(known_tracked_list)
        await arena._load_into_tracked(known_tracked_list)
        initial_ready_evt.set()
        return arena

    def __repr__(self) -> str:
        return (
            f"<SQLiteNamespaceArena {self._arena_id} (ns={self._namespace._namespace_id},"
            f" internal={self._arena_internal_id}/{self._namespace._namespace_internal_id})>"
        )

    def _schedule_new_task(self, task: Callable[[], Awaitable[None]], name: str) -> None:
        """Schedule a new task to be run when all pending tasks have completed."""
        self.ensure_active()
        counter = self._pending_tasks_counter
        self._pending_tasks_counter += 1
        prev_evt = self._pending_tasks_completed_evt
        next_evt = anyio.Event()
        self._pending_tasks_completed_evt = next_evt

        async def _thunk():
            # The try-catch structure here guarantees that:
            # - No matter what happens, next_evt will be set.
            # - If the task fails with any kind of exception, the arena will be closed, with a
            #   timeout if we are closing due to cancellation.
            # Note: Tasks are responsible for checking is_active() themselves if they need to skip
            # when the arena is closed.
            await prev_evt.wait()
            try:
                logger.debug(
                    "Arena %s running pending task %s: %s", self._arena_internal_id, counter, name
                )
                await task()
                logger.debug(
                    "Arena %s completed task %s: %s", self._arena_internal_id, counter, name
                )
            except Exception as e:
                if not self._closed:
                    self._close_reason = e
                logger.error(
                    "Arena %s failed in task %s: %s",
                    self._arena_internal_id,
                    counter,
                    name,
                    exc_info=True,
                )
                await self._close_for_exception()
            except BaseException:
                with anyio.fail_after(10.0, shield=True):
                    await self._close_for_exception()
                raise
            finally:
                next_evt.set()

        self._session._background_task_group.start_soon(_thunk)
        logger.debug("Arena %s enqueued task %s: %s", self._arena_internal_id, counter, name)

    @contextlib.asynccontextmanager
    async def _running_in_queue_slot(self, name: str) -> AsyncIterator[None]:
        """Run a code block in a slot of the pending tasks queue.

        Ensures that, when the code block enters, any pending tasks that were scheduled before this
        have completed, and furthermore, any pending tasks that are scheduled during the code
        block do not begin until the code block exits.

        In particular: _db_tracked will match _tracked at the beginning of the block, and
        _db_tracked MUST match _tracked at the end of the block, but it is OK to temporarily make
        them diverge during the block.

        All invariants must still be maintained even if the code block raises an exception!

        Not reentrant safe!
        """
        self.ensure_active()
        counter = self._pending_tasks_counter
        self._pending_tasks_counter += 1
        prev_evt = self._pending_tasks_completed_evt
        next_evt = anyio.Event()
        self._pending_tasks_completed_evt = next_evt

        # See comment in _schedule_new_task for explanation of the try-catch structure.
        # Note: Callers are responsible for checking is_active() themselves if they need to skip
        # when the arena is closed.
        try:
            logger.debug(
                "Arena %s reserved queue slot %s for action %s",
                self._arena_internal_id,
                counter,
                name,
            )
            await prev_evt.wait()
            try:
                logger.debug(
                    "Arena %s running action %s in queue slot %s",
                    self._arena_internal_id,
                    name,
                    counter,
                )
                yield
                logger.debug(
                    "Arena %s released queue slot %s for action %s",
                    self._arena_internal_id,
                    counter,
                    name,
                )
            except Exception as e:
                if not self._closed:
                    self._close_reason = e
                logger.error(
                    "Arena %s failed in action %s in queue slot %s",
                    self._arena_internal_id,
                    name,
                    counter,
                    exc_info=True,
                )
                await self._close_for_exception()
                raise  # Let the user of the context manager deal with it
            except BaseException:
                with anyio.fail_after(10.0, shield=True):
                    await self._close_for_exception()
                raise
        finally:
            next_evt.set()

    @contextlib.contextmanager
    def _delay_pending_tasks(self, msg: str, timeout: float = 10.0) -> Generator[None, None, None]:
        evt = anyio.Event()

        async def _blocking_thunk() -> None:
            try:
                with anyio.fail_after(timeout):
                    await evt.wait()
            except TimeoutError:
                raise RuntimeError(
                    f"Timeout waiting for block_pending_tasks_until({msg}); deadlock?"
                )

        self._schedule_new_task(_blocking_thunk, f"block_pending_tasks_until({msg})")
        try:
            yield
        finally:
            evt.set()

    async def _wait_for_all_pending_tasks(self) -> None:
        """Wait until all currently-queued tasks have completed."""
        last_evt = self._pending_tasks_completed_evt
        await last_evt.wait()

    async def _load_into_tracked(self, internal_ids: Iterable[InternalEntityID]) -> None:
        """Add db-tracked nodes into _tracked and precompute all necessary preloaded data.

        Assumes that the internal_ids are already in _db_tracked, but not in _tracked, and preloaded
        data may be missing. As such, MUST be called inside _running_in_queue_slot, or before
        _pending_tasks_completed_evt is set for the first time.
        """
        assert self.is_active()
        internal_ids_list = list(internal_ids)
        # Filter to only nodes we haven't already loaded precomp data for
        frontier: set[InternalEntityID] = {
            iid for iid in internal_ids_list if iid not in self._internal_id_to_fxtr
        }
        if not frontier:
            # All nodes already have precomp data loaded, just update _tracked
            self._tracked.update(self._internal_id_to_fxtr[iid] for iid in internal_ids_list)
            return

        # The dep kinds that need to be precomputed for sync operations
        sync_dep_kinds = (DepKind.CTX_NODE, DepKind.BACKSTEP, DepKind.UNWRAPS_TO)

        async with self._session._async_engine_group.read_txn_engine.begin() as conn:
            while frontier:
                # Step 1: Fetch FxtrIDs for internal IDs we haven't seen yet
                result = await conn.execute(
                    select(entity_table.c.id, entity_table.c.public_id).where(
                        entity_table.c.id.in_(frontier)
                    )
                )
                for row in result.fetchall():
                    internal_id, pub_id = row
                    fxtr_id = FxtrID(raw_data_id=bytes(pub_id))
                    self._fxtr_id_to_internal[fxtr_id] = internal_id
                    self._internal_id_to_fxtr[internal_id] = fxtr_id
                    # Possible we already have a value count if we added this node due to
                    # encountering a dependency in an earlier iteration of this loop.
                    if internal_id not in self._precomp_value_count:
                        self._precomp_value_count[internal_id] = 0

                # Step 2: Fetch deps for the frontier
                result = await conn.execute(
                    select(
                        entity_dep_table.c.requirer_id,
                        entity_dep_table.c.required_id,
                        entity_dep_table.c.kind,
                    ).where(
                        entity_dep_table.c.requirer_id.in_(frontier)
                        & entity_dep_table.c.kind.in_(sync_dep_kinds)
                    )
                )
                dep_rows = result.fetchall()

                # Clear frontier, will be repopulated with new internal IDs we discover
                frontier = set()

                for requirer_id, required_id, kind in dep_rows:
                    # Store the dep in precomp
                    if kind == DepKind.UNWRAPS_TO:
                        self._precomp_unwraps_to[requirer_id] = required_id
                    elif kind == DepKind.CTX_NODE:
                        self._precomp_ctx_node[requirer_id] = required_id
                    elif kind == DepKind.BACKSTEP:
                        self._precomp_step_back_p_ctx[requirer_id] = required_id
                    else:
                        raise ValueError(f"Unknown dep kind: {kind}")

                    # Update value count. Note that we set the _precomp_value_count for new nodes
                    # when we first discover them; the other precomp dicts get filled out in the
                    # next iteration once added to the frontier.
                    self._precomp_value_count[required_id] = (
                        self._precomp_value_count.get(required_id, 0) + 1
                    )

                    # If we haven't seen this required_id, add to frontier for next iteration
                    if required_id not in self._internal_id_to_fxtr:
                        frontier.add(required_id)

        # Now that precomp data is up to date, add the FxtrIDs to _tracked
        self._tracked.update(self._internal_id_to_fxtr[iid] for iid in internal_ids_list)

    @override
    def is_tracked(self, node_id: FxtrID) -> bool:
        """Check if a node is tracked by this arena."""
        self.ensure_active()
        return node_id in self._tracked

    def _track_from_precomputed(self, internal_id: InternalEntityID) -> FxtrID:
        """Track a precomputed entity in the arena.

        Schedules an update to the database state, and also immediately adds the entity to
        _tracked. Assumes the entity already has precomputed data.
        """
        assert self.is_active()
        assert internal_id in self._internal_id_to_fxtr
        fxtr_id = self._internal_id_to_fxtr[internal_id]

        async def _do_track_precomputed() -> None:
            if not self.is_active():
                logger.debug(
                    "Arena %s is closed, skipping track_precomputed for %s",
                    self._arena_internal_id,
                    fxtr_id,
                )
                return
            assert self._arena_internal_id is not None
            async with self._session._async_engine_group.write_txn_engine.begin() as conn:
                # For safety, make sure the entity is in the namespace in the DB
                result = await conn.execute(
                    select(namespace_entity_ownership_table.c.entity_id).where(
                        (
                            namespace_entity_ownership_table.c.namespace_id
                            == self._namespace._namespace_internal_id
                        )
                        & (namespace_entity_ownership_table.c.entity_id == internal_id)
                    )
                )
                if result.fetchone() is None:
                    raise RuntimeError(
                        f"Internal error: Entity {internal_id} is not in namespace"
                        f" {self._namespace._namespace_internal_id}"
                    )
                # Add the entity to the arena
                await conn.execute(
                    arena_membership_table.insert().values(
                        arena_id=self._arena_internal_id, entity_id=internal_id
                    )
                )
            self._db_tracked.add(internal_id)

        # Schedule the update. Once it is done, the _db_tracked will be updated, and will match the
        # database. We add to _tracked now so that the invariant will be restored at that point.
        self._schedule_new_task(_do_track_precomputed, f"track_precomputed({fxtr_id})")
        self._tracked.add(fxtr_id)
        return fxtr_id

    @override
    def unwrap_provenance(self, node_id: FxtrID) -> FxtrID | None:
        """Unwrap a provenance node."""
        if not self.is_tracked(node_id):
            raise ValueError(f"Node {node_id} is not tracked by this arena")
        internal_id = self._fxtr_id_to_internal[node_id]
        next_internal_id = self._precomp_unwraps_to.get(internal_id)
        if next_internal_id is None:
            return None
        return self._track_from_precomputed(next_internal_id)

    @override
    def c_node_to_context_data(self, c_node_id: FxtrID) -> FxtrID:
        """Unstructure a context provenance node."""
        if not self.is_tracked(c_node_id):
            raise ValueError(f"Node {c_node_id} is not tracked by this arena")
        if c_node_id.get_tag() != "c":
            raise ValueError(f"Node {c_node_id} is not a c-node")
        internal_id = self._fxtr_id_to_internal[c_node_id]
        next_internal_id = self._precomp_ctx_node.get(internal_id)
        assert next_internal_id is not None
        return self._track_from_precomputed(next_internal_id)

    @override
    def p_node_to_pathroot(self, p_node_id: FxtrID) -> FxtrID:
        """Unstructure a pathroot provenance node."""
        if not self.is_tracked(p_node_id):
            raise ValueError(f"Node {p_node_id} is not tracked by this arena")
        if p_node_id.get_tag() != "p":
            raise ValueError(f"Node {p_node_id} is not a p-node")
        internal_id = self._fxtr_id_to_internal[p_node_id]
        next_internal_id = self._precomp_ctx_node.get(internal_id)
        assert next_internal_id is not None
        return self._track_from_precomputed(next_internal_id)

    @override
    def step_back_p_ctx(self, p_node_id: FxtrID) -> FxtrID | None:
        """Step back a pathroot provenance node."""
        if not self.is_tracked(p_node_id):
            raise ValueError(f"Node {p_node_id} is not tracked by this arena")
        if p_node_id.get_tag() != "p":
            raise ValueError(f"Node {p_node_id} is not a p-node")
        internal_id = self._fxtr_id_to_internal[p_node_id]
        next_internal_id = self._precomp_step_back_p_ctx.get(internal_id)
        if next_internal_id is None:
            return None
        return self._track_from_precomputed(next_internal_id)

    @override
    def abandon(self, node_id: FxtrID):
        """Abandon a node from this arena."""
        if not self.is_tracked(node_id):
            raise ValueError(f"Node {node_id} is not tracked by this arena")

        # Remove from Python liveset immediately
        self._tracked.remove(node_id)

        # Schedule database cleanup and cache cleanup. (We delay precomputed cache cleanup to make
        # sure the cache is in a clean state / not being modified by some async op in the queue.)
        internal_id = self._fxtr_id_to_internal[node_id]

        async def _do_abandon():
            if not self.is_active():
                logger.debug(
                    "Arena %s is closed, skipping abandon for %s",
                    self._arena_internal_id,
                    node_id,
                )
                return
            # Database cleanup: Remove from the arena, then rebind to garbage if needed
            assert self._arena_internal_id is not None
            async with self._session._async_engine_group.write_txn_engine.begin() as conn:
                await conn.execute(
                    arena_membership_table.delete().where(
                        (arena_membership_table.c.arena_id == self._arena_internal_id)
                        & (arena_membership_table.c.entity_id == internal_id)
                    )
                )
                await self._session._rebind_to_garbage_if_needed(
                    conn, self._namespace._namespace_internal_id, internal_id
                )
            self._db_tracked.discard(internal_id)

            # Precomp cache cleanup: Remove cached data for nodes that are no longer needed.
            # A node can be removed if it's not in _tracked and not referenced by any other
            # node's precomp data (i.e. _precomp_value_count is 0).
            # Note that it is _possible_ this node's precomp data was already removed by some other
            # abandon call prior to this one triggering, so we also have to make sure this node
            # was not already cleaned up.
            if internal_id not in self._internal_id_to_fxtr:
                return

            to_remove: deque[InternalEntityID] = deque()
            if node_id not in self._tracked and self._precomp_value_count[internal_id] == 0:
                to_remove.append(internal_id)

            while to_remove:
                iid = to_remove.popleft()
                # Skip if this node became tracked again or is still referenced
                fxtr_id = self._internal_id_to_fxtr[iid]
                assert fxtr_id not in self._tracked
                assert self._precomp_value_count[iid] == 0

                # Remove from precomp dicts and decrement value counts for referenced nodes
                for precomp_dict in (
                    self._precomp_unwraps_to,
                    self._precomp_ctx_node,
                    self._precomp_step_back_p_ctx,
                ):
                    referenced_id = precomp_dict.pop(iid, None)
                    if referenced_id is not None:
                        new_count = self._precomp_value_count[referenced_id] - 1
                        assert new_count >= 0
                        self._precomp_value_count[referenced_id] = new_count
                        if new_count == 0:
                            # Check if this referenced node can now be removed too
                            ref_fxtr = self._internal_id_to_fxtr[referenced_id]
                            if ref_fxtr not in self._tracked:
                                to_remove.append(referenced_id)

                # Remove from ID mappings
                self._fxtr_id_to_internal.pop(fxtr_id)
                self._internal_id_to_fxtr.pop(iid)
                self._precomp_value_count.pop(iid)
                logger.debug(
                    "Cleaned up precomputed state for %s in arena %s",
                    fxtr_id,
                    self._arena_internal_id,
                )

        self._schedule_new_task(_do_abandon, f"abandon({node_id})")

    @override
    def copy_to_new_arena(self, node_ids: Collection[FxtrID]) -> SQLiteNamespaceArena:
        """Copy a subset of nodes to a new arena.

        Creates a new arena synchronously with copied precomputed data, while deferring
        DB work to this arena's task queue. The returned arena will be ready to use
        immediately for sync operations, but async operations will block until the DB
        work completes.
        """
        self.ensure_active()

        # 1. Validate all requested nodes are tracked
        node_ids_list = list(node_ids)
        for node_id in node_ids_list:
            if node_id not in self._tracked:
                raise ValueError(f"Node {node_id} is not tracked by this arena")

        # 2. Create new arena object via constructor (not in DB yet)
        new_arena = SQLiteNamespaceArena(
            self._session,
            self._namespace,
            uuid.uuid4(),
        )
        new_arena_ready_evt = new_arena._pending_tasks_completed_evt

        # 3. Compute transitive closure of precomp data needed
        to_copy: set[InternalEntityID] = set()
        frontier: deque[InternalEntityID] = deque()
        for node_id in node_ids_list:
            internal_id = self._fxtr_id_to_internal[node_id]
            if internal_id not in to_copy:
                to_copy.add(internal_id)
                frontier.append(internal_id)
        while frontier:
            iid = frontier.popleft()
            for precomp_dict in (
                self._precomp_unwraps_to,
                self._precomp_ctx_node,
                self._precomp_step_back_p_ctx,
            ):
                target = precomp_dict.get(iid)
                if target is not None and target not in to_copy:
                    to_copy.add(target)
                    frontier.append(target)

        # 4. Copy precomp data to new arena
        for iid in to_copy:
            fxtr_id = self._internal_id_to_fxtr[iid]
            new_arena._fxtr_id_to_internal[fxtr_id] = iid
            new_arena._internal_id_to_fxtr[iid] = fxtr_id
            new_arena._precomp_value_count[iid] = 0
        for iid in to_copy:
            for src_dict, tgt_dict in [
                (self._precomp_unwraps_to, new_arena._precomp_unwraps_to),
                (self._precomp_ctx_node, new_arena._precomp_ctx_node),
                (self._precomp_step_back_p_ctx, new_arena._precomp_step_back_p_ctx),
            ]:
                if iid in src_dict:
                    value_iid = src_dict[iid]
                    tgt_dict[iid] = value_iid
                    new_arena._precomp_value_count[value_iid] += 1
        new_arena._tracked = set(node_ids_list)

        # 5. Schedule DB work on THIS arena's queue
        async def _do_copy_to_new_arena():
            if not self.is_active():
                logger.debug(
                    "Arena %s is closed, skipping copy_to_new_arena for %s",
                    self._arena_internal_id,
                    new_arena._arena_internal_id,
                )
                new_arena._closed = True
                new_arena._close_reason = RuntimeError("Source arena closed during copy")
                new_arena_ready_evt.set()
                return

            try:
                async with self._session._async_engine_group.write_txn_engine.begin() as conn:
                    result = await conn.execute(
                        arena_table.insert()
                        .values(
                            public_id=new_arena._arena_id.bytes,
                            namespace_id=self._namespace._namespace_internal_id,
                            session_id=self._session._internal_sid,
                        )
                        .returning(arena_table.c.id)
                    )
                    row = result.fetchone()
                    assert row is not None
                    new_internal_id = row[0]

                    # Batch insert all arena memberships
                    internal_ids = [self._fxtr_id_to_internal[nid] for nid in node_ids_list]
                    await conn.execute(
                        arena_membership_table.insert(),
                        [{"arena_id": new_internal_id, "entity_id": iid} for iid in internal_ids],
                    )

                new_arena._finish_init_with_internal_id(new_internal_id)
                new_arena._db_tracked = set(internal_ids)
                logger.debug(
                    "Copied %d nodes from arena %s to new arena %s (internal %s)",
                    len(node_ids_list),
                    self._arena_internal_id,
                    new_arena._arena_internal_id,
                    new_internal_id,
                )
            except BaseException as exc:
                new_arena._closed = True
                new_arena._close_reason = RuntimeError(
                    "Error while initializing new arena from previous arena"
                )
                if isinstance(exc, Exception):
                    logger.error(
                        "Failed to copy nodes from arena %s to new arena %s",
                        self._arena_internal_id,
                        new_arena._arena_internal_id,
                        exc_info=True,
                    )
                raise
            finally:
                new_arena_ready_evt.set()

        self._schedule_new_task(
            _do_copy_to_new_arena,
            f"copy_to_new_arena({new_arena._arena_internal_id}, {len(node_ids_list)} nodes)",
        )
        return new_arena

    @override
    async def load(self, node_id: FxtrID) -> FxtrCbor:
        """Load an entity from the SQLite database.

        For d/n nodes, fetches and returns the stored CBOR data.
        For p/u nodes, loads the underlying data and creates wrapped child entities,
        adding them to entity tables, namespace, and arena as needed.
        """
        self.ensure_active()
        if not self.is_tracked(node_id):
            raise ValueError(
                f"Node {node_id} is not tracked in the fxtr arena! Only tracked nodes can be"
                " loaded."
            )

        tag = node_id.get_tag()
        if tag in (".", "!", "c"):
            raise ValueError(f"Cannot load contentless node type: {tag}")

        internal_id = self._fxtr_id_to_internal[node_id]

        async with self._running_in_queue_slot(f"load({node_id})"):
            self.ensure_active()  # in case we got closed while waiting for the queue slot
            async with self._session._async_engine_group.write_txn_engine.begin() as conn:
                # Load data (creates child entities in entity tables for p/u nodes)
                data = await self._session._load_cbor_and_save_children(
                    conn, node_id, internal_id, self._namespace._namespace_internal_id
                )

                # Extract child IDs from returned data
                child_ids: list[FxtrID] = []
                visit_ids(data, lambda tid: child_ids.append(tid.inner_id))

                # Add children to namespace and arena
                child_internal_ids: list[int] = []
                for child_id in child_ids:
                    # Look up internal ID
                    result = await conn.execute(
                        select(entity_table.c.id).where(
                            entity_table.c.public_id == child_id._raw_data_id
                        )
                    )
                    row = result.fetchone()
                    assert row is not None, f"Child {child_id} not found in entity table"
                    child_internal = row[0]
                    child_internal_ids.append(child_internal)

                    # Add to namespace if needed
                    await self._namespace._add_entity_to_namespace_if_needed(conn, child_internal)

                    # Add to arena membership
                    # Use INSERT ... ON CONFLICT to handle already-existing memberships
                    stmt = sqlite_insert(arena_membership_table).values(
                        arena_id=self._arena_internal_id,
                        entity_id=child_internal,
                    )
                    stmt = stmt.on_conflict_do_nothing(index_elements=["arena_id", "entity_id"])
                    await conn.execute(stmt)

            # Update Python tracking state
            await self._load_into_tracked(child_internal_ids)

        return data

    @override
    async def copy_from_arena(self, entity_id: FxtrID, source: ReadableArena):
        """Copy an entity from another arena into this arena."""
        self.ensure_active()

        # Check if entity is already tracked (early return if so)
        if self.is_tracked(entity_id):
            logger.debug(
                "Entity %s already tracked in arena %s, skipping copy",
                entity_id,
                self._arena_internal_id,
            )
            return

        # Use a queue slot to ensure proper synchronization
        async with self._running_in_queue_slot(f"copy_from_arena({entity_id})"):
            # Use the namespace's method for copying
            # This will copy the entity from the source arena into a temporary arena in the namespace
            temp_arena_internal_id = (
                await self._namespace._build_tmp_arena_copying_from_external_arena(
                    entity_id, source.copy_to_new_arena([entity_id])
                )
            )

            try:
                # Look up the internal entity ID and verify it's in the namespace
                async with self._session._async_engine_group.write_txn_engine.begin() as conn:
                    result = await conn.execute(
                        select(entity_table.c.id)
                        .select_from(
                            entity_table.join(
                                namespace_entity_ownership_table,
                                entity_table.c.id == namespace_entity_ownership_table.c.entity_id,
                            )
                        )
                        .where(
                            (entity_table.c.public_id == entity_id._raw_data_id)
                            & (
                                namespace_entity_ownership_table.c.namespace_id
                                == self._namespace._namespace_internal_id
                            )
                        )
                    )
                    row = result.fetchone()
                    if row is None:
                        raise RuntimeError(f"Entity {entity_id} not found in namespace after copy")
                    entity_internal_id: int = row[0]

                    # Add the entity to this arena's tracked set in the database
                    assert self._arena_internal_id is not None
                    await conn.execute(
                        arena_membership_table.insert().values(
                            arena_id=self._arena_internal_id, entity_id=entity_internal_id
                        )
                    )

                # Update _db_tracked to reflect the new entity
                self._db_tracked.add(entity_internal_id)

                # Use _load_into_tracked to populate precomputed data and add to _tracked
                await self._load_into_tracked([entity_internal_id])

                logger.debug(
                    "Copied entity %s from external arena into arena %s",
                    entity_id,
                    self._arena_internal_id,
                )
            finally:
                # Abandon the temporary arena - entity stays in namespace because it's now in this arena
                await self._session._dispose_arena_in_db(temp_arena_internal_id)

    @classmethod
    def _schedule_dispose_for_finalization(
        cls,
        session: SQLiteFxtrSession,
        arena_internal_id: int,
    ):
        """Schedule disposal of the arena for finalization."""

        async def _do_dispose():
            logger.debug("Arena %s being cleaned up by finalizer!", arena_internal_id)
            await session._dispose_arena_in_db(arena_internal_id)

        if session.is_active():
            session._background_task_group.start_soon(_do_dispose)

    async def _close_for_exception(self):
        """Close and dispose of the arena immediately."""
        self._closed = True
        if self._finalizer is not None:
            logger.debug("Disposing of arena %s in the DB due to error", self._arena_internal_id)
            assert self._arena_internal_id is not None
            await self._session._dispose_arena_in_db(self._arena_internal_id)
            self._finalizer.detach()
            self._finalizer = None

    @override
    def close(self):
        """Close the arena, and schedule disposal of its resources."""
        if self._closed:
            return

        self._closed = True
        self._tracked.clear()
        if self._finalizer is not None:
            logger.debug(
                "Triggering finalizer of arena %s due to manual close", self._arena_internal_id
            )
            self._finalizer()
            self._finalizer = None
