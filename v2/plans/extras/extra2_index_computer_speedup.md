# Plan: IndexComputer Speedup via Persistent Indexer Caching

## Problem

Pinning entities into indexed SQLite namespaces is slow because every pin operation:
1. Creates a `SQLiteNamespaceArena` containing entity + all indexers (SQLite precomp reads)
2. Creates an isolated `FxtrScope`, deserializes every indexer from scratch
3. For each (indexer, entity) pair: creates a child scope, loads entity CBOR as mutable data
4. Tears down all scopes

With 20 indexers and 100 entities, this means 2000 child scopes, 2000 indexer deserializations,
and 100 SQLiteNamespaceArena constructions.

## Benchmark (before)

```
Case         Entities   Pins   Min (s)    Median   Max (s)      Ent/s
---------------------------------------------------------------------
flat_indexed      100    100    1.1114    1.1356    1.2333         88
```

## Design

### 1. IndexComputer as async context manager with persistent scope

`IndexComputer` becomes an async context manager (implements `__aenter__`/`__aexit__`). On enter
it opens a persistent isolated `FxtrScope` for caching indexer loads. On exit it closes the scope.

It is unsafe to open/close a `FxtrScope` without using it as a context manager, so the
IndexComputer's scope lifecycle is managed inside `SQLiteFxtrSession._lifecycle_handler` (the
background task that manages session lifetime). The session enters the IndexComputer context
inside the lifecycle handler and exits it during teardown.

`SQLiteFxtrSession` stores:
- `self._index_computer: IndexComputer | None` — `None` until the lifecycle handler enters it.

`SQLiteNamespaceBackend` accesses the index computer via `session._index_computer` (asserting
it is not None, which is safe since the session must be active for namespace operations).

The persistent scope is **not** entered as active — it's only used explicitly via `scope=`
parameter and `as_active_scope()` context manager when child scopes need to inherit from it.

### 2. `cached_indexers` tracking

`IndexComputer` maintains `_cached_indexers: dict[FxtrID, WellKnownBuiltinIndexer]` — a map from
indexer FxtrID to its loaded Python object. This is populated when indexers are first encountered
and reused on subsequent calls.

A public property `cached_indexers` returns the key set: `AbstractSet[FxtrID]`.

### 3. New `compute_indexer_entries` signature

```python
async def compute_indexer_entries(
    self,
    entities: Sequence[FxtrID],
    indexers: Sequence[FxtrID],
    entity_arena: ReadableArena,
    indexer_arena: ReadableArena | None,  # None if all indexers already cached
) -> dict[tuple[FxtrID, FxtrID], Sequence[tuple[...]]]:
```

The caller splits the arena responsibility:
- `entity_arena`: contains the entities to index (and their transitive deps)
- `indexer_arena`: contains only *new* indexers (not in `cached_indexers`), or `None` if all
  are cached

Flow inside `compute_indexer_entries`:
1. If `indexer_arena` is not None: attach new indexers into the persistent scope, load them,
   add to `_cached_indexers`
2. Look up all indexers from `_cached_indexers`
3. Activate the persistent scope with `as_active_scope()` so child scopes inherit from it
4. Create **one** child scope for the entire call. Attach all entities from `entity_arena`, then
   for each entity run all indexers against it.
5. Return results

Using a single child scope avoids per-entity scope creation overhead. The child scope
inherits from the persistent scope (which has all indexers loaded), so `load_frozen_cbor`
on indexers works automatically.

### 4. `unpack_entity` signature change

The `Indexer.unpack_entity` abstract method changes from:

```python
async def unpack_entity(self, entity_data: FxtrCbor, scope: FxtrScope) -> ...
```

to:

```python
async def unpack_entity(self, entity_id: FxtrID, scope: FxtrScope) -> ...
```

The indexer is now responsible for calling `scope.load_frozen_cbor(entity_id)` internally.
This takes advantage of the scope's caching: if the same entity is indexed by multiple indexers,
the frozen CBOR is loaded once and cached.

Both `TypedPathValueIndexer.unpack_entity` and `TypedPathExistsIndexer.unpack_entity` are updated.

### 5. Frozen CBOR throughout the indexer path

All CBOR processing in `indexers.py` switches from mutable (`dict`/`list`) to frozen
(`frozendict`/`tuple`):

- `_walk_path`: change `isinstance(data, dict)` → `isinstance(data, frozendict)`,
  `isinstance(data, list)` → `isinstance(data, tuple)`
- `_check_initial_lookup`: change `isinstance(entity_data, dict)` → `isinstance(entity_data, frozendict)`
- Deref cases in `_walk_path`: change `scope.load_cbor(fid)` → `scope.load_frozen_cbor(fid)`
- Type annotations: `FxtrCbor` → `FrozenFxtrCbor` in function signatures

`frozendict` supports `.get()`, iteration, and indexing, so only the isinstance checks change.
`tuple` supports indexing and enumeration, so `PathIndex` and `VarMarker` logic stays the same.

### 6. Changes to `_pin_entity_from_arena_impl`

Currently:
```python
arena = await SQLiteNamespaceArena._create_for_entities(session, self, all_iids)
results = await self._index_computer.compute_indexer_entries(entities, indexers, arena)
```

