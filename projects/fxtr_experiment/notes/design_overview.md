# fxtr_experiment — design overview

*(Formerly `spikes/fxtr_workflow_3`.)*

The root source of truth for the design decisions in this project. Each
section states what is decided and points at the document or module
docstring carrying the full writeup; a few principles that have no better
home are stated in full here. The *rationale trail* (why, alternatives
considered, review rounds) lives in `notes/discussion_log.md` and its
archives — but the log is never the source of truth: by the time a commit
lands, every decision in it must also live in the self-contained docs
rooted here (see the policy at the top of the log).

## Reading order

1. `CODE_OVERVIEW.md` — what exists and where.
2. `fxtr_experiment/view_spec.py`'s module docstring — the reference
   exposition of the static concepts: holders and frames, positional axis
   addressing, mapped/unmapped axes, the strip-set, and canonicalization,
   with a worked running example. Terminology used everywhere else is
   defined there.
3. `notes/execution_semantics.md` — the dynamic model: cells, generations
   and runs, the universal record-check, demand policy, the safety pass,
   errored vs interrupted, scheduling, and the fxtr2 embodiment
   commitments.
4. `notes/grounding.md` — the intent axis: user sourcing records and
   targets as the root facts, the derived **wanted** / **would_start** /
   **grounded** predicates (one least fixed point), orphanhood, gating
   policy, and the replay contract.
5. `notes/at_rest_format.md` — the at-rest export layout: the workflow
   state as (git repo + entity store), the directory-tree format, the
   generation-record entity, and the import/export contracts.
