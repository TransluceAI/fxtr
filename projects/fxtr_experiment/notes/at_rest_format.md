# The at-rest state format (design note)

Standing, self-contained spec of the workflow's **at-rest export
layout**: how a workflow state db is projected into a git-friendly
directory of TOML + TSV files whose id columns reference the *same*
entity store the session runs against, and loaded back. Terms used
without definition (grids, cells, generations, the record, sourcing,
targets, verdict stamps) are defined in `notes/execution_semantics.md`
and `notes/grounding.md`; the rationale trail is
`notes/discussion_log.md` (the 2026-07-09 phase-3 rounds).

**Implementation status: fully implemented** (phase 3,
`plans/phase3_persistence/`): the key-charset law (creation-time
enforcement, `encoding.validate_grid_key`); the generation-record
entity (`model.GenerationRecord` + `encoding.GenerationRecordEntity`
with the canonical ordering, `WorkflowSession.build_generation_record`
/ `load_generation_record`, the checkpoint path persisting step type
entities); flip-time persistence (schema v7: the ``record_id`` column
with the invariant *non-NULL ⇔ terminal*, maintained by
``set_status``'s choreography — the one status choke point — with
running flips clearing the pointer; the startup crashed-run flip
follows the same gapless choreography, so terminal-without-record is
unrepresentable); export (`export.py` — the one-lock snapshot via
`WorkflowSession.export_snapshot`, the proven-ownership validator, the
marker-bracketed deterministic writer — served as
`UserCapabilityProto.export_state` returning counts); and import
(`importer.py` — parse + verify entirely before anything is written,
then one locked destructive transaction — served as
`UserCapabilityProto.import_state`, per the import-semantics section
below). The cross-cutting behavior — messy-state round-trips
(verdict-for-verdict, byte-for-byte) and the real-git
partial/completed restore arc — is pinned by
`tests/test_at_rest_e2e.py`.

## The model: (git repo + entity store)

A workflow at rest is representable as a pair:

- a **directory of small text files** (this spec) capturing every
  at-rest fact by reference — the git-tracked, human-diffable half;
- the **entity store** holding every referenced entity's closure — the
  content-addressed half (the session's own store; export never copies
  entity bytes, and the liveness invariant guarantees every recorded
  id's closure is already pinned there).

Ordinary git operations on the directory then provide sharing,
versioning, and history: **git commits are the version roots**. There
is deliberately no snapshot root entity and no workflow identity check
— a commit identifies a state, and importing against the wrong store
surfaces as missing entities.

The layout is an **at-rest format**: export and import refuse while any
generation is running. Any at-rest state is serializable — errored,
interrupted, ungrounded, dangling intent included; at-rest generation
statuses are exactly ``complete | errored | interrupted`` (the
``pending`` member of `model.ExecStatus` is never stored: generations
are inserted running).

## Directory layout: the git tree mirrors the grid tree

```
<export dir>/                 # wholly export-owned (see below)
  manifest.toml               # format_version = 1
  inputs/                     # one directory per grid, named by its key
    grid.toml                 #   the grid row (always present)
    cells.tsv                 #   populated cells (omitted if none)
    sourcing.tsv              #   user sourcing records (omitted if none)
  t/                          # a task grid
    grid.toml
    generations.tsv
    targets.tsv
    scratch/                  #   an internals child grid, nested inside
      grid.toml
      ...
```

- **Nesting is the grid tree**: a grid's directory contains its child
  grids' directories (subtree children and task-internals children
  alike — the step kind is derivable from the parent's kind, exactly as
  in the state db). The export root directory *is* the zero-axis root
  namespace.
- **The export directory is wholly export-owned, and ownership is
  proved, never assumed**: before touching anything, the export
  validates that the directory is absent, empty, or contains only known
  layout members (the manifest, the marker, per-grid reserved files,
  valid-key directories) — anything else refuses wholesale
  (`errors.ExportLayoutError`) with nothing modified, so a misdirected
  export is a safe error, never a destructive sweep. A proved directory
  is then totally rebuilt, with known members the state no longer wants
  removed (a deleted grid loses its directory). **Dot-entries**
  (``.DS_Store`` and friends drop uninvited) are invisible: never
  validated, never written, never removed except with a removed grid
  directory. Keep unrelated files (READMEs etc.) *outside* the export
  dir — typically the export dir is a subdirectory of the sharing repo.
