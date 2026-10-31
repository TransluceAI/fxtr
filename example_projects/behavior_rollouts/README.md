# Behaviors rollout examples

`scripted_rollout` uses a deterministic EchoAPI to demonstrate independent two-turn conversations.
`openai_rollout` compares models over ten named, five-question sequences. Each sequence keeps
its own conversation history; each model/sequence pair is a separate fxtr step. Rollouts are
stored as behaviors `ChatRollout` entities and can be inspected and compared in the viewer.

## Setup and launch

Each directory is an independent uv/fxtr project with its own lockfile and environment. Its
`[tool.fxtr]` lists the modules the CLI and viewer import; `[tool.fxtr.views]` points to its
`views/` bundle. The editable `fxtr` and `behaviors` dependencies come from this checkout.
From the project's directory:

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

Select Node using the [repository JavaScript setup](../../README.md#javascript-setup). Install JavaScript
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

```bash
uv run python launch_scripted.py
uv run python launch_openai.py --model MODEL --model ANOTHER_MODEL
uv run python launch_openai.py --model MODEL --question "What is a prime?" --question "Give an example."
```

The scripted example needs Postgres but makes no provider calls. The OpenAI example requires
`OPENAI_API_KEY`, accepts `--max-tokens` and `--concurrency`, and uses a fresh cache namespace on
each launch. `--question` replaces the default dataset with one ordered sequence. The workflow's
**compare** panel aligns two models' replies by turn and retains their system initialization.

## Tests

The code and its tests are mapped in `CODE_OVERVIEW.md`. From this directory:

```bash
uv run --group dev pytest -n auto
uv run --group dev pyright
pnpm --dir views test
```
