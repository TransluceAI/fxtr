# Stage 5: Serdes Robustness - frozendict/frozenset Support

## Goal

Add support for `frozendict` serialization/deserialization in `entity_serdes.py`, and add comprehensive tests for serializing `frozenset`, `frozendict`, `dict`, and `set` fields. Also test EntityRef/FxtrID usage as dict keys and set values with proper canonical CBOR semantics.

## Background

- `frozendict` is already a dependency (v2.4.6+) in `pyproject.toml`
- mashumaro handles `frozenset` by default (serializes as list, deserializes back to frozenset)
- mashumaro does NOT handle `frozendict` by default - needs a custom serialization strategy
- EntityRef and FxtrID are hashable, so they can be used as dict keys or set values
- The system uses `cbor2.dumps(..., canonical=True)` for deterministic serialization

## Key Requirements

### 1. CBOR Non-String Keys for FxtrID/EntityRef

When FxtrID or EntityRef is used as a dict/frozendict key, it must serialize using CBOR's native non-string map key support (i.e., as RawWeakFxtrID/RawStrongFxtrID tags). This preserves the FxtrID type invariants about data storage.

**Simplification**: Support either all-string keys OR all-FxtrID/EntityRef keys in a single dict, but not mixed. This keeps the implementation simpler.

### 2. Canonical Byte Determinism

Serializing to CBOR and converting to bytes must **always produce the exact same bytes** regardless of insertion order. This is critical for content-addressing.

- **Maps (dict/frozendict)**: Already handled by `cbor2.dumps(canonical=True)` which sorts keys by encoded bytes
- **Sets (set/frozenset)**: **NOT handled by default** - mashumaro serializes as lists, which preserve insertion order

### 3. Self-Sorted Sets

To achieve canonical ordering for sets, we sort elements ourselves before serializing to a list:

- **String elements**: Sort by UTF-8 bytes (consistent with CBOR canonical ordering)
- **FxtrID/EntityRef elements**: Sort by `_raw_data_id` bytes (deterministic)
- **Other hashable elements**: Sort by their CBOR-encoded bytes

On deserialization, convert the list back to set/frozenset.

## Current State

The existing test file (`tests/test_entity_serdes.py`) has:
- Tests for dict fields (`EntityWithDict`) - basic dict[str, Any] roundtripping
- No tests for `frozenset`, `frozendict`, or `set` fields
- No tests for EntityRef/FxtrID as container keys/values
- No tests for canonical byte determinism across insertion orders

## Implementation Plan

### Part 1: Write tests for basic collection types (expect some to fail)

- [x] Add test entity classes and helper to track them:
  ```python
  @dataclass(frozen=True)
  class EntityWithSet(DataclassEntity, fxtr_type=...):
      items: set[str]

  @dataclass(frozen=True)
  class EntityWithFrozenset(DataclassEntity, fxtr_type=...):
      items: frozenset[str]

  @dataclass(frozen=True)
  class EntityWithFrozendict(DataclassEntity, fxtr_type=...):
      mapping: frozendict[str, int]
  ```

- [x] Add roundtrip tests for each type:
  - `test_set_field_roundtrip` - PASS (mashumaro handles set)
  - `test_frozenset_field_roundtrip` - PASS (mashumaro handles frozenset)
  - `test_frozendict_field_roundtrip` - FAIL (frozendict → dict, type not preserved)
  - `test_dict_field_roundtrip` - PASS

### Part 2: Write tests for EntityRef/FxtrID in collections (expect some to fail)

- [x] Test EntityRef as frozenset value:
  ```python
  @dataclass(frozen=True)
  class EntityWithRefFrozenset(DataclassEntity, fxtr_type=...):
      refs: frozenset[EntityRef[SimpleEntity]]
  ```
  - `test_entityref_in_frozenset_roundtrip` - PASS (works out of box!)

- [x] Test EntityRef as frozendict key:
  ```python
  @dataclass(frozen=True)
  class EntityWithRefKeyFrozendict(DataclassEntity, fxtr_type=...):
      mapping: frozendict[EntityRef[SimpleEntity], str]
  ```
  - `test_entityref_as_frozendict_key_roundtrip` - FAIL (frozendict → dict)

- [x] Test FxtrID as set value and dict key:
  - `test_fxtrid_in_set_roundtrip` - PASS (works out of box!)
  - `test_fxtrid_as_dict_key_roundtrip` - PASS (works out of box!)

- [x] Verify that refs in these collections can be loaded after roundtrip - PASS

### Part 3: Write canonical determinism tests (expect to fail)

