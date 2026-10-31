# Debug viewer backend — code overview

A small, **read-only** GraphQL server over the Postgres runner's state, and the server of the
experiment viewer in `frontend/` at the repo root (see `frontend/CODE_OVERVIEW.md`). Its API,
under `/api`, exposes job and invocation records, step state, and stored entities; every other
path is the built viewer (`ViewerFiles`, each file with `Cache-Control: no-cache`; until it is
built, a 404 saying how to build it). That is `_frontend/` beside `app.py` in an installed sdist
or wheel (`fxtr/hatch_build.py` copies `frontend/dist` there at build time), and otherwise the
checkout's `frontend/dist` (`default_frontend_dist`). From a checkout (`checkout_frontend`: no
`_frontend`, and `frontend/package.json` beside the package), `serve` instead has the app run the
viewer's Vite dev server for its lifetime (`dev_server.py`: `frontend/node_modules/.bin/vite` on
the same host at the server's port plus `DEV_SERVER_PORT_OFFSET`, 10000, and nowhere else,
`FXTR_DEV_BACKEND` pointing its `/api` proxy back here), and `Viewer` redirects every non-API path
to the same path on it. Preferring the project's last successful port on restart lets Vite's client
find its server at the same address and reload the page, provided that port pair is still free.
When Vite is missing or does not start
(its port taken since `serve` checked it included), it logs why and prints that it serves the
built viewer instead, at the same URL.
When a checkout falls back to serving its `frontend/dist`, `serve` has the app
first check it against the frontend's current sources (`viewer_build.py`, the same source hash as
the project's view bundle): a stale one is rebuilt, and when the rebuild cannot run or fails,
every non-API path answers 503 saying why (`Viewer.refusal`) rather than serve a viewer that may
not match this server's API. One origin means the viewer's URLs are origin-relative
and there is no CORS. It is isolated from runner execution: nothing
else in the Python package imports it except `fxtr view` (`project/cli.py`), which imports it only
when run, but the experiment viewer requires it.

Needs the `fxtr[debug-web]` extra (Ariadne, Starlette, Uvicorn, and watchfiles on top of
`fxtr[runner]`). Run it with `fxtr view`, through `server.serve`
(`--host` / `--port`; `--no-open`, which only prints the viewer's URL instead of also opening it in a browser once
the socket is bound; `--project`, a path inside the project to
serve, the working directory by default; the project's local configuration names the database,
`FXTR_DATABASE_URL` overriding it — the ordinary role, read-only only by never writing).
`ports.bind_viewer` handles port selection and reservation: without `--port`, it prefers the
project's remembered port on the same host, then tries 8000 through 8050 (50 additional ports).
In a checkout, both the backend and its paired dev-server port must be available. Explicit ports
are strict; only address-in-use errors advance automatic selection. Exhaustion or another bind
error raises `ViewerPortError` (`fxtr view` exits 2). The backend socket stays reserved through
startup and is passed to Uvicorn; Vite binds its own checked port with `--strictPort`.

`serve` is safe to run twice. It first asks whether the project's viewer already runs
(`fxtr.project.viewer.find_viewer`, by the identity returned by `GET /api/info`); if so it opens
that one and returns. Once listening, it records its URL for the project
(`record_viewer`, in `.fxtr/viewer.json` in the project root, which projects gitignore). The record
persists after shutdown as a port preference; discovery verifies the live server's identity
before reusing it. This is how a launch of the project finds the viewer to show its job in
(`project/CODE_OVERVIEW.md`). When the server shuts down, uvicorn closes the pages' open event
sockets, so its graceful shutdown does not wait on them.