- **The in-progress marker**: every export writes ``EXPORT_IN_PROGRESS``
  first and removes it last; its text explains that an export is running
  or crashed. A crashed export is therefore legible on disk and never
  mistaken for a complete one (import refuses on it); recovery is
  re-export or git checkout — git is the backup, by doctrine.
- **Omit-if-empty**: per-grid TSVs exist only when they have rows;
  absence means empty. `grid.toml` is mandatory (it *is* the grid row,
  and it marks the directory as a grid for import's tree walk).

## The key-charset law

Grid keys become directory names, so they are constrained **at grid
creation time** (`encoding.validate_grid_key`, called by
`WorkflowSession.create_grid` — the one choke point, so no invalid key
ever reaches the ``grids`` table or the layout):

```
key ::= [a-z0-9][a-z0-9_-]*        (length ≤ encoding.GRID_KEY_MAX_LEN)
```

Lowercase-only sidesteps case-insensitive-filesystem collisions
(macOS); excluding ``.`` makes collision with the layout's reserved
file names (``grid.toml``, ``*.tsv``, ``manifest.toml``) structurally
impossible; no leading ``-``/``_`` keeps names shell-friendly.
Deliberately maximally strict to start: relaxing later is compatible,
un-relaxing is not.

## File schemas

**TSV conventions** (shared by every ``*.tsv``): headerless,
tab-separated, one row per fact, rows sorted lexicographically by the
leading key column(s), trailing newline. Columns are either **canonical
identity texts** (the same encodings as the db columns —
`encoding.py`'s flattened-coords / key-path JSON, which canonical-JSON
escaping makes TSV-safe: no raw tab or newline can appear) or **FxtrID
strings** (``id_string()`` form). The schema below is versioned by the
manifest; files carry no self-description.

- **`manifest.toml`** — ``format_version = 1``. Nothing else (room for
  future fields; pre-release, no compatibility promises yet).
- **`grid.toml`** — the grid row:
  ```toml
  kind = "task"          # checked-redundant (also encoded in the config)
  config_id = "dw5d7n…"  # the canonical config entity
  ```
- **`cells.tsv`** — ``coords → value_id``: one row per populated
  data/dataframe/handle cell; the value entity (cell entity, canonical
  frame entity, or handle entity) is the whole fact.
- **`generations.tsv`** — ``coords → generation, status, record_id``:
  one row per task cell with a current generation. ``generation`` (the
  public uuid) and ``status`` are checked-redundant (both live in the
  record): ``status`` makes a status flip a readable one-word diff, and
  ``generation`` makes retry semantics legible — a retried incomplete
  generation diffs as the *same* generation with a *new* record id,
  while turnover diffs as a new generation.
- **`sourcing.tsv`** — ``coords → fingerprint``: user sourcing records;
  the grid-level record is the row with empty ``coords``.
- **`targets.tsv`** — ``coords → slice_id, expected_config``: one row
  per target on this grid (``coords`` checked-redundant against the
  decoded slice template).

**Checked redundancy**: every derivable column (`grid.toml`'s ``kind``,
generations' ``generation`` and ``status``, targets' ``coords``) exists
purely for git-diff readability and is **verified against the decoded
truth on import, never trusted**.

## The generation-record entity

The canonical "everything this generation owns", stored **at flip
time** — whenever status leaves ``running`` (run end, the startup
crashed-run flip, retry re-flips) — and pointed at by ``record_id`` on
``task_execs``. The maintenance invariant: **terminal ⇒ the record
entity exists and matches the rows**. Turnover deletes the pointer with
the row; the entity survives in the store, and git history of
``generations.tsv`` keeps superseded record ids findable — version
history at commit granularity.

The invariant is maintained at one choke point — ``set_status`` runs
the choreography on every terminal flip (assemble at the target status,
re-attach the referenced ids, persist, then update status +
``record_id`` in one transaction) and clears the pointer on flips to
running. The startup crashed-run flip follows the same gapless
choreography per row, with the RunnerCrash failure synthesized into the
assembled record and inserted in that same final transaction — so
**terminal-without-record is unrepresentable**: a crash at any point
leaves the row ``running`` with nothing written, retried cleanly and
idempotently at the next open.

Fields (autoincrement-agnostic — exec_ids and version_ids are storage
bookkeeping, assigned fresh on import; canonical CBOR via the
`encoding.py` field-strategy machinery; embedded ids are strong refs,
so persisting a record pins its whole closure):

```
generation      the public uuid string
status          complete | errored | interrupted
current_fn      FxtrID     executed_fn  FxtrID
results         output name → FxtrID            (sorted by name)
failures        ordered list of (kind, error_class, message, ts)
                                                (insertion order kept)
observations    sorted set of (kind, grid_path, coords, output, fingerprint)
assertions      sorted set of (grid_path, config_id)
request_edges   sorted set of (kind, grid_path, coords)
checkpoints     sorted set of (grid_path, coords, step_type_id,
                               args_id, value_id)
```

Failure timestamps stay: they are at-rest provenance facts, and byte
determinism is only ever claimed across exports of the *same* state.

## Format invariants

1. **The extraction contract**: every FxtrID the state references
   appears in a schema-declared id-typed column (TSV columns, the
   `grid.toml` ``config_id``, the record entity's id fields) — nothing
   hides ids in opaque non-entity blobs — so collecting all root ids is
   one pass over the files, and the store's complete-subgraph invariant
   supplies the transitive closure. Every id column is a *fetchable
   root*: its entity's closure is pinned in the store (``step_type_id``
   included — the checkpoint path persists the step's type entity
   beside its payloads).
2. **Byte-stable determinism**: the same state exports to identical
   bytes (canonical encodings, canonical orders, total rebuild), so an
   unchanged re-export is a no-op git diff and diffs are row-level.
3. **Terminal ⇒ record exists** (the flip-time maintenance invariant
   above), which makes export O(rows) — no assembly at export time.
4. **At-rest statuses only**: export and import refuse while anything
   is running.
5. **Checked redundancy**: derivable columns verified on import, never
   trusted.

## What is deliberately not exported

- **Derived state**: currency/grounding/wanted stamps (memos —
  everything imports unstamped and re-derives; a fresh pipeline
  re-derives fresh with *no re-runs*), ``grid_refs`` and
  ``parent_path`` (rebuilt from configs/paths), targets' denormalized
  locations (rebuilt from slice entities).
- **The events log** (passive provenance, never load-bearing;
  local-only).
- **Ephemeral bindings** (runner configuration by doctrine — an
  imported workflow re-binds connectors, exactly like a session
  reopen). *Sandboxed* bindings are the deliberate exception: they
  ARE exported, as the sorted human-readable TOML bindings file at
  the repo root (the lockfile role; contract and record shape:
  `notes/sandboxed_bindings.md`), and import restores them — inert
  until the runner's sandbox repo is configured. The runner's repo
  path itself is machine-local and never exported.
- Internal bookkeeping ids (exec_ids, version_ids, failure_ids).

## Import semantics

Import is a **destructive capability operation**
(`UserCapabilityProto.import_state`): it sets the current state to the
imported layout. Parsing and verification happen entirely *before*
anything is written (`importer.read_layout` /
`importer.verify_and_decode`): the manifest version, no in-progress
marker, canonical files (sorted unique rows, right column counts,
omit-if-empty enforced), every referenced id resolvable in the shared
store (all misses in one `MissingEntitiesError`), every
checked-redundant column verified against its decoded truth, and the
cross-row laws (file kind vs grid kind, sourcing's grid-before-cell,
checkpoint grids present). The destructive half is **one locked
transaction** (`importer.apply_import_body` via
`WorkflowSession.apply_import`): refuse while anything runs
(`NotAtRestError`); refuse a non-empty state db unless forced
(`StateNotEmptyError` — the backup affordance is doctrine, not
machinery: export first; git is the backup); wipe the fact and intent
tables — **the events log survives** (provenance is history, not
state; the import appends its own event) — then insert everything with
fresh exec_ids, re-interned versions, rebuilt ``parent_path`` /
``grid_refs`` / target locations, and **zero stamps** (everything
imports conservatively unstamped; verdicts re-derive on demand; `lint`
is the what-did-I-just-load check). Returns counts per fact kind,
nothing else. Ephemeral bindings are re-registered afterwards, exactly
as after a session reopen (sandboxed bindings arrive with the import
and need only the repo configuration); outstanding view grants may dangle
(the same hazard `delete_grid` already creates; ops fault at use time;
the root grant survives).
