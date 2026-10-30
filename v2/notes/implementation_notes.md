# Implementation Notes

Detailed notes on non-obvious implementation details discovered while reviewing and testing the fxtr2 data layer.

## The `_provenance_parent` Pattern

### How it works

Every `FxtrID` has two components:
- `_raw_data_id`: The actual node identity (tag byte + payload)
- `_provenance_parent`: Optional reference to a provenance-level ID

The key insight is that **equality and hashing only use `_raw_data_id`**:

```python
def __eq__(self, other):
    return type(other) is type(self) and self._raw_data_id == other._raw_data_id

def __hash__(self):
    return hash((type(self), self._raw_data_id))
```

This means:
```python
parent1 = FxtrID.builtin("context_a")
parent2 = FxtrID.builtin("context_b")

id1 = FxtrID(raw_data_id=some_bytes, provenance_parent=parent1)
id2 = FxtrID(raw_data_id=some_bytes, provenance_parent=parent2)

id1 == id2  # True! Same data, different provenance
hash(id1) == hash(id2)  # True!
{id1: "value"}[id2]  # Works! Returns "value"
```

### Why this matters

This design enables "transparent" provenance tracking. Application code that only cares about data can compare IDs normally, while the provenance system can attach metadata without affecting application behavior.

However, this can be surprising when debugging. If you print two IDs and they look different (because `repr` shows the provenance), they might still be "equal" in Python's sense.

### When provenance is used

The `_provenance_parent` is used when:
1. Storing entities - the arena may use the provenance ID instead of the data ID
2. Loading entities - child IDs may inherit provenance from their parent
3. The (not yet implemented) ProvenanceArena inspects provenance for automatic tracking

## Canonical CBOR Order and P-Nodes

### The problem p-nodes solve

P-nodes allow attaching provenance to an entire subgraph without deeply traversing it. Instead of wrapping every nested ID, you create a single p-node that says "this is the data at path [0, 2, 1] from root X".

### How path indices work

The path is a list of integers. Each integer is the index of the child ID to follow, in **CBOR canonical serialization order**:
1. For dicts: keys are sorted, then values are visited in key order
2. For lists: items are visited in order
3. The `$fxtr.ctx` marker counts as an ID

Example:
```python
entity = {
    "$fxtr.ctx": marker,  # Index 0 (sorted first because "$" < other chars)
    "refs": [ref_a, ref_b],  # Indices 1, 2
    "type": type_ref,  # Index 3
}
```

### Why this matters for code

If you need to transform IDs in an entity that might be used with p-nodes, you **must** use `visit_and_project_ids_in_canonical_cbor_order`, not the regular `visit_and_project_ids`. The regular version visits in Python dict iteration order, which may differ from CBOR canonical order.

The canonical order version:
1. Serializes to CBOR (which sorts dict keys)
2. Deserializes while tracking/transforming IDs
3. Returns the transformed structure

This is slower but necessary for correctness with p-nodes.

## The Varint32 Encoding

### Why custom encoding?

P-node paths need to be encoded in the ID string representation. The encoding should be:
- Compact for small numbers (most paths are short)
- Human-readable-ish for debugging
- Unambiguous (no separators needed between numbers)

### How it works

The encoding uses a 32-character alphabet (`0-9a-v`) where:
- Characters 0-15 (`0-9a-f`) are terminal (end of number)
- Characters 16-31 (`g-v`) are continuation (more nibbles follow)

Numbers are encoded big-endian, 4 bits at a time:
- `0` = 0
- `f` = 15
- `g0` = 16 (continuation `g` = 0 with more coming, terminal `0` = 0, so 0*16 + 0 = wait no...)

Actually looking at the code more carefully:
```python
# Continuation nibbles are offset by 16
if i < len(nibbles) - 1:
    result.append(_VARINT32_ALPHABET[nibble + 16])  # Continuation
else:
    result.append(_VARINT32_ALPHABET[nibble])  # Terminal
```

So `16` encodes as: nibbles = [1, 0], encoded as `alphabet[1+16]` + `alphabet[0]` = `r0`.

### Edge cases

- Zero encodes as just `"0"`
- Negative numbers raise `ValueError`
- Very large numbers work but produce long strings

## Node Type Taxonomy

### Two orthogonal properties

1. **Ground vs Provenance**: Can the node be unwrapped?
   - Ground (`.`, `!`, `d`): No unwrapping possible
   - Provenance (`n`, `c`, `p`, `u`): Can unwrap to data node

2. **Contentful vs Contentless**: Does the node have stored CBOR data?
   - Contentful (`d`, `n`): Has CBOR bytes in storage
   - Contentless (`.`, `!`, `c`): No stored data, derived from structure
   - Depends (`p`, `u`): Contentful if wrapped node is contentful

### Storage implications

| Node | Stored Data | Dependencies |
|------|-------------|--------------|
| `.` | None | None |
| `!` | None | None |
| `d` | CBOR bytes | STRONG_REF, WEAK_REF to children |
| `n` | CBOR bytes | STRONG_REF, WEAK_REF to children, UNWRAPS_TO |
| `c` | None | CTX_NODE to d-node, UNWRAPS_TO |
| `p` | None | CTX_NODE to pathroot, BACKSTEP (if not root), UNWRAPS_TO |
| `u` | None | UNWRAPS_TO |

### Loading behavior

- `d`/`n`: Deserialize stored CBOR
- `c`: Error (contentless)
- `p`: Load unwrapped node, wrap each child as new p-node with extended path
- `u`: Load unwrapped node, wrap each child as u-node

## System Entities and the Arena API

### The "value" field requirement

Both `create_c_node` and `create_p_node` expect their input data to have a `value` field:
- For c-nodes: `value` is what the c-node unwraps to (usually `.`)
- For p-nodes: `value` is the root node of the subgraph

The `ContextEntity` and `PathRootEntity` classes are convenience wrappers that include this field.

### Why this caught me off guard

Initially, `ContextEntity` only had `ctx_key` and `metadata`. The `value` field was implied but not present. This would have caused runtime errors when trying to use `entity.to_fxtr_cbor()` with `create_c_node()`.

The fix was to add `value: FxtrID = field(default_factory=_default_ground_node)` to `ContextEntity`.

## Strong vs Weak References

### Semantic difference

- **Strong** (tag 33002): Ownership/containment. "This entity contains that entity."
- **Weak** (tag 33001): Reference. "This entity refers to that entity."

### Storage behavior

Both types keep the referenced entity alive. The distinction is for:
1. Visualization (drawing ownership trees)
2. Understanding data flow (strong links form a tree, weak links can be arbitrary)

### The "orphan" concept

An entity with no strong links pointing to it is considered "orphaned" - we don't know where it "belongs" in the data model. It might still be reachable via weak links, but it's not owned by anything.

## Reference Counting and GC

### Why reference counting works

Content-addressed immutable data can't form cycles:
- An entity's ID is the hash of its content
- Content includes the IDs it references
- Therefore, an entity can only reference entities that existed before it

This makes reference counting safe - no cycle detection needed.

### What counts as a reference

From the arena's perspective:
- Strong and weak links both count
- The `$fxtr.ctx` marker counts
- Provenance relationships (unwraps_to, ctx_node, backstep) count

### When cleanup happens

In MemoryArena:
- Nodes are ref-counted per arena
- Abandoning a node decrements its count
- Zero-count nodes are cleaned up (if not referenced by store internals)

In SQLite:
- Nodes are tracked per namespace and arena
- Orphaned nodes (no pins, no arena membership, no backrefs) can be garbage collected
- GC runs incrementally via background tasks