```
debug_web/
├── schema.graphql   the SDL (Ariadne schema-first)
├── resolvers.py     Query resolvers and the scalars, over a LocalProjectClient
├── versions.py      shared ledger-based invocation and per-run revisions
├── contracts.py     typed summary/page records and output-query inputs, matching the SDL
├── projections.py   shared typed node/error views for detailed resolvers and overview readers
├── browse.py        payload-free job/level summaries and bounded invocation/dimension-key pages
├── output.py        current output pages, bounded immutable-array cache, and global table queries
├── table_values.py  table display/search text and ordering, verified against shared browser fixtures
├── app.py           the Starlette app: under /api the GraphQL mount (HTTP only) and the /entity,
│                    /entities, /views (and /views/status), /events (a WebSocket), /launches,
│                    /source, /file, /experiment_readme, and /info routes; the viewer (Viewer:
│                    the dev server's redirect or ViewerFiles) elsewhere; client, view bundle
│                    watcher, and dev server lifetime
├── events.py        the viewer's events: EventHub (fan-out to the open sockets, counted),
│                    serve_events (one page's socket), same_origin, and reading and announcing a
│                    launch (launch_request, launch_event)
├── views_watch.py   ViewsWatcher: the current immutable bundle, its state, and automatic
│                    rebuilding as its sources change
├── ports.py         bind_viewer: automatic/explicit port selection and backend socket reservation;
│                    ViewerPortError and viewer_url
├── dev_server.py    run_dev_server: the checkout's Vite dev server at the viewer's port plus
│                    DEV_SERVER_PORT_OFFSET, or None when it can't start
├── viewer_build.py  the checkout's built viewer: its source hash, viewer_stale_reason,
│                    build_viewer, and refresh_viewer (rebuild or refuse a stale one)
└── server.py        serve (what `fxtr view` runs): the project's running viewer
                     reused, its listening socket reserved, uvicorn, then announce the
                     viewer's URL, record it, and open a browser
```

## How it reads the state

