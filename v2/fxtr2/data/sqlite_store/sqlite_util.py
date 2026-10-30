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
import sqlite3
import threading
import time
from typing import Callable, Concatenate, Generator, ParamSpec, TypeVar

import anyio
import anyio.to_thread

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")


class ConnectionPool:
    """A pool of sqlite3 connections with capacity limiting.

    Manages a freelist of reusable connections and enforces a maximum
    concurrent connection limit via an anyio CapacityLimiter.

    Thread safety note: Python's GIL makes list.append() and list.pop()
    atomic for simple cases. The capacity limiter bounds concurrent access.
    """

    def __init__(
        self,
        db_path: str,
        max_connections: int,
        connection_setup: Callable[[sqlite3.Connection], None] | None = None,
        is_memory_db: bool = False,
    ):
        """Initialize a connection pool.

        Args:
            db_path: Path to the SQLite database file, or a URI.
            max_connections: Maximum number of concurrent connections.
            connection_setup: Optional callback to run on each new connection
                (e.g., to set PRAGMAs like foreign_keys).
            is_memory_db: If True, this is a named shared-cache memory database.
                Pool connections will use URI mode to properly connect to the
                shared memory database.
        """
        self._db_path = db_path
        self._connection_setup = connection_setup
        self._is_memory_db = is_memory_db
        self._capacity_limiter = anyio.CapacityLimiter(max_connections)
        self._freelist: list[sqlite3.Connection] = []
        self._closed = False
        # We shouldn't need this semaphore, but it is sometimes necessary to avoid an anyio capacity
        # limiter bug
        self._thread_semaphore = threading.BoundedSemaphore(max_connections)

    def _create_connection(self) -> sqlite3.Connection:
        """Create a new sqlite3 connection."""
        # sqlite3 handles URI mode automatically for file: URIs
        # Check if db_path already has file: prefix (e.g., for memory databases)
        use_uri = self._db_path.startswith("file:")

        # Create connection with manual transaction control
        conn = sqlite3.connect(
            self._db_path, isolation_level=None, check_same_thread=False, uri=use_uri
        )
        if self._connection_setup is not None:
            self._connection_setup(conn)
        return conn

    def _get_connection_sync(self) -> sqlite3.Connection:
        """Get a connection from freelist or create new (sync, called from thread)."""
        logger.debug(f"(starting to fetch a connection)")
        if self._closed:
            raise RuntimeError("ConnectionPool is closed")
        if self._freelist:
            conn = self._freelist.pop()
            logger.debug(f"[conn {id(conn):x}] Re-using existing connection for {self._db_path}")
            return conn
        conn = self._create_connection()
        logger.debug(f"[conn {id(conn):x}] Created new connection for {self._db_path}")
        return conn

    def _return_connection_sync(self, conn: sqlite3.Connection) -> None:
        """Return connection to freelist or close it (sync, called from thread)."""
        logger.debug(f"(starting to return a connection)")
        # Rollback any uncommitted transaction
        if conn.in_transaction:
            logger.warning(
                "Connection returned to pool with uncommitted transaction; rolling back. "
                "Operations should explicitly commit or rollback their transactions."
            )
            conn.execute("ROLLBACK")
        assert not conn.in_transaction

        if self._closed:
            conn.close()
            logger.debug(f"[conn {id(conn):x}] Closed connection for {self._db_path}")
        else:
            self._freelist.append(conn)
            logger.debug(f"[conn {id(conn):x}] Returned connection to pool for {self._db_path}")

    async def run_sync_with_connection(
        self,
        fn: Callable[Concatenate[sqlite3.Connection, P], R],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        """Run a sync function with a pooled connection in a thread.

        The function receives a sqlite3.Connection as its first argument,
        followed by any additional positional and keyword arguments.

        The connection is automatically returned to the pool after the
        function completes (or on error).

        Args:
            fn: A sync function that takes a Connection as first arg.
            *args: Additional positional arguments for fn.
            **kwargs: Additional keyword arguments for fn.

        Returns:
            The return value of fn.

        Raises:
            RuntimeError: If the pool is closed.
        """
        if self._closed:
            raise RuntimeError("ConnectionPool is closed")

        import uuid

        reqid = str(uuid.uuid4())

        if logger.isEnabledFor(logging.DEBUG):
            # Look up the name and line of the caller's stack frame
            frame = inspect.currentframe()
            for i in range(1):
                if frame is None:
                    raise RuntimeError("can't find frame")
                frame = frame.f_back
            if frame is None:
                raise RuntimeError("No current frame")

            caller_file = frame.f_code.co_filename
            caller_name = frame.f_code.co_name
            caller_line = frame.f_lineno
            logger.debug(
                f"[req {reqid}] Requested to run sync at {caller_name} at"
                f" {caller_file}:{caller_line} (pool {id(self):x}, limiter {self._capacity_limiter.statistics()})"
            )
        else:
            caller_name = "(unknown)"
            caller_line = None
            caller_file = "(unknown)"

        def _inner() -> R:
            logger.debug(
                f"[req {reqid} inner started] (limiter {self._capacity_limiter.statistics()})"
            )
            try:
                with self._thread_semaphore:
                    logger.debug(f"[req {reqid} got thread semaphore]")
                    conn = self._get_connection_sync()
                    logger.debug(f"[req {reqid} got conn {id(conn):x}]")
                    try:
                        return fn(conn, *args, **kwargs)
                    finally:
                        self._return_connection_sync(conn)
            finally:
                logger.debug(
                    f"[req {reqid} finished] (limiter {self._capacity_limiter.statistics()})"
                )

        try:
            logger.debug(f"[req {reqid} entering run_sync call]")
            return await anyio.to_thread.run_sync(
                _inner, abandon_on_cancel=True, limiter=self._capacity_limiter
            )
        except BaseException as e:
            logger.debug(f"[req {reqid} failed with {type(e)}: {e}]")
            raise
        finally:
            logger.debug(f"[req {reqid} finished]")

    def close(self) -> None:
        """Close all idle connections and mark pool as closed.

        After closing, any attempt to use the pool will raise RuntimeError.
        Connections currently in use will be closed when returned.
        """
        self._closed = True
        while self._freelist:
            conn = self._freelist.pop()
            conn.close()

    def __enter__(self) -> ConnectionPool:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


@dataclasses.dataclass
class ConnectionPoolGroup:
    """A pair of connection pools for read and write operations.

    Provides separate pools for readers and writers to take advantage
    of SQLite's WAL mode, which allows concurrent reads during writes.

    Should be created via setup_database_and_create_pool_group() in
    sqlite_ops.py, which handles schema initialization before creating
    the pools.

    Attributes:
        write_pool: ConnectionPool for write operations.
        read_pool: ConnectionPool for read operations.
        memory_keep_alive_connection: Connection to keep the memory database alive.
            This is only used for memory databases, and is None for other databases.
    """

    write_pool: ConnectionPool
    read_pool: ConnectionPool
    memory_keep_alive_connection: sqlite3.Connection | None = None

    def close(self) -> None:
        """Close both pools."""
        self.write_pool.close()
        self.read_pool.close()
        if self.memory_keep_alive_connection is not None:
            self.memory_keep_alive_connection.close()

    def __enter__(self) -> ConnectionPoolGroup:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


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
                    logger.warning(
                        f"[conn {id(conn):x}] conn is busy, attempt {i+1}/{MAX_RETRIES} (t={time.time()}) (for {caller_name} at {caller_file}:{caller_line} (failed to roll back txn start)"
                    )
                else:
                    logger.warning(
                        f"[conn {id(conn):x}] conn is busy, attempt {i+1}/{MAX_RETRIES} (t={time.time()}) (for {caller_name} at {caller_file}:{caller_line} (rolled back txn start)"
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
        logger.debug(
            f"[conn {id(conn):x}] Entered transaction {transaction_mode} in {caller_name} at"
            f" {caller_file}:{caller_line}"
        )
        yield
        logger.debug(
            f"[conn {id(conn):x}] About to commit transaction {transaction_mode} in {caller_name} at"
            f" {caller_file}:{caller_line}"
        )
        conn.execute("COMMIT")
    except BaseException as e:
        # Only rollback if we're still in a transaction
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if caller_line is not None:
            logger.debug(
                f"[conn {id(conn):x}] Transaction in {caller_name} at {caller_file}:{caller_line} failed with {type(e)}"
            )
        raise
    else:
        logger.debug(
            f"[conn {id(conn):x}] Transaction in {caller_name} at {caller_file}:{caller_line} committed"
        )
