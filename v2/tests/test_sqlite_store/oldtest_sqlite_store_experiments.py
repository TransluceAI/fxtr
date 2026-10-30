"""Experiments to diagnose concurrent session issues in sqlite_store.

These tests helped isolate the root cause of the "disk I/O error" that occurred
when multiple SQLiteFxtrSession instances were opened concurrently on the same
database file.

The issue: When session 2 closed before session 1, session 1 got a
"disk I/O error" when trying to mark itself as garbage during teardown.

## Key findings:

1. The original sqlite_arena.py does NOT have this issue (no APSW pools)

2. The issue required ALL of these conditions:
   - Two sessions with nested task groups (like open() creates)
   - Session lifecycle handlers running as background tasks
   - BOTH: SQLAlchemy engine2 disposed AND APSW pool2 closed

3. What WORKS (does NOT trigger the bug):
   - Two APSW pool groups on same DB - WORKS
   - Two SQLAlchemy engines on same DB - WORKS
   - APSW pool + SQLAlchemy engine together - WORKS
   - Two sessions in the SAME task group - WORKS
   - Close only pool2 (not engine2) - WORKS
   - Dispose only engine2 (not pool2) - WORKS
   - No disposal at all - WORKS

4. What FAILED (triggered the bug):
   - Two sessions in NESTED task groups + dispose engine2 + close pool2 - FAILED
   - This is exactly what open() does!

## Root cause hypothesis:

There was some interaction between:
- SQLAlchemy engine disposal (which may checkpoint WAL or close sqlite3 connections)
- APSW pool closure (which closes its separate SQLite connections)
- Having a background task using engine1 that's waiting

When both engine2 disposal AND pool2 closure happened while session1's lifecycle
handler was still waiting, something about the database file state became
corrupted for engine1's connections.

## Resolution:

The fix was to migrate the session lifecycle operations (insert_session and
mark_session_garbage) from SQLAlchemy to APSW. By using the APSW pool for these
operations instead of the SQLAlchemy engine, the lifecycle handler no longer
depends on the SQLAlchemy engine at all. This avoids the corruption issue because
the lifecycle handler now only uses APSW connections, which are unaffected by
SQLAlchemy engine disposal.

See: sqlite_ops.insert_session() and sqlite_ops.mark_session_garbage()
See: sqlite_namespace.py _lifecycle_handler()

The xfailed tests in this file may now pass since the bug is fixed.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from datetime import datetime, timezone

import pytest


class TestOriginalSqliteArenaWorks:
    """Verify the original sqlite_arena.py works with concurrent sessions."""

    @pytest.mark.anyio
    async def test_concurrent_sessions_original_sqlite_arena(self):
        """Original sqlite_arena handles concurrent sessions correctly."""
        from fxtr2.data.sqlite_arena import SQLiteFxtrSession

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.sqlite")
            url = f"sqlite+aiosqlite:///{db_path}"

            async with SQLiteFxtrSession.open(url) as session1:
                assert session1._internal_sid is not None

                async with SQLiteFxtrSession.open(url) as session2:
                    assert session2._internal_sid is not None
                    assert session2._internal_sid != session1._internal_sid
                # session2 closes here

            # session1 closes here - this should NOT fail
            # If we get here, test passes


class TestSqliteNamespaceFails:
    """Demonstrate that sqlite_namespace.py fails with concurrent sessions."""

    @pytest.mark.anyio
    @pytest.mark.xfail(
        reason="Known issue: concurrent sessions cause 'disk I/O error' during teardown"
    )
    async def test_concurrent_sessions_sqlite_namespace(self):
        """sqlite_namespace fails with concurrent sessions (KNOWN BUG)."""
        from fxtr2.data.sqlite_store.sqlite_namespace import SQLiteFxtrSession

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.sqlite")

            async with SQLiteFxtrSession.open(db_path) as session1:
                assert session1._internal_sid is not None

                async with SQLiteFxtrSession.open(db_path) as session2:
                    assert session2._internal_sid is not None
                # session2 closes here

            # session1 closes here - this fails with "disk I/O error"


class TestIsolatedComponents:
    """Test individual components to isolate the issue."""

    @pytest.mark.anyio
    async def test_two_apsw_pool_groups_same_db(self):
        """Two APSW pool groups can coexist on the same database."""
        from fxtr2.data.sqlite_store import sqlite_ops

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.sqlite")

            pool_group1 = sqlite_ops.setup_database_and_create_pool_group(db_path)
            pool_group2 = sqlite_ops.setup_database_and_create_pool_group(db_path)

            # Both can query
            def query(conn):
                cursor = conn.execute("SELECT 1")
                return cursor.fetchone()[0]

            result1 = await pool_group1.write_pool.run_sync_with_connection(query)
            result2 = await pool_group2.write_pool.run_sync_with_connection(query)
            assert result1 == 1
            assert result2 == 1

            # Close pool2, pool1 should still work
            pool_group2.close()
            result1_after = await pool_group1.write_pool.run_sync_with_connection(query)
            assert result1_after == 1

            pool_group1.close()

    @pytest.mark.anyio
    async def test_two_sqlalchemy_engines_same_db(self):
        """Two SQLAlchemy engines can coexist on the same database."""
        from fxtr2.data.sqlite_store.sqlite_namespace import create_fxtr_engine

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.sqlite")
            url = f"sqlite+aiosqlite:///{db_path}"

            # Need to initialize the DB first
            from fxtr2.data.sqlite_store import sqlite_ops

            pool_group = sqlite_ops.setup_database_and_create_pool_group(db_path)

            engine1 = create_fxtr_engine(url)
            engine2 = create_fxtr_engine(url)

            # Both can query
            async with engine1.write_txn_engine.begin() as conn:
                result = await conn.exec_driver_sql("SELECT 1")
                assert result.fetchone()[0] == 1

            async with engine2.write_txn_engine.begin() as conn:
                result = await conn.exec_driver_sql("SELECT 1")
                assert result.fetchone()[0] == 1

            # Dispose engine2, engine1 should still work
            await engine2.root_engine.dispose()

            async with engine1.write_txn_engine.begin() as conn:
                result = await conn.exec_driver_sql("SELECT 1")
                assert result.fetchone()[0] == 1

            await engine1.root_engine.dispose()
            pool_group.close()

    @pytest.mark.anyio
    async def test_apsw_pool_and_sqlalchemy_engine_same_db(self):
        """APSW pool and SQLAlchemy engine can coexist."""
        from fxtr2.data.sqlite_store import sqlite_ops
        from fxtr2.data.sqlite_store.sqlite_namespace import create_fxtr_engine

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.sqlite")
            url = f"sqlite+aiosqlite:///{db_path}"

            pool_group = sqlite_ops.setup_database_and_create_pool_group(db_path)
            engine = create_fxtr_engine(url)

            # Both can query
            def apsw_query(conn):
                cursor = conn.execute("SELECT 1")
                return cursor.fetchone()[0]

            result_apsw = await pool_group.write_pool.run_sync_with_connection(apsw_query)
            assert result_apsw == 1

            async with engine.write_txn_engine.begin() as conn:
                result = await conn.exec_driver_sql("SELECT 1")
                assert result.fetchone()[0] == 1

            await engine.root_engine.dispose()
            pool_group.close()

    @pytest.mark.anyio
    async def test_close_pool2_then_use_engine1(self):
        """Closing APSW pool 2 doesn't break SQLAlchemy engine 1."""
        from fxtr2.data.sqlite_store import sqlite_ops
        from fxtr2.data.sqlite_store.sqlite_namespace import create_fxtr_engine

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.sqlite")
            url = f"sqlite+aiosqlite:///{db_path}"

            # Set up two pool groups and two engines (like two sessions)
            pool_group1 = sqlite_ops.setup_database_and_create_pool_group(db_path)
            engine1 = create_fxtr_engine(url)

            pool_group2 = sqlite_ops.setup_database_and_create_pool_group(db_path)
            engine2 = create_fxtr_engine(url)

            # Use engine2
            async with engine2.write_txn_engine.begin() as conn:
                await conn.exec_driver_sql("SELECT 1")

            # Close engine2 and pool2 (like session2 closing)
            await engine2.root_engine.dispose()
            pool_group2.close()

            # Engine1 should still work
            async with engine1.write_txn_engine.begin() as conn:
                result = await conn.exec_driver_sql("SELECT 1")
                assert result.fetchone()[0] == 1

            await engine1.root_engine.dispose()
            pool_group1.close()

    @pytest.mark.anyio
    async def test_full_lifecycle_flow_manually(self):
        """Manually replicate the full lifecycle flow."""
        from fxtr2.data.sqlite_store import sqlite_ops
        from fxtr2.data.sqlite_store.sqlite_namespace import create_fxtr_engine, session_table

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.sqlite")
            url = f"sqlite+aiosqlite:///{db_path}"

            # Set up like two sessions would
            pool_group1 = sqlite_ops.setup_database_and_create_pool_group(db_path)
            engine1 = create_fxtr_engine(url)

            pool_group2 = sqlite_ops.setup_database_and_create_pool_group(db_path)
            engine2 = create_fxtr_engine(url)

            # Insert sessions (like lifecycle_handler start)
            async with engine1.write_txn_engine.begin() as conn:
                now = datetime.now(timezone.utc)
                result = await conn.execute(
                    session_table.insert()
                    .values(
                        public_id=uuid.uuid4().bytes,
                        created_at=now,
                        last_seen_at=now,
                        is_garbage=False,
                    )
                    .returning(session_table.c.id)
                )
                sid1 = result.fetchone()[0]

            async with engine2.write_txn_engine.begin() as conn:
                now = datetime.now(timezone.utc)
                result = await conn.execute(
                    session_table.insert()
                    .values(
                        public_id=uuid.uuid4().bytes,
                        created_at=now,
                        last_seen_at=now,
                        is_garbage=False,
                    )
                    .returning(session_table.c.id)
                )
                sid2 = result.fetchone()[0]

            assert sid1 != sid2

            # Mark session 2 as garbage (like lifecycle_handler teardown)
            async with engine2.write_txn_engine.begin() as conn:
                now = datetime.now(timezone.utc)
                await conn.execute(
                    session_table.update()
                    .where(session_table.c.id == sid2)
                    .values(is_garbage=True, last_seen_at=now)
                )

            # Dispose engine2 and close pool2 (like session2's finally block)
            await engine2.root_engine.dispose()
            pool_group2.close()

            # Now mark session 1 as garbage - THIS WORKS in isolation!
            async with engine1.write_txn_engine.begin() as conn:
                now = datetime.now(timezone.utc)
                await conn.execute(
                    session_table.update()
                    .where(session_table.c.id == sid1)
                    .values(is_garbage=True, last_seen_at=now)
                )

            # Cleanup
            await engine1.root_engine.dispose()
            pool_group1.close()


