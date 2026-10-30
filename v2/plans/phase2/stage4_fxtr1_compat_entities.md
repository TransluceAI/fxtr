# Stage 4: Fxtr1 Compatibility Layer for Entities

(note: after implementing this plan, we moved the implementation to fxtr2.legacy_compat.data instead of fxtr2.data.legacy_compat)

## Goal

Implement a compatibility layer that allows fxtr1 entities to be stored in fxtr2's entity system. This enables gradual migration from v1 to v2 while preserving existing v1 data and allowing v1 task infrastructure to eventually run on top of v2 storage.

## Overview

The compatibility layer wraps v1 entities in a v2-compatible format:

- `LegacyCompat[T]` is a v2 `Entity` subclass that wraps a v1 `EntityBase` instance
- Serialization converts v1's DAG-CBOR (with CID links) to v2's format (with FxtrID links)
- The original v1 CID is preserved for integrity verification on deserialization
- Child entities are recursively wrapped and stored

## Key Design Decisions

### Wrapper Entity Structure

The v2 serialized form of a wrapped v1 entity:

```python
{
    "$fxtr.ctx": RawFxtrEntityMarkerID(context_id),  # from active scope
    "$fxtr.type": RawWeakFxtrID(FxtrID.builtin("compat.legacy_v1")),
    "data": {
        # v1 entity's IPLD structure with cbrrr.CID replaced by RawStrongFxtrID
        "$typespec": {...},  # v1's typespec, preserved as-is
        "field1": ...,
        "field2": RawStrongFxtrID(child_fxtr_id),  # was cbrrr.CID(...)
        ...
    },
    "legacy_cid": bytes,  # raw bytes of original v1 CID for verification
}
```

### Link Replacement

- v1 entities reference children via `cbrrr.CID` objects
- These are replaced with `RawStrongFxtrID` wrappers containing the child's v2 FxtrID
- Strong links ensure proper lifetime management in v2's arena system

### Child Processing Order

Children must be stored before parents (depth-first, leaves first):

1. Collect all reachable entities using v1's `RecursiveSubgraphCollector`
2. Process in reverse order - `RecursiveSubgraphCollector.visited_entities` is in pre-order (parents before children), so `reversed()` gives us leaves first
3. Build CID→FxtrID mapping as each child is stored

### Type Information

- The v1 `$typespec` field is preserved in the "data" structure
- `T` in `LegacyCompat[T]` is for Python type hints only
- v1's deserialization machinery (`ipld_entity_data_to_entity`) reconstructs the correct type

### Context/Provenance

- Wrapped entities use v2's ground context (`.`) from the active scope
- All legacy-wrapped entities are ground nodes in v2's system
- Provenance tracking of legacy data requires wrapping in a separate v2 provenance context

## Serialization Flow

```
LegacyCompat[T]._to_fxtr_cbor()
    │
    ▼
1. Collect ALL entities in subgraph via RecursiveSubgraphCollector (single pass)
    │
    ▼
2. For each NON-ROOT entity (leaves first):
   - Get IPLD: typespec.entity_data_to_ipld(entity)
   - Replace CIDs with RawStrongFxtrID (using cid_to_fxtr_id map)
   - Build wrapper dict
   - Store via scope.store_cbor() → fxtr_id
   - Record: entity.cid → fxtr_id
    │
    ▼
3. For ROOT entity:
   - Get IPLD, replace CIDs, build wrapper dict
   - RETURN the dict (don't store - caller does that)
    │
    ▼
4. Caller (e.g., store() or store_entity()) stores returned CBOR
    │
    ▼
5. Return EntityRef[LegacyCompat[T]]
```

## Deserialization Flow

```
EntityRef[LegacyCompat[T]]
    │
    ▼
1. Load wrapper entity CBOR from scope
    │
    ▼
2. Verify $fxtr.type == "!compat.legacy_v1"
    │
    ▼
3. Collect all RawStrongFxtrID/RawWeakFxtrID in "data"
    │
    ▼
4. For each child FxtrID:
   - Load as LegacyCompat (verify type)
   - Extract legacy_cid from child
   - Build: child_fxtr_id → cbrrr.CID
    │
    ▼
5. Walk "data", replace FxtrID links → cbrrr.CID
    │
    ▼
6. Use v1's ipld_entity_data_to_entity(data, lookup) to reconstruct
    │
    ▼
7. Verify: computed CID == stored legacy_cid (raise if mismatch)
    │
    ▼
8. Return LegacyCompat[T] with .value = reconstructed entity
```

