# Chunk 3 — export

**Status: complete.** (223 passed + pyright clean: `export.py` — the
snapshot body run as one locked session op
(`WorkflowSession.export_snapshot`, `NotAtRestError` atomically with
the read), the proven-ownership validator, the marker-bracketed
deterministic writer (tomlkit; sorted TSVs; total rebuild + pruning) —
`UserCapabilityProto.export_state` → `ExportReport` counts + the
`Workflow` pass-through; `tests/test_export.py`. Scoping refinements
from the 2026-07-09 pre-chunk round in the Notes.)

## Scope

- **The session op** (`WorkflowSession.export_state(dir)` shape): one
  lock hold (an at-rest snapshot, the lint discipline); **refuses if
  any generation is running** (typed error); reads rows + `record_id`s
  and writes the chunk-0 layout — no entity assembly, no entity-byte
  copying (the shared store already holds every closure, by liveness).
- **Deterministic full-dump writer**: fixed filenames, sorted rows,
  canonical encodings; overwrites wholesale (git shows the delta;
  re-export of an unchanged state is a no-op diff). Manifest written
  last.
- **The capability + wrapper surface**: an introspection-class method
  (records nothing; nothing downstream can depend on it). The
  directory path is server-side (like the db paths at open) —
  documented on the wire method.

## Tests

- Byte determinism: export twice → identical bytes; mutate one fact →
  exactly the expected row-level diff.
- Refusal while running (a parked mid-run task; the existing gate
  harness idioms).
- Content correctness: every table's at-rest facts appear; derived
  state (stamps, grid_refs) does not; the extraction contract holds
  (collecting id-typed columns yields exactly the referenced roots).

Notes/log (2026-07-09, at landing — the pre-chunk round's resolutions
plus implementation notes):

- **Ownership is proved, never assumed** (the round's key upgrade over
  the plan): rather than trusting the export-owned doctrine at an
  arbitrary path, the validator walks the existing tree and refuses
  wholesale (`ExportLayoutError`, nothing touched) on any non-dot entry
  that isn't a known layout member — a misdirected `export_state` is a
  safe error, never a destructive sweep. Dot-entries (``.DS_Store``)
  are invisible: never block, never written, removed only with a
  removed grid directory.
- **The `EXPORT_IN_PROGRESS` marker** brackets every export (written
  first, removed last; its text explains in-progress-or-crashed), so a
  torn export is legible on disk; no temp-dir atomicity machinery (not
  actually atomic for trees, and git is the designated recovery).
  Import (chunk 4) refuses on a present marker.
- **Export returns `ExportReport` counts** (symmetric with import's
  report); **tomlkit** writes the two TOML files (already an fxtr2
  dep); the snapshot is one locked op and rendering runs in a worker
  thread outside the lock — the lock protects snapshot consistency,
  not file I/O.
- New wire-typed errors: `NotAtRestError` (shared with import),
  `ExportLayoutError`.
