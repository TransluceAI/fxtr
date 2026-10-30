# Stage 15: Flat Data Optimization

## Goal

Eliminate redundant CBOR serialize/deserialize cycles when transferring entity data between
arenas. Currently, loading an entity through N stacked memory scopes causes N-1 unnecessary
CBOR round-trips. After this change, CBOR bytes are copied directly between backing stores,
and deserialization only happens once at the final `load()` call.

## Overview

Introduce `load_flat` on `ReadableArena` which returns raw bytes + dependency metadata
(a `FxtrEntityFlatData`) instead of deserialized `FxtrCbor`. Add `bare_store_flat` on
`MemoryStore` which accepts flat data and stores it without CBOR parsing. Update the
inheritance loading path (`_ensure_own`) and the copy worklists to use these new methods.

Also remove `LazyStoredNode` as a simplification: every d/n node always keeps its own
bytes in the backing store.

Also remove `fetch_data_and_deps_from_arena` from `arena_util.py`, since `load_flat`
subsumes its functionality.

## New Types

### FxtrEntityFlatData (in abstract_arena.py)

```python
@dataclasses.dataclass(frozen=True)
class FxtrEntityFlatData:
    """Flat representation of an entity's data and dependencies.

    For d/n nodes: entity_bytes is the raw CBOR bytes.
    For derived nodes (c/p/u/./!): entity_bytes is None.
    deps is the full dependency list, same format as the removed
    fetch_data_and_deps_from_arena.
    """
    entity_id: FxtrID
    entity_bytes: bytes | None
    deps: list[tuple[FxtrID, DepKind]]
```

The `deps` list contains:
- **d-nodes**: `(child_id, STRONG_REF/WEAK_REF)` for each ID in the CBOR body (including `$fxtr.ctx` marker as WEAK_REF)
- **n-nodes**: same as d-nodes, plus `(unwrap_id, UNWRAPS_TO)`
- **c-nodes**: `(ctx_data_id, CTX_NODE)`, `(unwrap_id, UNWRAPS_TO)`
- **p-nodes**: `(pathroot_id, CTX_NODE)`, `(unwrap_id, UNWRAPS_TO)`, optionally `(backstep_id, BACKSTEP)`
- **u-nodes**: `(unwrap_id, UNWRAPS_TO)`
- **./!-nodes**: empty list

### DepKind relocation

Move `DepKind` from `arena_util.py` to `abstract_arena.py` (alongside `FxtrEntityFlatData`).
Update `arena_util.py` to import it from there.

## New Methods

### ReadableArena.load_flat (abstract, async)

```python
async def load_flat(self, node_id: FxtrID) -> FxtrEntityFlatData:
    """Load flat entity data without deserializing CBOR.

    All returned IDs in the result.deps will be tracked in the arena as a side effect, maintaining
    the invariant that all IDs returned by an arena method are always loadable in that arena.
    Returns raw bytes (for d/n) and the full dependency list.
    """
```

**Contract**: After `load_flat` returns, all IDs appearing in `deps` are tracked in this
arena. This matches the contract of `load()` (which tracks all child IDs).

### MemoryStore.bare_load_flat (async)

Returns `FxtrEntityFlatData` from the store. For d/n StoredNodes, reads bytes from the
backing store and constructs deps from the node's `requires`, `strong_linked_refs`, and
`unwraps_to`. For DerivedNodes, constructs deps from `ctx_node`, `backstep_node`,
`unwraps_to`. For InheritedNodes, calls `_ensure_own` first.

### MemoryStore.bare_store_flat (sync)

```python
def bare_store_flat(self, flat_data: FxtrEntityFlatData) -> FxtrID:
    """Store flat entity data without CBOR parsing.

    All dep IDs must already be in _entity_states.
    """
```

For d/n nodes:
- Extracts `requires` = set of IDs with STRONG_REF or WEAK_REF
- Extracts `strong_linked_refs` = set of IDs with STRONG_REF
- Extracts `unwraps_to` = ID with UNWRAPS_TO (for n-nodes)
- Saves bytes to backing store
- Creates `StoredNode`, calls `_save_or_upgrade_node`

