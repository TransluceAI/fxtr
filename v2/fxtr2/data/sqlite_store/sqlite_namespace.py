"""Entity store backed by a SQLite database using sqlite3 for high performance."""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import inspect
import logging
import os
import sqlite3
import uuid
import weakref
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Collection, Iterable, override

import anyio
import anyio.abc
import anyio.lowlevel
import anyio.to_thread

from fxtr2.data import entity_core
from fxtr2.data.abstract_arena import (
    DeepFetchable,
    DepKind,
    FlatEntitySource,
    FxtrEntityFlatData,
    LeaseGrantMismatchError,
    ReadableArena,
    validate_lease_grant,
)
from fxtr2.data.entity_core import FxtrCbor, FxtrID
from fxtr2.data.file_util.lock import (
    LockNotAcquiredError,
    async_file_lock_acquire,
    sync_file_lock_acquire_nonblocking,
)
from fxtr2.data.sqlite_store import entity_blob, sqlite_index_ops, sqlite_ops
from fxtr2.data.sqlite_store.sqlite_util import ConnectionWorkerPool, ConnectionWorkerPoolGroup
from fxtr2.data.store_indexing.index_computer import IndexComputer
from fxtr2.data.store_indexing.indexing_types import (
    BindingConstraint,
    IndexablePinningBackend,
    IndexerColumnType,
    IndexerColumnValue,
    IndexerConstraintByID,
    ParsedView,
)
from fxtr2.data.store_indexing.query_gen import (
    ResolvedConstraint,
    ResolvedView,
    ResolvedViewColumn,
    generate_direct_sql_query,
    generate_iter_matches_query,
)
from fxtr2.data.store_indexing.sql_expr import parse_condition
from fxtr2.leases.lease_types import FreshLifetime, Grant, Lease, LifetimeBrand, LifetimeToken
from fxtr2.leases.local_lease import UnconditionalLocalLifetime
from fxtr2.util import anyio_util

logger = logging.getLogger(__name__)


def _with_validated_arena_lease(fn):
    """Validate ``lease`` (first argument after ``self``) then hold it for the whole call.

    Used for ``SQLiteNamespaceArena`` methods only (arenas are lease-gated). Namespace
    backends do not use this wrapper.

    ``validate_lease_grant`` runs on the caller's lease; then ``lease.take()`` + the lease
    context manager keeps a clone active until the wrapped function returns (or the async
    generator finishes), matching structured lease consumption.
    """

    if inspect.isasyncgenfunction(fn):

        @functools.wraps(fn)
        async def _async_gen_wrapper(self, lease: Lease[Any], *args, **kwargs):
            validate_lease_grant(self._session.grant, lease)
            with lease.take() as held:
                async for item in fn(self, held, *args, **kwargs):
                    yield item

        return _async_gen_wrapper

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def _async_wrapper(self, lease: Lease[Any], *args, **kwargs):
            validate_lease_grant(self._session.grant, lease)
            with lease.take() as held:
                return await fn(self, held, *args, **kwargs)

        return _async_wrapper

    @functools.wraps(fn)
    def _sync_wrapper(self, lease: Lease[Any], *args, **kwargs):
        validate_lease_grant(self._session.grant, lease)
        with lease.take() as held:
            return fn(self, held, *args, **kwargs)

    return _sync_wrapper


def _column_value_to_sqlite(v: IndexerColumnValue) -> int | float | str | bytes:
    """Convert an IndexerColumnValue to a SQLite-compatible value."""
    if isinstance(v, entity_core.FxtrID):
        return v._raw_data_id
    return v


def _sqlite_to_column_value(
    raw: int | float | str | bytes,
    col_type: IndexerColumnType,
) -> IndexerColumnValue:
    """Convert a raw SQLite value back to the correct IndexerColumnValue type."""
    if col_type is IndexerColumnType.BOOL:
        return bool(raw)
    if col_type is IndexerColumnType.FXTR_ID:
        assert isinstance(raw, bytes)
        return FxtrID(raw_data_id=raw)
    return raw


# =============================================================================
# Bulk copy worklist items and data structures
# =============================================================================


@dataclasses.dataclass
class _BulkDiscoverWork:
    """Work item to discover an entity and determine what needs to be done."""

    entity_id: FxtrID


@dataclasses.dataclass
class _BulkEnqueueSaveWork:
    """Work item to add a SaveItem to the save queue (pushed before dep discovery)."""

    save_item: sqlite_ops.SaveItem


type InternalEntityID = int


class _SharedSqlitePoolGroupManager:
    """Manages singleton pool groups per database path.

    Ensures at most one ConnectionWorkerPoolGroup (and thus one SegmentManager)
    exists per (database path, read_only) pair. All code should use
    GLOBAL_POOL_GROUP_MANAGER.get_pool_group() instead of creating pool groups directly.
    """

    def __init__(self):
        self._pool_group_evts: dict[tuple[str, bool], anyio.Event] = {}
        self._pool_groups: dict[tuple[str, bool], ConnectionWorkerPoolGroup] = {}
        self._refcounts: dict[tuple[str, bool], int] = {}

    def is_path_held(self, db_path: str) -> bool:
        """Check if any pool group is currently held for this database path."""
        return (db_path, False) in self._pool_groups or (db_path, True) in self._pool_groups

    @contextlib.asynccontextmanager
    async def get_pool_group(
        self, db_path: str, *, read_only: bool = False
    ) -> AsyncIterator[ConnectionWorkerPoolGroup]:
        """Get a pool group for a database path.

        This context manager will construct a pool group for each database path it receives, but
        will then re-use the same pool group if there are multiple concurrent requests for a pool
        group for the same database path and mode. This makes it possible for multiple sqlite
        sessions to multiplex over a single set of database connections, avoiding lock contention
        between multiple namespaces in the same process.

        This ensures only one SegmentManager is open per database file per mode, which is not
        _strictly_ necessary for correctness, but should improve performance and avoid duplicate
        file handles.

        Args:
            db_path: Path to the SQLite database file.
            read_only: If True, open in read-only mode (no write pool, mode=ro connections).
        """
        key = (db_path, read_only)
        self._refcounts[key] = self._refcounts.get(key, 0) + 1
        try:

            if key in self._pool_group_evts:
                await self._pool_group_evts[key].wait()
                logger.info(
                    "Re-using existing connection pool for %s (read_only=%s)", db_path, read_only
                )
            else:
                assert key not in self._pool_groups
                evt = anyio.Event()
                self._pool_group_evts[key] = evt
                if logger.isEnabledFor(logging.INFO):
                    logger.info(
                        "Setting up a new connection pool for %s (read_only=%s)", db_path, read_only
                    )
                if read_only:
                    self._pool_groups[key] = await sqlite_ops.async_setup_readonly_pool_group(
                        db_path
                    )
                else:
                    self._pool_groups[key] = (
                        await sqlite_ops.async_setup_database_and_create_worker_pool_group(db_path)
                    )
                if logger.isEnabledFor(logging.INFO):
                    logger.info(
                        "Connection pool for %s (read_only=%s) is ready", db_path, read_only
                    )
                evt.set()

            try:
                grp = self._pool_groups[key]
            except KeyError as exc:
                raise RuntimeError(
                    f"Connection pool for {db_path} not found, likely failed during setup in a"
                    " different task"
                ) from exc
            yield grp
        finally:
            self._refcounts[key] -= 1
            if self._refcounts[key] == 0:
                grp = self._pool_groups.pop(key, None)
                try:
                    if grp is not None:
                        if logger.isEnabledFor(logging.INFO):
                            logger.info(
                                "Closing connection pool for %s (read_only=%s)", db_path, read_only
                            )
                        await grp.aclose()
                finally:
                    # Registry cleanup is unconditional: leaving the event
                    # behind after popping the group would poison this key
                    # for the rest of the process (a later opener would
                    # take the "re-use existing pool" branch and KeyError).
                    evt = self._pool_group_evts.pop(key, None)
                    if evt is not None:
                        evt.set()  # <- Make sure anybody waiting on the event advances (they'll get an error)
                    del self._refcounts[key]


GLOBAL_POOL_GROUP_MANAGER = _SharedSqlitePoolGroupManager()


