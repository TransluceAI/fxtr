# Stage 4: Entity Registry Refactor

This document describes the entity serialization/deserialization refactor that replaces the old `entity_serdes.py` + `fxtr_scope.py` split with a unified `entity_ref_scope.py` and a separate `conversion/` module tree.

## Motivation

The old system had several pain points:

1. **Unclear responsibility split**: `EntityRef` and `FxtrScope` were highly interdependent, but their logic was spread across two files (`entity_serdes.py` and `fxtr_scope.py`) with no clear boundary between them.

2. **Global caching was hard to reason about**: Entity caching was global (`_GLOBAL_ENTITY_CACHE`), which meant cache lifetimes were decoupled from scope lifetimes. This made it difficult to reason about when cached values were safe to use, and required careful side-effect replay logic.

3. **Serialization coupled to base class**: The old `Entity` ABC combined serialization (`_to_fxtr_cbor`), deserialization (`_from_fxtr_cbor`), type identification (`_fxtr_type_id`), and immutability (`_fxtr_immutable`) into a single base class. This made it impossible to define deserialization logic separately from the entity class (e.g., for unions or type aliases).

4. **One type ID per Python type**: The old system assumed a 1:1 mapping between Python types and type IDs. This didn't support fallback types like `UnknownEntity` (which can deserialize any type ID) or future plans for type families.

5. **Mashumaro coupling**: Core entity operations were coupled to mashumaro-specific serialization logic, making it harder to support alternative serialization strategies.

6. **Scope wrappers added complexity**: `FxtrStoringScope` and `FxtrLoadingScope` were restricted scope wrappers passed to `_to_fxtr_cbor` and `_from_fxtr_cbor`. They added indirection without much benefit, since the active scope could serve the same purpose.

7. **`__orig_class__` magic**: The old system used `__orig_class__` (via `preserve_orig_class` decorator) to capture generic type parameters at construction time. This was fragile and not well-documented in Python.

## New Architecture

### File Layout

```
fxtr2/
├── conversion/                          # Type-annotation-based conversion
│   ├── base_registry.py                 # Generic TypeAnnotationHandlerRegistry
│   └── converters/                      # Concrete handler implementations
│       ├── mashumaro_entities.py         # DataclassEntity + mashumaro serialization
│       ├── unknown_entity.py            # UnknownEntity fallback
│       └── legacy_entity_abc.py         # Bridge for old Entity ABC pattern
├── data/
│   ├── entity_ref_scope.py              # Entity, EntityRef, FxtrScope (unified)
│   ├── entity_serdes.py                 # Compatibility stub → entity_ref_scope + mashumaro_entities
│   └── fxtr_scope.py                    # Compatibility stub → entity_ref_scope
```

### Core Module: `entity_ref_scope.py`

This module is the single source of truth for the Python-level entity API. It contains:

- **`Entity`** — Minimal ABC with one abstract method: `_fxtr_store_impl() -> EntityRef[Self]`. Entity subclasses are responsible for serializing themselves and returning an `EntityRef` that declares the default type to use when loading. The convenience method `store(scope)` delegates to `_fxtr_store_impl` via the scope.

- **`EntityLoadHandler[T]` protocol** — Defines the deserialization interface: `supports_entity_type(type_id, target_type) -> bool` and `from_lowlevel_entity_data(cbor, target_type) -> DeserializedEntity[T]`. Handlers are registered globally and looked up via `TypeAnnotationHandlerRegistry`.

- **`EntityRef[T]`** — Reference to an entity, holding `entity_id: FxtrID` and `target_type: EntityTypeAnnotation[T]`. Delegates all operations (load, cache lookup, lift, copy) to `FxtrScope`. No caching logic lives in `EntityRef` itself.

- **`FxtrScope`** — Manages entity lifetimes and caching. Cache is scope-local (`_ref_loaded_value_cache`), tied to scope lifetime. Side-effect tracking and replay still exist but operate within the scope's cache rather than a global cache.

- **`register_entity_load_handler`** — Top-level function for registering load handlers.

