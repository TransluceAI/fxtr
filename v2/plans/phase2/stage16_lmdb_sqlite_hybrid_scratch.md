# Stage 16: LMDB-SQLite Hybrid - Scratch Notes

## Design Discussion Summary

### Key decisions from discussion:

1. **Blob header**: 3 x u64 offsets (24 bytes), pointing to fxtr_id, cbor, and metadata sections
2. **Precomp closedness**: Store the full transitive precomp closure. Provenance chains are short in practice (0-1). Revisit if storage overhead becomes a problem.
3. **Internal IDs as LMDB keys**: Safe because entity IDs are never reused currently. Note: future entity-level GC will need to also clean up LMDB.
4. **Two-phase write crash recovery**: Orphaned LMDB blobs and NULL public_id rows are acceptable. Future entity-level GC can clean these up.
5. **LMDB buffer lifetime**: Do a `bytes()` copy for now. Simpler and still much faster than sqlite thread dispatch.
6. **In-memory databases**: Disabled entirely. Tests should use temporary file-based DBs. Simplifies code (no memory keepalive connections, no URI parsing).
7. **GLOBAL_BACKING_STORE / MemoryStore**: Independent from this LMDB usage. Don't unify.
8. **LMDB thread safety**: Per-operation read transactions. LMDB env is thread-safe, just need separate transactions per thread.
9. **LMDB data persistence outside namespaces**: OK for now. No LMDB-level GC.
10. **stored_entity_data table**: Dropped entirely. Only "data1/1" storage location.
11. **Derived nodes in LMDB**: All entity types get LMDB blobs. Derived nodes have empty CBOR bytes but still have FxtrID + metadata. storage_location is "data1/1" for all.

### Who uses entity_dep (must keep this table):
- `_add_entity_to_namespace_if_needed`: copies deps to namespace_entity_backref
- `run_one_gc_step`: fetches deps for GC cascade
- `copy_track_deps`: loads dep internal IDs for the copy worklist

### Who uses stored_entity_data (removing this):
- `load_entity_cbor_and_children`: replaced by LMDB read
- `load_entity_flat`: replaced by LMDB read
- `copy_save`: replaced by LMDB write

### Who uses load_precomp_data_for_entities (replacing with LMDB read):
- `_load_into_tracked` in SQLiteNamespaceArena: will read LMDB blobs instead

### Write path: computing LMDB metadata at write time
- Deps are known from FxtrEntityFlatData
- Internal IDs of deps are known from worklist processing
- For each dep, read its LMDB blob to get its precomp map
- Merge all deps' precomp maps + add own entry = closed precomp map
- This works because deps are written before parents (depth-first order)

### Read path: _load_into_tracked from LMDB
- For each internal ID in frontier:
  - Read LMDB blob (synchronous mmap access)
  - Decode metadata (CBOR decode)
  - Populate _precomp_* dicts from precomp map entries
- Can batch reads in a single LMDB read transaction for efficiency

### Read path: load() from LMDB
- Look up internal_id from _fxtr_id_to_internal (already in memory)
- Read LMDB blob synchronously
- Extract CBOR bytes
- Decode metadata, populate precomp dicts for children
- Deserialize CBOR with fxtr_cbor_loads
- Return

### LMDB metadata format (CBOR-encoded):
```python
{
    "deps": [[internal_id, kind_str], ...],
    "precomp": {
        internal_id: [fxtr_id_bytes, unwraps_to_or_none, ctx_node_or_none, backstep_or_none],
        ...
    }
}
```

### LMDB environment management:
- Stored on ConnectionWorkerPoolGroup (shared between read/write pools)
- Opened during database setup
- Path: `{db_path}.data1.lmdb`
- Sub-database: `data1/1`
- map_size: 100GB (same as existing LMDBBytesBackingStore)
- Closed when pool group is closed
- _SharedSqlitePoolGroupManager already ensures one pool group per DB path

### Functions that need LMDB env passed in:
- copy_save (write path, runs in write pool thread)
- Any new LMDB write functions

### Functions that access LMDB directly (synchronous):
- SQLiteNamespaceArena._load_into_tracked
- SQLiteNamespaceArena.load
- SQLiteNamespaceArena.load_flat
