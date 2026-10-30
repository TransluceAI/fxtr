# Phase 5 — dataframes: working discussion log

Pre-decision working notes for the dataframe/gather design round.
Nothing here is settled unless marked so; once decisions firm up they
get promoted through `notes/discussion_log.md` into the canonical docs
(`notes/design_overview.md` + module docstrings) per the standing
policy, and this file becomes historical rationale like any other plan
file.

## Amendment 2026-07-17 — the frame value is `pl.DataFrame`, no wrapper

Supersedes the `Table` ABC of section G (and the `Table` bits of
section B.3 / D). Landed with chunk 2. Promoted to
`notes/dataframes.md` (the frame-value + write/read sections) and the
`notes/discussion_log.md` amendment entry.

**Decision.** Delete the `Table` type entirely; the public frame value
is a `polars.DataFrame` at every table position (`read_table` returns
one, `write_table` / `gather` / task frame outputs accept one). The
alternatives were the section-G wrapper, and a "loose user tables /
strict system tables" split (rejected: keeps the wrapper's maintenance
surface, and a table whose carried schema may not describe its
contents is a confusing object). Rationale for going all the way to
raw polars:

- The wrapper's carried schema is near value-free on inputs (the
  *declared* schema governs writes; a table's own flags were already
  advisory) and on outputs users immediately unwrap to query. It was a
  toll booth between the user and their query engine.
- The planned interop surface (`to_polars` / `to_arrow_ipc` /
  `__arrow_c_stream__` / `to_rows`) just reimplements what polars
  already exposes. Raw returns get duckdb interop for free.
- Concrete gather win: the driving frame's **payload** columns become
  unconstrained (any polars dtype), since only the *key* columns must
  map to axis kinds and payload never becomes identity. Under a wrapper
  a datetime/categorical driving column was unrepresentable.
- Validation collapses to one story at the write boundary
  (`tables.validate_and_align`): names aligned by set, exact dtype
  match (+ the `Null`-column cast), null legality via native
  `null_count`. No eager construction-time validation, no all-nullable
  inference rule, no bool-coercion special case (polars *is* the
  ergonomic coercion layer now).

**Compat guarantee (the key check that made this safe).** `pl.DataFrame`
is confined to the **view layer** (`views.py` + the task frame-output
return type) — client-side of the wire on both the task side (view
runtime) and user side (capability / `Workflow`). The wire
(`runner_api.FrameWireType`) carries only arrow bytes; identity is the
canonical CBOR entity (`frame_encoding.py`, polars-free); the host uses
polars *internally* but exposes only bytes. So a future frame-type
change (a different type, or a lazy/chunked variant) is a client-side
source change behind an unchanged bytes wire — **not** a runner-compat
or persistence break. The abstraction seam the wrapper promised
survives as this confinement, and polymorphism can be reintroduced
client-side later without a wire touch. Enforced discipline: no
`pl.DataFrame` in a `runner_api.py` / `user_api.py` signature.

