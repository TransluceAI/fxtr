# Project — code overview

`fxtr/project/` is what fxtr knows about the *project*: the user's repository of steps,
workflows, and launcher scripts, which depends on fxtr. It answers the questions a launch asks
before any job exists (which project is this, where is its database, which code is checked out,
does the venv match), defines the interface user code talks to the project through, and holds the
`fxtr` command line. The runner never imports this package's environment or configuration: it
knows about code only through the `CodeVersion` a client hands it to record.

The package imports nothing that needs the `runner` extra, so a project's configuration can be
read, and `fxtr --help` run, without a database driver. The one implementation of the client
interface lives with the runner, in `runner/postgres/client.py`; `running.open_local_client`
imports it only when a command or launcher needs it.

```
project/
├── config.py        Project, the two configuration files ([tool.fxtr.views] included, and
│                    load_views for that table alone), locating a project from an anchor, the
│                    local config writer, load_env_file (the project's .env)
├── environment.py   Environment (locate the project, pin the code, check the venv, find
│                    commits, read a file at a commit), Pin, Problem, LocalEnvironment over
│                    git, jj, and uv
├── code_version.py  CodeVersion: the code a process ran a job at (the pinned commit, the project,
│                    the environment check, where each runnable function is); function_sources
├── client.py        ProjectClient and JobHandle, the interface user code talks to (and, optional,
│                    where the project's viewer runs: viewer_url); their records (JobStatus,
│                    CacheConflict, JobSummary, ArrayInfo, Snapshot, CacheEntry, AdoptReport) and errors
├── job_record.py    JobRecord: one job's facts, and the facts they refer to, as one entity whose
│                    tag carries the format's version; one row type per ledger table
├── running.py       run_job, wait_for_job, and report_job_status: the CLI and Python
│                    launchers' shared execution and output (show_job shows a job in the viewer);
│                    open_local_client and launch_cli;
│                    launch_cli takes explicit options and leaves argument parsing to callers
├── viewer.py        where the project's `fxtr view` runs: its record (.fxtr/viewer.json in the
│                    project root, gitignored, kept after shutdown), recorded_viewer (the last
│                    successful address for this identity), find_viewer (by the viewer's /api/info),
│                    project_identity (root, schema, opaque database-target fingerprint),
│                    job_url, announce_job (tells the viewer's open pages), open_job (a new tab)
│                    (stdlib only)
├── formatting.py    status (with cache conflicts grouped by pattern), cache adoption reports,
│                    array, type, schema, and id text output
├── views.py         the project's view bundle from the Python side: bundle_dir, read_manifest,
│                    build_views (`fxtr views build`, checking Node first), sources_fingerprint,
│                    stale_reason, read_bundle / ViewBundle (immutable built bytes and their
│                    version), and bundle_version; the constants mirrored from fxtr-view and
│                    fxtr-view-bundle;
│                    package_sources_fingerprint and replace_directory, which the viewer's
│                    own rebuild (debug_web's viewer_build.py) shares; vendor_packages
│                    (`fxtr views vendor`)
├── ts_packages.py   the fxtr TypeScript packages a renderer package can use, staged for the
│                    wheel and for vendoring (stdlib only: fxtr/hatch_build.py loads it by path)
├── new.py           a new project (`fxtr new`): write_project, find_checkout, render, init_git
├── template/        the files new.py renders, as *.tmpl mirroring the project's layout
└── cli.py           the fxtr script, a small user of ProjectClient
```

Each module's docstring is the reference for its rules; this file is the map between them.

## The mental model

