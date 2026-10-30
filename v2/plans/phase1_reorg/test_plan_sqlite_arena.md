# Test Plan: sqlite_arena.py

This document outlines the tests needed for `fxtr2/data/sqlite_arena.py`.

## Overview

`sqlite_arena.py` provides persistent storage using SQLite:
- `SQLiteFxtrSession`: Manages database connections and session lifecycle
- `SQLitePinningNamespace`: A namespace with named pins for entities
- `SQLiteNamespaceArena`: Arena backed by SQLite storage

The SQLite backend implements `CopyTargetArena` (not full `ReadWriteArena`) - entities
are created in memory arenas then copied to SQLite for persistence.

## Test Categories

### 1. Database Initialization

- [ ] **test_init_database_creates_tables**: init_database() creates all required tables
- [ ] **test_init_database_idempotent**: Calling init_database() twice is safe
- [ ] **test_init_database_creates_garbage_session**: Session 0 is created
- [ ] **test_database_user_version**: User version pragma is set

### 2. SQLiteFxtrSession Lifecycle

- [ ] **test_session_open_creates_db**: SQLiteFxtrSession.open() creates database file
- [ ] **test_session_start_allocates_id**: start() allocates internal session ID
- [ ] **test_session_is_active**: is_active() returns True for active session
- [ ] **test_session_close_marks_garbage**: close() marks session as garbage
- [ ] **test_session_aclose_awaits_cleanup**: aclose() waits for cleanup
- [ ] **test_session_context_manager**: async with SQLiteFxtrSession.open() works
- [ ] **test_session_finalizer**: Session cleanup on GC

### 3. Namespace Operations

- [ ] **test_create_user_namespace**: create_or_retrieve_user_namespace() creates new
- [ ] **test_retrieve_existing_namespace**: Returns existing namespace by name
- [ ] **test_namespace_has_uuid**: Namespace gets unique UUID
- [ ] **test_multiple_namespaces**: Can create multiple namespaces

### 4. Pinning Entities

- [ ] **test_pin_entity_from_arena**: pin_entity_from_arena() copies and pins
- [ ] **test_pin_creates_entry**: Pin creates namespace_pin entry
- [ ] **test_pin_copies_transitive_deps**: All dependencies are copied
- [ ] **test_pin_duplicate_name_raises**: Duplicate pin name raises
- [ ] **test_unpin_entity**: unpin_entity() removes pin

### 5. Retrieving Pinned Entities

- [ ] **test_retrieve_pinned_entity**: retrieve_pinned() returns entity and arena
- [ ] **test_retrieve_nonexistent_raises**: Retrieving unknown pin raises
- [ ] **test_retrieved_entity_loadable**: Retrieved entity can be loaded

### 6. SQLiteNamespaceArena Operations

- [ ] **test_arena_is_tracked**: is_tracked() works for known entities
- [ ] **test_arena_load**: load() returns entity data
- [ ] **test_arena_load_tracks_children**: Loading tracks child entities
- [ ] **test_arena_unwrap_provenance**: unwrap_provenance() works
- [ ] **test_arena_c_node_to_context_data**: c_node_to_context_data() works
- [ ] **test_arena_p_node_to_pathroot**: p_node_to_pathroot() works
- [ ] **test_arena_step_back_p_ctx**: step_back_p_ctx() works
- [ ] **test_arena_abandon**: abandon() removes from tracked set
- [ ] **test_arena_copy_to_new_arena**: Creates independent arena copy

### 7. copy_from_arena

- [ ] **test_copy_from_memory_arena**: Copy entity from MemoryArena to SQLite
- [ ] **test_copy_preserves_data**: Copied entity has same content
- [ ] **test_copy_handles_dependencies**: Dependencies are copied correctly
- [ ] **test_copy_deduplicates**: Already-present entities aren't re-copied

### 8. Entity Reservations

- [ ] **test_reservations_protect_during_copy**: Entities reserved during copy
- [ ] **test_reservations_cleaned_on_session_close**: Reservations cleaned up

### 9. Garbage Collection

- [ ] **test_garbage_session_cleanup**: Garbage sessions eventually cleaned
- [ ] **test_orphaned_entity_cleanup**: Unreferenced entities cleaned up
- [ ] **test_referenced_entity_preserved**: Referenced entities kept

### 10. Concurrent Access

- [ ] **test_multiple_sessions_same_db**: Multiple sessions can access same DB
- [ ] **test_concurrent_reads**: Concurrent reads work
- [ ] **test_concurrent_writes**: Concurrent writes are serialized correctly

### 11. Persistence

- [ ] **test_data_persists_across_sessions**: Close and reopen, data still there
- [ ] **test_pin_persists**: Pins persist across sessions
- [ ] **test_namespace_persists**: Namespaces persist

### 12. Node Type Handling

- [ ] **test_d_node_storage**: d-nodes stored with CBOR data
- [ ] **test_n_node_storage**: n-nodes stored with CBOR data and unwrap info
- [ ] **test_c_node_storage**: c-nodes stored with context reference
- [ ] **test_p_node_storage**: p-nodes stored with path info
- [ ] **test_u_node_storage**: u-nodes stored with wrapped reference
- [ ] **test_builtin_node_storage**: ! nodes stored without data

### 13. Dependency Tracking

- [ ] **test_strong_ref_deps_tracked**: Strong refs create STRONG_REF deps
- [ ] **test_weak_ref_deps_tracked**: Weak refs create WEAK_REF deps
- [ ] **test_ctx_node_deps_tracked**: CTX_NODE deps for c/p nodes
- [ ] **test_backstep_deps_tracked**: BACKSTEP deps for p-node children
- [ ] **test_unwraps_to_deps_tracked**: UNWRAPS_TO deps for provenance nodes

### 14. Error Cases

- [ ] **test_load_unknown_entity_raises**: Loading unknown entity raises
- [ ] **test_operations_on_closed_session_raise**: Operations after close raise
- [ ] **test_invalid_db_path_raises**: Invalid path raises appropriate error

### 15. Integration Tests

- [ ] **test_full_workflow**: Create in memory -> copy to sqlite -> retrieve -> load
- [ ] **test_multiple_pins_same_entity**: Same entity pinned under multiple names
- [ ] **test_pin_entity_with_deep_deps**: Entity with many levels of dependencies

## Implementation Notes

- Tests should be in `tests/test_sqlite_arena.py`
- Use `tmp_db_path` and `tmp_db_url` fixtures for isolated test databases
- All tests involving SQLite are async - use `@pytest.mark.anyio`
- Consider using pytest-timeout for tests involving garbage collection
- May want transaction isolation tests in future
