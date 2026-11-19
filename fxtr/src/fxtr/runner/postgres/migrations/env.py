"""Alembic's entry point for the runner's migrations, run by
:func:`fxtr.runner.postgres.migrations.upgrade`.

It runs on the connection the caller passed in ``config.attributes``, inside the caller's
transaction, so a migration commits or rolls back with the rest of ``Database.create_schema``.
"""

from alembic import context

connection = context.config.attributes["connection"]
pg_schema = context.config.attributes["pg_schema"]

context.configure(connection=connection, version_table_schema=pg_schema, transactional_ddl=True)
with context.begin_transaction():
    context.run_migrations()
