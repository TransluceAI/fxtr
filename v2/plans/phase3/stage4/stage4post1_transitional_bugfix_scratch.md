# Scratch notes: Transitional entity bugfix

## The problem

`TransitionalUpgradedEntity` inherits from `SelfLoadingLegacyEntity`, which brings in 3 abstract
methods (`_to_fxtr_cbor`, `_from_fxtr_cbor`, `_fxtr_type_id`). These are never implemented by the
concrete transitional classes (`TaskWithPredecessors`, `ImplProvenanceSet`, `RunWithPredecessors`),
which means:

1. **Python's ABC prevents instantiation** — all three classes have unresolved `__abstractmethods__`:
   `{'_from_fxtr_cbor', '_fxtr_type_id', '_to_fxtr_cbor'}`. Confirmed at runtime.
2. **`_fxtr_type_id()` is called by `UpgradeCache`** — so even if we could instantiate them, this
   call would hit the abstract method and raise `NotImplementedError`.
3. **`_to_fxtr_cbor` / `_from_fxtr_cbor` are dead code** — `DataclassEntity._fxtr_store_impl` is
   used instead (via MRO), and `DataclassEntityLoadHandler` handles deserialization.

Separately, `DataclassEntity._fxtr_store_impl` returns `EntityRef[DataclassEntity]` instead of
`EntityRef[Self]`. This conflicts with `SelfLoadingLegacyEntity._fxtr_store_impl`'s `EntityRef[Self]`
return type, causing 3 pyright `reportIncompatibleMethodOverride` errors.

## Why it hasn't been caught

The `run_demo.py` example exercises this code path but isn't run as part of CI tests. No unit test
exists for transitional rundata. The code was likely written before the entity registry refactor
(stage4) changed the inheritance hierarchy.

## Analysis of what SelfLoadingLegacyEntity provides

From SelfLoadingLegacyEntity, TransitionalUpgradedEntity inherits:
- `_fxtr_store_impl` (concrete) → **shadowed by DataclassEntity**, not needed
- `_to_fxtr_cbor` (abstract) → **dead code**, causes ABC bug
- `_from_fxtr_cbor` (abstract) → **dead code**, causes ABC bug
- `_fxtr_type_id` (abstract) → **needed by UpgradeCache**, but abstract = broken
- `_fxtr_immutable` (concrete, returns False) → **not needed**, DataclassEntity path handles caching differently

Conclusion: `SelfLoadingLegacyEntity` provides nothing useful to `TransitionalUpgradedEntity`.

## What UpgradeCache needs from entities

`UpgradeCache.save_upgraded(entity: TransitionalUpgradedEntity | LegacyCompat[Any])` calls:
- `type(entity)._get_type_name_for_cache()` — defined on both
- `type(entity)._fxtr_type_id()` — defined on LegacyCompat, abstract/broken on TransitionalUpgradedEntity
- `entity._get_legacy_cid()` — defined on both
- `entity.store()` — from Entity base class

`UpgradeCache.lookup_upgraded(legacy_cid, as_type)` calls:
- `as_type.__name__`
- `as_type._fxtr_type_id()`

## Fix approach

1. Remove `SelfLoadingLegacyEntity` from `TransitionalUpgradedEntity`'s bases → just `Entity`
2. Add `_fxtr_known_type_id: ClassVar[FxtrID | None]` and a concrete `_fxtr_type_id()` to
   `TransitionalUpgradedEntity` (delegates to `_fxtr_known_type_id`, set by DataclassEntity.__init_subclass__)
3. Change `DataclassEntity._fxtr_store_impl` return type to `EntityRef[Self]`

## Potential concerns

- **`_fxtr_immutable`**: Only matters for `SelfLoadingLegacyEntityLoadHandler`, which won't match
  transitional entities anymore (correct — DataclassEntityLoadHandler should handle them).
- **Handler matching**: `SelfLoadingLegacyEntityLoadHandler.supports_entity_type` checks
  `issubclass(base_type, SelfLoadingLegacyEntity)`. After the change, transitional entities won't
  match, and will correctly fall through to `DataclassEntityLoadHandler`.
- **UpgradeCache type annotations**: Both `TransitionalUpgradedEntity` and `LegacyCompat` will have
  `_fxtr_type_id`, `_get_type_name_for_cache`, `_get_legacy_cid`, and `store()`. Union type should
  satisfy pyright.