The demand-loading queries `jobSummaries`, `jobSummary`, `levelOverview`, `runPage`, and `keyPage`
use `browse.py`. They query relational status projections without entity bytes or logs. A level
contains its graph, aggregate counts, each run's activity (when this job's attempts at it first
started and last finished, how many it finished, when its longest-running invocation started:
over the attempt `spawn_query` names for each spawn) and its failures grouped by error type, all
read with its counts in one grouped query over the spawns (`run_statuses`), the database's time of the read (`asOf`), compact timing/error fields, returned/awaited node
identities, and a ledger-based version; it contains no spawn list or output rows. Key and invocation
pages use typed JSON keys, bounded page sizes (at most 200, or `KEY_LIST_LIMIT` = 1000 for
`keyPage` and `outputDimValues`, whose first page is the viewer's whole key list), and query-scoped
cursors. Dimension pages contain
all values matching the search, with compatibility and grouped status counts for the other
selected dimensions. Their cursors are independent of that selection, so choosing another
dimension preserves the current page. Key and invocation queries also accept an offset for
direct page jumps, snapped to page boundaries and clamped to the current total. Incompatible values carry their
unfiltered status counts. Invocation pages filter on one display state (`state`) or on every
invocation that has not completed (`unfinished`).
Status policy is owned by `../status_queries.py`: execution, detailed readers, and browsing
queries all consume its canonical relations, including job-owned failures.
A job page selects its IDs first and derives their summaries in one grouped query. Latest and
active job attempts are SQL projections.
Shared node and error projections live below both readers and resolvers in `projections.py`;
the overview projection exposes mapped/replica dimension names without replica value lists.

The server holds one `LocalProjectClient` and never writes to the runner database. The complete-data
queries used by custom renderers read through the client's detailed readers, which assemble
consistent snapshots (each in one `snapshot=True` transaction):

- The **job overview** is `Jobs.status` (the derived job / per-invocation / per-step states, plus
  the job attempts) projected into a shallow invocation list and step list. It carries **no**
  nodes/attempts — just enough for the job panel's lists.
- One **invocation** is `Jobs.describe_invocation(job_id, pathname)`: that invocation's nodes,
  spawns, attempts (each with its events and `ctx.log` lines), its derived status, and — for its run
  nodes' spawns — the pathname, result, and derived state of each child invocation (from the
  child's record and the invocation relation of `StatusQueries`) and the bound result and derived state of each
  step (from `Jobs`' step description, the same one `describe_step` gives), so a run node can link,
  preview, and show the progress of each mapped call.
- One **step** is `Jobs.describe_step(job_id, cache_key)`: the request, its binding,
  the attempt and its logs, and the derived `StepState`.
- The **jobs list** is `Jobs.list` with a `Jobs.status` call per row for its state (expensive for large jobs); ordinary browsing uses `jobSummaries` instead.
- The **step cache** is `execution.list_cache_entries`, joined per entry to its request spec (`ledger.get_step_request`),
  current attempt (`execution.get_attempt`), and that attempt's logs (`logs.list_logs`) in one read
  transaction. These complete-data readers are separate from the paged browsing projections.

`describe_invocation` / `describe_step` and the paged queries share the runner's
`StatusQueries` relations. The detailed readers hydrate individual target judgements with
results, errors, and logs; browse queries aggregate and page the compact projection.

Derived intermediate node values are **not** rendered: only leaves (inputs, source arrays,
step/child results) and invocation results have stored values, and those are what the UI
shows (a node's `valueCid`, an invocation's `resultCid`, a step pin's `resultCid`). Derived nodes
show metadata only (kind, type, params, edges).

## The GraphQL shape

`schema.graphql` is the source of truth. The paged/metadata queries above and `outputPage` /
`entityMetas` / `outputDimValues` return explicit GraphQL object types consumed by `api/graphql.ts`, `api/browse.ts`, and
`components/PagedTable.tsx`. Output sources must name exactly one array CID or a complete
run identity; typed sort and filter inputs are validated before reading data. JSON is confined
to dynamic keys, schemas, node parameters, and values. The complete-data `Query` fields include `jobs`, `job(id)`,
`invocation(jobId, pathname)`, `invocationVersion(jobId, pathname)`, `step(jobId, cacheKey)`,
`stepCache`, `array(cid)`, and `entity(cid)`. Each `Spawn` carries `state`: its child invocation's
state or its step's `StepState`.

The schema supports a **normalized client cache**: every entity type with a cache key — `Job`,
`Invocation`, `Node`, `Attempt`, `JobAttempt`, `StepPin`, `CacheEntry` — carries a
globally-unique `id` (job-scoped ones are composites, e.g. an `Invocation`'s is `jobId/pathname`, a
`Node`'s is `jobId/pathname/identity` since a node identity repeats across invocations). `Job`'s
lists are **summary** types (`InvocationSummary`, `StepSummary`: name + state only) — the full
`Invocation` / `StepPin` are fetched through their own root fields. A consequence of the
dict-builder style (below): the `job` document must select only the summary fields under
`invocations` / `steps`; a deep field there would fail (missing on the summary type).

What the execution state shows through: an `Attempt` carries `startedAt` (an attempt is running
from the moment its row exists) and the `ownerJobAttemptId` of the job attempt whose process runs
it. **Provenance** (which code ran) is read from the ledger (`jobs.provenance`): a `Provenance` is
a job attempt's fact and its code version, flattened (`jobId`, `jobAttemptId`, `commit`, `tree`,
`ephemeral`, `projectPath`, `environmentVerified`). Every `Attempt` has its owner's;
`Invocation.provenance` is its result's producer's and `StepPin.provenance` its bound step
result's (which for a cache hit names the job that computed it), both null until there is a
result. A `CacheEntry` carries the provenance of what it holds (its current attempt, or the step
result it offers); its `attempt` is null when it offers a step result whose producing attempt this
database does not have, as is a `StepPin`'s. A `JobAttempt` carries both of its axes — the state (`active`, `succeeded`, `stopped`,
`cancelled`, with `reason`, `settledAt`) and the lease (`owner`, `heartbeatAt`,
`evictedAt`, `evictedReason`, and the derived `leaseFresh`, computed from
`JobAttemptRecord.fresh()` at the project's `stale_after`). `leaseFresh` is **display information only**:
staleness informs and nothing in the policy acts on it, though a resume of the job may take a stale
job attempt over. The state enums follow the derivations: `JobState` is `running` /
`succeeded` / `stopped`, `StepState` is `queued` / `running` / `completed` / `failed` / `conflict` /
`stopped`, and an invocation's state (a `String`, not an enum) is `queued` / `running` /
`completed` / `failed` / `interrupted` / `stopped`.

Resolvers in `resolvers.py` build **plain dicts keyed by the schema's field names**, so Ariadne's
default resolver reads them with no per-type binding — only the `Query` root and the scalars are
bound. The record→dict builders (`_node`, `_attempt`, `_invocation`, `_step_pin`,
the `_invocation_summary` / `_step_summary` list rows) are the whole mapping; `_job_overview`
assembles a job's overview, and `_invocation_view` / `_step_view` assemble one invocation / step
(returning `None` for a missing target, so those fields are nullable rather than erroring). Scalars:
`Cid` and `Base64` carry plain strings and need no binding; `DateTime` serializes to ISO 8601;
`JSON` passes structures (array types, node params, array keys) through.

Cross-links the UI follows: a `Spawn.stepCacheKey` is the key of a `StepPin` and a `CacheEntry`; a
`Spawn.childPathname` names a child `Invocation`; any `*Cid` opens an `ArrayView` (arrays) or the
entity viewer.

## The project's view bundle: `GET /api/views/{name}`, `/api/views/status`, `/api/events`

`views_response(views, name, version)` (`app.py`) serves a manifest and the assets it names
from the current immutable `ViewBundle` snapshot owned by `ViewsWatcher` (`views_watch.py`). The
manifest's `X-Fxtr-Views-Version` identifies the exact bytes served. `?v=<version>` requires that
version to match the current snapshot; a mismatch returns 410 so the browser can restart from
the latest manifest. Publishing a successful build replaces the snapshot; no history is kept.
Unknown files and unconfigured views return 404; an unavailable bundle returns 409 with its error.
Responses use `Cache-Control: no-cache` so even reused module URLs can be revalidated by ordinary
HTTP clients.

The app starts `ViewsWatcher.run` through AnyIO's task
readiness protocol. The native watch is established before the initial build. Refreshes build
until the captured inputs match the current sources; restarting watch roots also reconciles, so
edits during startup, builds, or a dependency change cannot silently leave stale output ready.
Build failures keep the last valid bundle, including a compatible bundle found on disk at
startup. Without one, the viewer uses its default renderers. The error is published and watching
continues, so fixing the source recovers without restarting the viewer. The snapshot and its
state are published together on the event loop; file reads and hashing use worker threads.

`ViewsState` describes the published version, build progress, and errors. `/api/views/status`
returns it, and the `/api/events` socket sends each change as a `views` event, the current state
first (next section). Both read the same state that selects the current bundle for HTTP requests.
The frontend's load and recovery behavior lives in `frontend/CODE_OVERVIEW.md`, “The view system”.

## The viewer's events: the `/api/events` WebSocket and `POST /api/launches`

`/api/events` is a WebSocket every open page of the viewer holds, on which the server sends JSON
events (`events.py` has their shapes): `views`, each state of the view bundle, the current one
first on every socket (none for a project without a bundle); and `job`, a job its launcher just
announced, with its workflow's name and whether it was `resumed` (it has more than one job attempt).
It is a WebSocket rather than an HTTP stream because a browser opens at most six HTTP/1.1
connections to one origin, across all its tabs: a stream held open by every page would leave none
for the pages' requests once six were open. The socket only sends. `EventHub` fans each event out
to the open sockets' queues and counts them; one that falls `QUEUE_SIZE` events behind is closed,
and its page reconnects and catches up. The lifespan's `follow_views` task feeds it the
watcher's states.

A launcher announces a job with `POST /api/launches`, JSON `{"jobId": ...}`
(`fxtr.project.viewer.announce_job`). `launch_event` reads the job's metadata through
`ledger.get_job` and counts its job attempts in one snapshot. The route publishes a
`job` event, and answers `{"listeners": n}`, the number of open pages it reached, which is how the
launcher decides whether to open a tab of its own. It writes nothing; the pages decide whether to
switch to the job (`frontend/CODE_OVERVIEW.md`). Browsers let any page open a WebSocket to any
address, so the socket refuses a page whose `Origin` names another host than the request's
(`same_origin`; the dev server's proxy keeps the page's `Host`), and so does the launch route, which
also takes only JSON: another site's page cannot send that without a CORS preflight, which this
server never grants.

## Code, the experiment, and what is served: `GET /api/source/{name}`, `GET /api/file`, `GET /api/experiment_readme`, `GET /api/info`

`GET /api/source/{name}` answers where a step or workflow is implemented, for the frontend's docs
and editor links. Given `?codeVersion=<cid>` (the `codeVersion` of `levelOverview`: the code
version the invocation's latest attempt ran), it answers from that stored `CodeVersion` when it
records the function inside the repository (`recorded_source_response`): the recorded file and
line range, with `repoRoot` the repository enclosing the viewed project on this machine, the
docstring parsed out of the file at the pinned commit (`Environment.committed_file`, then
`docstring_at`), and `commit`. So a function written after the viewer started still has its docs
and link, and the lines are the commit's even if the working tree has moved them. Otherwise (no
code version, or one recording the function outside the repository or not at all) it falls back to
`source_response(registry, name)`: the client's registry holds what the project's `[tool.fxtr]
modules` register when the viewer started, and `inspect` on the function's body gives the file
(relative to the enclosing jj or git repository), the line range with its decorators, and the
docstring as the working tree has them, with a null `commit`. A name nothing (or more than one
function) is registered under is a 404 there. Names are URL-encoded by
the frontend; the route accepts the full decoded name, including slashes.

`GET /api/file?path=<repo-relative path>[&commit=<hex>]` serves a file of the repository enclosing
the project, for the frontend's `code` panel, as JSON `{path, commit, text}` (`file_response`): at the
commit through `Environment.committed_file` when given (the commit a source answer names), else the
working tree's. `file_request_path` keeps the path inside the repository (no absolute paths, no
`..`, nothing a symlink leads out of), a commit must be a hex hash, and a file past
`MAX_FILE_BYTES` is a 413. `experiment_readme_response(project)`
serves the project root's `EXPERIMENT.md` as JSON `{path, markdown}`, 404 without one. These take what
they read rather than the client, so they are tested without a database. `GET /api/info` answers
the project root, schema, opaque database-target fingerprint
(`fxtr.project.viewer.project_identity`), and fxtr's version: how
`fxtr view` and launches tell this project's viewer from another's.

## Entities and arrays: raw CBOR

`outputPage(source, offset, first, filters, sort, last)` serves current completed output, defaulting to
25 rows. Its source names an array CID or a run node, and an optional selection: exact keys of any
dims of the output. A run's invocation dims are picked out in SQL; the other dims (produced
inside one invocation, and every dim of an array CID) filter rows as each array is read, so such
a selection examines rows it leaves out, though a plain page still stops early. A selection naming
a dim the output does not have is rejected. Each
request reads result references in batches of 32; ordinary pages stop when enough rows are found.
Filters and sorts scan the entire selected output before answering. Sorting retains only the
best `offset + first` rows with deterministic ties; deep sorted pages need more scratch memory.
Responses contain the actual offset, array type, CBOR row values, `hasMore`, and an exact total
when the whole source has been examined (otherwise null). Requests past the end clamp to its last
aligned page. `last: true` scans the selection explicitly, retaining a bounded tail or a reversed
sort heap of at most one page, and returns the last aligned page. Plain page queries also keep a
one-page tail for overshoot recovery. Rows may move between requests as results arrive.

`invocationPage(jobId, pathname, nodeId, selection, offset, first, filters, sort, state, last)`
(`OutputStore.invocation_page`) pages a run's invocations, every one the selection picks out
(of its invocation dims; only those in one display `state`, given one), each as its `Spawn` with
what it produced: its result's row count, and when every invocation produces one row (the
output has no dims but the invocation dims), that row's value as CBOR. Filters and the sort are
over each invocation's cells, as `outputPage`'s are over a row's (`table_values.py`): `key:<dim>`,
`state:` (its display state), `rows:` (its row count) and the one row's value columns (`value:`,
`value:<field>`, `value_cells`). An invocation with no result has no row count or value: a filter
on those leaves it out, and a sort on them puts it last either way; ties keep key order. With no
filter or sort, the page is read in key order in SQL and only its results load; otherwise every
invocation in the selection is read, and every result too when the filters or sort name the row
count or values. The offset snaps and clamps as a run page's (`browse.page_offset`), `last`
asking for the last page.

