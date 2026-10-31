# basic_project

A small fxtr project, set up the way a user's project is: a uv project of its own whose code is a
package of steps and workflows, with launcher scripts at its root that run them as jobs. It holds
the example experiments (`src/basic_project/`): a rollout-and-grade experiment, an iterative bio
refinement with child workflows and data-dependent control flow, nested sweeps over ragged
context, a walkthrough of project arrays, and two slow runs made for watching in the viewer. Each
module's docstring says what it shows.

## Setup

The project is independent of the repository's uv workspace: it has its own `.venv` and `uv.lock`,
and depends on the repository's `fxtr` as an editable path dependency. From this directory:

```bash
uv sync --group dev                          # the venv, with fxtr installed editable
cp fxtr.local.toml.example fxtr.local.toml   # then point database_url at your Postgres server
```

(`uv run fxtr init --database-url URL` writes the same file and creates the tables.)

`fxtr.local.toml` is gitignored: it says where *this machine* reaches the project's database.
`postgres_env/` at the repository root runs a server the example file already points at. The
project's tables are created on the first launch (in the `fxtr_basic_project` schema by default).

## Running

```bash
uv run python launch_rollout_experiment.py                  # as a job of this project
uv run python launch_bio_refinement.py                      # iterative bio refinement
uv run python launch_project_arrays.py                      # the project arrays walkthrough
uv run python launch_model_harness_sweep.py                 # nested sweeps over ragged context
uv run python launch_slow_mapped.py                         # a job to watch: ~4 minutes of steps
uv run python launch_huge_grid.py                           # a job to watch: 5,000 mapped steps
uv run python launch_array_queries.py                       # analysis with array queries
```

The last two take options of their own, such as `--concurrency` and `--delay-scale` (see `--help`).
They also accept `--allow-staged` to pin staged changes and `--no-verify` to skip the venv check.

A launch pins the code it runs, so commit changes first. It also checks that the venv matches
the lock (`uv sync`). Since this project lives inside the fxtr repository, a
launch here pins fxtr's own commit, with this directory as the project's path within it.

The `fxtr` command works on the project the working directory is in. It can launch a workflow by
its registered name, with scalar or project-array inputs (anything richer is what the launchers
are for), and inspect and manage the project's jobs and step cache:

```bash
uv run fxtr run rollout_experiment --input n_samples=2   # a job, run here until it stops
uv run fxtr jobs list                                    # every job, newest first
uv run fxtr jobs status <job>                            # its state, result, and errors
uv run fxtr resume <job>                                 # finish a stopped job
uv run fxtr cache list root/grade                        # cache entries under a key
uv run fxtr cache clear root/grade                       # forget them, to rerun those steps
```

`fxtr run` and `fxtr resume` accept `--allow-staged` to pin staged changes and `--no-verify` to
skip the environment check. Pins accumulate under `refs/fxtr/pins/`; there is no command to prune
them yet.

Watch runs in the experiment viewer (`frontend/` at the repository root): start
`uv run fxtr view` in a second terminal and leave it running. It serves the viewer with this
project's database (in this checkout, the viewer's dev server; see `frontend/README.md`) and opens
it in a browser, and each launch shows its job there: its open tabs switch to it, or a new tab
opens when none is open.

The project's renderers are the `views/` package (`[tool.fxtr.views]` in `pyproject.toml`), which
the viewer loads as a built bundle. After the core repository setup and `pnpm --dir views install`
from this project, `uv run fxtr view` builds it, and rebuilds it whenever a renderer changes while
it runs; the open viewer shows each build without a reload. To build it without the viewer:

```bash
uv run fxtr views build     # npm run build in views/, into views/dist
```

## Layout

```
pyproject.toml                the project, its dependency on fxtr, and [tool.fxtr] (the modules
                              whose import registers this project's steps and workflows, and
                              [tool.fxtr.views], its renderer package)
fxtr.local.toml.example       what a local configuration looks like
launch_*.py                   workflow launchers: running.launch_cli for Postgres jobs;
                              slow_mapped and huge_grid supply their parsed flags and inputs
views/                        the project's renderer package (basic-project-views): its bundle's
                              entry is src/bundle.ts, built into views/dist
src/basic_project/
    rollout_experiment.py     entity types, typed steps, replicas, a workflow over entity arrays
    bio_refinement.py         child workflows mapped over an array, observing values to decide what
                              to schedule next
    model_harness_sweep.py    full and filtered model/harness/task sweeps with ragged
                              model-specific context and nested workflows
    project_arrays.py         the project arrays walkthrough, over the ProjectClient
    slow_mapped.py            two lanes of slow mapped steps with progress logs, to watch
    huge_grid.py              a 5,000-cell mapped step and per-slice summaries, to watch
    array_queries.py          analyzing a model evaluation with array queries, in a workflow and
                              in a step
```

The workflow launchers call `fxtr.project.running.launch_cli` to run Postgres jobs.
`launch_cli` handles execution and reporting, including opening the local client. Its output is
shared with `fxtr run` and `fxtr resume`. The slow and huge-grid launchers parse their own flags
and pass prepared inputs, `max_concurrency`, and environment settings to `launch_cli`.
All launches open their job in the project's running viewer (or say how to start one)
immediately, then print status, errors, and the result.
Async scripts can call `run_job` with their own open client.
A launcher can supply an
async `on_result(storage, result)` callback to print a custom report while the result's storage is
open. The project arrays
walkthrough uses `fxtr.project.formatting.format_array` for its intermediate output.

## Tooling

Ruff runs from the repository root with the shared configuration (`uv run ruff check
example_projects`). Pyright runs in this project's venv (`uv run --group dev pyright` here), so
imports resolve against it; the repository's pre-commit hooks run it and check the lock. fxtr's own
test suite imports this package through pytest's `pythonpath` to run the experiments and
the walkthrough against a test database, and `fxtr/tests/test_example_project_launch.py` (opt-in:
`FXTR_TEST_EXAMPLE_PROJECT=1`) copies the project into a fresh git repository and runs a launcher
there through `uv run`, end to end.

When fxtr's dependencies change, run `uv lock` here too; the `uv-lock-basic-project` pre-commit
hook says when.
