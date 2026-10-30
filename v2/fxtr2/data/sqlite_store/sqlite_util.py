"""Connection pool utilities for sqlite3-based SQLite access.

This module provides connection pooling with capacity limiting and
async dispatch to thread pools.
"""

from __future__ import annotations

import contextlib
import dataclasses
import inspect
import itertools
import logging
import queue
import sqlite3
import threading
import time
from typing import Any, Callable, Generator, ParamSpec, TypeVar

import anyio
import anyio.abc
import anyio.from_thread
import anyio.to_thread

from fxtr2.data.sqlite_store.segment_io import SegmentManager

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

# Note: ConnectionPool and ConnectionPoolGroup have been removed in v4.
# They used per-call thread dispatch and are no longer needed (runtime code uses
# ConnectionWorkerPool which has persistent worker threads).

# Result tags for the result box
_RESULT_OK = 0
_RESULT_ERR = 1

# Type alias for work items on the queue
# (func, args, result_box, event, cancelled_flag)
# None is used as a shutdown sentinel.
type _WorkItem = tuple[
    Callable[..., Any],
    tuple[Any, ...],
    list[Any],
    anyio.Event,
    list[bool],
]


class ConnectionWorkerPool:
    """A pool of persistent worker threads that process DB operations from a shared queue.

    Unlike ConnectionPool (which spawns/acquires a thread per call via anyio.to_thread),
    this pool maintains persistent worker threads that each hold a database connection
    for their lifetime. Work items are submitted to a shared queue and picked up by
    the next available worker.

    This eliminates per-call thread dispatch overhead, which is significant when
    performing tens of thousands of small DB operations.

    Usage:
        1. Create the pool with max_connections
        2. Start workers via start_all_workers() in a task group
        3. Submit work via run_sync_with_connection()
        4. When done, call aclose() to gracefully shut down workers
    """

    def __init__(
        self,
        db_path: str,
        max_connections: int,
        connection_setup: Callable[[sqlite3.Connection], None] | None = None,
        connection_teardown: Callable[[sqlite3.Connection], None] | None = None,
        uri: bool = False,
    ):
        self._db_path = db_path
        self._max_connections = max_connections
        self._uri = uri
        self._capacity_limiter = anyio.CapacityLimiter(max_connections)
        self._connection_setup = connection_setup
        self._connection_teardown = connection_teardown
        self._queue: queue.Queue[_WorkItem | None] = queue.Queue()
        self._active_workers = 0
        self._closed = False
        # Thread-side tracking for graceful shutdown.
        # _active_threads tracks how many threads are actually alive (vs _active_workers
        # which tracks async tasks and gets decremented immediately on cancellation).
        self._thread_lock = threading.Lock()
        self._active_threads = 0
        self._all_threads_done = threading.Event()
        self._all_threads_done.set()  # No threads running initially

    def _create_connection(self) -> sqlite3.Connection:
        """Create a new sqlite3 connection (same logic as ConnectionPool)."""
        conn = sqlite3.connect(
            self._db_path, isolation_level=None, check_same_thread=False, uri=self._uri
        )
        if self._connection_setup is not None:
            self._connection_setup(conn)
        return conn

    def _close_connection(self, conn: sqlite3.Connection) -> None:
        """Run teardown callback and close a connection."""
        if self._connection_teardown is not None:
            try:
                self._connection_teardown(conn)
            except Exception:
                if logger.isEnabledFor(logging.ERROR):
                    logger.exception(f"[conn {id(conn):x}] Connection teardown failed")
        conn.close()

    def _worker_loop(self) -> None:
        """Main loop for a persistent worker thread.

        Creates a connection, then repeatedly polls the queue for work items.
        Exits when cancelled via anyio or when a shutdown sentinel is received.
        """
        with self._thread_lock:
            self._active_threads += 1
            self._all_threads_done.clear()
        conn = self._create_connection()
        try:
            while True:
                # Poll the queue with a timeout to allow periodic cancellation checks
                try:
                    item = self._queue.get(timeout=1.0)
                except queue.Empty:
                    # Timeout — check for cancellation and loop
                    anyio.from_thread.check_cancelled()
                    continue

                # Check for shutdown sentinel
                if item is None:
                    return

                # Check for cancellation before starting work.
                # If cancelled, put the item back so another worker (or drain) handles it.
                try:
                    anyio.from_thread.check_cancelled()
                except BaseException:
                    self._queue.put(item)
                    raise

                func, args, result_box, event, cancelled_flag = item

                # If the caller already cancelled, skip the work
                if cancelled_flag[0]:
                    continue

                # Execute the work
                try:
                    result = func(conn, *args)
                    result_box.append(_RESULT_OK)
                    result_box.append(result)
                except BaseException as exc:
                    result_box.append(_RESULT_ERR)
                    result_box.append(exc)

                # Rollback any uncommitted transaction left by the work function
                if conn.in_transaction:
                    if logger.isEnabledFor(logging.WARNING):
                        logger.warning(
                            "Worker connection had uncommitted transaction after operation; "
                            "rolling back."
                        )
                    conn.execute("ROLLBACK")

                # Signal the async caller that the result is ready
                try:
                    anyio.from_thread.run_sync(event.set)
                except Exception:
                    # Event loop may be shutting down — swallow
                    pass
        finally:
            self._close_connection(conn)
            with self._thread_lock:
                self._active_threads -= 1
                if self._active_threads == 0 and self._closed:
                    self._all_threads_done.set()

    async def run_worker_thread_forever(
        self, *, task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED
    ) -> None:
        """Run a single worker thread until cancelled or it receives a shutdown sentinel.

        Increments the active worker count while running. Uses the pool's capacity
        limiter to ensure at most max_connections threads are actually running
        concurrently. Extra workers wait on the limiter and can be safely cancelled
        without consequence.

        Can be started with `await tg.start(pool.run_worker_thread_forever)` to
        wait until the worker is registered, or with `tg.start_soon(...)`.
        """
        self._active_workers += 1
        task_status.started(None)
        try:
            await anyio.to_thread.run_sync(
                self._worker_loop, abandon_on_cancel=True, limiter=self._capacity_limiter
            )
        finally:
            self._active_workers -= 1

    async def start_all_workers(
        self, *, task_status: anyio.abc.TaskStatus[anyio.CancelScope]
    ) -> None:
        """Start max_connections worker threads and return the cancel scope to stop them.

        Creates an internal task group with max_connections workers. Returns
        the task group's cancel scope via task_status so the caller can cancel
        all workers when done. Cancelled workers are abandoned (threads continue
        briefly until check_cancelled() detects the cancellation).

        For final teardown (ensuring threads actually exit and close connections),
        use aclose() on the pool instead — this sends shutdown sentinels and waits.

        Usage::

            cancel_scope = await tg.start(pool.start_all_workers)
            # ... use pool ...
            cancel_scope.cancel()  # cancels async tasks, threads exit shortly after
        """
        async with anyio.create_task_group() as worker_tg:
            for _ in range(self._max_connections):
                await worker_tg.start(self.run_worker_thread_forever)
            task_status.started(worker_tg.cancel_scope)

    async def run_sync_with_connection(
        self,
        fn: Callable[..., R],
        /,
        *args: Any,
    ) -> R:
        """Submit a sync function to be run by a worker thread with a connection.

        Same interface as ConnectionPool.run_sync_with_connection (minus **kwargs).

        Args:
            fn: A sync function that takes a Connection as first arg.
            *args: Additional positional arguments for fn.

        Returns:
            The return value of fn.

        Raises:
            RuntimeError: If the pool is closed or no workers are running.
        """
        if self._closed:
            raise RuntimeError("ConnectionWorkerPool is closed")
        if self._active_workers == 0:
            raise RuntimeError(
                "No active worker threads. Start workers via run_worker_in_cancel_scope() "
                "before submitting work."
            )

        cancelled_flag: list[bool] = [False]
        result_box: list[Any] = []
        event = anyio.Event()

        self._queue.put((fn, args, result_box, event, cancelled_flag))
        try:
            await event.wait()
        except BaseException:
            cancelled_flag[0] = True
            raise

        if result_box[0] == _RESULT_OK:
            return result_box[1]
        else:
            raise result_box[1]

    def close(self) -> None:
        """Close the pool, drain pending work, and send shutdown sentinels to workers.

        After closing, new submissions raise RuntimeError. Any pending items
        in the queue are completed with a RuntimeError and their events are signaled.
        Shutdown sentinels (None) are pushed for each active worker so they exit
        their loops and close their connections.

        To wait for workers to actually finish, use aclose() instead.
        """
        if self._closed:
            return
        self._closed = True
        # Drain the queue — signal all pending items with an error
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                continue
            _, _, result_box, event, _ = item
            result_box.append(_RESULT_ERR)
            result_box.append(RuntimeError("ConnectionWorkerPool is closed"))
            event.set()
        # Send shutdown sentinels to all active threads (not _active_workers,
        # which tracks the async side and may already be 0 if cancel scopes
        # were cancelled).
        with self._thread_lock:
            for _ in range(self._active_threads):
                self._queue.put(None)
            if self._active_threads == 0:
                self._all_threads_done.set()

    async def aclose(self) -> None:
        """Close the pool and wait for all worker threads to actually finish.

        Calls close() to drain pending work and send shutdown sentinels,
        then waits for all worker threads to exit and close their connections.
        """
        self.close()
        if not self._all_threads_done.is_set():
            await anyio.to_thread.run_sync(self._all_threads_done.wait)


