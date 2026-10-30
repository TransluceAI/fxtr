# Stage 13 Scratch Notes

## Key Design Decisions (from discussion)

1. **p/u node loads**: Raise an error if user tries to load a p/u node directly from a SQLiteNamespaceArena. The error should say to use `unsafe_lazily_attach_from_arena` to get it into a MemoryStore first. This avoids the need for entity-table writes during load.

2. **Namespace tracking on load**: N/A since we don't load p/u nodes. For d/n node loads, children referenced in the CBOR already exist in the entity table (they were put there during copy). We just need to load their precomp data.

3. **GC behavior**: GC still runs during active sessions, cleaning up namespace metadata (namespace_entity_ownership, namespace_entity_backref). Entity data (entity, stored_entity_data, entity_dep) is never removed while any session is active. Note: current GC already doesn't delete from entity tables - it only removes namespace metadata. So no GC changes needed.

## Why This Works

### InheritedNode loading in MemoryStore

When the MemoryStore has an InheritedNode from a SQLiteNamespaceArena:
- For d/n nodes: calls `parent_arena.load(node_id)` to get CBOR, then adopts children via `_adopt_inheritance_arena_claim`. This calls `is_tracked` and `copy_to_new_arena` on the parent arena - all of which work with the new read-only design.
- For p/u/c/./! nodes: handled internally by `_ensure_own_derived()` - never calls `load` on the arena.

So the SQLiteNamespaceArena's `load` is only ever called for d/n nodes in the InheritedNode path.

### Orphan protection

Under the new design, there are fewer DB arenas (only temp arenas during copy and garbage arenas). Entities in the namespace are protected by:
- Pins (namespace_pin)
- Backrefs from other entities (namespace_entity_backref)
- Arena membership (only in temp/garbage arenas)

Since entity data persists in entity tables regardless, even if an entity is "orphaned" and removed from namespace metadata, loads still work because the data is in stored_entity_data.

### Load flow for d/n nodes

1. Read CBOR from stored_entity_data (read pool)
2. Extract child FxtrIDs from CBOR
3. Look up children's internal IDs (read pool)
4. Load precomp data for children (read pool, via load_precomp_data_for_entities)
5. Add children to Python _tracked set
6. Return CBOR

All reads, no writes. The children already exist in entity tables because they were put there during the copy operation (pin_entity_from_arena).

### What about children that don't exist in entity tables?

For d/n node children: they always exist because when the parent was copied in, all deps were copied too (the copy worklist follows the transitive closure).

For p/u node children referenced in CBOR: these also exist because p/u nodes referenced by d/n nodes were created during the copy operation (the copy worklist creates them in entity tables). The issue was only about LOADING a p/u node, which creates NEW child p/u nodes - but we're raising an error for that.

## Functions in sqlite_ops that change

### No longer needed (removed or simplified):
- `verify_in_namespace_and_add_to_arena` - was used by _track_from_precomputed (DB arena writes)
- `remove_from_arena_and_maybe_rebind` - was used by abandon (DB arena writes)
- `load_and_add_children_to_arena` - p/u child creation + namespace/arena adds. Replace with simpler read-only load.
- `retrieve_pinned_and_create_arena` - replace with read-only pin lookup
- `lookup_in_namespace_and_create_arena` - replace with read-only lookup
- `lookup_and_add_to_arena` - was used by copy_from_arena

### Still needed:
- Session ops, namespace ops, pin ops - unchanged
- `create_arena`, `create_arena_with_entities` - still used by _build_tmp_arena_copying_from_external_arena
- `reassign_arena_to_garbage_session` - still used for temp arena disposal
- Copy worklist ops (copy_discover, copy_save, copy_track_deps, copy_track) - still used by pin_entity_from_arena
- GC ops - unchanged
- `load_precomp_data_for_entities` - still used for precomp loading
- `get_backrefs_for_entity` - still used

### New functions needed:
- `retrieve_pinned_entity` - just look up pin, return (internal_id, public_id), no arena creation
- `lookup_entity_in_namespace` - just check if entity is in namespace, return internal_id or None
- `load_entity_data` - read CBOR from stored_entity_data, extract child IDs and their internal IDs (pure read)
