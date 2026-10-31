# Example projects

The projects here are officially maintained examples of how to use fxtr and behaviors.

- `basic_project/`: fxtr workflow, array, mapping, and visualization examples using mock data.
- `behavior_rollouts/`: deterministic EchoAPI conversations and a multi-model OpenAI sweep over
  ordered question sequences, with transcript and side-by-side comparison views.

Each directory is an independent uv/fxtr project with its own lockfile and environment. Its
`[tool.fxtr]` lists the modules the CLI and viewer import; `[tool.fxtr.views]` points to its
`views/` bundle. The editable `fxtr` dependency, and `behaviors` for `behavior_rollouts`, come
from this checkout. From the project's directory:

```bash
uv sync --group dev
cp fxtr.local.toml.example fxtr.local.toml
```

The example local config uses the server in `../../postgres_env/`. Each project has a separate
schema, so its jobs and cache are independent. `FXTR_DATABASE_URL` can override the configured
server; put a different schema in `fxtr.local.toml` for scratch runs. Commit the code and lockfile
before launching: fxtr pins the checkout and verifies the project's environment. Launchers also
accept `--allow-staged` to pin staged changes and `--no-verify` to skip environment verification.
Credentials stay in the process environment.

For missing-column errors after changing fxtr revisions, see
[existing project schemas](../postgres_env/README.md#existing-project-schemas), including how to
start fresh while preserving the old data.

Select Node using the [repository JavaScript setup](../README.md#javascript-setup). Install JavaScript
dependencies with `pnpm --dir views install` from the project, after the core repository setup.
Commit `views/pnpm-lock.yaml` with the project. From the project:

```bash
uv run fxtr view    # in a second terminal; leave it running
```

`fxtr view` builds this project's renderers, serves the viewer with its jobs (in this checkout,
the viewer's dev server, from the repository root's `pnpm install`), and opens it in a browser.
While it runs, its open tabs switch to each job you launch (a new tab opens when none is open),
and saving a renderer updates the open viewer without a reload. Manage jobs with
`uv run fxtr jobs list`, `uv run fxtr jobs status JOB`, and `uv run fxtr resume JOB`; resume uses
the project's registered modules and stored inputs.

Multiple project viewers can run at once: `fxtr view` chooses an available port automatically.
See [the viewer guide](../frontend/README.md#running-it) for the remembered port, the fallback
range, and how `--port` requires an exact port.

Each project README describes its experiment and launch options. Source is in `src/<package>/`,
launchers are at the project root, and `views/` contains the project's renderers and bundle.
The reusable conversation renderers are `../ts/packages/fxtr-view-behaviors`.

Run `uv run --group dev pytest -n auto` and `uv run --group dev pyright` from
`behavior_rollouts/`. The repository's full pytest suite also collects its tests; the basic
project's tests live in `fxtr/tests/`. Workflow tests need `FXTR_TEST_DATABASE_URL`; they use
throwaway schemas and fake code pins. Pure data and scoring unit tests run without a database.
Provider calls require the explicit `llm_integration` marker. `example_project_testing.py`
supplies the shared database fixture, importing the repository's fake environment from
`fxtr/tests`; launchers use the real project environment.
