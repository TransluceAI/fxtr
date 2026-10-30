# Part 3: User-facing API - Scratch Notes

## What Part 3 needs to do

Part 2 implemented the full IndexablePinningBackend on SQLiteNamespaceBackend. The backend
API works in terms of FxtrIDs and ReadableArenas. Part 3 adds the user-facing wrapper that
works in terms of Indexer objects and scopes.

## Key design decisions

### Indexer → FxtrID conversion

The backend takes FxtrIDs but users have Indexer objects. Since Indexers are content-addressed
DataclassEntities, storing them is idempotent — same content always produces the same ID.

For `register_indexer`: store the Indexer into the namespace scope, use the resulting FxtrID.
For `unregister_indexer`: same — store to get the ID (idempotent, entity might already exist).
For `iter_matches`: same — store each constraint's indexer to get its FxtrID.

This means we always call `store_entity(indexer, scope=self.scope)` to get the ID. For
already-registered indexers this is a no-op (content already in the arena). For unregistered
ones it's harmless (stored in memory arena, cleaned up when scope exits).

### iter_matches return type

Backend yields: `(key, FxtrID, bindings, ReadableArena)`
User-facing should yield: `(key, FxtrID, bindings)` — no arena, entities in scope

The user-facing iter_matches attaches each yielded entity to the namespace scope, then lifts
into the active scope. Users access entities through scopes, not arenas.

### list_indexers return type

Backend returns: `(list[(FxtrID, prefix)], ReadableArena)`
User-facing returns: `list[(EntityRef[Indexer], prefix)]` — EntityRefs in scope

All indexer entities from the arena get attached to the namespace scope. Then lifted/copied
into the target scope (following retrieve() pattern).

Note: `EntityRef[Indexer]` might need to be `EntityRef[WellKnownBuiltinIndexer]` or just
`EntityRef` depending on whether the type system can handle the union. Since both
TypedPathValueIndexer and TypedPathExistsIndexer are DataclassEntity with $fxtr.type
discrimination, loading an EntityRef[Indexer] should work — the deserializer will dispatch
to the correct subclass based on $fxtr.type. Need to verify this during implementation.

### FxtrQueryableNamespace as intermediate class

Class hierarchy:
- FxtrNamespace (base: pin/unpin/retrieve/list_pins)
  - FxtrQueryableNamespace (adds: register_indexer/unregister_indexer/list_indexers/iter_matches)
    - SQLiteFxtrNamespace (wraps SQLiteNamespaceBackend)
  - GitFxtrNamespace (wraps GitNamespaceBackend, not indexable)

FxtrQueryableNamespace._get_backend() returns IndexablePinningBackend.
SQLiteFxtrNamespace._get_backend() already returns SQLiteNamespaceBackend which implements
IndexablePinningBackend. So no change needed to the concrete method — we just need the
abstract type annotation to change in FxtrQueryableNamespace.

### Scope validation for iter_matches

iter_matches results go through the same scope validation as retrieve():
- If no explicit scope, the namespace scope must be included in the active scope
- Entities are attached to namespace scope, then lifted into active scope
- copy_into_scope is not needed for iter_matches since results are ephemeral

Actually, do we even need into_scope/copy_into_scope for iter_matches? For retrieve(), it
makes sense because you're getting a specific entity you want to keep. For iter_matches,
you're streaming results and typically processing them on the fly. I think just putting
them in the active scope (with the namespace scope included) is sufficient.

If users need to keep results across scopes, they can copy them explicitly.

## Files to modify

1. `fxtr2/data/fxtr_context_api.py` — add FxtrQueryableNamespace, update SQLiteFxtrNamespace
2. `fxtr2/data/store_indexing/indexing_types.py` — no changes needed (interfaces already defined)
3. `v2/CODE_OVERVIEW.md` — update with indexing info
4. `fxtr2/data/store_indexing/LOCAL_CODE_OVERVIEW.md` — already up to date from Part 1

## Testing considerations

Part 2 tests already cover the backend. Part 3 tests should focus on:
1. Indexer → FxtrID conversion correctness
2. Scope management: entities from list_indexers and iter_matches are accessible
3. IndexerConstraint → IndexerConstraintByID conversion
4. Error cases: querying unregistered indexer, scope mismatch
5. End-to-end: SQLiteFxtrNamespace with register/pin/query/unregister cycle
