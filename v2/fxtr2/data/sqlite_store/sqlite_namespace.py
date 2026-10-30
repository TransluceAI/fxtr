"""Entity store backed by a SQLite database using sqlite3 for high performance."""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import os
import threading
import traceback
import uuid
import weakref
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import (
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
import anyio.from_thread
import anyio.lowlevel

from fxtr2.data.abstract_arena import (
    CopyTargetArena,
    EntityBackref,
    ObservableStore,
    OtherBackref,
    PersistentPinningBackend,
    ReadableArena,
)
from fxtr2.data.arena_util import DepKind, fetch_data_and_deps_from_arena
from fxtr2.data.entity_core import FxtrCbor, FxtrID
from fxtr2.data.file_util.lock import (
    LockNotAcquiredError,
    async_file_lock_acquire,
    sync_file_lock_acquire_nonblocking,
)
from fxtr2.util import anyio_util

logger = logging.getLogger(__name__)


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


from fxtr2.data.sqlite_store import sqlite_ops
from fxtr2.data.sqlite_store.sqlite_util import ConnectionPoolGroup

type InternalEntityID = int


DEBUG_DROPPED_ARENAS = os.environ.get("FXTR_DEBUG_DROPPED_ARENAS", "0") == "1"


class _SharedSqlitePoolGroupManager:
    def __init__(self):
        self._pool_group_evts: dict[str, anyio.Event] = {}
        self._pool_groups: dict[str, ConnectionPoolGroup] = {}
        self._refcounts: dict[str, int] = {}

    @contextlib.asynccontextmanager
    async def get_pool_group(self, db_path: str) -> AsyncIterator[ConnectionPoolGroup]:
        """Get a pool group for a database path.

        This context manager will construct a pool group for each database path it receives, but
        will then re-use the same pool group if there are multiple concurrent requests for a pool
        group for the same database path. This makes it possible for multiple sqlite sessions to
        multiplex over a single set of database connections, avoiding lock contention between
        multiple namespaces in the same process.

        Args:
            db_path: Path to the SQLite database file, or a shared-cache memory URI.
        """
        self._refcounts[db_path] = self._refcounts.get(db_path, 0) + 1
        try:

            if db_path in self._pool_group_evts:
                await self._pool_group_evts[db_path].wait()
                logger.info("Re-using existing connection pool for %s", db_path)
            else:
                assert db_path not in self._pool_groups
                evt = anyio.Event()
                self._pool_group_evts[db_path] = evt
                logger.info("Setting up a new connection pool for %s", db_path)
                self._pool_groups[db_path] = (
                    await sqlite_ops.async_setup_database_and_create_pool_group(db_path)
                )
                logger.info(
                    "Connection pool for %s is ready [write conns: %s]",
                    db_path,
                    [f"{id(conn):x}" for conn in self._pool_groups[db_path].write_pool._freelist],
                )
                evt.set()

            try:
                grp = self._pool_groups[db_path]
            except KeyError as exc:
                raise RuntimeError(
                    f"Connection pool for {db_path} not found, likely failed during setup in a different task"
                ) from exc
            yield grp
        finally:
            self._refcounts[db_path] -= 1
            if self._refcounts[db_path] == 0:
                grp = self._pool_groups.pop(db_path)
                if grp is not None:
                    logger.info("Closing connection pool for %s", db_path)
                    grp.close()
                evt = self._pool_group_evts.pop(db_path)
                if evt is not None:
                    evt.set()  # <- Make sure anybody waiting on the event advances (they'll get an error)
                del self._refcounts[db_path]


GLOBAL_POOL_GROUP_MANAGER = _SharedSqlitePoolGroupManager()


class SQLiteFxtrSession:
    """A session for a fxtr sqlite db."""

    def __init__(
        self,
        db_path: str,
        pool_group: ConnectionPoolGroup,
        background_task_group: anyio.abc.TaskGroup,
        _should_start_garbage_worker: bool = True,
    ):
        """Create a new session.

        Args:
            db_path: Path to the SQLite database file, or a shared-cache memory URI.
            pool_group: sqlite3 connection pool group for database operations.
            background_task_group: A task group that is guaranteed to be running for the full
                lifetime of the session.
            _should_start_garbage_worker: If True, a garbage worker will be spawned to clean up garbage.
        """
        self._db_path = db_path
        self._pool_group = pool_group
        self._background_task_group = background_task_group
        self._evt_loop_thread_token = anyio.lowlevel.current_token()
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
        # Check if this is a memory database (URI format like file:name?mode=memory&cache=shared)
        if sqlite_ops._is_memory_db(self._db_path):
            return None
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
        if self._internal_sid is not None:
            raise RuntimeError("Session already started")
        assert self._finalizer is None

        # Set up background task to manage lifecycle.
        external_sid = self._external_sid
        should_close_evt = self._should_close_evt
        closed_evt = self._closed_evt

        async def _lifecycle_handler(task_status: anyio.abc.TaskStatus[int]) -> None:
            logger.debug("Starting session lifecycle handler for %s", external_sid)
            session_file = self._session_file_path()
            pool_group = self._pool_group

            async with contextlib.AsyncExitStack() as exit_stack:
                # Conditionally hold session file lock for the session lifetime
                if session_file is not None:
                    await exit_stack.enter_async_context(self._hold_session_file())
                    logger.debug(
                        "Session %s acquired lock on session file %s", external_sid, session_file
                    )

                # Insert session into DB and get internal ID
                now = datetime.now(timezone.utc)
                internal_sid = await pool_group.write_pool.run_sync_with_connection(
                    sqlite_ops.insert_session, external_sid.bytes, now
                )
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
    async def open(
        cls, db_path: str, _should_start_garbage_worker: bool = True
    ) -> AsyncIterator[SQLiteFxtrSession]:
        """Set up a session with a database path, initializing the database if necessary.

        This is the main entry point for creating a SQLiteFxtrSession. It:
        1. Sets up the database schema
        2. Creates connection pools for database operations
        3. Manages the session lifecycle

        Args:
            db_path: Path to the SQLite database file, or a named shared-cache memory URI
                (e.g., "file:memdb1?mode=memory&cache=shared").
            _should_start_garbage_worker: If True, spawn a garbage collection worker.
        """
        # Set up database and create connection pools
        async with GLOBAL_POOL_GROUP_MANAGER.get_pool_group(db_path) as pool_group:
            # Use a task group to manage cleanup tasks from finalizers
            async with cls.attach_to_connection_pool_group(
                db_path, pool_group, _should_start_garbage_worker
            ) as session:
                yield session

    @classmethod
    @contextlib.asynccontextmanager
    async def attach_to_connection_pool_group(
        cls,
        db_path: str,
        pool_group: ConnectionPoolGroup,
        _should_start_garbage_worker: bool = True,
    ) -> AsyncIterator[SQLiteFxtrSession]:
        """Attach to an existing connection pool group.

        This is an advanced entrypoint which can be useful for low-level use cases where you want
        to manage a shared set of connections to a single database across multiple sessions.

        Args:
            db_path: Path to the SQLite database file, or a shared-cache memory URI.
            pool_group: Connection pool group to use for database operations, created by
                sqlite_ops.async_setup_database_and_create_pool_group.
            _should_start_garbage_worker: If True, spawn a garbage collection worker.
        """
        # Use a task group to manage cleanup tasks from finalizers
        async with anyio_util.create_task_group(collapse_exc_group=True) as tg:
            session = cls(
                db_path=db_path,
                pool_group=pool_group,
                background_task_group=tg,
                _should_start_garbage_worker=_should_start_garbage_worker,
            )
            await session.start()
            try:
                yield session
            finally:
                await session.aclose()

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
        namespace_internal_id, namespace_uuid = (
            await self._pool_group.write_pool.run_sync_with_connection(
                sqlite_ops.get_or_create_user_namespace, name
            )
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
                        rowcount = await self._pool_group.write_pool.run_sync_with_connection(
                            sqlite_ops.mark_session_garbage_by_public_id, dead_session_uuid.bytes
                        )
                        if rowcount > 0:
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
        work_done, garbage_created = await self._pool_group.write_pool.run_sync_with_connection(
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
                            logger.warning(
                                "Error detecting dead sessions for %s",
                                self._external_sid,
                                exc_info=True,
                            )

                        # Check and claim GC
                        claimed_gc = await self._pool_group.write_pool.run_sync_with_connection(
                            sqlite_ops.check_and_claim_gc, internal_sid
                        )

                        if not claimed_gc:
                            logger.debug(
                                "Garbage worker: No garbage to collect or another session is"
                                " collecting, skipping this iteration"
                            )
                        else:
                            # If we claimed garbage collection, run the inner loop
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
                                    await self._pool_group.write_pool.run_sync_with_connection(
                                        sqlite_ops.release_gc_claim, internal_sid
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
        await self._pool_group.write_pool.run_sync_with_connection(
            sqlite_ops.reassign_arena_to_garbage_session, arena_internal_id
        )
        if self._garbage_evt is not None:
            self._garbage_evt.set()
        logger.debug("Disposed of arena %s in the database", arena_internal_id)


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
            _entity_internal_id, garbage_created = (
                await self._session._pool_group.write_pool.run_sync_with_connection(
                    sqlite_ops.create_or_update_pin,
                    self._namespace_internal_id,
                    entity_id._raw_data_id,
                    key,
                    overwrite,
                )
            )
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

        garbage_created = await self._session._pool_group.write_pool.run_sync_with_connection(
            sqlite_ops.unpin_and_maybe_rebind, self._namespace_internal_id, key
        )
        if garbage_created and self._session._garbage_evt is not None:
            self._session._garbage_evt.set()

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
        assert self._session._internal_sid is not None
        arena_uuid = uuid.uuid4()
        temp_arena_id = await self._session._pool_group.write_pool.run_sync_with_connection(
            sqlite_ops.create_arena,
            arena_uuid.bytes,
            self._namespace_internal_id,
            self._session._internal_sid,
        )

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
                internal_id, found = (
                    await self._session._pool_group.write_pool.run_sync_with_connection(
                        sqlite_ops.copy_discover,
                        self._session._internal_sid,
                        fxtr_id._raw_data_id,
                    )
                )
                if found:
                    assert internal_id is not None
                    fxtr_to_internal[fxtr_id] = internal_id
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

                already_in_namespace, dep_internal_ids = (
                    await self._session._pool_group.write_pool.run_sync_with_connection(
                        sqlite_ops.copy_track_deps,
                        self._session._internal_sid,
                        self._namespace_internal_id,
                        temp_arena_id,
                        internal_id,
                    )
                )

                if already_in_namespace:
                    logger.debug(
                        f"  TrackDeps({internal_id}): already in namespace, added to arena"
                    )
                    continue

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

                # Determine storage location
                tag = fxtr_id.get_tag()
                storage_loc = "here" if tag in ("d", "n") else "none"

                # Convert deps to format expected by copy_save (list of (public_id_bytes, kind_str))
                dep_tuples: list[tuple[bytes, str]] = [
                    (dep_id._raw_data_id, dep_kind.value) for dep_id, dep_kind in deps
                ]

                internal_id = await self._session._pool_group.write_pool.run_sync_with_connection(
                    sqlite_ops.copy_save,
                    self._session._internal_sid,
                    fxtr_id._raw_data_id,
                    storage_loc,
                    cbor_bytes,
                    dep_tuples,
                )

                fxtr_to_internal[fxtr_id] = internal_id
                worklist.append(_TrackWork(internal_id=internal_id))
                logger.debug(
                    f"  Save({fxtr_id.summary()}): saved to entity tables as {internal_id}"
                )
                logger.debug(f"    -> enqueue Track({internal_id})")

            elif isinstance(item, _TrackWork):
                internal_id = item.internal_id

                await self._session._pool_group.write_pool.run_sync_with_connection(
                    sqlite_ops.copy_track,
                    self._session._internal_sid,
                    self._namespace_internal_id,
                    temp_arena_id,
                    internal_id,
                )

                logger.debug(f"  Track({internal_id}): added to namespace and arena")

        source.close()
        return temp_arena_id

    @override
    async def retrieve_pinned(self, key: str) -> tuple[FxtrID, SQLiteNamespaceArena]:
        """Get a pinned entity by key."""
        if not self._session.is_active():
            raise RuntimeError("Session not active")

        assert self._session._internal_sid is not None
        arena_uuid = uuid.uuid4()
        entity_internal_id, entity_public_id, arena_internal_id = (
            await self._session._pool_group.write_pool.run_sync_with_connection(
                sqlite_ops.retrieve_pinned_and_create_arena,
                self._namespace_internal_id,
                self._session._internal_sid,
                key,
                arena_uuid.bytes,
            )
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

        rows = await self._session._pool_group.read_pool.run_sync_with_connection(
            sqlite_ops.list_pins_with_prefix, self._namespace_internal_id, prefix
        )
        return {row[0]: FxtrID(raw_data_id=bytes(row[1])) for row in rows}

    @override
    async def try_lookup(self, node_id: FxtrID) -> SQLiteNamespaceArena | None:
        """Try to look up an entity in the namespace."""
        if not self._session.is_active():
            raise RuntimeError("Session not active")

        assert self._session._internal_sid is not None
        arena_uuid = uuid.uuid4()
        result = await self._session._pool_group.write_pool.run_sync_with_connection(
            sqlite_ops.lookup_in_namespace_and_create_arena,
            self._namespace_internal_id,
            self._session._internal_sid,
            node_id._raw_data_id,
            arena_uuid.bytes,
        )

        if result is None:
            return None

        entity_internal_id, arena_internal_id = result
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

        # Get all backrefs
        _entity_internal_id, entity_backref_rows, pin_names, arena_backref_rows = (
            await self._session._pool_group.read_pool.run_sync_with_connection(
                sqlite_ops.get_backrefs_for_entity,
                self._namespace_internal_id,
                node_id._raw_data_id,
            )
        )

        if _entity_internal_id is None:
            return []  # Entity not in namespace

        backrefs: list[EntityBackref | OtherBackref] = []

        # 1. Entity backrefs
        for requirer_public_id, dep_kind in entity_backref_rows:
            requirer_fxtr_id = FxtrID(raw_data_id=bytes(requirer_public_id))
            # Map dep kind string to EntityBackref kind
            if dep_kind == "strong_ref":
                backrefs.append(EntityBackref(referrer=requirer_fxtr_id, kind="strong_ref"))
            elif dep_kind == "weak_ref":
                backrefs.append(EntityBackref(referrer=requirer_fxtr_id, kind="weak_ref"))
            elif dep_kind == "ctx_node":
                backrefs.append(EntityBackref(referrer=requirer_fxtr_id, kind="context_data"))
            elif dep_kind == "unwraps_to":
                backrefs.append(EntityBackref(referrer=requirer_fxtr_id, kind="unwrap"))
            # Skip backstep deps (like memory_arena does)

        # 2. Pin backrefs
        for pin_name in pin_names:
            backrefs.append(OtherBackref(kind="pin", data={"name": pin_name}))

        # 3. Arena backrefs
        for arena_public_id, session_public_id, is_garbage in arena_backref_rows:
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

        if DEBUG_DROPPED_ARENAS:
            self._debug_created_tb = traceback.extract_stack()
        else:
            self._debug_created_tb = None

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
            self._debug_created_tb,
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
                # Create arena
                internal_arena_id = await session._pool_group.write_pool.run_sync_with_connection(
                    sqlite_ops.create_arena,
                    arena_id.bytes,
                    namespace._namespace_internal_id,
                    session._internal_sid,
                )

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
        frontier_list: list[int] = [
            iid for iid in internal_ids_list if iid not in self._internal_id_to_fxtr
        ]
        if not frontier_list:
            # All nodes already have precomp data loaded, just update _tracked
            self._tracked.update(self._internal_id_to_fxtr[iid] for iid in internal_ids_list)
            return

        # The dep kinds that need to be precomputed for sync operations
        sync_dep_kinds = ["ctx_node", "backstep", "unwraps_to"]

        # Load all precomp data
        id_to_public_id, id_to_deps = (
            await self._session._pool_group.read_pool.run_sync_with_connection(
                sqlite_ops.load_precomp_data_for_entities, frontier_list, sync_dep_kinds
            )
        )

        # Process the loaded data
        for internal_id, pub_id in id_to_public_id.items():
            fxtr_id = FxtrID(raw_data_id=bytes(pub_id))
            self._fxtr_id_to_internal[fxtr_id] = internal_id
            self._internal_id_to_fxtr[internal_id] = fxtr_id
            if internal_id not in self._precomp_value_count:
                self._precomp_value_count[internal_id] = 0

        for requirer_id, deps in id_to_deps.items():
            for required_id, kind in deps:
                # Store the dep in precomp
                if kind == "unwraps_to":
                    self._precomp_unwraps_to[requirer_id] = required_id
                elif kind == "ctx_node":
                    self._precomp_ctx_node[requirer_id] = required_id
                elif kind == "backstep":
                    self._precomp_step_back_p_ctx[requirer_id] = required_id
                else:
                    raise ValueError(f"Unknown dep kind: {kind}")

                # Update value count
                self._precomp_value_count[required_id] = (
                    self._precomp_value_count.get(required_id, 0) + 1
                )

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
            # Verify entity is in namespace and add to arena
            await self._session._pool_group.write_pool.run_sync_with_connection(
                sqlite_ops.verify_in_namespace_and_add_to_arena,
                self._namespace._namespace_internal_id,
                self._arena_internal_id,
                internal_id,
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
            garbage_created = await self._session._pool_group.write_pool.run_sync_with_connection(
                sqlite_ops.remove_from_arena_and_maybe_rebind,
                self._namespace._namespace_internal_id,
                self._arena_internal_id,
                internal_id,
            )
            if garbage_created and self._session._garbage_evt is not None:
                self._session._garbage_evt.set()
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
            if not self._session.is_active():
                logger.debug(
                    "Session %s is closed, skipping copy_to_new_arena from arena %s for new arena %s",
                    self._session._internal_sid,
                    self._arena_internal_id,
                    new_arena._arena_internal_id,
                )
                new_arena._closed = True
                new_arena._close_reason = RuntimeError("Session closed during copy")
                new_arena_ready_evt.set()
                return

            try:
                # Create arena with all entities
                assert self._session._internal_sid is not None
                internal_ids = [self._fxtr_id_to_internal[nid] for nid in node_ids_list]
                new_internal_id = (
                    await self._session._pool_group.write_pool.run_sync_with_connection(
                        sqlite_ops.create_arena_with_entities,
                        new_arena._arena_id.bytes,
                        self._namespace._namespace_internal_id,
                        self._session._internal_sid,
                        internal_ids,
                    )
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
            assert self._arena_internal_id is not None

            # Load data and add children to arena
            data, child_internal_ids = (
                await self._session._pool_group.write_pool.run_sync_with_connection(
                    sqlite_ops.load_and_add_children_to_arena,
                    node_id,
                    internal_id,
                    self._namespace._namespace_internal_id,
                    self._arena_internal_id,
                )
            )

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
                # Look up the internal entity ID and add to arena
                assert self._arena_internal_id is not None
                entity_internal_id = (
                    await self._session._pool_group.write_pool.run_sync_with_connection(
                        sqlite_ops.lookup_and_add_to_arena,
                        entity_id._raw_data_id,
                        self._namespace._namespace_internal_id,
                        self._arena_internal_id,
                    )
                )
                if entity_internal_id is None:
                    raise RuntimeError(f"Entity {entity_id} not found in namespace after copy")

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
        tb: traceback.StackSummary | None,
    ):
        """Schedule disposal of the arena for finalization."""

        if tb:
            logger.warning(
                "Arena %s was finalized while open! This arena was created at:\n%s",
                arena_internal_id,
                "".join(tb.format()),
            )

        async def _do_dispose():
            logger.debug("Arena %s being cleaned up by finalizer!", arena_internal_id)
            await session._dispose_arena_in_db(arena_internal_id)

        if session.is_active():
            # session._background_task_group.start_soon(_do_dispose)
            try:
                tok = anyio.lowlevel.current_token()
            except anyio.NoEventLoopError:
                tok = None

            if tok == session._evt_loop_thread_token:
                logger.debug(
                    "Finalizer for arena %s tripped on main loop thread, scheduling disposal task",
                    arena_internal_id,
                )
                session._background_task_group.start_soon(_do_dispose)
            else:
                thread_name = threading.current_thread().name
                logger.info(
                    "Finalizer for arena %s tripped on non-main loop thread %s, scheduling disposal task in main loop",
                    arena_internal_id,
                    thread_name,
                )
                anyio.from_thread.run_sync(
                    session._background_task_group.start_soon,
                    _do_dispose,
                    token=session._evt_loop_thread_token,
                )

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

        if self._finalizer is not None:
            my_id = self._arena_internal_id
            assert my_id is not None
            logger.debug("Enqueuing disposal of arena %s due to manual close", my_id)

            async def _do_dispose():
                await self._session._dispose_arena_in_db(my_id)

            self._schedule_new_task(_do_dispose, f"dispose_for_close()")
            # Detach the finalizer to prevent disposal from being triggered again
            self._finalizer.detach()
            self._finalizer = None

        # Mark closed, preventing any further operations on the arena
        self._closed = True
        self._tracked.clear()
