# Plan: `SQLiteNamespaceArena.load`

## Goal

Load entity data from SQLite, returning FxtrCbor. For p/u nodes, this creates child entities lazily. All new entities must be saved to entity tables, added to namespace, and tracked in arena.

## Key Insight

P/U node children are created lazily during load, not during copy. When loading a p-node P that unwraps to d-node D:
1. Load D's stored CBOR
2. For each child ID in D's data, create a new p-node with extended path
3. These new p-nodes need to be persisted and tracked

If P unwraps to another p-node P2, we recursively load P2 first (creating P2's children), then wrap those as P1's children.

## Architecture

### Shared Utils: `arena_util.py`

Refactor from memory_arena.py, adding async versions:
- `visit_ids(data, visitor_fn)` - visit all IDs
- `visit_and_project_ids(data, projector_fn)` - transform IDs
- `visit_ids_in_canonical_cbor_order(data, visitor_fn)` - deterministic order
- `visit_and_project_ids_in_canonical_cbor_order(data, projector_fn)` - transform in canonical order
- `async_visit_and_project_ids_in_canonical_cbor_order(data, async_projector_fn)` - **async version** for p/u loading

### Session-Level: `_load_cbor_and_save_children`

```python
async def _load_cbor_and_save_children(
    self,
    conn,  # Open transaction
    fxtr_id: FxtrID,
    internal_id: int,
) -> FxtrCbor:
    """Load entity data, creating child entities in entity tables for p/u nodes.

    Returns the (possibly transformed) FxtrCbor data.
    Does NOT handle namespace or arena membership - caller does that.
    """
```

**For d/n nodes:**
- Fetch CBOR from stored_entity_data
- Deserialize and return

**For p nodes:**
1. Get parent's deps from entity_dep (unwraps_to, ctx_node)
2. Recursively load unwraps_to data
3. Use async visitor to transform children:

```python
async def transform_child(typed_id):
    child_fxtr_id = typed_id.inner_id
    new_path = current_path + [counter]
    counter += 1
    new_p_id = FxtrID.from_tag_and_payload('p', cbor2.dumps(new_path))

    # Check if exists, create if not
    existing = await db_lookup(conn, new_p_id)
    if existing is None:
        child_internal = await db_lookup(conn, child_fxtr_id)
        deps = {CTX_NODE: [ctx_node_internal], UNWRAPS_TO: [child_internal]}
        if isinstance(typed_id, RawFxtrEntityMarkerID):
            deps[BACKSTEP] = [parent_internal]
        await db_insert_entity(conn, new_p_id, None, deps)

    # Return wrapped
    return same_wrapper_type(typed_id, new_p_id)

return await async_visit_and_project_ids_in_canonical_cbor_order(
    underlying_data, transform_child
)
```

**For u nodes:**
Similar but simpler - no CTX_NODE or BACKSTEP, just UNWRAPS_TO.

### Arena-Level: `load`

Uses `_add_entity_to_namespace_if_needed` helper (defined in pt1) for adding children to namespace.

```python
async def load(self, node_id: FxtrID) -> FxtrCbor:
    self.ensure_active()
    if not self.is_tracked(node_id):
        raise ValueError(...)

    tag = node_id.get_tag()
    if tag in ('.', '!', 'c'):
        raise ValueError("Cannot load contentless node")

    internal_id = self._fxtr_id_to_internal[node_id]

    async with self._running_in_queue_slot(...):
        async with self._session._async_engine.begin() as conn:
            # Load data (creates child entities in entity tables)
            data = await self._session._load_cbor_and_save_children(
                conn, node_id, internal_id
            )

            # Extract child IDs from returned data
            child_ids = []
            visit_ids(data, lambda tid: child_ids.append(tid.inner_id))

            # Add children to namespace and arena
            for child_id in child_ids:
                child_internal = await db_lookup(conn, child_id)
                await self._namespace._add_entity_to_namespace_if_needed(conn, child_internal)
                # INSERT INTO arena_membership (inline)

        # Update Python tracking state using existing helper
        await self._load_into_tracked(child_ids)

    return data
```

## P-Node Child Deps

For a child p-node at index i:
- **CTX_NODE**: Same pathroot d-node as parent (inherited)
- **UNWRAPS_TO**: The original child ID from underlying data
- **BACKSTEP**: Parent p-node's internal ID, but **only if** the child is a `RawFxtrEntityMarkerID` (the $fxtr.ctx marker)

## U-Node Child Deps

For a child u-node:
- **UNWRAPS_TO**: The original child ID from underlying data
- No CTX_NODE or BACKSTEP

## Why This Is Simpler

1. **Session method just returns data** - no need to track/return new entities list
2. **Arena extracts children with `visit_ids`** - generic, works for any node type
3. **Reuse `_load_into_tracked`** - already handles precomp dict updates
4. **Async visitors** - clean inline transformation, no two-pass hack

## Transaction Scope

One transaction for the entire load:
- Session method creates child entities in entity tables
- Arena adds children to namespace and arena tables
- All atomic

## Edge Cases

**Already-existing children**: If a p-node child already exists in entity tables (from a previous load), the session method just skips creation. The arena still adds it to namespace/arena if needed.

**Nested p-nodes**: If P1 unwraps to P2 (another p-node), loading P1 recursively loads P2 first. P2's children are created, then P1's children wrap those.

**Empty underlying data**: If the underlying d-node has no children, we return the data as-is.
