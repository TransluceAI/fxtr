# Chunk 3 — view layer: shadows + true specs

Scope: make `views.py` objects real, per the opaque-token design.

- Split `view_spec.py` (explicitly a rewritable prototype) into:
  runner-side **true specs** (absolute paths; the canonicalizer stays
  here) vs task-side **shadows** (axes/kinds/mode/schema/outputs + the
  accumulated pure-rewrite state: renames are local, `at` accumulates
  positional bindings, `read_only` downgrades mode).
- Wire `views.py` classes over (backing capability, `ViewGrant`, shadow):
  pure rewrites fully local; effectful ops resolve local names →
  positions and emit `ViewSel`s; `open_*` returns new views from returned
  grants (prefix names overlaid from the holder's local names).
- Runner-side grant bookkeeping: token ↔ true spec map per run; rebuild a
  spec from (token, ViewSel) for ops and for `open_task` wiring
  (`WiredArg` → canonicalizer input, incl. effective mode validation).

Tests: rewrite semantics against the existing `test_view_spec` scenarios
(now through view objects), single-cell resolution errors, subtree
full-frame behaviors, mode downgrade + upgrade-rejection.

Notes/log:
- Done (`view_runtime.py` — Shadow + `ViewBackend` seam + concrete `Runtime*View`
  subclasses of the `views.py` contract classes + `build_view`;
  `spec_registry.py` — token↔true-spec registry, `sliced`/`wired_spec`
  translation, `open_task` → the *unchanged* canonicalizer;
  `tests/test_view_runtime.py`, 5 tests / 49 total green).
- Shadows came out template-free as predicted: grant + open axes (original
  positions + local names) + accumulated binds + mode + selected output.
- Concrete-subclass approach (Rt* over the contract classes) instead of
  editing `views.py` bodies — contracts/docstrings stay put; task code
  types against the bases.
- **Design finding, round 1 (superseded)**: a renamed subtree view
  false-collided runner-side; first fix shipped the holder's local names
  with every creation op.
- **Design finding, round 2 (final — promoted to the discussion log,
  2026-07-04)**: user review identified the cleaner partition — resolve the
  strip-set *holder-side* and make the wire purely positional.
  ``WiredArg.wiring`` = per-open-axis instructions (Consume(p) over the
  concatenated holder-visible child coordinates / KeepName); ``wire_by_names`` is the strip-set as a pure
  holder-side function shared by the view layer and the spec-level tests;
  ``canonicalize_open_task`` takes ``WiredSpecArg``s and does no name
  resolution; ``local_names`` left the wire; subtree params are bare tokens
  again; ``open_child`` rejects duplicate *own*-axis names but allows
  cross-level overlap (per-grid uniqueness; registry binds positionally). Only
  identity-bearing names (coordinate officials, received names) cross the
  wire.
- Frame view ops remain unimplemented (v1 cut); `run_step` passes through
  to the backend (impl in chunk 7).
