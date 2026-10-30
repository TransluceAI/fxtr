# Discussion log

Running co-design log for the fxtr_workflow_3 spike. Decisions sections are
newest-first. Each records decisions made, alternatives considered, and open
questions to come back to. This is a working document, not a polished design.

Reference points:
- `notes/sketch.md` — the initial design sketch this discussion starts from.
- `spikes/fxtr_workflow_2` — previous attempt; some ideas (service handles,
  stateless service provider, op-log/trace-based durability) carry forward.

## Status — resume here (as of 2026-07-03)

The **type surface of the design is settled** through several co-design
rounds; no runner/storage/execution logic exists yet. To get back up to
speed, read in this order:

1. `CODE_OVERVIEW.md` — what exists and where.
2. `fxtr_workflow_3/view_spec.py`'s module docstring — the reference
   *exposition* of the core concepts (holders/frames, positional addressing,
   mapped/unmapped axes, strip-set), with worked examples. Terminology used
   everywhere else is defined there.
3. `notes/execution_semantics.md` — the dynamic execution model
   (generations/runs, universal record-check, demand policy, safety pass,
   embodiment commitments). The dynamic sibling of view_spec's exposition.
4. `notes/example_mh_pilot.md` — how the API feels end to end (scheduler,
   grade with steps, aggregate with tables/gather; flat + nested variants).
5. The Decisions sections below (newest first) for the *why* of anything.

Settled highlights: grids as open partial functions with domain-dataframe
cells; positional axis addressing with names as local views (observability
principle for config identity); the capability-view rewrite calculus with
symbolic canonicalization (P1–P5; batch-invariance rejection; subtree
full-frame invariant); error-poisoning contract; generations vs attempts;
indexers/checkers dissolved into tasks + dataframe cells + named outputs +
Table/gather (no SQL engine in v0). The canonicalizer is implemented and
tested (`view_spec.py`); `views.py`/`task_api.py` are contract sketches.

**Next meaty topics (queued)**: execution semantics are decided and written
up (`notes/execution_semantics.md`); embodiment rounds 1–2 (scope tree, run
capability, ServiceRef boundaries for task fns *and* steps, opaque task-side
views, storage layout) are decided — see Decisions 2026-07-03. Remaining
order: the design queue is empty — every surface is decided, captured, and
sketched (`execution_semantics.md` + the contract modules + `storage.py`,
storage rounds 1–5). **Implementation has a plan:
`plans/phase0_walking_skeleton/`** (overview + 8 chunk files;
deterministic-only tasks walk end to end in chunk 6, steps join in chunk 7,
ServiceRefs from day one with RPC as a final smoke test). Next: implement
chunk 1.

## Open questions

(tracked here so we don't lose them; move into dated sections as they get resolved)

1. **Private-subtree read grants.** May a task grant read access to grids in its
   private internals subtree onward? Tentative compromise (2026-07-02): tasks
   created *inside* your internals may be granted reads of your internals; tasks
   created in external/input subtrees may only be granted input-reachable grids.
   Unclear how much this matters (staleness feels slightly weird otherwise); watch
   for consequences as the design continues rather than settling now.
2. **Data-dependent reads.** Correlating by data is banned for grid *creation*
   (P3), but data-dependent reads (read a cell, use its value to choose which
   coordinates to read next) are allowed and just create richer observation
   dependencies — `gather` driven by domain-dataframe rows is now the
   primary/canonical instance. Anything else to watch here (e.g. how such
   reads render in provenance views)?
3. **FxtrID vs EntityRef in capability signatures.** Draft uses bare `FxtrID` for
   cell values (arena coverage still ships on the wire). EntityRef would carry
   the target type for ergonomic loads — revisit when the runner exists and we
   see what task code actually looks like. (2026-07-03: the per-run-scope
   design makes a later EntityRef upgrade purely additive — every id a task
   receives is loadable from its run scope.)
4. **Concurrency and write model.** Two tasks writing disjoint coordinates of one
   data grid; user edits concurrent with tasks; conflict detection timing;
   single-runner v0 assumption.
5. **Tables/dataframes — remaining opens** (direction decided 2026-07-03, see
   Decisions): frame-gather collisions are handled via an optional
   ``column_prefix`` (the appended names come from config schemas, not
   renamable on views — this is the rename point); remaining: the broader
   table rename/ops surface;
   whether Tables in checkpointed step args need anything special; storage
   dual-representation (canonical frame entity + materialized sqlite table) as
   a perf question; lazy/pushed-down querying and `ctx.sql` sugar later if
   scale warrants.
6. **Domain cells and grid metadata.** Domain cells are now naturally zero-axis
   dataframe cells (schema = the semantic axis) — enumeration is native, though
   which cell governs which axis remains convention. Should the grid "tag"
   entity eventually point at the domain cells governing each axis (for
   UI/browsing/joins)?

## Followups (explicitly deferred past v0)

- **Step-boundary cancellation of stale runs.** v0 lets a running-but-stale
  generation finish (result lands already-stale; writes still conflict-checked and
  responsibility-linked). Later optimization: cancel at the next step boundary —
  prevent launching new tasks/checkpointed steps but let in-progress steps finish.
- **Rule migrations.** v0: a task grid's rule is fixed at creation; changing the
  rule means a new grid (contents invalidated). Later: allow migrating a grid to a
  mutatis-mutandis rule (e.g. swapped input grids with identical values) without
  invalidating, plausibly via the same migration machinery as handle migrations.
  The checkpoints may be the most valuable thing to migrate (glue-task results are
  often cheap to recompute; long step histories are not).
- **Sandboxed-task code identity.** Once sandboxed tasks exist, strengthen the
  handle trust boundary with the nix derivation hash of the actually-running code.
- **Best-effort determinism-footgun helpers.** Helper base classes for
  task/step implementers that catch common determinism mistakes early (the
  strong guarantee is the always-on replay check; helpers are UX on top).
- **Safety-pass opt-out.** v0 always runs the replay check before marking a
  task complete; later add a per-task opt-out for users confident their outer
  level is deterministic.
- **Typed handle keys.** A ``HandleKey[T]``-style wrapper (inspired by wf2's
  ``ServiceHandle[T]``) so task/step code can plumb the expected service
  protocol through ``read_key`` → ``connect`` and get a retyped ServiceRef
  ergonomically; the wire form stays a bare ``FxtrID``.

## Decisions

### 2026-07-04 — chunk 3 rework: strip-set resolved holder-side; wire is positional

Supersedes yesterday's "creation ops carry the holder's vocabulary" (user
review: that shipped name *resolution* to the mount side, breaking the
clean partition). Corrected principle: **only identity-bearing names cross
the wire; all name resolution is holder-side; wire wiring is positional
instructions.**

- ``WiredArg.wiring``: one positional instruction per open axis —
  ``Consume(p)`` (the p-th holder-visible child coordinate, over the
  collision-free concatenation *subtree view's axes ++ new coordinates*) /
  ``KeepName(received)``. (Initially two consume tags, unified on review —
  both compile to a Param of the child frame; the split was a false
  distinction.) The holder computes instructions from its local
  vocabularies (``wire_by_names`` — the strip-set as a pure holder-side
  function); the runner maps p → absolute child position via its true spec
  (the part the holder cannot know: Param-bound prefix positions are
  invisible to it — subtree transparency).
- ``canonicalize_open_task`` consumes ``WiredSpecArg`` (spec + wiring); no
  ``local_names`` on the wire (locals affect only holder-side matching and
  holder-side view naming); creation ops' ``subtree`` back to a bare token;
  the registry's relabel-to-holder-vocabulary step is gone.
- Spec-level axis names are now runner-internal bookkeeping only.
  Corrected on review (no silent dedupe — too magical): ``open_child``
  **rejects duplicate names within one grid's own new axes** (the model's
  per-grid official-uniqueness invariant, which task code can never
  legitimately violate) but **allows overlap across subtree levels** (legal
  per the model: uniqueness is per grid's own axes; prefix collisions are a
  rendering concern). ``check_spec`` no longer requires cross-level
  uniqueness, and the name-based spec rewrites (``rename_axes`` /
  ``bind_consts``) are **deleted** (follow-on review: they were vestiges of
  the pre-chunk-3 era when specs were the holder-facing surface; the
  explicit object with unique, renamable local names is the view runtime's
  Shadow, and no spec operation resolves names). ``bind_keeps`` (positional,
  exact-type coordinate validation) is the only spec-level bind. The
  user-facing *local*-name collision rule stays task-side; the
  false-collision bug class is gone by construction.
