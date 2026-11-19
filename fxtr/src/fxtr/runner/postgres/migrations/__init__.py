"""Migrations of the runner's tables, so a database outlives the fxtr version that created it.

A runner database is kept across fxtr upgrades (a project's own, and every project schema a hub
keeps), so its tables change by migration, never by recreating them. The migrations are Alembic
revisions in ``versions/``, one file each, applied in order; the database records the revision it
is at in an ``alembic_version`` table beside the runner's tables. :func:`upgrade` brings a schema to
the newest revision, and :meth:`fxtr.runner.postgres.database.Database.create_schema` calls it.

``schema.py`` stays the description of the tables as they are now; a change to it comes with a
revision that makes the same change to an existing database.
``tests/test_runner_postgres_migrations.py`` checks that a schema built by the revisions is exactly
what ``schema.py`` declares, so a change to one without the other fails there.

A schema whose tables exist but which has no ``alembic_version`` was created before fxtr managed
its tables with migrations (by creating every table ``schema.py`` declared). It is taken to be at
the first revision, ``0001``, and upgraded from there, but only if it has every table and column
that revision creates (:data:`FIRST_REVISION_TABLES`); a schema an older fxtr created lacks some,
and is refused with :class:`MigrationError` rather than recorded as a revision it is not at.

**Give each revision a ``downgrade``** that undoes it, where that is possible, so a revision can
be undone by hand (:func:`downgrade`); where it is not, as for the first revision, which creates
every table, ``downgrade`` raises and says why. A release never runs a downgrade.

**Write each revision so that the previous fxtr release still works on the migrated tables.** Add
a column or a table in one release, and remove the old one in a later release. A hub keeps one
runner schema for each of its projects, and it does not migrate them all at the same time. For
some minutes, the previous release and the new release run together, and either one can write to
a schema that the new release migrated. When an operator goes back to the previous release, the
migrated schemas stay migrated. A revision that the previous release cannot use breaks the hub in
both of these cases.
"""

from __future__ import annotations

import functools
from collections.abc import Mapping
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Inspector, inspect

from fxtr.runner.postgres.schema import metadata

_DIRECTORY = Path(__file__).parent

FIRST_REVISION = "0001"
"""The revision of the baseline migration: the tables as the first fxtr with migrations had
them."""

JOB_ENTITIES_REVISION = "0002"
"""The revision that adds ``job_entities``. A schema upgraded across it holds jobs the table
does not name yet; a caller that serves scoped reads records them (see
``fxtr.runner.postgres.job_records.record_job_entities``)."""


def is_before(revision: str | None, other: str) -> bool:
    """Whether a schema at ``revision`` (``None``: no revision recorded, as a schema from before
    the migrations has) is from before ``other`` was applied. Revision ids are zero-padded
    numbers, so their order is their text order."""
    return revision is None or revision < other


