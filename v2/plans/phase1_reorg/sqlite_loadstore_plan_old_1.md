# SQLite Load/Store Implementation Plan

## Design Decisions (from Phase 2)

| Decision | Choice |
|----------|--------|
| Transaction strategy | Per-entity transactions |
| P/U child persistence | Persist immediately to DB |
| Entity sharing | Entities can be shared across namespaces |
| Backref consistency | Full consistency (all deps get backrefs) |
| Recursion strategy | Iterative with worklist |
| Conflict handling | Check-first, then insert |
| Singleton handling | Created on demand |

## Architecture

### Two-Layer Separation

**Layer 1: Raw Entity Storage (Session-level)**
- Works on entity tables only: `entity`, `stored_entity_data`, `entity_dep`
- No namespace/arena awareness
- Non-recursive; requires children to already exist in entity tables
- Methods: `bare_store`, `bare_load`, `get_entity_deps`

**Layer 2: Namespace/Arena Handling**
- Manages: `namespace_entity_ownership`, `namespace_entity_backref`, `arena_membership`
- Owns recursive processing
- Uses Layer 1 helpers for data access
- Methods: `add_to_namespace`, `load`, `copy_from_arena`

### Two-Pass Copy Algorithm

```
_build_tmp_arena_copying_from_external_arena(entity_id, source):
    1. Create temp arena
    2. Pass 1: ensure_entity_data_exists(entity_id, source)
       - Ensures entity + all transitive deps exist in entity tables
       - Uses source arena only for entities not already in our tables
    3. Pass 2: add_to_namespace_recursive(entity_id, temp_arena_id)
       - Adds entity + all transitive deps to namespace
       - Does NOT need source arena (reads deps from our tables)
    4. Return temp_arena_id
```

---

## New Methods to Implement

### Session-Level Helpers (on `SQLiteFxtrSession`)

#### `bare_store_entity`
```python
async def bare_store_entity(
    self,
    entity_id: FxtrID,
    storage_location: StorageLocation,
    cbor_bytes: bytes | None,
    deps: dict[DepKind, list[int]],  # internal IDs
) -> int:
    """Store a single entity in the entity tables.

    Non-recursive. All deps must already exist in entity tables.

    Args:
        entity_id: The FxtrID to store
        storage_location: HERE for d/n nodes, NONE for derived nodes
        cbor_bytes: CBOR data for d/n nodes, None for derived nodes
        deps: Map of dep kind to list of internal IDs

    Returns:
        The internal ID of the stored entity
    """
```

**Implementation:**
1. Insert into `entity` table (public_id, storage_location)
2. If cbor_bytes provided, insert into `stored_entity_data`
3. For each dep kind and internal ID, insert into `entity_dep`
4. Return the new internal ID

#### `get_entity_internal_id`
```python
async def get_entity_internal_id(self, entity_id: FxtrID) -> int | None:
    """Look up internal ID for an entity, or None if not in entity tables."""
```

#### `get_entity_deps`
```python
async def get_entity_deps(self, internal_id: int) -> dict[DepKind, list[int]]:
    """Get all deps for an entity from entity_dep table."""
```

#### `get_stored_entity_data`
```python
async def get_stored_entity_data(self, internal_id: int) -> bytes:
    """Fetch CBOR bytes from stored_entity_data table."""
```

### Namespace-Level Helpers (on `SQLitePinningNamespace`)

#### `is_entity_in_namespace`
```python
async def is_entity_in_namespace(self, internal_id: int) -> bool:
    """Check if entity is in this namespace's ownership table."""
```

#### `add_entity_to_namespace`
```python
async def add_entity_to_namespace(self, internal_id: int) -> None:
    """Add entity to namespace ownership and create backref entries.

    Args:
        internal_id: The entity's internal ID (must exist in entity tables)
    """
```