`outputDimValues(source, dim, search, offset, first)` pages the distinct keys `dim` takes in a
source's current output, for dims no spawn key records (the viewer asks `keyPage` for invocation
dims). It merges a summary of each of the source's arrays (the dim's distinct values, and those
some row has alongside the other dims' picks), cached by array CID, invocation key, dim, and those
picks (at most `MAX_SUMMARY_VALUES` values in all): an array is decoded once per set of picks, so a
search, another page, or a run that has gained outputs reads only the result references and the
arrays not summarized yet (an array source still loads its one array to open it). Values are
ordered strings before numbers, as `keyPage`'s jsonb order is, but strings by code point, not
the database collation; offsets snap to page boundaries, and a value is `compatible` when some
row with it matches the source's selection of the other dims.

Two output requests can run concurrently. Loads coalesce by CID; cache hits and unrelated
arrays do not wait on another fetch or decode. In-flight results also serve concurrent readers
of oversized arrays, and surviving readers retry if the load owner is cancelled.
Blob reads are batched and CPU work runs in worker
threads. Disconnected requests stop between result batches. Decoded arrays are cached by immutable
CID, shared across cards/queries/spawns, with a 128-array / 256 MiB budget measured on decoded
objects. Oversized arrays can be queried but are not retained. Cache eviction is transparent;
there are no client query handles or cache-expiry errors. A cold array still decodes whole, and
a single array or a batch's temporary working data can exceed the cache budget. Filtering and
sorting repeat their scan on each request; caching saves blob reads and decode work. Text sorting
uses Unicode code-point order; numeric sorting preserves large integers. `table_values.py`
defines display/search text with ECMAScript float notation and exact decimal integers;
`frontend/test/fixtures/table-values.json` supplies CBOR cases tested in both runtimes. `entityMetas` batches
type-tag-only reads for chips without fetching entity bodies.

