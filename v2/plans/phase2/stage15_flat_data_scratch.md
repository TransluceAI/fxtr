# Stage 15: Flat Data Optimization — Scratch Notes

## Problem Analysis: Redundant CBOR Round-trips

### Hot Path 1: Loading through stacked scopes

When a node is loaded through N stacked memory scopes (each backed by its own MemoryStore),
the current `_ensure_own` path does:

```
For each intermediate scope:
  1. parent_arena.load(node_id)
     → SQLiteNamespaceArena: DB fetch bytes → fxtr_cbor_loads(bytes) → FxtrCbor
     → MemoryArena: bare_load → _ensure_own (recurse) → backing_store.get → fxtr_cbor_loads
  2. bare_store(loaded_data)
     → fxtr_cbor_dumps_with_marker_check(entity) → SHA-256 hash → save bytes
     → For n-nodes: also visit_and_project_ids to compute unwrap, then recursively bare_store
```

So with N scope levels, there are N-1 deserialize-serialize cycles, each involving CBOR
parsing, hash computation, and child walking.

### Hot Path 2: Pinning to SQLite

`pin_entity_from_arena` → `_build_tmp_arena_copying_from_external_arena` →
`fetch_data_and_deps_from_arena` which does `load` + `dumps`. One unnecessary round-trip.

## What bare_store actually needs for d/n nodes (vs what it computes)

For **d-nodes**, `bare_store` does:
- `fxtr_cbor_dumps_with_marker_check(entity)` — serializes to get bytes
- `FxtrID.from_tag_and_payload("d", hashlib.sha256(entity_bytes).digest())` — hashes for ID
- `visit_ids(entity, _visitor)` — walks children to compute requires + strong_linked_refs
- Saves bytes to backing store

But if we already have the bytes + ID + child deps, ALL of this is redundant.

For **n-nodes**, `bare_store` additionally:
- Calls `visit_and_project_ids` to strip provenance from children → unwrapped data
- Recursively calls `self.bare_store(unwrap_data)` to store the unwrapped d-node

But the unwrapped d-node already exists (the InheritedNode has `unwraps_to` set during
adoption, and the unwrapped node is already in `_entity_states`). So the projection +
recursive store is completely unnecessary.

## Key design decision: What does load_flat return?

For d/n nodes:
- `entity_bytes`: raw CBOR bytes (not deserialized)
- `deps`: list of (FxtrID, DepKind) — STRONG_REF/WEAK_REF for CBOR body children,
  plus UNWRAPS_TO for n-nodes

For derived nodes (c/p/u/./!):
- `entity_bytes`: None
- `deps`: CTX_NODE, BACKSTEP, UNWRAPS_TO as appropriate

This matches what `fetch_data_and_deps_from_arena` returns, allowing it to be dropped.

## Key design decision: load_flat tracks children

`load_flat` MUST track children in the calling arena (same as `load`), because:
- `_ensure_own` calls `_adopt_inheritance_arena_claim(child_id, parent_arena)` which
  checks `parent_arena.is_tracked(child_id)` — would fail if children aren't tracked
- The copy worklists also expect children to be tracked in the source arena

## Key design decision: Adoption in _ensure_own

In `_ensure_own` for d/n InheritedNodes, we only adopt STRONG_REF and WEAK_REF deps
(the CBOR body children), NOT the UNWRAPS_TO dep. The unwraps_to node was already adopted
when the InheritedNode was created (in `_adopt_inheritance_arena_claim`'s recursive call).

## Key design decision: Removing LazyStoredNode

LazyStoredNode was a storage optimization where a d-node's bytes were freed when a wrapping
n-node existed (the d-node could be reconstructed by loading the n-node and projecting IDs).

Removing it:
- Eliminates the wrapper-walking reconstruction path in `bare_load` (lines 1134-1168)
- Eliminates the `into_lazy()` downgrade in `bare_store`'s final cleanup
- Every d/n node always has its own bytes in the backing store
- `wrapped_by` field stays for GC (prevents unwrapped node from being collected while
  a wrapper exists)

## Key design decision: No hash verification in bare_store_flat

Trust the source arena. The hash is already embedded in the FxtrID, and the bytes were
already verified when first stored. Verification should happen at ownership boundaries
(e.g., remote sync), not on every internal store.

## Where DepKind should live

Currently `DepKind` is in `arena_util.py`. `FxtrEntityFlatData` (which uses DepKind)
belongs in `abstract_arena.py` alongside `ReadableArena`. So move `DepKind` to
`abstract_arena.py` and have `arena_util.py` import it from there.