**Implementation:**
1. Insert into `namespace_entity_ownership`
2. Get entity's deps from `entity_dep`
3. For each dep, insert into `namespace_entity_backref`

### Copy Implementation (on `SQLitePinningNamespace`)

#### `_build_tmp_arena_copying_from_external_arena`
```python
async def _build_tmp_arena_copying_from_external_arena(
    self,
    entity_id: FxtrID,
    source: ReadableArena,
) -> int:
    """Copy entity + transitive deps from external arena into namespace.

    Returns internal arena ID containing the copied entities.
    """
```

**Implementation:**

```python
async def _build_tmp_arena_copying_from_external_arena(self, entity_id, source):
    # 1. Create temp arena
    temp_arena_id = await self._create_temp_arena()

    # 2. Pass 1: Ensure all entities exist in entity tables
    await self._ensure_entity_data_exists(entity_id, source)

    # 3. Pass 2: Add all entities to namespace
    await self._add_to_namespace_recursive(entity_id, temp_arena_id)

    return temp_arena_id
```

#### `_ensure_entity_data_exists` (Pass 1)
```python
async def _ensure_entity_data_exists(
    self,
    root_id: FxtrID,
    source: ReadableArena,
) -> None:
    """Ensure entity and all deps exist in entity tables.

    Uses source arena only for entities not already in our tables.
    """
```

**Implementation (iterative with worklist):**

```python
async def _ensure_entity_data_exists(self, root_id, source):
    worklist = [root_id]
    processed = set()  # FxtrIDs fully processed

    while worklist:
        entity_id = worklist.pop()

        if entity_id in processed:
            continue

        # Check if already in entity tables
        internal_id = await self._session.get_entity_internal_id(entity_id)

        if internal_id is not None:
            # Already have data, get deps from our tables
            deps_dict = await self._session.get_entity_deps(internal_id)
            dep_ids = self._deps_dict_to_fxtr_ids(deps_dict)
        else:
            # Need to get deps from source arena
            dep_ids = await self._get_deps_from_source(entity_id, source)

        # Check if all deps are processed
        unprocessed = [d for d in dep_ids if d not in processed]
        if unprocessed:
            worklist.append(entity_id)  # Re-add self
            worklist.extend(unprocessed)
            continue

        # All deps processed, store if needed
        if internal_id is None:
            await self._store_entity_from_source(entity_id, source)

        processed.add(entity_id)
```

#### `_get_deps_from_source`
```python
async def _get_deps_from_source(
    self,
    entity_id: FxtrID,
    source: ReadableArena,
) -> list[FxtrID]:
    """Get all dep FxtrIDs for an entity from source arena."""
```

**Implementation (by node type):**

```python
async def _get_deps_from_source(self, entity_id, source):
    tag = entity_id.get_tag()
    deps = []

    if tag == '.':
        pass  # No deps
    elif tag == '!':
        pass  # No deps
    elif tag == 'd':
        # Load and scan for child refs
        data = await source.load(entity_id)
        deps = self._extract_child_ids(data)
    elif tag == 'n':
        data = await source.load(entity_id)
        deps = self._extract_child_ids(data)
        deps.append(source.unwrap_provenance(entity_id))
    elif tag == 'c':
        deps.append(source.c_node_to_context_data(entity_id))
        deps.append(source.unwrap_provenance(entity_id))
    elif tag == 'p':
        deps.append(source.p_node_to_pathroot(entity_id))
        deps.append(source.unwrap_provenance(entity_id))
        backstep = source.step_back_p_ctx(entity_id)
        if backstep:
            deps.append(backstep)
    elif tag == 'u':
        deps.append(source.unwrap_provenance(entity_id))

    return deps
```

#### `_store_entity_from_source`
```python
async def _store_entity_from_source(
    self,
    entity_id: FxtrID,
    source: ReadableArena,
) -> int:
    """Store entity data from source arena into entity tables.

    All deps must already exist in entity tables.
    Returns the new internal ID.
    """
```