class TestTaskGroupInteraction:
    """Test if task groups are involved in the issue."""

    @pytest.mark.anyio
    async def test_lifecycle_with_task_groups(self):
        """Test if the task group context affects the issue."""

        from fxtr2.data.sqlite_store import sqlite_ops
        from fxtr2.data.sqlite_store.sqlite_namespace import create_fxtr_engine, session_table
        from fxtr2.util import anyio_util

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.sqlite")
            url = f"sqlite+aiosqlite:///{db_path}"

            results = {}

            async def session_lifecycle(name: str, pool_group, engine):
                """Simulate the lifecycle handler."""
                # Insert session
                async with engine.write_txn_engine.begin() as conn:
                    now = datetime.now(timezone.utc)
                    result = await conn.execute(
                        session_table.insert()
                        .values(
                            public_id=uuid.uuid4().bytes,
                            created_at=now,
                            last_seen_at=now,
                            is_garbage=False,
                        )
                        .returning(session_table.c.id)
                    )
                    sid = result.fetchone()[0]
                results[name] = {"sid": sid, "status": "started"}
                return sid

            async def session_teardown(name: str, sid: int, engine):
                """Simulate teardown."""
                async with engine.write_txn_engine.begin() as conn:
                    now = datetime.now(timezone.utc)
                    await conn.execute(
                        session_table.update()
                        .where(session_table.c.id == sid)
                        .values(is_garbage=True, last_seen_at=now)
                    )
                results[name]["status"] = "torn_down"

            # Set up resources
            pool_group1 = sqlite_ops.setup_database_and_create_pool_group(db_path)
            engine1 = create_fxtr_engine(url)
            pool_group2 = sqlite_ops.setup_database_and_create_pool_group(db_path)
            engine2 = create_fxtr_engine(url)

            # Run with task groups like the real code
            async with anyio_util.create_task_group(collapse_exc_group=True) as tg1:
                sid1 = await session_lifecycle("session1", pool_group1, engine1)

                async with anyio_util.create_task_group(collapse_exc_group=True) as tg2:
                    sid2 = await session_lifecycle("session2", pool_group2, engine2)
                    # Teardown session2
                    await session_teardown("session2", sid2, engine2)

                # After tg2 exits, dispose engine2 and close pool2
                await engine2.root_engine.dispose()
                pool_group2.close()

                # Now teardown session1 - does this fail?
                await session_teardown("session1", sid1, engine1)

            await engine1.root_engine.dispose()
            pool_group1.close()

            assert results["session1"]["status"] == "torn_down"
            assert results["session2"]["status"] == "torn_down"