**A project is a directory, not an id.** The project root is the nearest directory holding a
`pyproject.toml` with a `[tool.fxtr]` table, found by walking up from an *anchor*. A launcher
anchors on the root workflow's source file (the code that is executing), the CLI on the working
directory or `--project`. The root need not be the repository root: the repository is whatever git
or jj checkout encloses it, and the project's path within it is recorded on each job attempt. Two
files beside each other configure it: committed `[tool.fxtr]` (`modules`, `stale_after`, shared by
every checkout) and gitignored `fxtr.local.toml` (`database_url`, `schema`: how this machine
reaches the project's state), with `FXTR_DATABASE_URL` overriding the URL. `Project` is exactly
what those files declare; what the VCS says (the repo root, the project's path in it) comes from
the environment, with the pin. A third, gitignored file, `.env`, holds this machine's secrets
(model providers' API keys): `LocalProjectClient.from_project` reads it into the process
environment with `load_env_file` before importing the project's modules, overriding variables
already set (except with an empty value).

**A launch pins the code it runs.** Every `submit` and `resume` asks the environment to pin the
checkout as a commit and keeps that commit alive under `refs/fxtr/pins/<commit>`. A clean checkout
pins what is checked out; unstaged or untracked changes are always refused; staged changes are
refused unless `allow_staged`, in which case fxtr commits them itself, deterministically, outside
the user's branch or jj change graph. The result is a `Pin` (commit, tree, `ephemeral`). The venv
check (lock current, venv synced, the running interpreter is the venv's) is
a footgun check delegated to uv; `verify_environment=False` skips it and is recorded, pinning is
never skipped.

**One process, one pin, one code version.** A process that takes a job records the code it runs
with the job attempt it creates (`job_attempt_provenance.code_version_cid`): a `CodeVersion`
entity holding the pin, the project's path within the repository, whether the environment was
checked, and, for each name the client's registry can run, where that function is implemented
(`function_sources`: a path in the repository, or a module and its distribution outside it). The
client builds it from the same registry its runners execute, so the locations are those of the
code that ran. Every
attempt that job attempt owns ran at that code, so attempts and jobs carry no version of their
own; a job's launch version is its first job attempt's. A resume may pin another commit.

**A job moves as its record.** `export_job` writes a settled job's `JobRecord` (`job_record.py`)
and every entity it reaches as a CAR file (`core/car.py`). The record is the job's facts from the
ledger: its own rows, the step results its bindings name, and the facts of the job attempts that
produced them (another job's, for a cache hit), so every result in it still says which code
produced it; execution state is left out. `import_job` adds such a job to another project as new
facts: it refuses a file that is not a whole record of this format, results produced at commits
the repository lacks (`Environment.missing_commits`), a job the project already has, and a fact it
shares with the project (a step result another job here took too) that differs. With `update`, a
job the project has is brought up to date from a later record of it instead, provided the record
holds everything the project knows of the job; nothing is removed. The job arrives settled,
`succeeded` or `stopped` and resumable here, and the step cache is left alone. `adopt_cache`
(`fxtr cache adopt`, or `fxtr jobs import --adopt-cache`) then makes the cache offer the job's
results, so later jobs here reuse them, with their provenance. Building, writing, reading, and
inserting records is the local client's (`runner/postgres/job_records.py`).

**User code holds one object.** `ProjectClient` is the top-level counterpart of
`WorkflowContext`: an `FxtrEntityStorage` that also has the project arrays, the jobs, and the step
cache as methods. There is one launch verb, `submit`, returning a `JobHandle` (`status`, `wait`,
`result`, `cancel`); `run` is submit then wait; `resume` and `job` return handles too. Its records
say only what every client can say. The local client (`runner/postgres/client.py`) runs its jobs
in the submitting process; a remote client (later) would use the same `Environment` to locate and
pin, then send the pin elsewhere.

**Functions are found by name.** `@step` and `@workflow` register into `GLOBAL_REGISTRY`
(`experiment/registry.py`) at import time. `[tool.fxtr] modules` tells a process with no script of
its own (a resume, the CLI, the viewer) what to import; `from_project` imports them, so a launcher
and the CLI see the same functions. A job whose function is not registered where it resumes fails
that attempt with an error naming the setting.

**The project's renderers are a view bundle.** A project with renderers of its own names its
renderer package in `[tool.fxtr.views] package`; the package is an ordinary npm package built with
`fxtr-view-bundle`'s preset (see `ts/CODE_OVERVIEW.md`), and its bundle is always its
`dist/`: a manifest (`fxtr-view-bundle-manifest.json`) naming an ES module and an optional
stylesheet, with the contract version they were built at. `fxtr views build` (`views.py`'s
`build_views`) runs the package's own `build` script with `npm run build` (the same under any
package manager; nothing is installed) into a scratch directory under the package's
`node_modules/`, checks the manifest there, and only then moves the result over `dist/`, so a
failed build keeps the previous bundle. It returns a `ViewBundle` captured before the move:
immutable manifest/module/stylesheet bytes, their version, and the source fingerprint. It also
records a hash of the bundle's sources in
`dist/fxtr-view-bundle-sources.json`: the package's files and those of the packages it links
locally, found through its `package.json` dependencies and `node_modules/`, less tests (see
`views.py`'s docstring). `stale_reason` compares that hash with the current one; `fxtr view`
rebuilds a stale bundle at startup and again as its sources change, and open pages load each build
without a reload (`runner/postgres/debug_web/CODE_OVERVIEW.md`). `bundle_version` is a hash of the
built files, so two builds of the same bundle are the same version. A build checks the `node` on
`PATH` first (`MIN_NODE_MAJOR`, 22) and refuses an older or missing one with how to fix it; a
failed build raises `ViewsBuildError` with the end of its output, less stack frames.
`VIEW_API_VERSION`, the
manifest's file name, and `dist` are mirrored from `fxtr-view`, and the variable that redirects the
build's output from `fxtr-view-bundle`; a test checks each against its source.

**Outside the checkout, a renderer package vendors the fxtr packages.** Inside the checkout a
renderer package links `ts/packages/` by relative path, whose own dependencies the checkout's
root install provides. Outside it there is nothing to link and the packages are not on npm, so
the package keeps a copy of
them, `views/vendor/fxtr/`, links them from there (`link:./vendor/fxtr/packages/fxtr-view`), and
commits it. `fxtr views vendor` (`views.py`'s `vendor_packages`) writes the copy from the running
fxtr: the staged tree an installed fxtr carries as `fxtr/_ts`, else the checkout's `ts/`, staged
the same way by `ts_packages.py` (`PROJECT_PACKAGES`, and each `package.json` without its
workspace and development dependencies). Because the copy sits inside the package, its imports
resolve to the package's own `node_modules`, and re-vendoring marks the bundle stale. The
details are in `views.py`'s and `ts_packages.py`'s docstrings.

**A new project is written from a template.** `fxtr new DIR` (`new.py`) writes an empty project:
`pyproject.toml` with an empty `[tool.fxtr] modules` list, a Python package under `src/`, a
`.gitignore`, a `.env` with empty API-key placeholders, a README, and an empty renderer bundle.
`--example`
(`write_project(example=True)`) adds `experiment.py`
with the greeting entity, step, and workflow, a `launch.py` running it as a Postgres job,
module registration, the greeting's renderers, and a results overview
of the workflow (`views/src/overview.tsx`, an example of a view built to the documentation's
Designing views guide). The documentation's project configuration page
(`docs/fxtr/reference/project-configuration.mdx`) covers both modes.
Where fxtr comes from is `find_checkout`'s answer: the checkout the running fxtr is imported
from, or none. With none the project is *published*:
it depends on `fxtr[runner,debug-web]` at the running version or later from the package index,
and its README covers Postgres with `docker run`. With a checkout it depends on it by path:
by relative paths for a project inside the checkout, by absolute paths outside it. Projects
outside the checkout get README instructions for the fxtr plugin in Claude Code and in Codex,
which distributes the agent skills and documentation. Every renderer package has its own pnpm workspace boundary,
lockfile, and installation in `views/`, so creation never edits an enclosing workspace; inside
the checkout it links the checkout's TypeScript source with `link:`, and outside it, it is
vendored (above). Then it runs `git init` unless the project is in a repository, and prints
setup commands starting with `uv sync` to install the project's dependencies.
The template is `template/`, one `*.tmpl` per file (the renderer
package's `package.json` is built in code, since its dependencies depend on where the project is,
with the config-loader convention described in `ts/CODE_OVERVIEW.md`);
`render` fills `{{name}}` and keeps `{{#flag}}...{{/flag}}` sections (a section inside another
too), and an unknown name is an
error.

## The life of a launch

The execution API in `running.py` takes Python arguments. Callers own any command-line parsing.
The basic example and template launchers call `launch_cli` with its defaults and contain no
argument parsing. This synchronous entry point manages the event loop, opens a local Postgres client,
and calls `run_job`.
`launch_cli` manages its client with `open_local_client`, passing the
environment-verification setting and optional `max_concurrency` step limit (the runner's default
when omitted). `cli.py` defines the `fxtr run` / `resume` flags, opens through
the same function, resolves its inputs, and calls the async execution API. Async callers keep
their client open around `run_job` and can reuse it for multiple jobs. `run_job` accepts
a workflow object or registered name and submits through the public `ProjectClient` interface;
`wait_for_job` shows the job (`show_job`) immediately, waits on its handle, prints status and
errors (`report_job_status`, which `fxtr jobs status` uses too), and obtains the result.
Showing a job asks the client where the project's viewer runs
(`ProjectClient.viewer_url`, which the local client answers with `viewer.py`'s `find_viewer`) and
prints the job's link there, or, with no viewer running, prints how to start one. It then
announces the job to the viewer (`announce_job`, `POST /api/launches`), whose open pages can
switch to it, and opens the link in a new browser tab (`open_job`) only when no page heard it: the
viewer answered with no open pages, or with no count at all (an older viewer). `open_browser=False`
(`--no-open` on `fxtr run` and `fxtr resume`) or `FXTR_NO_BROWSER` does neither and only prints the
link; the test suites set the variable. A job's
`JobStatus.cache_conflicts` are printed grouped by shape, each group's pattern with how many
entries it matches, for which `report_job_status` lists the cache under each group's literal
prefix, and then the commands that list and clear them (`fxtr cache list|clear --conflicts-in`).
`fxtr resume` also uses `wait_for_job`. A stopped job is reported, then raises
`JobNotFinishedError`; command-line entry points translate that to exit status 1.
The helpers print the result with
`formatting.format_array`, or call an async `on_result(storage, result)` callback instead while
storage is still open, so it can load result entities. `run_job` also accepts bound
array ids such as project-array snapshots. Only a Postgres run imports the optional runner extra.
Scripts needing custom input loading or other client settings use the client directly. The example
project's slow and huge-grid launchers parse their own flags and pass the prepared inputs,
`max_concurrency`, and environment options to `launch_cli`.

A reading order through the code, for a launcher calling `client.run(workflow, inputs)`:

1. `LocalProjectClient.from_project(__file__)` (`runner/postgres/client.py`) asks the environment
   to `locate_project` (`environment.py`, over `config.locate_project`: `find_project_root`, then
   `load_project` reading both files strictly), imports the project's `modules`, and builds a
   `Database` in the project's schema. Entering the client creates the tables if missing and opens
   the task group its jobs run in.
2. `submit` looks the workflow up in the registry, so a resume elsewhere finds the same function.
3. It asks the environment to `check_synced` (unless verification is off), refusing with
   `EnvironmentNotSyncedError` and the problems' fixes.
4. It asks the environment to `pin_code` (`LocalEnvironment._pin_git` or `_pin_jj`), which
   refuses with `PinRefusedError` or returns a `Pin`; the client stores a `CodeVersion` built
   from the pin, the check's outcome, and its registry (`function_sources`).
5. In one transaction: the job and its root invocation are created (`Jobs.create`) and the first
   job attempt is taken with that code version (`execution.resume`).
6. A `JobRunner` starts in the client's task group; the handle's `wait` returns the `JobStatus`
   once the job stops. From here on it is the runner's business
   (`runner/postgres/CODE_OVERVIEW.md`).

`resume(job_id)` is steps 3 to 6 against an existing job, taking over a job attempt whose lease
has been quiet for the project's `stale_after`. With `overwrite_cache_conflicts`, on `submit`,
`resume`, `run`, `run_job`, `launch_cli`, and `fxtr run|resume --overwrite-cache-conflicts`, the
job attempt the call creates overwrites the cache conflicts it finds with completed results
(`runner/postgres/CODE_OVERVIEW.md`, "Step resolution").

## The command line

`cli.py` is `fxtr`: `new <dir>`, `run <workflow>`, `resume <job>`,
`jobs list|status|cancel|export|import`, `cache list|clear|adopt`, `init`, `views build`, `view`. Every job, cache,
and `init` command opens the local client of the project the working directory (or `--project`)
is in and uses only the `ProjectClient` interface after that, so what it prints is what any
client could report; `view` is the one local-only command, serving `runner/postgres/debug_web/`,
and `new`, `views vendor`, and `views build` open no client at all (`new` writes a project that has
no database yet; `views vendor` only copies renderer packages; `views build` needs only
`[tool.fxtr.views]`). `new` ends by printing the commands to sync the venv, write the local config, build the
renderer bundle, and commit (including the renderer lockfile). Empty projects link to the
first-experiment walkthrough; `--example` also prints the commands to run the demo.
`run` takes a registered workflow name and inputs that are JSON
scalars or `@array` (a snapshot of a project array); anything richer is a launcher script's job.
Refusals the user can act on (the project and config errors, client errors, registry and
signature errors, a pin refused, a venv out of sync) print one line and exit 2, a job that stopped
exits 1. Progress is the runner's logging: one line per attempt start and end, from the `fxtr`
loggers, which the CLI shows at INFO. `main(argv, open_client=)` is the whole command line; tests
pass their own client opener.

## Where to change things

- **A new setting** in either file: declare it in `FXTR_SETTINGS`, `VIEWS_SETTINGS`, or
  `LOCAL_SETTINGS` with its check, add it to `Project`, and document it in `config.py`'s
  docstring, which is the reference.
- **A change to the bundle contract:** the TypeScript side (`fxtr-view`'s `view_bundle.ts`, the
  preset) and `views.py`'s mirrored constants together; the mirror test fails until they agree.
- **Another VCS, or another pinning rule:** `LocalEnvironment.pin_code` and its per-tool helpers;
  the rules are stated once in `environment.py`'s docstring for both tools.
- **Another kind of client:** implement `ProjectClient` and `JobHandle`; anything it knows beyond
  the records goes on the client, not the interface. The CLI's `OpenClient` is where it would be
  chosen.
- **Shared launch behavior:** `running.py` owns `run_job`, `wait_for_job`, the
  local-client opener, result callbacks, and `launch_cli` for synchronous scripts with explicit
  options. `formatting.py` owns status and array output. The CLI, example launchers, and generated
  `template/launch.py.tmpl` use this API; each caller owns its argument parsing and input preparation.
- **What a new project starts with:** the files under `template/` and the list of them in
  `new.py` (`_TEMPLATE_FILES`); a new value or flag goes in `write_project`'s `values` / `flags`.
  `tests/test_project_new.py` renders every template both ways and runs the generated launcher.
- **A new CLI command:** a coroutine `(open_client, args) -> exit status` in `cli.py`, registered
  in `_parser`, using only the interface.

## Tests

- `tests/test_project_running.py`: Postgres execution, result callbacks with live storage,
  explicit options and defaults, failure propagation, and optional imports. The grouped
  cache-conflict output is tested with the job that has the conflicts, in
  `tests/test_runner_postgres_jobs.py`.
- `tests/test_project_config.py`: locating a project, reading both files strictly, the override,
  the schema default, the writer, loading `.env`.
- `tests/test_project_views.py`: reading a bundle's manifest and its refusals, `build_views`
  against a temporary package whose `build` script writes a bundle (skipped without npm),
  including a failed build keeping the previous bundle; the sources fingerprint (linked packages
  in, installed packages and built output out) and staleness; `fxtr views build`; vendoring
  (`vendor_packages`, `fxtr views vendor`: the copy, its replacement, the packaged copy preferred,
  the refusals, the registry packages to add); and the mirrored constants against `fxtr-view`'s
  and `fxtr-view-bundle`'s source.
- `tests/test_project_ts_packages.py`: the staged TypeScript tree, its manifests, and that the
  staged packages import only one another and the registry packages they declare.
- `tests/test_project_new.py`: empty and example projects outside and inside a (fake) checkout,
  and published; the vendored renderer packages; nested template
  sections; Pyright over generated Python using the shared repository policy; the generated
  example launcher run on Postgres; the refusals; independent renderer environments and unchanged
  enclosing workspaces; `fxtr new`'s output; opt-in (`FXTR_TEST_EXAMPLE_PROJECT=1` and a test
  database), a project written, synced (pinned to the repository's `uv.lock`), committed, and run
  as a job through `uv run python launch.py`.
- `tests/test_project_viewer.py`: the viewer's record, finding the project's viewer by
  `/api/info` (another project's viewer and a dead viewer's record ignored), announcing a launch,
  and a launch showing its job in the viewer's open pages or a new tab when none heard it, or
  printing how to start a viewer, or only printing the link when told.
- `tests/test_project_environment.py`: `LocalEnvironment` against real temporary git and jj
  repositories and a real uv project (jj cases skipped without jj).
- `tests/test_project_cli.py`: every command through a local client with a fake environment over
  the test database: output, exit status, inputs, the refusals and their command-line notes,
  and matching launch output through the CLI and Python API.
- `tests/test_runner_postgres_client.py`: the local client against the interface (submit, resume,
  handles, cancel and Ctrl-C, the cache, `from_project`), with `FakeEnvironment`
  (`tests/postgres_testing.py`).
- `tests/test_example_project_launch.py` (opt-in, `FXTR_TEST_EXAMPLE_PROJECT=1`): the example
  project copied into a fresh git repository and launched through `uv run`, so the real
  environment pins and checks without touching this repository.