After:
```python
cached = self._index_computer.cached_indexers
new_indexer_fids = [fid for fid in indexer_fxtr_ids if fid not in cached]
new_indexer_iids = [iid for fid, iid in zip(indexer_fxtr_ids, indexer_iids) if fid not in cached]

indexer_arena = None
if new_indexer_iids:
    indexer_arena = await SQLiteNamespaceArena._create_for_entities(
        self._session, self, new_indexer_iids
    )

entity_arena = source.copy_to_new_arena([entity_id])

results = await self._index_computer.compute_indexer_entries(
    [entity_fxtr_id], indexer_fxtr_ids, entity_arena, indexer_arena
)
```

Key changes:
- **Entity arena** uses the original `source` arena (via `copy_to_new_arena`) instead of the
  SQLite-backed arena. The source is guaranteed to have all transitive deps and stays alive for
  the duration of the pin call.
- **Indexer arena** is only created for *new* indexers. If every indexer is already cached,
  `SQLiteNamespaceArena._create_for_entities` is skipped entirely — a major win since it involves
  SQLite reads for precomp data.

**FxtrID lookup for cached-indexer check**: The `indexed_regs` query
(`get_indexed_registrations_for_prefix`) currently returns `(reg_id, indexer_entity_iid, key_prefix)`.
We extend this to also JOIN the entity table and return the FxtrID of each indexer:
`(reg_id, indexer_entity_iid, key_prefix, indexer_fxtr_id)`. This is a cheap JOIN since
`indexer_entity_iid` is already the entity table's primary key. The same change applies to
`_get_indexed_registrations_for_key` (the shared helper) and `IndexMismatchError.current_registrations`.

### 7. Changes to `register_indexer`

`register_indexer` also calls `compute_indexer_entries` in its batched loop. The same persistent
scope benefits apply: the indexer being registered gets loaded once and stays cached for all
subsequent batches. Adapt to the new `compute_indexer_entries` signature (pass `source` arena
for entities, indexer arena for the single indexer being registered on first call, `None` on
subsequent batches since it's already cached).

### 8. `get_indexer_types` update

If the indexer is already in `_cached_indexers`, return the schema directly without creating
any scope or arena. If not, load the indexer into the persistent scope (populating the cache
for future use).

## Implementation Steps

- [x] 1. **Run benchmark (before)** — record baseline numbers (captured above)
- [x] 2. **Make IndexComputer an async context manager** — persistent isolated scope,
      `_cached_indexers` dict, `cached_indexers` property. Wire into
      `SQLiteFxtrSession._lifecycle_handler` (enter on start, exit on teardown). Store as
      `session._index_computer: IndexComputer | None`. `SQLiteNamespaceBackend` accesses via session.
- [x] 3. **Extend `_get_indexed_registrations_for_key`** — JOIN entity table to include
      `indexer_fxtr_id` (as bytes → parse to `FxtrID`) in results. Update
      `get_indexed_registrations_for_prefix`, `IndexMismatchError.current_registrations`, and
      all callers to handle the 4-tuple `(reg_id, iid, prefix, fxtr_id)`.
- [x] 4. **Change `unpack_entity` signature** — update `Indexer.unpack_entity` in `indexing_types.py`
      to take `entity_id: FxtrID` instead of `entity_data: FxtrCbor`. Update both concrete
      implementations in `indexers.py`.
- [x] 5. **Switch indexers to frozen CBOR** — update `_walk_path`, `_check_initial_lookup`, and
      both `unpack_entity` implementations to use `FrozenFxtrCbor` types (`frozendict`/`tuple`
      isinstance checks, `load_frozen_cbor` for derefs).
- [x] 6. **Rewrite `compute_indexer_entries`** — new signature with split arenas, persistent scope
      usage, caching of indexer loads, single child scope per compute call.
- [x] 7. **Update `get_indexer_types`** — use cached indexers when available.
- [x] 8. **Update `_pin_entity_from_arena_impl`** — split arena creation, use `source` arena for
      entities, only create `SQLiteNamespaceArena` for new indexers, use FxtrID from extended
      indexed_regs query.
- [x] 9. **Update `register_indexer`** — adapt to new `compute_indexer_entries` signature.
- [x] 10. **Fix tests** — update test_index_computer.py and any other tests that call
       `compute_indexer_entries` or `unpack_entity` directly. Update tests in test_unpack.py
       for the new frozen CBOR types and entity_id-based signature.
- [x] 11. **Run benchmark (after)** — record new numbers, compare.
- [x] 12. **Update CODE_OVERVIEW.md** — document the persistent IndexComputer scope pattern and
       frozen CBOR usage in indexers.

## Benchmark (after)

```
Case          Entities   Pins   Min (s)    Median   Max (s)      Ent/s
----------------------------------------------------------------------
flat_indexed       100    100    0.6694    0.6923    0.7924        144
```

**Result: 1.14s → 0.69s median, ~1.6x speedup (88 → 144 entities/s)**

No regressions on other benchmarks (tree_pin, tree_retrieve, flat_pin, flat_retrieve).