## Implementation Plan

### File: `fxtr2/data/legacy_compat.py`

#### 1. Constants and Type Definition

```python
from fxtr2.data.entity_core import FxtrID, RawStrongFxtrID, RawWeakFxtrID, RawFxtrEntityMarkerID
from fxtr2.data.entity_serdes import Entity, EntityRef, build_converter_for
from fxtr2.data import fxtr_scope
from fxtr.object_model import core as v1_core
from fxtr.object_model import dag_util as v1_dag_util
import cbrrr

LEGACY_V1_TYPE_ID = FxtrID.builtin("compat.legacy_v1")
```

#### 2. CBOR Traversal Helpers

- [ ] `walk_and_collect_cids(ipld_data) -> list[cbrrr.CID]`: Recursively find all CID objects in IPLD structure
- [ ] `walk_and_replace_cids(ipld_data, cid_to_fxtr_id: dict) -> modified_data`: Replace CIDs with RawStrongFxtrID
- [ ] `walk_and_collect_fxtr_ids(data) -> list[FxtrID]`: Find all FxtrID links in modified structure
- [ ] `walk_and_replace_fxtr_ids(data, fxtr_id_to_cid: dict) -> ipld_data`: Replace FxtrID links back to CIDs

These should handle:
- Dicts (recurse into values, and keys if they're CIDs)
- Lists (recurse into elements)
- Primitives (pass through)
- CID / FxtrID objects (collect/replace)

#### 3. LegacyCompat Class

```python
class LegacyCompat(Entity, Generic[T]):
    """Wrapper for v1 entities stored in v2's system."""

    _TYPE_ID = LEGACY_V1_TYPE_ID

    def __init__(self, value: T):
        self._value = value

    @property
    def value(self) -> T:
        return self._value

    @property
    def legacy_cid(self) -> cbrrr.CID:
        # v1 entities already cache their CID in _fxtr_cid
        return v1_core.entity_cid(self._value)
```

#### 4. Serialization (`_to_fxtr_cbor`)

- [ ] Collect ALL entities in subgraph using `RecursiveSubgraphCollector` (single traversal)
- [ ] Store all NON-ROOT children in reverse order (leaves first), building CID→FxtrID map
- [ ] For each child: get IPLD, replace CIDs, build wrapper dict, store via `scope.store_cbor()`
- [ ] For the root: get IPLD, replace CIDs, build wrapper dict, RETURN it (caller stores)

Key insight: `_to_fxtr_cbor()` stores children as side effects but returns the root's CBOR for the caller to store. This avoids mixing two recursive traversals.

```python
def _to_fxtr_cbor(self) -> FxtrCbor:
    scope = fxtr_scope.ACTIVE_SCOPE.get()
    v1_entity = self._value

    # Collect ALL entities in subgraph (single traversal)
    collector = v1_dag_util.RecursiveSubgraphCollector()
    collector.visit_entity(v1_entity)

    cid_to_fxtr_id: dict[cbrrr.CID, FxtrID] = {}

    # Store all NON-ROOT entities (leaves first)
    for entity in reversed(collector.visited_entities):
        entity_cid = v1_core.entity_cid(entity)

        if entity is v1_entity:
            continue  # skip root - we return its CBOR, caller stores it

        if entity_cid in cid_to_fxtr_id:
            continue  # already stored (handles shared children)

        # Get IPLD and replace child CIDs with FxtrIDs
        ipld_data = v1_core.typespec(entity).entity_data_to_ipld(entity)
        modified_data = walk_and_replace_cids(ipld_data, cid_to_fxtr_id)

        # Build and store wrapper for this child
        wrapper = {
            "$fxtr.ctx": fxtr_scope.get_active_context_marker(),
            "$fxtr.type": RawWeakFxtrID(inner_id=scope.track_builtin(self._TYPE_ID)),
            "data": modified_data,
            "legacy_cid": bytes(entity_cid),
        }
        fxtr_id = scope.store_cbor(wrapper)
        cid_to_fxtr_id[entity_cid] = fxtr_id

    # Build and RETURN the root's CBOR (don't store it - caller does that)
    root_ipld = v1_core.typespec(v1_entity).entity_data_to_ipld(v1_entity)
    root_modified = walk_and_replace_cids(root_ipld, cid_to_fxtr_id)

    return {
        "$fxtr.ctx": fxtr_scope.get_active_context_marker(),
        "$fxtr.type": RawWeakFxtrID(inner_id=scope.track_builtin(self._TYPE_ID)),
        "data": root_modified,
        "legacy_cid": bytes(self.legacy_cid),
    }
```

#### 5. Deserialization (`_from_fxtr_cbor`)

- [ ] Verify $fxtr.type is the legacy compat type
- [ ] Extract "data" and "legacy_cid" fields
- [ ] Collect all FxtrID links in "data"
- [ ] Load each child as LegacyCompat, verify type, extract their legacy_cid
- [ ] Build FxtrID→CID mapping from loaded children
- [ ] Walk "data" and replace FxtrID links back to CIDs
- [ ] Use v1's `ipld_entity_data_to_entity()` to reconstruct
- [ ] Verify computed CID matches stored legacy_cid
- [ ] Return LegacyCompat wrapping the reconstructed entity

```python
@classmethod
async def _from_fxtr_cbor(cls, cbor: FxtrCbor, annotated_type: Any | None) -> Self:
    scope = fxtr_scope.ACTIVE_SCOPE.get()

    data = cbor["data"]
    legacy_cid_bytes = cbor["legacy_cid"]
    stored_cid = cbrrr.CID(legacy_cid_bytes)  # cbrrr.CID constructor takes bytes

    # Collect all FxtrID links in data
    child_fxtr_ids = walk_and_collect_fxtr_ids(data)

    # Load all children, collecting both their CID and their v1 entity
    child_data: dict[FxtrID, tuple[cbrrr.CID, v1_core.EntityBase]] = {}
    for child_id in child_fxtr_ids:
        child_ref = EntityRef(child_id, build_converter_for(LegacyCompat))
        child_compat = await child_ref.load(scope)
        if not isinstance(child_compat, LegacyCompat):
            raise ValueError(f"Child {child_id} is not a LegacyCompat entity")
        child_data[child_id] = (child_compat.legacy_cid, child_compat.value)

    # Build both mappings from the collected data
    fxtr_id_to_cid = {fid: cid for fid, (cid, _) in child_data.items()}
    cid_to_entity = {cid: entity for _, (cid, entity) in child_data.items()}

    # Replace FxtrIDs with CIDs in data
    ipld_data = walk_and_replace_fxtr_ids(data, fxtr_id_to_cid)

    # v1 lookup function for reconstruction
    def lookup(cid: cbrrr.CID) -> v1_core.EntityBase:
        if cid in cid_to_entity:
            return cid_to_entity[cid]
        raise ValueError(f"Unknown CID during v1 deserialization: {cid}")

    # Reconstruct using v1's machinery
    v1_entity = v1_core.ipld_entity_data_to_entity(ipld_data, lookup)

    # Verify CID integrity
    computed_cid = v1_core.entity_cid(v1_entity)
    if computed_cid != stored_cid:
        raise ValueError(f"CID mismatch: stored {stored_cid} != computed {computed_cid}")

    return cls(v1_entity)
```

#### 6. Other Entity Methods

- [ ] `_fxtr_type_id()` → return `LEGACY_V1_TYPE_ID`
- [ ] `_fxtr_immutable()` → return `True` (v1 entities are immutable)

Note: No custom `store()` method needed - the standard `store_entity()` function works because `_to_fxtr_cbor()` handles everything.

### Implementation Steps

- [x] Create `fxtr2/data/legacy_compat.py`
- [x] Implement CBOR traversal helpers (walk_and_collect_cids, etc.)
- [x] Implement `LegacyCompat` class with basic structure
- [x] Implement `_to_fxtr_cbor` serialization
- [x] Implement `_from_fxtr_cbor` deserialization
- [x] Add basic tests for simple v1 entities (no children)
- [x] Add tests for v1 entities with children
- [x] Add tests for nested entity graphs
- [x] Add tests for CID verification (detect corruption)
- [x] Update CODE_OVERVIEW.md

### Resolved Issue: Entity Processing Order

**Status**: RESOLVED - implemented PostOrderEntityCollector

#### What Works

Simple entities (no children) serialize and deserialize correctly. The basic flow is working.

#### What Fails

Entities with children fail during serialization when we try to replace CIDs with FxtrIDs.

#### The Error

When processing `V1WithChild` (which has a `child: SimpleV1Entity` field), we get:
```
ValueError: CID CID(bafyreihdot3...) not found in mapping. Available CIDs: []
```

The mapping is empty when processing the first entity.

#### Current Approach

We use `RecursiveSubgraphCollector` which does **pre-order** traversal:
1. Add entity to list
2. Traverse entity's children (recursively)
3. Traverse entity's typespec (which visits schema entities)

This produces: `[V1WithChild, SimpleV1, SimpleV1's schema, V1WithChild's schema]`

We then reverse this, assuming it gives us "leaves first": `[V1WithChild's schema, SimpleV1's schema, SimpleV1, V1WithChild]`

#### Why It Fails

When we process `V1WithChild's schema` (first in reversed list), its IPLD looks like:
```python
{
    '$typespec': {'$kind': 'data_ent_schema'},
    'field_type_specs': {
        'name': {'$kind': 'str'},
        'child': {
            '$kind': 'data_entity',
            'schema': CID(bafyreihdot3...)  # <-- SimpleV1's schema!
        }
    }
}
```

The `child` field's typespec contains a CID reference to `SimpleV1's schema`. But `SimpleV1's schema` is **second** in our processing order - we haven't stored it yet!

#### The Core Problem

Reversing pre-order gives us children-before-parents, but it doesn't account for **dependencies between siblings**. Both schemas are "children" of the root traversal, but `V1WithChild's schema` depends on `SimpleV1's schema`.

#### Questions to Investigate

1. Why does `RecursiveSubgraphCollector` visit `SimpleV1's schema` before `V1WithChild's schema`?
   - Answer: Because SimpleV1 is visited first (as a field value), and its schema is visited during SimpleV1's `traverse_self`.

2. Would post-order traversal fix this?
   - In post-order, an entity is added to the list AFTER all its recursive children are processed.
   - This should mean: if A's traversal causes B to be visited, B appears before A in the list.
   - Need to verify this actually solves the schema dependency issue.

3. Is there something special about how `traverse_self` on TypeSpec_DataEntity works that causes the issue?
   - When we traverse `V1WithChild's schema`'s `field_type_specs`, we call `traverse_self` on each TypeSpec.
   - For `TypeSpec_DataEntity`, this visits the referenced schema entity.
   - But by this point, `SimpleV1's schema` is already in `seen_cids`, so we skip it.
   - The visit happens, but the entity was added to the list earlier (during SimpleV1's processing).

#### Solution: Post-Order Traversal

The fix is to create a custom visitor that uses **post-order** traversal instead of pre-order:
- Traverse all children first
- Add entity to list last

The key insight is that the `seen_cids` check happens during forward traversal. Reversing pre-order output doesn't produce reverse dependency order because the "already seen" decisions were made in forward order. But post-order traversal naturally produces dependency order: when we add an entity to the list, all entities reachable from it have already been added (because we traversed them first).

**Implementation**: Create `PostOrderEntityCollector` in `legacy_compat.py` that mirrors `RecursiveSubgraphCollector` but appends entities AFTER traversing their children.

### Other Implementation Notes

**CID-keyed dicts**: V1 supports `TypeSpec_FrozenDictEntityKey` where dict keys can be entities/CIDs. The `walk_and_replace_cids` helper handles this case, but it's not tested. May need tests if v1 entities use entity-keyed dicts.

**Return type in `_from_fxtr_cbor`**: Changed from `Self` to `LegacyCompat[Any]` due to type checking issues with the async inner function. This works but deviates slightly from the standard Entity pattern.

## Testing Strategy

### Unit Tests

1. **Simple entity roundtrip**: Store/load a v1 entity with no children
2. **Entity with children**: Store/load a v1 entity referencing other entities
3. **Deep nesting**: Store/load a graph with multiple levels of nesting
4. **CID verification**: Modify stored data, verify deserialization fails
5. **Type preservation**: Verify the correct v1 type is reconstructed

### Integration Tests

1. **Mixed v1/v2 storage**: Store legacy entities alongside native v2 entities
2. **Scope lifetime**: Verify legacy entities respect v2 scope lifetime rules
3. **Cross-scope copying**: Copy legacy entities between scopes

## Open Questions / Future Work

1. **Streaming large graphs**: For very large v1 entity graphs, we may want to stream storage rather than collecting all entities upfront

2. **Partial loading**: Currently we load all children to reconstruct a parent. Could we defer child loading until accessed?

3. **v1 task infrastructure**: This plan covers entity storage. The next step would be implementing v1's task/run system on top of v2 storage.

4. **Migration tooling**: Tools to bulk-migrate existing v1 data stores to v2 format