# Frozen here rather than derived from schema.py, which declares the tables as the newest
# revision leaves them: later revisions add to what 0001 creates (0002 adds job_entities, 0003 a
# column of job_attempt_provenance, 0004 attempt_previews), and a schema created before migrations
# has exactly 0001's tables, or those and ALSO_CREATED_BEFORE_MIGRATIONS. The migration tests
# check this list against what revision 0001 builds.
FIRST_REVISION_TABLES: Mapping[str, frozenset[str]] = {
    "entities": frozenset({"cid", "type_tag", "data", "created_at"}),
    "project_arrays": frozenset({"name", "type", "version", "created_at", "updated_at"}),
    "project_array_rows": frozenset({"array_name", "key", "value"}),
    "project_array_snapshots": frozenset({"array_name", "version", "cid", "created_at"}),
    "jobs": frozenset({"id", "workflow_name", "root", "created_at"}),
    "workflow_invocations": frozenset(
        {
            "id",
            "job_id",
            "pathname",
            "workflow_name",
            "cache_key_prefix",
            "returned_node",
            "result_cid",
            "job_attempt_id",
            "created_at",
            "finished_at",
        }
    ),
    "spawns": frozenset(
        {
            "invocation_id",
            "node_identity",
            "key",
            "child_invocation_id",
            "step_cache_key",
            "created_at",
        }
    ),
    "nodes": frozenset(
        {"invocation_id", "identity", "kind", "params", "type", "value_cid", "created_at"}
    ),
    "node_inputs": frozenset({"invocation_id", "node_identity", "label", "input_identity"}),
    "node_names": frozenset({"invocation_id", "name", "identity"}),
    "job_attempt_provenance": frozenset({"id", "job_id", "code_version_cid", "created_at"}),
    "attempts": frozenset(
        {
            "id",
            "kind",
            "invocation_id",
            "cache_key",
            "request_cid",
            "owner_job_attempt_id",
            "state",
            "error",
            "reason",
            "stopped_on",
            "started_at",
            "finished_at",
        }
    ),
    "attempt_events": frozenset({"attempt_id", "seq", "kind", "identity", "at"}),
    "attempt_logs": frozenset({"attempt_id", "seq", "message", "at"}),
    "step_requests": frozenset({"request_cid", "step_name", "created_at"}),
    "job_step_requests": frozenset({"job_id", "cache_key", "request_cid", "requested_at"}),
    "step_results": frozenset(
        {"id", "request_cid", "result_cid", "job_attempt_id", "completed_at"}
    ),
    "job_step_bindings": frozenset({"job_id", "cache_key", "step_result_id", "bound_at"}),
    "step_cache": frozenset(
        {"cache_key", "request_cid", "attempt_id", "step_result_id", "created_at", "updated_at"}
    ),
    "job_attempts": frozenset(
        {
            "id",
            "job_id",
            "state",
            "reason",
            "owner",
            "heartbeat_at",
            "evicted_at",
            "evicted_reason",
            "overwrite_cache_conflicts",
            "settled_at",
        }
    ),
}
"""The tables :data:`FIRST_REVISION` creates and the columns of each: what a schema created
before migrations must have to be taken as at that revision."""


class MigrationError(RuntimeError):
    """A schema's tables cannot be brought to the newest revision."""


def head_revision() -> str:
    """The newest revision, which :func:`upgrade` brings a schema to by default."""
    head = _scripts().get_current_head()
    assert head is not None, "the runner has no revisions"
    return head


def current_revision(connection: Connection, pg_schema: str | None) -> str | None:
    """The revision that the runner's tables in ``pg_schema`` are at. Returns ``None`` if the
    schema records no revision: it has no runner tables, or a fxtr created them before it used
    migrations."""
    return MigrationContext.configure(
        connection, opts={"version_table_schema": pg_schema}
    ).get_current_revision()


def needs_upgrade(revision: str | None) -> bool:
    """Whether tables at ``revision`` must be upgraded before this fxtr uses them.

    This is true for ``None`` (no recorded revision) and for each revision of this fxtr that is
    not the newest. It is false for the newest revision. It is also false for a revision that
    this fxtr does not know: a later fxtr made it, and this fxtr can use those tables, by the rule
    in the module docstring.
    """
    if revision is None:
        return True
    return revision != head_revision() and _known(revision)


def _known(revision: str) -> bool:
    # Exact membership: Alembic's get_revision also resolves a prefix of one revision's id,
    # which would take a revision this fxtr does not know for one it does.
    return revision in _revisions()


def known_revisions() -> frozenset[str]:
    """The ids of every revision this fxtr ships."""
    return _revisions()


@functools.cache
def _scripts() -> ScriptDirectory:
    """The revisions in ``versions/``. They do not change while a process runs, so they are
    read once."""
    return ScriptDirectory.from_config(_config())


@functools.cache
def _revisions() -> frozenset[str]:
    """The ids of every revision in ``versions/``."""
    return frozenset(script.revision for script in _scripts().walk_revisions())