**Implementation (by node type):**

```python
async def _store_entity_from_source(self, entity_id, source):
    tag = entity_id.get_tag()

    if tag == '.':
        return await self._session.bare_store_entity(
            entity_id, StorageLocation.NONE, None, {}
        )
    elif tag == '!':
        return await self._session.bare_store_entity(
            entity_id, StorageLocation.NONE, None, {}
        )
    elif tag == 'd':
        data = await source.load(entity_id)
        cbor_bytes = fxtr_cbor_dumps_with_marker_check(data)
        child_deps = await self._build_child_deps_dict(data)
        return await self._session.bare_store_entity(
            entity_id, StorageLocation.HERE, cbor_bytes, child_deps
        )
    elif tag == 'n':
        data = await source.load(entity_id)
        cbor_bytes = fxtr_cbor_dumps_with_marker_check(data)
        child_deps = await self._build_child_deps_dict(data)
        unwraps_id = source.unwrap_provenance(entity_id)
        unwraps_internal = await self._session.get_entity_internal_id(unwraps_id)
        child_deps[DepKind.UNWRAPS_TO] = [unwraps_internal]
        return await self._session.bare_store_entity(
            entity_id, StorageLocation.HERE, cbor_bytes, child_deps
        )
    elif tag == 'c':
        ctx_id = source.c_node_to_context_data(entity_id)
        ctx_internal = await self._session.get_entity_internal_id(ctx_id)
        unwraps_id = source.unwrap_provenance(entity_id)
        unwraps_internal = await self._session.get_entity_internal_id(unwraps_id)
        deps = {
            DepKind.CTX_NODE: [ctx_internal],
            DepKind.UNWRAPS_TO: [unwraps_internal],
        }
        return await self._session.bare_store_entity(
            entity_id, StorageLocation.NONE, None, deps
        )
    elif tag == 'p':
        pathroot_id = source.p_node_to_pathroot(entity_id)
        pathroot_internal = await self._session.get_entity_internal_id(pathroot_id)
        unwraps_id = source.unwrap_provenance(entity_id)
        unwraps_internal = await self._session.get_entity_internal_id(unwraps_id)
        deps = {
            DepKind.CTX_NODE: [pathroot_internal],
            DepKind.UNWRAPS_TO: [unwraps_internal],
        }
        backstep_id = source.step_back_p_ctx(entity_id)
        if backstep_id:
            backstep_internal = await self._session.get_entity_internal_id(backstep_id)
            deps[DepKind.BACKSTEP] = [backstep_internal]
        return await self._session.bare_store_entity(
            entity_id, StorageLocation.NONE, None, deps
        )
    elif tag == 'u':
        unwraps_id = source.unwrap_provenance(entity_id)
        unwraps_internal = await self._session.get_entity_internal_id(unwraps_id)
        deps = {DepKind.UNWRAPS_TO: [unwraps_internal]}
        return await self._session.bare_store_entity(
            entity_id, StorageLocation.NONE, None, deps
        )
```

#### `_add_to_namespace_recursive` (Pass 2)
```python
async def _add_to_namespace_recursive(
    self,
    root_id: FxtrID,
    temp_arena_id: int,
) -> None:
    """Add entity and all deps to namespace and temp arena.

    All entities must already exist in entity tables (from Pass 1).
    Does not need source arena.
    """
```

**Implementation (iterative with worklist):**