### Conversion Module: `conversion/`

- **`base_registry.py`** — Generic `TypeAnnotationHandlerRegistry[K, V]` with MRO-based dispatch, union support, type alias unwrapping, `OverrideFxtrHandlers` sentinel, and generic type parameter substitution.

- **`converters/mashumaro_entities.py`** — `DataclassEntity` base class, inline entity types (`Child[T]`, `LinkedEntity[T]`, `ChildRef[T]`), mashumaro serialization strategies, and `DataclassEntityLoadHandler`. Registers itself with the entity load registry on import.

- **`converters/unknown_entity.py`** — `UnknownEntity` fallback entity (wraps raw CBOR) and `LoadAsUnknownByDefault[T]` type alias for generic bounds that should fall back to UnknownEntity. Supports any type ID.

- **`converters/legacy_entity_abc.py`** — `SelfLoadingLegacyEntity` bridge for old-style entities with `_to_fxtr_cbor`/`_from_fxtr_cbor`/`_fxtr_type_id`/`_fxtr_immutable`. Adapts them to the new `Entity` + `EntityLoadHandler` system.

### Compatibility Stubs

- **`entity_serdes.py`** — Re-exports from new locations. Emits `DeprecationWarning` on import. Provides shims for `store_entity()`, `validate_entity_type()`, `deep_load()`. Has a `__getattr__` that gives a helpful error for the `Entity` name (which changed behavior).

- **`fxtr_scope.py`** — Re-exports from `entity_ref_scope`. Emits `DeprecationWarning` on import. Provides `type FxtrStoringScope = typing.Never` and `type FxtrLoadingScope = typing.Never` stubs.

## Key Design Changes

### Caching: Global → Scope-Local

**Before**: `_GLOBAL_ENTITY_CACHE` mapped `(entity_id, target_type) → CachedEntityLoadValue` globally. Cache entries lived forever and required careful side-effect replay to ensure scope safety.

**After**: Each `FxtrScope` has its own `_ref_loaded_value_cache: dict[FxtrID, dict[EntityTypeAnnotation, RefLoadCacheEntry]]`. Cache entries are:
- Created when an entity is loaded and the handler declares it cacheable
- Copied from parent scopes on `lift()` or `copy_from_scope()`
- Cleared on `clear_cache()`
- Destroyed when the scope exits

A fast-path set `_refs_loaded_since_abandon` tracks refs loaded since the last `abandon()` call, allowing immediate cache hits without replaying side effects.

### Serialization: ABC → `_fxtr_store_impl`

**Before**: `Entity._to_fxtr_cbor(scope) -> FxtrCbor` was the serialization entry point, with a `FxtrStoringScope` wrapper restricting available operations.

**After**: `Entity._fxtr_store_impl() -> EntityRef[Self]` is the serialization entry point. The entity is responsible for calling `scope.store_cbor(cbor)` and returning an `EntityRef` declaring its default load type. The active scope is used directly (no wrapper).

### Deserialization: ABC → Registry

**Before**: `Entity._from_fxtr_cbor(cbor, annotated_type, scope)` was called on the target class, with a `FxtrLoadingScope` wrapper tracking side effects.

**After**: `EntityLoadHandler.from_lowlevel_entity_data(cbor, target_type)` is called on the registered handler. The handler operates within the active scope. Side effects are tracked by the scope's `_load_tracer` context variable.

### Type Parameter Preservation

**Before**: `__orig_class__` was captured at construction time (via `preserve_orig_class` decorator) and used to determine the `target_type` for the `EntityRef` returned by `store()`.

**After**: Two separate mechanisms:
- **At load time**: The caller specifies the full type annotation (e.g., `EntityRef(id, WrapperWithRef[TypeA])`), and type variables are substituted through to the mashumaro decoder.
- **At store time**: The entity's `_fxtr_store_impl` declares the default load type. `DataclassEntity` uses `_infer_default_type_params(type(self))` which fills in type parameter bounds (e.g., `Wrapper[T: Entity]` → `Wrapper[UnknownEntity]`). Subclasses can override `_fxtr_store_impl` for custom logic (e.g., using runtime type introspection).

