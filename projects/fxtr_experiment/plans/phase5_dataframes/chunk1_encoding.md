# Chunk 1 — model + frame encoding

**Status: complete.**

Design source of truth: `notes/dataframes.md` (the column model and
identity sections). This chunk is dependency-free (no polars): it
lands the model changes and the owned canonical encoding over plain
Python rows; arrow/Table machinery is chunk 2.

## Scope

- **`model.py`**:
    - `ColumnKind` becomes `Literal["str","int","float","bool","bytes"]`
      (`entity` removed, `bytes` added). No data migration exists to
      do — frame cell ops were never implemented — but grep for
      `"entity"`-kind references in tests/docs/examples.
    - `ColumnSpec` gains `nullable: bool = False`.
    - The dataframe section comment rewritten: frames are leaf
      entities (no strong refs), pointer to `notes/dataframes.md`;
      drop the "entity columns pin their referents" clause.
- **`views.py`**:
    - Delete `DataView.gather` (scalar-only columns leave it nothing
      to return; the fan-out alternative is a loop of `read()`s).
    - `TableValue = CoordValue | float | bool | bytes | None`.
    - `Table` ABC contract updates: in-memory row order is definite
      and deterministic; `to_rows()` returns *current* order (not
      "canonical (sorted) order"); multiset semantics live at the
      storage boundary. Representation wording: concrete impl is the
      polars-backed class — TODO(chunk 2) marker until it exists.
    - `DataFrameView.gather` docstring: key columns must be str/int
      matching axis kinds; null keys are errors.
- **`encoding.py`**: the tagged column form grows the nullable flag
  **omitted when default** — proposed form `[name, kind]` /
  `[name, kind, true]` (decode accepts length 2, or length 3 with
  literal `true`; anything else is `EncodingError`). Every existing
  all-non-nullable schema's encoding — and therefore every existing
  config fingerprint — stays byte-identical.
- **New `frame_encoding.py`** (leaf module; model + cbor2 + fxtr2
  entity machinery only). Docstring states the byte-level spec and
  points at `notes/dataframes.md`:
    - `validate_frame_schema`: non-empty unique names, no control
      characters; called at every config-creation path that declares
      a schema (`open_dataframe` and `FrameOutput` declarations —
      both the task-wire and user-capability opens funnel through
      the same config-creation body; verify and note where).
    - Row encoding: one canonical-CBOR array per row, values in
      schema column order. Exact-type validation per kind
      (`type(v) is int` etc., bool rejected as int), int64 range,
      null legality per column flag.
    - Frame encoding: sort rows lexicographically by their encoded
      bytes; the frame entity's data is
      `{"schema": <tagged form>, "rows": [<row arrays>...]}` under a
      new `define_type`d fxtr type. The in-entity row representation
      must encode byte-identically to the standalone row encoding
      used as the sort key (canonical CBOR is compositional — pin
      with a test).
    - `decode_frame(entity) -> (schema, rows)` — the read half
      (projection rebuilds, import verification).

## Decisions to confirm at chunk start

- The tagged nullable element form (proposal above).
- Zero-column schemas: propose **reject** at validation (a
  zero-column frame is just a row counter; no use case, degenerate
  encoding; loosening later is compat-safe).
- The precise control-character rule (proposal: reject code points
  `< 0x20` and `0x7f`).
