# SQLite Load/Store Implementation Prep

## Target Methods

Two methods need implementation:
1. `SQLitePinningNamespace._build_tmp_arena_copying_from_external_arena(entity_id, source) -> int`
2. `SQLiteNamespaceArena.load(node_id) -> FxtrCbor`

---

## Key Concepts

### Node ID Types (from entity_core.py)

| Tag | Name | Contentful | Provenance | Notes |
|-----|------|------------|------------|-------|
| `.` | here | No | Ground | Anchor node, payload empty |
| `!` | primitive | No | Ground | Builtin types, payload is ASCII name |
| `d` | data | Yes | Ground | SHA-256 hash of CBOR content |
| `n` | normal | Yes | Provenance | SHA-256 hash; unwraps by replacing all child IDs with their unwrapped versions |
| `c` | context | No | Provenance | Points to d-node with "value" field |
| `p` | path | Depends | Provenance | Path into a subgraph; payload is [hash, path_indices...] |
| `u` | untracked | Depends | Provenance | Wraps another ID with no provenance tracking |

### CBOR Structure

- Entities are dicts with `$fxtr.ctx` field (tag 33000)
- References use tag 33001 (weak) or 33002 (strong)
- `entity_core.fxtr_cbor_dumps/loads` handle encoding/decoding

### Dependencies (DepKind enum in sqlite_arena.py)

| Kind | Meaning |
|------|---------|
| `STRONG_REF` | Strong child link (tag 33002) |
| `WEAK_REF` | Weak reference (tag 33001) |
| `CTX_NODE` | Context data node (for c/p nodes) |
| `BACKSTEP` | Parent p-node (for p-node $fxtr.ctx entries) |
| `UNWRAPS_TO` | What a provenance node unwraps to |

---

## Memory Arena Implementation (Reference)

### Storing (MemoryStore.bare_store)

For each `$fxtr.ctx` tag type:

**`d` nodes (ctx is `.`):**
1. Serialize entity to CBOR bytes
2. Compute SHA-256 hash for ID
3. Collect all child IDs as `requires`
4. Store as `StoredNode` with cbor_bytes

**`n` nodes (ctx is `c`):**
1. Serialize entity to CBOR bytes
2. Compute SHA-256 hash for ID
3. Project all child IDs to their unwrapped versions
4. Recursively store the unwrapped data as a d-node
5. Store as `StoredNode` with unwraps_to pointing to the d-node

**`p` nodes (ctx is `p`):**
- P-nodes are NOT directly stored; they're derived from their pathroot
- Store validates that structure is consistent
- Creates `DerivedNode` with ctx_node pointing to pathroot d-node

**`u` nodes (ctx is `u`):**
1. Project all child IDs to their unwrapped versions
2. Recursively store the unwrapped data
3. Create `DerivedNode` with unwraps_to

### Loading (MemoryStore.bare_load)

**`d`/`n` nodes (StoredNode):**
- Deserialize cbor_bytes directly

**`d`/`n` nodes (LazyStoredNode):**
- Walk up via `wrapped_by` to find a StoredNode
- Deserialize that, then project IDs downward through unwraps chain

**`p` nodes (DerivedNode):**
1. Load the data node it unwraps to
2. Wrap each child ID in the result as a new p-node with extended path
3. Store new child p-nodes as DerivedNodes (with backstep to parent)

**`u` nodes (DerivedNode):**
1. Load the data node it unwraps to
2. Wrap each child ID as a u-node

---

## SQLite Schema (Relevant Tables)

```
entity:
  id (int PK), public_id (bytes, unique), storage_location (enum)

entity_dep:
  requirer_id (FK), required_id (FK), kind (DepKind enum)
  -- indexed on requirer_id

stored_entity_data:
  entity_id (FK PK), data (blob)

namespace_entity_ownership:
  namespace_id (FK), entity_id (FK)

namespace_entity_backref:
  requirer_id, required_id, namespace_id
  -- indexed on (required_id, namespace_id)

arena_membership:
  arena_id (FK), entity_id (FK)
```

