# Stage 1, Part 1f: IndexComputer

Implements the `IndexComputer` helper that bridges between the store (which thinks in terms of
FxtrIDs and SQL) and the indexer logic (which needs loaded entities and scopes).

Depends on: 1bcd (indexer types, serialization, unpack logic).

## Overview

The `IndexComputer` lives in `store_indexing/index_computer.py` and provides two operations:

1. **`get_indexer_types`**: Given an indexer FxtrID + arena, load the indexer entity and return
   its column schema (key types + value type). Used by the store when registering a new indexer
   to determine what SQL columns to create.

2. **`compute_indexer_entries`**: Given lists of entity FxtrIDs and indexer FxtrIDs + an arena
   containing all of them, run every indexer on every entity and return the (key_tuple, value)
   results. Used by the store during both `register_indexer` (bulk indexing existing pins) and
   `pin_entity_from_arena` (indexing the newly pinned entity).

The caller (the store) is responsible for ensuring that all entities and indexers are already
in its namespace before calling the IndexComputer. This means the IndexComputer always receives
a single arena (e.g., a `SQLiteNamespaceArena`) that has everything it needs.

### Class lifecycle

`IndexComputer` has no constructor state for now. It could hold per-store caching in the future
(e.g., caching loaded `Indexer` objects across calls), but we start simple. Each method creates
fresh isolated scopes and tears them down when done.

## `get_indexer_types`

```python
async def get_indexer_types(
    self,
    indexer: FxtrID,
    arena: ReadableArena,
) -> tuple[Sequence[IndexerColumnType], IndexerColumnType]:
```

Implementation:

1. Create an isolated scope: `async with FxtrScope(isolated=True, name="index_computer_get_types") as scope:`
2. Attach the arena: `scope._main_arena.unsafe_lazily_attach_from_arena(indexer, arena)`
3. Create an `EntityRef[WellKnownBuiltinIndexer]` for the indexer ID
4. Load it: `loaded = await ref.load(scope=scope)`
5. Return `(loaded.index_key_types(), loaded.index_value_type())`

If loading fails or the entity isn't a `WellKnownBuiltinIndexer`, let the exception propagate.
The store should treat this as a registration error.

## `compute_indexer_entries`

```python
async def compute_indexer_entries(
    self,
    entities: Sequence[FxtrID],
    indexers: Sequence[FxtrID],
    arena: ReadableArena,
) -> dict[
    tuple[FxtrID, FxtrID],
    Sequence[tuple[tuple[IndexerColumnValue, ...], IndexerColumnValue]]
]:
```

### Scope structure

Two levels of isolation:

```
[outer scope]  (isolated=True, then made active via as_active_scope())
  - Arena has all entities and indexers lazily attached
  - Indexers are loaded here (cached across entity iterations)
  │
  ├─ [child scope 1]  (non-isolated, inherits outer via active scope)
  │    - Lifts one entity + one indexer
  │    - unpack_entity runs here; DerefMarker loads go through this scope
  │    - Cleaned up after each (indexer, entity) pair
  │
  ├─ [child scope 2]  ...
  ...
```

The outer scope is **isolated** so indexing doesn't touch the application's active scope.
It is then made active via `as_active_scope()` so that child scopes naturally inherit from it
without needing explicit `include=[outer]`.

> **Implementation note:** The original plan used `FxtrScope(isolated=True, include=[outer])`
> for child scopes. During implementation we switched to using `outer.as_active_scope()` +
> non-isolated child scopes instead. This is simpler and also ensures the indexer logic (which
> uses the active scope implicitly) works without needing explicit scope parameters everywhere.

Child scopes provide:
- Each (indexer, entity) run gets a clean environment
- Entities loaded via DerefMarker during path walking are cleaned up afterward
- The outer scope's loaded indexers remain available across children

### Implementation

```python
async def compute_indexer_entries(self, entities, indexers, arena):
    results = {}

    async with FxtrScope(isolated=True, name="index_computer_outer") as outer:
        # 1. Attach all IDs from the arena into the outer scope
        for eid in entities:
            outer._main_arena.unsafe_lazily_attach_from_arena(eid, arena)
        for iid in indexers:
            outer._main_arena.unsafe_lazily_attach_from_arena(iid, arena)

        # Make the outer scope active so child scopes inherit from it
        with outer.as_active_scope():
            # 2. Pre-load all indexer objects in the outer scope (shared across entities)
            loaded_indexers: dict[FxtrID, WellKnownBuiltinIndexer] = {}
            for iid in indexers:
                ref = EntityRef(iid, WellKnownBuiltinIndexer)
                loaded_indexers[iid] = await ref.load(scope=outer)

            # 3. For each (indexer, entity) pair, run in a child scope
            for iid, indexer_obj in loaded_indexers.items():
                for eid in entities:
                    async with FxtrScope(name="index_computer_run") as child:
                        child.lift(eid)
                        entity_data = await child.load_cbor(eid)
                        entries = await indexer_obj.unpack_entity(entity_data, child)
                        results[(iid, eid)] = entries

    return results
```