@dataclasses.dataclass
class ConnectionWorkerPoolGroup:
    """A pair of worker pools for read and write operations with segment file manager.

    Like ConnectionPoolGroup but using ConnectionWorkerPool for persistent
    worker threads instead of per-call thread dispatch.

    Worker threads must be started by the caller (e.g. in a task group) before
    submitting work. See ConnectionWorkerPool for the lifecycle pattern.

    Always use GLOBAL_POOL_GROUP_MANAGER (in sqlite_namespace.py) to ensure a single
    pool group per database path.

    Attributes:
        write_pool: Connection pool for write operations (None in read-only mode).
        read_pool: Connection pool for read operations.
        segment_manager: SegmentManager for reading entity data from segment files.
        segments_dir: Path to the segment files directory.
    """

    write_pool: ConnectionWorkerPool | None
    read_pool: ConnectionWorkerPool
    segment_manager: SegmentManager
    segments_dir: str

    def close(self) -> None:
        """Close both pools and segment manager (sync — does not wait for workers to finish)."""
        if self.write_pool is not None:
            self.write_pool.close()
        self.read_pool.close()
        self.segment_manager.close()

    async def aclose(self) -> None:
        """Close both pools, segment manager, and wait for all workers to finish."""
        if self.write_pool is not None:
            await self.write_pool.aclose()
        await self.read_pool.aclose()
        self.segment_manager.close()


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection, transaction_mode: str) -> Generator[None, None, None]:
    """Small helper to begin a transaction with a given transaction mode.

    Uses explicit BEGIN/COMMIT with manual transaction control (isolation_level=None).
    """
    if logger.isEnabledFor(logging.DEBUG):
        # Look up the name and line of the caller's stack frame
        frame = inspect.currentframe()
        for i in range(2):
            if frame is None:
                raise RuntimeError("can't find frame")
            frame = frame.f_back
        if frame is None:
            raise RuntimeError("No current frame")

        caller_file = frame.f_code.co_filename
        caller_name = frame.f_code.co_name
        caller_line = frame.f_lineno
        logger.debug(
            f"[conn {id(conn):x}] Initiating transaction {transaction_mode} in {caller_name} at"
            f" {caller_file}:{caller_line}"
        )
    else:
        caller_name = "(unknown)"
        caller_line = None
        caller_file = "(unknown)"

    # Use explicit BEGIN with retry logic for busy errors
    MAX_RETRIES = 5
    for i in itertools.count():
        try:
            conn.execute(f"BEGIN {transaction_mode}")
            break
        except sqlite3.OperationalError as e:
            # Check if it's a busy error (error code 5 = SQLITE_BUSY)
            if e.sqlite_errorcode == sqlite3.SQLITE_BUSY:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    if logger.isEnabledFor(logging.WARNING):
                        logger.warning(
                            f"[conn {id(conn):x}] conn is busy, attempt"
                            f" {i+1}/{MAX_RETRIES} (t={time.time()}) (for {caller_name} at"
                            f" {caller_file}:{caller_line} (failed to roll back txn start)"
                        )
                else:
                    if logger.isEnabledFor(logging.WARNING):
                        logger.warning(
                            f"[conn {id(conn):x}] conn is busy, attempt"
                            f" {i+1}/{MAX_RETRIES} (t={time.time()}) (for {caller_name} at"
                            f" {caller_file}:{caller_line} (rolled back txn start)"
                        )
                if i + 1 >= MAX_RETRIES:
                    raise
                else:
                    time.sleep(0.2)
                    continue
            else:
                # Not a busy error, re-raise
                raise
    try:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                f"[conn {id(conn):x}] Entered transaction {transaction_mode} in {caller_name} at"
                f" {caller_file}:{caller_line}"
            )
        yield
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                f"[conn {id(conn):x}] About to commit transaction {transaction_mode} in"
                f" {caller_name} at {caller_file}:{caller_line}"
            )
        conn.execute("COMMIT")
    except BaseException as e:
        # Only rollback if we're still in a transaction
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if caller_line is not None:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    f"[conn {id(conn):x}] Transaction in {caller_name} at"
                    f" {caller_file}:{caller_line} failed with {type(e)}"
                )
        raise
    else:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                f"[conn {id(conn):x}] Transaction in {caller_name} at"
                f" {caller_file}:{caller_line} committed"
            )