ALSO_CREATED_BEFORE_MIGRATIONS: frozenset[str] = frozenset({"attempt_previews"})
"""Tables that a later revision than :data:`FIRST_REVISION` creates and that a fxtr from before
migrations created too: it created every table ``schema.py`` declared, and for a time that
included these. A schema with no recorded revision may have them. The revision that creates
each one leaves an existing table as it is."""


def _config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(_DIRECTORY))
    return config


def upgrade(connection: Connection, pg_schema: str | None, revision: str = "head") -> None:
    """Bring the runner's tables in ``pg_schema`` (the connection's default schema when ``None``)
    to ``revision``, the newest by default, in the connection's current transaction. If a later
    fxtr has migrated the tables (their revision is one this fxtr does not know), this does
    nothing: by the rule in the module docstring, this fxtr can use them as they are.

    The caller serializes concurrent upgrades: of one schema across processes
    (``Database.create_schema`` takes an advisory lock first; two processes entering a client at
    once would otherwise both apply the same revision), and of any schemas within one process
    (``create_schema`` holds a process-wide lock; Alembic keeps the running migration in
    module-level state, so two upgrades in one process would interleave in it). A caller that
    runs this function directly must run only one at a time in its process.

    Raises:
        MigrationError: If the schema has tables but no recorded revision, and lacks a table or
            column of :data:`FIRST_REVISION`.
    """
    current = current_revision(connection, pg_schema)
    if current is not None and not _known(current):
        return
    config = _config()
    config.attributes["connection"] = connection
    config.attributes["pg_schema"] = pg_schema
    inspector = inspect(connection)
    if current is None and inspector.has_table("jobs", schema=pg_schema):
        later = sorted(
            name
            for name in metadata.tables
            if name not in FIRST_REVISION_TABLES
            and name not in ALSO_CREATED_BEFORE_MIGRATIONS
            and inspector.has_table(name, schema=pg_schema)
        )
        if later:
            where = pg_schema or "the default schema"
            raise MigrationError(
                f"The runner's tables in {where} have no recorded revision but include"
                f" {', '.join(later)}, which a later revision than {FIRST_REVISION} creates:"
                " they were made without migrations; recreate the schema, or stamp its"
                " revision by hand."
            )
        missing = _missing_from_first_revision(inspector, pg_schema)
        if missing:
            where = pg_schema or "the default schema"
            raise MigrationError(
                f"The runner's tables in {where} predate migrations but do not match revision"
                f" {FIRST_REVISION}, so they cannot be upgraded: missing {', '.join(missing)}."
                " An older fxtr created them; recreate the schema, or bring it to that revision"
                " by hand."
            )
        command.stamp(config, FIRST_REVISION)
    command.upgrade(config, revision)


def downgrade(connection: Connection, pg_schema: str | None, revision: str) -> None:
    """Undo the revisions after ``revision`` in ``pg_schema``, in the connection's current
    transaction: for undoing a revision by hand. A release never runs it; going back a release
    leaves the tables as they are (see the module docstring).

    Raises:
        RuntimeError: If a revision cannot be undone; its ``downgrade`` says why.
    """
    config = _config()
    config.attributes["connection"] = connection
    config.attributes["pg_schema"] = pg_schema
    command.downgrade(config, revision)


def _missing_from_first_revision(inspector: Inspector, pg_schema: str | None) -> list[str]:
    present = set(inspector.get_table_names(schema=pg_schema))
    missing: list[str] = []
    for table, columns in FIRST_REVISION_TABLES.items():
        if table not in present:
            missing.append(f"table {table}")
            continue
        found = {column["name"] for column in inspector.get_columns(table, schema=pg_schema)}
        missing.extend(f"column {table}.{name}" for name in sorted(columns - found))
    return missing


__all__ = [
    "FIRST_REVISION",
    "FIRST_REVISION_TABLES",
    "MigrationError",
    "current_revision",
    "downgrade",
    "head_revision",
    "needs_upgrade",
    "upgrade",
]
