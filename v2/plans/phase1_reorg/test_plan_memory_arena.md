# Test Plan: memory_arena.py

This document outlines the tests needed for `fxtr2/data/memory_arena.py`.

## Overview

`memory_arena.py` provides the in-memory implementation of fxtr storage:
- `MemoryStore`: The backing store holding entity data
- `MemoryArena`: A "view" into the store with a tracked liveset

## Test Categories

### 1. MemoryStore Basics

- [ ] **test_memory_store_creation**: Can create empty MemoryStore
- [ ] **test_memory_store_new_empty_arena**: Creates a working arena
- [ ] **test_memory_store_multiple_arenas**: Can create multiple arenas from same store

### 2. MemoryArena Basic Operations

- [ ] **test_arena_is_tracked_empty**: New arena tracks nothing
- [ ] **test_arena_track_builtin**: track_builtin() tracks "." and "!" nodes
- [ ] **test_arena_is_tracked_after_store**: Stored entity is tracked
- [ ] **test_arena_close**: Closed arena raises on operations

### 3. Storing Ground Entities (d-nodes)

- [ ] **test_store_simple_entity**: Store entity with basic fields
- [ ] **test_store_returns_d_node**: Stored ground entity returns d-node ID
- [ ] **test_store_same_data_same_id**: Identical entities get same ID (content-addressed)
- [ ] **test_store_different_data_different_id**: Different entities get different IDs
- [ ] **test_store_nested_entities**: Entity containing refs to other entities
- [ ] **test_store_requires_tracked_refs**: Cannot store entity with untracked refs

### 4. Loading Ground Entities (d-nodes)

- [ ] **test_load_simple_entity**: Load stored entity returns same data
- [ ] **test_load_tracks_children**: Loading entity tracks all its child refs
- [ ] **test_load_untracked_raises**: Loading untracked entity raises
- [ ] **test_load_roundtrip**: store -> load roundtrips data correctly

### 5. Storing Provenance Entities (n-nodes)

- [ ] **test_store_n_node**: Entity with c-node context creates n-node
- [ ] **test_n_node_unwraps_to_d_node**: n-node unwraps to corresponding d-node
- [ ] **test_n_node_children_are_provenance**: Children of n-node are also provenance nodes

### 6. Context Nodes (c-nodes)

- [ ] **test_create_c_node**: create_c_node() creates c-node from context data
- [ ] **test_c_node_requires_value_field**: context_data must have "value" field
- [ ] **test_c_node_unwraps_to_value**: c-node unwraps to its "value" field
- [ ] **test_c_node_to_context_data**: c_node_to_context_data() returns the d-node

### 7. Path Nodes (p-nodes)

- [ ] **test_create_p_node**: create_p_node() creates p-node from pathroot data
- [ ] **test_p_node_unwraps_to_root**: p-node with empty path unwraps to root
- [ ] **test_p_node_to_pathroot**: p_node_to_pathroot() returns the pathroot d-node
- [ ] **test_load_p_node_creates_children**: Loading p-node creates p-node children with extended paths
- [ ] **test_step_back_p_ctx**: step_back_p_ctx() navigates from ctx entry to parent

### 8. Untracked Nodes (u-nodes)

- [ ] **test_create_u_node**: create_u_node() creates u-node wrapping data node
- [ ] **test_u_node_unwraps**: u-node unwraps to wrapped node
- [ ] **test_load_u_node**: Loading u-node wraps children as u-nodes

### 9. Unwrapping Provenance

- [ ] **test_unwrap_d_node_returns_none**: Ground nodes return None on unwrap
- [ ] **test_unwrap_here_node_returns_none**: "." returns None on unwrap
- [ ] **test_unwrap_n_node**: n-node unwraps to d-node
- [ ] **test_unwrap_c_node**: c-node unwraps to value
- [ ] **test_unwrap_p_node**: p-node unwraps by following path
- [ ] **test_unwrap_u_node**: u-node unwraps to inner node

### 10. Arena Abandonment

- [ ] **test_abandon_removes_from_tracked**: Abandoned node is no longer tracked
- [ ] **test_abandon_allows_gc**: Abandoned unreferenced node can be garbage collected
- [ ] **test_abandon_keeps_referenced**: Node referenced by other tracked nodes stays alive

### 11. Arena Copying

- [ ] **test_copy_to_new_arena**: copy_to_new_arena() creates independent arena
- [ ] **test_copied_arena_independent**: Changes to original don't affect copy
- [ ] **test_copy_from_arena**: copy_from_arena() imports entities from another arena

### 12. Cached Operations

- [ ] **test_can_use_cached_load_after_load**: Returns True after load()
- [ ] **test_can_use_cached_load_before_load**: Returns False before load()
- [ ] **test_track_cached_store**: track_cached_store() with known ID

### 13. Reference Counting / GC

- [ ] **test_gc_unreferenced_nodes**: Unreferenced nodes are cleaned up
- [ ] **test_gc_preserves_referenced**: Referenced nodes stay alive
- [ ] **test_strong_vs_weak_refs_both_keep_alive**: Both ref types prevent GC

### 14. Error Cases

- [ ] **test_store_non_dict_raises**: Storing non-dict raises ValueError
- [ ] **test_store_missing_ctx_raises**: Missing $fxtr.ctx raises ValueError
- [ ] **test_store_multiple_markers_raises**: Multiple entity markers raises
- [ ] **test_load_contentless_raises**: Loading contentless node (., !) raises
- [ ] **test_operations_on_closed_arena_raise**: All ops on closed arena raise

### 15. Edge Cases

- [ ] **test_empty_entity**: Entity with only $fxtr.ctx
- [ ] **test_large_entity**: Entity with many fields
- [ ] **test_deeply_nested_entity**: Entity with deep nesting
- [ ] **test_entity_with_all_primitive_types**: All CBOR primitive types
- [ ] **test_entity_with_bytes_field**: Binary data in entity

### 16. Integration: Store/Load/Unwrap Flow

- [ ] **test_full_provenance_flow**: Create context, store n-node, unwrap, verify
- [ ] **test_p_node_path_traversal**: Create p-node tree, traverse via loads
- [ ] **test_mixed_ground_and_provenance**: Entity refs mixing d/n/p/u nodes

## Implementation Notes

- Tests should be in `tests/test_memory_arena.py`
- Use `memory_store` and `memory_arena` fixtures from conftest.py
- Many tests need async (load is async) - use `@pytest.mark.anyio`
- Consider parametrizing tests for different node types where applicable
