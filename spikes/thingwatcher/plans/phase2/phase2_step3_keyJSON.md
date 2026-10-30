# Phase 2 Step 3: keyJSON Support

## Overview

Add JSON representation of CBOR keys to thingwatcher. Keys are currently exposed only as base64-encoded CBOR bytes, which is opaque to humans. This feature adds `keyJSON` fields to make keys human-readable when they contain JSON-compatible data.

Key changes:
- **Output fields**: `Thing.keyJSON` and `Link.linkKeyJSON` return the decoded key as JSON (nullable)
- **Input parameters**: Queries/mutations accept `keyJSON` as an alternative to `key` or `shortId`
- **Frontend**: Display keyJSON instead of base64 in the UI
- **Agent docs**: Document keyJSON for CLI usage

## Design Decisions

### 1. Nullable Output Fields

`keyJSON` and `linkKeyJSON` are nullable:
- Returns the decoded JSON value if the CBOR key is JSON-compatible
- Returns `null` if the key contains non-JSON types (bytes, datetime, etc.)
- Does NOT throw GraphQLError (that would break entire queries)
- Users can always fall back to `key` (base64) or `shortId`

This is different from `Thing.data` which throws GraphQLError for non-JSON CBOR, because:
- `data` is explicitly requested content that the user expects to read
- `keyJSON` is a convenience view; the authoritative key is always available via `key` or `shortId`

### 2. Input Parameter Validation

Queries/mutations that accept keys now accept three mutually exclusive parameters:
- `shortId: Int` - integer alias (simplest for CLI)
- `key: String` - base64-encoded CBOR (unambiguous)
- `keyJSON: JSON` - JSON value to encode as CBOR key

Validation rule: provide exactly one of the three. Error message updated accordingly.

When `keyJSON` is provided as input:
- **Reject null**: If `keyJSON` is explicitly `null`, throw GraphQLError. This prevents user errors where they accidentally fetch `null` (from a non-JSON-compatible key) and try to send it back as a lookup.
- Serialize to CBOR with `canonical=True` to get key_bytes
- This matches how keys are stored, so lookups work correctly
- If serialization fails, throw GraphQLError (user error)

### 3. Frontend Display Strategy

Keys are expected to be small, so always display them as inline strings.

DetailsPanel key display:
- Show `keyJSON` as inline `JSON.stringify()` string if available
- Fall back to truncated base64 `key` if `keyJSON` is null
- Keep the tooltip with full key for copy/reference
- No tree view needed for keys (unlike data, which can be large)

Links display:
- Show `linkKeyJSON` as inline string if available, otherwise `linkKey`

## Implementation Steps

### Step 3.1: Add helper functions in server.py

- [x] Add `decode_key_to_json(key_bytes: bytes) -> Any | None` helper
  - Uses `cbor2.loads()` and `_is_json_compatible()` check
  - Returns decoded value or `None` if not JSON-compatible
- [x] Add `encode_key_from_json(key_json: Any) -> bytes` helper
  - Uses `cbor2.dumps(key_json, canonical=True)`
  - For converting input keyJSON to key_bytes

### Step 3.2: Update GraphQL schema

In `schema.graphql`:

- [x] Add `keyJSON: JSON` field to `Thing` type (nullable)
- [x] Add `linkKeyJSON: JSON` field to `Link` type (nullable)
- [x] Add `keyJSON: JSON` parameter to `thing` query
- [x] Add `keyJSON: JSON` parameter to `thingHistory` query
- [x] Add `keyJSON: JSON` parameter to `triggerPause` mutation
- [x] Add `keyJSON: JSON` parameter to `triggerRecords` query

### Step 3.3: Add field resolvers for output

In `server.py`:

- [x] Add `Thing.keyJSON` resolver that decodes `_key_bytes` to JSON
- [x] Update link resolver to include `_link_key_bytes` in returned dict
- [x] Add resolver for `Link.linkKeyJSON` using `_link_key_bytes`

### Step 3.4: Update query/mutation resolvers for input

- [x] Update `resolve_thing()`: accept `keyJSON`, validate exactly-one-of-three
- [x] Update `resolve_thing_history()`: same pattern
- [x] Update `resolve_trigger_pause()`: same pattern
- [x] Update `resolve_trigger_records()`: same pattern
- [x] Add helper `resolve_key_bytes_from_params(...)` to centralize logic

### Step 3.5: Backend tests

Tests added to `tests/test_graphql.py` in `TestKeyJson` class (10 tests total, all passing):

- [x] Test `Thing.keyJSON` returns correct JSON for JSON-compatible key (`test_key_json_output`)
- [x] Test `Thing.keyJSON` returns null for non-JSON key (bytes) (`test_key_json_null_for_bytes_key`)
- [x] Test `Link.linkKeyJSON` similarly (`test_link_key_json`)
- [x] Test `thing(keyJSON: ...)` lookup works (`test_thing_query_by_key_json`)
- [x] Test `thingHistory(keyJSON: ...)` lookup works (`test_thing_history_by_key_json`)
- [x] Test `triggerPause(keyJSON: ...)` works (`test_trigger_pause_by_key_json`)
- [x] Test `triggerRecords(keyJSON: ...)` filter works (`test_trigger_records_by_key_json`)
- [x] Test error when providing multiple key params (`test_error_multiple_key_params`)
- [x] Test error when providing zero key params (`test_error_no_key_params`)
- [x] Test error when providing `keyJSON: null` (`test_key_json_null_rejection`)

### Step 3.6: Update frontend GraphQL queries

In `graphql/queries.ts`:

- [x] Add `keyJSON` to Thing fields in all queries that fetch things
- [x] Add `linkKeyJSON` to Link fields where links are fetched
- [x] Update TypeScript types in `types/index.ts`

