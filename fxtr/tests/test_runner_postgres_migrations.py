"""Tests for the runner's migrations: a schema they build is the one ``schema.py`` declares, a
schema created before migrations is adopted at the first revision, a schema at an earlier
revision is upgraded with its rows kept, and concurrent and repeated upgrades are safe."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import anyio
import pytest

pytest.importorskip("sqlalchemy")

from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.runtime.migration import MigrationContext  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from sqlalchemy import CheckConstraint, Connection, inspect, select, text  # noqa: E402

from fxtr.runner.postgres import migrations  # noqa: E402
from fxtr.runner.postgres.database import Database  # noqa: E402
from fxtr.runner.postgres.schema import (  # noqa: E402
    entities,
    job_attempt_provenance,
    job_entities,
    jobs,
    metadata,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
async def fresh(database: Database) -> AsyncIterator[Database]:
    """A database on a schema of its own that does not exist yet."""
    async with Database(database.url, pg_schema=f"test_{uuid.uuid4().hex}") as other:
        try:
            yield other
        finally:
            with anyio.fail_after(30):
                await other.drop_schema(lock_timeout="5s")


def differences(connection: Connection) -> list[object]:
    # Alembic's comparison covers tables, columns, types, server defaults, indexes, unique and
    # foreign keys, and comments; check constraints are compared below.
    context = MigrationContext.configure(
        connection, opts={"compare_type": True, "compare_server_default": True}
    )
    found: list[object] = [
        diff
        for diff in compare_metadata(context, metadata)
        # The version table is the migrations' own, not one schema.py declares.
        if not (diff[0] == "remove_table" and diff[1].name == "alembic_version")
    ]
    # Postgres rewrites a check's expression, so compare the checks of each table by name.
    inspector = inspect(connection)
    for table in metadata.sorted_tables:
        declared = {c.name for c in table.constraints if isinstance(c, CheckConstraint) and c.name}
        built = {c["name"] for c in inspector.get_check_constraints(table.name)}
        if declared != built:
            found.append(("check constraints", table.name, declared, built))
    return found


async def revision(database: Database) -> str | None:
    async with database.transaction() as tx:
        return (await tx.execute(text("SELECT version_num FROM alembic_version"))).scalar()


def head() -> str | None:
    return ScriptDirectory(str(migrations.__path__[0])).get_current_head()


def create_before_migrations(connection: Connection, pg_schema: str | None) -> None:
    """Create the tables as a fxtr from before migrations did: exactly the first revision's,
    with no revision recorded. Built by that revision rather than from ``schema.py``, whose tables
    are as the newest revision leaves them."""
    migrations.upgrade(connection, pg_schema, migrations.FIRST_REVISION)
    version_table = "alembic_version" if pg_schema is None else f"{pg_schema}.alembic_version"
    connection.execute(text(f"DROP TABLE {version_table}"))


async def test_a_schema_knows_its_revision_and_only_a_known_older_one_needs_an_upgrade(
    fresh: Database,
) -> None:
    async with fresh.transaction() as tx:
        assert await tx.connection.run_sync(migrations.current_revision, fresh.pg_schema) is None
    await fresh.create_schema()
    async with fresh.transaction() as tx:
        built = await tx.connection.run_sync(migrations.current_revision, fresh.pg_schema)
    assert built == migrations.head_revision() == head()
    assert migrations.needs_upgrade(None)
    assert not migrations.needs_upgrade(migrations.head_revision())
    # A revision this fxtr does not know is a later fxtr's, whose tables this one can use.
    assert not migrations.needs_upgrade("a-later-fxtrs-revision")
    # A prefix of a known revision's id is not that revision (Alembic would resolve it).
    assert not migrations.needs_upgrade(migrations.FIRST_REVISION[:-1])


async def test_tables_a_later_fxtr_migrated_are_left_as_they_are(fresh: Database) -> None:
    await fresh.create_schema()
    async with fresh.transaction() as tx:
        await tx.execute(text("UPDATE alembic_version SET version_num = 'a-later-fxtrs-revision'"))
    # The previous release opens the schema during a rollout, or after a rollback.
    await fresh.create_schema()
    assert await revision(fresh) == "a-later-fxtrs-revision"


async def test_the_migrations_build_exactly_the_declared_schema(fresh: Database) -> None:
    await fresh.create_schema()
    async with fresh.transaction() as tx:
        assert await tx.connection.run_sync(differences) == []
    assert await revision(fresh) == head()


async def test_a_schema_created_before_migrations_is_adopted_and_upgraded(
    fresh: Database,
) -> None:
    async with fresh.transaction() as tx:
        await tx.execute(text(f"CREATE SCHEMA {fresh.pg_schema}"))
        await tx.connection.run_sync(
            lambda connection: create_before_migrations(connection, fresh.pg_schema)
        )
    await fresh.create_schema()
    assert await revision(fresh) == head()
    async with fresh.transaction() as tx:
        assert await tx.connection.run_sync(differences) == []


async def test_a_schema_from_before_migrations_that_has_the_previews_table_is_upgraded(
    fresh: Database,
) -> None:
    """A fxtr from before migrations created ``attempt_previews`` with its other tables for a
    time. The revision that adds the table leaves such a table and its rows as they are."""
    from fxtr.runner.postgres.schema import attempt_previews

    async with fresh.transaction() as tx:
        await tx.execute(text(f"CREATE SCHEMA {fresh.pg_schema}"))
        await tx.connection.run_sync(
            lambda connection: create_before_migrations(connection, fresh.pg_schema)
        )
        await tx.connection.run_sync(
            lambda connection: attempt_previews.create(
                connection.execution_options(schema_translate_map={None: fresh.pg_schema})
            )
        )
    await fresh.create_schema()
    assert await revision(fresh) == head()
    async with fresh.transaction() as tx:
        assert await tx.connection.run_sync(differences) == []


async def test_the_first_revisions_tables_are_listed_as_it_creates_them(fresh: Database) -> None:
    async with fresh.transaction() as tx:
        await tx.execute(text(f"CREATE SCHEMA {fresh.pg_schema}"))

        def built(connection: Connection) -> dict[str, frozenset[str]]:
            migrations.upgrade(connection, fresh.pg_schema, migrations.FIRST_REVISION)
            inspector = inspect(connection)
            return {
                table: frozenset(c["name"] for c in inspector.get_columns(table))
                for table in inspector.get_table_names()
                if table != "alembic_version"
            }

        assert await tx.connection.run_sync(built) == migrations.FIRST_REVISION_TABLES


@pytest.mark.parametrize("drop", ["TABLE attempt_logs", "COLUMN reason"])
async def test_a_schema_from_before_migrations_that_lacks_part_of_the_first_revision_is_refused(
    fresh: Database, drop: str
) -> None:
    async with fresh.transaction() as tx:
        await tx.execute(text(f"CREATE SCHEMA {fresh.pg_schema}"))
        await tx.connection.run_sync(
            lambda connection: create_before_migrations(connection, fresh.pg_schema)
        )
        if drop.startswith("TABLE"):
            await tx.execute(text(f"DROP {drop}"))
        else:
            await tx.execute(text(f"ALTER TABLE attempts DROP {drop}"))
    missing = "table attempt_logs" if drop.startswith("TABLE") else "column attempts.reason"
    with pytest.raises(migrations.MigrationError, match=f"missing {missing}"):
        await fresh.create_schema()
    # Nothing was recorded: the schema is not taken to be at any revision.
    async with fresh.transaction() as tx:
        assert not await tx.connection.run_sync(
            lambda connection: inspect(connection).has_table("alembic_version")
        )


async def test_a_schema_at_an_earlier_revision_is_upgraded_and_keeps_its_rows(
    fresh: Database,
) -> None:
    assert migrations.needs_upgrade(migrations.FIRST_REVISION)
    job_id = uuid.uuid4()
    async with fresh.transaction() as tx:
        await tx.execute(text(f"CREATE SCHEMA {fresh.pg_schema}"))
        await tx.connection.run_sync(
            lambda connection: migrations.upgrade(
                connection, fresh.pg_schema, migrations.FIRST_REVISION
            )
        )
        await tx.execute(jobs.insert().values(id=job_id, workflow_name="w", root="root"))
        await tx.execute(entities.insert().values(cid=b"code", type_tag="t", data=b""))
        held, unheld = uuid.uuid4(), uuid.uuid4()
        await tx.execute(
            job_attempt_provenance.insert(),
            [
                {"id": held, "job_id": job_id, "code_version_cid": b"code"},
                {"id": unheld, "job_id": uuid.uuid4(), "code_version_cid": b"code"},
            ],
        )
    assert await revision(fresh) == migrations.FIRST_REVISION
    await fresh.create_schema()
    assert await revision(fresh) == head()
    async with fresh.transaction() as tx:
        assert await tx.connection.run_sync(differences) == []
        assert (await tx.execute(select(jobs.c.id))).scalars().all() == [job_id]
        # Revision 0002's table: created empty, and filled only by an import.
        assert (await tx.execute(select(job_entities))).all() == []
        # Revision 0003's column: a fact of a job not held here came with another job's record.
        rows = (await tx.execute(select(job_attempt_provenance))).all()
        assert {row.id: row.from_other_job for row in rows} == {held: False, unheld: True}


async def test_each_revision_after_the_first_can_be_undone_and_done_again(
    fresh: Database,
) -> None:
    await fresh.create_schema()
    async with fresh.transaction() as tx:
        await tx.connection.run_sync(
            lambda connection: migrations.downgrade(
                connection, fresh.pg_schema, migrations.FIRST_REVISION
            )
        )
    assert await revision(fresh) == migrations.FIRST_REVISION
    async with fresh.transaction() as tx:
        with pytest.raises(RuntimeError, match="would delete every job"):
            await tx.connection.run_sync(
                lambda connection: migrations.downgrade(connection, fresh.pg_schema, "base")
            )
    await fresh.create_schema()
    assert await revision(fresh) == head()
    async with fresh.transaction() as tx:
        assert await tx.connection.run_sync(differences) == []


async def test_upgrading_is_repeatable_and_concurrent_upgrades_take_turns(
    fresh: Database,
) -> None:
    async with anyio.create_task_group() as group:
        for _ in range(4):
            group.start_soon(fresh.create_schema)
    await fresh.create_schema()
    assert await revision(fresh) == head()


async def test_concurrent_upgrades_of_different_schemas_in_one_process_take_turns(
    fresh: Database,
) -> None:
    """Alembic keeps the running migration in module-level state, so one process's upgrades of
    several schemas must not interleave: each is built whole, and each ends at the head."""
    others = [Database(fresh.url, pg_schema=f"test_{uuid.uuid4().hex}") for _ in range(3)]
    try:
        async with anyio.create_task_group() as group:
            for database in (fresh, *others):
                group.start_soon(database.create_schema)
        for database in (fresh, *others):
            assert await revision(database) == head()
            async with database.transaction() as tx:
                assert await tx.connection.run_sync(differences) == []
    finally:
        for database in others:
            with anyio.fail_after(30):
                await database.drop_schema(lock_timeout="5s")
            await database.dispose()