### Multi-Type-ID Support

**Before**: Each entity class had exactly one `_fxtr_type_id()`.

**After**: `EntityLoadHandler.supports_entity_type(type_id, target_type)` can return `True` for multiple type IDs. This is used by `UnknownEntity` (accepts any type ID) and will eventually support type families.

### Union Dispatch

**Before**: Union dispatch checked `$fxtr.type` against `_fxtr_type_id()` for each union variant.

**After**: Union dispatch calls `handler.supports_entity_type()` for each variant's handler. Multiple handlers matching the same type ID within a union is an error (must use `OverrideFxtrHandlers` to break ties).

### load_cbor Unification

**Before**: `scope.load_cbor(id)` loaded raw CBOR directly from the arena.

**After**: `scope.load_cbor(id)` goes through `EntityRef(id, UnknownEntity).load()`, meaning all loads flow through the same caching and side-effect tracking path.

## Breaking Changes

- **`copy_to_scope` / `copy_from_scope`**: No longer accepts collections (lists, sets, dicts of refs). Pass individual refs/IDs instead.
- **`Entity` base class**: No longer has `_to_fxtr_cbor`, `_from_fxtr_cbor`, `_fxtr_type_id`, `_fxtr_immutable`. Use `SelfLoadingLegacyEntity` for old-style entities.
- **`FxtrStoringScope` / `FxtrLoadingScope`**: Removed. Use the active scope directly.
- **`preserve_orig_class`**: Removed. Override `_fxtr_store_impl` for custom type parameter handling.
- **`store_entity()`**: Deprecated. Use `entity.store()` or `scope.store(entity)`.
- **`deep_load()`**: Deprecated. Use `scope.deep_load(thunk)`.
- **`clear_global_caches()`**: Removed. Use `scope.clear_cache()`.
- **`FxtrSendable` / `sendable_to_cbor`**: Removed.
- **`OutputStream`**: Removed.
- **`StrongLinkedEntityRef`**: Removed. Use `ChildRef` instead.

## Implementation Checklist

- [x] Implement `conversion/base_registry.py`
- [x] Implement `entity_ref_scope.py` (unified Entity, EntityRef, FxtrScope)
- [x] Implement `conversion/converters/mashumaro_entities.py`
- [x] Implement `conversion/converters/unknown_entity.py`
- [x] Implement `conversion/converters/legacy_entity_abc.py`
- [x] Create compatibility stubs for `entity_serdes.py` and `fxtr_scope.py`
- [x] Write tests for `conversion/base_registry.py`
- [x] Write tests for `entity_ref_scope.py`
- [x] Verify existing tests still pass
- [x] Update `notes/roundtrip_serdes.md`
- [x] Update `CODE_OVERVIEW.md`
- [x] Migrate all production code imports away from deprecated stubs
- [x] Migrate all test imports away from deprecated stubs (except test_entity_serdes.py)
- [x] Replace all `store_entity()` call sites with `entity.store()`
- [x] Reorganize tests (split test_entity_serdes.py, test_fxtr_scope.py, test_entity_ref_scope.py → new feature-based structure)
- [x] Migrate imports in examples

## Migration Steps (Reference)

Steps 1, 3, 4, 5 are complete. Step 2 (test_entity_serdes.py reorganization) is the only remaining work.

Current state: all 1297 tests pass, zero deprecated imports in all code.

### Step 1: Migrate `test_fxtr_scope.py` imports — DONE

`test_fxtr_scope.py` tests FxtrScope behavior exclusively (hierarchy, tracking, isolation, copying, lifting). Its file organization is fine — it just needs its imports updated:

