"""Tests for fxtr2/data/sqlite_store/sqlite_util.py."""

from __future__ import annotations

import sqlite3
import threading
import time

import anyio
import pytest

from fxtr2.data.sqlite_store.sqlite_util import ConnectionPool, ConnectionPoolGroup, transaction


def create_test_schema(conn: sqlite3.Connection) -> None:
    """Create a simple test schema for pool tests."""
    conn.execute("CREATE TABLE IF NOT EXISTS test_data (id INTEGER PRIMARY KEY, value TEXT)")


def setup_foreign_keys(conn: sqlite3.Connection) -> None:
    """Example connection setup callback."""
    conn.execute("PRAGMA foreign_keys = ON")


class TestConnectionPool:
    """Tests for ConnectionPool basic functionality."""

    @pytest.mark.anyio
    async def test_run_sync_with_connection_basic(self, tmp_path):
        """Basic operation: run a sync function with a connection."""
        db_path = str(tmp_path / "test.db")

        # Create database first
        conn = sqlite3.connect(db_path, isolation_level=None)
        create_test_schema(conn)
        conn.close()

        pool = ConnectionPool(db_path, max_connections=2)
        try:

            def insert_data(conn: sqlite3.Connection, value: str) -> int:
                with transaction(conn, "IMMEDIATE"):
                    cursor = conn.execute("INSERT INTO test_data (value) VALUES (?)", (value,))
                    return cursor.lastrowid

            row_id = await pool.run_sync_with_connection(insert_data, "hello")
            assert row_id == 1

            # Verify data was inserted
            def read_data(conn: sqlite3.Connection, row_id: int) -> str | None:
                cursor = conn.execute("SELECT value FROM test_data WHERE id = ?", (row_id,))
                row = cursor.fetchone()
                return row[0] if row else None

            value = await pool.run_sync_with_connection(read_data, row_id)
            assert value == "hello"
        finally:
            pool.close()

    @pytest.mark.anyio
    async def test_connection_setup_callback(self, tmp_path):
        """Connection setup callback is called for each new connection."""
        db_path = str(tmp_path / "test.db")
        setup_calls: list[int] = []

        def track_setup(conn: sqlite3.Connection) -> None:
            setup_calls.append(threading.current_thread().ident)
            conn.execute("PRAGMA foreign_keys = ON")

        # Create database first
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.close()

        pool = ConnectionPool(db_path, max_connections=2, connection_setup=track_setup)
        try:

            def check_foreign_keys(conn: sqlite3.Connection) -> int:
                cursor = conn.execute("PRAGMA foreign_keys")
                return cursor.fetchone()[0]

            result = await pool.run_sync_with_connection(check_foreign_keys)
            assert result == 1  # foreign_keys is ON
            assert len(setup_calls) == 1
        finally:
            pool.close()

    @pytest.mark.anyio
    async def test_freelist_reuses_connections(self, tmp_path):
        """Connections are returned to freelist and reused."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.close()

        pool = ConnectionPool(db_path, max_connections=1)
        try:
            connection_ids: list[int] = []

            def get_connection_id(conn: sqlite3.Connection) -> int:
                connection_ids.append(id(conn))
                return id(conn)

            # Run twice sequentially - should reuse the same connection
            await pool.run_sync_with_connection(get_connection_id)
            await pool.run_sync_with_connection(get_connection_id)

            assert len(connection_ids) == 2
            assert connection_ids[0] == connection_ids[1], "Connection should be reused"
        finally:
            pool.close()

    @pytest.mark.anyio
    async def test_capacity_limiter_bounds_concurrency(self, tmp_path):
        """Capacity limiter prevents exceeding max_connections."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.close()

        max_connections = 2
        pool = ConnectionPool(db_path, max_connections=max_connections)
        try:
            concurrent_count = 0
            max_concurrent = 0
            lock = threading.Lock()

            def slow_operation(conn: sqlite3.Connection) -> None:
                nonlocal concurrent_count, max_concurrent
                with lock:
                    concurrent_count += 1
                    max_concurrent = max(max_concurrent, concurrent_count)

                time.sleep(0.05)  # Simulate work

                with lock:
                    concurrent_count -= 1

            # Launch more tasks than max_connections
            async with anyio.create_task_group() as tg:
                for _ in range(5):
                    tg.start_soon(pool.run_sync_with_connection, slow_operation)

            assert (
                max_concurrent <= max_connections
            ), f"Max concurrent ({max_concurrent}) exceeded limit ({max_connections})"
        finally:
            pool.close()

    @pytest.mark.anyio
    async def test_rollback_uncommitted_transaction_on_return(self, tmp_path, caplog):
        """Uncommitted transactions are rolled back when connection is returned."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path, isolation_level=None)
        create_test_schema(conn)
        conn.close()

        pool = ConnectionPool(db_path, max_connections=1)
        try:

            def insert_without_commit(conn: sqlite3.Connection) -> None:
                conn.execute("BEGIN")
                conn.execute("INSERT INTO test_data (value) VALUES (?)", ("uncommitted",))
                # Don't commit - let the pool handle rollback

            with caplog.at_level("WARNING", logger="fxtr2.data.sqlite_store.sqlite_util"):
                await pool.run_sync_with_connection(insert_without_commit)

            # Verify warning was logged
            assert any(
                "uncommitted transaction" in record.message for record in caplog.records
            ), "Expected warning about uncommitted transaction"

            # Verify data was NOT persisted
            def count_rows(conn: sqlite3.Connection) -> int:
                cursor = conn.execute("SELECT COUNT(*) FROM test_data")
                return cursor.fetchone()[0]

            count = await pool.run_sync_with_connection(count_rows)
            assert count == 0, "Uncommitted transaction should have been rolled back"
        finally:
            pool.close()

    @pytest.mark.anyio
    async def test_close_prevents_new_operations(self, tmp_path):
        """Closed pool raises RuntimeError on new operations."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.close()

        pool = ConnectionPool(db_path, max_connections=1)
        pool.close()

        def noop(conn: sqlite3.Connection) -> None:
            pass

        with pytest.raises(RuntimeError, match="closed"):
            await pool.run_sync_with_connection(noop)

    @pytest.mark.anyio
    async def test_close_closes_idle_connections(self, tmp_path):
        """Closing pool closes all idle connections in freelist."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.close()

        pool = ConnectionPool(db_path, max_connections=2)

        # Run some ops to populate freelist
        def noop(conn: sqlite3.Connection) -> None:
            pass

        await pool.run_sync_with_connection(noop)
        await pool.run_sync_with_connection(noop)

        assert len(pool._freelist) > 0, "Freelist should have connections"

        pool.close()
        assert len(pool._freelist) == 0, "Freelist should be empty after close"

    @pytest.mark.anyio
    async def test_context_manager(self, tmp_path):
        """Pool works as context manager."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.close()

        with ConnectionPool(db_path, max_connections=1) as pool:

            def noop(conn: sqlite3.Connection) -> None:
                pass

            await pool.run_sync_with_connection(noop)

        assert pool._closed, "Pool should be closed after exiting context"

    @pytest.mark.anyio
    async def test_pool_can_read_and_write(self, tmp_path):
        """Pool can both read and write."""
        db_path = str(tmp_path / "test.db")

        # Create database and table first
        conn = sqlite3.connect(db_path, isolation_level=None)
        create_test_schema(conn)
        conn.execute("INSERT INTO test_data (value) VALUES (?)", ("initial",))
        conn.close()

        pool = ConnectionPool(db_path, max_connections=1)
        try:
            # Read should work
            def read_count(conn: sqlite3.Connection) -> int:
                cursor = conn.execute("SELECT COUNT(*) FROM test_data")
                return cursor.fetchone()[0]

            count = await pool.run_sync_with_connection(read_count)
            assert count == 1

            # Write should also work
            def write_data(conn: sqlite3.Connection) -> None:
                with transaction(conn, "IMMEDIATE"):
                    conn.execute("INSERT INTO test_data (value) VALUES (?)", ("new",))

            await pool.run_sync_with_connection(write_data)

            # Verify write worked
            count = await pool.run_sync_with_connection(read_count)
            assert count == 2
        finally:
            pool.close()


