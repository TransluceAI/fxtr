# fxtr and behaviors

**fxtr** (pronounced "fixture") is a configuration and orchestration system for modular,
reproducible experiments with language models: content-addressed entities, arrays and array
queries, steps and workflows, a Postgres-backed runner, and an experiment viewer.
**behaviors** is a library of chat-model rollouts built on fxtr: context and tool policies,
provider adapters, and conversation records stored as fxtr entities.

- Documentation: [docs.transluce.ai](https://docs.transluce.ai)
- Packages: [`fxtr`](https://pypi.org/project/fxtr/) and
  [`behaviors`](https://pypi.org/project/behaviors/) on PyPI
- Agent skills: the `fxtr` plugin for
  [Claude Code](https://github.com/TransluceAI/claude-code-plugins) and
  [Codex](https://github.com/TransluceAI/codex-plugins)

This repository holds the source of both packages, the viewer and the TypeScript libraries its
renderers use, the documentation site, example projects, and the agent skills.

To use fxtr and behaviors, install them from PyPI and follow the documentation. The rest of this
README is for working in a checkout of this repository. `CODE_OVERVIEW.md` describes how the
repository is organized.

## Quickstart

Install uv 0.12.2 or newer, then run these commands from the repository root:

```bash
uv sync --all-packages --all-extras
source .venv/bin/activate
pre-commit install
```

`uv sync --all-packages --all-extras` installs both packages, their optional extras (such as
`fxtr[runner]`, the Postgres-backed runner), and the root `dev` dependency group into the shared
`.venv`. Activating the environment is convenient but not required when using `uv run`.

## JavaScript setup

Use Node 22 for the viewer, renderer builds, and preparing the agent skills for the plugin. The
root `.nvmrc` selects this major for both local development and CI; `package.json` declares Node
22 or newer. With nvm, run from the repository root:

```bash
nvm install
nvm use
pnpm install
```

Use the pnpm version pinned in `package.json` (10.12.2). This installs the TypeScript libraries,
the viewer, and the skill preparation tools. Example projects install their renderer
dependencies separately: run `pnpm --dir views install` from each project and commit its
renderer lockfile.
`uv run fxtr views build` runs `npm run build` with the Node and npm on your shell's `PATH`;
activating a Python environment does not select Node. After switching Node versions, check
`node --version` and `command -v npm` in the shell that launches fxtr. See
[the frontend guide](frontend/README.md) for viewer commands.

## Packages and tests

`fxtr/` and `behaviors/` are members of the uv workspace declared in the root `pyproject.toml`.
They share the root `.venv`, the root `uv.lock`, development dependencies such as Ruff, Pyright,
pytest, and pre-commit, and repository-wide Ruff and Pyright policy. Runtime dependencies belong
in each package's own `pyproject.toml`, as do its pytest configuration and Pyright source scope.
Each package's Pyright section extends `pyright-base.json`. To add another production package,
add its directory to `[tool.uv.workspace].members` in the root `pyproject.toml`, declare its
runtime dependencies in its own `pyproject.toml`, and extend the shared Pyright configuration.

Run both packages' tests, and the example projects', from the repository root with:

```bash
uv run --all-packages --all-extras pytest -n auto
```

Live provider tests are excluded by default. Postgres tests require `FXTR_TEST_DATABASE_URL`;
`postgres_env/` runs a local Postgres server for them (its `README.md` has the commands). Run the
TypeScript typechecks and tests with `pnpm typecheck` and `pnpm test`, after the JavaScript setup
above; `pnpm test` includes the skill preparation and plugin sync tests, which
`pnpm test:skills` runs alone.

## Example projects

[`example_projects/`](example_projects/README.md) holds maintained examples, each an independent
uv project set up the way a user's is, with fxtr (and behaviors) as editable path dependencies on
this checkout:

- `basic_project/`: fxtr's workflows, arrays, mappings, and visualizations on mock data. fxtr's
  tests import its package to run the example experiments.
- `behavior_rollouts/`: scripted and OpenAI rollouts with behaviors, with its own database
  schema, launchers, and view bundle.

Jobs run from a project's launchers or from the `fxtr` command (`fxtr run`, `fxtr jobs`,
`fxtr cache`, `fxtr view`, ...).

## A project of your own

`uv run fxtr new <dir>` writes an empty project with an empty views bundle; add `--example` to
include a runnable greeting experiment, its launcher, greeting renderers, and a results overview.
[Project configuration](docs/fxtr/reference/project-configuration.mdx) describes what it writes,
and [Your first experiment](docs/fxtr/first-experiment.mdx) has an agent guide a newcomer through
an experiment in one.

The documentation describes a project that installs fxtr from PyPI. Made from this checkout, a
project differs in a few ways:

- It depends on the checkout's fxtr by path (`[tool.uv.sources]`). To use behaviors, add
  `"behaviors[models]"` to `dependencies` and
  `behaviors = { path = "<checkout>/behaviors", editable = true }` to `[tool.uv.sources]`.
- Outside the checkout, its renderer package vendors the TypeScript packages, as for a PyPI
  install.
- `postgres_env/` runs a local Postgres server at the URL the documentation uses.
- `fxtr view` runs the viewer's dev server from the checkout; see
  [the frontend guide](frontend/README.md).

## Agent skills

The `fxtr`, `behaviors`, and `first-experiment` skills live in `.agents/skills/`, where Codex
finds them; `.claude/skills` is a relative symlink to that directory for Claude Code. Each skill
links the pages of `docs/` it needs by relative path, so an agent in this checkout reads them
where they are written. Outside this repository, they are distributed as the `fxtr` plugin, with
copies of their documentation and a `provenance.json` recording its source. Write the skills and
pages for someone who installed fxtr and behaviors from PyPI: no paths into this repository. A
skill links only pages of `docs/` and the other shipped skills; preparation refuses broken links.
A new shipped skill is added to `skillNames` in `scripts/skills/bundle.mjs`.

To update the plugins, complete the JavaScript setup above, then run
`pnpm sync:plugin PATH/TO/claude-code-plugins PATH/TO/codex-plugins` against checkouts of those
repositories (either one alone also works) and open a pull request in each with the result. The
command prepares the skills once, copies them into each checkout's plugin, and bumps that plugin's
patch version when content changes, including documentation alone. It tells the two kinds of
checkout apart by their marketplace files. Unchanged content writes nothing, even when its source
commit changed. Generated copies may normalize formatting; authored sources are never modified. To
inspect a bundle directly, run `node scripts/skills/prepare.mjs . /tmp/fxtr-skills-preview` with a
new output directory.

Repository instructions for coding agents live in `AGENTS.md`, with `CLAUDE.md` linked to it.

## Versions

fxtr and behaviors are versioned and published to PyPI independently. behaviors declares the fxtr
versions it works with as a range in its dependencies, such as `fxtr>=0.2.1,<0.3`:

- The lower bound is the oldest fxtr behaviors works with. CI runs behaviors' tests against that
  fxtr from PyPI, alongside the usual tests against the workspace's fxtr. Raise it when behaviors
  starts relying on something newer in fxtr.
- The upper bound is fxtr's next breaking release. While the packages are pre-1.0, a breaking
  release bumps the minor version (including changes to entity IDs or the runner's database
  schema), so `0.2.x` is followed by `<0.3`. A breaking fxtr release needs a behaviors release that
  allows it before behaviors users can install the two together.

`scripts/release_version.py check` (a pre-commit hook) requires the range to include the
workspace's fxtr and its upper bound not to pass fxtr's next breaking release. For now, releases
are pre-releases only (`aN`, `bN`, `rcN`).

## License

fxtr, behaviors, and the rest of this repository are Copyright 2026 Clarity AI Research Inc., dba
Transluce, and licensed under the Apache License 2.0; see `fxtr/LICENSE` and `fxtr/NOTICE`.
Third-party material keeps its own license, noted alongside it.