- `fxtr2.data.fxtr_scope.ACTIVE_SCOPE` → `fxtr2.data.entity_ref_scope.ACTIVE_SCOPE`
- `fxtr2.data.fxtr_scope.FxtrScope` → `fxtr2.data.entity_ref_scope.FxtrScope`
- `fxtr2.data.entity_serdes.DataclassEntity` → `fxtr2.conversion.converters.mashumaro_entities.DataclassEntity`
- `fxtr2.data.entity_serdes.EntityRef` → `fxtr2.data.entity_ref_scope.EntityRef`

### Step 2: Reorganize test files — DONE

Reorganized test_entity_serdes.py (99 tests), test_fxtr_scope.py (41 tests), and test_entity_ref_scope.py (34 tests) into feature-based organization:

**New structure**:
- `test_entity_ref_scope/` folder (102 tests, organized by feature):
  - `test_entity_ref.py` - EntityRef creation, properties, validation (12 tests)
  - `test_scope_lifecycle.py` - FxtrScope lifecycle, hierarchy, tracking, isolation (45 tests)
  - `test_caching.py` - Entity caching and side effect replay (10 tests)
  - `test_store_load.py` - Store/load operations, deep load, copy (17 tests)
  - `test_custom_entities.py` - Custom Entity implementations with new base class (7 tests)
  - `test_load_union.py` - Union type loading with mixed entity types (6 tests)
- `test_conversion/test_mashumaro_entities.py` - DataclassEntity tests (63 tests)
- `test_conversion/test_legacy_entity_abc.py` - SelfLoadingLegacyEntity bridge (4 tests)
- `test_conversion/test_unknown_entity.py` - UnknownEntity fallback (1 test)

**Final count**: 165 tests (174 original - 9 merges)
**Duplicate tests merged**: 9 (equality, scope basics, caching, deep_load, inline entities)
**Custom entities converted**: All tests using SelfLoadingLegacyEntity in test_custom_entities.py converted to new Entity base class
**All imports updated**: No deprecated imports remain

See `stage4_entity_registry_testdetail.md` and `stage4_entity_registry_implementation_notes.md` for complete accounting.

### Step 3: Migrate production code imports — DONE

These files imported from the deprecated stubs and have been updated. The mapping is:

**From `fxtr2.data.entity_serdes`:**
| Old import | New location |
|------------|-------------|
| `Entity` | `fxtr2.data.entity_ref_scope` |
| `EntityRef` | `fxtr2.data.entity_ref_scope` |
| `EntityNotLoadedError` | `fxtr2.data.entity_ref_scope` |
| `EntityTypeAnnotation` / `EntityAnnotatedType` | `fxtr2.data.entity_ref_scope` |
| `DataclassEntity` | `fxtr2.conversion.converters.mashumaro_entities` |
| `Child`, `ChildRef`, `ChildFxtrID` | `fxtr2.conversion.converters.mashumaro_entities` |
| `LinkedEntity` | `fxtr2.conversion.converters.mashumaro_entities` |
| `DeferredLoadPlaceholder` | `fxtr2.conversion.converters.mashumaro_entities` |
| `FxtrIDSerializationStrategy` | `fxtr2.conversion.converters.mashumaro_entities` |
| `FrozensetSerializationStrategy` | `fxtr2.conversion.converters.mashumaro_entities` |
| `EntityRefSerializationStrategy` | `fxtr2.conversion.converters.mashumaro_entities` |
| `store_entity(entity)` | `entity.store()` or `scope.store(entity)` |
| `store_entity(entity, as_type=T)` | `entity.store().replace_target_type(T)` |
| `deep_load(thunk)` | `scope.deep_load(thunk)` |

**From `fxtr2.data.fxtr_scope`:**
| Old import | New location |
|------------|-------------|
| `FxtrScope` | `fxtr2.data.entity_ref_scope` |
| `ACTIVE_SCOPE` | `fxtr2.data.entity_ref_scope` |
| `GLOBAL_SCOPE` | `fxtr2.data.entity_ref_scope` |
| `GLOBAL_BACKING_STORE` | `fxtr2.data.entity_ref_scope` |
| `get_active_scope` | `fxtr2.data.entity_ref_scope` |
| `get_active_context_id` | `fxtr2.data.entity_ref_scope` |
| `get_active_context_marker` | `fxtr2.data.entity_ref_scope` |
| `track_builtin_id` | `fxtr2.data.entity_ref_scope` |
| `FxtrStoringScope` | removed (use `FxtrScope` directly) |
| `FxtrLoadingScope` | removed (use `FxtrScope` directly) |