Entities travel as raw DRISL (DAG-CBOR) bytes and are decoded on the frontend (with a DAG-CBOR
library), so entity ids come back as CID objects — links, distinct from strings — with nothing lost
to a value→JSON encoding:

- `GET /api/entity/{cid}` (a plain Starlette route, not GraphQL — binary does not belong in JSON)
  returns the stored bytes verbatim from `entities.load_raw_bytes`, `application/cbor` (404 if
  absent, 400 on a malformed cid). `entity(cid)` in GraphQL returns just the type tag alongside.
- `POST /api/entities` with JSON `{"cids": [...]}` (at most `MAX_ENTITIES_PER_REQUEST`, 200) is
  the same for many entities in one request, read in one query by
  `entities.load_raw_bytes_many`: a DAG-CBOR list in the order asked, each item the entity's bytes
  as a byte string or null when it is not stored (400 on a malformed body or cid, or too many). The
  viewer loads every entity through it, in batches (`createBatchedDrislEntityLoader` in
  `fxtr-view-shell-util`), so a view over thousands of spawn outputs never has thousands of
  requests outstanding in the browser.
- `array(cid, limit)` loads the `Array` via its loader and returns `ArrayView`: the array's type as
  JSON, its total `rowCount`, and its rows — each key as JSON and each **value as base64-encoded
  DRISL**, so the frontend decodes cells with the same path as the entity viewer and gets
  id-cells-as-links for free. `limit` caps the rows returned (for small previews); `rowCount` is
  always the total, so a preview can say "N of M rows". The whole array entity is loaded either way;
  `limit` only bounds the payload.

