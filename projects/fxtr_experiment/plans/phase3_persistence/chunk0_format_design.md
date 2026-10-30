# Chunk 0 — the format design round

**Status: complete.** The spec landed as `notes/at_rest_format.md`;
resolutions below. One scope deviation: the key-charset law's
creation-time enforcement landed *here* (a small code ride-along —
`encoding.validate_grid_key` + the `create_grid` check + tests) so the
note describes true current state rather than an aspiration.

## Resolutions (2026-07-09 round)

- **The git tree mirrors the grid tree**: one directory per grid named
  by its key (subtree and internals nesting alike), per-grid files
  (`grid.toml` mandatory; `cells.tsv` / `generations.tsv` /
  `sourcing.tsv` / `targets.tsv` omit-if-empty, coords-only keys —
  grid-path columns disappear). Intent files are **per-grid** (they
  scale with cells; the global intent view is `list_*`/lint's job).
- **The export dir is wholly export-owned**: total rebuild, everything
  unrecognized removed; unrelated files live outside it.
- **The key-charset law**: keys match ``[a-z0-9][a-z0-9_-]*`` (≤ 100
  chars), rejected **at creation** (the `create_grid` choke point) —
  maximally strict to start; lowercase-only avoids case-insensitive
  filesystem collisions; no ``.`` makes reserved-filename collision
  impossible. All existing keys already conformed (209 tests
  unchanged).
- **TOML for the non-row files** (`manifest.toml` with
  ``format_version`` only; `grid.toml` per grid); headerless sorted
  TSVs for row files, schema versioned by the manifest.
- **Checked-redundant readable columns**: `grid.toml` ``kind``,
  generations' ``generation`` + ``status``, targets' ``coords`` —
  verified on import, never trusted. The generation uuid column was
  briefly dropped as no-diff-signal, then kept (revised in-round): it
  is what makes retry semantics legible in git — a retried incomplete
  generation diffs as the same uuid with a new record id, turnover as
  a new uuid.
- **Failure timestamps stay in the record** (at-rest provenance; byte
  determinism is only claimed across exports of the same state).
- **Findings**: ``pending`` is never stored (generations insert
  running → at-rest statuses are exactly the three terminal ones), and
  **``step_type_id`` violates the extraction contract today**
  (`define_type` stores type entities into GLOBAL_SCOPE only; the
  checkpoint path doesn't persist them) — fix assigned to chunk 1,
  TODO'd in the note.

## Scope (as planned)

Settle and write up:

- **The file set and per-file schemas** (columns, sort orders, the
  canonical encodings each column uses). Working sketch:
  - `grids.tsv` — key_path → config entity id (kind/axes live in the
    config; `parent_path`/`grid_refs` derived on import).
  - `cells.tsv` — (grid_path, coords) → value entity id
    (data/dataframe/handle alike).
  - `generations.tsv` — (grid_path, coords) → generation-record entity
    id.
  - `sourcing.tsv` — (grid_path, coords) → pinned fingerprint id (the
    row *is* the fact; no wrapper entity).
  - `targets.tsv` — slice entity id + expected-config id per target.
  - the **manifest** — format version only.
- **Readable-but-derivable columns**: whether targets (and possibly
  generations) carry denormalized location columns for git-diff
  readability, with import verifying the redundancy (checked, never
  trusted).
- **The generation-record entity schema**: generation uuid, status,
  current_fn/executed_fn, named results, failures (insertion-ordered
  list — order is display order, ids are not data), observations as
  (kind, location, fingerprint) tuples, grid assertions, request
  edges, checkpoint entries — autoincrement-agnostic throughout
  (exec_ids and version_ids are storage bookkeeping, re-created on
  import).
- **The format invariants**, stated as law in the note: the extraction
  contract (every referenced id in a schema-declared id-typed column);
  byte-stable determinism (same state ⇒ identical bytes); *terminal ⇒
  record entity exists* (chunk 2's maintenance invariant).
- **`pending` generations at rest**: verify whether a `pending` row can
  exist when nothing is running; decide refuse vs exclude vs
  represent. (Suspicion: transient within demand only — then export's
  refuse-while-running check should cover or assert it.)
- **Manifest details**: filename, encoding, version literal, error
  shape on version mismatch at import.

## Tests

None (design round). The spec's executable form is chunks 1–5.

Notes/log: (fill in during the chunk)