**Files to update (production code):**
- `fxtr2/data/system_entities.py` — imports `DataclassEntity` from `entity_serdes`, `fxtr_scope`
- `fxtr2/data/fxtr_context_api.py` — imports `EntityRef` from `entity_serdes`, `FxtrScope` from `fxtr_scope`
- `fxtr2/data/store_indexing/indexing_types.py` — imports `ChildFxtrID`, `DataclassEntity` from `entity_serdes`
- `fxtr2/data/store_indexing/indexers.py` — imports `DataclassEntity`, serialization strategies from `entity_serdes`
- `fxtr2/data/store_indexing/index_computer.py` — imports `EntityRef` from `entity_serdes`, `FxtrScope` from `fxtr_scope`
- `fxtr2/data/git_store/git_namespace.py` — imports `EntityRef` from `entity_serdes`, `FxtrScope` from `fxtr_scope`
- `fxtr2/data/sqlite_store/sqlite_namespace.py` — imports `store_entity` from `entity_serdes`, `FxtrScope` from `fxtr_scope`
- `fxtr2/legacy_compat/data.py` — imports `EntityRef`, `store_entity` from `entity_serdes`
- `fxtr2/legacy_compat/transitional_rundata.py` — imports `Child`, `ChildRef`, `DataclassEntity`, `EntityRef` from `entity_serdes`
- `fxtr2/legacy_compat/transitional_session_manager.py` — imports `EntityRef` from `entity_serdes`

**Files to update (tests):**
- `tests/conftest.py` — imports `GLOBAL_SCOPE`, `FxtrScope` from `fxtr_scope`
- `tests/test_fxtr_context_api.py` — imports `ACTIVE_SCOPE`, `FxtrScope` from `fxtr_scope`
- `tests/test_legacy_compat/test_data.py` — imports from both stubs
- `tests/test_sqlite_store/test_sqlite_store.py` — imports from both stubs
- `tests/test_store_indexing/test_backend_integration.py` — imports from both stubs
- `tests/test_store_indexing/test_index_computer.py` — imports from both stubs
- `tests/test_store_indexing/test_indexer_serdes.py` — imports from both stubs
- `tests/test_store_indexing/test_unpack.py` — imports `FxtrScope` from `fxtr_scope`
- `tests/test_store_indexing/test_user_facing_api.py` — imports from both stubs
- `tests/test_git_store/test_git_namespace_indexing.py` — imports from both stubs
- `tests/test_git_store/test_index_sync.py` — imports from both stubs

**Files to update (examples/scratch, lower priority):**
- `examples/benchmarks/sqlite_benchmark.py`
- `examples/store_indexing/entities.py`
- `examples/store_indexing/query_data.py`
- `scratch_notebooks/nb_02_06_demo.py`

### Step 4: Replace `store_entity` call sites — DONE

Several files use `store_entity(entity)` or `store_entity(entity, as_type=T)`. These need to be rewritten:

- `store_entity(entity)` → `entity.store()` (or `scope.store(entity)`)
- `store_entity(entity, as_type=T)` → `entity.store().replace_target_type(T)`
- `store_entity(entity, scope=s)` → `entity.store(scope=s)` (or `s.store(entity)`)

Call sites: `sqlite_namespace.py`, `legacy_compat/data.py`, and several test files.

### Step 5: Verify and clean up — DONE (for completed steps)

- All 1306 tests pass
- Zero deprecated imports in `fxtr2/` production code
- Zero deprecated imports in `tests/` except `test_entity_serdes.py`
- `CODE_OVERVIEW.md` test counts updated to 1306
- Examples/scratch files not yet updated (low priority)
