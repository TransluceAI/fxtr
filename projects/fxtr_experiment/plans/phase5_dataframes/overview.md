# Phase 5 — dataframes

Goal: make dataframe cells real per the standing design note
`notes/dataframes.md` — the canonical frame entity and its encoding
spec, the frame runtime (frames are `polars.DataFrame`; conversions in
`tables.py`), frame cell reads/writes and frame task outputs on both
capabilities, and `gather` as a host-stitched stream — efficient enough
that task code queries gathered data with polars/duckdb without the
framework looping over rows in Python on the read path. Design
rationale: `discussion_log.md` (this directory, esp. the 2026-07-17
amendment) and the 2026-07-16 round in `notes/discussion_log.md`.

## Chunks

Each chunk lands with tests, a CODE_OVERVIEW update, and design-doc
updates as decisions become current state (in particular, retiring
the corresponding TODO in `notes/dataframes.md` when the last chunk
lands); details and deviations live in the chunk's own file (chunks
1–3 are scoped now; 4–5 carry their settled constraints and get
detailed at chunk start).

- [x] [chunk1_encoding.md](chunk1_encoding.md) — **model + frame
      encoding**: `ColumnKind` gains
      `bytes` / loses `entity`; `ColumnSpec.nullable` (default
      non-nullable; tagged encoding omits the flag when default);
      schema column-name validation at config creation; contract
      updates in `views.py` (delete `DataView.gather`, `TableValue`
      loses `FxtrID`, the `Table` ABC's in-memory-order stance);
      the new `frame_encoding.py` (canonical frame entity: embedded
      tagged schema + rows sorted lex by canonical row bytes; the
      scalar rules) with spec tests including cbor2-conformance
      checks (NaN normalization, `-0.0`, shortest-form floats,
      int64).
- [x] [chunk2_table.md](chunk2_table.md) — **the frame runtime**:
      polars as a dependency; the frame value is a raw `pl.DataFrame`
      (the `Table` wrapper is **deleted** — 2026-07-17 amendment in
      `discussion_log.md`), so `views.py` / task frame-output
      signatures take/return `pl.DataFrame`; `tables.py` is the
      conversion/validation layer — exact one-to-one dtype mapping (no
      implicit casts), `make_frame(schema, rows)`, `frame_to_ipc` /
      `ipc_to_frame`, `frame_to_rows`, and `validate_and_align` (names
      by set + exact dtypes + `Null`-cast + native null-legality). The
      arrow-IPC↔canonical-rows conversions (both directions — the
      CBOR→frame builder is needed for projection rebuilds) live here.
- [x] [chunk3_cell_ops.md](chunk3_cell_ops.md) — **frame cell ops
      end to end**: `write_cell_frame` /
      `read_cell_frame` carrying arrow bytes on the run and user
      capabilities (`read_batch` deleted from both); host-side
      validation (align-by-name, kinds,
      null legality vs the declared schema) and canonicalization;
      the in-memory projection LRU (behind a persistable seam);
      ops-engine integration
      (observations fingerprinted by frame entity id, record-check
      serving via the projection, write conflicts on multiset
      identity); frame task outputs through `run_task` (results
      loop + safety-pass canonicalization); at-rest
      export/import round trip (projection rebuilt lazily).
- [x] [chunk4_gather.md](chunk4_gather.md) — **gather**: the wire op
      (deduped key table in →
      host-stitched canonical stream out: sorted keys, per-cell
      canonical rows, one observation per distinct cell,
      fault-on-missing, empty cells contribute zero rows); the
      view-layer sugar on both surfaces (key extraction + dedupe,
      the local key-join, `column_prefix` + collision checks,
      driving-order output pin); an MH-aggregate-shaped scenario
      test. (`read_batch` was cut in chunk 3.)
- [x] [chunk5_polish.md](chunk5_polish.md) — **surface polish**:
      dashboard frame rendering — rescoped at chunk start from the
      planned `CellReport` preview variant to **zero wire changes**
      (the report already carries the self-describing frame entity's
      id; the frontend loads and renders it as a table —
      `frontend/src/frame_table.tsx`, drawer embeds + `frame_v0`
      dispatch renderers); MH example touch-ups; final doc sweep.
      (The former "`Workflow.write_table` accepts a polars frame
      directly" item was moot — under the 2026-07-17 amendment a
      polars frame is the only thing it ever accepts.)
