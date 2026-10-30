# Test Plan: entity_core.py

This document outlines the tests needed for `fxtr2/data/entity_core.py`.

## Overview

`entity_core.py` is the foundation of the fxtr system. It defines:
- `FxtrID`: The immutable identifier class
- CBOR encoding/decoding with custom tags
- Node type handling (., !, d, n, c, p, u)

## Test Categories

### 1. FxtrID Construction and Basic Operations

- [ ] **test_fxtr_id_immutability**: Verify FxtrID is immutable (setattr raises AttributeError)
- [ ] **test_fxtr_id_equality**: Two FxtrIDs with same raw_data_id are equal, different are not
- [ ] **test_fxtr_id_hash**: Equal FxtrIDs have same hash, can be used as dict keys
- [ ] **test_fxtr_id_equality_ignores_provenance_parent**: Two FxtrIDs with same data but different provenance parents are equal
- [ ] **test_fxtr_id_from_tag_and_payload**: Create FxtrID from tag and payload
- [ ] **test_fxtr_id_split_tag_and_payload**: Round-trip tag/payload extraction
- [ ] **test_fxtr_id_get_tag**: Returns correct single-character tag

### 2. FxtrID String Serialization

For each node type (., !, d, n, c, p, u):
- [ ] **test_{type}_id_string_roundtrip**: local_id_string() -> from_id_string() roundtrips
- [ ] **test_{type}_id_summary**: local_id_summary() returns truncated but valid representation

Additional:
- [ ] **test_id_string_with_provenance_parent**: id_string() includes @provenance_part
- [ ] **test_from_id_string_with_provenance**: from_id_string() parses X@Y format
- [ ] **test_from_id_string_empty_raises**: Empty string raises ValueError
- [ ] **test_from_id_string_unknown_tag_raises**: Unknown tag raises ValueError

### 3. Node Type: "." (here/ground)

- [ ] **test_here_node_creation**: Create "." node with empty payload
- [ ] **test_here_node_payload_must_be_empty**: Non-empty payload raises error in from_id_string
- [ ] **test_here_node_id_string**: Returns exactly "."

### 4. Node Type: "!" (primitive/builtin)

- [ ] **test_builtin_creation**: FxtrID.builtin("name") creates correct node
- [ ] **test_builtin_id_string**: Returns "!name" format
- [ ] **test_builtin_roundtrip**: Various builtin names roundtrip correctly
- [ ] **test_builtin_utf8_names**: UTF-8 names work correctly

### 5. Node Type: "d" (data)

- [ ] **test_d_node_creation**: Create d node with SHA-256 hash payload
- [ ] **test_d_node_id_string**: Returns base32-encoded hash
- [ ] **test_d_node_roundtrip**: Roundtrips through string serialization

### 6. Node Type: "n" (normal/provenance)

- [ ] **test_n_node_creation**: Create n node with SHA-256 hash payload
- [ ] **test_n_node_id_string**: Returns "n" + base32-encoded hash
- [ ] **test_n_node_roundtrip**: Roundtrips through string serialization

### 7. Node Type: "c" (context)

- [ ] **test_c_node_creation**: Create c node with SHA-256 hash payload
- [ ] **test_c_node_id_string**: Returns "c" + base32-encoded hash
- [ ] **test_c_node_roundtrip**: Roundtrips through string serialization

### 8. Node Type: "p" (path)

- [ ] **test_p_node_creation**: Create p node with [hash, indices...] payload
- [ ] **test_p_node_id_string_empty_path**: Returns "pXXXX[]" for empty path
- [ ] **test_p_node_id_string_with_path**: Returns "pXXXX[varint...]" with path indices
- [ ] **test_p_node_roundtrip**: Roundtrips through string serialization
- [ ] **test_p_node_various_path_lengths**: Test paths of length 0, 1, 5, 100
- [ ] **test_p_node_large_indices**: Path indices > 15 use continuation encoding

### 9. Node Type: "u" (untracked)