**Consequences already landed** (chunk 2): `Table`/`TableValue` leave
`views.py`'s signatures (`TableValue` stays in `model.py` as the
row-value union, used by `frame_encoding`); `tables.py` is the runtime
conversion/validation layer (dtype map, `make_frame`, IPC↔frame,
frame→rows, `validate_and_align`), the sole eager-polars module (core
keeps polars `TYPE_CHECKING`-only); `make_frame(schema, rows)` replaces
`make_table`/`from_polars` (a thin typed constructor, empty-safe, no
identity validation — that is the write boundary's job). Chunk 5's
"`Workflow.write_table` accepts a polars frame directly" special case
disappears (it is now the only case).

**Dependency split (settled after checking what actually needs
pyarrow, 2026-07-17).** Probed with pyarrow uninstalled: the whole
framework path (`make_frame`, IPC↔frame, `frame_to_rows`,
`validate_and_align`) and all native polars querying of a returned
frame (`group_by`/`filter`/`join`/`write_parquet`/`__arrow_c_stream__`)
work **without pyarrow** — polars' IPC is native Rust arrow. Only
`df.to_arrow()` and `duckdb.sql("from df")` (which calls `to_arrow`)
need pyarrow. Since duckdb is *already* opt-in (task authors bring their
own), pyarrow is treated the same: **runtime deps = polars only;
`duckdb` + `pyarrow` sit in the dev group** (backing the interop smoke
test), and a task author who wants the duckdb path installs both.
(Earlier in the round pyarrow was briefly made a full runtime dep for
"whatever is most ergonomic"; on inspection that was needless weight
for the polars-only majority, since nothing but the duckdb bridge uses
it — reverted to dev/opt-in.) This restores the 07-16 round's "duckdb
and pyarrow are never required" for the *runtime*: the framework itself
depends on neither; both are user-side opt-ins for the duckdb query
path. An `fxtr_experiment[duckdb]` optional extra is the natural future
packaging if this is ever published.

For the record, the *why* behind "duckdb needs pyarrow": every
ergonomic way to hand duckdb a real polars frame
(`duckdb.sql("from df")`, `con.register`, `df.to_arrow()`) routes
through `DataFrame.to_arrow()`; the only pyarrow-free route is an ugly
capsule-only wrapper (duckdb detects a polars frame and takes its
`to_arrow` path, so hiding the polars-ness is the sole way onto the
generic `__arrow_c_stream__` path). Not worth inflicting; the smoke
test just uses the natural path with pyarrow present in dev.

**Sub-point confirmed.** `read_table` returns frames whose declared
schema may be non-nullable, but polars cannot express NOT NULL — the
returned frame is ordinary-nullable with dtypes pinned to the canonical
five; nullability is a `FrameSchema`-only constraint (the user was
fine with this).

## Scope

Make dataframe cells real: the canonical entity representation, the
runtime `Table`, `read_table` / `write_table` / `gather`, frame task
outputs, and the user-surface equivalents — efficient enough that task
code can query gathered data with polars/duckdb without the framework
ever looping over rows in Python.

## The settled baseline (from the existing design)

- Dataframe cells hold rows matching a config-declared `FrameSchema`;
  canonical identity is an entity, so fingerprints, write conflicts,
  idempotent replay, record-serving, targets, and the at-rest format
  all work unchanged (`model.py`).
- `FrameWireType` is documented as an opaque v0 stand-in — changing it
  is anticipated, not a break (`runner_api.py`).
- `gather` is task-side sugar over one batched wire call
  (`read_batch`), with one `cell_value` observation per *distinct*
  addressed cell; missing/errored cells fault, empty cells contribute
  zero rows.
- `Table` is deliberately abstract (`schema` + `to_rows()`); the
  runtime representation was left unpinned on purpose.

## Direction agreed 2026-07-16 (tentative until promoted)

### A. No first-class FxtrID columns

`ColumnKind` drops `"entity"`; `TableValue` drops `FxtrID`;
`DataView.gather` is deleted (its only output was an entity column).
Rationale:

- Entity columns are what force frame entities into the
  complete-subgraph machinery (each column value pins its referent).
  Without them a frame entity is a **leaf** — trivial closure, and the
  payload can move through the system as opaque bytes without ever
  being scanned for references. This is what makes the streaming
  design below possible at all.
- They also carry the loadability obligation ("receiving a native
  FxtrID means you can load it"), which is very hard to honor for ids
  flowing through arbitrary relational ops.
- The covered use cases: do the data-intensive query on scalar
  columns, use the result to compute *keys*, then read the entities
  from an ordinary `DataView` per key. Ids can still ride tables as
  plain `str`/`bytes` values, explicitly not load-bearing.
- Re-adding entity columns later (with their obligations) stays
  possible once real use cases exist; removing them now is the cheap
  direction.

Known ergonomics regression to record: domain-driven fan-out over an
*entity*-output task grid was previously expressible as a
`DataView.gather` (which batched demands via `read_batch`); it becomes
a loop / task group of `read()` calls — semantically identical (same
per-cell observations) but without the batching hint. A
`read_many(table, on=...) -> list[FxtrID]` convenience over
`read_batch` can be added later if fan-out scheduling wants it.

### B. Three representations, one identity

1. **Canonical form (identity-bearing): an owned CBOR rows entity**,
   exactly as already designed. This stays what the entity store
   holds durably, what observations fingerprint, what write
   conflict/idempotence compares, what export references, and what
   import rebuilds from. Arrow bytes are *never* identity-bearing
   (Arrow IPC output is not stable across library versions —
   fingerprints must not be codec-derived).
2. **Transport/serving form (never identity-bearing): Arrow IPC
   stream bytes.** Both polars and duckdb consume Arrow natively, and
   the Arrow PyCapsule interface (`__arrow_c_stream__`) gives
   zero-copy interop without pinning a query engine. Column mapping:
   `str`→utf8, `int`→int64, `float`→float64, `bool`→bool (+ see the
   open questions on nulls/bytes).
3. **Task-side handle: `Table` stays abstract** but grows the interop
   surface — `to_polars()`, `to_arrow_ipc()` / `__arrow_c_stream__`,
   constructors from polars/arrow/rows with schema validation;
   `to_rows()` remains the slow path and the semantic reference.

Write path: task code hands the adapter a `Table` → arrow bytes cross
the wire → the **runner** validates against the declared schema,
derives the canonical rows form, stores the canonical entity (the
identity), and keeps the arrow form as a **projection** — pure cache,
derived, rebuildable from the canonical entity, never exported, never
fingerprinted. Read path: serve the projection (rebuild from CBOR on
miss); the CBOR→arrow builder must exist regardless because import
starts with an empty projection store.

This resolves the design overview's open question 5's
"dual-representation" perf item as: projection = arrow IPC blob
(cache), not a per-grid sqlite table.

### C. Multiset identity; canonical sort owned by the framework

Settled this round (2026-07-16): row **multisets** stay the semantics,
and the framework canonicalizes at write time — row-order determinism
is *not* delegated to task programs. Rationale:

- Execution semantics bless ordinary anyio concurrency at the task
  outer level (records replay-match as a set); assembling rows in
  completion order is the natural fan-out shape, and under ordered
  identity that program fingerprints differently per run.
- polars/duckdb do not guarantee join/group-by output order across
  versions or engine settings, so ordered identity would put an
  explicit order-pinning obligation on the main gather → aggregate →
  `write_table` path, with violations surfacing as delayed staleness
  false positives (possibly only after a library upgrade) rather than
  loud failures.
- Unlike the general "canonicalize before storing" obligation on
  arbitrary entities (which the framework cannot discharge), for
  frames the framework *can* discharge it centrally.

Alternatives considered: ordered identity + user obligation (rejected
per the above); ordered identity with a default sort in `write_table`
(unstable middle — the default sort determines stored bytes and
therefore ids, so its spec must be pinned/owned anyway, and same-set-
different-order rewrites would spuriously conflict).

The sort spec is deliberately trivial: **rows sort lexicographically
by their canonical per-row encoded bytes.** The key derives from the
encoding we own anyway, so there is no per-kind total order to
design, no null-placement or NaN-ordering decisions, and nothing that
can drift. The resulting order is semantically arbitrary — readers
must never attach meaning to row order (order-meaningful data carries
an explicit rank column). Implementation can still sort natively
(encode columns, argsort the byte strings).

### D. Wire inventory (which boundary carries what)

- **Task view-runtime ↔ run capability** (`RunCapabilityProto`; the
  service boundary, possibly cross-sandbox): frames cross as arrow
  bytes in both directions (`read_cell_frame`, `write_cell_frame`,
  the gather op's key table and stitched result, `run_task`'s
  frame-output slots). **Frame entity ids never surface to task
  code** — `Table` carries no id; the id is runner-side bookkeeping
  (fingerprint, projection key).
- **Runner internals** (ops engine ↔ session/stores): observations,
  record-check, conflicts, results all keyed by the canonical frame
  entity id, unchanged. A record hit serves the recorded fingerprint
  id → materialized to bytes via the projection store.
- **User capability ↔ clients**: same dialect as the run capability
  for `Workflow`/RPC use. The browser/JSON dialect (dashboard) is the
  rendering question (open question 8) — frames need at least a
  non-erroring `CellReport` story.

### E. Gather: host-stitched stream + thin local key-join

Settled this round (2026-07-16). Gather is one effectful wire op
whose request is a **deduped key table** — arrow bytes carrying only
the columns that bind the view's open axes, never the caller's
payload columns — against the granted token. The **host** resolves
the distinct addressed cells, runs the record-check / demand /
fault-on-missing, records the per-distinct-cell observations, and
returns **one natively-stitched arrow stream**: key columns + the
cell schema columns, all matched cells' rows concatenated, in
canonical order (sorted keys; each cell's rows in canonical row
order — input key order is deliberately irrelevant, which also
absorbs any nondeterminism in the sender's dedup).

The user-facing `gather(table, on, column_prefix)` is view-runtime
sugar over that op (framework code in the task's process): extract
and dedupe the key columns → wire call → key-join the returned
stream back onto the caller's table, applying `column_prefix` and
the collision checks (which need the driving schema the host never
sees). Output order pins to driving-row order × stream order. For
the common domain-cross driving table (key columns only), the local
join is essentially the identity.

Division-of-ownership rationale:

- Everything load-bearing for system state or the record — which
  cells were addressed, observations, faults, the served rows and
  their canonical order — is host-side, under the host's pinned
  dependencies.
- The wire carries the minimum in both directions: keys out, each
  distinct cell's content back exactly once, as one stream stitched
  natively by the backend (no per-cell IPC blobs to re-concat
  sandbox-side). No fan-in/fan-out shape changes this.
- The one task-process step, the local key-join, is acceptable
  determinism-wise: within-generation replay runs the same task env;
  cross-version engine drift in a key-join is no different in kind
  from the task fn's own local polars use, and is absorbed at the
  next frame write by the canonical sort.

Alternatives considered: (i) one `read_batch` of the distinct cells
plus a full view-runtime-side stitch+join — rejected: pays per-cell
encoding overhead for a tuple of separate blobs and puts the stitch
in sandbox-resident code when the host can stitch natively; (ii)
shipping the entire driving table for a complete host-side join —
rejected: payload columns cross for no reason, and fan-in (many rows
addressing few cells) duplicates cell rows in the return payload.
Task code cannot observe where the join happens, so revisiting the
split later is a transport change, not a semantic one.

`read_batch` note: its documented role as "the gather substrate"
lapses under this design; whether it stays in the wire protocol
(e.g. as the substrate for a later `read_many` entity fan-out) is an
implementation-time call.

### F. The scalar encoding spec

Resolved this round (2026-07-16). Home: a new leaf module
(`frame_encoding.py`-ish) whose docstring *is* the spec, per the
`encoding.py` pattern — import-light, depending only on the model
types; the polars-backed `Table` runtime lives elsewhere. Per the
owned-encodings doctrine the rules are stated as spec, with tests
verifying that cbor2's canonical mode conforms — the codec
implements the spec, never defines it. (Verified empirically
2026-07-16: cbor2 canonical mode normalizes every NaN — any
payload/sign — to `f97e00`, preserves `-0.0` distinct from `0.0`,
uses RFC 8949 shortest-form floats, and bignum-encodes ints past
int64, so int64 is an arrow-side validation rule, not a CBOR limit.)

- **Floats**: shortest-form encoding; all NaNs normalize to the one
  canonical NaN (identity-wise NaN = NaN — required for a frame
  containing NaN to fingerprint-match its own replay); `-0.0`
  preserved as a distinct value (real and deterministically
  produced; the footgun — identity distinguishes two floats polars
  compares equal — is documented, not engineered away).
- **Ints**: pinned to int64 range (arrow interop; matches sqlite
  INTEGER and de-facto coordinate range), enforced at validation.
  Exact-type checks (`type(v) is int`; bool rejected), matching the
  coordinate convention in `encoding.py`.
- **`bytes` column kind**: added now (CBOR- and arrow-native; the
  sanctioned FxtrID-as-plain-data escape hatch).
- **Nullability: per-column, in the schema, default non-nullable.**
  The python dataframe layer is all-nullable by convention (polars
  cannot even express NOT NULL; arrow's field flag is advisory), but
  that is the *computation* layer, where nulls are the natural
  algebra of relational ops in flight. `FrameSchema` is a storage
  contract closer to SQL DDL, where NOT NULL is normal hygiene;
  missing data is usually an error and write-time is the cheapest
  place to catch it. `nullable=True` is the explicit, visible opt-in
  for genuinely-optional columns. Encoding: flag omitted when
  default, so strict columns' encoded form never carries it. Note
  gather never manufactures nulls (missing cells fault, empty cells
  contribute zero rows, null key-column values are op errors), so
  nulls exist only where a writer wrote them into a declared-nullable
  column.
- **Frame entity shape**: embeds the tagged schema (the
  `_schema_to_json` form) plus rows as arrays in schema column
  order, sorted lexicographically by encoded row bytes; an empty
  frame (schema + zero rows) is a valid value (the "SKIP" shape from
  the old status-code discussion). Embedding keeps the entity
  self-describing per the at-rest doctrine ("the value entity is the
  whole fact"); corollary: a task frame output and a dataframe cell
  with equal schema and rows share an id, so fingerprint equality
  means what you'd expect. Cost accepted: same rows under different
  column names don't dedup in the entity store.
- **The encoding layer is exact-type strict** (identity code never
  coerces); ergonomic coercion belongs to `Table` constructors
  (section G).

### G. The `Table` API

**Superseded by the 2026-07-17 amendment (top of file): the `Table`
wrapper is gone; the frame value is a raw `pl.DataFrame`.** The dtype
mapping, exact-cast rule, write-boundary validation, and column-name
constraints below all carry over verbatim to `tables.py`; only the
wrapper class, its constructors (`make_table` / `from_polars`), and its
interop methods are dropped (polars provides them). Kept for the
rationale trail.

Settled 2026-07-16 (wrapper details now historical):

- **polars is the required table dependency** of `fxtr_experiment`,
  used host-side (IPC parse, the canonical-sort assist, gather
  stitching) and task-side (backing `Table`). duckdb/pyarrow are
  never required — interop via IPC bytes + `__arrow_c_stream__`
  (duckdb queries a `to_polars()` result via replacement scans).
  Known v0 perf note: the canonical row encode (identity + sort key)
  is a Python loop over rows, write-path only; a native encoder is a
  later follow-up.
- **Contract/impl split follows the views pattern**: the `Table` ABC
  in `views.py` stays the API reference; one polars-backed concrete
  class lives beside `frame_encoding.py`; factories
  (`make_table(schema, rows)`, `Table.from_polars(df, schema=None)`)
  import polars lazily.
- **In-memory order stance** (an ABC contract fix): a `Table` has a
  definite, deterministic row order (gather: driving order;
  `read_table`: canonical order; constructors: given order);
  multiset semantics apply at the *storage boundary* only.
  `to_rows()` returns current order (was: "canonical sorted order").
- **Interop surface**: `to_polars()`, `to_arrow_ipc()`,
  `__arrow_c_stream__`, `to_rows()` as the slow path/semantic
  reference.
- **Dtypes: exact one-to-one, no implicit casts** — `str`↔String,
  `int`↔Int64, `float`↔Float64, `bool`↔Boolean, `bytes`↔Binary;
  `from_polars` rejects e.g. Int32 (an upcast changes encoded bytes,
  i.e. identity — users cast explicitly). Loosening later is
  compat-safe; tightening is not.
- **Nullability at the write boundary: the grid's declared schema
  governs; a Table's own flags are advisory.** Write validation is
  names + kinds + data legality (no nulls present in a
  declared-non-nullable column), never flag-vs-flag equality
  (nullability is a constraint on data, not a property of it; flag
  inference from `null_count` would make schemas data-dependent).
  `from_polars(df)` infers kinds and marks all columns nullable;
  `from_polars(df, schema=)` validates the data against it.
- **Consequence: canonicalization is host-side by necessity.** The
  canonical entity embeds the *declared* schema, which only the host
  knows (token → grid config; for frame outputs, the task grid's
  declared `FrameOutput` schema), so identity is minted host-side
  after config resolution in every path. The task adapter ships raw
  arrow bytes; canonical-encoding code never exists in a sandbox.
- **Column order at write: aligned by name** (unambiguous — names
  are unique within a schema); the error is a name-*set* mismatch.
  Exact-order writes are not required (polars ops reorder freely).
- **Column name constraints enforced at config creation** (the
  `validate_grid_key` pattern): non-empty, unique, no control
  characters; identifier-safety left to users. Declared schemas are
  clean by construction; locally-built tables with odd names simply
  fail the write-time name match, and gather's `column_prefix`
  output is never stored so needs no constraint.
- **No `__eq__` in v0** (an accidental O(n) multiset compare is too
  easy to invoke); an explicit `same_data()` helper can come later.
- Reads are eager in v0; `on=` gather keys must be str/int columns
  exactly matching the axis kinds; gather layout stays driving
  columns then (prefixed) cell schema columns.
- **Top-level ergonomics** (closes the former open question on
  `Workflow`-side conveniences): the user capability's wire takes
  arrow bytes like the run capability; `Workflow.write_table`
  accepts a `Table` or a polars frame directly — sugar over
  `from_polars` inference, safe because the grid's declared schema
  governs matching.

## Round 2026-07-17 — chunk 3 kickoff

Settled at chunk-3 planning (details in `chunk3_cell_ops.md`):

- **Open question 1 resolved as (a)+seam**: v0 projection = in-memory
  bytes-budget LRU, lazy-only population, caching IPC bytes (the
  serving form) keyed by frame id, behind a `get`/`put` seam so the
  persistent variant (lmdb, or a sqlite blob sidecar — pick by
  benchmark once gather makes cold-many-cells real) drops in later.
  Budget: bytes-budget kwarg, default 256MB. Rebuilds use a
  **trusting decode**, not `load_frame`'s full re-validation
  (proposed, then reversed on review): no path — wire or user — can
  record a frame id that wasn't minted host-side by
  `build_frame_entity`, and the system trusts its own store on read
  everywhere else; per-read re-proof only guarded "the database was
  corrupted", which breaks everything regardless. The strict loader
  survives as the spec's read half (tests / tooling / future
  import-time verification of foreign stores — the trust boundary
  where verification actually belongs, once, not per read).
- **`read_batch` cut now** (both protocols), not deferred to chunk 4:
  gather is a dedicated wire op, and a future `read_many` fan-out
  would be a new op anyway.
- **Safety-pass canonicalization** (found at planning): the pass
  compares replayed `run_task` outputs against recorded results, and
  a replay legitimately reassembles rows in a different order — frame
  outputs must be canonicalized to ids before the comparison, or
  every frame-writing task false-positives `NondeterminismError`.
- **Output-kind checks** on both read ops (entity read rejects
  `FrameOutput` selections and vice versa) — needed to keep "frame
  ids never surface" true.
- Surface parity: user `write_cell_frame` gains `adopt`;
  `Workflow.demand_table` is the frame sibling of `demand` (which
  stays entity-only). `FrameWireType` keeps its name (= `bytes`); no
  v0 wire-size guard.

## Round 2026-07-17 (later) — chunk 4 kickoff

Settled at chunk-4 planning (details in `chunk4_gather.md`):

- **Gather resolves its misses concurrently** (one anyio task group;
  request edges all committed up front; per-cell observations as they
  land). The initially proposed sequential resolution had no
  justification: nothing recorded depends on resolution order (the
  record, request edges, and safety-pass comparisons are set-shaped;
  the response stream is sorted afterwards; the task-visible output
  is driving-order × canonical-order either way), the executor's
  in-flight map already dedups concurrent demands of shared upstream
  cells, and gather fan-out is exactly where parallel demand pays.
  Failure shape (as revised in the follow-up round below): per-request
  recording + one synthesized signal; cancelled siblings'
  asked-but-unanswered request edges are the normal interrupted
  shape. No v0 concurrency cap (a global limit is a deferred
  executor-level scheduling knob).
- **User gather = the single-read rule per cell** (target required
  for task-output cells, gated reads for dataframe cells), and **no
  auto-target sugar** — deliberately minimal, because of the revisit
  below.
- Wire form: positional key-column names; host dedups duplicate keys;
  empty key table and zero-open-axes gathers legal; local inner join
  via row-index (empty cells ⇒ their driving rows contribute zero
  output rows — no null-padding).

## Round 2026-07-17 (later still) — gather poisoning mechanics revised

The first-landed failure shape — catch the task group's exception
group, pick one representative (error-class preferred), let the op
wrapper record it — was flagged as a smell on review and replaced.
The problem: the poisoning contract's substance is the failure
*rows*, recorded at raise time, with the escaping exception being
transit-informational; the pick design inverted that (classification
rode the escaping exception through postprocessing, and concurrent
multi-failures lost every row but the representative's).

Replacement (the "k parallel requests" reference semantics, durable
half): **each failing cell records its own failure row at raise
time**, classified from its own typed exception — exactly what k
parallel single reads would record — so the generation's terminal
status derives from the recorded set (error outranks fault) with no
pick logic anywhere. The op then raises one **synthesized**
`errors.GatherFailedError` naming every failed cell, chained from the
group; it is a signal for already-recorded causes and is itself never
persisted (the `StepInvocationFailedError` skip-list precedent).
Uniform on both surfaces (the user twin records nothing but raises
the same signal shape; unwrapping single-failure cases to the bare
typed error was considered and rejected — robust callers must handle
`GatherFailedError` regardless, so the unwrap only added a second
case). Cancellation stays op-grain: sibling/external cancellation
records nothing per cell (the request edges already carry the
asked-but-unanswered facts; full parallel-reads emulation would spam
one abort row per cancelled sibling and drown the real cause), and an
externally cancelled gather records the wrapper's single
`RequestAbortedError` as before. `_effectful_op` stays on the op —
its remaining jobs (obligation lifecycle, op-level validation
failures, the cancellation arm) are genuinely op-grain;
`batch_representative` is deleted.

Two follow-up calls from the same review thread:

- **Signal class: one type, not a fault/error pair.** A
  `GatherFaultedError(CellFaultError, GatherFailedError)` aggregate
  subtype (raised for all-fault batches) was proposed and then
  reverted before landing: auditing the consumers showed the signal's
  class has *no durable reader* (terminal status, the demand chain,
  and task-side handling all key off the per-cell rows or the
  generation status; poisoning stands regardless of catching), so the
  only beneficiary would be a hypothetical interactive caller
  branching on the aggregate — speculative, and adding the subtype
  later is purely additive while removing it later would break
  catchers. `GatherFailedError` stays deliberately not-a-
  `CellFaultError`, documented.
- **Group handling: catch `ExceptionGroup`, not `BaseExceptionGroup`.**
  The constructor auto-specializes (all-`Exception` leaves ⇒
  `ExceptionGroup`), and every `Exception` leaf of the gather's task
  group by construction passed through `resolve_one`'s collector — so
  `except ExceptionGroup` catches exactly the groups that are wholly
  ours, and a cancellation-bearing `BaseExceptionGroup` passes through
  untranslated (cancellation is never absorbed; nothing manufactured).
  Replaces both the earlier `if not failures: raise` (which could let
  a bare group escape past every wrapper arm) and an interim
  leaf-dissection variant (correct but needlessly surgical).

### Flagged for end-of-phase revisit: targeting as expressions

(User sketch, 2026-07-17 — deliberately not designed now; it made the
"how do user gathers interact with targets" question moot-leaning.)
Per-cell target records are likely the wrong at-rest representation
of user intent. Instead: targeting state determined by an
*expression* (or set of expressions) in a small DSL describing the
user's demands — including a "gather-demand" form ("demand every
cell addressed by gathering per this deterministic op sequence",
the successor of the deferred domain-cell-driven open-slice
targets). Then possibly *drop the target requirement on demands*
entirely: users may demand arbitrary cells, with a **warning** when
the demanded subject is currently *unwanted*; such demands are
ungrounded until the targeting expressions are updated to match —
grounding stays the honest ledger, targeting becomes declarative and
managed separately.

## Open questions

### 1. Projection persistence — RESOLVED 2026-07-17 (see round above)

The projection is pure cache under every option (canonical entity is
the durable truth; import rebuilds). Options:

- (a) In-memory LRU keyed by frame id, build-from-CBOR on miss.
  Simplest; every cold read after restart pays one decode.
- (b) Persistent cache table (state db or sidecar), explicitly
  excluded from export, emptied on import. Buys: restart-warm reads,
  and `read_batch` can serve many blobs in one sqlite query.

Lean 2026-07-16: either is acceptable; start with the simpler (a)
unless writing (b) is nearly free once the write path exists —
decide at implementation-plan time. The CBOR→arrow builder exists in
both worlds.

### 2. Dashboard rendering minimum

Soon after core mechanics: frame cells must at least not error in
`inspect_cell` / the cell watch (`CellReport` needs a frame variant —
likely row count + schema + truncated row preview in JSON form;
full-table rendering and the browser-side arrow story can come
later).

### 3. Follow-up: table entities into step fns

v0: `Table` is not `FxtrServiceData` — tasks convert explicitly if a
step needs tabular data. Possible later affordance: pass a *frame
entity* into step args and rehydrate to a `Table` step-side.

### 4. Wire payload size

Large frames cross the run-capability wire as one bytes value —
including gather's stitched result streams, which span many cells.
Fine locally/v0; chunked transport and chunked/partitioned
cell storage are deferred together (they interact with the canonical
sort and question 1's cache shape, so note but don't design).