class TestConnectionPoolCancellation:
    """Tests for cancellation behavior."""

    @pytest.mark.anyio
    async def test_cancellation_rolls_back_pending_transaction(self, tmp_path):
        """Cancellation during second transaction rolls back, first commits.

        This test verifies that:
        1. A completed transaction is committed and persists
        2. A transaction in progress when cancellation occurs is rolled back as long as
           `check_cancelled` is called before committing

        The test uses a sync operation that:
        - Commits a first transaction
        - Starts a second transaction
        - Sleeps (to allow cancellation to happen)
        - Checks for cancellation before committing the second transaction

        When the async side cancels during the sleep, the thread is abandoned
        (abandon_on_cancel=True), and when check_cancelled() is called, it
        raises an exception. Since we're inside a `with conn:` block (wrapped by convenience
        wrapper `transaction`), sqlite3 automatically rolls back the transaction when the exception
        propagates.
        """
        db_path = str(tmp_path / "test.db")

        # Setup database
        conn = sqlite3.connect(db_path, isolation_level=None)
        create_test_schema(conn)
        conn.close()

        pool = ConnectionPool(db_path, max_connections=1)
        first_txn_done = anyio.Event()
        operation_completed = anyio.Event()
        thread_done = anyio.Event()

        try:

            def two_transactions(conn: sqlite3.Connection) -> None:
                try:
                    # First transaction - commits immediately
                    with transaction(conn, "IMMEDIATE"):
                        conn.execute("INSERT INTO test_data (value) VALUES (?)", ("first",))
                    # Signal that first txn is done
                    anyio.from_thread.run_sync(first_txn_done.set)

                    # Second transaction - uses context manager for automatic rollback on exception
                    with transaction(conn, "IMMEDIATE"):
                        conn.execute("INSERT INTO test_data (value) VALUES (?)", ("second",))
                        time.sleep(0.15)  # Sleep to allow cancellation

                        # Check for cancellation before the context manager commits
                        # This will raise if the async side was cancelled, causing
                        # the context manager to rollback instead of commit
                        anyio.from_thread.check_cancelled()

                    anyio.from_thread.run_sync(operation_completed.set)
                finally:
                    anyio.from_thread.run_sync(thread_done.set)

            async with anyio.create_task_group() as tg:
                tg.start_soon(pool.run_sync_with_connection, two_transactions)

                # Wait for first transaction to complete
                await first_txn_done.wait()
                # Wait a bit for second transaction to start
                await anyio.sleep(0.05)
                # Cancel the task group - this abandons the thread
                tg.cancel_scope.cancel()

            # Verify: we exited the task group BEFORE the thread finished
            # (because abandon_on_cancel=True means we don't wait for it)
            assert not thread_done.is_set(), (
                "Thread should still be running after task group exits - "
                "abandon_on_cancel should have let us exit immediately"
            )

            # Now wait for the thread to actually finish (with timeout)
            with anyio.fail_after(1.0):
                await thread_done.wait()

            # Verify: first transaction committed, second rolled back
            def get_values(conn: sqlite3.Connection) -> list[str]:
                cursor = conn.execute("SELECT value FROM test_data ORDER BY id")
                return [row[0] for row in cursor]

            values = await pool.run_sync_with_connection(get_values)
            assert values == ["first"], f"Expected only 'first' to be committed, got {values}"

            # Verify the operation did not complete normally
            assert (
                not operation_completed.is_set()
            ), "Operation should not have completed - cancellation should have interrupted it"
        finally:
            pool.close()