## Change detection: `invocationVersion`

`invocationVersion(jobId, pathname)` (`versions.py`) is an opaque string that changes whenever
`invocation(jobId, pathname)` would return something different, spawn states included, or `null`
for a missing invocation. A client polls the versions of the invocations it shows (aliased fields,
one request) and refetches only those that moved. It is one aggregate query of **counts** — the
invocation's nodes, spawns, events and log lines, its children with a result, the job's bindings at
its step keys, and per-state tallies of its own, its children's, and
its step keys' attempts and of the job's job attempts — rather than timestamps, which a
late-committing transaction can make go backwards, plus a digest of the step cache's entries at its
step keys (the one input that is replaced and deleted rather than only added to). It must be kept in step with what
`describe_invocation` reads; its module docstring says why the counts cannot repeat.
`levelOverview` reads this same invocation revision inside its own snapshot. Each run node also
has a revision built from the same target facts, restricted to that run's spawns, plus the job's
job attempt facts that determine what is due. These revisions detect resumes and cache
replacements even when aggregate displayed counts are unchanged. All run revisions are read in
one query; unrelated target progress leaves a run's revision unchanged.

## GraphQL over HTTP only

The schema has no subscriptions. The server is a different process from the runners, so
`Database.wake` topics never reach it, and the experiment viewer in `frontend/` polls over HTTP:
overviews, with `invocationVersion` polling for complete records. The GraphQL mount refuses
WebSockets (`http_only` in `app.py`): Ariadne would otherwise serve GraphQL over one, queries
included, and browsers let a page of any site open a WebSocket to any address.