class SQLiteFxtrSession[B: FreshLifetime[Any]]:
    """A session for a fxtr sqlite db.

    Parameterized on a lifetime brand ``B``. The session owns an
    ``UnconditionalLocalLifetime`` whose token is the session's brand; all
    namespaces, backends, and arenas produced by this session share that
    brand. Callers obtain leases via ``session.root_lease.clone()`` (stable
    reference; clone to hand out).
    """

    # Default segment rotation threshold (512 MB)
    DEFAULT_MAX_SEGMENT_SIZE: int = 512 * 1024 * 1024

    def __init__(
        self,
        db_path: str,
        pool_group: ConnectionWorkerPoolGroup,
        background_task_group: anyio.abc.TaskGroup,
        _should_start_garbage_worker: bool = True,
        max_segment_size: int | None = None,
        read_only: bool = False,
    ):
        """Create a new session.

        Args:
            db_path: Path to the SQLite database file, or a shared-cache memory URI.
            pool_group: Worker pool group for database operations.
            background_task_group: A task group that is guaranteed to be running for the full
                lifetime of the session.
            _should_start_garbage_worker: If True, a garbage worker will be spawned to clean up garbage.
            max_segment_size: Soft limit in bytes for segment file rotation. When the active
                segment exceeds this size after a commit, it is sealed and a new segment is
                created. Defaults to 512 MB.
            read_only: If True, open in read-only mode. No session is tracked in the DB,
                no session files are created, no GC runs, and write operations raise errors.
        """
        self._db_path = db_path
        self._pool_group = pool_group
        self._max_segment_size = max_segment_size or self.DEFAULT_MAX_SEGMENT_SIZE
        self._background_task_group = background_task_group
        self._read_only = read_only
        self._evt_loop_thread_token = anyio.lowlevel.current_token()
        self._external_sid: uuid.UUID = uuid.uuid4()
        # If internal ID is not set, session has not been started yet
        # If internal ID is set and finalizer is live, session is active
        # If internal ID is set and finalizer is None, session is garbage
        self._internal_sid: int | None = None
        # Limit concurrent pin operations to avoid duplicate discovery/store work
        # when many callers pin entities with overlapping subgraphs simultaneously.
        self._pin_limiter = anyio.CapacityLimiter(5)
        # IndexComputer with persistent scope — managed inside _lifecycle_handler.
        # None until the lifecycle handler enters it.
        self._index_computer: IndexComputer | None = None
        self._started: bool = False
        self._finalizer: weakref.finalize | None = None
        self._should_close_evt: anyio.Event = anyio.Event()
        self._closed_evt: anyio.Event = anyio.Event()
        # Set to True once arenas should stop operating. This is set later than
        # _should_close_evt to give the IndexComputer time to clean up its token
        # (which abandons nodes from inheritance arenas that reference this session).
        self._arenas_closing: bool = False
        # The garbage event is set whenever we may have more garbage to clean up.
        self._garbage_evt: anyio.Event | None = None
        self._should_start_garbage_worker = _should_start_garbage_worker
        # Lease-backed lifetime. Populated in ``start()``; revoked during the
        # lifecycle handler's teardown path. All backend/arena operations on
        # this session validate their leases against ``_root_lease.grant``.
        self._lifetime: UnconditionalLocalLifetime[B] | None = None

    def __repr__(self) -> str:
        return f"<SQLiteFxtrSession {self._external_sid} ({self._internal_sid})>"

    @property
    def token(self) -> LifetimeToken[B]:
        """The lifetime token gating this session's backend/arena operations."""
        if self._lifetime is None:
            raise RuntimeError("Session has not been started")
        return self._lifetime.token

    @property
    def grant(self) -> Grant[B]:
        """Expected grant for lease validation (matches the ULL's root-lease grant)."""
        if self._lifetime is None:
            raise RuntimeError("Session has not been started")
        return self._lifetime.root_lease.grant

    @property
    def root_lease(self) -> Lease[B]:
        """Stable reference to the session's root lease. Clone before handing out."""
        if self._lifetime is None:
            raise RuntimeError("Session has not been started")
        return self._lifetime.root_lease

    def _session_files_dir_path(self) -> Path | None:
        """Get the path to the session files directory.

        Returns None for in-memory databases.
        """
        # v4 only supports file-based databases, so we always use session files
        # (Memory databases are not supported in v4)
        return Path(self._db_path + "-fxtrsessions")

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
        if self._started:
            raise RuntimeError("Session already started")
        if not self._read_only and self._internal_sid is not None:
            raise RuntimeError("Session already started")
        assert self._finalizer is None

        # Lease-backed lifetime: the session owns a ULL; its token becomes the
        # brand of backends and arenas produced by this session. Instantiated
        # before the lifecycle handler so ``token``/``root_lease`` are available
        # immediately after ``start()`` returns.
        lifetime = UnconditionalLocalLifetime(f"sqlite-session-{self._external_sid}")
        self._lifetime = lifetime

        if self._read_only:
            # Read-only mode: no session insertion, no session files, no GC.
            # We still need an IndexComputer for query resolution.
            index_computer = IndexComputer()

            async def _readonly_lifecycle_handler(
                task_status: anyio.abc.TaskStatus[None],
            ) -> None:
                async with lifetime, index_computer:
                    self._index_computer = index_computer
                    task_status.started(None)
                    await self._should_close_evt.wait()
                    lifetime.root_lease.discard()

            await self._background_task_group.start(_readonly_lifecycle_handler)
            self._started = True
            self._finalizer = weakref.finalize(self, self._should_close_evt.set)
            return

        # Set up background task to manage lifecycle.
        external_sid = self._external_sid
        should_close_evt = self._should_close_evt
        closed_evt = self._closed_evt

        async def _lifecycle_handler(task_status: anyio.abc.TaskStatus[int]) -> None:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("Starting session lifecycle handler for %s", external_sid)
            session_file = self._session_file_path()
            pool_group = self._pool_group
            assert (
                pool_group.write_pool is not None
            )  # lifecycle handler is never used in read-only mode

            async with contextlib.AsyncExitStack() as exit_stack:
                # Conditionally hold session file lock for the session lifetime
                if session_file is not None:
                    await exit_stack.enter_async_context(self._hold_session_file())
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            "Session %s acquired lock on session file %s",
                            external_sid,
                            session_file,
                        )

                # Insert session into DB and get internal ID
                now = datetime.now(timezone.utc)
                internal_sid = await pool_group.write_pool.run_sync_with_connection(
                    sqlite_ops.insert_session, external_sid.bytes, now
                )
                try:
                    async with lifetime:  # Keep lifetime token alive until block scope ends.
                        # IndexComputer lives inside the try block so it exits BEFORE
                        # the finally block marks arenas as closing. This ensures the
                        # IndexComputer's token invalidation can still abandon nodes
                        # from inheritance arenas that reference this session.
                        async with IndexComputer() as index_computer:
                            self._index_computer = index_computer
                            if logger.isEnabledFor(logging.DEBUG):
                                logger.debug(
                                    "Session lifecycle handler for %s started with internal ID %s",
                                    external_sid,
                                    internal_sid,
                                )
                            task_status.started(internal_sid)
                            await should_close_evt.wait()
                            if logger.isEnabledFor(logging.DEBUG):
                                logger.debug(
                                    "Session lifecycle handler for %s received should close event",
                                    external_sid,
                                )
                        self._index_computer = None
                        lifetime.root_lease.discard()
                finally:
                    # Mark arenas as closing — from this point, arena operations
                    # will fail. The IndexComputer has already exited above.
                    self._arenas_closing = True
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            "Session lifecycle handler for %s tearing down session", external_sid
                        )
                    with anyio.fail_after(10.0, shield=True):
                        should_close_evt.set()
                        if self._garbage_evt is not None:
                            self._garbage_evt.set()  # Wake up the garbage worker if it is waiting
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug(
                                "Session lifecycle handler for %s marking session as garbage",
                                external_sid,
                            )
                        # Mark session as garbage
                        now = datetime.now(timezone.utc)
                        await pool_group.write_pool.run_sync_with_connection(
                            sqlite_ops.mark_session_garbage, internal_sid, now
                        )
                        # Delete session file after marking as garbage (lock still held)
                        if session_file is not None:
                            try:
                                session_file.unlink(missing_ok=True)
                                if logger.isEnabledFor(logging.DEBUG):
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
                                        if logger.isEnabledFor(logging.DEBUG):
                                            logger.debug(
                                                "Session %s removed empty session dir %s",
                                                external_sid,
                                                session_dir,
                                            )
                                    except OSError:
                                        pass  # Directory not empty or other error
                            except OSError as e:
                                if logger.isEnabledFor(logging.WARNING):
                                    logger.warning(
                                        "Session %s failed to delete session file %s: %s",
                                        external_sid,
                                        session_file,
                                        e,
                                    )
                        closed_evt.set()
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug("Session lifecycle handler for %s completed", external_sid)

        self._internal_sid = await self._background_task_group.start(_lifecycle_handler)
        self._started = True
        if self._should_start_garbage_worker:
            self._spawn_cleanup_worker()
        self._finalizer = weakref.finalize(self, should_close_evt.set)

    async def aclose(self):
        """Close the session and clean up the session state in the database."""
        if not self._started:
            raise RuntimeError("Session not started")
        if self._finalizer is None:
            return
        if self._garbage_evt is not None:
            self._garbage_evt.set()
        self._should_close_evt.set()
        self._finalizer.detach()
        self._finalizer = None
        if not self._read_only:
            await self._closed_evt.wait()

    def close(self):
        """Close the session immediately, scheduling database cleanup."""
        if not self._started:
            raise RuntimeError("Session not started")
        if self._finalizer is None:
            return
        if self._garbage_evt is not None:
            self._garbage_evt.set()
        self._finalizer()
        self._finalizer = None

    def is_active(self) -> bool:
        """Check if the session is active (accepting new work)."""
        if self._read_only:
            return self._started and not self._should_close_evt.is_set()
        return self._internal_sid is not None and not self._should_close_evt.is_set()

    def _arenas_active(self) -> bool:
        """Check if arenas backed by this session can still perform operations.

        This stays True longer than is_active() — arenas remain operational during
        the window between the close signal and the actual teardown, so that the
        IndexComputer's token invalidation can abandon nodes from inheritance arenas.
        """
        if self._read_only:
            return self._started and not self._arenas_closing
        return self._internal_sid is not None and not self._arenas_closing

    @classmethod
    @contextlib.asynccontextmanager
    async def open(
        cls,
        db_path: str,
        _should_start_garbage_worker: bool = True,
        max_segment_size: int | None = None,
        read_only: bool = False,
    ) -> AsyncIterator[SQLiteFxtrSession]:
        """Set up a session with a database path, initializing the database if necessary.

        This is the main entry point for creating a SQLiteFxtrSession. It:
        1. Sets up the database schema (skipped in read-only mode)
        2. Creates connection pools for database operations
        3. Manages the session lifecycle

        Args:
            db_path: Path to the SQLite database file, or a named shared-cache memory URI
                (e.g., "file:memdb1?mode=memory&cache=shared").
            _should_start_garbage_worker: If True, spawn a garbage collection worker.
            max_segment_size: Soft limit in bytes for segment file rotation. Defaults to 512 MB.
            read_only: If True, open in read-only mode. No writes will be performed.
        """
        # Set up database and create connection pools
        async with GLOBAL_POOL_GROUP_MANAGER.get_pool_group(
            db_path, read_only=read_only
        ) as pool_group:
            # Use a task group to manage cleanup tasks from finalizers
            async with cls.attach_to_connection_pool_group(
                db_path,
                pool_group,
                _should_start_garbage_worker,
                max_segment_size=max_segment_size,
                read_only=read_only,
            ) as session:
                yield session

    @classmethod
    @contextlib.asynccontextmanager
    async def attach_to_connection_pool_group(
        cls,
        db_path: str,
        pool_group: ConnectionWorkerPoolGroup,
        _should_start_garbage_worker: bool = True,
        max_segment_size: int | None = None,
        read_only: bool = False,
    ) -> AsyncIterator[SQLiteFxtrSession]:
        """Attach to an existing connection pool group.

        This is an advanced entrypoint which can be useful for low-level use cases where you want
        to manage a shared set of connections to a single database across multiple sessions.

        Starts persistent worker threads for the pool group. Workers run for the
        lifetime of the session and are cancelled on exit via cancel scope.

        Args:
            db_path: Path to the SQLite database file, or a shared-cache memory URI.
            pool_group: Worker pool group to use for database operations, created by
                sqlite_ops.async_setup_database_and_create_worker_pool_group.
            _should_start_garbage_worker: If True, spawn a garbage collection worker.
            max_segment_size: Soft limit in bytes for segment file rotation. Defaults to 512 MB.
            read_only: If True, open in read-only mode.
        """
        # Use a task group to manage cleanup tasks from finalizers
        async with anyio_util.create_task_group(collapse_exc_group=True) as tg:
            # Start persistent worker threads for both pools.
            # start_all_workers returns cancel scopes we use to stop workers on exit.
            write_cancel_scope = None
            if pool_group.write_pool is not None:
                write_cancel_scope = await tg.start(pool_group.write_pool.start_all_workers)
            read_cancel_scope = await tg.start(pool_group.read_pool.start_all_workers)

            session = cls(
                db_path=db_path,
                pool_group=pool_group,
                background_task_group=tg,
                _should_start_garbage_worker=_should_start_garbage_worker and not read_only,
                max_segment_size=max_segment_size,
                read_only=read_only,
            )
            await session.start()
            try:
                yield session
            finally:
                await session.aclose()
                # Cancel workers after session cleanup is done (aclose submits
                # final DB work that needs workers alive). Threads are abandoned
                # (they exit shortly after via check_cancelled); the capacity
                # limiter ensures bounded connection count in the meantime.
                if write_cancel_scope is not None:
                    write_cancel_scope.cancel()
                read_cancel_scope.cancel()

    async def create_or_retrieve_user_namespace(self, name: str) -> SQLiteNamespaceBackend:
        """Create or retrieve a user namespace.

        Allocates a new namespace with the given name and "user" kind if it one not exist, and
        returns the namespace. Generates a new UUID if needed.

        In read-only mode, only retrieves existing namespaces (raises KeyError if not found).

        Args:
            name: The name of the namespace.

        Returns:
            A SQLiteNamespaceBackend object.
        """
        if not self.is_active():
            raise RuntimeError("Session not active")
        if self._read_only:
            namespace_internal_id, namespace_uuid = (
                await self._pool_group.read_pool.run_sync_with_connection(
                    sqlite_ops.get_user_namespace, name
                )
            )
        else:
            namespace_internal_id, namespace_uuid = await self._write_pool.run_sync_with_connection(
                sqlite_ops.get_or_create_user_namespace, name
            )
        return SQLiteNamespaceBackend(self, namespace_uuid, namespace_internal_id)

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
                        if logger.isEnabledFor(logging.INFO):
                            logger.info(
                                "Detected dead session %s (file: %s)",
                                session_file.name,
                                session_file,
                            )

                        # Parse session UUID from filename and mark as garbage in DB
                        try:
                            dead_session_uuid = uuid.UUID(session_file.name)
                        except ValueError:
                            if logger.isEnabledFor(logging.WARNING):
                                logger.warning(
                                    "Invalid session file name (not a UUID): %s",
                                    session_file.name,
                                )
                            # Delete the invalid file anyway
                            session_file.unlink(missing_ok=True)
                            continue

                        # Mark session as garbage in DB
                        rowcount = await self._write_pool.run_sync_with_connection(
                            sqlite_ops.mark_session_garbage_by_public_id, dead_session_uuid.bytes
                        )
                        if rowcount > 0:
                            if logger.isEnabledFor(logging.INFO):
                                logger.info("Marked dead session %s as garbage", dead_session_uuid)
                        else:
                            if logger.isEnabledFor(logging.DEBUG):
                                logger.debug(
                                    "Dead session %s not found in DB (may have been cleaned up)",
                                    dead_session_uuid,
                                )

                        # Delete the session file (while still holding lock)
                        session_file.unlink(missing_ok=True)
                        if logger.isEnabledFor(logging.DEBUG):
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
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug("Removed empty session files directory %s", session_dir)
                except OSError:
                    pass  # Directory not empty or other error

        except OSError as e:
            if logger.isEnabledFor(logging.WARNING):
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
        work_done, garbage_created = await self._write_pool.run_sync_with_connection(
            sqlite_ops.run_one_gc_step
        )
        if garbage_created and self._garbage_evt is not None:
            self._garbage_evt.set()
        return work_done

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
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("Garbage cleanup worker started for session %s", self._external_sid)
            internal_sid = self._internal_sid
            assert internal_sid is not None
            try:
                while self.is_active():
                    try:
                        # First, detect and mark any dead sessions from crashed processes
                        try:
                            await self._detect_and_mark_dead_sessions()
                        except Exception:
                            if logger.isEnabledFor(logging.WARNING):
                                logger.warning(
                                    "Error detecting dead sessions for %s",
                                    self._external_sid,
                                    exc_info=True,
                                )

                        # Check and claim GC
                        claimed_gc = await self._write_pool.run_sync_with_connection(
                            sqlite_ops.check_and_claim_gc, internal_sid
                        )

                        if not claimed_gc:
                            if logger.isEnabledFor(logging.DEBUG):
                                logger.debug(
                                    "Garbage worker: No garbage to collect or another session is"
                                    " collecting, skipping this iteration"
                                )
                        else:
                            # If we claimed garbage collection, run the inner loop
                            try:
                                if logger.isEnabledFor(logging.DEBUG):
                                    logger.debug(
                                        "Session %s starting garbage collection", self._external_sid
                                    )
                                while self.is_active():
                                    cleaned = await self._clean_up_garbage()
                                    if not cleaned:
                                        break
                                if logger.isEnabledFor(logging.DEBUG):
                                    logger.debug(
                                        "Session %s finished garbage collection", self._external_sid
                                    )
                            finally:
                                # Release garbage collection claim
                                with anyio.fail_after(10.0, shield=True):
                                    await self._write_pool.run_sync_with_connection(
                                        sqlite_ops.release_gc_claim, internal_sid
                                    )

                    except Exception:
                        if logger.isEnabledFor(logging.WARNING):
                            logger.warning(
                                "Ignoring error in garbage cleanup worker for session %s",
                                self._external_sid,
                                exc_info=True,
                            )

                    if not self.is_active():
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug(
                                "Garbage cleanup worker for session %s exiting at end of collection"
                                " pass because session is no longer active",
                                self._external_sid,
                            )
                        break

                    # Reset the garbage event and wait for new garbage or timeout
                    self._garbage_evt = anyio.Event()
                    await self._garbage_evt.wait()
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            "Garbage cleanup worker for session %s awoken by garbage event",
                            self._external_sid,
                        )
            finally:
                self._garbage_evt = None
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "Garbage cleanup worker exiting for session %s", self._external_sid
                    )

        self._background_task_group.start_soon(_cleanup_worker)

    @property
    def _write_pool(self) -> ConnectionWorkerPool:
        """Access the write pool, asserting it exists (not read-only mode)."""
        pool = self._pool_group.write_pool
        assert pool is not None, "Write pool not available (session is read-only)"
        return pool

    async def _dispose_arena_in_db(self, arena_internal_id: int):
        """Dispose of the arena in the database."""
        if not self.is_active():
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Session %s is not active, skipping disposal of arena %s",
                    self._external_sid,
                    arena_internal_id,
                )
            return
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Disposing of arena %s in the database", arena_internal_id)
        await self._write_pool.run_sync_with_connection(
            sqlite_ops.reassign_arena_to_garbage_session, arena_internal_id
        )
        if self._garbage_evt is not None:
            self._garbage_evt.set()
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Disposed of arena %s in the database", arena_internal_id)