- Mode enforcement at the transfer boundary is unchanged (``WiredArg.mode``
  → ``SliceBinding.mode``, validated ≤ grant mode). A subtree ``read_only``
  downgrade binds when the view is *forwarded as an arg* (ownership
  transfer); using your own retained full grant is fine — a downgrade mints
  a new view, it does not mutate authority. The subtree *being opened into*
  is the holder exercising its own capability (bare token; grant mode is
  the authority).
- New canonicalizer validations: wiring arity, out-of-range/duplicated
  consumption targets, duplicate received names (diagonal consumption
  stays inexpressible).

### 2026-07-03 — chunk 3 (view layer): creation ops carry the holder's vocabulary

Promoted from `plans/phase0_walking_skeleton/chunk3_view_layer.md`: wiring
the view runtime to the canonicalizer exposed a wire-model gap — collision
rules, the strip-set, and received names are all defined over the
*holder's local* names, but creation ops shipped only a bare subtree
token, so the runner checked/derived against *official* spec names and a
locally-renamed subtree view false-collided (breaking the documented
"rename your own view first" affordance). Fix: **every ``open_*``'s
``subtree`` argument is a ``WiredArg``** (token + the holder's local axis
names + mode) — mirroring how arg views already worked — and the runner
relabels its true spec to the holder's vocabulary before deriving or
canonicalizing. Runner-side spec names are thereby always
holder-vocabulary at use time; grants still return official names for new
axes with prefix names overlaid task-side. (Also confirms chunk 3's
predictions: shadows are template-free; `view_spec.py`'s canonicalizer
survives unchanged behind `spec_registry.py`.)

### 2026-07-03 — chunk 1 (encoding): hand-rolled tagged payloads, not a codec library

Promoted from `plans/phase0_walking_skeleton/chunk1_encoding.md` (design-
relevant finding during implementation): the model's untagged unions are
**shape-ambiguous** under try-in-order decoding — Data/Handle/Checkpoint/
Subtree configs are all ``axes``-only, and subgrid vs internals path steps
have identical fields — so the planned mashumaro/service-dialect route
would mis-decode them; and since these encodings are *fingerprints*
(identity), they must not depend on codec-library behavior at all. The
canonical payload format is therefore a small explicitly-tagged JSONable
encoding we own (`encoding.py`; array tags for union variants, kind-tagged
configs). `model.py`'s "untagged union, keep X last for codec purposes"
notes were obsoleted accordingly. Refined same day (user review): no
JSON-*string* step — canonical strings exist only for state-db identity
columns (key-paths/coords/parent_path); the entities hold the actual model
object via a field-level ``SerializationStrategy`` producing the tagged
tree, with byte determinism supplied by fxtr's canonical entity CBOR
(sorted map keys — empirically pinned by an insertion-order test). The
"what lives where" map (db columns vs entity-encoded vs never-stored) is
in `encoding.py`'s module docstring.

### 2026-07-03 — delete_grid re-scoped to rmdir-style; `grids.parent_path`

- The walking-skeleton plan was about to defer *correct recursion* in
  ``delete_grid`` while shipping shallow cases — cutting an **invariant**,
  not a feature (user's objection: better to have no feature than a
  spec-nonconformant one). Re-scoped: the v1 *spec* of ``delete_grid`` is
  **rmdir-style** — delete exactly one grid, refusing if any child grids
  exist (delete leaves first) or if a generation inside is currently
  running. Fully conformant with no partially-deleted states; recursive
  delete arrives later as a convenience layered over the primitive.
- Schema: ``grids.parent_path`` (denormalized parent key-path, indexed via
  ``grids_by_parent``). One column serves the delete emptiness check,
  ``list_keys`` presence queries, and future recursive walks via parent
  chains — eliminating JSON-prefix text matching (previously flagged as
  fiddly) from the design entirely.

### 2026-07-03 — storage round 5: public/private identity split; int enums

