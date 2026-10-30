# 1bcd Scratch Notes

## Serialization challenge

The `path` field in TypedPathValueIndexer/TypedPathExistsIndexer is:
```python
path: tuple[str | int | VarMarker | DerefMarker | TypedDerefMarker, ...]
```

mashumaro can't roundtrip this union automatically because:
- VarMarker serializes to `{}`, DerefMarker also serializes to `{}` → ambiguous
- TypedDerefMarker has `frozenset[FxtrID]` which needs CBOR tag handling
- On deserialization, mashumaro can't distinguish between the dict variants

### Approaches considered

**A: Override _to_fxtr_cbor / _from_fxtr_cbor entirely**
- Lose mashumaro's automatic handling for all fields
- Have to manually handle supported_types (frozenset[FxtrID]), var_types (tuple[enum]), etc.
- Fragile if we add fields later

**B: SerializationStrategy per marker type in DataclassEntity.Config**
- Would affect all DataclassEntity subclasses (pollutes global config)
- mashumaro's union deserialization still wouldn't distinguish VarMarker from DerefMarker

**C: Custom Config on indexer subclasses**
- mashumaro doesn't merge parent Configs, would need to duplicate all parent strategies
- Fragile

**D: Wrapper type implementing SerializableType**
- Change `path: tuple[...]` to `path: PathElementSequence`
- PathElementSequence implements mashumaro.types.SerializableType
- Handles its own serialization: elements become tagged dicts in CBOR
- Clean but changes the API from tuple to a custom type

**E: Mashumaro discriminated union hierarchy** ← CHOSEN
- Make PathElement a DataClassDictMixin base with class-level Discriminator on `_k` field
- Each variant (PathKey, PathIndex, VarMarker, DerefMarker, TypedDerefMarker) is a subclass
  with a unique `_k` tag
- Add FxtrID + frozenset serialization strategies to PathElement.Config
- `path: tuple[PathElement, ...]` works natively with mashumaro
- mashumaro handles all serialization/deserialization automatically via the discriminator
- Clean, idiomatic, no custom wrappers needed

### Chosen CBOR representation (approach E)

```
PathKey("turns")        → {"_k": "k", "key": "turns"}
PathIndex(3)            → {"_k": "i", "index": 3}
VarMarker()             → {"_k": "v"}
DerefMarker()           → {"_k": "d"}
TypedDerefMarker(...)   → {"_k": "td", "supported_types": [RawWeakFxtrID, ...], "strict": bool}
```

FxtrIDs in TypedDerefMarker.supported_types are handled by the FxtrID and frozenset
serialization strategies on PathElement.Config.

### var_types and value_type

`var_types: tuple[IndexerColumnType, ...]` — IndexerColumnType is an enum with string values.
mashumaro handles enums natively (serializes to the `.value` string). Should work fine.

`value_type: IndexerColumnType` — same, works fine.

### supported_types: frozenset[FxtrID]

This is a top-level field, not inside path. The existing FrozensetSerializationStrategy +
FxtrIDSerializationStrategy in DataclassEntity.Config should handle this automatically.

## Path expression grammar

For `parse()` (unnamed variables use `*`):
```
expression    := segment ("." segment)* (":" col_type)?
segment       := name bracket_part? deref_part?
name          := [a-zA-Z_][a-zA-Z0-9_]*
bracket_part  := "[" bracket_inner "]"
bracket_inner := "*" ":" col_type        (VarMarker)
               | integer                 (fixed index)
deref_part    := "!"                     (DerefMarker)
               | "!" "(as" ws fxtr_list ")" (TypedDerefMarker)
fxtr_list     := quoted_id (ws "|" ws quoted_id)*
quoted_id     := "'" id_string "'"
col_type      := "int" | "str" | "bool" | "bytes" | "float" | "FxtrID"
```

For `query()` (named variables use `$name`):
```
bracket_inner := "$" varname ":" col_type   (VarMarker + named)
               | integer                    (fixed index)
```

Key differences:
- `parse()` uses `*` for variables, returns indexer only
- `query()` uses `$name` for variables, returns IndexerConstraint (calls parse + bind)
- TypedPathExistsIndexer: no final `: col_type` (path only)
- TypedPathValueIndexer: final `: col_type` required

## Unpack logic

### Core algorithm: list-monad-style path walking

```
walk(data, path, var_bindings=[]) -> list[(var_bindings, final_value)]
```

At each path element:
- **str key**: `data = data[key]` (dict lookup). If key not found → skip (lax) or error (strict)
- **int index**: `data = data[index]` (list index). If out of bounds → skip/error
- **VarMarker**: iterate over all keys/indices of current container, bind each to the variable
  - If data is dict: iterate over keys, bind key
  - If data is list: iterate over indices, bind index
  - For each binding: recurse with remaining path
- **DerefMarker**: `data` should be a FxtrID (RawWeakFxtrID/RawStrongFxtrID in CBOR). Load the
  referenced entity from scope. Continue walking into the loaded CBOR data.
- **TypedDerefMarker**: Like DerefMarker but first check `$fxtr.type` of the loaded entity
  against supported_types. If type mismatch → skip (lax) or error (strict).

At end of path:
- TypedPathValueIndexer: extract primitive value, check type matches value_type
- TypedPathExistsIndexer: return True

### Root entity type check

Before walking the path, check the entity's `$fxtr.type` against the indexer's
`supported_types`. If not matched → empty list (lax) or ValueError (strict).

### Async considerations

DerefMarker/TypedDerefMarker require loading entities from the scope, which is async.
The walk function must be async. For VarMarker, we fan out over all possible bindings.
We can process these sequentially (no need for parallel loading during indexing since we're
already in a controlled context).

### FxtrCbor access patterns

FxtrCbor is the decoded CBOR dict. FxtrID references appear as RawWeakFxtrID or
RawStrongFxtrID wrapper objects. Need to unwrap these to get FxtrID for scope.load().
After loading, we get back another FxtrCbor dict.

To extract the FxtrID from a raw ref: `ref.inner_id` (already a FxtrID)