class SQLiteNamespaceBackend[B: FreshLifetime[Any]](IndexablePinningBackend[B], DeepFetchable[B]):
    """Pinning backend backed by a SQLite database.

    Each SQLiteNamespaceBackend object combines a SQLiteFxtrSession with a namespace
    identifier. It then handles the individual operations within the namespace.

    Operations in a given SQLiteNamespaceBackend should act like the entities reachable from that
    namespace are the only ones that exist. In particular, ``try_lookup`` only returns entities
    that are reachable from the namespace.

    Parameterized on the same lifetime brand ``B`` as its owning session; returned arenas share
    ``B``. Callers obtain ``Lease[B]`` via ``get_lease()`` (or the session's ``root_lease``).
    """

    def __init__(
        self,
        session: SQLiteFxtrSession[B],
        namespace_id: uuid.UUID,
        namespace_internal_id: int,
    ):
        self._session = session
        self._namespace_id = namespace_id
        self._namespace_internal_id = namespace_internal_id

    @property
    @override
    def token(self) -> LifetimeToken[B]:
        return self._session.token

    def get_lease(self) -> Lease[B]:
        """Return a fresh clone of the underlying session's root lease.

        Use this for arenas returned by this backend and for any arena/scope operations that
        need a ``Lease[B]`` for this session's brand.
        """
        return self._session.root_lease.clone()

    @property
    def _index_computer(self) -> IndexComputer:
        """Access the session's IndexComputer (asserts session is active)."""
        ic = self._session._index_computer
        assert ic is not None, "Session not active — IndexComputer not available"
        return ic

    def _check_not_read_only(self) -> None:
        """Raise if the session is in read-only mode."""
        if self._session._read_only:
            raise RuntimeError("Store is open in read-only mode")

    @override
    async def pin_entity_from_arena[
        B_Src: LifetimeBrand
    ](
        self,
        key: str,
        entity_id: FxtrID,
        source: FlatEntitySource[B_Src],
        src_lease: Lease[B_Src],
        *,
        overwrite: bool = False,
    ):
        """Pin an entity from an arena into this backend.

        Uses a compare-and-swap pattern: reads INDEXED registrations, computes
        index entries, then atomically pins + writes index data while verifying
        the registrations haven't changed.  Retries on mismatch.

        Note: Supports ``FlatEntitySource`` in addition to ``ReadableArena`` as the source type.
        """
        if not self._session.is_active():
            src_lease.discard()
            raise RuntimeError("Session not active")
        try:
            self._check_not_read_only()
        except BaseException:
            src_lease.discard()
            raise

        async with self._session._pin_limiter:
            # ``src_lease`` is forwarded (taken ownership of) into the impl, which
            # clones it for each call against ``source``.
            await self._pin_entity_from_arena_impl(
                key, entity_id, source, src_lease, overwrite=overwrite
            )

    async def _pin_entity_from_arena_impl[
        B_Src: LifetimeBrand
    ](
        self,
        key: str,
        entity_id: FxtrID,
        source: FlatEntitySource[B_Src],
        src_lease: Lease[B_Src],
        *,
        overwrite: bool = False,
    ):
        # Take ownership of the caller's source lease so we can clone it for
        # every access to ``source`` during the pin flow.
        with src_lease.take() as src_lease:
            # Create a temporary arena to hold the entity during the pinning process.
            # ``copy_to_new_arena`` returns a same-brand source (no new lifetime);
            # we reuse ``src_lease`` clones to access it.
            source_copy = source.copy_to_new_arena(src_lease.clone(), [entity_id])
            temp_arena_internal_id, entity_iid = (
                await self._build_tmp_arena_copying_from_external_arena(
                    entity_id, source_copy, src_lease.clone()
                )
            )
            try:
                # Read INDEXED registrations covering this key
                # Each reg is (reg_id, indexer_entity_iid, key_prefix, indexer_public_id)
                indexed_regs: list[tuple[int, int, str, bytes]] = (
                    await self._session._pool_group.read_pool.run_sync_with_connection(
                        sqlite_index_ops.get_indexed_registrations_for_prefix,
                        self._namespace_internal_id,
                        key,
                    )
                )

                computed_entries: dict[int, list[tuple]] = {}  # indexer_entity_iid -> flat rows

                while True:
                    # Build the mapping from indexer_entity_iid -> FxtrID and find uncomputed indexers
                    reg_iid_to_fxtr: dict[int, FxtrID] = {
                        r[1]: FxtrID(raw_data_id=r[3]) for r in indexed_regs
                    }
                    uncomputed = [
                        (iid, fid)
                        for iid, fid in reg_iid_to_fxtr.items()
                        if iid not in computed_entries
                    ]

                    if uncomputed:
                        uncomputed_fids = [fid for _, fid in uncomputed]

                        # Only create a SQLite arena for indexers not already cached
                        cached = self._index_computer.cached_indexers
                        new_indexer_iids = [iid for iid, fid in uncomputed if fid not in cached]
                        indexer_arena: SQLiteNamespaceArena[B] | None = None
                        if new_indexer_iids:
                            indexer_arena = await SQLiteNamespaceArena._create_for_entities(
                                self._session, self, new_indexer_iids
                            )

                        # ``compute_indexer_entries`` needs a full ``ReadableArena``
                        # for provenance walks through ``lazily_attach_from_arena``.
                        # If ``source`` is already one (e.g. a ``SQLiteNamespaceArena``
                        # / ``MemoryArena`` from the caller), pass it directly — that
                        # preserves any precomp / cache the source holds and avoids a
                        # pointless re-read. Otherwise (``source`` is a narrow
                        # ``FlatEntitySource`` such as ``TransferEntitySource``), materialize a
                        # lightweight session-branded arena over the just-stored entity;
                        # we pay one sqlite read per indexed entity to support narrow
                        # sources rather than pushing the constraint back to the caller.
                        entity_arena_for_indexing: ReadableArena[Any]
                        entity_lease_for_indexing: Lease[Any]
                        entity_arena_is_synthetic: bool
                        if isinstance(source, ReadableArena):
                            entity_arena_for_indexing = source
                            entity_lease_for_indexing = src_lease.clone()
                            entity_arena_is_synthetic = False
                        else:
                            entity_arena_for_indexing = (
                                await SQLiteNamespaceArena._create_for_entities(
                                    self._session, self, [entity_iid]
                                )
                            )
                            entity_lease_for_indexing = self._session.root_lease.clone()
                            entity_arena_is_synthetic = True

                        try:
                            # Compute indexer entries. The index computer takes both
                            # (entity arena, entity lease) and (indexer arena, indexer lease);
                            # indexer arenas are session-branded, so we clone the session's
                            # root lease for that leg.
                            if indexer_arena is not None:
                                indexer_lease: Lease[B] | None = self._session.root_lease.clone()
                            else:
                                indexer_lease = None
                            results = await self._index_computer.compute_indexer_entries(
                                [entity_id],
                                uncomputed_fids,
                                entity_arena_for_indexing,
                                entity_lease_for_indexing,
                                indexer_arena,
                                indexer_lease,
                            )
                        finally:
                            if entity_arena_is_synthetic:
                                entity_arena_for_indexing.close()
                            if indexer_arena is not None:
                                indexer_arena.close()

                        # Convert results to flat rows keyed by internal ID
                        fid_to_iid = {fid: iid for iid, fid in uncomputed}
                        for (idx_fid, _eid_fid), entries in results.items():
                            computed_entries[fid_to_iid[idx_fid]] = [
                                tuple(_column_value_to_sqlite(v) for v in (*key_tuple, value))
                                for key_tuple, value in entries
                            ]

                    try:
                        _entity_internal_id, garbage_created = (
                            await self._session._write_pool.run_sync_with_connection(
                                sqlite_ops.create_or_update_pin,
                                self._namespace_internal_id,
                                entity_id._raw_data_id,
                                key,
                                overwrite,
                                computed_entries or None,
                            )
                        )
                        break
                    except sqlite_index_ops.IndexMismatchError as e:
                        # Registrations changed — retry with updated set
                        indexed_regs = e.current_registrations
                        continue

                if garbage_created and self._session._garbage_evt is not None:
                    self._session._garbage_evt.set()
            finally:
                # Abandon the temporary arena - entity stays in namespace because it's now pinned
                await self._session._dispose_arena_in_db(temp_arena_internal_id)

    @override
    async def unpin_entity(self, key: str):
        """Unpin an entity from this namespace."""
        if not self._session.is_active():
            raise RuntimeError("Session not active")
        self._check_not_read_only()

        garbage_created = await self._session._write_pool.run_sync_with_connection(
            sqlite_ops.unpin_and_maybe_rebind, self._namespace_internal_id, key
        )
        if garbage_created and self._session._garbage_evt is not None:
            self._session._garbage_evt.set()

    async def _discover_sweep_for_copy[
        B_Src: LifetimeBrand
    ](
        self,
        entity_id: FxtrID,
        source: FlatEntitySource[B_Src],
        src_lease: Lease[B_Src],
    ) -> tuple[
        list[sqlite_ops.SaveItem], dict[FxtrID, int]
    ]:
        """Read-only discover sweep: partition entities into preexisting and to-save.

        Walks the entity graph using the read pool. For each entity:
        - If it exists in the entity table: add to preexisting set, stop recursing
          (the CTE in the commit phase handles transitive deps).
        - If it doesn't exist: load from source arena, add to save queue, recurse on deps.

        The save queue is topologically sorted by construction (DFS post-order).

        Args:
            entity_id: The root entity to discover.
            source: The arena to load entity data from.

        Returns:
            (save_queue, preexisting) where:
            - save_queue: list of SaveItem in topological order (deps first)
            - preexisting: dict mapping FxtrID -> internal_id for entities already in entity table
        """
        # Take ownership of the source lease for the duration of the sweep so we
        # can clone it for every ``source.load_flat`` call below.
        src_lease = src_lease.take()
        save_queue: list[sqlite_ops.SaveItem] = []
        preexisting: dict[FxtrID, int] = {}
        already_seen: set[FxtrID] = set()

        worklist: list[_BulkDiscoverWork | _BulkEnqueueSaveWork] = [
            _BulkDiscoverWork(entity_id=entity_id)
        ]

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"Bulk copy discover sweep starting for entity {entity_id.summary()}")

        while worklist:
            item = worklist.pop()

            if isinstance(item, _BulkEnqueueSaveWork):
                save_queue.append(item.save_item)
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        f"  EnqueueSave({item.save_item.fxtr_id.summary()}):"
                        f" added to save queue (position {len(save_queue) - 1})"
                    )
                continue

            assert isinstance(item, _BulkDiscoverWork)
            fxtr_id = item.entity_id

            if fxtr_id in already_seen:
                continue
            already_seen.add(fxtr_id)

            # Check if entity exists in entity table (read-only, use read pool)
            internal_id = await self._session._pool_group.read_pool.run_sync_with_connection(
                sqlite_ops.discover_entity_by_public_id,
                fxtr_id._raw_data_id,
            )

            if internal_id is not None:
                # Entity exists — record and stop (CTE handles transitive deps)
                preexisting[fxtr_id] = internal_id
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        f"  Discover({fxtr_id.summary()}): found in entity table as {internal_id}"
                    )
                continue

            # Entity not in entity table — load from source arena
            flat_data = await source.load_flat(src_lease.clone(), fxtr_id)

            deps_as_bytes: list[tuple[bytes, str]] = [
                (dep_id._raw_data_id, dep_kind.value) for dep_id, dep_kind in flat_data.deps
            ]

            save_item = sqlite_ops.SaveItem(
                fxtr_id=fxtr_id,
                public_id_bytes=fxtr_id._raw_data_id,
                cbor_bytes=flat_data.entity_bytes,
                deps=flat_data.deps,
                deps_as_bytes=deps_as_bytes,
            )

            # Push EnqueueSave first (will be popped AFTER deps due to LIFO)
            worklist.append(_BulkEnqueueSaveWork(save_item=save_item))
            # Then push Discover for each dep (will be popped BEFORE EnqueueSave)
            for dep_id, _ in flat_data.deps:
                worklist.append(_BulkDiscoverWork(entity_id=dep_id))

            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    f"  Discover({fxtr_id.summary()}):"
                    f" not found, loaded from source with {len(flat_data.deps)} deps"
                )

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                f"Discover sweep complete: {len(save_queue)} to save,"
                f" {len(preexisting)} preexisting"
            )

        src_lease.discard()
        return save_queue, preexisting

    async def _build_tmp_arena_copying_from_external_arena[
        B_Src: LifetimeBrand
    ](
        self,
        entity_id: FxtrID,
        source: FlatEntitySource[B_Src],
        src_lease: Lease[B_Src],
    ) -> tuple[
        int, int
    ]:
        """Build a new temporary arena that contains an entity copied from an external arena.

        Builds a new temporary arena that contains the given entity from a source arena. The arena
        also contains all entities transitively reachable from the entity.

        Uses a two-phase batched algorithm:
        1. Discover sweep (read-only): walk the entity graph, partition into preexisting
           (already in entity table) and save queue (need to be written).
        2. Big commit: allocate IDs on the fly, save entities to segment files + SQLite,
           then use a recursive CTE to add all reachable entities to namespace and arena
           (single IMMEDIATE transaction).

        The temporary arena should usually be closed and discarded after use.

        Args:
            entity_id: The entity to copy into the temporary arena.
            source: The arena to copy the entity from. This arena will be taken over by the
                operation and closed afterward.

        Returns (arena_internal_id, entity_internal_id).
        """
        if not self._session.is_active():
            raise RuntimeError("Session not active")
        assert self._session._internal_sid is not None

        # Phase 0: Discover sweep (read pool, no namespace checks)
        # save_queue is topologically sorted by the DFS walk (deps first)
        save_queue, preexisting = await self._discover_sweep_for_copy(entity_id, source, src_lease)
        source.close()

        arena_uuid = uuid.uuid4()
        session_id = self._session._internal_sid
        namespace_id = self._namespace_internal_id
        pool_group = self._session._pool_group
        write_pool = self._session._write_pool

        if not save_queue:
            # Fast path: all entities already in entity table.
            # Single write txn: create arena + CTE namespace/arena add.
            root_iid = preexisting[entity_id]
            arena_id = await write_pool.run_sync_with_connection(
                sqlite_ops.bulk_copy_commit_preexisting_only,
                namespace_id,
                arena_uuid.bytes,
                session_id,
                root_iid,
            )
            return arena_id, root_iid

        # Phase 1: Big commit (single IMMEDIATE txn — allocates IDs on the fly)
        preexisting_bytes = {fid._raw_data_id: iid for fid, iid in preexisting.items()}
        resolved, arena_id = await write_pool.run_sync_with_connection(
            sqlite_ops.bulk_copy_commit,
            namespace_id,
            arena_uuid.bytes,
            session_id,
            entity_id._raw_data_id,
            preexisting_bytes,
            save_queue,
            pool_group.segment_manager,
            pool_group.segments_dir,
            self._session._max_segment_size,
        )

        entity_iid = resolved[entity_id._raw_data_id]
        return arena_id, entity_iid

    @override
    async def retrieve_pinned(self, key: str) -> tuple[FxtrID, SQLiteNamespaceArena[B]]:
        """Get a pinned entity by key."""
        if not self._session.is_active():
            raise RuntimeError("Session not active")

        entity_internal_id, entity_public_id, seg_id, seg_offset, seg_length = (
            await self._session._pool_group.read_pool.run_sync_with_connection(
                sqlite_ops.retrieve_pinned_entity,
                self._namespace_internal_id,
                key,
            )
        )

        fxtr_id = FxtrID(raw_data_id=bytes(entity_public_id))
        arena = await SQLiteNamespaceArena._create_for_entities(
            self._session,
            self,
            [entity_internal_id],
            segment_locations={entity_internal_id: (seg_id, seg_offset, seg_length)},
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

        rows = await self._session._pool_group.read_pool.run_sync_with_connection(
            sqlite_ops.list_pins_with_prefix, self._namespace_internal_id, prefix
        )
        return {row[0]: FxtrID(raw_data_id=bytes(row[1])) for row in rows}

    async def check_entities_exist(self, entity_ids: list[FxtrID]) -> set[FxtrID]:
        """Check which entities exist in this namespace.

        Args:
            entity_ids: Entity IDs to check.

        Returns:
            Set of entity IDs that exist in the namespace.
        """
        if not self._session.is_active():
            raise RuntimeError("Session not active")

        public_ids = [eid._raw_data_id for eid in entity_ids]
        id_map = {eid._raw_data_id: eid for eid in entity_ids}

        found_bytes = await self._session._pool_group.read_pool.run_sync_with_connection(
            sqlite_ops.check_entities_exist, self._namespace_internal_id, public_ids
        )
        return {id_map[b] for b in found_bytes if b in id_map}

    async def get_transitive_closure(self, root_ids: list[FxtrID]) -> list[tuple[FxtrID, int]]:
        """Walk entity_dep to find the transitive closure of root entities.

        Returns (entity_id, size_bytes) pairs in topological order (deps
        before dependents). Uses SQL recursive CTE — no blob I/O.
        """
        if not self._session.is_active():
            raise RuntimeError("Session not active")

        root_public_ids = [eid._raw_data_id for eid in root_ids]
        rows = await self._session._pool_group.read_pool.run_sync_with_connection(
            sqlite_ops.get_transitive_closure, self._namespace_internal_id, root_public_ids
        )
        return [(FxtrID(raw_data_id=pub_id), size) for pub_id, size in rows]

    @override
    async def try_lookup(self, node_id: FxtrID) -> SQLiteNamespaceArena[B] | None:
        """Try to look up an entity in the namespace."""
        if not self._session.is_active():
            raise RuntimeError("Session not active")

        result = await self._session._pool_group.read_pool.run_sync_with_connection(
            sqlite_ops.lookup_entity_in_namespace,
            self._namespace_internal_id,
            node_id._raw_data_id,
        )

        if result is None:
            return None

        entity_internal_id, seg_id, seg_offset, seg_length = result
        arena = await SQLiteNamespaceArena._create_for_entities(
            self._session,
            self,
            [entity_internal_id],
            segment_locations={entity_internal_id: (seg_id, seg_offset, seg_length)},
        )
        return arena

    # =========================================================================
    # IndexablePinningBackend methods
    # =========================================================================

    @override
    async def register_indexer[
        B_Src: LifetimeBrand
    ](
        self,
        indexer_id: FxtrID,
        source: ReadableArena[B_Src],
        src_lease: Lease[B_Src],
        key_prefix: str = "",
    ) -> None:
        """Register an indexer and build its index for existing pins."""
        from tqdm import tqdm

        with src_lease.take() as src_lease:

            if not self._session.is_active():
                raise RuntimeError("Session not active")
            self._check_not_read_only()

            # 1. Copy the indexer entity into the namespace (reuse existing copy flow).
            # ``copy_to_new_arena`` returns a same-brand arena; reuse ``src_lease`` clones
            # to access it.
            source_copy = source.copy_to_new_arena(src_lease.clone(), [indexer_id])
            temp_arena_id, indexer_entity_iid = (
                await self._build_tmp_arena_copying_from_external_arena(
                    indexer_id, source_copy, src_lease.clone()
                )
            )

            try:
                # 2. Get indexer column schema (loads + caches the indexer in IndexComputer)
                indexer_arena: SQLiteNamespaceArena[B] | None = None
                if indexer_id not in self._index_computer.cached_indexers:
                    indexer_arena = await SQLiteNamespaceArena._create_for_entities(
                        self._session, self, [indexer_entity_iid]
                    )
                try:
                    indexer_lease = (
                        self._session.root_lease.clone() if indexer_arena is not None else None
                    )
                    key_types, value_type = await self._index_computer.get_indexer_types(
                        indexer_id, indexer_arena, indexer_lease
                    )
                finally:
                    if indexer_arena is not None:
                        indexer_arena.close()

                # 4. Create registration + tables
                reg_id = None
                try:
                    assert self._session._internal_sid is not None
                    reg_id = await self._session._write_pool.run_sync_with_connection(
                        sqlite_index_ops.register_indexer_start,
                        self._namespace_internal_id,
                        indexer_entity_iid,
                        key_prefix,
                        self._session._internal_sid,
                        key_types,
                        value_type,
                    )

                    # Get total count of pins to index for progress tracking
                    total_pins = await self._session._pool_group.read_pool.run_sync_with_connection(
                        sqlite_index_ops.count_unprocessed_pins,
                        self._namespace_internal_id,
                        indexer_entity_iid,
                        key_prefix,
                    )

                    # 5. Build loop — indexer is already cached, only need entity arenas
                    BATCH = 100
                    with tqdm(
                        total=total_pins, desc="Building index", unit="pin", delay=0.5
                    ) as pbar:
                        while True:
                            unprocessed = (
                                await self._session._pool_group.read_pool.run_sync_with_connection(
                                    sqlite_index_ops.get_unprocessed_pins,
                                    self._namespace_internal_id,
                                    indexer_entity_iid,
                                    key_prefix,
                                    BATCH,
                                )
                            )

                            if not unprocessed:
                                done = await self._session._write_pool.run_sync_with_connection(
                                    sqlite_index_ops.try_finish_registration,
                                    reg_id,
                                    self._namespace_internal_id,
                                    indexer_entity_iid,
                                    key_prefix,
                                )
                                if done:
                                    break
                                else:
                                    # new pins appeared, update total
                                    new_unprocessed = await self._session._pool_group.read_pool.run_sync_with_connection(
                                        sqlite_index_ops.count_unprocessed_pins,
                                        self._namespace_internal_id,
                                        indexer_entity_iid,
                                        key_prefix,
                                    )
                                    new_total_with_processed = pbar.n + new_unprocessed
                                    if new_total_with_processed > pbar.total:
                                        pbar.total = new_total_with_processed
                                        pbar.refresh()
                                    continue

                            # Create arena for batch entities only (indexer already cached)
                            entity_iids = [e_iid for _, e_iid in unprocessed]
                            entity_arena = await SQLiteNamespaceArena._create_for_entities(
                                self._session,
                                self,
                                entity_iids,
                            )
                            entity_fxtr_ids = [
                                entity_arena._internal_id_to_fxtr[e_iid] for _, e_iid in unprocessed
                            ]

                            results = await self._index_computer.compute_indexer_entries(
                                entity_fxtr_ids,
                                [indexer_id],
                                entity_arena,
                                self._session.root_lease.clone(),
                                None,
                                None,
                            )

                            # Translate to flat entries keyed by internal ID
                            batch_entries: dict[int, list[tuple]] = {}
                            for (_, entity_fxtr_id), entries in results.items():
                                eiid = entity_arena._fxtr_id_to_internal[entity_fxtr_id]
                                batch_entries[eiid] = [
                                    tuple(_column_value_to_sqlite(v) for v in (*key_tuple, value))
                                    for key_tuple, value in entries
                                ]

                            await self._session._write_pool.run_sync_with_connection(
                                sqlite_index_ops.write_index_entries_batch,
                                self._namespace_internal_id,
                                indexer_entity_iid,
                                batch_entries,
                            )
                            entity_arena.close()

                            # Update progress bar
                            pbar.update(len(unprocessed))
                except BaseException:
                    # Clean up incomplete registration
                    if reg_id is not None:
                        await self._session._write_pool.run_sync_with_connection(
                            sqlite_index_ops.set_registration_garbage,
                            self._namespace_internal_id,
                            indexer_entity_iid,
                            key_prefix,
                        )
                    raise
            finally:
                await self._session._dispose_arena_in_db(temp_arena_id)

    @override
    async def unregister_indexer(self, indexer_id: FxtrID, key_prefix: str = "") -> None:
        """Unregister an indexer (sets its registration to GARBAGE)."""
        if not self._session.is_active():
            raise RuntimeError("Session not active")
        self._check_not_read_only()

        result = await self._session._pool_group.read_pool.run_sync_with_connection(
            sqlite_ops.lookup_entity_in_namespace,
            self._namespace_internal_id,
            indexer_id._raw_data_id,
        )
        if result is None:
            raise KeyError(f"Indexer entity not found in namespace")

        indexer_entity_iid = result[0]

        await self._session._write_pool.run_sync_with_connection(
            sqlite_index_ops.set_registration_garbage,
            self._namespace_internal_id,
            indexer_entity_iid,
            key_prefix,
        )
        if self._session._garbage_evt is not None:
            self._session._garbage_evt.set()

    @override
    async def list_indexers(
        self,
    ) -> tuple[list[tuple[FxtrID, str]], ReadableArena[B]]:
        """List all INDEXED indexers registered in this namespace."""
        if not self._session.is_active():
            raise RuntimeError("Session not active")

        rows = await self._session._pool_group.read_pool.run_sync_with_connection(
            sqlite_index_ops.get_indexed_registrations_for_namespace,
            self._namespace_internal_id,
        )
        # rows: list of (indexer_entity_iid, key_prefix)
        entity_iids = list(set(r[0] for r in rows))
        arena = await SQLiteNamespaceArena._create_for_entities(
            self._session,
            self,
            entity_iids,
        )
        registrations = [(arena._internal_id_to_fxtr[r[0]], r[1]) for r in rows]
        return registrations, arena

    @override
    async def iter_matches(  # type: ignore[override]
        self,
        first: IndexerConstraintByID,
        /,
        *rest: IndexerConstraintByID,
        key_prefix: str = "",
        where: BindingConstraint | None = None,
    ) -> AsyncIterator[tuple[str, FxtrID, dict[str, IndexerColumnValue], ReadableArena[B]]]:
        """Iterate over all matches for a given set of indexer constraints."""
        if not self._session.is_active():
            raise RuntimeError("Session not active")

        all_constraints = [first, *rest]

        # 1. Resolve each constraint to table/column info
        resolved: list[ResolvedConstraint] = []
        for c in all_constraints:
            info = await self._session._pool_group.read_pool.run_sync_with_connection(
                sqlite_index_ops.resolve_indexer_for_query,
                self._namespace_internal_id,
                c.indexer_id._raw_data_id,
                key_prefix,
            )
            indexer_entity_iid, c_key_types, c_value_type = info
            rewritten = parse_condition(
                c.condition,
                frozenset(c.key_variables),
                allow_value=True,
            )
            resolved.append(
                ResolvedConstraint(
                    table_name=sqlite_index_ops._index_table_name(indexer_entity_iid),
                    key_variables=c.key_variables,
                    key_column_types=c_key_types,
                    value_column_type=c_value_type,
                    condition=rewritten,
                )
            )

        # 2. Build query
        binding_sql: str | None = None
        if where:
            all_vars: set[str] = set()
            for c in all_constraints:
                all_vars.update(c.key_variables)
            binding_sql = parse_condition(
                where.condition,
                frozenset(all_vars),
                allow_value=False,
            )

        query = generate_iter_matches_query(
            resolved,
            binding_sql,
            self._namespace_internal_id,
            key_prefix,
        )

        # Build variable name -> IndexerColumnType map for result conversion.
        # Each variable's type comes from the first constraint that defines it.
        var_types: dict[str, IndexerColumnType] = {}
        for rc, c in zip(resolved, all_constraints):
            for var_name, col_type in zip(c.key_variables, rc.key_column_types):
                if var_name not in var_types:
                    var_types[var_name] = col_type

        # 3. Execute
        rows = await self._session._pool_group.read_pool.run_sync_with_connection(
            sqlite_index_ops.execute_index_query,
            query.sql,
            query.parameters,
        )

        # 4. Map results
        if not rows:
            return

        entity_iids = list(set(row[0] for row in rows))
        source_arena = await SQLiteNamespaceArena._create_for_entities(
            self._session,
            self,
            entity_iids,
        )
        try:
            for row in rows:
                entity_iid = row[0]
                pin_key = row[1]
                raw_var_values = row[2:]
                fxtr_id = source_arena._internal_id_to_fxtr[entity_iid]
                bindings: dict[str, IndexerColumnValue] = {}
                for var_name, raw_val in zip(query.binding_variable_names, raw_var_values):
                    bindings[var_name] = _sqlite_to_column_value(
                        raw_val,
                        var_types[var_name],
                    )
                result_arena = source_arena.copy_to_new_arena(
                    self._session.root_lease.clone(), [fxtr_id]
                )
                yield (pin_key, fxtr_id, bindings, result_arena)
        finally:
            source_arena.close()

    # -------------------------------------------------------------------------
    # Direct SQL execution
    # -------------------------------------------------------------------------

    @override
    async def run_direct_sql(
        self,
        query: str,
        views: list[ParsedView],
    ) -> list[sqlite3.Row]:
        """Run an ad-hoc SQL query against named index views.

        Resolves each view's indexer IDs to table info, generates the full
        ``WITH ... <user_query>`` SQL, and executes it in a fresh in-memory
        connection with the store ATTACHed read-only.
        """
        if not self._session.is_active():
            raise RuntimeError("Session not active")

        # 1. Resolve each view's columns to table info
        resolved_views: list[ResolvedView] = []
        for view in views:
            resolved_columns: list[ResolvedViewColumn] = []
            for col in view.columns:
                indexer_entity_iid, key_types, value_type = (
                    await self._session._pool_group.read_pool.run_sync_with_connection(
                        sqlite_index_ops.resolve_indexer_for_query,
                        self._namespace_internal_id,
                        col.indexer_id._raw_data_id,
                        view.prefix,
                    )
                )
                resolved_columns.append(
                    ResolvedViewColumn(
                        column_name=col.column_name,
                        table_name=sqlite_index_ops._index_table_name(indexer_entity_iid),
                        key_variables=col.key_variables,
                        key_column_types=key_types,
                        value_column_type=value_type,
                        condition=col.condition,
                    )
                )
            resolved_views.append(
                ResolvedView(
                    view_name=view.view_name,
                    prefix=view.prefix,
                    columns=resolved_columns,
                )
            )

        # 2. Generate the full SQL
        generated = generate_direct_sql_query(
            resolved_views,
            query,
            self._namespace_internal_id,
        )

        # 3. Execute in an isolated read-only connection
        db_path = self._session._db_path

        def _run() -> list[sqlite3.Row]:
            conn = sqlite3.connect("file::memory:", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute(
                    "ATTACH DATABASE ? AS _fxtr_internal_store",
                    (f"file:{db_path}?mode=ro",),
                )
                cursor = conn.execute(generated.sql, generated.parameters)
                return cursor.fetchall()
            finally:
                conn.close()

        return await anyio.to_thread.run_sync(_run, abandon_on_cancel=True)

    # -------------------------------------------------------------------------
    # Archive export / import
    # -------------------------------------------------------------------------

    async def _export_indexer_metadata(
        self,
        archive_session: SQLiteFxtrSession[Any],
    ) -> None:
        """Save indexer registration metadata into the archive.

        If indexers are registered, creates a "metadata" namespace in the
        archive and pins a NamespaceIndexMetadata entity under key "indexers".
        The entity holds strong links to all indexer entities, keeping them
        alive in the archive.
        """
        from fxtr2.data import entity_ref_scope
        from fxtr2.data.entity_ref_scope import FxtrScope
        from fxtr2.data.store_indexing.indexing_types import NamespaceIndexMetadata

        registrations, indexer_arena = await self.list_indexers()
        if not registrations:
            indexer_arena.close()
            return

        # Build the metadata entity in an isolated scope, then pin it
        async with FxtrScope(isolated=True, include=[entity_ref_scope.GLOBAL_SCOPE]) as scope:
            # Copy each indexer entity so ChildFxtrID references can be lifted
            for indexer_id, _prefix in registrations:
                await scope._main_arena.copy_from_arena(
                    scope._current_auth_lease().clone(),
                    indexer_arena,
                    self._session.root_lease.clone(),
                    indexer_id,
                )

            metadata = NamespaceIndexMetadata(
                registrations=tuple(registrations),
            )
            meta_ref = metadata.store(scope=scope)

            # Create metadata namespace and pin the metadata entity
            metadata_ns = await archive_session.create_or_retrieve_user_namespace("metadata")
            await metadata_ns.pin_entity_from_arena(
                "indexers",
                meta_ref.entity_id,
                scope._main_arena,
                scope._current_auth_lease().clone(),
                overwrite=True,
            )

        indexer_arena.close()
        logger.info(f"Exported indexer metadata: {len(registrations)} registrations")

    async def export_archive(
        self, output_path: Path, *, tar: bool = True, gz: bool = False
    ) -> None:
        """Export this namespace to a standalone archive.

        Creates a new SQLite fxtr store containing all entities reachable from
        currently pinned entries, then packages it for distribution.

        By default produces a tarball containing ``store.sqlite`` and
        ``store.sqlite.data2.shards/``.  With ``tar=False``, writes the sqlite
        and segment files directly to the output location.

        Args:
            output_path: Path to create the archive at.
            tar: If True (default), package as a tarball.
            gz: If True, compress the tarball with gzip (ignored when tar=False).
        """
        import shutil
        import tarfile
        import tempfile

        from tqdm import tqdm

        self._check_not_read_only()

        with tempfile.TemporaryDirectory() as tempdir:
            build_dir = Path(tempdir) / "build"
            build_dir.mkdir()
            build_sqlite = build_dir / "store.sqlite"

            # Build archive in temp directory
            async with SQLiteFxtrSession.open(str(build_sqlite)) as archive_session:
                archive_ns = await archive_session.create_or_retrieve_user_namespace("archive")

                all_pins = await self.list_pins()

                for key, entity_id in tqdm(
                    all_pins.items(), desc="Copying pins to archive", unit="pin", delay=0.5
                ):
                    _, arena = await self.retrieve_pinned(key)
                    try:
                        await archive_ns.pin_entity_from_arena(
                            key,
                            entity_id,
                            arena,
                            self._session.root_lease.clone(),
                            overwrite=True,
                        )
                    finally:
                        arena.close()

                # Export indexer metadata if any indexers are registered
                await self._export_indexer_metadata(archive_session)

            # Archive session is now closed.  Copy sqlite + segment files.
            build_shards = build_dir / "store.sqlite.data2.shards"
            if not build_shards.exists():
                raise FileNotFoundError(
                    f"Expected segment directory at {build_shards} after archive build"
                )

            if tar:
                tar_mode = "w:gz" if gz else "w:"
                tqdm.write("Creating tarball (gzip)..." if gz else "Creating tarball...")
                with tarfile.open(output_path, tar_mode) as tf:
                    tf.add(build_sqlite, arcname="store.sqlite")
                    for seg_file in sorted(build_shards.iterdir()):
                        tf.add(
                            seg_file,
                            arcname=f"store.sqlite.data2.shards/{seg_file.name}",
                        )
            else:
                tqdm.write("Copying SQLite database...")
                shutil.copy2(build_sqlite, output_path)
                dest_shards = output_path.parent / (output_path.name + ".data2.shards")
                tqdm.write("Copying segment files...")
                shutil.copytree(build_shards, dest_shards)

        logger.info(f"Exported {len(all_pins)} pins to {output_path}")


class SQLiteNamespaceArena[B: FreshLifetime[Any]](ReadableArena[B]):
    """A lightweight, read-only arena backed by a SQLite database.

    Arenas maintain:
    - Reference to the session and namespace
    - A Python set of tracked entity IDs (for synchronous is_tracked checks)
    - Preloaded sync-accessible data (unwrap targets, etc.)

    This arena does NOT create or manage DB arena rows. It is a pure Python-side view
    into entity data that already exists in the SQLite database. All operations are
    either pure Python or read-pool DB queries — no write operations.

    For every node in _tracked:
    - All _precomp_* dicts must be populated so that all synchronous ops on those nodes can be
      evaluated synchronously without touching the database.

    Parameterized on the same lifetime brand ``B`` as its owning session; every
    public method takes a ``Lease[B]`` first (validated and consumed).
    """

    def __init__(
        self,
        session: SQLiteFxtrSession[B],
        namespace: SQLiteNamespaceBackend[B],
    ):
        self._session = session
        self._namespace = namespace
        # _tracked represents the set of entities that are tracked from the perspective of the
        # Python program (uses FxtrID for the public API).
        self._tracked: set[FxtrID] = set()
        self._closed = False
        # Preloaded data. Must exist for all nodes in _tracked and all
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
        self._precomp_reservations: Counter[InternalEntityID] = Counter()
        # Segment location cache: internal_id -> (seg_id, seg_offset, seg_length).
        # seg_length is None when only the offset is known (same-segment precomp fast path).
        self._segment_locations: dict[InternalEntityID, tuple[int, int, int | None]] = {}

    @property
    @override
    def token(self) -> LifetimeToken[B]:
        return self._session.token

    @classmethod
    async def _create_for_entities(
        cls,
        session: SQLiteFxtrSession[B],
        namespace: SQLiteNamespaceBackend[B],
        entity_internal_ids: list[InternalEntityID],
        segment_locations: dict[int, tuple[int, int, int]] | None = None,
    ) -> SQLiteNamespaceArena[B]:
        """Create a lightweight arena tracking the given entities.

        Loads precomputed data for the given entity internal IDs via the read pool.
        No DB arena row is created.

        Args:
            session: The session for pool access.
            namespace: The namespace these entities belong to.
            entity_internal_ids: The internal IDs of entities to track.
            segment_locations: Optional pre-fetched segment locations from upstream queries.

        Returns:
            A new arena with the given entities tracked and precomp data loaded.
        """
        arena = cls(session, namespace)
        if segment_locations:
            arena._segment_locations.update(segment_locations)
        await arena._load_into_tracked(entity_internal_ids)
        return arena

    def __repr__(self) -> str:
        return (
            f"<SQLiteNamespaceArena (ns={self._namespace._namespace_id},"
            f" ns_internal={self._namespace._namespace_internal_id},"
            f" tracked={len(self._tracked)})>"
        )

    def _ensure_active(self) -> None:
        """Ensure the arena is active."""
        if self._closed or not self._session._arenas_active():
            raise RuntimeError("Arena is closed")

    def _read_entity_blob(self, internal_id: InternalEntityID) -> bytes:
        """Read an entity blob from segment files using the cached segment location.

        Handles both full locations (seg_id, offset, length) and partial locations
        where length is None (known from precomp same-segment fast path).
        """
        loc = self._segment_locations.get(internal_id)
        if loc is None:
            raise ValueError(
                f"Entity with internal_id {internal_id} has no cached segment location"
            )
        seg_id, seg_offset, seg_length = loc
        sm = self._session._pool_group.segment_manager
        if seg_length is None:
            # Length unknown (from precomp fast path) — read envelope header to determine it
            return sm.read_record_at(seg_id, seg_offset)
        return sm.read_record(seg_id, seg_offset, seg_length)

    def _process_precomp_entries(
        self,
        entity_seg_id: int,
        precomp_map: dict[InternalEntityID, list],
    ) -> None:
        """Register precomp entries: map IDs, populate precomp dicts, cache same-segment locations.

        Processes the precomp map from a decoded entity blob. For each entry not already known:
        - Registers the FxtrID <-> InternalEntityID mapping
        - Stores precomp relationships (unwraps_to, ctx_node, backstep)
        - Caches same-segment dep locations for the fast read path
        """
        for iid, entry in precomp_map.items():
            if iid in self._internal_id_to_fxtr:
                continue

            fxtr_id_bytes, unwraps_to, ctx_node, backstep, seg_offset = entry
            fxtr_id = FxtrID(raw_data_id=bytes(fxtr_id_bytes))

            self._fxtr_id_to_internal[fxtr_id] = iid
            self._internal_id_to_fxtr[iid] = fxtr_id

            if iid not in self._precomp_value_count:
                self._precomp_value_count[iid] = 0

            if unwraps_to is not None:
                self._precomp_unwraps_to[iid] = unwraps_to
                self._precomp_value_count[unwraps_to] = (
                    self._precomp_value_count.get(unwraps_to, 0) + 1
                )
            if ctx_node is not None:
                self._precomp_ctx_node[iid] = ctx_node
                self._precomp_value_count[ctx_node] = self._precomp_value_count.get(ctx_node, 0) + 1
            if backstep is not None:
                self._precomp_step_back_p_ctx[iid] = backstep
                self._precomp_value_count[backstep] = self._precomp_value_count.get(backstep, 0) + 1

            # Same-segment fast path: cache dep location for fast reads
            if seg_offset is not None and iid not in self._segment_locations:
                self._segment_locations[iid] = (entity_seg_id, seg_offset, None)

    async def _load_into_tracked(self, internal_ids: Iterable[InternalEntityID]) -> None:
        """Load precomputed data for entities and add them to _tracked.

        Args:
            internal_ids: Internal IDs of entities to load and track.
        """
        internal_ids_list = list(internal_ids)
        # Filter to only nodes we haven't already loaded precomp data for
        already_loaded: list[int] = [
            iid for iid in internal_ids_list if iid in self._internal_id_to_fxtr
        ]
        frontier_list: list[int] = [
            iid for iid in internal_ids_list if iid not in self._internal_id_to_fxtr
        ]
        if not frontier_list:
            # All nodes already have precomp data loaded, just update _tracked
            self._tracked.update(self._internal_id_to_fxtr[iid] for iid in internal_ids_list)
            return

        for iid in already_loaded:
            # Make sure to keep around the reservation while we load the others.
            self._precomp_reservations[iid] += 1

        # Batch-fetch segment locations for frontier IDs not already cached
        missing_locs = [iid for iid in frontier_list if iid not in self._segment_locations]
        if missing_locs:
            new_locs = await self._session._pool_group.read_pool.run_sync_with_connection(
                sqlite_ops.fetch_entity_segment_locations,
                missing_locs,
            )
            self._segment_locations.update(new_locs)

        # Read blobs from segment files
        for internal_id in frontier_list:
            blob = self._read_entity_blob(internal_id)
            entity_seg_id = self._segment_locations[internal_id][0]

            # Decode metadata and process precomp entries
            _deps_list, precomp_map = entity_blob.decode_metadata(blob)
            self._process_precomp_entries(entity_seg_id, precomp_map)

        for iid in already_loaded:
            self._precomp_reservations[iid] -= 1

        # Now that precomp data is up to date, add the FxtrIDs to _tracked
        self._tracked.update(self._internal_id_to_fxtr[iid] for iid in internal_ids_list)

    def _track_from_precomputed(self, internal_id: InternalEntityID) -> FxtrID:
        """Track a precomputed entity in the arena.

        Simply adds the entity to _tracked. Assumes the entity already has precomputed data.
        No DB writes.
        """
        assert internal_id in self._internal_id_to_fxtr
        fxtr_id = self._internal_id_to_fxtr[internal_id]
        self._tracked.add(fxtr_id)
        return fxtr_id

    @override
    @_with_validated_arena_lease
    def is_tracked(self, lease: Lease[B], node_id: FxtrID) -> bool:
        """Check if a node is tracked by this arena."""
        self._ensure_active()
        return node_id in self._tracked

    @override
    @_with_validated_arena_lease
    def unwrap_provenance(self, lease: Lease[B], node_id: FxtrID) -> FxtrID | None:
        """Unwrap a provenance node."""
        self._ensure_active()
        if node_id not in self._tracked:
            raise ValueError(f"Node {node_id} is not tracked by this arena")
        internal_id = self._fxtr_id_to_internal[node_id]
        next_internal_id = self._precomp_unwraps_to.get(internal_id)
        if next_internal_id is None:
            return None
        return self._track_from_precomputed(next_internal_id)

    @override
    @_with_validated_arena_lease
    def c_node_to_context_data(self, lease: Lease[B], c_node_id: FxtrID) -> FxtrID:
        """Unstructure a context provenance node."""
        self._ensure_active()
        if c_node_id not in self._tracked:
            raise ValueError(f"Node {c_node_id} is not tracked by this arena")
        if c_node_id.get_tag() != "c":
            raise ValueError(f"Node {c_node_id} is not a c-node")
        internal_id = self._fxtr_id_to_internal[c_node_id]
        next_internal_id = self._precomp_ctx_node.get(internal_id)
        assert next_internal_id is not None
        return self._track_from_precomputed(next_internal_id)

    @override
    @_with_validated_arena_lease
    def p_node_to_pathroot(self, lease: Lease[B], p_node_id: FxtrID) -> FxtrID:
        """Unstructure a pathroot provenance node."""
        self._ensure_active()
        if p_node_id not in self._tracked:
            raise ValueError(f"Node {p_node_id} is not tracked by this arena")
        if p_node_id.get_tag() != "p":
            raise ValueError(f"Node {p_node_id} is not a p-node")
        internal_id = self._fxtr_id_to_internal[p_node_id]
        next_internal_id = self._precomp_ctx_node.get(internal_id)
        assert next_internal_id is not None
        return self._track_from_precomputed(next_internal_id)

    @override
    @_with_validated_arena_lease
    def step_back_p_ctx(self, lease: Lease[B], p_node_id: FxtrID) -> FxtrID | None:
        """Step back a pathroot provenance node."""
        self._ensure_active()
        if p_node_id not in self._tracked:
            raise ValueError(f"Node {p_node_id} is not tracked by this arena")
        if p_node_id.get_tag() != "p":
            raise ValueError(f"Node {p_node_id} is not a p-node")
        internal_id = self._fxtr_id_to_internal[p_node_id]
        next_internal_id = self._precomp_step_back_p_ctx.get(internal_id)
        if next_internal_id is None:
            return None
        return self._track_from_precomputed(next_internal_id)

    @override
    @_with_validated_arena_lease
    def abandon(self, lease: Lease[B], node_id: FxtrID):
        """Abandon a node from this arena. No DB writes — just Python state cleanup."""
        self._ensure_active()
        if node_id not in self._tracked:
            raise ValueError(f"Node {node_id} is not tracked by this arena")

        # Remove from Python liveset
        self._tracked.remove(node_id)

        # Precomp cache cleanup: Remove cached data for nodes that are no longer needed.
        internal_id = self._fxtr_id_to_internal[node_id]
        if (
            self._precomp_value_count[internal_id] == 0
            and not self._precomp_reservations[internal_id]
        ):
            to_remove: deque[InternalEntityID] = deque([internal_id])

            while to_remove:
                iid = to_remove.popleft()
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
                        if new_count == 0 and not self._precomp_reservations[referenced_id]:
                            # Check if this referenced node can now be removed too
                            ref_fxtr = self._internal_id_to_fxtr[referenced_id]
                            if ref_fxtr not in self._tracked:
                                to_remove.append(referenced_id)

                self._fxtr_id_to_internal.pop(fxtr_id)
                self._internal_id_to_fxtr.pop(iid)
                self._precomp_value_count.pop(iid)
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("Cleaned up precomputed state for %s", fxtr_id)

    @override
    @_with_validated_arena_lease
    def copy_to_new_arena(
        self, lease: Lease[B], node_ids: Collection[FxtrID]
    ) -> SQLiteNamespaceArena[B]:
        """Copy a subset of nodes to a new arena. Pure Python — no DB writes."""
        self._ensure_active()

        # 1. Validate all requested nodes are tracked
        node_ids_list = list(node_ids)
        for node_id in node_ids_list:
            if node_id not in self._tracked:
                raise ValueError(f"Node {node_id} is not tracked by this arena")

        # 2. Create new arena object
        new_arena = SQLiteNamespaceArena(self._session, self._namespace)

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

        return new_arena

    async def _load_dn_impl(
        self,
        internal_id: InternalEntityID,
    ) -> tuple[bytes, list[tuple[InternalEntityID, str]]]:
        """Common d/n blob loading: fetch location, read blob, decode metadata, process precomp.

        Returns (cbor_bytes, deps_list).
        """
        # Ensure segment location is cached
        if internal_id not in self._segment_locations:
            locs = await self._session._pool_group.read_pool.run_sync_with_connection(
                sqlite_ops.fetch_entity_segment_locations,
                [internal_id],
            )
            self._segment_locations.update(locs)

        # Read from segment file
        blob = self._read_entity_blob(internal_id)

        cbor_bytes = entity_blob.decode_cbor_bytes(blob)
        if cbor_bytes is None:
            raise ValueError(
                f"Entity with internal_id {internal_id} has no CBOR data (derived node)"
            )

        deps_list, precomp_map = entity_blob.decode_metadata(blob)
        entity_seg_id = self._segment_locations[internal_id][0]

        # Process precomp entries: register IDs, populate precomp dicts, cache locations
        self._process_precomp_entries(entity_seg_id, precomp_map)

        return cbor_bytes, deps_list

    @override
    @_with_validated_arena_lease
    async def load(self, lease: Lease[B], node_id: FxtrID) -> FxtrCbor:
        """Load an entity from the SQLite database via the read pool.

        Only supports d/n nodes (contentful nodes with stored CBOR data).
        For p/u/c/./! nodes, raises NotImplementedError — users should use
        ``lazily_attach_from_arena`` to get entities into a MemoryStore first,
        which handles p/u derivation internally.
        """
        self._ensure_active()
        if node_id not in self._tracked:
            raise ValueError(
                f"Node {node_id} is not tracked in the fxtr arena! Only tracked nodes can be"
                " loaded."
            )

        tag = node_id.get_tag()
        if tag not in ("d", "n"):
            raise NotImplementedError(
                f"SQLiteNamespaceArena does not support loading '{tag}' nodes directly. "
                "Use lazily_attach_from_arena to attach entities to a MemoryStore first, "
                "which handles derived node types (p, u, c, ., !) internally."
            )

        internal_id = self._fxtr_id_to_internal[node_id]
        cbor_bytes, deps_list = await self._load_dn_impl(internal_id)

        # Collect child internal IDs from deps_list (direct dependencies only)
        child_internal_ids = [
            dep_iid
            for dep_iid, kind in deps_list
            if kind in (DepKind.STRONG_REF.value, DepKind.WEAK_REF.value)
        ]

        # Add children to tracked set
        if child_internal_ids:
            self._tracked.update(self._internal_id_to_fxtr[iid] for iid in child_internal_ids)

        # Deserialize CBOR
        data = entity_core.fxtr_cbor_loads(cbor_bytes)

        return data

    @override
    @_with_validated_arena_lease
    async def load_flat(self, lease: Lease[B], node_id: FxtrID) -> FxtrEntityFlatData:
        """Load flat entity data without deserializing CBOR."""
        self._ensure_active()
        if node_id not in self._tracked:
            raise ValueError(
                f"Node {node_id} is not tracked in the fxtr arena! Only tracked nodes can be"
                " loaded."
            )

        internal_id = self._fxtr_id_to_internal[node_id]
        tag = node_id.get_tag()

        if tag in ("d", "n"):
            cbor_bytes, deps_list = await self._load_dn_impl(internal_id)

            # Convert deps_list to FxtrIDs and DepKinds
            deps: list[tuple[FxtrID, DepKind]] = []
            dep_internal_ids: list[int] = []

            for dep_internal_id, dep_kind_str in deps_list:
                dep_fxtr_id = self._internal_id_to_fxtr[dep_internal_id]
                dep_kind = DepKind(dep_kind_str)
                deps.append((dep_fxtr_id, dep_kind))
                dep_internal_ids.append(dep_internal_id)

            # Track all deps (load precomp data for them if needed)
            if dep_internal_ids:
                await self._load_into_tracked(dep_internal_ids)

        else:
            # For derived nodes: construct deps from precomp data
            cbor_bytes = None
            deps = []

            # Check for ctx_node
            if internal_id in self._precomp_ctx_node:
                ctx_internal_id = self._precomp_ctx_node[internal_id]
                ctx_fxtr_id = self._track_from_precomputed(ctx_internal_id)
                deps.append((ctx_fxtr_id, DepKind.CTX_NODE))

            # Check for backstep_node
            if internal_id in self._precomp_step_back_p_ctx:
                backstep_internal_id = self._precomp_step_back_p_ctx[internal_id]
                backstep_fxtr_id = self._track_from_precomputed(backstep_internal_id)
                deps.append((backstep_fxtr_id, DepKind.BACKSTEP))

            # Check for unwraps_to
            if internal_id in self._precomp_unwraps_to:
                unwraps_internal_id = self._precomp_unwraps_to[internal_id]
                unwraps_fxtr_id = self._track_from_precomputed(unwraps_internal_id)
                deps.append((unwraps_fxtr_id, DepKind.UNWRAPS_TO))

        return FxtrEntityFlatData(
            entity_id=node_id,
            entity_bytes=cbor_bytes,
            deps=deps,
        )

    @override
    def close(self):
        """Close the arena. Just clears Python state — no DB cleanup needed."""
        if self._closed:
            return
        self._closed = True
        self._tracked.clear()