class TestGarbageWorkerInteraction:
    """Test if the garbage worker affects the issue."""

    @pytest.mark.anyio
    @pytest.mark.xfail(
        reason="Known issue: concurrent sessions cause 'disk I/O error' during teardown"
    )
    async def test_concurrent_sessions_without_garbage_worker(self):
        """Test concurrent sessions with garbage worker disabled."""
        from fxtr2.data.sqlite_store.sqlite_namespace import SQLiteFxtrSession

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.sqlite")

            # Disable garbage worker to see if it affects the issue
            async with SQLiteFxtrSession.open(
                db_path, _should_start_garbage_worker=False
            ) as session1:
                assert session1._internal_sid is not None

                async with SQLiteFxtrSession.open(
                    db_path, _should_start_garbage_worker=False
                ) as session2:
                    assert session2._internal_sid is not None
                # session2 closes here

            # session1 closes here - still fails!


class TestSessionFileInteraction:
    """Test if session files affect the issue."""

    @pytest.mark.anyio
    async def test_session_file_locking_effect(self):
        """Test if session file locking causes the issue.

        The _hold_session_file() method acquires an exclusive flock on a file.
        Maybe this affects SQLite access somehow?
        """
        from fxtr2.data.sqlite_store import sqlite_ops
        from fxtr2.data.sqlite_store.sqlite_namespace import (
            SQLiteFxtrSession,
            create_fxtr_engine,
            session_table,
        )
        from fxtr2.util import anyio_util

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.sqlite")
            url = f"sqlite+aiosqlite:///{db_path}"

            # Set up resources manually, mimicking open() but with explicit control
            pool_group1 = sqlite_ops.setup_database_and_create_pool_group(db_path)
            engine1 = create_fxtr_engine(url)

            pool_group2 = sqlite_ops.setup_database_and_create_pool_group(db_path)
            engine2 = create_fxtr_engine(url)

            async with anyio_util.create_task_group(collapse_exc_group=True) as tg:
                # Create session objects but don't call start() - just test the file locking
                session1 = SQLiteFxtrSession(
                    db_path=db_path,
                    pool_group=pool_group1,
                    async_engine_group=engine1,
                    background_task_group=tg,
                    _should_start_garbage_worker=False,
                )
                session2 = SQLiteFxtrSession(
                    db_path=db_path,
                    pool_group=pool_group2,
                    async_engine_group=engine2,
                    background_task_group=tg,
                    _should_start_garbage_worker=False,
                )

                # Hold session files like the lifecycle handler does
                async with session1._hold_session_file():
                    async with session2._hold_session_file():
                        # Both holding locks, try to use engines
                        async with engine1.write_txn_engine.begin() as conn:
                            now = datetime.now(timezone.utc)
                            result = await conn.execute(
                                session_table.insert()
                                .values(
                                    public_id=uuid.uuid4().bytes,
                                    created_at=now,
                                    last_seen_at=now,
                                    is_garbage=False,
                                )
                                .returning(session_table.c.id)
                            )
                            sid1 = result.fetchone()[0]

                        async with engine2.write_txn_engine.begin() as conn:
                            now = datetime.now(timezone.utc)
                            result = await conn.execute(
                                session_table.insert()
                                .values(
                                    public_id=uuid.uuid4().bytes,
                                    created_at=now,
                                    last_seen_at=now,
                                    is_garbage=False,
                                )
                                .returning(session_table.c.id)
                            )
                            sid2 = result.fetchone()[0]

                    # session2's file lock released, dispose engine2 and close pool2
                    await engine2.root_engine.dispose()
                    pool_group2.close()

                    # Now try to mark session1 as garbage while still holding its lock
                    async with engine1.write_txn_engine.begin() as conn:
                        now = datetime.now(timezone.utc)
                        await conn.execute(
                            session_table.update()
                            .where(session_table.c.id == sid1)
                            .values(is_garbage=True, last_seen_at=now)
                        )

            await engine1.root_engine.dispose()
            pool_group1.close()


