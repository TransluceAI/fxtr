# Dataframes and tables

The dataframe subsystem: dataframe grid cells and task frame outputs,
the `polars.DataFrame` frame value, the `gather` primitive, and the
representations behind them. This is the canonical statement of
everything cross-cutting; the API contracts live on the view classes
(`views.py`), the canonical model types in `model.py`, and the runtime
conversions in `tables.py`. The
rationale trail (alternatives considered, review rounds) is
`notes/discussion_log.md` and `plans/phase5_dataframes/discussion_log.md`.

The subsystem is fully implemented (the encoding, the frame runtime,
the frame cell ops on both capabilities with the projection, `gather`,
and the dashboard's frame rendering); implementation rationale lives
in `plans/phase5_dataframes/`.

## The column model

A dataframe cell holds zero or more rows matching a config-declared
schema. Columns are **scalar-only**: `str`, `int`, `float`, `bool`,
`bytes`.

- **No FxtrID columns.** There is deliberately no entity column kind:
  a frame entity is therefore a *leaf* (no strong references), so
  closures are trivial and frame payloads can move through the system
  as opaque bytes without ever being scanned for references — which
  is what makes the arrow-native transport below possible. It also
  keeps the loadability obligation ("receiving a native FxtrID means
  you can load it") away from values flowing through arbitrary
  relational ops. The covered pattern: do the data-intensive query on
  scalar columns, derive *keys*, then read the keyed entities from an
  ordinary `DataView`. Ids may ride tables as plain `str`/`bytes`
  values, explicitly not load-bearing. (Re-adding first-class entity
  columns, with their obligations, stays possible if real use cases
  appear.) Consequently there is no `DataView.gather` — it would have
  nothing to return; domain-driven fan-out over entity-output task
  grids is a loop/task-group of `read()` calls (same per-cell
  observations).
- **Per-column nullability, default non-nullable.** The Python
  dataframe layer is all-nullable by convention (polars cannot even
  express NOT NULL), but that is the *computation* layer, where nulls
  are the natural algebra of relational ops in flight. A
  `FrameSchema` is a storage contract closer to SQL DDL: missing data
  is usually an error, and write-time is the cheapest place to catch
  it. `nullable=True` is the explicit, visible opt-in for
  genuinely-optional columns. Note that `gather` never manufactures
  nulls (missing cells fault, empty cells contribute zero rows, null
  key-column values are op errors), so nulls exist only where a
  writer wrote them into a declared-nullable column.
- **Schema column names are validated at config creation** (the
  `validate_grid_key` pattern): non-empty, unique, no control
  characters; identifier-safety is left to users. Declared schemas
  are therefore clean by construction; a locally-built table with odd
  names simply fails the write-time name match.

## Identity: the canonical frame entity

Cells have row-**multiset** semantics at the storage boundary, and
the framework canonicalizes at write time — row-order determinism is
*not* delegated to task programs. Rationale: execution semantics
bless ordinary anyio concurrency at the task outer level (records
replay-match as a set), and assembling rows in completion order is
the natural fan-out shape; engine output order (polars/duckdb
joins/group-bys) is not guaranteed across versions; and unlike the
general "canonicalize before storing" obligation on arbitrary
entities, for frames the framework *can* discharge the obligation
centrally. A consequence readers must internalize: **stored row order
never carries meaning** — order-meaningful data carries an explicit
rank column.

The canonical entity embeds the tagged schema (self-describing per
the at-rest doctrine: the value entity is the whole fact) plus the
rows as arrays in schema column order, **sorted lexicographically by
their canonical per-row encoded bytes**. The sort key derives from
the encoding itself, so there is no per-kind total order, no
null-placement or NaN-ordering decision, and nothing that can drift;
the resulting order is semantically arbitrary. An empty frame
(schema + zero rows) is a valid value. Corollary of embedding the
schema: a task frame output and a dataframe cell with equal schema
and rows share an id, so fingerprint equality means what you'd
expect; the accepted cost is that equal rows under different column
names do not dedup in the entity store.

Scalar encoding rules (owned spec; cbor2's canonical mode is the
implementation, verified by tests, never the definition):

- **Exact-type strict**: identity code never coerces (`type(v) is
  int` rejects bool, matching the coordinate convention); ergonomic
  coercion belongs to polars' own frame construction (the ergonomic
  layer).
- **Floats**: RFC 8949 shortest-form encoding; every NaN normalizes
  to the single canonical NaN (identity-wise NaN = NaN — required
  for a frame containing NaN to fingerprint-match its own replay);
  `-0.0` is preserved as distinct from `0.0` (a real,
  deterministically-produced value; the footgun — identity
  distinguishes two floats that compare equal — is documented, not
  engineered away).
- **Ints**: pinned to int64 range (arrow interop; matches sqlite
  INTEGER), enforced at validation.
- **Nulls**: CBOR null, legal only per the declared schema.

Everything downstream of identity is unchanged by frames being
"big": fingerprints, write conflicts (multiset equality is an id
comparison), idempotent replay, record-serving, targets, and the
at-rest format all operate on the frame entity id.

## Representations and storage

Three representations, one identity:

1. **The canonical entity** (above) — the durable truth: what the
   entity store holds, what export references, what import rebuilds
   from.
2. **Arrow IPC stream bytes** — the transport/serving form, *never*
   identity-bearing (arrow IPC output is not stable across library
   versions; fingerprints must not be codec-derived). Column mapping:
   `str`↔String/utf8, `int`↔Int64, `float`↔Float64, `bool`↔Boolean,
   `bytes`↔Binary.
3. **A `polars.DataFrame`** — the API type task and user code hold
   (below). There is no wrapper.

The runner keeps the arrow form as a **projection** — pure cache,
keyed by the frame entity id, rebuildable from the canonical entity
(the CBOR→arrow builder must exist regardless: import starts with an
empty projection store). Never exported, never fingerprinted. v0 is an
in-memory **bytes-budget** LRU (`state_store.FrameProjection`, behind
the session's one `frame_bytes` get-or-rebuild method; budget
configurable at session open); a persistent cache (lmdb or a sqlite
blob sidecar — restart-warm reads, batched blob serving) is a deferred
drop-in behind the same seam. Rebuilds use a *trusting* decode of the
session's own store — every recorded frame id was minted by the
framework's canonicalization, and the system trusts its own store on
read everywhere else; the strict validating loader
(`frame_encoding.load_frame`) remains for tests, tooling, and future
import-time verification of foreign stores. Reads serve rows in
**canonical order** — deterministic by construction.

**polars is the only required table dependency** (host-side: IPC
parsing, the canonical-sort assist, gather stitching; task-side: the
frame type itself). All of the framework's frame handling and all
native polars querying of a returned frame are pyarrow-free (polars'
IPC is native Rust arrow). **duckdb and pyarrow are opt-in, not
required** — they are needed *only* to query a returned frame through
duckdb (`duckdb.sql("... from df")` → polars' `to_arrow()` → pyarrow);
a polars-only task needs neither. A task author who wants the duckdb
path installs `duckdb` + `pyarrow` (they live in this package's dev
group, backing the interop smoke test). Known v0 perf note: the
canonical row encode (identity + sort key) is a Python-level loop over
rows, on the write path only; a native encoder is a deferred
follow-up.

## The frame value: `polars.DataFrame`

Relational data moves through the calculus as a `polars.DataFrame` —
there is deliberately **no wrapper type**. polars *is* the runtime:
its interop surface (`to_arrow`, `write_ipc_stream`, `__arrow_c_stream__`,
`.rows()`, the whole query API) is what task and user code get, and a
frame carries no fxtr identity — its schema/dtypes are ordinary polars
metadata.

- **The concrete type lives only in the view layer.** `pl.DataFrame`
  appears exclusively in `views.py`'s `DataFrameView` frame ops
  (`read_table` / `write_table` / `gather`) and the task frame-output
  return type — all client-side of the wire, on both the task side
  (view runtime) and the user side (capability / `Workflow`). The wire
  (`runner_api.FrameWireType`) carries only arrow bytes, and identity
  is the canonical CBOR entity; neither mentions polars. So a future
  backend swap (a different frame type, or a lazy/chunked variant) is a
  client-side change behind an unchanged bytes wire — not a
  runner-compat or persistence break. The seam the wrapper would have
  provided survives as this confinement; polymorphism can be
  reintroduced client-side later without touching the wire.
- **In-memory order is definite and deterministic** (gather results:
  driving order; `read_table`: canonical order); multiset semantics
  apply at the storage boundary only.
- **Declared-schema nullability is not carried by the frame** (polars
  cannot express NOT NULL). It lives only in the grid's `FrameSchema`
  and is enforced at the write boundary; a read returns an ordinary
  (polars-nullable) frame whose dtypes are pinned to the canonical
  five.
- **Dtypes are exact one-to-one, no implicit casts** — an Int32 column
  is an error at the write boundary, not a silent upcast (an upcast
  changes encoded bytes, i.e. identity); users cast explicitly.
  Loosening later is compat-safe; tightening is not. The lone
  sanctioned cast is a polars `Null` column (all-null, no dtype to
  preserve → cast to the declared kind, then ordinary null-legality
  applies).

The dtype mapping, the write-boundary validation, and every conversion
among the representations live in `tables.py` (the one module that
imports polars eagerly; the package core keeps polars
`TYPE_CHECKING`-only so importing it stays cheap): `make_frame(schema,
rows)` (a typed constructor, empty-safe — the friendly way to build a
frame with pinned dtypes, esp. an empty one), `frame_to_ipc` /
`ipc_to_frame`, `frame_to_rows`, and `validate_and_align` (below).
Construction runs no identity-level value validation — polars applies
its own construction coercion (its role as the ergonomic layer), and
the authoritative check is at the write boundary.

## Write and read paths

Writes: task code hands the adapter a `pl.DataFrame` → **raw arrow
bytes cross the wire** → the host validates and canonicalizes.
Validation (`tables.validate_and_align`) is **names + kinds + data
legality** against the target grid's *declared* schema — columns are
aligned by name (unambiguous; the error is a name-set mismatch),
dtypes must match the declared kinds exactly (the `Null`-cast
exception aside), and no null may sit in a declared-non-nullable
column (checked natively via `null_count`, no row loop). A frame's own
nullability is not even expressible in polars, so it is never compared;
nullability is a constraint on data, checked against the schema. The
stored entity embeds the declared schema. Because the canonical entity
embeds the *declared* schema, which only the host knows (token → grid
config; for frame outputs, the task grid's declared `FrameOutput`
schema), **identity is minted host-side after config resolution in
every path** — canonical-encoding code never exists in a sandbox. The
minting funnel every such path shares is
`frame_encoding.canonical_frame_from_ipc` (parse → align → canonical
entity): it lives with the identity spec rather than in the frame
runtime precisely because `tables.py` is the task-side module a guest
installs.
Writes are idempotent on the canonical id (multiset equality); a
different multiset already present is a conflict, exactly as for entity
cells.

Reads (`read_table`, frame-output reads): same contract as entity cell
reads (demand policy, `cell_value` observation with the frame entity id
as fingerprint, record hits serve the recorded fingerprint), with the
payload served from the projection as arrow bytes in canonical order,
which the task side parses straight into a `pl.DataFrame` (framework-
produced bytes are trusted — no re-validation). **Frame entity ids
never surface to task code** — the frame carries no id; ids are
runner-side bookkeeping.

## Gather

`DataFrameView.gather(table, on, column_prefix)` is the
fault-on-missing join: for each driving row, bind the view's axes
from the row's key columns and join the addressed cell's rows on.
Missing/errored cells fault; empty cells contribute zero rows —
**join arity is inner-join-shaped**: a driving row whose cell holds a
zero-row frame simply does not appear in the output (no null-padding;
gather never manufactures nulls); one
`cell_value` observation per *distinct* addressed cell. Key columns
must be str/int kinds exactly matching the axis kinds (the one
sanctioned cast: an all-null `Null`-dtype column, typically an empty
literal frame, takes the axis dtype); each axis binds from a distinct
column; null key values are errors. Result layout: the driving
columns, then the cell schema columns (with `column_prefix` as the
rename point for collisions).

Mechanics — one wire op, host-stitched:

- The request is a **deduped key table** (arrow bytes carrying only
  the axis-binding columns, named **positionally** — column `"i"`
  binds the selection's i-th open position, so no axis names cross)
  against the granted token; the selection may carry `at()`-bound
  positions, and duplicate keys are deduped host-side (never
  rejected). The **host**
  resolves the distinct addressed cells, runs the record-check /
  demand / fault-on-missing, records the observations, and returns
  **one natively-stitched arrow stream**: the positional key columns
  + cell schema
  columns, all matched cells' rows concatenated, in canonical order
  (sorted keys, each cell's rows in canonical row order — input key
  order is deliberately irrelevant, which also absorbs any
  nondeterminism in the sender's dedup). An empty key table addresses
  zero cells (zero observations, an empty typed stream); zero open
  positions address the single bound cell (a degenerate cross join).
- **The op is one request obligation decomposing into ordinary
  per-cell state**: every miss's request edge is committed *before*
  any resolution (request = observation, batch form), and the misses
  then resolve **concurrently** (one task group; the executor's
  in-flight map dedups shared upstream demands — nothing recorded
  depends on resolution order, and gather fan-out is exactly where
  parallel demand pays; there is deliberately no v0 concurrency cap —
  a global limit is an executor-level scheduling knob). **Failure
  semantics are per request**, exactly as if the cells were k parallel
  single reads: each failing cell records *its own* failure row at
  raise time, classified from its own typed exception, so the
  generation's terminal status derives from the recorded set (error
  outranks fault) with no exception-picking anywhere; the op then
  raises one synthesized `GatherFailedError` naming every failed cell
  — a signal for the already-recorded causes, itself never persisted
  (the `StepInvocationFailedError` precedent), and the uniform surface
  shape on the user capability too (where nothing records).
  Observations recorded
  before a failure stand, and a resumed identical gather replays
  them as hits; cancellation stays op-grain (one `RequestAbortedError`
  for an externally cancelled gather — the per-cell
  asked-but-unanswered facts are already durable as request edges).
  The batch is *not* one snapshot: each cell's
  check-and-serve is individually value-pinned, and cross-cell
  turnover mid-gather surfaces as ordinary staleness.
- The view runtime (framework code in the task's process) provides
  the user-facing sugar: extract and dedupe the key columns → wire
  call → key-join the returned stream back onto the caller's table
  (an explicit row index + stable sort, never join-engine ordering),
  applying `column_prefix` and the collision checks (which need the
  driving schema the host never sees). Output order pins to
  driving-row order × stream order. For the common domain-cross
  driving table (key columns only), the local join is essentially
  the identity.
- Division of ownership: everything load-bearing for system state or
  the record (which cells, observations, faults, the served rows and
  their canonical order) is host-side under the host's pinned
  dependencies; the wire carries the minimum in both directions
  (keys out, each distinct cell's content back exactly once, as one
  stream — no per-cell blobs to re-concat sandbox-side); the local
  key-join is acceptable determinism-wise (within-generation replay
  runs the same task env; cross-version engine drift in a key-join
  is no different in kind from the task fn's own local polars use
  and is absorbed at the next frame write by the canonical sort).
  Task code cannot observe where the join happens, so revisiting the
  split later is a transport change, not a semantic one.
- **The user capability's twin** serves the same wire op as an
  unrecorded interactive event under the single-read rules applied
  per cell: task-output cells require an attached target (user
  demands declare intent), dataframe cells go through the grounding
  read gate. There is deliberately no auto-target sugar — the
  end-of-phase targeting-expression revisit
  (`plans/phase5_dataframes/discussion_log.md`) would obsolete it.

## The user surface

The user capability carries the same frame ops with the same wire
forms (unrecorded interactive events, as for all user-backed views):
`read_table` / `write_table` / `gather` take and return
`pl.DataFrame`, exactly as task-side, safe because the grid's declared
schema governs write matching. Presence queries and the introspection
doctrine are unchanged, and `inspect_cell` / the cell watch need **no
frame-specific report shape**: a frame cell's `CellReport` carries the
frame entity id like any value, and — the entity being self-describing
— the dashboard renders the table by loading it through the
workstation's ordinary entity access (`frontend/src/frame_table.tsx`;
truncation is a display choice, not a wire policy). A `CellReport`
preview variant was considered and dropped: it would have pushed frame
content through every watch event for something entity loading already
serves on demand.

## Deferred

- Chunked/partitioned frame cells and chunked wire transport (large
  frames currently cross as one bytes value and store as one
  entity).
- Lazy tables and pushed-down querying.
- A native (non-Python-loop) canonical row encoder.
- `read_many(table, on=) -> list[FxtrID]` entity fan-out convenience
  (restores the batching hint lost with `DataView.gather`) — would be
  a new wire op (the old `read_batch` was removed with the frame cell
  ops; gather is its own host-stitched op).
- Table entities into step fns (pass a frame entity as a step arg,
  rehydrate step-side); v0: tasks convert explicitly.
- A multiset-equality helper over two frames (polars equality is
  order-sensitive; comparing frames as row multisets means canonical-
  encoding both — deferred until a use case appears).
- The persistent projection cache (vs the v0 in-memory LRU).
- Richer dashboard frame rendering (the current renderer loads the
  whole frame entity into the browser on demand and renders a
  truncated-display table — `frontend/src/frame_table.tsx`; browser-
  side arrow / partial loading belongs with the chunked-cell story).
