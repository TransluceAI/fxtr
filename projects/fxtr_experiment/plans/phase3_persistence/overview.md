# Phase 3 — persistence: the at-rest state as (git repo + entity store)

Goal: bidirectional import/export between the workflow state db and a
git-friendly at-rest layout — a directory of sorted, canonically-encoded
TSVs whose id columns reference the *same* entity store the session
runs against. Export is a deterministic projection of the at-rest state
(no running tasks); import destructively loads a state db from the
layout, with every derived verdict conservatively unstamped (stamps are
memos — a fresh pipeline re-derives fresh with no re-runs, and `lint` is
the what-did-I-just-load check). Normal git operations on the exported
directory then give sharing, versioning, and history: **git commits are
the version roots** (no snapshot entity), at manual granularity — a v1
of the deferred version log.

## Chunks

Each chunk lands with tests, a CODE_OVERVIEW update, and design-doc
updates as decisions become current state; details and deviations live
in the chunk's own file (promote anything design-relevant to
`notes/discussion_log.md` and the canonical docs).

- [x] [chunk0_format_design.md](chunk0_format_design.md) — design round
      with a decision gate: the file/column/manifest spec, the
      generation-record entity schema, and the format invariants.
      **Landed** as the standing note `notes/at_rest_format.md` (the
      directory-tree layout — git tree mirrors the grid tree — with
      per-grid TOML/TSV files), plus the key-charset law's
      creation-time enforcement as a code ride-along.
- [x] [chunk1_generation_record.md](chunk1_generation_record.md) — the
      generation-record entity in `encoding.py`: assemble-from-rows /
      decode-to-rows, canonical ordering, strong-ref closure pinning.
      No schema change. **Landed**, including the `step_type_id`
      extraction-contract fix (the checkpoint path persists the step's
      type entity).
- [x] [chunk2_flip_time.md](chunk2_flip_time.md) — flip-time
      persistence: the `record_id` column and the invariant *terminal ⇒
      record entity exists* (run end, the startup crashed-run flip,
      retry re-flips). **Landed** (schema v7), as the biconditional
      ``record_id`` non-NULL ⇔ terminal: the choreography inside
      ``set_status`` (one choke point), with the startup crashed-run
      flip following the same gapless per-row choreography —
      terminal-with-NULL-record is unrepresentable.
- [x] [chunk3_export.md](chunk3_export.md) — export: one-lock-hold
      session op (refuse while running), deterministic full-dump
      writer + manifest, capability/wrapper surface. **Landed**, with
      the pre-chunk refinements: directory ownership *proved* by an
      allowlist validator (misdirection is a safe error), the
      `EXPORT_IN_PROGRESS` marker bracketing every write, dot-entries
      invisible, counts returned.
- [x] [chunk4_import.md](chunk4_import.md) — import: the destructive
      capability op (empty-or-`force`), loader + derived rebuilds +
      validation, the counts-only `ImportReport`. **Landed**: parse +
      verify entirely before anything is written (all missing ids in
      one error; checked redundancy enforced), one locked destructive
      transaction, events surviving the wipe.
- [x] [chunk5_suites.md](chunk5_suites.md) — the suites: round-trip
      byte identity, re-derivation equivalence, at-rest variety
      round-trips, and the real-git e2e (a partial-then-completed
      `data → task1 → task2` arc pinning both restore flavors —
      result fidelity and definition fidelity). **Landed** as the
      messy-state round-trip (verdict-for-verdict + byte-for-byte) +
      the four-leg git arc.

## Cross-cutting decisions (settled in the 2026-07-09 scoping round)

- **Own format, not the pin-namespace TSV format.** The existing
  `fxtr2` tsv-namespace layout is strictly two-column key→id with
  colon-free keys — our identity texts don't fit it, and we don't need
  its tooling in v0. The at-rest layout is purpose-built multi-column
  TSVs inspired by its conventions: **sorted rows, canonical encodings,
  byte-stable re-export** (clean git diffs; idempotent export is a
  no-op diff). No hybrid/standard namespace ships in this phase.
- **The extraction contract is a format law**: every FxtrID the state
  references appears in a schema-declared id-typed column — nothing
  hides ids inside opaque non-entity blobs — so collecting all root ids
  is one pass over the TSVs, and the store's complete-subgraph
  invariant supplies the transitive closure. This is what makes the
  "(git repo + entity store)" pairing well-defined without a snapshot
  root entity.
- **Canonical identity text is TSV-safe by construction**: canonical
  JSON escapes control characters, so key-path/coords columns can never
  contain a raw tab or newline. No extra escaping layer.
- **One shared entity store.** The session's entity store *is* the
  export's backing store; export writes TSVs (plus the record entities
  already stored at flip time) and never copies entity bytes. The
  liveness invariant guarantees every recorded id's closure is pinned
  there. Cross-machine store sync stays out of scope (behaviors-hub
  territory).
- **Generation records are stored at flip time** (not assembled at
  export): one canonical entity per non-running generation holding
  everything the generation owns — status, fn ids, named results,
  failures, observations, assertions, request edges, checkpoints —
  autoincrement-agnostic (no exec_ids, no version_ids). A `record_id`
  column on `task_execs` points at it (persist-then-record). Export is
  then O(rows) with no assembly. Turnover deletes the *pointer*; the
  entity survives in the store, and git history of `generations.tsv`
  keeps old record ids findable — the manual-granularity version log.
- **Derived state is not exported**: currency/grounding/wanted stamps,
  `grid_refs`, `parent_path` are recomputed or rebuilt on import
  ("conservatively dirty" = unstamped; nothing is lost because verdicts
  are genuinely derived).
- **Export/import refuse while anything is running** — the layout is an
  at-rest format. Any at-rest state is serializable: errored,
  interrupted, ungrounded, dangling intent included.
- **Import is a capability method and destructive**: it sets the
  current state to the imported state. Refuses on a non-empty state db
  unless `force=True` (wipe + load, one transaction); the backup
  affordance is doctrine, not machinery — export first, git is the
  backup. Missing entities → typed error naming them. Returns
  counts-per-fact-kind only. Outstanding view grants may dangle
  afterward — the same hazard `delete_grid` already creates, faulting
  at use time.
- **Events stay local-only** (passive provenance, never load-bearing);
  the `meta` table is superseded by the layout's **manifest**, which
  carries the format version and nothing else — versioned from day one,
  though pre-release compatibility promises stay at zero.
- **One workflow-db export per repo/directory**; fixed filenames;
  export overwrites wholesale (git shows the delta).

## Out of scope (this phase)

- Merge / partial / row-subset import (where git semantics get
  genuinely interesting — future).
- Replay-install from sourcing records (grounding.md's replay step 1 —
  belongs to a future replay/version-log phase; without it, an
  intent-only file subset imports as dangling records, by design).
- Cross-machine entity-store sync; automating git commands; GC/pinning;
  a CLI; format migrations.

## Status

**The phase is complete**: chunks 0 (the format spec —
`notes/at_rest_format.md` — plus the key-charset law), 1 (the
generation-record entity + the `step_type_id` fix), 2 (flip-time
persistence, schema v7), 3 (export), 4 (import), and 5 (the
cross-cutting suites, `tests/test_at_rest_e2e.py`). Scoping and format
rounds are logged in `notes/discussion_log.md`.
