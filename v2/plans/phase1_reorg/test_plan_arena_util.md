# Test Plan: arena_util.py

This document outlines the tests needed for `fxtr2/data/arena_util.py`.

## Overview

`arena_util.py` provides utility functions for traversing and transforming entity data:
- `visit_ids()`: Visit all IDs in an entity
- `visit_and_project_ids()`: Transform all IDs in an entity
- `visit_and_project_ids_in_canonical_cbor_order()`: Transform IDs in CBOR order
- `async_visit_and_project_ids_in_canonical_cbor_order()`: Async version

## Test Categories

### 1. visit_ids

- [ ] **test_visit_ids_weak_ref**: Calls callback for RawWeakFxtrID
- [ ] **test_visit_ids_strong_ref**: Calls callback for RawStrongFxtrID
- [ ] **test_visit_ids_marker**: Calls callback for RawFxtrEntityMarkerID
- [ ] **test_visit_ids_nested_list**: Visits IDs in nested lists
- [ ] **test_visit_ids_nested_dict**: Visits IDs in nested dicts
- [ ] **test_visit_ids_primitives_ignored**: Primitives don't trigger callback
- [ ] **test_visit_ids_order**: IDs visited in traversal order
- [ ] **test_visit_ids_unsupported_type_raises**: Unknown type raises ValueError

### 2. visit_and_project_ids

- [ ] **test_project_ids_transforms_weak**: Weak refs are transformed
- [ ] **test_project_ids_transforms_strong**: Strong refs are transformed
- [ ] **test_project_ids_transforms_marker**: Markers are transformed
- [ ] **test_project_ids_preserves_type**: Transformed ID keeps wrapper type
- [ ] **test_project_ids_nested_structures**: Works in nested lists/dicts
- [ ] **test_project_ids_primitives_unchanged**: Primitives pass through
- [ ] **test_project_ids_roundtrip**: Identity function returns same structure

### 3. visit_and_project_ids_in_canonical_cbor_order

- [ ] **test_canonical_order_matches_cbor**: Order matches CBOR serialization
- [ ] **test_canonical_order_dict_sorted**: Dict keys processed in sorted order
- [ ] **test_canonical_order_transforms**: Transforms work correctly
- [ ] **test_canonical_order_preserves_structure**: Structure preserved after transform

### 4. async_visit_and_project_ids_in_canonical_cbor_order

- [ ] **test_async_canonical_order**: Async version works
- [ ] **test_async_callback_awaited**: Async callback is awaited
- [ ] **test_async_order_preserved**: Order preserved with async callback
- [ ] **test_async_error_propagation**: Errors in callback propagate

### 5. Edge Cases

- [ ] **test_empty_dict**: Empty dict works
- [ ] **test_empty_list**: Empty list works
- [ ] **test_deeply_nested**: Deeply nested structures work
- [ ] **test_multiple_refs_same_id**: Multiple refs to same ID all transformed

## Implementation Notes

- Tests should be in `tests/test_arena_util.py`
- Most tests are synchronous except async visitor tests
- Use `@pytest.mark.anyio` for async tests
- Create test FxtrCbor structures as fixtures or inline