class TestExactOpenFlow:
    """Replicate the exact flow of open() step by step."""

    @pytest.mark.anyio
    async def test_single_session_start_aclose(self):
        """Test that a single session can start and close properly."""
        from fxtr2.data.sqlite_store import sqlite_ops
        from fxtr2.data.sqlite_store.sqlite_namespace import SQLiteFxtrSession, create_fxtr_engine
        from fxtr2.util import anyio_util

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.sqlite")
            url = f"sqlite+aiosqlite:///{db_path}"

            pool_group = sqlite_ops.setup_database_and_create_pool_group(db_path)
            engine = create_fxtr_engine(url)

            try:
                async with anyio_util.create_task_group(collapse_exc_group=True) as tg:
                    session = SQLiteFxtrSession(
                        db_path=db_path,
                        pool_group=pool_group,
                        async_engine_group=engine,
                        background_task_group=tg,
                        _should_start_garbage_worker=False,
                    )
                    await session.start()
                    assert session.is_active()
                    await session.aclose()
                    assert not session.is_active()
            finally:
                await engine.root_engine.dispose()
                pool_group.close()

    @pytest.mark.anyio
    async def test_two_sessions_same_task_group(self):
        """Test two sessions in the same task group (not nested)."""
        from fxtr2.data.sqlite_store import sqlite_ops
        from fxtr2.data.sqlite_store.sqlite_namespace import SQLiteFxtrSession, create_fxtr_engine
        from fxtr2.util import anyio_util

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.sqlite")
            url = f"sqlite+aiosqlite:///{db_path}"

            pool_group1 = sqlite_ops.setup_database_and_create_pool_group(db_path)
            engine1 = create_fxtr_engine(url)
            pool_group2 = sqlite_ops.setup_database_and_create_pool_group(db_path)
            engine2 = create_fxtr_engine(url)

            try:
                async with anyio_util.create_task_group(collapse_exc_group=True) as tg:
                    session1 = SQLiteFxtrSession(
                        db_path=db_path,
                        pool_group=pool_group1,
                        async_engine_group=engine1,
                        background_task_group=tg,
                        _should_start_garbage_worker=False,
                    )
                    session2 = SQLiteFxtrSession(
                        db_path=db_path,
                        pool_group=pool_group2,
                        async_engine_group=engine2,
                        background_task_group=tg,
                        _should_start_garbage_worker=False,
                    )

                    await session1.start()
                    await session2.start()

                    assert session1.is_active()
                    assert session2.is_active()

                    # Close session2 first
                    await session2.aclose()
                    assert not session2.is_active()
                    assert session1.is_active()

                    # Now close session1
                    await session1.aclose()
                    assert not session1.is_active()
            finally:
                await engine1.root_engine.dispose()
                await engine2.root_engine.dispose()
                pool_group1.close()
                pool_group2.close()

    @pytest.mark.anyio
    @pytest.mark.xfail(reason="Known issue: nested task groups with concurrent sessions fail")
    async def test_two_sessions_nested_task_groups(self):
        """Test two sessions in nested task groups - THIS FAILS.

        This is the key finding: when session2's task group is nested inside
        session1's task group, closing session2 somehow breaks session1.
        """
        from fxtr2.data.sqlite_store import sqlite_ops
        from fxtr2.data.sqlite_store.sqlite_namespace import SQLiteFxtrSession, create_fxtr_engine
        from fxtr2.util import anyio_util

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.sqlite")
            url = f"sqlite+aiosqlite:///{db_path}"

            pool_group1 = sqlite_ops.setup_database_and_create_pool_group(db_path)
            engine1 = create_fxtr_engine(url)

            try:
                # Session 1 in outer task group
                async with anyio_util.create_task_group(collapse_exc_group=True) as tg1:
                    session1 = SQLiteFxtrSession(
                        db_path=db_path,
                        pool_group=pool_group1,
                        async_engine_group=engine1,
                        background_task_group=tg1,
                        _should_start_garbage_worker=False,
                    )
                    await session1.start()

                    # Session 2 in NESTED task group
                    pool_group2 = sqlite_ops.setup_database_and_create_pool_group(db_path)
                    engine2 = create_fxtr_engine(url)

                    try:
                        async with anyio_util.create_task_group(collapse_exc_group=True) as tg2:
                            session2 = SQLiteFxtrSession(
                                db_path=db_path,
                                pool_group=pool_group2,
                                async_engine_group=engine2,
                                background_task_group=tg2,
                                _should_start_garbage_worker=False,
                            )
                            await session2.start()
                            await session2.aclose()
                    finally:
                        await engine2.root_engine.dispose()
                        pool_group2.close()

                    # Now close session1 - THIS FAILS with "disk I/O error"
                    await session1.aclose()
            finally:
                await engine1.root_engine.dispose()
                pool_group1.close()

    @pytest.mark.anyio
    @pytest.mark.xfail(
        reason="Known issue: disposing engine2 AND closing pool2 together breaks engine1"
    )
    async def test_nested_task_groups_dispose_inside(self):
        """Test nested task groups but dispose engine2 INSIDE the inner task group.

        This tests if the order of disposal matters.
        """
        from fxtr2.data.sqlite_store import sqlite_ops
        from fxtr2.data.sqlite_store.sqlite_namespace import SQLiteFxtrSession, create_fxtr_engine
        from fxtr2.util import anyio_util

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.sqlite")
            url = f"sqlite+aiosqlite:///{db_path}"

            pool_group1 = sqlite_ops.setup_database_and_create_pool_group(db_path)
            engine1 = create_fxtr_engine(url)

            try:
                async with anyio_util.create_task_group(collapse_exc_group=True) as tg1:
                    session1 = SQLiteFxtrSession(
                        db_path=db_path,
                        pool_group=pool_group1,
                        async_engine_group=engine1,
                        background_task_group=tg1,
                        _should_start_garbage_worker=False,
                    )
                    await session1.start()

                    pool_group2 = sqlite_ops.setup_database_and_create_pool_group(db_path)
                    engine2 = create_fxtr_engine(url)

                    async with anyio_util.create_task_group(collapse_exc_group=True) as tg2:
                        session2 = SQLiteFxtrSession(
                            db_path=db_path,
                            pool_group=pool_group2,
                            async_engine_group=engine2,
                            background_task_group=tg2,
                            _should_start_garbage_worker=False,
                        )
                        await session2.start()
                        await session2.aclose()

                        # Dispose INSIDE the inner task group
                        await engine2.root_engine.dispose()
                        pool_group2.close()

                    # Inner task group exited, engine2 already disposed
                    # Now close session1
                    await session1.aclose()
            finally:
                await engine1.root_engine.dispose()
                pool_group1.close()

    @pytest.mark.anyio
    async def test_nested_only_close_pool2_not_engine2(self):
        """Test nested task groups: close pool2 but don't dispose engine2.

        Tests if APSW pool closure is the problem vs SQLAlchemy disposal.
        """
        from fxtr2.data.sqlite_store import sqlite_ops
        from fxtr2.data.sqlite_store.sqlite_namespace import SQLiteFxtrSession, create_fxtr_engine
        from fxtr2.util import anyio_util

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.sqlite")
            url = f"sqlite+aiosqlite:///{db_path}"

            pool_group1 = sqlite_ops.setup_database_and_create_pool_group(db_path)
            engine1 = create_fxtr_engine(url)
            pool_group2 = sqlite_ops.setup_database_and_create_pool_group(db_path)
            engine2 = create_fxtr_engine(url)

            try:
                async with anyio_util.create_task_group(collapse_exc_group=True) as tg1:
                    session1 = SQLiteFxtrSession(
                        db_path=db_path,
                        pool_group=pool_group1,
                        async_engine_group=engine1,
                        background_task_group=tg1,
                        _should_start_garbage_worker=False,
                    )
                    await session1.start()

                    async with anyio_util.create_task_group(collapse_exc_group=True) as tg2:
                        session2 = SQLiteFxtrSession(
                            db_path=db_path,
                            pool_group=pool_group2,
                            async_engine_group=engine2,
                            background_task_group=tg2,
                            _should_start_garbage_worker=False,
                        )
                        await session2.start()
                        await session2.aclose()

                    # Only close APSW pool, don't dispose SQLAlchemy engine
                    pool_group2.close()

                    # Now close session1
                    await session1.aclose()
            finally:
                await engine1.root_engine.dispose()
                await engine2.root_engine.dispose()
                pool_group1.close()

    @pytest.mark.anyio
    async def test_nested_only_dispose_engine2_not_pool2(self):
        """Test nested task groups: dispose engine2 but don't close pool2.

        Tests if SQLAlchemy disposal is the problem vs APSW pool closure.
        """
        from fxtr2.data.sqlite_store import sqlite_ops
        from fxtr2.data.sqlite_store.sqlite_namespace import SQLiteFxtrSession, create_fxtr_engine
        from fxtr2.util import anyio_util

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.sqlite")
            url = f"sqlite+aiosqlite:///{db_path}"

            pool_group1 = sqlite_ops.setup_database_and_create_pool_group(db_path)
            engine1 = create_fxtr_engine(url)
            pool_group2 = sqlite_ops.setup_database_and_create_pool_group(db_path)
            engine2 = create_fxtr_engine(url)

            try:
                async with anyio_util.create_task_group(collapse_exc_group=True) as tg1:
                    session1 = SQLiteFxtrSession(
                        db_path=db_path,
                        pool_group=pool_group1,
                        async_engine_group=engine1,
                        background_task_group=tg1,
                        _should_start_garbage_worker=False,
                    )
                    await session1.start()

                    async with anyio_util.create_task_group(collapse_exc_group=True) as tg2:
                        session2 = SQLiteFxtrSession(
                            db_path=db_path,
                            pool_group=pool_group2,
                            async_engine_group=engine2,
                            background_task_group=tg2,
                            _should_start_garbage_worker=False,
                        )
                        await session2.start()
                        await session2.aclose()

                    # Only dispose SQLAlchemy engine, don't close APSW pool
                    await engine2.root_engine.dispose()

                    # Now close session1
                    await session1.aclose()
            finally:
                await engine1.root_engine.dispose()
                pool_group1.close()
                pool_group2.close()

    @pytest.mark.anyio
    async def test_nested_task_groups_no_dispose_engine2(self):
        """Test nested task groups without disposing engine2 at all.

        If this passes, the issue is specifically with engine disposal.
        """
        from fxtr2.data.sqlite_store import sqlite_ops
        from fxtr2.data.sqlite_store.sqlite_namespace import SQLiteFxtrSession, create_fxtr_engine
        from fxtr2.util import anyio_util

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.sqlite")
            url = f"sqlite+aiosqlite:///{db_path}"

            pool_group1 = sqlite_ops.setup_database_and_create_pool_group(db_path)
            engine1 = create_fxtr_engine(url)
            pool_group2 = sqlite_ops.setup_database_and_create_pool_group(db_path)
            engine2 = create_fxtr_engine(url)

            try:
                async with anyio_util.create_task_group(collapse_exc_group=True) as tg1:
                    session1 = SQLiteFxtrSession(
                        db_path=db_path,
                        pool_group=pool_group1,
                        async_engine_group=engine1,
                        background_task_group=tg1,
                        _should_start_garbage_worker=False,
                    )
                    await session1.start()

                    async with anyio_util.create_task_group(collapse_exc_group=True) as tg2:
                        session2 = SQLiteFxtrSession(
                            db_path=db_path,
                            pool_group=pool_group2,
                            async_engine_group=engine2,
                            background_task_group=tg2,
                            _should_start_garbage_worker=False,
                        )
                        await session2.start()
                        await session2.aclose()

                    # Inner task group exited
                    # DON'T dispose engine2 or close pool_group2 yet!
                    # Now close session1
                    await session1.aclose()
            finally:
                await engine1.root_engine.dispose()
                await engine2.root_engine.dispose()
                pool_group1.close()
                pool_group2.close()

    @pytest.mark.anyio
    @pytest.mark.xfail(reason="Known issue: nested task groups with concurrent sessions fail")
    async def test_replicate_open_flow_exactly(self):
        """Replicate open() flow as closely as possible without using open()."""
        from fxtr2.data.sqlite_store import sqlite_ops
        from fxtr2.data.sqlite_store.sqlite_namespace import SQLiteFxtrSession, create_fxtr_engine
        from fxtr2.util import anyio_util

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.sqlite")
            url = f"sqlite+aiosqlite:///{db_path}"

            # Replicate open() for session 1
            pool_group1 = sqlite_ops.setup_database_and_create_pool_group(db_path)
            engine1 = create_fxtr_engine(url)

            try:
                async with anyio_util.create_task_group(collapse_exc_group=True) as tg1:
                    session1 = SQLiteFxtrSession(
                        db_path=db_path,
                        pool_group=pool_group1,
                        async_engine_group=engine1,
                        background_task_group=tg1,
                        _should_start_garbage_worker=False,
                    )
                    await session1.start()

                    try:
                        # Now open session 2 inside session 1's context
                        pool_group2 = sqlite_ops.setup_database_and_create_pool_group(db_path)
                        engine2 = create_fxtr_engine(url)

                        try:
                            async with anyio_util.create_task_group(collapse_exc_group=True) as tg2:
                                session2 = SQLiteFxtrSession(
                                    db_path=db_path,
                                    pool_group=pool_group2,
                                    async_engine_group=engine2,
                                    background_task_group=tg2,
                                    _should_start_garbage_worker=False,
                                )
                                await session2.start()

                                try:
                                    # Both sessions are now running
                                    assert session1.is_active()
                                    assert session2.is_active()
                                finally:
                                    await session2.aclose()
                        finally:
                            await engine2.root_engine.dispose()
                            pool_group2.close()

                        # Session 2 is fully closed, now session 1 should still work
                        assert session1.is_active()
                    finally:
                        await session1.aclose()
            finally:
                await engine1.root_engine.dispose()
                pool_group1.close()


