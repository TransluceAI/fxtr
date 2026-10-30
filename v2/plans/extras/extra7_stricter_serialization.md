# Extra 7: Stricter mashumaro serialization (`forbid_extra_keys` + `omit_default`)

## Goal

Enable two mashumaro `BaseConfig` options on `DataclassEntity.Config` so they apply to
every entity by default:

- **`forbid_extra_keys = True`** — deserialization rejects keys not defined on the
  dataclass (catches typos, stale/renamed fields, schema drift).
- **`omit_default = True`** — serialization drops any field whose value equals its
  default / default-factory result (smaller, more canonical cbor).

`forbid_extra_keys` is a clean drop-in. `omit_default` is not, because it collides with
the two framework-managed fields and with the bare-nesting safety check. This plan
records why and how we make `omit_default` safe.

## Background: the two framework fields

`DataclassEntity` (in `fxtr2/conversion/converters/mashumaro_entities.py`) carries two
fields every entity inherits:

- `fxtr_type: Final[FxtrID]` — aliased to `$fxtr.type`; also the mashumaro
  `discriminator` field used to pick the concrete subtype on load. Its value is *always*
  equal to the per-subclass default (the type id is fixed per class).
- `fxtr_ctx: ... | RawFxtrEntityMissingMarkerSentinel` — aliased to `$fxtr.ctx`; defaults
  to `MISSING_FXTR_CTX_SENTINEL`. The real marker is injected at *store* time (in
  `_fxtr_convert_to_cbor`), so at `to_dict()` time the value is *always* the sentinel
  default.

Both fields therefore equal their default at serialization time, so naive `omit_default`
would strip `$fxtr.type` (killing the discriminator and the stored type tag) and
`$fxtr.ctx` (so store-time marker injection has nothing to replace).

There is a second, sharper problem at *class-compile* time: `omit_default` makes
mashumaro evaluate every field's default when it generates the packer, and the base
`DataclassEntity.fxtr_type` uses `default_factory=_bad_fxtr_type`, which is *designed to
raise*. With `omit_default` on and `fxtr_type` still a normal mashumaro field, the module
fails to import.

### Why per-field control isn't available

`omit_default` is global in upstream mashumaro — settable only on `Config` (whole class)
or a `Dialect`. There is no per-field "never omit" knob. The only per-field metadata
option is `metadata={"serialize": "omit"}`, which omits a field *unconditionally*. We use
exactly that to take the framework fields out of mashumaro's hands entirely, then inject
them ourselves. (We stay on upstream mashumaro — no fork.)

Confirmed mechanic: a field marked `serialize:"omit"` is skipped in the packer's first
loop, so mashumaro never calls `get_field_default` for it — which also sidesteps the
`_bad_fxtr_type` compile-time crash.

## Design

### 1. Config flags

Add to `DataclassEntity.Config`:

```
forbid_extra_keys = True
omit_default = True
```

`forbid_extra_keys` still accepts `$fxtr.type` / `$fxtr.ctx` on decode because the
`aliases` map recognizes them (verified — they are not treated as "extra").

### 2. Take framework fields out of mashumaro serialization

Add `metadata={"serialize": "omit"}` to all three field definitions of the framework
fields:

- base `fxtr_type` field
- base `fxtr_ctx` field
- the `fxtr_type` field rebuilt in `__init_subclass__` (the `fxtr_type=...` keyword path)

These are *serialize-only* opt-outs. Decode is unaffected: the `aliases` map still routes
`$fxtr.type` / `$fxtr.ctx` to the fields, and the `discriminator` reads `$fxtr.type`
straight from the raw dict.

> Edge case: a subclass that overrides `fxtr_type` as a *manually-declared field* (the
> `elif "fxtr_type" in cls.__dict__` branch in `__init_subclass__`) would need to include
> the metadata itself. This is rare; we leave it as-is and note it. The common
> `fxtr_type=...` keyword form is handled.

### 3. Re-inject framework fields in `__post_serialize__`