- [x] Test that different insertion orders produce identical CBOR:
  ```python
  def test_set_canonical_ordering():
      # Create sets with different insertion orders
      set1 = {"z", "a", "m"}
      set2 = {"a", "m", "z"}
      set3 = {"m", "z", "a"}

      e1 = EntityWithSet(items=set1)
      e2 = EntityWithSet(items=set2)
      e3 = EntityWithSet(items=set3)

      # All should produce identical CBOR
      assert e1._to_fxtr_cbor() == e2._to_fxtr_cbor() == e3._to_fxtr_cbor()
  ```
  - `test_set_canonical_ordering` - PASS (coincidentally consistent in this session)
  - `test_frozenset_canonical_ordering` - PASS (coincidentally consistent)
  - `test_entityref_set_canonical_ordering` - PASS
  - `test_fxtrid_set_canonical_ordering` - FAIL (FxtrID set order not deterministic!)

### Part 4: Write edge case tests

- [x] Test empty collections (empty set, frozenset, frozendict, dict) - added
- [x] Test nested collections (e.g., `frozendict[str, frozenset[int]]`) - added
- [x] Test that type is preserved (frozenset doesn't become set, etc.) - tested

### Part 5: Run tests and verify expected failures

- [x] Run all new tests, document which ones fail as expected
- [x] Verify no unexpected failures in existing tests

**Summary of failures (5 total):**
1. `test_frozendict_field_roundtrip` - frozendict → dict
2. `test_empty_frozendict_roundtrip` - frozendict → dict
3. `test_nested_frozen_collections_roundtrip` - frozendict → dict
4. `test_entityref_as_frozendict_key_roundtrip` - frozendict → dict
5. `test_fxtrid_set_canonical_ordering` - non-deterministic ordering

---

### Part 6: Implement frozendict serialization strategy

- [x] Create `FrozendictSerializationStrategy` in `entity_serdes.py`
- [x] Register in `DataclassEntity.Config.serialization_strategy`
- [x] Verify frozendict tests now pass

### Part 7: Implement canonical set/frozenset serialization

- [x] Create helper function `_canonical_sort_key` to canonically sort set elements
- [x] Create `SetSerializationStrategy` and `FrozensetSerializationStrategy`
- [x] Register both in `DataclassEntity.Config.serialization_strategy`
- [x] Verify canonical ordering tests now pass

**Key insight**: The strategies are implemented as `Generic[_T_m1]` (or `Generic[_T_m1, _T_m2]` for frozendict) which allows mashumaro to properly propagate type information to nested item serialization. This ensures that `set[EntityRef[T]]` correctly serializes the EntityRef items using their own serialization strategy.

### Part 8: Implement EntityRef/FxtrID as dict keys (if needed)

- [x] Investigate whether mashumaro supports non-string dict keys natively - YES, it does!
- [x] Verify EntityRef/FxtrID key tests pass - all pass

### Part 9: Final verification and cleanup

- [x] Run full test suite, verify all tests pass (93 tests in test_entity_serdes.py)
- [x] Clean up any test scaffolding

### Part 10: Update CODE_OVERVIEW.md

- [x] Document new supported collection types in entity_serdes section
- [x] Note the canonical ordering behavior for sets

## Test Cases Summary

| Test | Description |
|------|-------------|
| `test_set_field_roundtrip` | `set[str]` stores and loads correctly |
| `test_frozenset_field_roundtrip` | `frozenset[str]` stores and loads correctly |
| `test_frozendict_field_roundtrip` | `frozendict[str, int]` stores and loads correctly |
| `test_dict_field_roundtrip` | `dict[str, Any]` stores and loads correctly |
| `test_entityref_in_frozenset_roundtrip` | `frozenset[EntityRef[T]]` roundtrips correctly |
| `test_entityref_as_frozendict_key_roundtrip` | `frozendict[EntityRef[T], V]` roundtrips correctly |
| `test_fxtrid_in_set_roundtrip` | `set[FxtrID]` roundtrips correctly |
| `test_fxtrid_as_dict_key_roundtrip` | `dict[FxtrID, V]` roundtrips correctly |
| `test_set_canonical_ordering` | Different insertion orders → same bytes |
| `test_frozenset_canonical_ordering` | Different insertion orders → same bytes |
| `test_entityref_set_canonical_ordering` | EntityRef sets are canonically ordered |
| `test_empty_collections_roundtrip` | Empty collections preserve type |
| `test_nested_frozen_collections` | Nested frozen types work |

## Notes

- The frozendict import should be at module level in entity_serdes.py since it's already a dependency
- The `_canonical_sort_key` function needs access to `fxtr_cbor_dumps` for the fallback case
- For EntityRef keys in dicts, mashumaro needs to handle non-string keys - may need custom handling or a wrapper type
- Consider whether we need `ChildRef` variants for strong-linked set/dict values (probably not for this initial implementation)