## Tests

`tests/test_debug_web_browse.py` checks SQL status projections against the runner's full readers,
including shared cache failures and resumes, and measures bounded responses/query counts
with 100,000 mapped invocations. `tests/test_debug_web_output.py` covers large arrays, global
filtering/sorting, exact CBOR values, repeated CIDs, and empty batches.

`tests/test_runner_postgres_debug_web.py` runs a tiny workflow (a step behind one run_step node) to
completion in a job runner, then drives the resolvers through `ariadne.graphql`: spawn states, the job overview
with its shallow invocation/step summaries, the per-entity `invocation` (nodes, the run-step spawn's
cache key, the workflow `ctx.log` line, composite `id`s) and `step` (bound pin, result, the step's
`ctx.log` line), null for missing per-entity targets, the jobs list, the step cache, the array view
(a cell that decodes to its scalar), the entity view and raw bytes, app construction, GraphQL
refusing WebSockets, the built viewer beside the `/api` routes (and its not-built message, and the
installed copy preferred to the checkout's). A gated job (a mapped step and a mapped child
workflow, one call of each held on an event) is walked through untaken, blocked, and finished,
checking spawn states, version changes, and current-page updates as results arrive. This file
also covers all typed summary/page GraphQL selections, invalid input/field rejection, transparent
output-cache eviction, byte budgets, 25-row defaults, and offset/source validation.
Two jobs pinned at different commits (fake environments), the second reusing the first's step
from the cache, check that each root and attempt says its own job and commit while the step, in
both jobs and in the step cache, names the producing job and commit.
It also covers the view bundle's routes and version header, `/api/info`, a second `serve` opening
the running viewer, a taken port refused (the viewer's and the dev server's), and the dev server
on its port or not at all.
Skipped unless Ariadne is installed and `FXTR_TEST_DATABASE_URL` is set.

`tests/test_debug_web_ports.py` covers automatic selection, strict explicit ports, paired Vite
conflicts, the 50-retry boundary, remembered ports, socket ownership and cleanup, and the chosen
address reaching Uvicorn, Vite, the browser, and the persistent record.

`tests/test_debug_web_views_watch.py` covers the watcher against a renderer package whose build
copies a source into its bundle: rebuilt on an edit with a new version, not rebuilt on a test's
edit, a failed build's error and output with the last bundle kept, an identical rebuild keeping
its version; immutable snapshot HTTP requests and superseded versions expiring, edits during startup
and later builds and while switching watch roots; startup failures with and without a valid
bundle, recovery after fixing sources, the hub following the watcher, and the views events on the
socket and the status route; and the build's side in `fxtr.project.views` (the Node check, the kept
output, tests out of the fingerprint).

`tests/test_debug_web_events.py` covers the hub (the current views state first, the socket that
falls behind dropped), the origin and body checks, the socket for a project without a bundle and
refused to another site's page, disconnects during a send, and launches announced to every open
page, a resumed job as resumed (on Postgres). `tests/asgi_testing.py` drives the app's requests and
sockets without a server.

## Not yet built

Recomputing derived node values (read-only, via the evaluator's pure `derive`/`evaluate`);
a persistent row index; authentication; paged attempt log tails (selected step details still load
their logs through the complete reader).
