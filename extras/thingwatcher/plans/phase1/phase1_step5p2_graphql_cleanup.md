# Step 5.2: GraphQL API Cleanup

## Goal

Simplify the GraphQL API by:
1. Deduplicating near-identical types
2. Moving queries into their parent objects (e.g., `tick.things` instead of `thingsAtTick`)
3. Making links and trigger records reference full Things (enabling graph traversal)
4. Making the API more natural for agents to explore hierarchically

## Current Pain Points

From agent testing:
- **No "current tick" shortcut**: Must query `latestTick` first, then `thingsAtTick(tick: N)`
- **Triggerable things lack context**: `triggerableThings` returns shortId/thingType but not data or links
- **Multiple queries to understand state**: Need separate calls for things, their data, and their links
- **Links are opaque**: `Link.toThingKey` is just a string - can't traverse without another query
- **Redundant types**: Thing, ThingData, TriggerableThing are ~90% identical

## Types Being Merged

### Current: Three Near-Identical Types

```graphql
# 1. Thing - used in thingsAtTick, changedThings
type Thing {
  shortId: Int!
  key: String!
  thingType: String!
  triggerable: Boolean!
  links: [Link!]!
  # NO data fields
}

# 2. ThingData - used in thingData, thingHistory
type ThingData {
  shortId: Int!
  tick: Int!              # extra field
  key: String!
  thingType: String!
  triggerable: Boolean!
  links: [Link!]!
  dataChunks: [DataChunk!]!  # extra field
}

# 3. TriggerableThing - used in triggerableThings
type TriggerableThing {
  shortId: Int!
  key: String!
  thingType: String!
  # NO links, NO triggerable (implicitly true), NO data
}
```

### After: Single Unified Thing Type

```graphql
type Thing {
  shortId: Int!
  key: String!
  thingType: String!
  triggerable: Boolean!
  links: [Link!]!

  # Data fields - nullable, lazily loaded only if requested
  dataChunks: [DataChunk!]
  data: JSON                 # Convenience: first chunk's data
}
```

All three use cases now use the same type - callers select which fields they need.

## Other Type Improvements

### Link: Reference Thing Directly

**Before:**
```graphql
type Link {
  linkType: String!
  linkKey: String!
  toThingKey: String!  # Opaque - need another query to get the thing
}
```

**After:**
```graphql
type Link {
  linkType: String!
  linkKey: String!
  toThing: Thing!      # Direct reference - can traverse graph in one query
}
```

This enables queries like:
```graphql
{ latestTick {
  things(triggerable: true) {
    shortId
    data
    links { linkType toThing { shortId thingType data } }
  }
} }
```

### TriggerRecord: Include Thing Directly

**Before:**
```graphql
type TriggerRecord {
  id: Int!
  thingKey: String!       # Opaque key
  thingstateId: Int
  triggerTick: Int!
  payload: JSON
}
```

**After:**
```graphql
type TriggerRecord {
  id: Int!
  thing: Thing!           # Full thing at trigger time
  triggerTick: Int!
  payload: JSON
}
```

### Tick: Contains Things Directly

**Before:**
```graphql
type Tick {
  tick: Int!
  timestamp: Float!
  level: String!
  type: String!
  data: JSON
}
# Things fetched separately via thingsAtTick(tick: N)
```

**After:**
```graphql
type Tick {
  index: Int!             # Renamed from 'tick' for clarity
  timestamp: Float!
  level: String!
  type: String!
  desc: JSON              # Renamed from 'data' to avoid confusion

  things(triggerable: Boolean): [Thing!]!  # Nested, filterable
  changes(since: Int!): [ThingChange!]!    # Delta from previous tick
}
```

## Removed Types and Queries

| Removed | Replaced By |
|---------|-------------|
| `TriggerableThing` type | `Thing` (unified) |
| `ThingData` type | `Thing` (unified) |
| `thingsAtTick(tick)` query | `tick(index).things` |
| `triggerableThings` query | `latestTick.things(triggerable: true)` |
| `thingData(tick, key/shortId)` query | `thing(shortId/key, atTick)` |
| `changedThings(from, to)` query | `tick(index).changes(since)` |

## Example Queries (Before → After)

### Get triggerable things with their data

**Before (3 queries):**
```graphql
{ latestTick }
{ triggerableThings { shortId thingType } }
{ thingData(tick: 5, shortId: 1) { dataChunks { data } } }
```

**After (1 query):**
```graphql
{ latestTick { things(triggerable: true) { shortId thingType data } } }
```

### Traverse links to see related things

**Before (N+1 queries):**
```graphql
{ thingsAtTick(tick: 5) { shortId links { toThingKey } } }
# Then for each toThingKey:
{ thingData(tick: 5, key: "...") { thingType data } }
```

**After (1 query):**
```graphql
{ tick(index: 5) {
  things {
    shortId
    links { linkType toThing { shortId thingType data } }
  }
} }
```

### View trigger history with thing context

**Before:**
```graphql
{ triggerRecords { thingKey triggerTick payload } }
# Then lookup each thing separately
```

**After:**
```graphql
{ triggerRecords { thing { shortId thingType data } triggerTick payload } }
```

## Implementation Plan

### Files to Modify

