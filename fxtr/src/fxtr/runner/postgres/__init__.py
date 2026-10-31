"""The Postgres-backed runner: experiment state kept in a Postgres database.

Requires the ``fxtr[runner]`` extra (SQLAlchemy Core on the asyncpg driver). :mod:`.database`
owns the connection, the schema's lifecycle, and the in-process wake; :mod:`.schema` declares the
tables and documents what they mean; :mod:`.entities` is the entity store and :mod:`.project_arrays`
the project arrays; :mod:`.ledger` holds the facts of the durable record and :mod:`.execution` the
execution state and the policy deciding what runs next; :mod:`.runner` is the
:class:`~fxtr.runner.postgres.runner.JobRunner`, the process that runs one job; :mod:`.jobs` the
job commands; and :mod:`.client` the :class:`~fxtr.runner.postgres.client.LocalProjectClient`, the
:class:`~fxtr.project.client.ProjectClient` that runs the jobs it submits in this process.
"""
