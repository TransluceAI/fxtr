# Chunk 1 — canonical encoding layer

Scope: the identity conversions everything else keys on.

- Canonical JSON codecs for storage identity columns: key-paths (arrays of
  key strings; step kinds derivable, not stored) and flattened coord tuples
  (arrays of str|int). Strict encoding (no spaces, sorted keys), injective,
  bidirectional with the model types (`GridPath`/`CellRef` ↔ storage form —
  a repacking: per-step cells fold into the flattened tuple).
- Canonical **entity encoding** for the blobby identity-bearing values:
  grid configs (`GridConfig` union → entity; its id is the `grid_config`
  fingerprint), target slice templates.
- Fingerprint helpers (nothing clever: fingerprints are entity ids).

## Decisions (made at implementation start)

- **Context-free codecs.** Encoding model → storage text is pure; decoding
  back to *typed* model paths needs context (step kinds from parent grid
  kinds; per-grid arities to split flattened coords), so chunk 1 decodes to
  raw tuples only and typed reconstruction belongs to the state store
  (chunk 2).
- **Hand-rolled, explicitly tagged payload encoding** (mashumaro/service-
  dialect codec rejected for this): the model's untagged unions are
  ambiguous under try-in-order decoding — Data/Handle/Checkpoint/Subtree
  configs are all shape-identical (`axes` only), and
  `SubgridStepTemplate` / `InternalsStepTemplate` have identical fields —
  so faithful decoding requires tags anyway. More fundamentally, these
  encodings are *fingerprints* (identity): they must never change out from
  under us via a codec-library behavior change, so the format is a small
  explicit function we own. Format: compact JSON with array tags
  (`["lit",v] | ["param",i] | ["keep"]`; steps `["sub"|"int", cells, key]`;
  configs `{"kind": ...}`), canonical flags (sorted keys, no spaces,
  `allow_nan=False`).
- **Entity forms hold the actual model object** (`GridConfigEntity.config`
  / `SliceTemplateEntity.template`, via field-level
  ``SerializationStrategy``): the strategy produces the tagged JSONable
  tree, and fxtr's *canonical entity CBOR* (sorted map keys) supplies byte
  determinism — no JSON-string step, no double marshalling. (Refined from
  an initial ``payload: str`` design after review: canonical *strings* are
  only needed for state-db identity columns; config/slice comparison is
  always by entity id.) Decoders are container-tolerant
  (Mapping/Sequence, not dict/list) for CBOR-roundtrip shapes.
- Coord values are validated by *exact* type (`type(x) is int` — rejects
  `bool`, a subclass of int; floats rejected).
- Consequence for `model.py`: the "untagged union, X must stay last for
  codec purposes" notes are obsolete — replaced with pointers to the tagged
  encoding.

Tests: codec roundtrips (incl. escaping-hostile keys/coords), injectivity
(`"1"` vs `1`), bool/float rejection, parent-path helper + root edge,
model↔storage repacking of CellRefs through internals steps, config
encoding determinism (same → same id via store), distinctness across
shape-identical kinds (data vs handle with equal axes), full roundtrips for
all six config kinds and for slice templates with internals steps (the
ambiguity case that forced tags).

Notes/log:
- Done (`fxtr_workflow_3/encoding.py`, `tests/test_encoding.py`, 16 tests;
  incl. async store→load→store roundtrip and a frozendict-insertion-order
  fingerprint-stability test that empirically pins the canonical-CBOR
  sorted-keys assumption).
- The "what lives where" map (state-db columns vs entity-encoded vs
  never-stored) is in `encoding.py`'s module docstring.
- The store-side fingerprint helpers (`store_config` / `store_slice`) store
  into the *active scope* and rely on content addressing for determinism;
  the state store (chunk 2) will route them through the entity store +
  pin-per-id.
- `model.py`'s two "untagged union, keep X last for codec purposes" notes
  replaced with pointers to the tagged encoding.
- Payload-entity `payload: str` (JSON-in-CBOR) noted as revisable while
  nothing durable exists; changing it changes fingerprints.
