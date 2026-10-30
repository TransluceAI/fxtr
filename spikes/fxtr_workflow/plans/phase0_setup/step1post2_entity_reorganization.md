# Step 1 Post-2: Entity Reorganization

## Goal

Clean up entity types: consolidate them into one file, stop using `DataclassEntity` for types that don't need to be independent entities, add indirection entities for lazy-loading slot deps and results, use `EntityRef[T]` instead of bare `FxtrID` where the referenced type is known, make `LiveCacheSlot` store `EntityRef` state instead of loaded entities, tighten `Any` types, and ensure all entity contents are immutable.

## Changes

### 1. New file: `entities.py`

Move all entity type definitions into a single `entities.py` file. Currently they're split across `cache_slot.py` (slot state + tracking types) and `resolve.py` (`CacheKeyEntity`). Consolidating makes it easier to see all the persisted types at a glance.

The shared namespace UUID `_NS` moves here too.

### 2. Demote tracking types to plain dataclasses

`ForeignSlotRef`, `InternalSlotRef`, `ForeignArgMapping`, and `TrackedForeignRun` are never referenced independently and don't need content-addressed hashes. They're always stored inline as part of a containing entity.

Change them from `DataclassEntity` (with `fxtr_type=...`) to plain `@dataclasses.dataclass(frozen=True)` with `mashumaro.DataClassDictMixin`. This means:
- No `fxtr_type` registration
- No `Child[T]` wrappers needed — they serialize inline as plain dicts
- `TrackedForeignRun.run_location` changes from `Child[ForeignSlotRef] | Child[InternalSlotRef]` to `ForeignSlotRef | InternalSlotRef`
- `TrackedForeignRun.foreign_arg_mappings` changes from `list[Child[ForeignArgMapping]]` to `tuple[ForeignArgMapping, ...]`

For the `run_location` union field: add a literal `kind` field to each (`kind: Literal["foreign"]` and `kind: Literal["internal"]`) and configure mashumaro's `Discriminator` on the union.

### 3. Immutability

All entity and mixin fields use immutable collections:
- `list[T]` → `tuple[T, ...]`
- No dicts in structural fields currently, so no `frozendict` needed for those

Fields affected:
- `ForeignSlotRef.subslot_path`: `list[str]` → `tuple[str, ...]`
- `InternalSlotRef.subslot_path`: `list[str]` → `tuple[str, ...]`
- `ForeignArgMapping.subslot_path`: `list[str]` → `tuple[str, ...]`
- `TrackedForeignRun.foreign_arg_mappings`: → `tuple[ForeignArgMapping, ...]`
- `TrackedForeignSlotDeps.runs`: → `tuple[TrackedForeignRun, ...]`

Callers that currently build these as lists (e.g. `TracingCacheSlot.subslot` extending `subslot_path`, `ForeignRunTracker.build_slot_deps` building lists of mappings) will need to produce tuples instead.

### 4. Tighten `Any` types: cache key args

Currently `CacheKeyEntity.args_data: Any` and `_serialize_for_cache_key` uses magic dicts (`{"$placeholder": "cache_slot"}`, `{"$step_handle": identity}`) that could collide with user data.

Replace with proper entity marker types:

```python
@dataclasses.dataclass(frozen=True)
class CacheSlotMarker(DataclassEntity, fxtr_type=...):
    """Singleton marker for cache slot placeholders in cache keys."""

@dataclasses.dataclass(frozen=True)
class StepHandleMarker(DataclassEntity, fxtr_type=...):
    """Marker for step handle references in cache keys."""
    step_identity: FxtrID
```

Define a frozen recursive type alias for serialized cache key data:

```python
type FrozenCacheKeyData = (
    int | str | bool | bytes | float | None
    | EntityRef[CacheSlotMarker]
    | EntityRef[StepHandleMarker]
    | tuple[FrozenCacheKeyData, ...]
    | frozendict[str, FrozenCacheKeyData]
)
```

Then `CacheKeyEntity.args_data: FrozenCacheKeyData` instead of `Any`.

`_serialize_for_cache_key` needs a `scope` parameter threaded through from `compute_cache_key` (which already has one) so it can `.store()` the marker entities. It returns `FrozenCacheKeyData` and produces tuples/frozendicts instead of lists/dicts.

### 5. Tighten `Any` types: step results

Currently `SlotFinished.result: Any` holds user result data validated by `_validate_result`. Wrap it in its own entity behind an `EntityRef` so that:
- The result's mutability is safely contained (only loaded when reading from cache)
- `SlotFinished` itself stays fully immutable and hashable

```python
@dataclasses.dataclass(frozen=True)
class StepOutput(DataclassEntity, fxtr_type=...):
    """Wrapper entity for step result data."""
    value: SerializableTypes
```

`SlotFinished` stores `result_ref: EntityRef[StepOutput]` instead of `result: Any`.

Note: `StepOutput.value` uses `SerializableTypes` which currently includes mutable `list`/`dict`. This is OK — the entity is behind a ref, so its Python-level mutability doesn't affect the containing `SlotFinished`'s hashability. The content hash is computed from the serialized CBOR, not the Python object.

### 6. New entity: `TrackedForeignSlotDeps`

