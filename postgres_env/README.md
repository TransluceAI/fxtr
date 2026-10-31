# Local Postgres for the fxtr runner

The persistent runner (`fxtr.runner`, installed with the `fxtr[runner]` extra) keeps all
experiment state in a Postgres server. This directory runs one locally in a container. It is a
development convenience: nothing in `fxtr` depends on these files, only on a connection URL.

Any Compose-compatible container runtime works. With podman:

```bash
cd postgres_env
cp .env.example .env          # edit the port or credentials if you like
podman compose up -d --wait   # or: docker compose up -d --wait
```

The server listens on the port in `.env` (`55433` by default, to stay clear of any Postgres
already on `5432`). Point fxtr at it with a SQLAlchemy URL using the `asyncpg` driver:

```bash
export FXTR_DATABASE_URL=postgresql+asyncpg://fxtr:fxtr@localhost:55433/fxtr
```

The Postgres-backed tests run only when `FXTR_TEST_DATABASE_URL` is set; they create and drop a
throwaway Postgres schema per test, so it can point at the same database:

```bash
export FXTR_TEST_DATABASE_URL=$FXTR_DATABASE_URL
uv run pytest -n auto
```

For a walkthrough of the client against this server, run (from `fxtr/`):

```bash
uv run python examples/example_project_arrays.py
```

To inspect the database with `psql` (bundled in the image):

```bash
podman compose exec postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

`podman compose down` stops the server and keeps its data; `podman compose down --volumes`
deletes the data too.

## Existing project schemas

Each project's `fxtr.local.toml` selects a schema within the database. fxtr creates missing tables
but does not migrate existing tables. After switching fxtr revisions, an error about a missing
column can mean the stored schema no longer matches the code. Restarting the viewer or running
`fxtr init --force` does not migrate those tables.

For development runs where you can start with empty jobs, project arrays, and cache:

1. Finish or stop the project's running jobs, then stop its viewer.
2. Record the current `schema` from `fxtr.local.toml`. If omitted, it defaults to `fxtr_` followed
   by the project name, lowercased with characters other than letters, digits, and `_` replaced
   by `_`.
3. Set `schema` to an unused name, keeping the database URL unchanged. For example:

   ```toml
   schema = "fxtr_my_project_scratch"
   ```

4. Start `uv run fxtr view` again, open its printed URL, and launch a new job. The viewer creates
   the current tables in the fresh schema.

The old schema and its data remain stored, but its jobs are absent from the fresh schema's viewer.
Reading the old jobs requires compatible fxtr code or a data migration; switching schemas does
not migrate them. If you need those jobs in the updated viewer, preserve the old schema and
migrate its data instead. There is no need to delete the Postgres database or container volume.