For derived nodes:
- Extracts `ctx_node`, `backstep_node`, `unwraps_to` from deps
- Creates `DerivedNode`, calls `_save_or_upgrade_node`

For ./!:
- Creates `DerivedNode` with empty requires

**Key difference from `bare_store`**: Does NOT parse the CBOR body, compute hashes,
or recursively store unwrapped data. The ID comes from `flat_data.entity_id`, the bytes
are stored as-is, and deps are taken directly from `flat_data.deps`. All deps must be checked as
already existing in the store.

## Changes to Existing Code

### _ensure_own (MemoryStore, d/n InheritedNode path)

Before:
```
loaded_data = await parent_arena.load(node_id)          # deserialize
visit_ids(loaded_data, collect_children)                 # walk CBOR tree
for child_id in children:
    _adopt_inheritance_arena_claim(child_id, parent_arena)
own_node_id = self.bare_store(loaded_data)               # serialize + hash + store
```

After:
```
flat_data = await parent_arena.load_flat(node_id)        # raw bytes, no CBOR parsing
for dep_id in set(dep_id for dep_id, _ in flat_data.deps):
    _adopt_inheritance_arena_claim(dep_id, parent_arena)
self.bare_store_flat(flat_data)                           # store bytes directly
```

For simplicity, we adopt every dep we receive from load_flat; nodes we already have will be handled
automatically. (Note: it is important that we only adopt each node once, even if it is linked by
multiple dep kinds; this should be rare though.)

### Remove LazyStoredNode

- Delete the `LazyStoredNode` class and `StoredNode.into_lazy()` method
- Update `_entity_states` type: `dict[FxtrID, StoredNode | DerivedNode | InheritedNode]`
- In `bare_store`: remove the final cleanup block that calls `into_lazy()` and frees
  unwrapped node bytes (the block at lines ~1001-1012)
- In `bare_load`: remove the `elif isinstance(node, LazyStoredNode)` branch
  (lines ~1134-1168) that walks up through wrappers to reconstruct data
- Update `_ensure_own` return type and isinstance checks
- Update `_collect_one_garbage` and `_maybe_mark_dead` — simplify isinstance checks
- Update `backrefs` method match arms
- Keep `wrapped_by` field on `StoredNode` and `DerivedNode` for GC
- Update module docstring to remove LazyStoredNode mention

### MemoryArena.load_flat implementation

```python
async def load_flat(self, node_id: FxtrID) -> FxtrEntityFlatData:
    if not self.is_tracked(node_id): raise ...
    flat_data = await self._store.bare_load_flat(node_id)
    # Track all deps in this arena (matching load()'s contract)
    for dep_id, _dep_kind in flat_data.deps:
        self._store.track_in_arena(dep_id, self._key)
    return flat_data
```

### SQLiteNamespaceArena.load_flat implementation

For **d/n nodes**: New SQL query in `sqlite_ops.py` that fetches CBOR bytes + all deps
with their FxtrIDs (public_id bytes) and dep kinds, in a single read transaction. Track
deps via `_load_into_tracked`.

```python
# New function in sqlite_ops.py
def load_entity_flat(conn, internal_id):
    """Load CBOR bytes + all deps (with public IDs and kinds).

    Returns:
        (cbor_bytes_or_none, deps) where deps is
        list of (dep_internal_id, dep_public_id_bytes, dep_kind_str)
    """
```

For **derived nodes (c/p/u/./!)**: Construct deps from precomputed data
(`_precomp_ctx_node`, `_precomp_step_back_p_ctx`, `_precomp_unwraps_to`) without
touching the DB.

### Update _build_tmp_arena_copying_from_external_arena (MemoryStore)

Replace `fetch_data_and_deps_from_arena(item.entity_id, tmp_source_arena)` with
`await tmp_source_arena.load_flat(item.entity_id)`.