- [ ] **test_u_node_creation**: Create u node wrapping another FxtrID
- [ ] **test_u_node_wrapping_d_node**: u node wrapping a d node
- [ ] **test_u_node_wrapping_here_node**: u node wrapping "."
- [ ] **test_u_node_id_string**: Returns "uXXX" where XXX is inner node's id_string
- [ ] **test_u_node_roundtrip**: Roundtrips through string serialization
- [ ] **test_u_node_nested**: u node wrapping u node (if valid)

### 10. Varint32 Encoding (used by p-nodes)

- [ ] **test_encode_varint32_zero**: 0 encodes to "0"
- [ ] **test_encode_varint32_small**: Values 1-15 encode to single character
- [ ] **test_encode_varint32_continuation**: Values >= 16 use continuation
- [ ] **test_encode_varint32_large**: Large values encode correctly
- [ ] **test_decode_varint32_roundtrip**: encode -> decode roundtrips
- [ ] **test_encode_varint32_negative_raises**: Negative values raise ValueError

### 11. Base32 Encoding Helpers

- [ ] **test_b32enc_dec_roundtrip**: _b32enc -> _b32dec roundtrips
- [ ] **test_b32dec_case_insensitive**: Decoding is case-insensitive
- [ ] **test_b32enc_no_padding**: Encoded strings have no '=' padding

### 12. CBOR Wrapper Classes

- [ ] **test_raw_weak_fxtr_id**: RawWeakFxtrID wraps FxtrID correctly
- [ ] **test_raw_strong_fxtr_id**: RawStrongFxtrID wraps FxtrID correctly
- [ ] **test_raw_entity_marker_id**: RawFxtrEntityMarkerID wraps FxtrID correctly
- [ ] **test_raw_id_classes_frozen**: All Raw*ID classes are frozen dataclasses

### 13. CBOR Encoding/Decoding

- [ ] **test_fxtr_cbor_dumps_loads_roundtrip**: Simple entity roundtrips
- [ ] **test_fxtr_cbor_tag_33000**: Entity marker uses tag 33000
- [ ] **test_fxtr_cbor_tag_33001**: Weak ref uses tag 33001
- [ ] **test_fxtr_cbor_tag_33002**: Strong ref uses tag 33002
- [ ] **test_fxtr_cbor_nested_entities**: Entities containing entity refs
- [ ] **test_fxtr_cbor_primitive_types**: All primitive types (None, bool, int, float, str, bytes, list, dict)

### 14. fxtr_cbor_dumps_with_marker_check

- [ ] **test_marker_check_valid_entity**: Valid entity passes check
- [ ] **test_marker_check_non_dict_raises**: Non-dict raises ValueError
- [ ] **test_marker_check_missing_ctx_raises**: Missing $fxtr.ctx raises ValueError
- [ ] **test_marker_check_wrong_ctx_type_raises**: Wrong type for $fxtr.ctx raises ValueError
- [ ] **test_marker_check_multiple_markers_raises**: Multiple RawFxtrEntityMarkerID raises ValueError

### 15. fxtr_cbor_lift_to_provenance

- [ ] **test_lift_to_provenance_weak_id**: Lifts RawWeakFxtrID
- [ ] **test_lift_to_provenance_strong_id**: Lifts RawStrongFxtrID
- [ ] **test_lift_to_provenance_marker_id**: Lifts RawFxtrEntityMarkerID
- [ ] **test_lift_to_provenance_nested**: Lifts IDs in nested structures
- [ ] **test_lift_to_provenance_missing_parent_raises**: Raises if provenance_parent missing
- [ ] **test_lift_to_provenance_multiple_levels**: Can lift multiple levels

### 16. FxtrCbor Type

- [ ] **test_fxtr_cbor_type_accepts_primitives**: Type accepts None, int, float, bool, str, bytes
- [ ] **test_fxtr_cbor_type_accepts_containers**: Type accepts list, dict
- [ ] **test_fxtr_cbor_type_accepts_raw_ids**: Type accepts Raw*FxtrID classes

## Implementation Notes

- Tests should be organized in `tests/test_entity_core.py`
- Use the `ground_context` fixture for "." nodes
- Most tests are unit tests (no async needed)
- Consider property-based testing (hypothesis) for roundtrip tests in future