- Generations get the same two-tier identity as facts (user's suggestion):
  ``task_execs.exec_id`` — a private INTEGER surrogate, the FK target for
  observations / results / checkpoints / responsibility — plus
  ``generation``, the public app-minted uuid (UNIQUE), which remains the
  exported identity. The observations edge table becomes (int, int, tiny
  enum). General rule documented in ``storage.py``: *public identities are
  uuids or location tuples; every db-internal reference is a compact
  private surrogate that never leaves the db* — rowid reuse is harmless
  exactly as long as ids stay inside (FK cascades guarantee no dangling
  internal refs).
- Small closed enums stored as ints with named constants (``OBS_KIND_*``,
  ``ACTOR_*``); ``status`` stays TEXT (five values, one row per cell,
  surfaced raw constantly).

### 2026-07-03 — read-xor-write is op-level; ``read_only`` downgrade added

Found via the generator pattern (create a data grid for writing, write it,
then grant it to child tasks for reading): the "modes are per-grant, so a
task never holds both" framing was too strong — the creator legitimately
holds a write view *and* hands out read views of the same grid — and the
calculus had no way to produce the read grant at all (mode travels with the
view; as sketched the child would have received write mode). Resolution:

- **The real invariant is op-level, per generation, per grid**: a
  generation's op record may not contain both reads and writes of one grid
  (enforced at op time against the record; violation = integrity error →
  errored). Holding both is fine (P5: holding ≠ observing); *performing*
  both is the violation. The observations-PK soundness argument rests on
  this op-level rule (kind is functionally dependent on (generation,
  subject location); grid_config subjects live at the distinct coords=''
  location).
- **Third pure rewrite: ``read_only()``** (write→read downgrade; upgrades
  do not exist). Downgrades accumulate task-side like ``at`` and surface on
  the wire only in ``open_task`` wiring, where the effective mode is
  identity-bearing — ``WiredArg`` gains a ``mode`` field (validated ≤ the
  grant's mode).
- MH generator example updated (``gen.read_only()`` in the rollouts wiring).

### 2026-07-03 — storage round 4: leaner key-paths; per-column DDL docs

- **Stored key-paths drop the step-kind discriminators**: ``grids.key_path``
  is now a plain canonical JSON array of key strings (``["mh","grades",
  "judge_calls"]``). Kinds are derivable — a task parent implies internals
  descent, a subtree parent implies subgrid descent — and the one aliasing
  hazard (a kind-swapping delete-recreate at the same keys) is already
  covered: observers' ``grid_config`` observations fingerprint the config,
  which encodes the kind, so they go stale regardless. Model-level paths
  keep the typed step variants; this is storage encoding only.
- **Every column of every table now carries a docstring-style comment in
  the DDL** (prompted by ``output`` being ambiguous by name: it is part of
  the subject *location* — the observed task-output name, present because
  staleness is per-output — while ``fingerprint`` is the observed value).

### 2026-07-03 — storage round 3: subjects interned as *versions*

Supersedes round 2's subject shape (user's refinement): the interned unit
is a **subject version** — a (location, fingerprint) pair, an *observed
fact* — not a bare location with the fingerprint on the edge.
``subject_versions`` is unique on (location, fingerprint) (one index,
doubling as the reverse-by-location prefix scan); many floating versions
of one location coexist, and which is *current* is determined by what the
location actually holds — so direct staleness is literally "this
generation references a non-current version". ``observations`` becomes a
pure bipartite edge set ``(generation, version_id, kind)`` (kind stays on
the edge: writing vs reading the same value are different relations to
the same fact). Wins: fingerprints stored once per distinct observed
value instead of once per observer (the synchronized fan-in case);
responsibility rows now reference the *caused value*, not just a
location; and a future materialized-staleness bolt-on is just an
``is_current`` flag maintained on version rows. Cost accepted: the
one-entry-per-(generation, location) set invariant moves to app level
(the in-memory record enforces it during runs; the safety pass
re-verifies) — the PK only forbids duplicate edges. Version rows are pure
interning: sweepable when unreferenced, and delete-recreate with
identical values re-interns to the same version (observers resurrect).
`storage.py` + tests updated (incl. a floating-versions test).

### 2026-07-03 — storage round 2: bindings gone, subjects interned; `storage.py` written

- **Checkpoint ownership records dropped entirely** (beyond round 1's table
  dissolution): ``model.CheckpointBinding`` is deleted — ownership is fully
  structural (the internals step in the key-path + the flattened-coords
  prefix), a binding record would never be consulted, and deleting or
  replacing a task grid already cleans up its internals wholesale.
- **Interned observation subjects** (user's idea, symmetric to the
  generation-id normalization): "a thing being observed" gets a surrogate
  ``subject_id`` (``subjects`` table, unique on path+coords+output with
  ``''`` sentinels — sqlite unique indexes treat NULLs as distinct), and
  ``observations`` becomes the tiny join ``(generation, subject_id) →
  (kind, fingerprint)``. Wins exactly the shared-fan-in case (many
  generations observing a few shared config/handle cells) without
  repeating path text. Fingerprint stays on the join row (observers at
  different times see different values); one row per (generation, subject)
  — the keyed-set op record — is the composite PK. Subject rows are pure
  interning: never load-bearing to delete; a recreated grid re-interns to
  the same row, preserving path-identity semantics.
- **`storage.py` written** (docstring = the schema writeup) with the full
  DDL + `create_workflow_state_schema`; `tests/test_storage.py` exercises
  schema creation, one-current-generation-per-cell, turnover-as-one-delete
  (cascades), subject-interning uniqueness, observation set semantics,
  target idempotence, and grid-delete cascades.

### 2026-07-03 — storage round 1: state-db shape

Two files (state db + fxtr entity store) as decided earlier; this round
pins the state db's shape. Durable: grids/configs, cell contents, task
executions (current generation only), per-output results, checkpoints,
observations, responsibility/sourcing, targets, passive event log. Not
durable: view tokens, handle bindings, the intent set (re-demanded after
restart), all derived state (staleness, target attachment, active flags).

- **Identity columns are canonical JSON text** (paths, coords): we own all
  conversions, so the criterion is simplest mental model + easiest to
  implement/test; text keeps identity columns inspectable.
- **Grids are keyed by *key-path*; cells by *full flattened coords*.**
  Configs attach to keys (P1), so the grids table stores coordinate-free
  key-paths (step kind + key), and a cell row carries the whole flattened
  coordinate tuple (prefix + own) as one canonical JSON array — storage is
  literally the sketch's "prepend the subtree's columns" table view; slice
  queries become prefix filters.
- **Generations as PK / FK normalization** (user's idea): ``task_execs``
  has PK ``generation`` (globally unique id) + UNIQUE ``(grid_path,
  coords)``; observations, results, checkpoints, and task responsibility
  rows carry only the generation FK — no repeated observer paths — and
  ``ON DELETE CASCADE`` makes generation turnover a single row delete
  (the atomic old-state cleanup falls out of the schema). Subjects remain
  path-keyed (semantic: delete-recreate with identical values resurrects
  observers).
- **checkpoint_bindings table dissolved**: ownership is structural — a
  checkpoint grid's key-path passes through the owning task grid's
  internals step, and the owning *cell* is the flattened-coords prefix up
  to that step. ``model.CheckpointBinding`` survives as invariant
  documentation only.
- **One observations table, kind-discriminated** (not per-kind tables):
  both hot queries want exactly one index each — record-check on
  ``(generation, subject)`` and staleness reversal on ``(subject_grid,
  subject_coords)`` — and splits would force UNIONs. Per-kind nullable
  contract: ``grid_config`` = no coords/output; ``wrote_cell`` = coords
  only; ``cell_value`` = coords + output iff the subject is a task grid.
- **Staleness (and target attachment / active flags) derived on demand**
  (recursive CTE) in v0; materialization via dirty-set propagation over
  the reverse index is a bolt-on later if overhead shows.
- **Entity-encode the blobby things** (uniform rule): anything the state
  db references that can itself reference entities is stored as an entity
  id — checkpoint args (pins their closures automatically, which a blob
  would silently not do), target slice templates, configs.
- **Retention = pin-per-recorded-id**: fxtr2 sqlite-store pins are the GC
  roots, so the liveness invariant is enforced by the store — pin the
  closure (pin key = the id, idempotent), then record the id. (User note:
  may later clean up entity blob storage to avoid the pin-by-id dance;
  fine for v0.)
- **Access layer**: reuse fxtr2's connection-pool utils; WAL;
  single-writer; one transaction per effectful op. During a run the
  record-check runs against the generation's in-memory record — db rows
  exist for crash durability, cross-generation staleness queries, and UI.
- **Deferred/futures**: gather-heavy observation volume is fine v0
  (batched inserts; in-memory record); escape hatch if needed = a
  rectangle-compressed observation kind (range subject + combined
  fingerprint) — schema-compatible later. Parquet/ducklake: not the source
  of truth (hot paths are OLTP; parquet is immutable-batch), but a natural
  *analytical export* format for version-log commit snapshots
  (provenance-graph analysis via duckdb/polars) — future direction.

### 2026-07-03 — top-level API round 4: one capability; the wrapper grants nothing

Corrects round 3's framing (parts superseded). The three things tracked
separately: the task capability set (``RunCapabilityProto``), the user
capability set (``UserCapabilityProto``), and an *unprivileged* convenience
wrapper. Resolutions:

- **Nothing is "deliberately not on the wire".** fxtr2 blurs in-process vs
  remote by design (a workflow service on a cluster, driven from a
  notebook, is a primary use case). There is exactly one user capability —
  ``UserCapabilityProto`` — and everything a user can do is doable through
  a resolved ``ServiceRef[UserCapabilityProto]``.
- **``bind_handle`` is on the protocol** (moved from the wrapper):
  connectors are ServiceRefs and register fine over RPC. Bindings remain
  ephemeral/non-durable, but ephemerality is about durability, not
  location; a binding's lifetime is bounded by its connector service's
  lifetime (fxtr2 scopes/leases — a dropped/revoked connector unbinds
  naturally). Task-fn registration is the same mechanism (the connector
  produces the ``WorkflowTaskFnProto`` service).
- **Session lifecycle is deployment, not capability**: constructing a local
  runner+capability vs resolving a remote ref are two ways of *obtaining*
  the capability; neither belongs on it.
- **``Workflow`` is pure glue**: holds one capability (local impl or remote
  proxy — indistinguishable), forwards everything, adds no capability, and
  may run on a different machine than the capability's mount source.
- **View-class split re-justified as UX, not security** (correcting round
  3's "privilege can't leak via views" rationale — views are unprivileged
  forwarders and the wire model already prevents capability transfer
  through wiring; the backend capability is the sole gate). Privileged ops
  stay off the shared view classes so the task-facing view surface is
  exactly the task contract and top-level-only affordances fail at the
  method level rather than poisoning at runtime inside a task. Explicitly
  revisable independently of the wire surface (e.g. ``overwrite=`` flags on
  views erroring in task context would also be acceptable).
- **``bind_handle`` returns a release capability** (follow-on, same day),
  modeled on the workstation's ``register_service`` /
  ``ReleaseCallbackProto`` pattern (`fxtr_workstation/protocols.py`): hold
  the ref to keep the binding, drop it to auto-release via scope drain,
  call ``release()`` for graceful immediate unbind (idempotent both ways);
  the binding is additionally bounded by the connector service's own
  lifetime. Double-binding a currently-bound handle raises (release
  first); rebinding after release/drop is the promised-stateless
  equivalence assertion. The wrapper will grow an ``expose_service``-style
  async-CM helper (timed graceful release + unconditional bridge drop on
  exit) with the runner.

### 2026-07-03 — top-level API round 3: Workflow ↔ UserCapabilityProto layering

Triggered by noticing neither surface subsumed the other. Resolved:

- ``UserCapabilityProto`` is the *complete remotable wire surface* (calculus
  ops with user semantics + privileged ops). The wrapper splits it into two
  ergonomic homes:
  - *Calculus ops* are exercised through the **shared view classes**
    (`views.py`), constructed over the user capability — ``wf.root`` is an
    ordinary ``SubtreeView``. Same calculus as task code (helper libraries
    manipulating views work in both worlds); op semantics follow the
    backing capability (noted in views.py's module docstring).
  - *Privileged ops* (targets / overwrite / clear / delete / presence
    queries) are **Workflow methods taking views as arguments** — privilege
    lives in the Workflow handle, never in views, so holding or forwarding
    a view cannot leak it, structurally.
- ``Workflow`` additionally owns what is deliberately *not* on the wire:
  session lifecycle (async CM) and runner administration (``bind_handle``)
  — bindings are process configuration of wherever the runner runs, so a
  remote client driving the wire capability rightly has no such method.
- Rejected alternative: privileged ops as user-side view *subclasses*
  (type-accurate but forks all six view classes and puts privilege into
  objects designed to be passed around).

### 2026-07-03 — connect authority: handle-grid reads only (resolves the handles-in-data-grids question)

- **Authority = a ``cell_value`` read whose subject is a handle-grid cell.**
  Ids read from data grids confer no connect authority: a task may move
  handle keys around as data, but connecting with one fails the authority
  check — authority never flows through data. The sanctioned way to hand
  connect-ability onward is P5 capability passing: grant the handle *view*;
  the grantee reads the key itself.
- **Tasks never mint authority**: no task-side handle-grid *creation* (even
  via subtree write access — the one carve-out from write-implies-create)
  and no task writes into handle cells; handle grants reaching tasks are
  read-mode. Amended same day after review: the original "no task-side
  ``open_handle`` at all" overshot — tasks may **attach** read-only handle
  views of *existing* handle grids through granted subtrees (attach mints
  nothing; blocking it would make handle grids the one kind unreachable via
  subtree capability passing). A missing key on such an attach is a
  not-ready dependency (fault → interrupted; user creates it, re-demand). A
  workflow's complete external-authority surface = the handle grids the
  user populated + the bindings registered on the runner — auditable
  without reading any task code.
- **Generator pattern restated (MH example updated)**: a generated usim's
  natural shape is a *config entity* (plain data), not a handle. The
  generator writes configs into a data grid; rollout steps connect to a
  single shared simulator handle read from a common user-created cell and
  pass their config to it at call time (services returning services makes
  the configured session natural; connections still live at most one step).
  Generation produces data that *parameterizes* user-granted authority —
  never authority itself (the same ontology sandboxed code will later need
  for effect declaration).

### 2026-07-03 — top-level API round 2: "targets" named; dangling mechanics; user_api sketched

- **Name: "target"** (over requested / wanted / current / required / goal
  set / in scope / pinned / enabled — "pinned" and "in scope" collide with
  fxtr pins and scopes; "required"/"enabled" carry wrong connotations;
  "target" works as noun and verb with the right build-system association).
  The sketch's derived vocabulary (task-requested / active / unused) gets
  renamed consistently at storage-schema time.
- **Demand-time coupling flag: default off** (a demand is a pure event —
  ``nix build`` doesn't edit your flake); the flag is wrapper-level sugar;
  the wire surface keeps target and materialize fully orthogonal.
- **Dangling-target mechanics** (confirmed + refined): a target is shaped
  like a ``grid_config`` observation *minus observed values* — it pins the
  lookup key (grid path + expected canonical-config fingerprint) and the
  wanted slice (`model.TargetRecord`; root-frame, so literals/keeps only).
  Grid deleted → dangling; incompatible grid at the same key → *still*
  dangling (fingerprint mismatch); dangling targets are preserved (intent,
  not data) and surfaced as incoherent/errored in the UI — never silently
  rebound or dropped.
- **`user_api.py` sketched** (docstring is the writeup):
  ``UserCapabilityProto`` reuses the runner_api wire types — demand-policy
  reads with no observation recording, user-sourced writes with an
  ``overwrite=`` privilege, the same opens plus ``open_handle`` (root handle
  grids are user-created; the task-side calculus deliberately has no
  ``open_handle`` for now), target ops (add/remove/list with attachment
  status), ``clear_cells`` (v0 refuses running cells), recursive
  ``delete_grid``, presence queries (``list_keys`` per subtree — keys are
  subtree-level by regularity; ``list_cells`` over a selection's open
  positions). Registration stays on the runner object. ``Workflow`` wrapper
  skeleton carries the UX affordances (``demand(..., target=False)``,
  ``run_targets()``, ``bind_handle``).

### 2026-07-03 — top-level API round 1: user semantics, request vs materialize

- **The user is a holder outside the deterministic world (confirmed).** Same
  view calculus for familiarity/code reuse, but user ops have no op record,
  no record-check/replay, no poisoning (errors are ordinary interactive
  exceptions), and record no observations — the user never becomes stale.
  What user ops leave behind is *sourcing*: writes/creates mark things
  user-sourced; requests mark slices user-requested. User actions may
  additionally be passively written to the event log, without creating any
  links.
- **Request vs materialize (decoupled).** Two separate notions:
  - *Request* — a durable, declarative statement of intent: "this cell/slice
    is a root of the DAG I want". Part of exportable/shareable state
    (version-log commits capture it; a recipient learns the experiment's
    *intent* and can keep it while clearing data or changing inputs); fully
    reversible with no side effects on data beyond deterministic derived
    propagation (task-requested / active flags). Slice-level only — there is
    no "whole grid" in the mental model (grids are open, non-enumerable).
    Nix analogy: a derivation in your flake.
  - *Materialize* — an ephemeral runner *event*: "compute this now"
    (demand/await). Not durable, not exported (at most event-logged). Nix
    analogy: building the derivation.
  Coupling is opt-in, not default: you can request without running, and
  run/await without requesting; a convenience flag on await may
  mark-as-requested (default under discussion, see Q7). "Run the workflow"
  = an optional UX affordance over the primitives ("demand every
  currently-requested slice"), not core design. v0 is strictly explicit-run.
- **Enumeration = presence queries.** Grids stay unbounded (no "bounds"
  query); the privileged introspection surface is: list a grid's nonempty
  cells with coordinates (+ status/staleness for task cells), and list a
  subtree cell's keys. No data-flow semantics — records nothing, nothing
  downstream can depend on it; the open-map principle stays intact where it
  matters (tasks).
- **Grid deletion is in scope** for the privileged surface (fully deleting
  grids from any subtree, incl. the root) — needed to resolve conflicts and
  make breaking changes to task code. Auto-pruning of unused/orphaned cells
  stays deferred.
- **Runner owns handle registration in v0** (in-memory bound/unbound state).
  **Step poisoning via connect** (new consequence, propagated to
  errors/task_api/runner_api/semantics note): poisoning follows workflow
  *operations*, not levels — ``connect`` is the one workflow op inside
  steps, and its errors poison the *step invocation* at raise time (no
  checkpoint entry, even if step code catches and returns a value), failing
  the task per error class. Unbound handle = ``HandleUnboundError``
  (``CellFaultError`` subtype → interrupted): it *must* poison because
  bindings are untracked runner config — a fallback result computed around
  a missing binding could never be invalidated once the binding appears.
  Connecting a key the generation never read = integrity violation
  (→ errored).
- **Top-level surface is a service protocol**: a user capability sibling of
  ``RunCapabilityProto`` (same wire view model + privileged ops +
  introspection), with the ergonomic in-process python API wrapping it —
  same both-sides pattern as tasks. Enables web UI / MCP / workstation
  driving a workflow remotely.

### 2026-07-03 — embodiment round 4: runner_api review — handles as keys, renames

Review round on the `runner_api.py` sketch; all applied:

- **Handles cross as keys (redesign).** The outer level reads a handle
  cell's *key* — the handle entity id — via ``HandleView.read_key`` /
  ``RunCapabilityProto.read_handle_key``, recording an ordinary
  ``cell_value`` identity observation. The key travels into step args as
  plain data; ``StepContext.connect(key)`` exchanges it for a live service.
  Connect records no observation (the read was the observation) and its
  *authority follows the record*: only keys the current generation has read
  may be connected, so knowing an id grants nothing. The
  ``connected_handle`` observation kind is removed from the model.
  Consequences: the step capability is entirely calculus-free (steps consume
  only plain data — no view ever serializes into checkpoint args), durable
  records never contain view tokens, and therefore **tokens are ephemeral
  per-run wire handles with no cross-run stability requirement**
  (supersedes round 3's token-determinism requirement).
- **``FrameWireType`` alias** (currently ``= FxtrID``, the canonical frame
  entity): the v0 stand-in wire form for dataframe cells/outputs; may become
  a capability or lazy-storage token post-v0 — code must treat it as opaque.
- **Renames for consistency**: ``read_cell_entity`` / ``read_cell_frame`` /
  ``write_cell_entity`` / ``write_cell_frame`` (every one reads/writes a
  *cell*; the suffix is the payload kind); ``open_task``'s ``coordinates``
  parameter is now ``axes`` everywhere (views, view_spec, runner_api, tests,
  MH example) — it always meant the same thing as the other opens' ``axes``.
- **Clarifications recorded in docstrings**: a ``ViewGrant``'s axes are only
  the view's *own* (kept/received) axes — vmap-consumed positions are
  invisible task-side; opens mint fresh grants/tokens (a ``ViewSel`` never
  derives child views — creation is effectful and recorded); ``ViewGrant``
  may later become a per-kind discriminated union (wire-compatible either
  way; flat for now).

### 2026-07-03 — embodiment round 3: wire model sketched (`runner_api.py`)

The task↔runner contracts now exist as a sketch (`runner_api.py`; its module
docstring is the writeup, per the capture plan). Micro-decisions embedded in
it, flagged here for review:

- **Positional wire addressing**: pure rewrites never cross the wire —
  renames are entirely task-side (ops address positions in the grant's axis
  order), and ``at`` bindings accumulate into ``ViewSel.bound``. Names cross
  the wire only where identity-bearing (``open_task`` wiring: coordinate
  names + each ``WiredArg``'s open-position local names, which strip-match
  and promote to received names).
- **Deterministic token minting**: tokens are derived from the derivation
  path (root grants: arg name; internals: reserved; opens: parent token +
  key) so they are stable across a generation's runs — required because
  checkpoint invocations may embed serialized views and are
  replay-compared. Alternative kept in reserve: normalize embedded view
  refs to canonical specs when recording invocations.
- **``run_task`` returns ``Mapping[str, FxtrID]``**: the task-side adapter
  canonicalizes ``Table`` outputs into frame entities; closures ride the
  return arena; the runner persists before recording (liveness invariant).
- **``read_batch`` as the gather substrate**: gather is task-side sugar over
  one batched wire call (same per-cell observations; lets the runner batch
  demands for fan-out scheduling).
- **Frame reads return the canonical frame entity id** (not inline rows) —
  uniform with the record-serving story (the fingerprint *is* the return).
- Protocols are plain ``typing.Protocol`` constrained to the service-dialect
  vocabulary; ``@service_protocol`` decoration lands with the runner (the
  option-A boundary is the design commitment; decoration is mechanics).

### 2026-07-03 — execution semantics round 3: interrupted vs errored; step identity settled

- **Failed-state split: ``errored`` vs ``interrupted``.** Poisoning has two
  flavors, split by whose fault it is:
  - ``errored`` — the task itself went wrong: its own logic raised, an error
    escaped a step, or it violated integrity (write conflict, config
    mismatch, view misuse, nondeterminism).
  - ``interrupted`` — the task wanted something that isn't ready: a
    dependency fault (``CellFaultError``: subject missing / errored / stale —
    incl. pending upstreams under a future no-auto-demand policy), or the
    run was cut off (crash/shutdown; a ``running`` status found at runner
    startup is flipped to ``interrupted``). Conceptually "pending, but with
    progress attempted and a recorded reason".
  Both fault consumers identically (the freshness invariant covers errored,
  interrupted, and stale alike), but they surface differently at the top
  level: cascades render as one root cause plus a tree of interrupted
  dependents, not errors all the way down. The split falls exactly along the
  existing error taxonomy: ``CellFaultError`` → interrupted; everything else
  (WriteConflict / GridConfigMismatch / ViewAxis / Nondeterminism /
  task-raised) → errored.
  - Demand policy gains a row: *interrupted + up-to-date* → resume by default
    (an ordinary run of the same generation: replay hits to the frontier,
    retry the blocked op live — succeeds if the blockage resolved, faults
    again cheaply if not). No special-casing: a fault records no
    observation, so the blocked op simply sits at the frontier; if the task
    had already *observed* the now-changed input it is stale and the stale
    rule applies instead.
- **"Attempt" = "run".** Wording fix (in the semantics note): an attempt is
  just a run that tries to carry the generation forward; the executor has no
  attempt-specific state or resume mode.
- **Step identity settled (closes the step-identity open question)**: the
  recorded step-fn identity is the step service's fxtr *service type id* —
  user-controllable (a registrant can point it at an entity encoding the
  step name and any other identity info), and the type id is already the
  canonical place that describes what a service is. Recorded invocation =
  (step type id, args).

### 2026-07-03 — fxtr2 embodiment round 2: opaque views, steps as ServiceRefs, capture plan

- **Opaque task-side views (confirmed).** Absolute spec paths never cross the
  service boundary. A task-side view is an opaque token + a local shadow
  (axis names/kinds, mode, schema/output info) sufficient for local
  validation and pure rewrites; effectful ops ship ``(token, accumulated
  rewrite state, op args)`` and the runner replays the rewrites against the
  true spec it kept. "A task cannot tell which subtree it runs in" becomes
  structural rather than honor-system. `view_spec.py` is a prototype and may
  be rewritten/split to serve this.
- **Steps as ServiceRefs (confirmed).** ``run_step`` goes task → runner
  (record-check); on a miss the *runner* invokes the step ServiceRef with the
  step capability as an ordinary service call. Chosen over a two-phase
  begin/commit protocol because: step-capability lifetime falls out of
  service scoping automatically; replayed and fresh results reach the task
  through the same return path (uniform placement/lifetime); and the runner
  stays in control of persistence (result lands runner-side via the
  return-arena path, is persisted, *then* served — no trusted commit call,
  no in-progress checkpoint state). Accepted cost: step args round-trip
  task→runner→task. Plain task-side ``StepFn``s are wrapped/mounted by the
  task-side adapter (the ``StepFn | ServiceRef`` union in ``run_step`` is
  sugar | the real thing). Step *identity* for the recorded invocation is
  still open — see open question 4.
- **Arg descriptions are pushed (confirmed).** The task-fn invocation carries
  the arg-view descriptions (static per grid, derived from the rule); the
  task-side shim builds views from description + capability ref, calls the
  user fn, normalizes the return into per-output values, and returns them
  over the standard return-arena path.
- **wf2 carry-over scoped down.** The *task-facing* surface keeps only the
  ``ServiceHandle[T]`` phantom-typing idea and "hand a handle view to
  ``StepContext.connect``, get a ServiceRef back". The concrete provider
  machinery (``ExternalServiceHandle`` / spawner / connector registration —
  possibly owned by the runner object itself rather than a separate spawner)
  is deliberately deferred to the top-level user-API conversation: it is part
  of "how you manipulate the system at the top level", not how tasks work.
- **Design capture plan.** Execution semantics get a standing self-contained
  note — `notes/execution_semantics.md`, the dynamic sibling of
  `view_spec.py`'s static exposition. The fxtr2 service layer is captured
  directly as python interface sketches whose docstrings are the writeup
  (next: `runner_api.py`).

### 2026-07-03 — fxtr2 embodiment round 1: scopes, capability shape, service boundary, storage

- **Scope tree** (three levels, mirroring the three lifetimes): a long-lived
  **runner scope** (the fxtr entity store's namespace scope attached as a
  parent, so any id recorded in the state db is loadable below; external
  handle bindings mounted here) → a **per-run scope** (one per *run* of a task
  execution — per run, not per generation: scopes are cheap and
  re-instantiated on every re-invocation, and one generation spans several
  runs under universal record-check; nothing in-memory is ever load-bearing
  across runs, which is also what makes evicting blocked executions safe) →
  a **per-step scope** (child of the run scope, lives for one ``run_step``;
  a ``connect``ed ServiceRef is bounded by it structurally).
- **Entity-liveness invariant**: an FxtrID may be recorded in the state db
  (cell write, checkpoint value, task result, config fingerprint) only *after*
  its closure is persisted into the entity store. A crash between the two
  leaves only a harmless unreferenced entity, never a dangling id.
- **Storage layout**: two sqlite files side by side — the workflow state db
  (custom schema) and the fxtr entity store. v0 retains everything; real
  pinning/GC arrives with the version log (commits are natural pin roots).
- **Capability embodiment (tentative).** Views are plain serializable values,
  not individually-mounted services; every effectful op proxies through one
  mounted **run capability** per run, which performs the record-check,
  observation recording, demand/policy logic, and state-db writes. Wrappers on
  both sides: to the runner, a task is just a service that takes the root run
  capability; to the task, the user fn receives abstracted views/context.
  Steps get a separate, narrower step capability (connect/log/channel).
  Marked tentative — revisit if it bites.
- **Task fns sit behind a ServiceRef boundary in v0 (hard requirement).** The
  embodiment is not "done" until the task fn is abstracted behind a
  ServiceRef and can in principle be invoked over RPC; nothing may be
  implemented in a way that forecloses this.
- **Model-type split (was Q3, resolved)**: grid configs stay plain dataclasses
  in memory with a *canonical entity encoding* (used for ``grid_config``
  observation fingerprints and later by version-log commits); linkage/state
  records are sqlite-only, never entities.

### 2026-07-03 — execution semantics round 2: universal record-check, strict by default

- **Universal record-check.** There is no separate live/replay or
  checked/unchecked mode: *every* effectful op, on every run, consults the
  generation's record first — a hit serves the recorded outcome, a miss goes
  live and records. Consequences: "attempt" is a non-concept in the executor
  (resume-after-crash is just a run whose early ops all hit), and reads get
  per-subject coherence within a generation (two reads of one cell can never
  return different values, so a concurrent external edit cannot fabricate a
  later replay-check failure).
- **Hit-mismatch rule.** Ops that carry a request payload (writes: the value;
  opens: the config; steps: the invocation) and hit a record with a
  *different* payload raise NondeterminismError — conflicting with your own
  record is nondeterminism (writing one cell twice with different values is
  caught immediately rather than surfacing as a confusing self-write-
  conflict). A first-time (record-miss) write that conflicts with a live
  cell's existing different value remains an ordinary WriteConflictError.
- **Hits don't re-demand.** Serving a recorded read of a task output does not
  re-demand the subject. The subject may meanwhile be errored or stale and the
  recorded value is served anyway — it wasn't stale when observed, the demand
  relationship is already recorded, and the never-observe-stale invariant
  governs only *new* observations; the staleness machinery covers the rest.
- **Safety pass: strict by default in v0.** The pass = re-run the outer level
  requiring (i) every op is a hit (the frontier is never reached), (ii) the
  visited op set equals the recorded set, and (iii) the returned outputs equal
  the recorded results. Pure compute, no live effects. v0 always runs it
  before marking a task complete (build a feel for the system; the machinery
  is already there); a per-task opt-out for known-deterministic tasks comes
  later (see Followups).
- Record-sufficiency note: serving hits needs nothing beyond what staleness
  already records — ``cell_value`` fingerprints *are* the returned ids (frame
  reads load the frame entity from the store), ``grid_config`` serves the
  stored config, ``wrote_cell`` serves a no-op, steps serve checkpoint values.

### 2026-07-03 — execution semantics round 1: replay-from-record + demand policy

- **Unified replay-from-record.** Crash-attempt re-runs (and the strict safety
  pass) replay effectful ops from the generation's recorded op set rather than
  re-executing them live: a hit (an already-recorded subject) serves the
  recorded observation/result without touching live state; past the record's
  frontier, ops go live and record. No sequential log — the same
  keyed-by-subject observation/checkpoint records used for staleness double as
  the replay record, so this is nearly free. Consequences: a resumed execution
  behaves as if it did everything pre-crash, waited, then continued at the
  resume point; if an already-observed input changed in between, the execution
  stays *coherent* (it does not see the change), completes normally, and is
  simply stale — and if the input change is later reverted, the result can
  become up-to-date again instead of being permanently diverged. This
  supersedes the earlier "live second run" framing of the safety check, which
  was unsound under legal data-dependent reads + no snapshot isolation (a live
  re-run could legitimately diverge or perform new effectful ops).
- **Staleness applies to crashed generations** exactly like completed ones: a
  crashed generation whose observed inputs changed is stale, and retrying it
  falls under the stale-cell policy below (v0: clear + rerun) rather than
  resuming.
- **Demand policy (v0 defaults; per-situation user configurability intended).**
  On observing/demanding a task cell:
  - *errored + up-to-date*: fault or retry per policy (default retry — a new
    attempt of the same generation, replaying the record); always fault if the
    retry fails again.
  - *not yet run*: run it; use the result if it completes, fault if it errors.
  - *stale (completed or crashed)*: fault immediately and ask the user to
    clear the stale state — v0 never silently discards a generation's state.
  - **Key invariant**: a new task execution never observes an errored or stale
    cell — it either blocks until the value is ready or faults and must be
    retried. (No input version skew: global correctness would require
    re-running the stale input anyway.)
  - Eventual shape: a user-supplied execution policy consulted in each of
    these situations, possibly interactive ("clear and rerun these X stale
    cells?").
- **Runner concerns.** Demand cycles: rare; acceptable v0 behavior is to block
  until user interrupt, optionally with a background checker surfacing an
  informative message. Fan-out concurrency: keep (a) an intent set of cells to
  run and (b) a smaller actively-running set, feeding (b) from (a) in batches;
  because outer levels are idempotent + replayable, long-blocked executions
  may be evicted and resumed later via replay.
- **Cross-generation grid evolution (resolves the 2026-07-03 open question).**
  When a changed task fn's new generation opens previously-created child keys
  with different configs: children of the same (now-changed) fn are typically
  stale anyway, so the staleness clear-flow covers them; a genuinely different
  grid at the same key is an ordinary conflict. v0 answer: clear error +
  manual pruning.

### 2026-07-03 — indexers dissolved into tasks + dataframe cells (confirmed)

Round-2 direction confirmed and implemented (model/views/view_spec + tests +
MH example reworked):

- **Checkers are ordinary task grids** (read input → return unchanged or
  error; downstream explicitly points at validated vs raw). No attachment, no
  retraction, no effective fingerprints. Confirmed rationale on retries: a
  task *completing with* an invalid value is the task asserting the error is
  representative/visible (e.g. a refusal) — the failing party should be the
  downstream reader; re-sampling is an in-task step-retry loop or explicit
  user invalidation. A task that got an invalid result it *wasn't* expecting
  should raise normally, which already retries via re-demand.
- **Named task outputs (full generality)**: `TaskGridConfig.outputs:
  {name: EntityOutput | FrameOutput(schema)}`; every complete execution fills
  all of them. Single-output conveniences (keyed on *exactly one declared
  output*, not on any magic name): `outputs` defaults to
  `DEFAULT_TASK_OUTPUTS` (`{"result": EntityOutput()}`, normalized at
  canonicalization so implicit == explicit); a single-output task fn may
  return the bare value (wrapped under that output's name); no-arg
  `TaskView.output()` selects the sole output, resolved to the concrete name
  at selection time (specs/stored rules always carry explicit names — an
  implicit stored form would spuriously regularity-conflict with an explicit
  one). Name "result" kept for the default: reads as what it is; `TaskCellExecution.results` is per-output; slices select
  outputs via `SliceTemplate.output` (no path step; view-side
  `TaskView.output(name)`, spec-side `select_output`); observations carry
  `subject_output`, so staleness is per-output. "Indexers" = tasks with
  dataframe outputs that happen not to checkpoint/connect — no special type,
  purity is a trust contract (they can never receive handles).
- **Dataframe cells**: row *multisets* with config-declared schemas
  (`ColumnSpec`/`FrameSchema`; `entity` columns hold FxtrIDs and pin their
  referents). Canonical identity: encoded as a sorted-rows entity, so cells
  stay FxtrIDs and fingerprints / conflicts (multiset equality) / replay /
  complete-subgraph all work unchanged; any per-grid sqlite materialization
  is a query-speed projection (possibly stored alongside the entity copy —
  perf question, deferred). `DataFrameGridConfig` joins the config union
  (symmetry: task entity-output ↔ entity data grid; frame-output ↔ dataframe
  data grid; tasks may write to either). Domain cells are zero-axis
  dataframe cells — enumeration is native.
- **Tables + gather instead of an SQL engine (v0)**: `Table` is an immutable
  multiset value (serializable, e.g. into step args) — kept **abstract** (ABC:
  `schema` + `to_rows()`): the semantic contract is schema + row multiset
  only; the runtime representation (tuples / arrow / dataframe object, maybe
  lazy later) is deliberately unpinned. The one new
  effectful primitive is **gather** — bind table columns to a view's axes,
  extend each row with the addressed cell's entity (`DataView.gather`, new
  column) or rows (`DataFrameView.gather`, join), *faulting* on
  missing/errored cells; empty frame cells contribute zero rows without
  fault. Gathers observe exactly the addressed cells; materialized tables
  are plain observed data, so local polars/duckdb is ordinary outer-level
  pure computation. No in-system SQL in v0.
- WARNING/SKIP/ERROR statuses are gone: SKIP = empty frame, ERROR = errored
  cell, warnings = an ordinary advisory dataframe output surfaced by viz
  (see the MH example's grade task).

### 2026-07-02 — canonicalizer implemented (`view_spec.py`)

P2 is now executable. `ViewSpec` is the runtime-internal symbolic state of a
view (absolute template path + per-position templates + local axis names,
relative to a **holder frame**); pure functions implement the rewrites
(`rename_axes`, `bind_consts`), child derivation (`open_child`), and
`canonicalize_open_task` (view wiring → stored key-level `TaskGridConfig` +
the returned task-view spec). Notes from implementation:

- The creation-path correspondence is read off the subtree view's own
  templates: prefix position `q`'s template says how the holder addresses it
  (`Param(i)` → holder axis `i` ↔ child prefix `q`; `KeepAxis` → strip-set by
  the subtree view's local name; `LiteralCoord` → no correspondence).
- Uncorrelated holder Params literalize via `holder_coords` (the executing
  cell's concrete coordinates — the only cell-dependent input; tests verify
  correlated wiring yields identical configs across cells and uncorrelated
  wiring yields differing ones).
- **Subtree full-frame invariant** (decided 2026-07-02, superseding the
  earlier diagonal analysis): a subtree view is a *creation site*, so it must
  always be full-frame — every flattened axis of the target is either a named
  Keep or bound to a **distinct** holder axis; never a literal (sliced),
  never a duplicated Param (diagonal). Enforced by
  `check_subtree_full_frame` at every open, plus: `at`/`bind_consts` is
  unavailable on subtree views (slice the leaf instead — subtree views have
  no read ops anyway), and forwarding a subtree view whose bound holder axis
  the creation site does not correlate is rejected at create time (it would
  hand the child a sliced-by-literal creation site and guarantee a regularity
  conflict at the second creating cell). With the invariant, the
  creation-path correspondence is total and injective — canonicalization
  never guesses. Rationale for banning slice-creation outright: key-level
  configs make creating into a slice a silent broadcast across the removed
  axis. The one sanctioned way to remove a subtree axis remains vmap
  consumption by a created task's coordinates, where the created grid takes
  responsibility for a coherent config across it.
- **Uniform batch-invariance rejection** (decided 2026-07-02, tightening the
  above): forwarding *any* view — leaf or subtree — bound to an **unmapped**
  holder axis is rejected at create time. Definition (in `view_spec.py`'s
  module docstring): the creation path *maps* holder axis `i` to child prefix
  position `q` when the subtree view's template at `q` is `Param(i)` — i.e.
  which subtree cell the holder creates into varies along axis `i`, tracked
  one-to-one by child coordinate `q`; an axis appearing in none of the
  subtree view's templates is *unmapped* (all holder cells along it create
  into the same subtree cells). Rationale:
  since every cell of a task grid runs the same code, literalizing such a
  binding is *always* a delayed, guaranteed regularity conflict for any axis
  with ≥2 populated cells — it implicitly collapses a whole grid of distinct
  configs into one smaller-dimensional key. (JAX analogy: declaring an output
  batch-invariant when it varies with the batch dim is rejected, not averaged
  away — here the created grid is the "out parameter".) Consequences:
  literalization is gone from the canonicalizer entirely; it takes **no
  per-cell input** (`holder_coords` removed), so cell-independence of
  canonicalization holds by construction; stored configs contain literals
  only where creating code wrote them explicitly (`at(...)`), which is
  cell-independent and safe. Tasks can still freely *read* inputs bound to
  any of their axes — per-cell reads are what observation tracking handles.
- Diagonals also
  remain inexpressible by the rewrite calculus itself (view axis names are
  unique, and translate preserves Param-distinctness inductively); if an
  einsum-style ``diag`` op is ever wanted, it now conflicts with this
  invariant and would need its own design.
- Tests (`tests/test_view_spec.py`) recreate the MH example flat + nested,
  incl. official/local coordinate renaming and task_fn-must-be-consumed.
- `views.py` objects will wrap specs and delegate (not yet wired — comes with
  the runner).

### 2026-07-02 — error contract + task/step signatures (code-review round)

- **Error-poisoning contract** (documented in `errors.py` + `task_api.py`):
  catching errors is only meaningful *inside* step functions. Any workflow
  error raised by an outer-level operation, and any error propagating out of
  a step fn, is persisted to the execution log at raise time and forces the
  task into ``errored`` — even if task code catches it and returns a result.
  Rationale: a swallowed error could silently persist a condition that would
  not replay cleanly; also lets the UI surface the specific underlying
  error(s). Steps may freely catch their *own* internal errors (workflow
  errors cannot occur there — no outer access). All error types moved to
  `errors.py`; `ViewAxisError` is a `WorkflowError` and poisons too.
- **Task/step fns receive their args as keyword arguments** with ``ctx``
  positional-only: ``TaskFn.run(ctx, /, **args: GridView)``,
  ``StepFn.run(ctx, /, **args: FxtrServiceData)``. ``TaskContext.arg()`` is
  gone; the context carries only non-arg surface (``own_internals``, later
  introspection/logging). ``run_step`` takes *both* ``args`` and ``coords``
  as explicit keyword-only mappings — no ``**kwargs`` slot at all, since with
  two name-keyed mappings in play letting either claim it invites confusion;
  ``args`` is delivered as the step's kwargs.
- **Checkpoint replay is strict**: a current-generation entry's recorded
  invocation (step fn identity + args) must match the replay's, else
  `NondeterminismError` (invocations are small; the cost is a canonical
  serialize + compare, and silent arg drift is exactly the corruption we're
  guarding against).
- **`StepContext.connect` returns `ServiceRef[OverrideThisTypeBeforeUsing]`**
  (caller substitutes the expected protocol via `replace_service_type`).
- **`StepContext.open_channel() -> FxtrChannelWriter`** added to the API to
  track the requirement (structured output channels for steps); plumbing
  deferred — string `log()` is the first implemented path.

### 2026-07-02 — capability-view rewrite calculus (replaces slice drafts)

Triggered by asking where the names in a draft actually bind: drafts contained
axis names of *other* grids with no defined name authority (foreign configs'
names would leak into task code). Resolved by a bigger, cleaner reframing:

- **Names are views of hidden positional dimensions.** User code *only and
  always* addresses by local axis names; backend storage *only and always*
  addresses positionally, remembering name→position translations at interface
  points. Every grid slice capability carries unique local axis names
  (uniqueness required within one view only; distinct views reuse freely).
- **Observability principle for identity**: config identity is exactly what
  task logic can observe. A grid's own axis names and each binding's
  *received axis names* (the vocabulary the task fn is handed — the creator's
  local names at creation time, promoted to official) are identity-bearing
  and participate in config equality; names never participate in addressing.
  `model.SliceBinding` gained `received_axis_names`.
- **The construction API is a calculus of capability rewrites**, not a slice
  data language (`views.py` replaces `draft.py`): views support pure local
  rewrites — `rename_axes` (name→name), `at` (bind constants) — plus
  effectful ops (read/write/demand/open/run_step). Rewrites are free,
  unrecorded, and never change stored identity; only effectful ops enter the
  replayed op set. View specs are symbolic → regularity by construction.
- **`open_task` wiring is name-matching with a strip-set**: caller supplies
  `coordinates` (official stored names), optional `local_names` (official →
  local for this context; must not collide with the subtree view's axes),
  `task_fn` (a handle view that must be *fully consumed* by the strip-set),
  and `args` (views; strip-set axes vmap-consume, all remaining local names
  become the child's official received names). Strip-set = subtree view's
  axes + local coordinate names. Returned task view axes = subtree view's
  axes + local coordinate names. Access mode travels with each arg view (no
  separate modes argument). The official/local coordinate rename restores
  full naming flexibility (a task grid's coordinate names may even reuse
  arg-axis names via renaming).
- Nested-subtree transparency falls out with zero syntax: an arg subtree view
  whose axis is consumed by a coordinate hands each child cell a
  smaller-dimensional (e.g. zero-axis) workspace.
- **The string DSL is dropped** (superseded note in `notes/slice_dsl.md`):
  method chaining *is* the calculus; the notation may return as display
  syntax for stored rules.
- **Rewrite ops aim to be purely local**; how task code adapts onto fxtr2
  services / RPC (the user-written task fn need not be the mounted
  ServiceRef; a promise-pipelining convenience may sit in between) is
  deliberately deferred until the user-facing API is settled. `task_api.py`
  is now plain `typing.Protocol` contracts (TaskContext/TaskFn/StepContext/
  StepFn) over views, no `@service_protocol`.

### 2026-07-02 — positional axis addressing + draft/canonical type split

- **Positional axis addressing (binder/lambda model).** Name-based flattened
  axis addressing was unsound: uniqueness would have to hold across the child's
  own axes plus every enclosing subtree's axes, and subtree-transparency means
  the draft author cannot know the prefix names, so collisions were unavoidable
  in principle. Instead: a grid's full coordinate space is an ordered tuple of
  axis *positions* (path order — enclosing subtrees' own axes, then the grid's
  own); a slice is a function from binder parameters to a location expression;
  kept axes = partial application (the residual positions are the handed-out
  capability's coordinate space). Names in `AxisSpec` are non-semantic,
  binder-local shorthand + rendering metadata (unique only within one grid's
  own axes; prefix collisions are a rendering concern). Config equality
  (regularity) is α-invariant; the P2 correlation map is a pure index map.
  User-facing API coords stay name-keyed against each capability's local names.
- **Flattened axis list is derived, not stored** (normalized): configs store
  own axes only; interpreting `Param(i)` walks the path and concatenates.
  Configs are fixed at creation so a denormalized cached copy would be safe —
  deferred as a debugging/UI convenience.
- **Draft vs canonical are separate types in separate files**: `draft.py`
  (user-facing, name-based, capability-relative; `BoundAxis` resolved against
  the draft's `axes` binder) vs `model.py` (canonical, positional,
  root-absolute; `Param(position)`). The runtime canonicalizes at creation and
  matches idempotent re-opens on the canonical form.
- **Kept axes are allowed anywhere in the flattened space**, including path
  steps — a slice is an arbitrary partial application over the target's
  flattened axes (e.g. `@variants{variant=*}/agg`: one child key spanning all
  subtree cells). Sound because regularity gives every cell the same config for
  a key; also gives finer-grained grants than handing over the whole subtree
  slice. Single-cell-ness is a per-use validation (`is_single_cell()`, now
  covering step cells too), required only where semantics demand it (task_fn).
- **Slice string DSL**: sketched in `notes/slice_dsl.md`
  (`@workspace/grades[subject=$subject, judge=*]`; bidirectional
  parse/stringify with the draft dataclasses as parse target; mode stays
  structured). Implementation deferred until the canonicalizer exists.

### 2026-07-02 — rule canonicalization and paths (P1–P5 review)

Worked through `notes/example_mh_pilot.md`; P1–P5 adopted (full statements there):

- **P1**: stored configs attach to subtree *keys* (one config per key, the
  regularity invariant as storage format) and rules are expressed over
  *flattened* axes — the enclosing subtree's axes stacked before the grid's own;
  stored bindings may reference prefix axes.
- **P2**: canonicalization is a metadata-level *symbolic* composition of the
  creator's rule binding for the root capability with the draft's further
  slicing; creator axes normalize into the child's stacked prefix axes via the
  creation-path correspondence, literalize otherwise. Never value-matching
  against the instantiated capability; per-cell values never enter. Task code
  never references its own axes/coordinates (pure vmap-style).
- **P3**: drafts must be symbolic (input names, child axes, literals); anything
  per-cell-varying in a draft is structurally a regularity conflict. Prefix-axis
  bindings arise *only* through composition — a task cannot explicitly bind "the
  subtree cell I'm running in".
- **P4**: adopted semantically; represented as a tagged path-step variant
  (`TaskInternalsStep(cell, key)`), not a literal reserved string key (no
  user-key collisions). The step carries a *key* — a task grid literally
  doubles as a subtree grid, with a per-key internals config map parallel to a
  subtree's, so no degenerate zero-axis intermediate grid exists in paths.
  Consequence: the bare internals root is not path-addressable and thus can
  never be a slice target — sharing space out of internals means creating a
  named subtree inside it and granting that. There is no `$out` — demanding
  the task cell itself *is* the output. Mostly implementation detail: the
  user-level surface is just `own_subtree()` (a zero-axis subtree *view*;
  canonicalization fuses its first step) + opening checkpoint grids there.
- **P5**: rule inputs are grantable capabilities; holding ≠ observing. Only
  reads / demands / connects / writes record observations.
- **Broadcast coupling consequence** (confirmed correct): granting a subtree to
  a task grid with an extra axis uncorrelated to the subtree forces creates to
  be identical across that axis (same key-level config), and divergent writes
  across it are ordinary conflicts — same as two task cells running different
  things into one subtree cell.
- **Write access = create access** on subtrees, and implies write/create access
  to everything transitively inside. Starting point; revisit if it bites.
- **Task-fn observation**: every executing task cell reads its task_fn handle
  cell at execution time (a `cell_value` observation — this is where
  `executed_fn` identity comes from). A task-fn handle threaded as a plain
  *argument* is observed by whichever cell ultimately resolves/uses it: a child
  grid created with it as executor observes it at the child's execution; a
  parent calling it manually from a checkpointed step observes it directly.
- **Checkpoint-grid opening**: `open_checkpoint` lives on the subtree capability
  and only succeeds within the owning task's internals subtree (no dedicated
  task-context method).

### 2026-07-02 — initial design discussion

- **Open-map principle (answers old Q1).** The set of populated cells of a grid is
  _not_ an observable fact. No operation may ask "what are all the facts"; adding a
  new fact must never change an old result — only modifying or removing facts can.
  Expected index sets come from **domain cells**: zero-coordinate data cells whose
  scalar value is the list of valid values for a semantic axis. Reading "all of axis
  ix for category foo" means joining against an explicit domain cell; expanding the
  domain is an explicit write to that cell, which staleness-propagates. Domain cells
  are a usage pattern/convention, not a schema feature — grids do not know which
  domain governs them. Consequently: a "whole axis" slice handed to a task is not
  enumerable, only readable at specific coordinates; SQL-over-slice always takes an
  explicitly enumerated coordinate set. Observation granularity bottoms out at
  per-cell values (incl. domain-cell values).
- **Staleness × execution state are two axes (old Q2).** A task cell tracks
  staleness (is this result a current sample?) and execution state (none / running /
  complete / errored) independently. Vocabulary: a **generation** is one logical
  execution identity (new generation on staleness-invalidated or explicit rerun);
  **attempts** are crash-retries within a generation and share its checkpoints.
  Old-generation state (checkpoints, observations) is atomically deleted before a
  new generation starts (single-db transaction). An explicit generation id exists
  in storage (needed for version-log coherence and debug metadata) even though the
  API mostly hides it. A running generation whose inputs change is simply both
  running and stale; v0 lets it finish (see followups for step-boundary
  cancellation). Known accepted property: no snapshot isolation — a task's reads
  happen over time, so it can observe a torn mix of pre- and post-change values;
  it ends up stale either way, staleness is the safety net.
- **Rule DSL is axis plumbing, not correctness (old Q3).** No conditional or
  computed rules. The rule DSL (vmap-like: bind a task-grid coordinate to an input
  axis, or pass a whole axis through) permutes/binds axes into the shape the task
  code wants and grants access to input grids; **staleness tracking depends on what
  the task actually read at runtime**, not on the declared slices. Tasks can emulate
  conditional reads by reading a smaller slice. Task grids are demand-driven: any
  coordinates may be demanded; missing inputs fault at read time.
- **Determinism enforcement (old Q4).** Set-based (not sequence-based) replay
  matching, keyed by path/coordinates — outer-level code may use ordinary anyio
  concurrency. Enforcement strategy: **strong enforcement via the replay/safety
  check** (which must catch everything, since context tracking does not naturally
  cross RPC boundaries and RPC-proxied services can be task handles), plus a
  best-effort helper base class for step implementers that catches footguns early.
- **Caching is position-addressed (old Q5).** Results live at grid coordinates with
  staleness links; no input-addressed cache keys (deliberate break from v1 and
  fxtr_workflow_2). Identical invocations at different cells do not share results;
  re-execution is via invalidation.
- **Handle trust boundary (old Q9).** Observation of a handle observes handle
  identity, not behavior; external-handle binding is explicitly trusted for now.
  Later, sandboxed tasks can strengthen this with the nix derivation hash of the
  running code.
- **Irregular-subtree escape hatch (old Q6/regularity).** Regular subtrees can
  emulate irregular ones: a data grid where each cell allocates a UUID prefix, with
  cell-specific subkeys under that prefix (no conflicts by construction). Possibly
  storage-heavy (table per cell) but optimizable in the backend without API changes.
  Motivating case: a task hosting an internal coding agent that stores state however
  it likes.