In the `_SaveWork` handler, replace the manual node construction with
`self.bare_store_flat(flat_data)`.

The `_DiscoverWork` / `_SaveWork` worklist structure stays the same, but `_SaveWork` now
holds a `FxtrEntityFlatData` instead of separate `(cbor, deps)`.

### Update _build_tmp_arena_copying_from_external_arena (SQLiteNamespaceBackend)

Replace `fetch_data_and_deps_from_arena(fxtr_id, source)` with
`await source.load_flat(fxtr_id)`.

The rest of the worklist logic stays the same — it already works with `(bytes | None, deps)`.

### Remove fetch_data_and_deps_from_arena

Delete the function from `arena_util.py`. Remove imports from `memory_arena.py`,
`sqlite_namespace.py`, and `deprecated/sqlite_arena.py`.

Note: `deprecated/sqlite_arena.py` also uses `fetch_data_and_deps_from_arena`. Since it's
deprecated code, we can either update it to use `load_flat` or leave a local copy of the
old function there. **Decision:** Make a local copy of `fetch_data_and_deps_from_arena` in
`deprecated/sqlite_arena.py` to avoid modifying deprecated code behavior.

## Implementation Steps

- [x] 1. **Move `DepKind` to `abstract_arena.py`**; add `FxtrEntityFlatData` dataclass; add abstract `load_flat` method on `ReadableArena`. Update `arena_util.py` to import `DepKind` from new location.

- [x] 2. **Remove `LazyStoredNode`**: delete class, delete `StoredNode.into_lazy()`, update `_entity_states` type annotation, remove lazy downgrade in `bare_store`, remove lazy reconstruction in `bare_load`, update all isinstance checks in `_ensure_own`, `_collect_one_garbage`, `_maybe_mark_dead`, `backrefs`, etc. Update module docstring.

- [x] 3. **Add `bare_store_flat` to `MemoryStore`**: implement storing flat data for all node types (d/n via backing store + StoredNode, derived via DerivedNode, builtins via DerivedNode).

- [x] 4. **Add `bare_load_flat` to `MemoryStore`**: implement returning flat data from StoredNode (bytes + deps from node fields) and DerivedNode (None + deps from node fields). For InheritedNode, call `_ensure_own` first.

- [x] 5. **Update `_ensure_own`** for d/n InheritedNodes: call `parent_arena.load_flat` instead of `parent_arena.load`, adopt STRONG_REF/WEAK_REF children, call `bare_store_flat`.

- [x] 6. **Implement `MemoryArena.load_flat`**: delegate to `bare_load_flat`, track all deps in arena.

- [x] 7. **Add `load_entity_flat` SQL query to `sqlite_ops.py`**: fetch CBOR bytes + all deps with public IDs and dep kinds.

- [x] 8. **Implement `SQLiteNamespaceArena.load_flat`**: for d/n nodes, use new SQL query + track deps; for derived nodes, construct deps from precomp data.

- [x] 9. **Update `_build_tmp_arena_copying_from_external_arena` in `MemoryStore`**: use `load_flat` instead of `fetch_data_and_deps_from_arena`; use `bare_store_flat` in `_SaveWork` handler.

- [x] 10. **Update `_build_tmp_arena_copying_from_external_arena` in `SQLiteNamespaceBackend`**: use `load_flat` instead of `fetch_data_and_deps_from_arena`.

- [x] 11. **Remove `fetch_data_and_deps_from_arena`** from `arena_util.py`. Copy it locally into `deprecated/sqlite_arena.py` if needed. Clean up imports.

- [x] 12. **Update tests**: update `test_memory_arena.py` (remove LazyStoredNode tests, add load_flat/bare_store_flat tests), update `test_arena_util.py` (remove fetch_data_and_deps tests or redirect), add load_flat tests for SQLiteNamespaceArena.

- [x] 13. **Update CODE_OVERVIEW.md**: reflect removal of LazyStoredNode, new load_flat/bare_store_flat methods, removal of fetch_data_and_deps_from_arena.