```python
@dataclasses.dataclass(frozen=True)
class TrackedForeignSlotDeps(DataclassEntity, fxtr_type=...):
    runs: tuple[TrackedForeignRun, ...]
```

This wraps the tracked runs as its own content-addressed entity, so it can be lazy-loaded separately from the `SlotFinished` that references it.

### 7. Update `SlotFinished`

```python
@dataclasses.dataclass(frozen=True)
class SlotFinished(DataclassEntity, fxtr_type=...):
    cache_key_ref: EntityRef[CacheKeyEntity]
    result_ref: EntityRef[StepOutput]
    slot_deps_ref: EntityRef[TrackedForeignSlotDeps]
```

### 8. Update `SlotPartial`

```python
@dataclasses.dataclass(frozen=True)
class SlotPartial(DataclassEntity, fxtr_type=...):
    cache_key_ref: EntityRef[CacheKeyEntity]
```

### 9. Replace `FxtrID` with `EntityRef[T]` where type is known

- `SlotPartial.cache_key_id` and `SlotFinished.cache_key_id`: → `EntityRef[CacheKeyEntity]`
- `TrackedForeignRun.observed_finished_id`: → `EntityRef[SlotFinished]`
- `CacheKeyEntity.step_identity`: stays as `FxtrID` — opaque user-provided key

### 10. `LiveCacheSlot` stores `EntityRef` state

Currently `LiveCacheSlot` stores the loaded entity directly in `_loaded_state` and re-stores it to get its ID (e.g. in `build_slot_deps` and `replay_check`). Instead:

- `_loaded_state` becomes `_state_ref: EntityRef[SlotSplit | SlotPartial | SlotFinished] | None`
- `_load_state()` calls `await self._state_ref.load()` to get the entity (EntityRef caches the loaded value)
- `_save_state()` stores the entity, keeps the `EntityRef`
- `build_slot_deps` reads `entry.inner_slot._state_ref` to get the entity ID directly — no re-storing
- `replay_check` reads `current_slot._state_ref.entity_id` to compare — no re-storing

### 11. Update consumers

**`ForeignRunTracker.build_slot_deps`** (`cache_slot.py`):
- Gets `observed_finished_ref` from `entry.inner_slot._state_ref` directly instead of re-storing
- Returns `tuple[TrackedForeignRun, ...]` (caller wraps in `TrackedForeignSlotDeps`)

**`_execute_in_slot`** (`entrypoint.py`):
- `cache_key_ref` comparison: compare `state.cache_key_ref.entity_id == cache_key_ref.entity_id`
- `slot_deps` access: `await state.slot_deps_ref.load()` to get `TrackedForeignSlotDeps`, then `.runs`
- Building `SlotFinished`: `.store()` both `StepOutput` and `TrackedForeignSlotDeps`, pass their refs to `SlotFinished`
- `_validate_result` stays as-is (validates before wrapping in `StepOutput`)
- Result retrieval: `await state.result_ref.load()` then `.value`

**`replay_check`** (`cache_slot.py`):
- `observed_finished_ref.entity_id` comparison against `current_slot._state_ref.entity_id`
- Recursive call: `await current_state.slot_deps_ref.load()` before recursing

**`_serialize_for_cache_key`** (`resolve.py`):
- Takes a `scope` parameter
- `CacheSlot` → `CacheSlotMarker().store(scope=scope)` (returns EntityRef)
- `StepHandle` → `StepHandleMarker(step_identity=step.identity).store(scope=scope)` (returns EntityRef)
- `list` → `tuple(...)`, `dict` → `frozendict(...)`
- Returns `FrozenCacheKeyData` instead of `Any`

## Serialization risk

The `FrozenCacheKeyData` type alias mixes primitives, `EntityRef`, `tuple`, and `frozendict` in a recursive union. This _should_ serialize correctly via mashumaro + fxtr2's CBOR encoding (EntityRefs become tagged CBOR links), but it's possible we'll hit limitations in fxtr2's serialization layer. If we do, Daniel will step in to fix the fxtr2 side.

## Implementation Steps

- [x] 1. Create `entities.py` with all entity types moved/refactored: discriminator `kind` fields, `DataClassDictMixin` for tracking types, marker entities, `StepOutput`, `TrackedForeignSlotDeps`, `EntityRef[T]` fields, immutable collections, `FrozenCacheKeyData` type alias.
- [x] 2. Update `cache_slot.py`: remove entity type definitions (import from `entities.py`), refactor `LiveCacheSlot` to store `EntityRef` state, update `ForeignRunTracker.build_slot_deps` and `replay_check`.
- [x] 3. Update `resolve.py`: remove `CacheKeyEntity` (import from `entities.py`), thread `scope` through serialization, produce `FrozenCacheKeyData`.
- [x] 4. Update `entrypoint.py`: adapt `_execute_in_slot` for `EntityRef` comparisons, `StepOutput`/`TrackedForeignSlotDeps` loading/storing.
- [x] 5. Update `step_context.py` and `cache_slot.py` callers: produce tuples for `subslot_path`, etc.
- [x] 6. Update all imports in `__init__.py` and tests.
- [x] 7. Run tests, fix any issues.
- [x] 8. Update CODE_OVERVIEW.md.