class TestConnectionPoolGroup:
    """Tests for ConnectionPoolGroup."""

    @pytest.mark.anyio
    async def test_separate_read_write_pools(self, tmp_path):
        """Read and write pools are separate."""
        db_path = str(tmp_path / "test.db")

        # Create database
        conn = sqlite3.connect(db_path, isolation_level=None)
        create_test_schema(conn)
        conn.close()

        write_pool = ConnectionPool(db_path, max_connections=1)
        read_pool = ConnectionPool(db_path, max_connections=3)
        group = ConnectionPoolGroup(write_pool=write_pool, read_pool=read_pool)

        try:
            # Write via write pool
            def insert(conn: sqlite3.Connection) -> None:
                with transaction(conn, "IMMEDIATE"):
                    conn.execute("INSERT INTO test_data (value) VALUES (?)", ("test",))

            await group.write_pool.run_sync_with_connection(insert)

            # Read via read pool
            def read(conn: sqlite3.Connection) -> int:
                cursor = conn.execute("SELECT COUNT(*) FROM test_data")
                return cursor.fetchone()[0]

            count = await group.read_pool.run_sync_with_connection(read)
            assert count == 1

            # Read pool can also write (readonly is not enforced)
            def write_via_read_pool(conn: sqlite3.Connection) -> None:
                with transaction(conn, "IMMEDIATE"):
                    conn.execute("INSERT INTO test_data (value) VALUES (?)", ("via_read_pool",))

            await group.read_pool.run_sync_with_connection(write_via_read_pool)

            # Verify write worked
            count = await group.read_pool.run_sync_with_connection(read)
            assert count == 2
        finally:
            group.close()

    @pytest.mark.anyio
    async def test_context_manager(self, tmp_path):
        """ConnectionPoolGroup works as context manager."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.close()

        write_pool = ConnectionPool(db_path, max_connections=1)
        read_pool = ConnectionPool(db_path, max_connections=2)

        with ConnectionPoolGroup(write_pool=write_pool, read_pool=read_pool) as group:
            assert not group.write_pool._closed
            assert not group.read_pool._closed

        assert group.write_pool._closed, "Write pool should be closed"
        assert group.read_pool._closed, "Read pool should be closed"

    @pytest.mark.anyio
    async def test_close_closes_both_pools(self, tmp_path):
        """Closing group closes both pools."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.close()

        write_pool = ConnectionPool(db_path, max_connections=1)
        read_pool = ConnectionPool(db_path, max_connections=2)
        group = ConnectionPoolGroup(write_pool=write_pool, read_pool=read_pool)

        group.close()

        assert write_pool._closed
        assert read_pool._closed
