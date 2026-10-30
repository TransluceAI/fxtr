# keyJSON Planning Scratch

## Current State

- Keys are CBOR-encoded bytes stored in DB
- Over GraphQL, keys are exposed as base64 strings via `encode_key()`
- `shortId` provides a convenient integer alias
- Frontend shows truncated base64 in DetailsPanel (line 95-97)
- Links have `linkKey` which is also base64-encoded

## What We Need

1. **GraphQL output fields:**
   - `Thing.keyJSON: JSON` - nullable, returns decoded key if JSON-compatible
   - `Link.linkKeyJSON: JSON` - same for links

2. **GraphQL input parameters:**
   - Add `keyJSON: JSON` parameter to queries/mutations that accept `key`
   - These are: `thing`, `thingHistory`, `triggerPause`, `triggerRecords`
   - Input validation: must provide exactly one of `key`, `shortId`, or `keyJSON`

3. **Frontend changes:**
   - DetailsPanel: Replace base64 display with keyJSON display
   - If keyJSON is null, fall back to showing base64
   - Links section: show linkKeyJSON if available

4. **Agent docs update:**
   - Document keyJSON in querying guide
   - Show examples of querying with keyJSON

## Error Handling Decision

For output fields (`keyJSON`, `linkKeyJSON`):
- Return `null` when CBOR key is not JSON-compatible
- Don't throw GraphQLError (that would break entire queries)
- The base64 `key` field always works as fallback

For input parameter (`keyJSON`):
- If provided, serialize to CBOR and use as key_bytes
- If CBOR serialization fails, throw GraphQLError (user error)

## Implementation Details

### Converting keyJSON input to key_bytes

```python
def encode_key_from_json(key_json: Any) -> bytes:
    """Convert JSON value to CBOR key bytes for lookup."""
    return cbor2.dumps(key_json, canonical=True)
```

This is the inverse of what we do for output:
- Output: CBOR bytes -> cbor2.loads() -> validate JSON-compatible -> return
- Input: JSON value -> cbor2.dumps(canonical=True) -> key_bytes for lookup

### Decoding key_bytes to keyJSON

Already have `_is_json_compatible()` and `decode_cbor_data()` helpers.

```python
def decode_key_to_json(key_bytes: bytes) -> Any | None:
    """Decode CBOR key bytes to JSON if compatible, else None."""
    try:
        raw = cbor2.loads(key_bytes)
        if _is_json_compatible(raw):
            return raw
        return None
    except Exception:
        return None
```

### GraphQL Schema Changes

```graphql
type Thing {
  shortId: Int!
  key: String!
  keyJSON: JSON              # NEW: nullable, returns decoded key if JSON-compatible
  thingType: String!
  # ... rest unchanged
}

type Link {
  linkType: String!
  linkKey: String!
  linkKeyJSON: JSON          # NEW: nullable
  toThing: Thing!
}

# Update queries/mutations to accept keyJSON
thing(shortId: Int, key: String, keyJSON: JSON, atTick: Int): Thing
thingHistory(shortId: Int, key: String, keyJSON: JSON, from: Int, to: Int): [Thing!]!
triggerPause(shortId: Int, key: String, keyJSON: JSON, payload: JSON): TriggerResult!
triggerRecords(key: String, keyJSON: JSON, from: Int, to: Int): [TriggerRecord!]!
```

### Server Changes

1. Add helper functions for keyJSON encoding/decoding
2. Add field resolvers for `Thing.keyJSON` and `Link.linkKeyJSON`
3. Update query/mutation resolvers to accept `keyJSON` parameter
4. Update validation: "exactly one of key, shortId, or keyJSON"

### Frontend Changes

1. Update GraphQL queries to fetch `keyJSON` and `linkKeyJSON`
2. Update DetailsPanel to display keyJSON (with JSON formatting)
3. Fall back to base64 key if keyJSON is null
4. Consider: show keyJSON in Graph tooltips too?

## Questions Resolved

- UI display: Replace base64 with keyJSON (fall back if null)
- Link keys: Yes, add linkKeyJSON
- Error handling: Return null for non-JSON-compatible (consistent with nullable GraphQL fields)
- Query inputs: All key-accepting queries get keyJSON parameter