6. `notes/example_mh_pilot.md` — how the API feels end to end (also the
   home of the P1–P5 canonicalization principles' full statements).
7. `notes/boundary_discipline.md` — the cross-cutting rules for anything
   that crosses a service boundary (ids and coverage, persistence scopes,
   refs and lifetimes, error transit) — read before adding any wire
   surface.
8. The decision map below for everything else.

## The system in brief

A workflow is a global namespace of **grids** — tables whose cells are
indexed by coordinate axes and typed as data (entities or dataframes),
service **handles**, **task** results (with named entity/dataframe
outputs), **checkpoints**, or **subtrees** of further grids. Task grids
carry a stored **rule** wiring a task fn (resolved through a handle) to
input **views**; execution is durable and demand-driven, with every
effectful operation recorded per generation. The records double as
**observation** links, from which *currency* (fresh / stale / unsettled)
is derived, and as the generation's **claims** — written values
(``wrote_cell`` edges) and asserted grids (would-create rows on
write-authority opens) — which give provenance. User
sourcing records and **targets** root the second derived axis, *grounding*
(wanted / would_start / grounded — what a from-scratch replay would
re-create; `notes/grounding.md`). The top-level user
manipulates the same view calculus through a privileged capability, and
every boundary (task fns, steps, the user surface) is an fxtr2 service
boundary, RPC-capable in principle.

## Decision map

### Data model and addressing

- **Positional axes; names are local views.** Storage addresses cells by
  ordered flattened positions (enclosing subtrees' own axes, then the
  grid's own); axis *names* are per-view local vocabulary, resolved
  holder-side and never used for addressing. Full exposition:
  `view_spec.py`; canonical model types: `model.py`.
- **Open-map principle.** The set of populated cells of a grid is not an
  observable fact: no operation may ask "what are all the facts", and
  *adding* a fact never changes an old result (only modifying or removing
  facts can). Expected index sets come from **domain cells** — by
  convention, zero-axis dataframe cells whose value enumerates a semantic
  axis; reading "all of axis X" means joining against an explicit domain
  cell, and expanding the domain is an ordinary write that
  staleness-propagates. Domain cells are a usage convention, not a schema
  feature. (Top-level *presence queries* are the one deliberate exception:
  privileged, record nothing, and nothing downstream can depend on them —
  see `user_api.py`.)
- **Caching is position-addressed, not input-addressed** (a deliberate
  break from fxtr v1 and `spikes/fxtr_workflow_2`): results live at grid
  coordinates with staleness links; there are no input-derived cache keys,
  identical invocations at different cells do not share results, and
  re-execution happens via invalidation, never key collision.
- **Regularity: one config per subtree key.** Stored configs attach to
  subtree *keys*, so every cell of a subtree holds structurally identical
  children. Irregular shapes are emulated regularly when needed: a data
  grid whose cells each allocate a UUID prefix, with cell-specific subkeys
  under that prefix — no conflicts by construction.
- **Dataframe cells, named task outputs, frames/gather.** Dataframe cells
  are row multisets over scalar-only config-declared schemas
  (str/int/float/bool/bytes, per-column nullability defaulting to
  non-nullable; deliberately **no FxtrID columns**, so frames are leaf
  entities), canonically encoded as sorted-rows entities (so
  fingerprints/conflicts/replay work unchanged; the framework owns row
  canonicalization — sort key = the canonical per-row encoded bytes);
  task grids declare named outputs (staleness is per-output). Relational
  data moves through the calculus as a **`polars.DataFrame`** (no wrapper
  type; the concrete type is confined to the view layer, so the wire /
  identity never depend on it — the runtime + conversions are
  `tables.py`), and the effectful `gather` primitive replaces any
  in-system SQL engine in v0, with arrow-IPC transport (never
  identity-bearing), host-side canonicalization, and gather as a
  host-stitched stream. Checkers/indexers are *ordinary task grids* — no
  special node types. Whole writeup: `notes/dataframes.md`; canonical
  types: `model.py`; API contracts: `views.py`.

### Views and wiring

- **The construction API is a calculus of capability rewrites**
  (`views.py`): pure local rewrites (`rename_axes`, `at`, `read_only`) are
  free and unrecorded; only effectful ops (read/write/open/gather/steps)
  enter the record. Task-side views are opaque tokens + local shadows —
  absolute paths never cross the service boundary (`view_runtime.py`,
  `runner_api.py`).
- **Observability principle for config identity**: a grid's identity-
  bearing config is exactly what its task code can observe — its own axis
  names and each input's *received* axis names participate in config
  equality; nothing else about naming does.
- **The wire is positional; only identity-bearing names cross.** All name
  resolution is holder-side; open/wiring instructions cross as positional
  consume/keep instructions. Writeup: `runner_api.py` + `view_spec.py`.
- **Holding ≠ observing.** Rule inputs are grantable capabilities; only
  reads/demands/connects/writes record observations (principle P5 in
  `notes/example_mh_pilot.md`).
- **Coordinates are structure, not data** (`views.CoordView`, constructors
  `int_coord`/`str_coord`; stored form `model.CoordBinding`): the path-less
  1D view whose value along its single axis *is* the coordinate. Wired as a
  task input with the axis vmap-consumed, each cell learns its own
  coordinate — the sanctioned way in, since consumed axes are otherwise
  unobservable. Reading one is unrecorded-pure (no observation, no
  staleness edge — a coordinate can never change), sync, and wire-free:
  consumed/literal values resolve runner-side at grant-mint time and ride
  the grant; kept axes are the identity function. Identity-bearing like
  any input (kind + template + received name in config equality; literals
  allowed). Received coord grants are token-backed, so forwarding into a
  child rule translates like any spec (batch invariance included) — only
  locally created coord views cross as the token-less wire variant, and no
  effectful op accepts a coord token. Spec form: `view_spec.CoordViewSpec`
  in the disjoint `ViewSpec` family.
- **Read-xor-write is op-level, per generation, per grid** — holding both
  a read and a write view is fine; *performing* both is an integrity
  violation (`errors.ReadWriteExclusionError`).
- **Opens follow the chain; `read_only()` is a holder-local privilege
  drop.** Opens carry no mode: every open/attach returns a grant with the
  chain's authority (child authority never exceeds the grant's — a read
  grant's children are read-only, sliced or not, so read access to a
  subtree confers no write authority anywhere inside it, and an unsliced
  read grant cannot launder itself back to write through an attach-open;
  internals are read-only from the top; task grants are read-shaped).
  ``read_only()`` drops privileges *locally* (the view layer enforces the
  drop; the capability behind it keeps the chain's authority) — where the
  flag becomes real is **wiring**: forwarding a view into a child rule
  populates the binding's identity-bearing mode from the view's effective
  mode, so an undropped view wires as a write grant and ``read_only()``
  is how read-only inputs are declared. The one explicit-create remnant
  is the top-level ``open_handle``'s ``create`` flag (authority minting
  is deliberate). Enforcement: `view_spec.open_child` + the op-level
  write/create checks.
- **Creation sites are total; full-frame is a property of creation
  capability.** Write-authority subtree views — and the creation path of
  ``open_task``, whatever its mode — are full-frame, and wiring bound to
  an unmapped holder axis is rejected at create time (the batch-invariance
  principle), so canonicalization is cell-independent by construction.
  **Slicing sheds create authority** (`view_spec.bind_keeps` downgrades a
  bound subtree spec to read whatever the grant's mode, so crafted wire
  selections are sound; ``at()`` drops the local flag to match): a sliced
  view is a browsing window — opens/attach carry ``ViewSel``
  selections so holder-local slices reach the runner, children attach
  read-only, creates are refused; top-level internals browsing
  (``attach_internals``) is the canonical instance. Writeups:
  `view_spec.check_subtree_full_frame` / `view_spec.bind_keeps`.
- **A task/step fn is any async ``(ctx, /, **args)`` callable**, and the
  author decorators *are* the service adapters: ``@task_fn`` ≡
  ``TaskFnAdapter(fn)``, ``@step_fn(type_id=...)`` ≡
  ``StepFnAdapter(fn, type_id)`` (the step's definition site owns its
  identity — the recorded half of every checkpoint invocation). The
  adapters are themselves such callables, so plain fns, class instances,
  and decorated objects satisfy one protocol and behave identically when
  called locally. Contracts: `task_api.py` (pure protocols); adapters
  beside the machinery (`task_adapter.py` / `view_runtime.py`).

### Execution

- The whole dynamic model — generations vs runs, the universal
  record-check and hit-mismatch rules, the currency model (fresh / stale /
  unsettled, with "settled" the derived fresh-or-stale resolution and
  well-founded support required for freshness), the request-=-observation
  invariant (a task observes what it *requests*, and completes only if
  every request resolved — an aborted or faulted request poisons the run
  to interrupted, and committed work is never waited on under
  cancellation), per-generation failure records (engine-written at raise
  time; plural), the demand policy with its strict-freshness v0 default,
  fault propagation as interruptions, the always-on safety pass, and
  single-runner v0 scheduling — is `notes/execution_semantics.md`.
- **Currency verdicts are cached in the state db as stamps** (semantics
  unchanged; a cached verdict is never served stale): marking = clearing
  stamps up the observer DAG in the mutating transaction, with no
  separate dirty bit ("dirty" ≡ "unstamped"), and verification serves
  live stamps in O(1) or derives-and-stamps settled generations — the
  stamps are the only memo (no in-process cache, no change counter). The
  session owns the maintenance (`state_store.py` — every mutation body
  declares its "Marks:"); writeup: the "Currency maintenance in the
  database" section of `notes/execution_semantics.md`.
- **The error-poisoning contract** is `errors.py`'s module docstring:
  workflow errors persist at raise time and fail the generation even if
  task code catches them; the errored/interrupted split follows the error
  taxonomy.
- **Errors that cross a service boundary are informational only.** fxtr2
  wraps uncaught exceptions as `IRException` with the original class name
  as `category`; that transit form serves tracebacks and tests, and
  nothing durable is ever classified from it — poisoning persistence and
  the errored/interrupted classification happen runner-side, where the
  original typed exception (or the engine's own record of it) is
  available. Nothing that crossed the wire is trusted for system state.
  The *consuming* boundaries (the view runtime's effectful ops, the
  `Workflow` wrapper) do reconstruct the documented typed exception from
  the transit form for their callers (`errors.typed_wire_call` — chained
  from the transit exception, unknown categories pass through), so task
  and user code catch `WriteConflictError`, not a category string; this
  is ergonomics on top of the doctrine, never an input to system state.

### Intent and grounding

- The whole intent-axis model — the editable root facts (revocable,
  fingerprint-pinned **sourcing records** plus **targets**), the
  record-derived claims (write-authority opens, writes, output reads), the
  three mutually recursive derived predicates (**wanted** /
  **would_start** / **grounded**, one least fixed point), local
  *orphanhood*, gate policies, the replay contract, and the maintenance
  sketch — is `notes/grounding.md`. Key commitments:
- **Two orthogonal derived axes.** Currency judges *values* (do recorded
  observations still match the world); grounding judges *intent and
  structure* (does this fact trace to a durable user declaration, and
  would a from-scratch replay re-create it). Grounding is deliberately
  fingerprint-blind; the linter-clean state — the replay contract's
  hypothesis — is the conjunction.
- **Grounding gates are pure policy over stamped derived state** (like
  currency gating): v0 default is the strict gate (new observations of
  ungrounded subjects fault, fault-class), with local-only and off as
  policy levels. Requests are self-vouching, so the gate only has bite on
  data/handle-cell reads — the one channel demand does not flow through.
- **Launch requires a target**: user demands that would execute require
  the cell be a declared target (with auto-add sugar); looking is served
  by the labeled inspection channels and needs no demand.
- **Grid deletion with dependents is surfaced immediately** via the
  reverse rule-reference index (dependents' grounding flips in the
  deleting transaction); refuse-vs-notify is a UX-round followup.
- **The linter is structural and target-rooted** (`WorkflowSession.lint`,
  served as the capability's `lint` → `user_api.LintReport`): a
  one-snapshot inventory of dangling intent, red targets (attached,
  materialized, ungrounded — root checks suffice by cone-transitivity),
  and every non-grounded fact. Staleness is deliberately out (currency
  is the operational rerun question); unmaterialized targets are pending
  intent, never findings. Two clean levels: *sound* / *clean*. Writeup:
  the linter section of `notes/grounding.md`.

### Authority and trust

- **Handles cross as keys; authority follows the record.** The outer level
  reads a handle cell's *key* (an ordinary identity observation); the key
  travels into step args as plain data; `connect(key)` exchanges it for a
  live service, and only keys the current generation has read *from handle
  grants* may be connected. Authority never flows through data — ids read
  from data grids confer nothing — and tasks never mint authority (no
  task-side handle-grid creation or handle writes; attach-only, read-mode
  handle views). Consequence: a workflow's complete external-authority
  surface is the handle grids the user populated plus the bindings
  registered on the runner — auditable without reading task code.
- **The handle trust boundary is identity, not behavior**: observing a
  handle observes *which* service was bound, and external bindings are
  explicitly trusted in v0. Sandboxed task code can later strengthen this
  with the nix derivation hash of the actually-running code.
- **Two binding classes, one serving path; neither is identity.**
  *Ephemeral bindings* are connector-based runner configuration (a
  registrant-supplied factory service whose `connect(request)` produces
  the bound service, spawn-per-use), bounded by the connector service's
  lifetime, with a release capability per binding: explicit `release()`
  is immediate; dropping the returned ref auto-releases (the release
  impl's mount lifespan on the dispatch call scope). Auto-release
  granularity is wherever the caller's ref bridge lives — the guarantee
  is that a release capability stranded in a closed scope cannot wedge a
  binding, not promptness within one shared scope. *Sandboxed bindings*
  are durable data the runner owns: a nix service defn + pinned
  dependency map + JSON config per handle key, saved in the state db,
  exported as the bindings file, and spun up via `fxtr_sandboxing` on
  demand (against one runner-configured repo, ephemeral machine-local
  state). A key is bound in at most one class at a time. Both classes
  deliver one uniform `BindingConnectRequest` (key + config +
  pre-spawned dependency refs), and swapping any binding is governed by
  the **compatibility ratchet** (the swapper asserts
  identical-in-distribution past results; only input-set widening may
  drift). Whole writeup: `notes/sandboxed_bindings.md`; contracts:
  `user_api.UserCapabilityProto.bind_handle` / `.bind_sandbox`;
  mechanics: `bindings.py` (`BindingHost` — the one binding authority
  and serving funnel, both classes) with the launcher seam's real impl
  in `sandbox_launcher.py` (the package's sole `fxtr_sandboxing`
  importer) and `user_service.py` (the release impl).

### The user surface

- **There is exactly one user capability** (`user_api.UserCapabilityProto`,
  a decorated wire protocol like everything else); the `Workflow` wrapper
  (`user_service.py`) is unprivileged convenience over a resolved proxy —
  in-process or remote, it cannot tell — and the local bootstrap
  (`user_service.open_local`) is a plain async CM building the runner and
  mounting the capability (deliberately not a mount-lifespan bootstrap;
  rationale in `user_service.py`'s docstring). The user is a holder
  outside the deterministic world: same calculus, no op record, never
  stale; writes/creates are marked user-sourced; `clear_cells` is atomic
  (any addressed cell running refuses the whole call).
- **Targets vs materialization are decoupled**: a *target* is durable,
  declarative DAG-root intent (exported/shareable; dangling targets are
  preserved and surfaced, never silently rebound); a *demand* is an
  ephemeral runner event. Targets are **fully bound** (one cell per
  record; open-slice targets await gather) and **unique per slice
  identity** (`add_target` conflicts across config drift; `remove_target`
  matches slice identity alone, so dangling records stay removable);
  `demand_target` is the by-record materialization primitive — targets
  are absolute root-frame slices no granted token addresses — and
  `run_targets` is wrapper sugar over it. Writeup: `user_api.py`.
- **Attach asserts nothing; opens assert a proposed config.** The
  `open_*` calls are create-or-attach against a canonical config the
  caller proposes (what task determinism needs); `attach` mints a grant
  for an *existing* grid from its stored config alone (kind, axes,
  schema, declared outputs — task grids included, no rule needed), so
  `list_keys` + `attach` reconstructs any view after a session reopen.
  Writeup: `user_api.UserCapabilityProto.attach`.
- **The introspection doctrine: values arrive labeled or not at all.**
  Presence queries (`list_keys` / `list_cells`) carry **no values** —
  a bulk listing serving values would smuggle data past the value-pinned
  demand gate; `inspect_cell` serves a cell's content *together with* its
  currency verdict from one session snapshot (the sanctioned way to see
  stale/unsettled results — never observable *as if fresh*); the only
  unlabeled-value channel is a read/demand, which is gated. Task
  **internals are browsable but read-only** (`attach_internals`: subtree
  transparency says the owner may look; the surface guarantees look-only
  — read-mode forced under internals roots, checkpoints listable but not
  attachable). Writeups: `user_api.py`.

### The monitoring UI

- The whole dashboard design — the tree/detail/drawer layout over the
  workstation view layer, the status-surfacing inventory, the
  capability's monitoring surface (`summarize`, generation reports,
  reverse observers, bindings, `delete_cell`), renderer packaging, and
  the demo shape — is `notes/monitoring_ui.md`; the live-update
  mechanics (watch subscriptions, event schemas, the delivery
  contract, the server-side engine) are `notes/event_system.md`. Key
  commitments:
- **The renderer drives the user capability directly** (no UI-specific
  gateway service): typed service dispatch decodes wire-form JSON into
  the protocol's dataclass args and encodes returns symmetrically, so
  the browser speaks the same wire contract as any RPC client, and the
  capability stays the single complete user surface.
- **Live state is served as watch subscriptions** — path-addressed,
  presence-class subscriptions delivering an initial snapshot plus
  keyed diffs, maintained by a server-side recompute-and-diff engine
  over the session's internal post-commit change hints
  (`change_hints.py`); subscribers hold a fold, never refetch, and a
  broken subscription is resubscribed for a fresh mirror. Ordering and
  losslessness within a live subscription come from the fxtr2
  `SubscriberHub` queue-mode contract. Writeup:
  `notes/event_system.md`.
- **The UI never widens the introspection doctrine**: presence-class
  rows stay value-free, content arrives with its verdicts (the
  `CellReport` shape, whether via `inspect_cell` or the cell watch),
  and the demand affordance keeps demand-requires-target explicit
  ("Target + run" vs "Run").
- **The plate view** (`notes/plate_view.md`) is a second dashboard
  visualization: the grids as a plate-notation graph — a node per grid,
  plates for replication axes (subtree containment plus **grant-matched**
  cross-tree merges, provable from `Param` vmap bindings), and typed
  grid-level edges (input grants, observations, writes, grid creations).
  It rests on a small relational overlay (`grid_graph` / a `watch_graph`
  family) over the existing workflow mirror; nodes/axes/status stay the
  summary's job.

### Storage and embodiment

- **Two sqlite files** side by side: the workflow state db (schema writeup
  = `storage.py`'s docstring: key-path grids, flattened-coords cells,
  public/private identity split, interned subject versions, targets,
  event log) and the fxtr entity store. The **liveness invariant**: an id
  is recorded in the state db only after its closure is persisted in the
  entity store (`state_store.py`).
- **Served ids are re-attached, per call site.** Durable presence is not
  wire coverage: a service return can only cover ids *tracked* in the
  dispatch scope's ancestry, and after a session reopen nothing has
  tracked a recorded id — so **every wire method returning ids
  re-attaches them** (`WorkflowSession.ensure_loadable`; the liveness
  invariant guarantees the pin exists, making the re-attach total).
  Deliberately a per-call-site rule, not an automatic layer: the
  namespace scope tracks only what was explicitly retrieved, and the
  invariant maintenance stays at the point of use. Full boundary rules:
  `notes/boundary_discipline.md`.
- **Canonical encodings are owned, not codec-derived** — fingerprints must
  not depend on library behavior; what lives in db columns vs entities vs
  never-stored is `encoding.py`'s docstring.
- **The at-rest state is exportable as (git repo + entity store)** —
  a directory of sorted TOML/TSV files mirroring the grid tree, id
  columns referencing the session's own store (the extraction
  contract), git commits as the version roots; grid keys are
  constrained at creation (`encoding.validate_grid_key`) so keys are
  filesystem-safe directory names. Sandboxed bindings export as the
  human-readable bindings file (the lockfile; ephemeral bindings stay
  unexported — `notes/sandboxed_bindings.md`). Whole writeup:
  `notes/at_rest_format.md`; implementation:
  `plans/phase3_persistence/`.
- **Scope tree**: runner scope (entity-store namespace attached; bindings)
  → per-*run* scope → per-step scope; task fns and steps sit behind
  ServiceRef boundaries from day one; every effectful op proxies through
  one mounted run capability per run. Writeup: the embodiment section of
  `notes/execution_semantics.md`; mechanics: `task_service.py` /
  `bindings.py`. Library
  code follows `v2/notes/scope_discipline.md` (explicit scopes threaded
  alongside refs; active scope only at the documented boundaries).

## Open questions

(carried forward; move to the discussion log as they get worked)

1. **Private-subtree read grants.** May a task grant read access to grids
   in its private internals subtree onward? Tentative compromise: tasks
   created *inside* your internals may be granted reads of your internals;
   tasks created in external/input subtrees may only be granted
   input-reachable grids. Unclear how much this matters; watching for
   consequences rather than settling now.
2. **Data-dependent reads.** Allowed (they just create richer observation
   dependencies; domain-cell-driven `gather` is the canonical instance) —
   anything else to watch, e.g. how such reads render in provenance views?
3. **FxtrID vs EntityRef in capability signatures.** Cell values cross as
   bare `FxtrID` (arena coverage ships on the wire); EntityRef would carry
   the target type for ergonomic loads. Revisit once real task code
   exists — the per-run-scope design makes the upgrade purely additive.
4. **Concurrency and write model.** Two tasks writing disjoint coordinates
   of one data grid; user edits concurrent with tasks; conflict detection
   timing; the single-runner v0 assumption.
5. **Domain cells and grid metadata.** Should a grid's tag entity
   eventually point at the domain cells governing each axis (for
   UI/browsing/joins)?

## Deferred followups (explicitly past v0)

- **Step-boundary cancellation of stale runs** (v0 lets a running-but-
  stale generation finish).
- **Rule migrations** (v0: changing a task grid's rule means a new grid;
  later, mutatis-mutandis migration — checkpoints are the valuable part).
- **Sandboxed-task code identity** (nix derivation hash in the trust
  boundary; the natural anchor is drv-hash recording in the bindings
  file — see `notes/sandboxed_bindings.md`).
- **Sandboxed-binding followups** (design settled in
  `notes/sandboxed_bindings.md`; its Deferred section carries the
  list): rebind/re-pin sugar, expected-key pre-declaration, per-defn
  process sharing + guest rollover, idle shutdown, negative build
  caching.
- **Best-effort determinism-footgun helpers** (base classes catching
  common mistakes early; the strong guarantee stays the replay check).
- **Safety-pass opt-out** for known-deterministic tasks.
- **Currency policy knobs** (execution gating is pure policy over the
  derived tri-state): permissive execution against non-fresh inputs;
  await-instead-of-fault when a target is unsettled only because its
  support is running; the derived "settling" state (would resolve fresh if
  all running support resolves to observers' recorded expectations) for
  optimistic downstream parking.
- **Gather-driven (open-slice) targets.** v0 targets are one fully-bound
  cell per record — an open slice would mean enumerating its cells, which
  the open-map principle forbids. The future form is domain-cell-driven:
  "target the cells spanned by this gather expression", deferred until
  gather expressions themselves are implemented.
- **Typed handle keys** (`HandleKey[T]`-style wrapper threading the
  expected protocol through `read_key` → `connect`; wire form stays a bare
  `FxtrID`).
- **Step-entry (checkpoint history) introspection.** Internals browsing
  lists checkpoint grids but does not attach them: entries live in their
  own table (invocation fingerprints + payloads), so browsing a task's
  recorded step history for the UI is a separate read-only affordance
  (roughly "list entries with step identity/args/result ids"), not a
  grid view.
- **Recursive delete** as a convenience over the rmdir-style primitive.
- **Grounding followups** (design settled in `notes/grounding.md`; these
  are its deferred items): the UX round's final gate-default call (strict
  vs local-only) and the `delete_grid` refuse-vs-notify ``force`` toggle;
  the pruning affordance over orphaned/unwanted facts; the
  blocking/parking execution mode (one-pass replay convergence — the
  same mechanism as the await-instead-of-fault currency knob above).
- **Version log / pinning-GC / analytical export** (parquet snapshots of
  provenance for duckdb/polars analysis).
- **Dataframe followups** (design settled in `notes/dataframes.md`;
  these are its deferred items): chunked/partitioned frame cells and
  chunked wire transport; lazy/pushed-down table querying; a native
  canonical row encoder; the `read_many` entity fan-out convenience;
  table entities into step args; a frame multiset-equality helper; the
  persistent projection cache; richer dashboard frame rendering
  (browser-side arrow / partial loading — the current renderer loads
  the whole entity on demand).
