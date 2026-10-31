# fxtr and behaviors — code overview

This repository holds fxtr, a configuration and orchestration system for modular, reproducible
experiments; behaviors, a library of chat-model rollouts built on fxtr's entities; the experiment
viewer and the TypeScript libraries its renderers are written with; and example projects built on
them. This file is the map at the level of the repository: which units there are, how they depend
on and talk to each other, and the tooling they share. Each unit has its own `CODE_OVERVIEW.md`,
which is where to go next. `AGENTS.md` says how the overviews are organized and kept; `README.md`
has setup and development commands.

## Units

| Unit | What it is | Depends on |
|---|---|---|
| `fxtr/` | The production package: content-addressed entities, arrays and array queries, experiment definitions (steps and workflows), the Postgres-backed runner, the viewer's backend, and the project, its client, and the `fxtr` command line | — |
| `behaviors/` | A production package: chat-model rollouts, context and tool policies, provider adapters, and conversation records stored as fxtr entities | fxtr |
| `ts/` | The TypeScript libraries of the visualization system: `fxtr-view`, the helpers shells build on, the view bundle preset, the default renderers, and shared conversation views | — |
| `frontend/` | The experiment viewer: a Vite/React app, and a shell of the visualization system, over the viewer backend in fxtr | ts, fxtr's viewer API |
| `example_projects/basic_project/` | The example project, set up the way a user's is, and the home of fxtr's example experiments | fxtr, ts |
| `example_projects/behavior_rollouts/` | The maintained example of a project using behaviors | fxtr, behaviors, ts |

## Repository layout

```
.
├── AGENTS.md               shared working conventions, including how the code overviews are kept
├── CLAUDE.md -> AGENTS.md  Claude Code entry point for the same instructions
├── README.md               setup, the environment model, and development commands
├── CODE_OVERVIEW.md        this file
├── LICENSE, NOTICE         Apache License 2.0, and the copyright notice: copies of fxtr's
├── .agents/skills/         the fxtr, behaviors, and first-experiment skills, discovered directly
│   │                       by Codex, and distributed, with copies of the docs/ pages they link,
│   │                       in the fxtr plugin for Claude Code and Codex
│   ├── fxtr/               SKILL.md: when it applies, the docs/fxtr/ pages each task needs, and
│   │                       best practices for agents
│   ├── behaviors/          SKILL.md: the same for calling models with behaviors (docs/behaviors/)
│   └── first-experiment/   SKILL.md: a guided walkthrough of a newcomer's first experiment with
│                           language models, from their question to results
├── .claude/skills -> ../.agents/skills  Claude Code discovery of the same skills
├── pyproject.toml          the uv workspace, shared development dependencies, Ruff policy, and the
│                           root pytest configuration
├── pyright-base.json       shared Pyright policy
├── uv.lock                 resolved lockfile for the workspace
├── .python-version         workspace interpreter pin (3.12.4, the oldest supported)
├── .nvmrc                  Node major shared by local development and CI
├── .pre-commit-config.yaml
├── .github/workflows/      ci.yml, continuous integration; see Tooling
├── scripts/release_version.py  checks and sets fxtr's and behaviors' versions and fxtr range
├── scripts/skills/         Node/remark preparation and plugin sync for Claude Code and Codex
│                           (CODE_OVERVIEW.md)
├── package.json            the pnpm workspace root, and `pnpm build:dist`, which builds the viewer
│                           and then fxtr's sdist and wheel
├── pnpm-workspace.yaml     the pnpm workspace's members
├── pnpm-lock.yaml
├── postgres_env/           a local Postgres server for the runner (compose file, .env.example,
│                           README)
├── docs/                   the Mintlify site for docs.transluce.ai, and the reference for using
│                           fxtr and behaviors: docs.json (configuration, navigation, and
│                           redirects), index.mdx (the platform home), fxtr/ (introduction,
│                           installation, the guided first experiment, concepts/, guides/, and
│                           reference/), and behaviors/ (introduction, guides/, and reference/);
│                           images/ holds page illustrations. The skills link its pages and ship
│                           copies of them. Its README.md covers local preview (`npx mint dev`)
│                           and keeping pages consistent with the code and their links resolvable
│                           in the skills' copies
├── fxtr/                   a unit
├── behaviors/              a unit
├── ts/                     a unit
├── frontend/               a unit
└── example_projects/       maintained example projects
    ├── README.md           what the example projects share: setup, the viewer, testing
    ├── example_project_testing.py  the Postgres test fixture the example projects share
    ├── basic_project/      a unit
    └── behavior_rollouts/  a unit
```

## How the units fit together

**Dependencies.** behaviors depends on fxtr, and fxtr on nothing else here. A project depends on
fxtr, and on behaviors if it calls models through it, as editable path dependencies on this
checkout, or as packages from PyPI. Renderer code depends on `ts/`'s `fxtr-view`; the frontend depends on the `ts/` packages
it is a shell over, and reaches fxtr only over HTTP.