---

## SQLiteNamespaceArena Current State

### Already Implemented

- `is_tracked`: Uses in-memory `_tracked` set
- `unwrap_provenance`, `c_node_to_context_data`, `p_node_to_pathroot`, `step_back_p_ctx`: Use precomputed dicts
- `abandon`: Removes from _tracked, schedules DB cleanup
- `copy_to_new_arena`: Copies precomp data, schedules DB arena creation
- `copy_from_arena`: Calls `_build_tmp_arena_copying_from_external_arena`, then adds to this arena

### Precomputed State

```python
_fxtr_id_to_internal: dict[FxtrID, int]
_internal_id_to_fxtr: dict[int, FxtrID]
_precomp_unwraps_to: dict[int, int]      # UNWRAPS_TO deps
_precomp_ctx_node: dict[int, int]        # CTX_NODE deps
_precomp_step_back_p_ctx: dict[int, int] # BACKSTEP deps
```

These are populated by `_load_into_tracked`, which:
1. Queries entity table for FxtrIDs
2. Queries entity_dep for CTX_NODE, BACKSTEP, UNWRAPS_TO deps
3. Expands transitively to ensure all reachable nodes are cached

---

## Implementation Requirements

### `_build_tmp_arena_copying_from_external_arena`

**Purpose:** Copy entity + transitive deps from external arena (e.g. MemoryArena) into SQLite namespace.

**Contract:**
- Input: `entity_id` (FxtrID), `source` (ReadableArena)
- Output: internal arena ID containing the copied entity
- All transitive deps must be in namespace after completion
- Entity data must be stored in `stored_entity_data` table

**Algorithm sketch (from docstring):**
1. Create temp arena in this session
2. Recursively add entities starting from root:
   - If entity already in namespace: just add to arena, done
   - Otherwise: recursively process all dependencies first
   - Then store the entity itself (entity table, stored_entity_data, entity_dep)
   - Add to namespace ownership and backrefs
   - Add to temp arena
3. Return temp arena internal ID

**Key considerations:**
- Need to serialize entities via `fxtr_cbor_dumps_with_marker_check`
- Need to compute all deps by visiting the CBOR structure
- Need to compute `unwraps_to` for n/p/u/c nodes
- Different handling per node type (d/n vs c/p/u)

### `SQLiteNamespaceArena.load`

**Purpose:** Load entity data from SQLite, deserialize, and track children.

**Contract:**
- Input: `node_id` (FxtrID) - must be tracked
- Output: `FxtrCbor` - the entity contents
- Side effect: all child IDs become tracked in the arena

**Algorithm sketch:**
- For d/n nodes: query `stored_entity_data`, deserialize CBOR
- For p nodes: load underlying data, wrap child IDs as p-nodes
- For u nodes: load underlying data, wrap child IDs as u-nodes
- For c/!/. nodes: error (not contentful)

**Key considerations:**
- Need to create new DerivedNode entries for p-node children
- Need to update precomputed state for new children
- Must match the memory arena semantics exactly

---

## What Must Be Stored in DB per Node Type

Based on memory arena patterns, here's what SQLite needs to store:

| Node Type | `entity` row | `stored_entity_data` | `entity_dep` entries |
|-----------|--------------|---------------------|---------------------|
| `.` / `!` | Yes (storage=NONE) | No | None |
| `d` | Yes (storage=HERE) | Yes (CBOR bytes) | STRONG_REF, WEAK_REF for children |
| `n` | Yes (storage=HERE) | Yes (CBOR bytes) | STRONG_REF, WEAK_REF for children + UNWRAPS_TO |
| `c` | Yes (storage=NONE) | No | CTX_NODE to d-node, UNWRAPS_TO |
| `p` | Yes (storage=NONE) | No | CTX_NODE to pathroot d-node, BACKSTEP (if not root), UNWRAPS_TO |
| `u` | Yes (storage=NONE) | No | UNWRAPS_TO |

**Key insight**: Only `d` and `n` nodes have actual data stored. Other nodes are "derived" and reconstructed from their deps.
