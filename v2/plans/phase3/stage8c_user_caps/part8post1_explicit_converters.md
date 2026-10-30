# Part 8 post-1 — Explicit converters for typed-service users

## Goal

Expose two pieces of the typed-service conversion machinery directly to
users so they can hook into encoding / decoding from inside service
methods or helper functions, without having to declare every type up
front on the Protocol:

1. `FxtrServiceData` becomes a usable annotation on service-method
   parameters and return values. A field annotated `FxtrServiceData`
   skips dialect-level conversion entirely — the value flows through
   in untyped form. This lets a typed Protocol mix typed and untyped
   slots in the same signature (e.g. `async def foo(x: EntityRef[T],
   payload: FxtrServiceData) -> FxtrServiceData`).
2. New module-level helpers `encode_to_service_data` and
   `decode_from_service_data` expose the same per-annotation
   encode/decode the typed-service layer uses internally, so callers
   can dynamically convert values when the static annotation isn't
   available (e.g. the impl method took `payload: FxtrServiceData`
   but at runtime knows the payload is supposed to be `EntityRef[T]`).

Together these let a typed service method take an untyped chunk of
data, then dynamically re-encode/decode parts of it against types
chosen at runtime.

## Design

### Part A — `FxtrServiceData` as a passthrough strategy

Mashumaro's strategy lookup on a field's annotation goes through
`dialect.serialization_strategy.get(ftype)` *before* unwrapping a
`TypeAliasType` to its `__value__`. So we just add a single dict
entry on `FxtrServicesDefaultDialect`:

```
serialization_strategy = {
    ...
    FxtrServiceData: mashumaro.helper.pass_through,
    ...
}
```

This makes mashumaro short-circuit the field — the value is encoded
and decoded as-is, regardless of where `FxtrServiceData` appears
(top-level annotation, inside `tuple[FxtrServiceData, ...]` for
`*args: FxtrServiceData`, inside `dict[str, FxtrServiceData]` for
`**kwargs: FxtrServiceData`, etc.).

Because `pass_through` skips the conversion layer entirely, the user
is responsible for ensuring the value is `FxtrServiceData`-shaped
when they choose this annotation. Validation against the
`FxtrServiceData` vocabulary still happens later in
`invoke_service_method`, so a malformed value surfaces with the
existing error rather than going silently to the wire.

**Edge case to verify in tests:** parameters with defaults end up
typed as `anno | _UseRemoteDefault` in the wrapper dataclass. With
`anno = FxtrServiceData`, the resulting union is
`FxtrServiceData | _UseRemoteDefault`. Both members resolve to
`pass_through` strategies, and mashumaro's union packer collapses
to the shortcut path (`len(packers) == 1 and packers[0] == "value"`),
so the value flows through. Add a test that exercises an optional
`FxtrServiceData` parameter with `USE_REMOTE_DEFAULT` semantics to
catch any regression here.

### Part B — Module-level encode / decode helpers

Two new functions in `service_dialect.py`:

```
def encode_to_service_data(
    value: Any,
    annotated_type: Any,
    *,
    dialect: type[mashumaro.dialect.Dialect] = FxtrServicesDefaultDialect,
) -> FxtrServiceData: ...

def decode_from_service_data(
    value: FxtrServiceData,
    annotated_type: Any,
    *,
    dialect: type[mashumaro.dialect.Dialect] = FxtrServicesDefaultDialect,
) -> Any: ...
```

Implementation notes:

- Hand `annotated_type` directly to `BasicEncoder` / `BasicDecoder`
  (via the existing `get_basic_encoder` / `get_basic_decoder` cache).
  No shell wrapper. The typed-service layer wraps return values in a
  single-field dataclass for its own reasons (TypeVar substitution
  via `__parameters__` on the wrapper, plus uniform handling of the
  args+return pair); none of that applies here, since the caller
  hands us a fully-resolved annotation. The codec cache key is just
  `(annotated_type, dialect)`.

Validation:

- `encode_to_service_data` runs `_validate_fxtr_service_data` on the
  encoded result before returning. This catches the case where the
  user's annotation didn't capture some type that mashumaro then
  encoded into a non-`FxtrServiceData` shape. Promote
  `_validate_fxtr_service_data` (and its `_NotFxtrServiceDataError`)
  to public names — drop the underscore prefix or add explicit
  re-exports — since this validation is now reachable from the
  user-facing helpers.