Since mashumaro no longer emits `fxtr_type` / `fxtr_ctx` (step 2), put them back in the
`__post_serialize__` hook on `DataclassEntity` — which mashumaro runs after building each
dataclass dict, including for *nested* values and via an *inherited* hook:

```
def __post_serialize__(self, d):
    d["$fxtr.type"] = RawWeakFxtrID(inner_id=self.fxtr_type)
    d["$fxtr.ctx"]  = self.fxtr_ctx     # sentinel for a fresh entity; real marker once stored/loaded
    return d
```

`_fxtr_convert_to_cbor` then only replaces the **top-level** `$fxtr.ctx` *sentinel* with the
destination scope's marker (nested sentinels are left for the bare-nesting check, step 5):

```
cbor = self.to_dict()   # __post_serialize__ has populated $fxtr.type / $fxtr.ctx
if isinstance(cbor.get("$fxtr.ctx"), RawFxtrEntityMissingMarkerSentinel):
    cbor["$fxtr.ctx"] = RawFxtrEntityMarkerID(inner_id=get_active_scope().get_context_marker_id())
```

`RawWeakFxtrID(inner_id=...)` matches exactly what mashumaro used to emit for the `fxtr_type`
field (via `FxtrIDSerializationStrategy(strong=False)`), so the stored bytes are unchanged.
Because the hook runs on every path (`to_dict` and the raw `BasicEncoder`), the framework
fields appear in both — the raw encoder output differs from stored cbor only by the
top-level sentinel→marker replacement (same as before extra7).

### 4. Remove the now-unused mashumaro discriminator

`DataclassEntity.Config` previously declared a mashumaro `discriminator`
(`field="$fxtr.type"`, `include_subtypes=True`, `variant_tagger_fn=...`) so mashumaro could
pick the concrete subtype on decode. **It is dead weight** — fxtr does its own tagged
decoding in `entity_ref_scope._load_entity`:

- `find_handler_or_union_handlers(ref.target_type)` splits a union (e.g.
  `ChildRef[TypeA | TypeB]`) into per-variant handlers,
- `DataclassEntityLoadHandler.supports_entity_type` reads the stored `$fxtr.type` and keeps
  only the variant whose `_fxtr_known_type_id` matches,
- the chosen **concrete** subtype is passed to `from_lowlevel_entity_data`, which builds a
  `BasicDecoder` for it.