### Step 3.7: Update DetailsPanel display

In `components/DetailsPanel/DetailsPanel.tsx`:

- [x] Update key display section to show keyJSON if available
- [x] Use `JSON.stringify()` for inline display (no tree view for keys)
- [x] Fall back to base64 key if keyJSON is null
- [x] Keep full key in tooltip
- [x] Update links section to show linkKeyJSON where available

### Step 3.8: Manual verification

Before updating documentation, verify the feature works end-to-end:

- [x] Run the demo server or a test server with some tracked things
- [x] Query via graphqurl to verify `keyJSON` appears in responses
- [x] Test querying with `keyJSON` input parameter works
- [x] Verify frontend displays keyJSON correctly
- [x] Test that `keyJSON: null` input is rejected with clear error

### Step 3.9: Update agent documentation

In `docs/querying_thingwatcher_as_an_agent.md`:

- [x] Document keyJSON output field
- [x] Add examples of querying with keyJSON input parameter
- [x] Update "Keys" section to explain relationship between shortId, key, keyJSON
- [x] Add examples showing keyJSON in query results

### Step 3.10: Update CODE_OVERVIEW.md

- [x] Document keyJSON fields in GraphQL schema section
- [x] Update frontend section to note keyJSON display

## GraphQL Schema Changes

```graphql
type Thing {
  shortId: Int!
  key: String!
  keyJSON: JSON              # NEW: decoded key as JSON, null if not JSON-compatible
  thingType: String!
  triggerable: Boolean!
  links: [Link!]!
  dataChunks: [DataChunk!]
  data: JSON
}

type Link {
  linkType: String!
  linkKey: String!
  linkKeyJSON: JSON          # NEW: decoded linkKey as JSON
  toThing: Thing!
}

type Query {
  # Updated to accept keyJSON
  thing(shortId: Int, key: String, keyJSON: JSON, atTick: Int): Thing
  thingHistory(shortId: Int, key: String, keyJSON: JSON, from: Int, to: Int): [Thing!]!
  # ... other queries unchanged except triggerRecords
  triggerRecords(key: String, keyJSON: JSON, from: Int, to: Int): [TriggerRecord!]!
}

type Mutation {
  triggerTick(level: String = "info", desc: String = ""): Tick!
  # Updated to accept keyJSON
  triggerPause(shortId: Int, key: String, keyJSON: JSON, payload: JSON): TriggerResult!
}
```

## Example Usage

### Querying with keyJSON output

```bash
# Get thing with keyJSON in response
graphqurl http://localhost:8765/graphql -q '{
  thing(shortId: 1) {
    shortId
    key
    keyJSON
    thingType
    data
  }
}'

# Response might include:
# { "thing": { "shortId": 1, "key": "oWJpZAE=", "keyJSON": {"id": 1}, ... } }
```

### Querying with keyJSON input

```bash
# Look up thing by its JSON key
graphqurl http://localhost:8765/graphql -q '{
  thing(keyJSON: {"id": 1}) {
    shortId
    thingType
    data
  }
}'

# Trigger by keyJSON
graphqurl http://localhost:8765/graphql \
  -q 'mutation { triggerPause(keyJSON: {"task": "main"}) { ok } }'
```

## Edge Cases

1. **Non-JSON CBOR key**: `keyJSON` returns `null`, frontend falls back to base64 `key`

2. **Complex JSON key**: e.g., `{"nested": {"deep": [1,2,3]}}` - displayed as inline JSON string via `JSON.stringify()`

3. **Simple JSON key**: e.g., `"mykey"` or `42` - displayed as inline JSON string

4. **keyJSON input with special types**: If user passes JSON that cbor2 encodes differently than expected (unlikely but possible), lookup may fail to find the thing. User should use `key` (base64) for guaranteed match.

5. **Empty key**: Empty object `{}` or empty string `""` are valid JSON keys

6. **Null key as output**: CBOR can encode `null` as a key value. In this case, `keyJSON` would return `null` (indistinguishable from "not JSON-compatible"). This is an accepted edge case for output.

7. **Null keyJSON as input**: Explicitly rejected with GraphQLError. This prevents user errors where they fetch `null` from a non-JSON-compatible key and accidentally try to use it for lookup.

## Implementation Notes & Future Considerations

### Pragmatic shortcuts taken

1. **Tests added to existing file**: Initially tried to create a separate `test_key_json.py` file but ran into infrastructure issues (server not starting, fixture conflicts with `watching_and_serving()` context manager). Added tests to `test_graphql.py` instead, which works fine but means keyJSON tests aren't isolated.

2. **triggerRecords has different validation**: Unlike `thing` and `thingHistory` which require exactly one of `shortId`/`key`/`keyJSON`, `triggerRecords` allows zero or one (since key filtering is optional). The validation is "at most one of key/keyJSON" rather than "exactly one".

### Things worth revisiting

1. **No test for non-JSON link key**: There's a test for bytes as a thing key returning `keyJSON: null`, but no equivalent test for `linkKeyJSON` with a bytes link key. Low priority since the code path is the same.

2. **Null key ambiguity**: If CBOR key is literally `null`, `keyJSON` returns `null` which is indistinguishable from "key is not JSON-compatible". Documented in edge cases but could confuse users. Could add a separate `keyJSONError` field if this becomes a problem.

3. **GraphQL inline JSON limitation**: GraphQL doesn't support inline JSON object literals in queries - you must use variables. This is a GraphQL limitation, not ours, but it means the example usage in docs shows `keyJSON: {"id": 1}` which only works with graphqurl's special handling, not in raw GraphQL queries.
