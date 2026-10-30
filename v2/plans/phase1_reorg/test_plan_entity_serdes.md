# Test Plan: entity_serdes.py

This document outlines the tests needed for `fxtr2/data/entity_serdes.py`.

## Overview

`entity_serdes.py` provides the high-level API for working with fxtr entities:
- `Entity`: Abstract base class for serializable entities
- `DataclassEntity`: Convenient base for dataclass entities
- `EntityRef[T]`: Lazy reference to an entity
- `ACTIVE_ARENA` / `ACTIVE_NODE_CONTEXT`: Context variables
- `OutputStream`: Experimental streaming interface

## Test Categories

### 1. Entity Base Class

- [ ] **test_entity_is_abstract**: Cannot instantiate Entity directly
- [ ] **test_entity_subclass_requires_methods**: Subclass must implement abstract methods
- [ ] **test_entity_fxtr_type**: _fxtr_type() returns type ID
- [ ] **test_entity_fxtr_immutable**: _fxtr_immutable() returns immutability flag

### 2. DataclassEntity Basics

- [ ] **test_dataclass_entity_creation**: Can create DataclassEntity subclass
- [ ] **test_dataclass_entity_frozen**: Entity is frozen (immutable)
- [ ] **test_dataclass_entity_type_registration**: Type is registered for deserialization
- [ ] **test_dataclass_entity_fields**: Fields are preserved in serialization

### 3. Entity Serialization (to_fxtr_cbor)

- [ ] **test_to_fxtr_cbor_basic**: Simple entity serializes to FxtrCbor
- [ ] **test_to_fxtr_cbor_has_ctx**: Result has $fxtr.ctx field
- [ ] **test_to_fxtr_cbor_has_type**: Result has $fxtr.type field
- [ ] **test_to_fxtr_cbor_primitive_fields**: int, str, bool, float fields
- [ ] **test_to_fxtr_cbor_list_fields**: List fields serialize correctly
- [ ] **test_to_fxtr_cbor_dict_fields**: Dict fields serialize correctly
- [ ] **test_to_fxtr_cbor_nested_entity**: EntityRef fields become refs
- [ ] **test_to_fxtr_cbor_optional_fields**: Optional[T] fields work

### 4. Entity Deserialization (from_fxtr_cbor)

- [ ] **test_from_fxtr_cbor_basic**: Deserialize simple entity
- [ ] **test_from_fxtr_cbor_type_dispatch**: Correct type selected by $fxtr.type
- [ ] **test_from_fxtr_cbor_unknown_type_raises**: Unknown type raises error
- [ ] **test_from_fxtr_cbor_nested_refs**: EntityRefs are recreated
- [ ] **test_from_fxtr_cbor_roundtrip**: to_fxtr_cbor -> from_fxtr_cbor roundtrips

### 5. EntityRef

- [ ] **test_entity_ref_creation**: Create EntityRef from FxtrID
- [ ] **test_entity_ref_ref_property**: .ref returns the FxtrID
- [ ] **test_entity_ref_load**: await ref.load() returns entity
- [ ] **test_entity_ref_load_requires_arena**: load() raises without ACTIVE_ARENA
- [ ] **test_entity_ref_equality**: EntityRefs with same ID are equal
- [ ] **test_entity_ref_hash**: EntityRefs can be used as dict keys

### 6. Entity.store()

- [ ] **test_store_returns_entity_ref**: store() returns EntityRef
- [ ] **test_store_requires_active_arena**: store() raises without ACTIVE_ARENA
- [ ] **test_store_uses_active_context**: Uses ACTIVE_NODE_CONTEXT for $fxtr.ctx
- [ ] **test_store_idempotent**: Storing same entity twice returns same ref

### 7. ACTIVE_ARENA Context Variable

- [ ] **test_active_arena_default_none**: Default value is None
- [ ] **test_active_arena_set_get**: Can set and get arena
- [ ] **test_active_arena_reset**: Can reset to previous value
- [ ] **test_active_arena_contextvar_isolation**: Isolated across async tasks

### 8. ACTIVE_NODE_CONTEXT Context Variable

- [ ] **test_active_node_context_default**: Default is "." ground node
- [ ] **test_active_node_context_set_get**: Can set and get context
- [ ] **test_active_node_context_affects_store**: Stored entities use context

### 9. Caching (_fxtr_cached_ref_weakref)

- [ ] **test_cached_ref_reused**: Same entity instance reuses cached ref
- [ ] **test_cached_ref_weakref**: Cache uses weak reference
- [ ] **test_cached_ref_cleared_after_gc**: Ref cleared when entity GC'd

### 10. Type Discrimination

- [ ] **test_multiple_entity_types**: Multiple DataclassEntity subclasses
- [ ] **test_type_discrimination_on_load**: Correct type instantiated on load
- [ ] **test_type_id_uniqueness**: Each type has unique ID
- [ ] **test_builtin_type_id**: Can use FxtrID.builtin() for type

### 11. Complex Field Types

- [ ] **test_entity_with_entity_ref_field**: Field of type EntityRef[T]
- [ ] **test_entity_with_list_of_refs**: Field of type list[EntityRef[T]]
- [ ] **test_entity_with_optional_ref**: Field of type Optional[EntityRef[T]]
- [ ] **test_entity_with_union_types**: Field with Union types
- [ ] **test_entity_with_bytes_field**: Field of type bytes

### 12. OutputStream (Experimental)

- [ ] **test_output_stream_creation**: Create OutputStream
- [ ] **test_output_stream_emit**: emit() sends items
- [ ] **test_output_stream_finish**: finish_with_result() closes stream
- [ ] **test_output_stream_substream**: make_substream() creates child stream

### 13. Error Cases

- [ ] **test_store_without_arena_raises**: Raises RuntimeError
- [ ] **test_load_without_arena_raises**: Raises RuntimeError
- [ ] **test_invalid_fxtr_cbor_raises**: Malformed data raises
- [ ] **test_missing_required_field_raises**: Missing field raises
- [ ] **test_wrong_field_type_raises**: Type mismatch raises

### 14. Integration Tests

- [ ] **test_full_store_load_cycle**: Create -> store -> load -> verify
- [ ] **test_nested_entities_store_load**: Entity containing other entities
- [ ] **test_circular_refs_prevented**: Content-addressing prevents cycles
- [ ] **test_large_entity_graph**: Many interconnected entities

## Implementation Notes

- Tests should be in `tests/test_entity_serdes.py`
- Use `active_arena` fixture to set up ACTIVE_ARENA
- Most store/load tests need async - use `@pytest.mark.anyio`
- Create test entity classes as fixtures or inline in tests
- May need to reset type registry between tests