```python
async def _add_to_namespace_recursive(self, root_id, temp_arena_id):
    worklist = [root_id]
    processed = set()

    while worklist:
        entity_id = worklist.pop()

        if entity_id in processed:
            continue

        internal_id = await self._session.get_entity_internal_id(entity_id)

        # Check if already in namespace
        if await self.is_entity_in_namespace(internal_id):
            await self._add_to_arena(internal_id, temp_arena_id)
            processed.add(entity_id)
            continue

        # Get deps from entity tables
        deps_dict = await self._session.get_entity_deps(internal_id)
        dep_ids = self._deps_dict_to_fxtr_ids(deps_dict)

        # Check if all deps processed
        unprocessed = [d for d in dep_ids if d not in processed]
        if unprocessed:
            worklist.append(entity_id)
            worklist.extend(unprocessed)
            continue

        # Add to namespace
        await self.add_entity_to_namespace(internal_id)
        await self._add_to_arena(internal_id, temp_arena_id)
        processed.add(entity_id)
```

---

### Load Implementation (on `SQLiteNamespaceArena`)

#### `load`
```python
async def load(self, node_id: FxtrID) -> FxtrCbor:
    """Load entity data from SQLite.

    For d/n nodes: fetch CBOR, deserialize, track children.
    For p/u nodes: load underlying data, transform children, persist new nodes.
    """
```

**Implementation:**

```python
async def load(self, node_id: FxtrID) -> FxtrCbor:
    self.ensure_active()
    if not self.is_tracked(node_id):
        raise ValueError(f"Node {node_id} is not tracked")

    tag = node_id.get_tag()

    if tag in ('.', '!', 'c'):
        raise ValueError(f"Cannot load contentless node {node_id}")

    internal_id = self._fxtr_id_to_internal[node_id]

    if tag in ('d', 'n'):
        return await self._load_stored_node(node_id, internal_id)
    elif tag == 'p':
        return await self._load_p_node(node_id, internal_id)
    elif tag == 'u':
        return await self._load_u_node(node_id, internal_id)
```

#### `_load_stored_node`
```python
async def _load_stored_node(self, node_id: FxtrID, internal_id: int) -> FxtrCbor:
    """Load a d or n node from stored_entity_data."""
    cbor_bytes = await self._session.get_stored_entity_data(internal_id)
    entity_data = fxtr_cbor_loads(cbor_bytes)

    # Track all children (add to arena's tracked set)
    await self._track_children_from_data(entity_data)

    return entity_data
```

#### `_load_p_node`
```python
async def _load_p_node(self, node_id: FxtrID, internal_id: int) -> FxtrCbor:
    """Load a p node by loading underlying data and wrapping children."""
    # Get what this p-node unwraps to
    unwraps_internal = self._precomp_unwraps_to[internal_id]
    unwraps_id = self._internal_id_to_fxtr[unwraps_internal]

    # Load the underlying data
    unwrapped_data = await self.load(unwraps_id)

    # Get path info from this p-node's payload
    _, payload = node_id.split_tag_and_payload()
    current_path = cbor2.loads(payload)  # [pathroot_hash, indices...]

    # Get pathroot internal ID (same for all p-nodes in this tree)
    pathroot_internal = self._precomp_ctx_node[internal_id]

    # Wrap children as p-nodes
    child_counter = [0]  # Use list for mutation in closure

    def wrap_child(typed_id):
        child_id = typed_id.inner_id
        new_path = current_path + [child_counter[0]]
        child_counter[0] += 1

        new_p_id = FxtrID.from_tag_and_payload('p', cbor2.dumps(new_path))

        # Create and persist the child p-node
        self._create_child_p_node(
            new_p_id,
            pathroot_internal=pathroot_internal,
            backstep_internal=internal_id,
            unwraps_to_id=child_id,
        )

        # Return wrapped reference
        if isinstance(typed_id, RawStrongFxtrID):
            return RawStrongFxtrID(new_p_id)
        else:
            return RawWeakFxtrID(new_p_id)

    return _visit_and_transform_ids(unwrapped_data, wrap_child)
```