- Frame entity wrapper shape: plain `encode_frame`/`decode_frame`
  functions + `define_type`, NOT a fifth `DataclassEntity` wrapper.
  Why the existing pattern doesn't fit: (a) there is no runtime
  model object to wrap — the runtime form is the polars `Table`,
  which this module must not know (chunk 1 is polars-free; the host
  write path goes bytes → rows → entity without a `Table`), so the
  field would be a throwaway `(schema, rows)` value; (b) the frame
  encoding is *normalizing* (the canonical row sort), so a
  sort-inside-serialize strategy breaks the pattern's store/load
  round-trip equality, while pre-sorted construction leaves the
  invariant unenforced — a plain function owning
  validate → sort → construct is honest; (c) the wrapper's real
  benefit (registry-typed decode for tooling) is presentation-side
  and can be added later as a thin read-only wrapper without
  touching identity. Mechanism (confirmed 2026-07-16):
  `FxtrScope.store_cbor` / `load_frozen_cbor` — build the entity
  dict directly with `"$fxtr.type": RawWeakFxtrID(<define_type id>)`
  (what `DataclassEntity` injects); frames are leaf entities (no
  FxtrIDs in the tree), so `store_cbor`'s child-ref/provenance
  dispatch is trivially inert, and byte determinism comes from the
  same canonical entity-CBOR layer as every other identity entity.
  Use `load_frozen_cbor` on read paths (cached, immutable).

## Tests

- cbor2-conformance suite freezing the spec: every NaN (payload /
  sign variants) → the canonical NaN encoding; `-0.0` distinct from
  `0.0`; shortest-form float samples; int64 boundary (2^63−1 encodes,
  2^63 rejected by validation).
- Exact-type rejections (bool-as-int, int-in-float-column, str-in-
  bytes-column); null legality (nullable vs non-nullable columns).
- Canonicalization: permuted row orders and duplicate rows →
  identical entity id; a **golden id test** pinning a small frame's
  entity id so any future encoding drift fails loudly.
- Compositionality pin: the standalone sort-key bytes appear verbatim
  in the entity encoding.
- Schema encoding: non-nullable schemas encode byte-identically to
  the pre-change pair form (config fingerprints stable); malformed
  forms rejected on decode.
- Config-creation validation: bad column names (empty / dup /
  control chars) rejected from both open paths.
- Pyright clean; no remaining `entity`-ColumnKind references.

## Notes/log

- **`validate_frame_schema` landed in `encoding.py`** (beside
  `validate_grid_key`), not `frame_encoding.py` as planned: it is a
  charset law like the others, and `frame_encoding` imports
  `encoding` for the tagged schema form — the planned placement would
  have been a cycle. The enforcement funnel is `store_config` (both
  open bodies — `ops_engine._open` and the user capability's — call
  it to compute the proposed config's fingerprint before any
  create/compare, so no invalid schema ever gets one; verified, not
  a shared body). `frame_encoding` additionally validates in
  `build_frame_entity` (belt over braces).
- **`FrameEntity` is a `StepPayloadEntity`-shaped dumb carrier**
  (raw `FrozenFxtrCbor` fields, pre-canonicalized), superseding the
  scoped-out raw `store_cbor` mechanism: `DataclassEntity` rides the
  `$fxtr.ctx` marker + `$fxtr.type` machinery that raw `store_cbor`
  would have skipped (a missing marker would have forked frame ids
  from any future marker-bearing path), and round-trip equality
  holds because the builders — `build_frame_entity` / `store_frame`
  / `load_frame`, named per the `store_config` precedent — own
  validate → sort → construct outside the wrapper.
- **`load_frame` fully validates on read**: schema decode, exact-type
  value checks, and canonical-order verification by re-encoding — a
  hand-built/corrupt frame entity is refused, never silently served.
  The typed load itself rejects wrong-type entities (plain
  `ValueError` from the `DataclassEntity` machinery).
- **`TableValue` moved to `model.py`** (frame_encoding needs it and
  must not import `views`); `views.py` re-imports it, so
  `views.TableValue` still resolves.
- Zero-column schemas rejected; control-char rule = code points
  < 0x20 plus 0x7f; tagged nullable form `[name, kind, true]` — all
  as proposed. Golden id pinned
  (`d3gnvkgy…`, `tests/test_frame_encoding.py`); the cbor2
  conformance suite freezes canonical-NaN / `-0.0` / shortest-form /
  int64-boundary behavior as spec.
