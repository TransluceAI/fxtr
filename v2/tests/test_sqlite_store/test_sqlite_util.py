"""Tests for fxtr2/data/sqlite_store/sqlite_util.py."""

from __future__ import annotations

import sqlite3
import threading
import time

import anyio
import pytest

from fxtr2.data.sqlite_store.sqlite_util import ConnectionWorkerPool, transaction


def create_test_schema(conn: sqlite3.Connection) -> None:
    """Create a simple test schema for pool tests."""
    conn.execute("CREATE TABLE IF NOT EXISTS test_data (id INTEGER PRIMARY KEY, value TEXT)")


def setup_foreign_keys(conn: sqlite3.Connection) -> None:
    """Example connection setup callback."""
    conn.execute("PRAGMA foreign_keys = ON")


# Note: TestConnectionPool, TestConnectionPoolCancellation, and TestConnectionPoolGroup
# tests have been removed in v4 (ConnectionPool/ConnectionPoolGroup no longer used).


class TestConnectionWorkerPool:
    """Tests for ConnectionWorkerPool with persistent worker threads."""

    @pytest.mark.anyio
    async def test_basic_operation(self, tmp_path):
        """Basic: submit work, get result."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path, isolation_level=None)
        create_test_schema(conn)
        conn.close()

        pool = ConnectionWorkerPool(db_path, max_connections=1)
        try:
            async with anyio.create_task_group() as tg:
                await tg.start(pool.run_worker_thread_forever)

                def insert_data(conn: sqlite3.Connection, value: str) -> int:
                    with transaction(conn, "IMMEDIATE"):
                        cursor = conn.execute("INSERT INTO test_data (value) VALUES (?)", (value,))
                        return cursor.lastrowid

                row_id = await pool.run_sync_with_connection(insert_data, "hello")
                assert row_id == 1

                def read_data(conn: sqlite3.Connection, row_id: int) -> str | None:
                    cursor = conn.execute("SELECT value FROM test_data WHERE id = ?", (row_id,))
                    row = cursor.fetchone()
                    return row[0] if row else None

                value = await pool.run_sync_with_connection(read_data, row_id)
                assert value == "hello"

                await pool.aclose()
        finally:
            pool.close()

    @pytest.mark.anyio
    async def test_multiple_workers_concurrency(self, tmp_path):
        """Multiple workers can process tasks concurrently."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.close()

        pool = ConnectionWorkerPool(db_path, max_connections=3)
        try:
            num_workers = 3
            concurrent_count = 0
            max_concurrent = 0
            lock = threading.Lock()

            def slow_operation(conn: sqlite3.Connection) -> None:
                nonlocal concurrent_count, max_concurrent
                with lock:
                    concurrent_count += 1
                    max_concurrent = max(max_concurrent, concurrent_count)
                time.sleep(0.05)
                with lock:
                    concurrent_count -= 1

            async with anyio.create_task_group() as tg:
                for _ in range(num_workers):
                    await tg.start(pool.run_worker_thread_forever)

                # Submit more tasks than workers
                async with anyio.create_task_group() as submit_tg:
                    for _ in range(6):
                        submit_tg.start_soon(pool.run_sync_with_connection, slow_operation)

                assert max_concurrent >= 2, (
                    f"Expected at least 2 concurrent operations with {num_workers} workers, "
                    f"got {max_concurrent}"
                )
                assert max_concurrent <= num_workers

                await pool.aclose()
        finally:
            pool.close()

    @pytest.mark.anyio
    async def test_connection_reuse(self, tmp_path):
        """Each worker holds a single connection for its lifetime."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.close()

        pool = ConnectionWorkerPool(db_path, max_connections=1)
        try:
            connection_ids: list[int] = []

            def get_conn_id(conn: sqlite3.Connection) -> int:
                connection_ids.append(id(conn))
                return id(conn)

            async with anyio.create_task_group() as tg:
                # Single worker — should reuse the same connection
                await tg.start(pool.run_worker_thread_forever)

                await pool.run_sync_with_connection(get_conn_id)
                await pool.run_sync_with_connection(get_conn_id)
                await pool.run_sync_with_connection(get_conn_id)

                assert len(connection_ids) == 3
                assert all(
                    cid == connection_ids[0] for cid in connection_ids
                ), "Single worker should always use the same connection"

                await pool.aclose()
        finally:
            pool.close()

    @pytest.mark.anyio
    async def test_connection_setup_callback(self, tmp_path):
        """Connection setup callback is called for each worker's connection."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.close()

        setup_called = threading.Event()

        def setup(conn: sqlite3.Connection) -> None:
            conn.execute("PRAGMA foreign_keys = ON")
            setup_called.set()

        pool = ConnectionWorkerPool(db_path, max_connections=1, connection_setup=setup)
        try:
            async with anyio.create_task_group() as tg:
                await tg.start(pool.run_worker_thread_forever)

                def check_fk(conn: sqlite3.Connection) -> int:
                    cursor = conn.execute("PRAGMA foreign_keys")
                    return cursor.fetchone()[0]

                result = await pool.run_sync_with_connection(check_fk)
                assert result == 1
                assert setup_called.is_set()

                await pool.aclose()
        finally:
            pool.close()

    @pytest.mark.anyio
    async def test_exception_propagation(self, tmp_path):
        """Exceptions from work functions propagate to the caller."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.close()

        pool = ConnectionWorkerPool(db_path, max_connections=1)
        try:
            async with anyio.create_task_group() as tg:
                await tg.start(pool.run_worker_thread_forever)

                def failing_op(conn: sqlite3.Connection) -> None:
                    raise ValueError("test error")

                with pytest.raises(ValueError, match="test error"):
                    await pool.run_sync_with_connection(failing_op)

                # Worker should still be alive after the error
                def ok_op(conn: sqlite3.Connection) -> str:
                    return "still alive"

                result = await pool.run_sync_with_connection(ok_op)
                assert result == "still alive"

                await pool.aclose()
        finally:
            pool.close()

    @pytest.mark.anyio
    async def test_uncommitted_transaction_rolled_back(self, tmp_path, caplog):
        """Worker rolls back uncommitted transactions left by work functions."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path, isolation_level=None)
        create_test_schema(conn)
        conn.close()

        pool = ConnectionWorkerPool(db_path, max_connections=1)
        try:
            async with anyio.create_task_group() as tg:
                await tg.start(pool.run_worker_thread_forever)

                def bad_op(conn: sqlite3.Connection) -> None:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("INSERT INTO test_data (value) VALUES (?)", ("uncommitted",))
                    # Don't commit

                with caplog.at_level("WARNING", logger="fxtr2.data.sqlite_store.sqlite_util"):
                    await pool.run_sync_with_connection(bad_op)

                assert any("uncommitted transaction" in r.message for r in caplog.records)

                # Verify data was NOT persisted
                def count_rows(conn: sqlite3.Connection) -> int:
                    cursor = conn.execute("SELECT COUNT(*) FROM test_data")
                    return cursor.fetchone()[0]

                count = await pool.run_sync_with_connection(count_rows)
                assert count == 0

                await pool.aclose()
        finally:
            pool.close()

    @pytest.mark.anyio
    async def test_close_rejects_new_work(self, tmp_path):
        """Closed pool raises RuntimeError on new submissions."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.close()

        pool = ConnectionWorkerPool(db_path, max_connections=1)
        pool._active_workers = 1  # Pretend there's a worker to get past that check
        pool.close()

        def noop(conn: sqlite3.Connection) -> None:
            pass

        with pytest.raises(RuntimeError, match="closed"):
            await pool.run_sync_with_connection(noop)

    @pytest.mark.anyio
    async def test_no_workers_raises(self, tmp_path):
        """Submitting work with no active workers raises RuntimeError."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.close()

        pool = ConnectionWorkerPool(db_path, max_connections=1)

        def noop(conn: sqlite3.Connection) -> None:
            pass

        with pytest.raises(RuntimeError, match="No active worker"):
            await pool.run_sync_with_connection(noop)

        pool.close()

    @pytest.mark.anyio
    async def test_close_drains_queue(self, tmp_path):
        """Close drains pending items from the queue with errors."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.close()

        pool = ConnectionWorkerPool(db_path, max_connections=1)

        # Manually push items onto the queue without workers to process them
        events: list[anyio.Event] = []
        result_boxes: list[list] = []
        for _ in range(3):
            evt = anyio.Event()
            rbox: list = []
            events.append(evt)
            result_boxes.append(rbox)
            pool._queue.put((lambda conn: None, (), rbox, evt, [False]))

        pool.close()

        # All result boxes should have error results
        for rbox in result_boxes:
            assert len(rbox) == 2
            assert rbox[0] == 1  # _RESULT_ERR
            assert isinstance(rbox[1], RuntimeError)

    @pytest.mark.anyio
    async def test_cancelled_flag_skips_work(self, tmp_path):
        """Work items with cancelled_flag set are skipped by the worker."""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path, isolation_level=None)
        create_test_schema(conn)
        conn.close()

        pool = ConnectionWorkerPool(db_path, max_connections=1)
        executed: list[str] = []

        try:
            async with anyio.create_task_group() as tg:
                await tg.start(pool.run_worker_thread_forever)

                # Manually put a pre-cancelled item and a normal item on the queue
                cancelled_event = anyio.Event()
                cancelled_box: list = []
                pool._queue.put(
                    (
                        lambda conn, label: executed.append(label),
                        ("should_not_run",),
                        cancelled_box,
                        cancelled_event,
                        [True],  # Already cancelled
                    )
                )

                # Now submit a normal item
                def track_op(conn: sqlite3.Connection, label: str) -> str:
                    executed.append(label)
                    return label

                result = await pool.run_sync_with_connection(track_op, "should_run")
                assert result == "should_run"
                assert executed == ["should_run"]

                await pool.aclose()
        finally:
            pool.close()