mashumaro therefore only ever decodes a concrete type, and a concrete leaf with an
*inherited* discriminator decodes its fields directly (mashumaro checks the discriminator on
the class's *own* `Config`, via `get_discriminator(look_in_parents=False)`). So the
discriminator never fires. Verified: deleting it leaves the full suite green (including the
union-ref tests).

Remove it. The validator it implied already exists: `__post_init__` asserts
`self.fxtr_type == self._fxtr_known_type_id`, and on decode `fxtr_type` is populated from the
`$fxtr.type` alias — so a wrong tag is still caught at construction (and `supports_entity_type`
already pre-filters by tag before decode).

**Why this matters beyond cleanup:** a Config-level discriminator makes *any* entity that
overrides `Config` (for any reason) re-root the discriminated hierarchy at itself and fail to
decode with `SuitableVariantNotFoundError` — because the override puts a `Config` in the
class's own `__dict__` whose inherited `discriminator` is non-None. Removing the base
discriminator eliminates this footgun: the c-node payload entities (below) can opt out of
`omit_default` with a plain `class Config(DataclassEntity.Config): omit_default = False` and
no `discriminator = None` workaround.

Also fix the stale `from_lowlevel_entity_data` docstring, which claimed a `cls.from_dict()`
fallback "which uses mashumaro's discriminator support" — that fallback no longer exists.

### 5. Preserve the bare-nesting safety net (via `__post_serialize__`)

`fxtr_cbor_dumps_with_marker_check` (in `entity_core.py`) detects an illegal *bare-nested*
entity — a `DataclassEntity` embedded directly in another without a `Child[T]` /
`LinkedEntity[T]` wrapper — by finding the nested entity's `$fxtr.ctx` *sentinel* in the
encoded tree. That signal depends on every entity emitting its `$fxtr.ctx`. Once `fxtr_ctx`
is `serialize:"omit"` (step 2), nothing emits it during `to_dict`, so the marker check would
silently pass on a bare-nested entity — silent data corruption (the inner entity is inlined
instead of being stored and referenced).

Fix: re-inject the framework fields in mashumaro's `__post_serialize__` hook on
`DataclassEntity`, instead of only in `_fxtr_convert_to_cbor`:

```
def __post_serialize__(self, d):
    d["$fxtr.type"] = RawWeakFxtrID(inner_id=self.fxtr_type)
    d["$fxtr.ctx"]  = self.fxtr_ctx     # sentinel for a fresh entity; real marker once stored/loaded
    return d
```

The key property (verified): mashumaro runs `__post_serialize__` for **nested** dataclass
values too (and for an *inherited* hook, so defining it once on `DataclassEntity` covers all
subclasses, on both the `to_dict` and `BasicEncoder` paths). So a bare-nested entity gets its
`$fxtr.ctx` *sentinel* re-emitted into its sub-dict. `_fxtr_convert_to_cbor` replaces only the
**top-level** `$fxtr.ctx` sentinel with the destination scope's marker; a nested sentinel
survives and trips `fxtr_cbor_dumps_with_marker_check` → `DuplicateEntityMarkerError`, exactly
as before extra7. `_fxtr_store_impl` attaches the `Child[T]` hint as a note.

This is the **original (pre-extra7) detection mechanism**, restored — the only change is
*where* the marker is emitted (a `__post_serialize__` hook rather than the field default,
which `omit_default` now strips). It needs no per-subclass registration, no mutation of a
shared `serialization_strategy` dict, and no special handling for `Child[T]`-wrapped fields
(those are replaced by a ref during packing, so they never serialize as a nested dataclass and
never carry a sentinel).

## Decode safety — already verified

Spiked the whole design and ran the decode-path test classes: `TypeDiscrimination`,
`TypeParameterPreservation` (incl. union refs), `ComplexFieldTypes`, `InlineEntity*`,
`FxtrCtxCaptureAtStore` — all pass. Union `EntityRef` decoding is unaffected:
`EntityRefSerializationStrategy.deserialize` only stamps the union annotation onto the
`EntityRef`; the concrete type is resolved later at `.load()` via the *referenced*
entity's own `$fxtr.type`. Nothing on the decode path depended on the parent field-level
serialization of `fxtr_type` / `fxtr_ctx`.

## Test updates

Existing tests in `tests/test_conversion/test_mashumaro_entities.py`:

- `_to_cbor` helper: point it at `entity._fxtr_convert_to_cbor()` (under an active scope)
  so it reflects real stored cbor — i.e. the top-level `$fxtr.ctx` sentinel is resolved to
  the scope marker (the raw `BasicEncoder` would leave the sentinel in place). The framework
  fields themselves are now present on both paths thanks to `__post_serialize__`.
- `test_to_fxtr_cbor_has_type` / `test_to_fxtr_cbor_has_ctx`: keep asserting the keys are
  present, now via the convert path.
- `test_nested_entity_without_child_wrapper_raises_with_hint`: unchanged assertions
  (still `DuplicateEntityMarkerError` + the `Child[T]` note, via the marker check).

## New tests

In `tests/test_conversion/test_mashumaro_entities.py`, a `TestStrictSerializationDefaults`
class proving both options are on **by default** for a freshly-defined entity (no per-test
opt-in):

- `forbid_extra_keys`: decoding cbor with an unknown key raises `ExtraKeysError` (or the
  mashumaro extra-keys error type); a clean store/load roundtrip with only known keys
  still works.
- `omit_default`: a field left at its default is absent from the stored cbor; a
  non-default value is present. Verify via the storage-bound cbor (load the stored cbor by
  id, or `_fxtr_convert_to_cbor`).
- Framework fields survive `omit_default`: stored cbor for a default-valued entity still
  contains `$fxtr.type` and `$fxtr.ctx`, and the entity round-trips (discriminator still
  works).
- Bare-nesting still caught: a new entity that bare-nests another raises
  `DuplicateEntityMarkerError` with the `Child[T]` hint; the `Child[T]`-wrapped form
  stores + loads fine.

## Implementation steps

- [x] Add `forbid_extra_keys = True` + `omit_default = True` to `DataclassEntity.Config`.
- [x] Add `metadata=_OMIT_FROM_MASHUMARO` to the framework field definitions (base
  `fxtr_type`, base `fxtr_ctx`, and the `__init_subclass__` `fxtr_type` field).
- [x] Inject `$fxtr.type` / `$fxtr.ctx` via `__post_serialize__` (fires for nested entities
  too); `_fxtr_convert_to_cbor` does the top-level sentinel→scope-marker replacement, and the
  bare-nesting marker check in `fxtr_cbor_dumps_with_marker_check` stays the detector.
- [x] Remove the mashumaro `discriminator` from base `Config`; fix the stale
  `from_lowlevel_entity_data` docstring.
- [x] Update the existing affected tests (`_to_cbor` helper now routes through
  `_fxtr_convert_to_cbor`; the `has_type` / `has_ctx` / roundtrip tests pass via it).
- [x] Update CODE_OVERVIEW.md (`mashumaro_entities.py` section).
- [x] Add `TestStrictSerializationDefaults` new tests.
- [x] Opt c-node context payload entities out of `omit_default` (see note below).
- [x] Run the full v2 suite (`uv run pytest -n auto`) → 2678 passed.

## Things hit during implementation

- **c-node payload entities need `value` always present.** `MemoryStore.create_c_node`
  requires the context cbor to carry a `"value"` field. Three context entities —
  `FallbackContextMarker` (`system_entities.py`), `NixInputAddressedBuildProvenance` and
  `NixFixedOutputBuildProvenance` (`nix_build_provenance.py`) — declare
  `value: FxtrID = field(default_factory=_ground_fxtr_id)`, which is *always* the ground
  default, so `omit_default` stripped it and broke c-node minting (10 test failures).
  Fix: each opts out via `class Config(DataclassEntity.Config): omit_default = False`.
- **The discriminator-removal (design step 4) was what made that opt-out clean.** With the
  base discriminator still present, the bare `class Config(...)` override re-rooted the
  discriminated hierarchy at the leaf and broke its decode with
  `SuitableVariantNotFoundError`, which would have forced a `discriminator = None` workaround
  on every `Config` override. Removing the (unused) discriminator dissolved that whole class
  of footgun — so the c-node overrides are now just `omit_default = False`.

## Open questions / things to revisit during review

- **Blast radius of `omit_default`** across real entities (services, reports, channels,
  system entities): any code that reads stored cbor and *expects* a default-valued key to
  be physically present would break. Most consumers go through decode (which re-applies
  defaults), but raw-cbor inspectors (`UnknownEntity.raw_cbor`, debug tooling, cross-
  language consistency fixtures) may see fewer keys. Worth a scan during review.
- **Cross-language consistency** (`tests/test_cross_language/`): if any pinned/byte-equal
  fixtures encode default-valued fields, they may shift. Check and re-pin if needed.
- Whether to also enable these on `DataclassFxtrSubpart` (subparts reuse only
  `serialization_strategy`, not the full Config, so they are *not* covered here). Left out
  for now — subparts have no framework fields; revisit if we want omit there too.

## Follow-up: `omit_default` reverted to off-by-default

After landing the above, we decided **not** to enable `omit_default` globally. `forbid_extra_keys`
stays on by default, but `omit_default` is now *off* on `DataclassEntity.Config` — default-valued
fields are serialized like any other. The splicing machinery is kept: an individual entity can opt
in with `class Config(DataclassEntity.Config): omit_default = True`, and the framework-managed
fields survive the opt-in via `__post_serialize__` (they carry `_OMIT_FROM_MASHUMARO`). Because the
base is off, the c-node payload entities (`FallbackContextMarker`, `NixInputAddressedBuildProvenance`,
`NixFixedOutputBuildProvenance`) no longer need their `omit_default = False` overrides, which were
removed. This also moots the two "open questions" above about `omit_default` blast radius.