**The life of a job, across units.** A project's launcher, or `fxtr run`, uses fxtr's project
layer to locate the project, pin the code it runs, and submit a workflow to the Postgres runner,
which runs the step and workflow bodies in the launching process and records everything in the
project's database schema. `fxtr view` serves the viewer backend (in fxtr) and the frontend on one
origin. The frontend shows the job's invocations and arrays, and loads the project's *view bundle*,
built from the project's own renderer package with `ts/`'s bundle preset, to render the project's
entity types and step invocations. While it runs, `fxtr view` rebuilds that bundle as its sources
change and announces each build on the `/api/events` WebSocket, and the frontend swaps the new
renderers in without a reload. A launch finds the project's running viewer (`/api/info`) and
announces its job there (`/api/launches`): the open pages switch to it, and the launch opens a new
tab only when no page is open.

**The interfaces between units**, each defined in one place:

- **Stored values** are DAG-CBOR with a `$type` tag (fxtr's `core/codec.py` and
  `core/entity_data.py`); the frontend decodes them in the browser with `fxtr-view-shell-util`'s
  DRISL loader, so an entity id and a string stay distinct.
- **The viewer API** is GraphQL, a few plain routes, and the events WebSocket under `/api`,
  defined by fxtr's `runner/postgres/debug_web/` and read by `frontend/src/api/`.
- **The view bundle contract** is `fxtr-view`'s `view_bundle.ts` (`VIEW_API_VERSION`, the manifest,
  the shared modules); fxtr's `project/views.py` mirrors its constants, and a test checks the two
  agree.
- **Renderers are keyed by name**: entity renderers by `$type`, invocation renderers by a step's or
  workflow's registered name (`name=` of `@step` / `@workflow`).
- **Shipped files**: `fxtr/hatch_build.py` puts the built frontend and the `ts/` packages a
  project's renderers use into fxtr's sdist and wheel. An installed fxtr serves the viewer without
  Node or this repository, and a project outside the checkout can build renderers against its own
  copy of the packages (`fxtr views vendor`).

## Tooling

- **Python environments.** `fxtr/` and `behaviors/` are members of the uv workspace in the root
  `pyproject.toml`, sharing its `.venv`, `uv.lock`, development dependencies, and Ruff and Pyright
  policy; each keeps its runtime dependencies, pytest configuration, and Pyright scope in its own
  `pyproject.toml`. Both use a src layout, where hatchling derives the package directory from
  `[project].name`, so the name and the `src/` subdirectory must stay in sync. The example
  projects are independent uv projects with their own venvs and lockfiles.
- **TypeScript.** The root pnpm workspace contains `ts/packages/*` and `frontend`. The example
  projects own separate renderer environments, with local workspace boundaries, lockfiles, and
  installations; they link the shared TypeScript source by path. `pnpm install` at the root
  installs the core libraries, the viewer, and the Node skill-preparation tools. The root `.nvmrc`
  selects Node for nvm and CI's `setup-node`; `package.json` declares the supported Node range
  and pins pnpm. Setup commands live in `README.md`.
- **Tests.** The root pytest configuration collects fxtr's, behaviors', and the example projects'
  tests and excludes live provider calls (`llm_integration`) by default; Postgres tests need
  `FXTR_TEST_DATABASE_URL`.
- **Pre-commit** runs Ruff, the lockfile checks, Pyright for each package and example project, a
  check of fxtr's and behaviors' versions and behaviors' fxtr range
  (`scripts/release_version.py check`), and a check that every copy of the `LICENSE` and `NOTICE`
  matches.
- **CI** (`.github/workflows/ci.yml`, GitHub Actions) runs on every pull request and push to main:
  the pre-commit hooks over all files; the pytest suite against a Postgres service; behaviors'
  tests against the lowest fxtr its range allows, from PyPI; the pnpm workspace's typecheck and
  tests, including the skill preparation and plugin sync tests; each example's renderer
  environment; and `pnpm build:dist` with behaviors' build.
- **The skills' plugin.** The fxtr, behaviors, and first-experiment skills and the documentation
  they link reach users as the `fxtr` plugin in two public marketplaces,
  TransluceAI/claude-code-plugins (Claude Code) and TransluceAI/codex-plugins (Codex), each plugin
  with a version of its own. The Node tooling in `scripts/skills/` (its code overview) prepares the
  documentation and syncs local marketplace checkouts; a pull request in each publishes it.
- **Releases.** fxtr and behaviors have independent PEP 440 versions, and behaviors requires a
  bounded range of fxtr versions; `scripts/release_version.py` (its docstring has the rules) checks
  and sets them.
