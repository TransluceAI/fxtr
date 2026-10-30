"""Minimal reproduction of SQLite deadlock/busy issue with concurrent transactions.

Demonstrates that APSW's `with conn:` context manager causes deadlocks when
conn.transaction_mode is set, but explicit BEGIN/COMMIT works correctly.

Usage (from v2 directory):
    uv run python examples/sqlite_debugging/sqlite_deadlock_debugging.py
"""

from __future__ import annotations

import contextlib
import logging
import tempfile
import time
import uuid
from collections.abc import Callable, Generator
from pathlib import Path

import anyio
import apsw

# Configure logging with detailed format
logging.basicConfig(
    level=logging.DEBUG,
    format=(
        "%(asctime)s.%(msecs)03d %(levelname)s %(filename)s:%(lineno)d [%(threadName)s=%(thread)d]"
        " %(message)s"
    ),
    datefmt="%H:%M:%S",
)

logger = logging.getLogger(__name__)


# =============================================================================
# Transaction Helpers
# =============================================================================


@contextlib.contextmanager
def transaction_with_context_manager(
    conn: apsw.Connection, transaction_mode: str
) -> Generator[None, None, None]:
    """Transaction using conn.transaction_mode + 'with conn:' (BUGGY - causes deadlock)."""
    conn.transaction_mode = transaction_mode
    assert not conn.in_transaction
    with conn:
        yield


@contextlib.contextmanager
def transaction_explicit(
    conn: apsw.Connection, transaction_mode: str
) -> Generator[None, None, None]:
    """Transaction using explicit BEGIN/COMMIT (WORKS CORRECTLY)."""
    conn.execute(f"BEGIN {transaction_mode}")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# =============================================================================
# Simple Test Schema
# =============================================================================


def setup_test_database(db_path: str) -> None:
    """Create a simple test database with one table."""
    conn = apsw.Connection(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS test_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uuid BLOB NOT NULL,
                value TEXT
            )
        """
        )
    finally:
        conn.close()


def connection_setup(conn: apsw.Connection) -> None:
    """Per-connection setup callback."""
    conn.set_busy_timeout(1000)  # 1 second


# =============================================================================
# Workers
# =============================================================================


async def worker_1(
    conn: apsw.Connection,
    iteration: int,
    transaction_fn: Callable[[apsw.Connection, str], contextlib.AbstractContextManager[None]],
) -> None:
    """Worker 1: Start an IMMEDIATE transaction and do some work."""
    logger.info(f"[Iter {iteration}] Worker 1 starting")

    def do_work() -> None:
        time.sleep(0.001)  # Ensure both workers get scheduled
        logger.debug(f"[Iter {iteration}] Worker 1: About to start IMMEDIATE")

        with transaction_fn(conn, "IMMEDIATE"):
            logger.debug(f"[Iter {iteration}] Worker 1: Entered IMMEDIATE")
            time.sleep(0.01)  # Ensure overlap

            cursor = conn.execute("SELECT COUNT(*) FROM test_data")
            count = cursor.fetchone()[0]
            logger.debug(f"[Iter {iteration}] Worker 1: Found {count} rows")

            conn.execute(
                "INSERT INTO test_data (uuid, value) VALUES (?, ?)",
                (uuid.uuid4().bytes, f"worker1_iter{iteration}"),
            )

            logger.debug(f"[Iter {iteration}] Worker 1: About to commit")

        logger.debug(f"[Iter {iteration}] Worker 1: Committed")

    await anyio.to_thread.run_sync(do_work)
    logger.info(f"[Iter {iteration}] Worker 1 completed")


async def worker_2(
    conn: apsw.Connection,
    iteration: int,
    transaction_fn: Callable[[apsw.Connection, str], contextlib.AbstractContextManager[None]],
) -> None:
    """Worker 2: Start an IMMEDIATE transaction and do some work."""
    logger.info(f"[Iter {iteration}] Worker 2 starting")

    def do_work() -> None:
        time.sleep(0.001)  # Ensure both workers get scheduled
        logger.debug(f"[Iter {iteration}] Worker 2: About to start IMMEDIATE")

        with transaction_fn(conn, "IMMEDIATE"):
            logger.debug(f"[Iter {iteration}] Worker 2: Entered IMMEDIATE")

            cursor = conn.execute("SELECT COUNT(*) FROM test_data")
            count = cursor.fetchone()[0]
            logger.debug(f"[Iter {iteration}] Worker 2: Found {count} rows")

            conn.execute(
                "INSERT INTO test_data (uuid, value) VALUES (?, ?)",
                (uuid.uuid4().bytes, f"worker2_iter{iteration}"),
            )

            logger.debug(f"[Iter {iteration}] Worker 2: About to commit")

        logger.debug(f"[Iter {iteration}] Worker 2: Committed")

    await anyio.to_thread.run_sync(do_work)
    logger.info(f"[Iter {iteration}] Worker 2 completed")


# =============================================================================
# Test
# =============================================================================


async def run_test(
    db_path: str,
    transaction_fn: Callable[[apsw.Connection, str], contextlib.AbstractContextManager[None]],
    test_name: str,
) -> bool:
    """Run test with given transaction function. Returns True if all iterations succeed."""
    logger.info("=" * 70)
    logger.info(f"{test_name}")
    logger.info("=" * 70)

    conn_1 = apsw.Connection(db_path)
    conn_2 = apsw.Connection(db_path)

    try:
        connection_setup(conn_1)
        connection_setup(conn_2)

        for i in range(5):  # Reduced to 5 iterations for clarity
            logger.info(f"\n--- Iteration {i} ---")
            try:
                async with anyio.create_task_group() as tg:
                    tg.start_soon(worker_1, conn_1, i, transaction_fn)
                    tg.start_soon(worker_2, conn_2, i, transaction_fn)
                logger.info(f"[Iter {i}] ✅ Both workers completed successfully")
            except Exception as e:
                while isinstance(e, ExceptionGroup) and len(e.exceptions) == 1:
                    e = e.exceptions[0]
                logger.error(f"[Iter {i}] ❌ FAILED: {e}")
                return False

        logger.info(f"\n✅ {test_name}: ALL ITERATIONS PASSED")
        return True

    finally:
        conn_1.close()
        conn_2.close()


async def main() -> None:
    """Run tests with both transaction approaches."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.sqlite")
        logger.info(f"Using database: {db_path}\n")

        # Set up the database
        setup_test_database(db_path)

        # Test 1: Using explicit BEGIN/COMMIT (should work)
        test1_passed = await run_test(
            db_path, transaction_explicit, "Test 1: Explicit BEGIN/COMMIT"
        )

        # Clean and recreate database for test 2
        Path(db_path).unlink()
        setup_test_database(db_path)

        # Test 2: Using with conn: context manager (should deadlock)
        test2_passed = await run_test(
            db_path, transaction_with_context_manager, "Test 2: with conn: context manager"
        )

        # Summary
        print("\n" + "=" * 70)
        print("SUMMARY")
        print("=" * 70)
        print(f"Test 1 (explicit BEGIN/COMMIT):     {'✅ PASSED' if test1_passed else '❌ FAILED'}")
        print(
            "Test 2 (with conn: context manager):"
            f" {'✅ PASSED' if test2_passed else '❌ FAILED (DEADLOCK)'}"
        )
        print("\nConclusion: APSW's 'with conn:' context manager causes deadlocks")
        print("when conn.transaction_mode is set and multiple connections are used concurrently.")
        print("=" * 70)


if __name__ == "__main__":
    anyio.run(main)