1. **`thingwatcher/schema.graphql`** - New schema definition
2. **`thingwatcher/server.py`** - Update resolvers
3. **`thingwatcher/db.py`** - May need minor adjustments for new query patterns
4. **`tests/test_graphql.py`** - Update all GraphQL test queries
5. **`docs/querying_thingwatcher_as_an_agent.md`** - Update documentation
6. **`examples/demo_pause_trigger.py`** - Update example queries in help text

### Implementation Steps

- [x] 1. Update `schema.graphql` with new types and queries
- [x] 2. Add Tick object type resolver in `server.py`
- [x] 3. Add `things` field resolver on Tick (with `triggerable` filter)
- [x] 4. Add `changes` field resolver on Tick
- [x] 5. Update `Thing` type to include optional `dataChunks` and `data` fields
- [x] 6. Add lazy data loading - only fetch dataChunks if requested in query
- [x] 7. Update `Link` type to resolve `toThing` as full Thing
- [x] 8. Update `TriggerRecord` to resolve `thing` as full Thing
- [x] 9. Add `tick(index)` query resolver
- [x] 10. Update `latestTick` to return Tick object instead of Int
- [x] 11. Add `thing` query resolver (replaces `thingData`)
- [x] 12. Remove deprecated resolvers and types
- [x] 13. Update tests
- [x] 14. Update documentation
- [x] 15. Update demo example
- [x] 16. Update CODE_OVERVIEW.md

### Backwards Compatibility

This is a breaking change. Since thingwatcher is still in development (pre-1.0), we'll do a clean break rather than maintaining deprecated aliases.

### Testing Strategy

- Update existing test cases to use new query patterns
- Add tests for:
  - `latestTick.things` with and without `triggerable` filter
  - `tick(index).things`
  - `tick(index).changes(since:)`
  - Lazy data loading (verify dataChunks not fetched when not requested)
  - `thing(shortId:)` lookup
  - Link traversal via `toThing`
  - TriggerRecord with nested `thing`

## Design Decisions

1. **`data` convenience field**: Yes - `data: JSON` returns the first chunk's data. Keep `dataChunks` for when you need the full chain or custom merging.

2. **Cycle handling in link traversal**: No special handling needed - users control depth via their GraphQL query.

3. **Thing context in TriggerRecord**: `thing` resolves at `triggerTick` time (historical state) - shows what was actually triggered.

---

## Full New Schema

```graphql
# Custom scalar for arbitrary JSON objects (decoded CBOR data)
scalar JSON

# =============================================================================
# Query Root
# =============================================================================

type Query {
  # Primary entry points
  latestTick: Tick!
  tick(index: Int!): Tick

  # Timeline listing
  ticks(from: Int, to: Int, levels: [String!]): [Tick!]!

  # Direct thing lookup (for bookmarks, deep links)
  thing(shortId: Int, key: String, atTick: Int): Thing
  thingHistory(shortId: Int, key: String, from: Int, to: Int): [Thing!]!

  # Single data chunk by ID (for caching/deduplication)
  dataChunk(id: Int!): DataChunk

  # Config
  renderingConfig: RenderingConfig!

  # Trigger history
  triggerRecords(key: String, from: Int, to: Int): [TriggerRecord!]!
  triggerRecord(id: Int!): TriggerRecord
}

# =============================================================================
# Mutations
# =============================================================================

type Mutation {
  triggerTick(level: String = "info", desc: String = ""): Tick!
  triggerPause(shortId: Int, key: String, payload: JSON): TriggerResult!
}

# =============================================================================
# Subscriptions
# =============================================================================

type Subscription {
  newTick: Tick!
}

# =============================================================================
# Core Types
# =============================================================================

type Tick {
  index: Int!
  timestamp: Float!
  level: String!
  type: String!
  desc: JSON              # Tick description/message (renamed from 'data')

  # Nested things at this tick
  things(triggerable: Boolean): [Thing!]!

  # Changes since a previous tick
  changes(since: Int!): [ThingChange!]!
}

type Thing {
  shortId: Int!
  key: String!
  thingType: String!
  triggerable: Boolean!
  links: [Link!]!

  # Data - nullable, lazily loaded only when requested
  dataChunks: [DataChunk!]

  # Convenience: first chunk's data (use dataChunks for full chain)
  data: JSON
}

type Link {
  linkType: String!
  linkKey: String!
  toThing: Thing!         # Direct reference to target thing
}

type DataChunk {
  id: Int!                # thingstate_id - stable identifier for caching
  data: JSON
}

# =============================================================================
# Change Tracking
# =============================================================================

enum ChangeKind {
  ADDED
  MODIFIED
  REMOVED
}

type ThingChange {
  kind: ChangeKind!
  thing: Thing!           # For REMOVED: last known state before removal
}

# =============================================================================
# Trigger Types
# =============================================================================

type TriggerResult {
  ok: Boolean!
  triggerId: Int          # ID of the trigger record created
  error: String           # Error message if ok=false
}

type TriggerRecord {
  id: Int!
  thing: Thing!           # The thing that was triggered (state at trigger time)
  triggerTick: Int!
  payload: JSON           # Payload sent with the trigger
}

# =============================================================================
# Config Types
# =============================================================================

type RenderingConfig {
  thingTypes: [ThingTypeConfig!]!
}

type ThingTypeConfig {
  thingType: String!
  color: String!
  linkColors: [LinkColorConfig!]!
}

type LinkColorConfig {
  linkType: String!
  color: String!
}
```
