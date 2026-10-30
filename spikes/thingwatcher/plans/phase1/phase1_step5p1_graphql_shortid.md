# Step 5.1: GraphQL Short IDs

Add database-persisted short integer IDs as an ergonomic alternative to base64-encoded CBOR keys.

## Motivation

Base64 keys like `g2l3YWl0cG9pbnRlYWxpY2VwYWZ0ZXJfZmV0Y2hfZGF0YQ==` are:
- Long and error-prone to type
- Impossible to construct (must discover and copy/paste)
- Opaque (can't tell what they refer to)

Short IDs like `1`, `2`, `3` would make CLI interaction much more pleasant:
```bash
# Before
graphqurl ... -q 'mutation { triggerPause(key: "g2l3YWl0cG9pbnRlYWxpY2VwYWZ0ZXJfZmV0Y2hfZGF0YQ==") { ok } }'

# After
graphqurl ... -q 'mutation { triggerPause(shortId: 1) { ok } }'
```

## Design

### Database-scoped IDs

- IDs are stored in a DB table, scoped to the lifetime of the trace
- Same key always maps to same shortId (even across watcher restarts with same DB)
- IDs start at 1 (autoincrement primary key)
- Indexed in both directions for fast lookups

### Database Schema

```sql
CREATE TABLE short_ids (
    id INTEGER PRIMARY KEY AUTOINCREMENT,  -- The short ID (1, 2, 3, ...)
    thing_key BLOB NOT NULL UNIQUE         -- The CBOR-encoded key
);

CREATE INDEX idx_short_ids_key ON short_ids(thing_key);
```

### Database Methods

```python
# In db.py:

async def get_or_create_short_id(self, conn, key_bytes: bytes) -> int:
    """Get existing shortId or create a new one."""
    # Try to find existing
    result = await conn.execute(
        select(short_ids.c.id).where(short_ids.c.thing_key == key_bytes)
    )
    row = result.fetchone()
    if row:
        return row[0]

    # Insert new
    result = await conn.execute(
        insert(short_ids).values(thing_key=key_bytes)
    )
    return result.lastrowid

async def resolve_short_id(self, conn, short_id: int) -> bytes | None:
    """Look up key_bytes from shortId, or None if not found."""
    result = await conn.execute(
        select(short_ids.c.thing_key).where(short_ids.c.id == short_id)
    )
    row = result.fetchone()
    return row[0] if row else None

async def get_short_id(self, conn, key_bytes: bytes) -> int | None:
    """Look up shortId from key_bytes, or None if not assigned."""
    result = await conn.execute(
        select(short_ids.c.id).where(short_ids.c.thing_key == key_bytes)
    )
    row = result.fetchone()
    return row[0] if row else None
```

### ID Assignment

IDs are assigned during the background writer when persisting thing states:
- When writing a new thing_state, also ensure short_id exists for that key
- This keeps ID assignment transactional with state persistence

### GraphQL Schema Changes

```graphql
type Thing {
  shortId: Int!          # NEW
  key: String!
  thingType: String!
  triggerable: Boolean!
  links: [Link!]!
}

type ThingData {
  shortId: Int!          # NEW
  tick: Int!
  key: String!
  thingType: String!
  triggerable: Boolean!
  links: [Link!]!
  dataChunks: [DataChunk!]!
}

type TriggerableThing {
  shortId: Int!          # NEW
  key: String!
  thingType: String!
}

type ThingChange {
  kind: ChangeKind!
  thing: Thing!          # Already includes shortId via Thing type
}

type Mutation {
  triggerTick(level: String = "info", desc: String = ""): Tick!

  # Accept EITHER key OR shortId (exactly one required)
  triggerPause(key: String, shortId: Int, payload: JSON): TriggerResult!
}

type Query {
  # ... existing queries ...

  # Accept EITHER key OR shortId for thing lookups
  thingData(tick: Int!, key: String, shortId: Int): ThingData
  thingHistory(key: String, shortId: Int, fromTick: Int, toTick: Int): [ThingData!]!
}
```

### Resolver Changes

#### triggerPause mutation

```python
@mutation.field("triggerPause")
async def resolve_trigger_pause(_, info, key: str = None, shortId: int = None, payload=None):
    watcher: Watcher = info.context["watcher"]
    db: Database = info.context["db"]

    # Validate exactly one identifier provided
    if (key is None) == (shortId is None):
        return {"ok": False, "error": "Provide exactly one of 'key' or 'shortId'"}

    # Resolve to key_bytes
    if shortId is not None:
        async with db.begin() as conn:
            key_bytes = await db.resolve_short_id(conn, shortId)
        if key_bytes is None:
            return {"ok": False, "error": f"Unknown shortId: {shortId}"}
    else:
        key_bytes = decode_key(key)

    result = await watcher.trigger_pause(key_bytes, payload)
    return {"ok": result.ok, "triggerId": result.trigger_id, "error": result.error}
```

#### Thing resolvers

The shortId is fetched from DB alongside thing data. Since things are written with their shortId during persistence, resolvers can just include it from the query result:

```python
# In queries that return things, JOIN with short_ids table:
# SELECT t.*, s.id as short_id FROM thing_states t
# JOIN short_ids s ON t.thing_key = s.thing_key
# ...

def resolve_thing(thing_row, info):
    return {
        "shortId": thing_row["short_id"],  # From JOIN
        "key": encode_key(thing_row["thing_key"]),
        "thingType": thing_row["thing_type"],
        # ...
    }
```

#### thingData / thingHistory queries

```python
@query.field("thingData")
async def resolve_thing_data(_, info, tick: int, key: str = None, shortId: int = None):
    db: Database = info.context["db"]

    # Validate exactly one identifier
    if (key is None) == (shortId is None):
        return None  # Or raise error

    # Resolve to key_bytes
    async with db.begin() as conn:
        if shortId is not None:
            key_bytes = await db.resolve_short_id(conn, shortId)
            if key_bytes is None:
                return None
        else:
            key_bytes = decode_key(key)

        # ... rest of existing logic using key_bytes ...
```

## Implementation Order

1. [x] Add `short_ids` table to DB schema (`db.py`)
2. [x] Add DB methods: `get_or_create_short_id()`, `resolve_short_id()`, `get_short_id()`
3. [x] Call `get_or_create_short_id()` in background writer when persisting thing states
4. [x] Update GraphQL schema with `shortId` fields
5. [x] Update Thing/ThingData/TriggerableThing resolvers to include shortId
6. [x] Update `triggerPause` mutation to accept shortId
7. [x] Update `thingData` query to accept shortId
8. [x] Update `thingHistory` query to accept shortId
9. [x] Add tests
10. [x] Update docs

## Edge Cases

### Unknown shortId in mutation
- Return `{"ok": false, "error": "Unknown shortId: N"}`

### Unknown shortId in query
- Return `null` (same as unknown key)

### Thing disappears but shortId was assigned
- ID remains in DB forever (trace lifetime)
- This is correct - historical queries can still use the shortId

### Concurrent requests
- SQLite handles concurrency
- UNIQUE constraint prevents duplicate assignments

## Tests

```python
class TestShortIds:
    async def test_short_id_assigned_to_things(self):
        """Things in thingsAtTick have shortId field."""
        # Query thingsAtTick, verify shortId present and > 0

    async def test_short_id_stable_across_ticks(self):
        """Same thing has same shortId across multiple ticks."""
        # Track object, tick twice, verify shortId unchanged

    async def test_trigger_pause_by_short_id(self):
        """Can trigger pause using shortId instead of key."""
        # Set up pause, get shortId from triggerableThings
        # Trigger using shortId, verify it works

    async def test_thing_data_by_short_id(self):
        """Can query thingData using shortId."""
        # Get shortId from thingsAtTick
        # Query thingData(shortId: N), verify returns data

    async def test_unknown_short_id_returns_error(self):
        """Triggering unknown shortId returns appropriate error."""
        # triggerPause(shortId: 99999) should return ok=false

    async def test_must_provide_key_or_short_id(self):
        """Must provide exactly one of key or shortId."""
        # triggerPause() with neither -> error
        # triggerPause(key: "...", shortId: 1) with both -> error
```

## Files to Modify

- `thingwatcher/db.py` - Add `short_ids` table, add DB methods
- `thingwatcher/watcher.py` - Call `get_or_create_short_id()` in background writer
- `thingwatcher/schema.graphql` - Add shortId fields, update mutation/query signatures
- `thingwatcher/server.py` - Update resolvers to include/accept shortId
- `tests/test_graphql.py` or `tests/test_short_id.py` - Add tests
- `docs/querying_thingwatcher_as_an_agent.md` - Document shortId usage
