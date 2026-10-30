"""Minimal reproduction of SQLite deadlock/busy issue with concurrent transactions.

Reproduces issue where two IMMEDIATE transactions from separate connections
seem to deadlock, with both threads blocking for the busy timeout duration.

Usage (from v2 directory):
    uv run python examples/sqlite_debugging/sqlite_deadlock_debugging.py
"""

from __future__ import annotations

import logging
import tempfile
import time
import uuid
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
# Simple Test Schema
# =============================================================================


def setup_test_database(db_path: str) -> None:
    """Create a simple test database with one table."""
    conn = apsw.Connection(db_path)
    try:
        # Set WAL mode
        conn.execute("PRAGMA journal_mode = WAL")

        # Create simple test table
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
    # Set busy timeout
    conn.set_busy_timeout(1000)  # 1 second
    # Enable foreign keys
    conn.execute("PRAGMA foreign_keys = ON")


# =============================================================================
# Workers
# =============================================================================


async def worker_1(conn: apsw.Connection, iteration: int) -> None:
    """Worker 1: Use a raw APSW connection directly."""
    logger.info(f"[Iter {iteration}] Worker 1 starting")

    def do_work() -> None:
        # Small sleep to ensure both workers get scheduled
        time.sleep(0.001)
        logger.debug(f"[Iter {iteration}] Worker 1: About to start IMMEDIATE transaction")
        conn.transaction_mode = "IMMEDIATE"
        with conn:
            logger.debug(f"[Iter {iteration}] Worker 1: Entered IMMEDIATE transaction")

            # Add delay to ensure overlap
            time.sleep(0.01)

            # Do some simple DB work
            cursor = conn.execute("SELECT COUNT(*) FROM test_data")
            row = cursor.fetchone()
            count = row[0] if row else 0
            logger.debug(f"[Iter {iteration}] Worker 1: Found {count} rows")

            # Insert a row
            conn.execute(
                "INSERT INTO test_data (uuid, value) VALUES (?, ?)",
                (uuid.uuid4().bytes, f"worker1_iter{iteration}"),
            )

            logger.debug(f"[Iter {iteration}] Worker 1: About to commit")

        logger.debug(f"[Iter {iteration}] Worker 1: Transaction committed")

    await anyio.to_thread.run_sync(do_work)
    logger.info(f"[Iter {iteration}] Worker 1 completed")


async def worker_2(conn: apsw.Connection, iteration: int) -> None:
    """Worker 2: Use a raw APSW connection directly."""
    logger.info(f"[Iter {iteration}] Worker 2 starting")

    def do_work() -> None:
        # Small sleep to ensure both workers get scheduled
        time.sleep(0.001)
        logger.debug(f"[Iter {iteration}] Worker 2: About to start IMMEDIATE transaction")
        conn.transaction_mode = "IMMEDIATE"
        with conn:
            logger.debug(f"[Iter {iteration}] Worker 2: Entered IMMEDIATE transaction")

            # Do some simple DB work
            cursor = conn.execute("SELECT COUNT(*) FROM test_data")
            row = cursor.fetchone()
            count = row[0] if row else 0
            logger.debug(f"[Iter {iteration}] Worker 2: Found {count} rows")

            # Insert a row
            conn.execute(
                "INSERT INTO test_data (uuid, value) VALUES (?, ?)",
                (uuid.uuid4().bytes, f"worker2_iter{iteration}"),
            )

            logger.debug(f"[Iter {iteration}] Worker 2: About to commit")

        logger.debug(f"[Iter {iteration}] Worker 2: Transaction committed")

    await anyio.to_thread.run_sync(do_work)
    logger.info(f"[Iter {iteration}] Worker 2 completed")


# =============================================================================
# Test
# =============================================================================


async def test_concurrent_transactions_raw_connections(db_path: str) -> None:
    """Test: Two workers using raw APSW connections (no pool)."""
    logger.info("=" * 70)
    logger.info("Test: Two workers, raw APSW connections (no pool)")
    logger.info("=" * 70)

    # Create two separate APSW connections
    conn_1 = apsw.Connection(db_path)
    conn_2 = apsw.Connection(db_path)

    try:
        # Apply connection setup
        connection_setup(conn_1)
        connection_setup(conn_2)

        for i in range(10):
            logger.info(f"\n--- Iteration {i} ---")
            try:
                async with anyio.create_task_group() as tg:
                    tg.start_soon(worker_1, conn_1, i)
                    tg.start_soon(worker_2, conn_2, i)
                logger.info(f"[Iter {i}] Both workers completed successfully")
            except Exception as e:
                logger.error(f"[Iter {i}] Error: {e}", exc_info=True)
                break
    finally:
        conn_1.close()
        conn_2.close()


async def main() -> None:
    """Run the test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.sqlite")
        logger.info(f"Using database: {db_path}")

        # Set up the database
        setup_test_database(db_path)

        # Test with raw connections
        await test_concurrent_transactions_raw_connections(db_path)


if __name__ == "__main__":
    anyio.run(main)