- `decode_from_service_data` runs `_validate_fxtr_service_data` on
  the *input* before invoking the decoder, so garbage input fails
  with the standard error rather than a deep mashumaro stack trace.

### Part C — Re-export

Re-export `FxtrServiceData` from `service_dialect.py` so users have a
stable import path next to the helpers:

```
from fxtr2.services.service_dialect import (
    FxtrServiceData,
    encode_to_service_data,
    decode_from_service_data,
)
```

`service_dialect.py` already imports `FxtrServiceData` indirectly
through `service_cap_common`'s `_UseRemoteDefault` import — make the
import explicit and add it to the module's public surface.

Don't touch `fxtr2/services/__init__.py` for now; that's a
follow-up if and when we decide to flatten the public API.

## Implementation steps

1. [x] Add `FxtrServiceData: mashumaro.helper.pass_through` to
   `FxtrServicesDefaultDialect.serialization_strategy` in
   `service_dialect.py`. Update the dialect's docstring to mention
   the passthrough behavior.
2. [x] Implement `encode_to_service_data` and
   `decode_from_service_data` in `service_dialect.py`. Each fetches
   the codec via `get_basic_encoder` / `get_basic_decoder` keyed on
   `(annotated_type, dialect)` and runs validation in the
   appropriate direction.
3. [x] Promote `_validate_fxtr_service_data` and
   `_NotFxtrServiceDataError` to public names in
   `service_cap_common.py` (renamed by dropping the underscore prefix).
   Updated internal callers.
4. [x] Re-export `FxtrServiceData` from `service_dialect.py` (explicit
   import + mention in module docstring).
5. [x] Tests in `tests/test_services/`:
   - Method with mixed annotations (`EntityRef[T]` + `FxtrServiceData`).
   - `*args: FxtrServiceData` and `**kwargs: FxtrServiceData`.
   - Optional `payload: FxtrServiceData = USE_REMOTE_DEFAULT`.
   - Dynamic decode inside the impl using `decode_from_service_data`.
   - `encode_to_service_data` / `decode_from_service_data` round-trip
     for `EntityRef[T]`, `frozenset[int]`, `tuple[FxtrID, ...]`,
     plain dataclass.
   - Malformed-input rejection (decode side and encode side).
   - Custom `dialect=` override.
   - Codec-cache reuse across calls.
6. [x] Update `LOCAL_CODE_OVERVIEW.md` and `CODE_OVERVIEW.md`.

## Open risks / follow-ups

- The dialect-level `FxtrServiceData` strategy relies on mashumaro
  performing a direct `dialect.serialization_strategy.get(ftype)`
  lookup before unwrapping the alias to `__value__`. Verified by
  reading `mashumaro/core/meta/code/builder.py::iter_serialization_strategies`
  and `pack.py::pack_default` — the strategy check (line ~146 in
  `pack.py`'s `get_overridden_serialization_method`) precedes the
  TypeAliasType unwrap (line ~555). The
  `TestFxtrServiceDataPassthrough` tests in `test_service_dialect.py`
  pin this so a future mashumaro upgrade that reorders the dispatch
  chain (and silently expands `FxtrServiceData` to its union members)
  fails loudly.

## Notes from implementation

- **Union ordering matters in `pack_union`.** First attempt at the
  optional `FxtrServiceData` parameter case crashed inside mashumaro:
  `pack_union` calls `issubclass(get_type_origin(arg), Collection)`
  for each member, and the `TypeAliasType` origin of
  `FxtrServiceData` isn't a class. Crucially, the check only runs
  the *first time* `pack_union` sees each unique packer
  (`if packer not in packers:`). Both `_UseRemoteDefault` (passthrough)
  and `FxtrServiceData` (passthrough) resolve to the same `"value"`
  packer, so changing the widened union from
  `anno | _UseRemoteDefault` to `_UseRemoteDefault | anno` makes
  `_UseRemoteDefault` (a real class) trigger the check, and the
  subsequent `FxtrServiceData` member rides along without
  re-triggering it. No special-casing on `anno`. Applies to all
  optional params, not just `FxtrServiceData` ones.
- **Why bare-scalar `BasicEncoder`/`BasicDecoder` work.** Sanity check
  during development confirmed `BasicEncoder(int, ...).encode(42)` /
  `BasicDecoder(EntityRef[T], ...).decode(fid)` etc. all behave
  cleanly without a wrapper-shell intermediary, validating the
  decision to drop the wrapper from the design.
