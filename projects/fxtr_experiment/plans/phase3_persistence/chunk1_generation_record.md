# Chunk 1 — the generation-record entity

**Status: complete.** (214 passed + pyright clean: `model.
GenerationRecord` + the fact types; `encoding.GenerationRecordEntity`
with `canonical_generation_record` and the strict tree decode;
`WorkflowSession.build_generation_record` (one-transaction assembly,
kind codes → name literals at the boundary, at-rest statuses only) +
`load_generation_record`; the checkpoint path persists step type
entities (`put_checkpoint` and the engine's `record_checkpoint` call
site) — the extraction-contract TODO in `notes/at_rest_format.md` is
resolved; `tests/test_generation_record.py` covers assembly round-trip,
order-insensitive fingerprints with failures order identity-bearing,
the at-rest-only refusal, and strong-ref closure across a session
reopen.) Notes at the bottom.

The canonical "everything this generation owns" entity in
`encoding.py`, per the chunk-0 schema. No storage-schema change yet
(that is chunk 2); this chunk is the encoding plus the two conversions,
unit-tested in isolation.

## Scope

- The entity type beside `GridConfigEntity`/`SliceTemplateEntity`/
  `StepPayloadEntity`, using the same field-strategy / canonical-CBOR
  machinery: byte determinism, id = fingerprint.
- **Embedded ids become strong refs** (the existing `encoding.py`
  convention), so persisting one record pins its whole closure —
  results, observed fingerprints, fn handles, checkpoint payloads.
- **Assemble-from-rows**: build the record from a generation's
  session-level rows (results, failures, observations ⋈ versions,
  assertions, request edges, checkpoints), canonically ordered
  (sorted sets for the record-derived facts; failures keep insertion
  order; results keyed by name).
- **Decode-to-rows**: the inverse, producing the row data an import
  inserts (exec_ids/version_ids assigned fresh by the caller).
- **The `step_type_id` extraction-contract fix** (chunk-0 finding):
  `put_checkpoint`/`record_checkpoint` persist the step's type entity
  (loadable from the dispatch chain — the runner scope includes
  GLOBAL_SCOPE) before recording, making every id column a fetchable
  root; remove the TODO in `notes/at_rest_format.md`.

## Tests

- Round trip: rows → entity → rows is the identity (modulo the
  bookkeeping ids).
- Determinism: same rows in any insertion order → the same entity id;
  failures' order preserved.
- Closure: persisting a record pins its referenced ids (loadable from
  a fresh scope over the store).
- Representative variety: complete with results/checkpoints; errored
  with failure rows; interrupted with unresolved request edges.

Notes/log (2026-07-09, at landing):

- **Canonical order lives at the entity boundary**: the serialization
  strategy canonicalizes on both serialize and deserialize
  (`encoding.canonical_generation_record`), so equal fact sets yield
  equal *fingerprints* regardless of construction order — but two
  in-memory records differing only in order are `!=` until
  canonicalized. Compare fingerprints (or canonicalize first);
  `build_generation_record` returns the canonical form already. SQL
  `ORDER BY` is deliberately not relied on for the set-valued fields
  (blob ordering ≠ `id_string` ordering); only failures use it
  (insertion order via ``failure_id``).
- `model.ObservationKind` already existed (the currency model's
  literal, same three names) — reused rather than duplicated; only
  `RequestKind`/`FailureKind` are new, with the int↔name maps beside
  the storage constants (`storage.OBS_KIND_NAMES` etc. — the one
  conversion point; records never hold the integers).
- The closure test pins the semantics precisely: an id embedded in a
  record is *not* individually pinned (`ensure_loadable` raises before
  the record is loaded) but rides the record's strong-ref closure
  (loadable through the entity scope after one load) — exactly the
  extraction contract's "id columns are fetchable roots, everything
  else travels inside closures".