class TestFileStateAfterClose:
    """Check database file state after session closures."""

    @pytest.mark.anyio
    async def test_wal_files_after_pool_close(self):
        """Check if WAL files are affected by pool closure."""
        from fxtr2.data.sqlite_store import sqlite_ops

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.sqlite")

            def list_db_files():
                return sorted(f for f in os.listdir(tmpdir) if f.startswith("test.sqlite"))

            pool_group1 = sqlite_ops.setup_database_and_create_pool_group(db_path)
            files_after_pool1 = list_db_files()

            pool_group2 = sqlite_ops.setup_database_and_create_pool_group(db_path)
            files_after_pool2 = list_db_files()

            # Do some writes
            def write_something(conn):
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO session (public_id, created_at, last_seen_at, is_garbage) "
                    "VALUES (?, datetime('now'), datetime('now'), 0)",
                    (uuid.uuid4().bytes,),
                )
                conn.execute("COMMIT")

            await pool_group1.write_pool.run_sync_with_connection(write_something)
            await pool_group2.write_pool.run_sync_with_connection(write_something)
            files_after_writes = list_db_files()

            # Close pool2
            pool_group2.close()
            files_after_pool2_close = list_db_files()

            # Can pool1 still write?
            try:
                await pool_group1.write_pool.run_sync_with_connection(write_something)
                pool1_write_success = True
            except Exception as e:
                pool1_write_success = False

            pool_group1.close()
            files_after_pool1_close = list_db_files()

            # Report findings
            print(f"Files after pool1 setup: {files_after_pool1}")
            print(f"Files after pool2 setup: {files_after_pool2}")
            print(f"Files after writes: {files_after_writes}")
            print(f"Files after pool2 close: {files_after_pool2_close}")
            print(f"Pool1 write after pool2 close: {pool1_write_success}")
            print(f"Files after pool1 close: {files_after_pool1_close}")

            assert pool1_write_success, "Pool1 should still work after pool2 closes"
