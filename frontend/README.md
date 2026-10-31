# fxtr frontend

A Vite/React viewer for one completed job from the Postgres runner: a tree of workflow
invocations, each a list of step cards and the arrays they produced. It talks only to the
`debug_web` backend, which also serves the built viewer itself.

How the app is built is in `CODE_OVERVIEW.md`.

## Running it

1. Install the viewer's dependencies once, from the repo root (this app is a member of the root
   pnpm workspace):

   ```bash
   pnpm install
   ```

   From a checkout, `fxtr view` then runs the viewer's Vite dev server itself (see
   [Developing the viewer](#developing-the-viewer)). If the dev server cannot start, it falls
   back to `frontend/dist`, rebuilding it when stale. Nothing about a project is compiled into
   that build, so one build serves every project.

2. Start the server for the project whose jobs you want to see (its `fxtr.local.toml` names the
   database; `FXTR_DATABASE_URL` overrides it; see `postgres_env/README.md` for a local server).
   From the project's directory, in its venv:

   ```bash
   uv run fxtr view
   ```

   It serves the viewer and its API on one origin, prints the viewer's URL
   (starting at `http://127.0.0.1:8000/`), and opens it in a browser. It prefers the project's last
   successful port, then tries 8000–8050. `--port` requires that exact port; a conflict is an error.
   `--no-open` only prints the URL; `--host` changes the bind address.
   `#job=<job id>` opens one job. While the project's viewer runs, a launch tells it about its
   job, and its open pages switch to the job, except a page where someone picked a job, which
   offers the new one in a toast; with no page open, the launch opens that link in a new tab.
   Running `fxtr view` again for the same project opens the running one in another tab instead of
   starting another.

   The API is under `/api`: GraphQL at `/api/graphql/`, entity bytes at `/api/entity/{cid}` (many
   at once at `POST /api/entities`), the project's view bundle at `/api/views/…`, code links at `/api/source/{name}`, the project's
   EXPERIMENT.md at `/api/experiment_readme`, and launches announced at `POST /api/launches`.
   `fxtr view` builds a
   project's own renderers when it starts, and rebuilds them whenever their sources change while
   it runs; open pages hear of each build, and of each launch, on the `/api/events` WebSocket and
   switch to the new renderers without a reload. Build failures, including at
   startup, keep the last valid bundle or use the default renderers when none exists, and show
   an error in the rail. Watching continues, so fixing the source recovers without a restart.

### Developing the viewer

In a checkout (an editable install of fxtr, with no packaged viewer), `fxtr view` runs the viewer's
Vite dev server, with hot reloading, for as long as it runs. It starts
`frontend/node_modules/.bin/vite` on the same host, at the viewer's port plus 10000 (for example,
`18000` for `8000`). Automatic selection skips a pair if either port is occupied. Vite's `/api`
proxy points back at the backend (`FXTR_DEV_BACKEND`, read by `vite.config.ts`), which redirects
every viewer path, such as `http://127.0.0.1:8000/#job=…`,
to the same path on the dev server. A tab left open while `fxtr view` restarts reloads itself once
the dev server is back at the same address. If a conflict forces a new address, use the newly
printed URL. When the dev server is not installed or does not start, `fxtr view` logs why and
serves `frontend/dist` instead.

To run the dev server by hand against a running `fxtr view`, use `cd frontend && pnpm dev`: the
app is then at `http://localhost:5173`, proxying `/api` to `http://127.0.0.1:8000` unless
`FXTR_DEV_BACKEND` says otherwise. `pnpm preview` serves a built `dist/` the same way.

On the dev server only, the rail has a **replay** section above the theme row: replay the job on
screen as if it were running, with play/pause, a progress slider, a speed, and a **fail** toggle
that fails the busiest step from the current moment, to see how a run in progress looks without
running anything. Use it on finished jobs.

### Packaging it with fxtr

The built viewer ships inside fxtr's sdist and wheel, so `pip install 'fxtr[debug-web]'` gives a
working `fxtr view` without Node or this repository. Build both from the repo root with

```bash
pnpm build:dist
```

which builds the viewer and then runs `uv build --package fxtr` (output in `dist/`). The build
hook `fxtr/hatch_build.py` copies `frontend/dist` into the package as
`fxtr/runner/postgres/debug_web/_frontend`, in the sdist too, so a wheel built from the sdist
carries it without `frontend/`. A non-editable build without a built viewer fails; editable
installs (every project that depends on a checkout) skip the hook and serve `frontend/dist`.

The app's URLs are origin-relative by default. `VITE_GRAPHQL_HTTP`, `VITE_ENTITIES_URL`,
`VITE_VIEWS_BASE`, `VITE_SOURCE_BASE`, `VITE_EXPERIMENT_README_URL`, `VITE_FILE_URL`, and
`VITE_EVENTS_URL` override them at build time (see
`src/config.ts`); the backend sends no CORS headers, so another origin has to be behind a proxy.

## Code links

Each step or workflow card, and the header of the workflow on screen, can show the function's
docstring and an **implemented** chip naming the file, lines, and the commit the job ran (none for
a function read from the working tree). Clicking the chip opens the function's file in a panel of the viewer, with the function's lines marked. The
panel shows the file as the commit the job ran has it, so it is the code that ran even after the
working tree has changed. The app asks the backend (`GET /api/source/{name}`) with the code version
the invocation's job ran, which records where each function the job could run is implemented, then
fetches the file at that commit (`GET /api/file`). A function outside the repository, or a level
with no attempt yet, falls back to the functions the project's `[tool.fxtr] modules` register in
the backend's process, as the working tree has them. A card that says **source not indexed** names
a function neither knows.

The **Open** button at the panel's upper right opens the function in a local editor. Its menu lists
the editors (Cursor, VS Code, VS Code Insiders, Windsurf, and Positron) and copies the file's path;
choosing an editor opens the function there and makes it the one **Open** uses, kept in the
browser. The link has the form
`<scheme>://file/<absolute path>:<line>` and opens the working tree's file, whose lines may have
moved since the job ran. `VITE_REPO_ROOT` overrides the checkout path in the link when the editor
is not on the machine running the backend.

## Experiment link

The rail, under the job picker, shows the `EXPERIMENT.md` at the project root, which the backend
serves (`GET /api/experiment_readme`). The link does not appear when the project has none.

The link behaves like an entity or invocation chip: clicking opens the experiment in the detail
view's columns, dragging carries it into them, and it is highlighted while a panel shows it.
