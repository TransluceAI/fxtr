"""The runner's Postgres connection: transactions, rollback, and schema isolation."""

from __future__ import annotations

from typing import Any

import anyio
import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import event, text  # noqa: E402
from sqlalchemy.util import await_only  # noqa: E402

from fxtr.runner.postgres.database import Database  # noqa: E402

pytestmark = pytest.mark.anyio


def test_pg_schema_names_are_validated() -> None:
    with pytest.raises(ValueError):
        Database("postgresql+asyncpg://x@localhost/x", pg_schema='bad"name')
    with pytest.raises(ValueError):
        Database("postgresql+asyncpg://x@localhost/x", pg_schema="1abc")


async def test_transaction_commits_on_exit(database: Database) -> None:
    async with database.transaction() as conn:
        await conn.execute(text("CREATE TABLE things (id int PRIMARY KEY, name text)"))
        await conn.execute(text("INSERT INTO things VALUES (1, 'one')"))
    async with database.transaction() as conn:
        rows = (await conn.execute(text("SELECT id, name FROM things"))).all()
    assert [tuple(r) for r in rows] == [(1, "one")]


async def test_transaction_rolls_back_on_error(database: Database) -> None:
    async with database.transaction() as conn:
        await conn.execute(text("CREATE TABLE things (id int PRIMARY KEY)"))
    with pytest.raises(RuntimeError):
        async with database.transaction() as conn:
            await conn.execute(text("INSERT INTO things VALUES (1)"))
            raise RuntimeError("abort")
    async with database.transaction() as conn:
        count = (await conn.execute(text("SELECT count(*) FROM things"))).scalar_one()
    assert count == 0


async def test_tables_live_in_the_named_pg_schema(database: Database) -> None:
    """Unqualified names resolve in the database's Postgres schema, so two databases on different
    schemas do not see each other's tables."""
    async with database.transaction() as conn:
        await conn.execute(text("CREATE TABLE things (id int PRIMARY KEY)"))
        # Resolve the unqualified name through the connection's search_path (the pg_schema), so
        # this sees only this database's table however many other schemas hold a 'things'.
        query = (
            "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE c.oid = to_regclass('things')"
        )
        schema = (await conn.execute(text(query))).scalar_one()
    assert schema == database.pg_schema
    async with Database(database.url, pg_schema=f"{database.pg_schema}_other") as other:
        await other.create_schema()
        try:
            async with other.transaction() as conn:
                exists = (await conn.execute(text("SELECT to_regclass('things')"))).scalar_one()
            assert exists is None
        finally:
            await other.drop_schema()


async def test_on_commit_runs_only_after_a_commit(database: Database) -> None:
    ran: list[str] = []
    async with database.transaction() as tx:
        tx.on_commit(lambda: ran.append("first"))
        tx.on_commit(lambda: ran.append("second"))
        assert ran == []
    assert ran == ["first", "second"]
    with pytest.raises(RuntimeError):
        async with database.transaction() as tx:
            tx.on_commit(lambda: ran.append("rolled back"))
            raise RuntimeError("abort")
    assert ran == ["first", "second"]


async def test_drop_schema_evicts_a_lingering_transaction(database: Database) -> None:
    """A transaction left open on the schema's tables (an interrupted task, a client never
    closed) would block the drop indefinitely; the drop terminates it first and bounds its wait.
    Uses a second instance on the same schema, as a separate process would."""
    other = Database(database.url, pg_schema=database.pg_schema)
    lingering = other.transaction()
    tx = await lingering.__aenter__()
    await tx.execute(
        text("INSERT INTO jobs (id, workflow_name, root) VALUES (gen_random_uuid(), 'wf', 'root')")
    )
    try:
        with anyio.fail_after(10):
            await database.drop_schema(lock_timeout="2s")
    finally:
        # The lingering connection was terminated by the drop; its exit only errors.
        with anyio.move_on_after(5):
            try:
                await lingering.__aexit__(None, None, None)
            except Exception:
                pass
        await other.dispose()
    # The schema is gone (the fixture's own drop then finds nothing).
    async with database.transaction() as tx2:
        row = (
            await tx2.execute(
                text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :s"),
                {"s": database.pg_schema},
            )
        ).first()
    assert row is None


async def test_many_topics_share_one_event_and_wake_all_matching_waiters() -> None:
    async with Database("postgresql+asyncpg://unused@localhost/unused") as db:
        results: list[bool] = []

        async def many() -> None:
            results.append(await db.wait_for_any((f"key/{i}" for i in range(10000)), 5))

        async def one() -> None:
            results.append(await db.wait_for("key/9999", 5))

        async with anyio.create_task_group() as tg:
            tg.start_soon(many)
            await anyio.wait_all_tasks_blocked()
            assert len(set.union(*db._wakes.values())) == 1
            tg.start_soon(one)
            await anyio.wait_all_tasks_blocked()
            db.wake("unrelated")
            assert results == []
            db.wake("key/9999")
        assert results == [True, True]
        assert db._wakes == {}


async def test_topic_waits_release_subscriptions_on_timeout_and_cancellation() -> None:
    async with Database("postgresql+asyncpg://unused@localhost/unused") as db:
        assert not await db.wait_for_any(["a", "b", "a"], 0.001)
        assert db._wakes == {}
        async with anyio.create_task_group() as tg:
            tg.start_soon(db.wait_for_any, ["a", "b"], 5)
            await anyio.wait_all_tasks_blocked()
            assert set(db._wakes) == {"a", "b"}
            tg.cancel_scope.cancel()
        assert db._wakes == {}


async def test_cancelled_wait_does_not_remove_another_waiters_subscription() -> None:
    async with Database("postgresql+asyncpg://unused@localhost/unused") as db:
        scope = anyio.CancelScope()
        result: list[bool] = []

        async def cancelled() -> None:
            with scope:
                await db.wait_for("shared", 5)

        async def survivor() -> None:
            result.append(await db.wait_for_any(["shared", "other"], 5))

        async with anyio.create_task_group() as tg:
            tg.start_soon(cancelled)
            tg.start_soon(survivor)
            await anyio.wait_all_tasks_blocked()
            scope.cancel()
            await anyio.wait_all_tasks_blocked()
            assert len(db._wakes["shared"]) == 1
            db.wake("shared")
        assert result == [True]
        assert db._wakes == {}


async def test_a_transaction_cancelled_while_connecting_finishes_and_gives_the_connection_back(
    database: Database,
) -> None:
    """A cancel that arrives while the pool is opening a connection waits for the connection to
    open, then rolls back and returns it, rather than interrupting the connect. An interrupted
    asyncpg connect can leave a connection behind for the garbage collector, whose late failure
    surfaces in whatever task runs next."""
    async with Database(database.url, pg_schema=database.pg_schema) as other:
        connecting, release = anyio.Event(), anyio.Event()

        def hold_the_connect(*_: Any) -> None:  # runs in SQLAlchemy's greenlet, so it may await
            connecting.set()
            await_only(release.wait())

        event.listen(other.engine.sync_engine, "do_connect", hold_the_connect)
        entered = False

        async def open_one() -> None:
            nonlocal entered
            async with other.transaction():
                entered = True

        async with anyio.create_task_group() as tg:
            tg.start_soon(open_one)
            await connecting.wait()
            tg.cancel_scope.cancel()
            release.set()
        assert not entered
        pool = other.engine.pool
        assert (pool.checkedout(), pool.checkedin()) == (0, 1)  # type: ignore[attr-defined]