#### `_load_u_node`
```python
async def _load_u_node(self, node_id: FxtrID, internal_id: int) -> FxtrCbor:
    """Load a u node by loading underlying data and wrapping children."""
    unwraps_internal = self._precomp_unwraps_to[internal_id]
    unwraps_id = self._internal_id_to_fxtr[unwraps_internal]

    unwrapped_data = await self.load(unwraps_id)

    def wrap_child(typed_id):
        child_id = typed_id.inner_id
        new_u_id = FxtrID.from_tag_and_payload('u', child_id._raw_data_id)

        # Create and persist the child u-node
        self._create_child_u_node(new_u_id, unwraps_to_id=child_id)

        if isinstance(typed_id, RawStrongFxtrID):
            return RawStrongFxtrID(new_u_id)
        else:
            return RawWeakFxtrID(new_u_id)

    return _visit_and_transform_ids(unwrapped_data, wrap_child)
```

#### `_create_child_p_node`
```python
def _create_child_p_node(
    self,
    new_p_id: FxtrID,
    pathroot_internal: int,
    backstep_internal: int,
    unwraps_to_id: FxtrID,
) -> None:
    """Create a child p-node entity during load.

    Creates entity in DB, adds to namespace, adds to arena, updates precomputed state.
    """
```

**Implementation:**
1. Get/create internal ID for unwraps_to_id
2. Call `bare_store_entity` with deps (CTX_NODE, BACKSTEP, UNWRAPS_TO)
3. Add to namespace ownership + backrefs
4. Add to arena membership
5. Update `_fxtr_id_to_internal`, `_internal_id_to_fxtr`, `_precomp_*` dicts

#### `_create_child_u_node`
Similar but simpler (only UNWRAPS_TO dep, no CTX_NODE or BACKSTEP).

---

## Helper Functions Needed

### `_extract_child_ids(entity_data) -> list[FxtrID]`
Walk the CBOR structure and collect all RawStrongFxtrID and RawWeakFxtrID references.

### `_build_child_deps_dict(entity_data) -> dict[DepKind, list[int]]`
Similar to above but returns internal IDs categorized by STRONG_REF vs WEAK_REF.

### `_visit_and_transform_ids(data, transform_fn) -> data`
Walk CBOR structure, apply transform_fn to each ID reference, return transformed data.
Must preserve canonical CBOR order for p-node path indices to be deterministic.

### `_deps_dict_to_fxtr_ids(deps_dict) -> list[FxtrID]`
Convert dict of internal IDs to flat list of FxtrIDs.

---

## Existing Code to Reuse

From `sqlite_arena.py`:
- `_load_into_tracked` - pattern for loading precomputed state
- Table references (`_entity_table`, `_stored_entity_data_table`, etc.)
- `_running_in_queue_slot` - for async coordination

From `memory_arena.py`:
- `_visit_ids_in_canonical_cbor_order` - for deterministic ID ordering
- `_project_ids` - for transforming IDs in CBOR structure

From `entity_core.py`:
- `fxtr_cbor_dumps_with_marker_check` / `fxtr_cbor_loads`
- `RawStrongFxtrID`, `RawWeakFxtrID`, `RawFxtrEntityMarkerID`

---

## Implementation Order

1. **Session-level helpers** (foundation)
   - `get_entity_internal_id`
   - `get_entity_deps`
   - `get_stored_entity_data`
   - `bare_store_entity`

2. **Namespace helpers**
   - `is_entity_in_namespace`
   - `add_entity_to_namespace`

3. **Copy implementation**
   - `_get_deps_from_source`
   - `_store_entity_from_source`
   - `_ensure_entity_data_exists` (Pass 1)
   - `_add_to_namespace_recursive` (Pass 2)
   - `_build_tmp_arena_copying_from_external_arena` (main entry)

4. **Load implementation**
   - `_load_stored_node`
   - `_create_child_p_node` / `_create_child_u_node`
   - `_load_p_node` / `_load_u_node`
   - `load` (main entry)

5. **Utility functions**
   - `_extract_child_ids`
   - `_build_child_deps_dict`
   - `_visit_and_transform_ids`