### Key details

**Lazy attachment**: `unsafe_lazily_attach_from_arena(id, arena)` makes the entity loadable
through the outer scope's arena without eagerly copying data. When `load_cbor` is called, the
arena pulls data from the source arena lazily, and child entities are tracked transitively.

**Indexer caching within a call**: Indexers are loaded once in the outer scope. Because the
outer scope is included by every child scope, `ref.load()` hits the entity_serdes global cache
on subsequent calls (the entity was already deserialized with the same scope lineage). This
avoids redundant deserialization when the same indexer is applied to many entities.

**`unpack_entity` scope parameter**: The child scope is passed directly to `unpack_entity`.
When the indexer follows a `DerefMarker`, it calls `scope.load_cbor(fxtr_id)`, which does
`lift` + `load` through the child scope. The lifted entity's data comes from the outer scope's
arena (via the lazy attachment chain). Any transitively loaded entities are tracked in the
child scope and cleaned up when it exits.

**Error propagation**: If `unpack_entity` raises `ValueError` (strict indexer on incompatible
entity), it propagates out of `compute_indexer_entries`. The store is responsible for catching
and handling this (e.g., reporting it to the user during pin or registration).

**Empty results**: If an indexer returns `[]` for an entity (lax mode, non-matching type), the
result dict contains `(iid, eid) -> []`. The store uses the presence of the key to know the
pair was processed (needed for the `NS_INDEX_PROCESSED__` table), even if no index rows were
produced.

## Tests

New file: `tests/test_store_indexing/test_index_computer.py`

Tests use the `local_fxtr_scope` fixture for the ambient scope context, but the IndexComputer
creates its own isolated scopes internally. We need to store test entities into the
`local_fxtr_scope`'s arena and pass that arena to the IndexComputer.

### Test helpers

```python
def _make_arena_with_entities(scope, *cbor_entities) -> tuple[list[FxtrID], MemoryArena]:
    """Store CBOR entities into the scope and return their IDs + an arena containing them."""
```

Use `scope.store_cbor()` to store entities, then `scope._main_arena.copy_to_new_arena(ids)`
to produce a standalone arena for the IndexComputer.

For indexers, use `store_entity(indexer_obj, scope=scope)` to store them, which returns an
`EntityRef` whose `.entity_id` gives the FxtrID.

### Test cases

- **`test_get_indexer_types_value_indexer`**: Store a `TypedPathValueIndexer`, call
  `get_indexer_types`, verify it returns the correct key_types and value_type.

- **`test_get_indexer_types_exists_indexer`**: Same but with `TypedPathExistsIndexer`. Verify
  value_type is `BOOL`.

- **`test_compute_single_indexer_single_entity`**: One `TypedPathValueIndexer` on one entity.
  Verify the results dict has one entry with the expected (key_tuple, value) pairs.

- **`test_compute_single_indexer_multiple_entities`**: One indexer on three entities. Verify
  results for all three entity keys.

- **`test_compute_multiple_indexers_single_entity`**: Two different indexers on one entity.
  Verify both indexer results.

- **`test_compute_multiple_indexers_multiple_entities`**: Cartesian product. Verify all
  (indexer, entity) combinations are present.

- **`test_compute_lax_indexer_skips_incompatible`**: Lax indexer with `TypedDerefMarker`
  initial_lookup. Entity has wrong type. Verify result is `[]` (not missing from dict).

- **`test_compute_strict_indexer_raises`**: Strict indexer on incompatible entity. Verify
  `ValueError` propagates.

- **`test_compute_with_deref`**: Entity has a FxtrID field pointing to a child entity also in
  the arena. Indexer path includes a `DerefMarker`. Verify the indexer follows the ref and
  extracts the child's field.

- **`test_compute_with_typed_deref`**: Like above but with `TypedDerefMarker`. Verify type
  filtering works on the child entity.

- **`test_scope_isolation`**: After `compute_indexer_entries` returns, verify that entities
  loaded during indexing (e.g., via DerefMarker) are NOT tracked in the test's
  `local_fxtr_scope`. This confirms the isolated scope cleanup works.

- **`test_empty_entities_list`**: Empty entities list. Verify returns empty dict.

- **`test_empty_indexers_list`**: Empty indexers list. Verify returns empty dict.

- **`test_exists_indexer_compute`**: A `TypedPathExistsIndexer` on an entity. Verify results
  contain `True` as the value.

## Implementation steps

- [x] Implement `get_indexer_types` in `index_computer.py`
- [x] Implement `compute_indexer_entries` in `index_computer.py`
- [x] Write test helpers in `test_index_computer.py`
- [x] Write tests for `get_indexer_types`
- [x] Write tests for `compute_indexer_entries` (all cases above)
- [x] Verify all tests pass
