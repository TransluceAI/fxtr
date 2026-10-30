# Step 3 API Design Scratch

## Use Cases

### Frontend UI
1. **Timeline navigation**: Scrub through ticks, see graph at each point
2. **Graph visualization**: Things as nodes, links as edges, colored by type
3. **Change highlighting**: New (green), modified (yellow), removed (red dashed)
4. **Detail inspection**: Click thing to see full data
5. **Live mode**: Subscribe to new ticks, update in real-time

### MCP Server / AI Debugging
1. **Overview**: "What things exist?" "What happened recently?"
2. **Inspection**: "Show me thing X" "What's its history?"
3. **Relationships**: "What links to X?" "What does X link to?"
4. **Debugging**: "When did X change?" "What was X at tick N?"
5. **Control**: Trigger ticks, fire debug events

## Data Encoding Decisions

- **Keys**: Base64-encoded CBOR bytes (compact, roundtrips perfectly)
- **Data**: JSON-encoded (human/LLM readable, frontend can parse)
- **Tick data**: JSON-encoded

## Query Categories

### 1. Timeline Queries
- `latestTick`: Current tick number
- `ticks(from, to, levels)`: Tick metadata for timeline UI

### 2. Point-in-Time Queries (for drawing graph)
- `thingsAtTick(tick)`: All things valid at tick, with links embedded

### 3. Delta Queries (for efficient updates)
- `changedThings(fromTick, toTick)`: What changed between ticks
  - Returns: ADDED, MODIFIED, REMOVED things
  - Question: Should link-only changes count as MODIFIED?

### 4. Detail Queries
- `thingData(tick, key)`: Full data for one thing (follows data_extends chain)
- `thingHistory(key, fromTick?, toTick?)`: All states of a thing over time (for debugging)

### 5. Config
- `renderingConfig`: Color mappings for UI

## Proposed Schema

```graphql
type Query {
  # Timeline
  latestTick: Int!
  ticks(fromTick: Int, toTick: Int, levels: [String!]): [Tick!]!

  # Point-in-time (for graph)
  thingsAtTick(tick: Int!): [Thing!]!

  # Delta (for efficient updates)
  changedThings(fromTick: Int!, toTick: Int!): [ThingChange!]!

  # Detail
  thingData(tick: Int!, key: String!): ThingData
  thingHistory(key: String!, fromTick: Int, toTick: Int): [ThingSnapshot!]!

  # Config
  renderingConfig: RenderingConfig!
}

type Subscription {
  # Pushes tick metadata when new tick occurs
  newTick: Tick!
}

type Mutation {
  # Manual tick (for testing/debugging)
  triggerTick(level: String!, desc: String): Tick!
}

# --- Types ---

type Tick {
  tick: Int!
  timestamp: Float!
  level: String!
  type: String!
  data: String  # JSON, nullable (desc for api_tick)
}

type Thing {
  key: String!        # Base64 CBOR
  thingType: String!
  triggerable: Boolean!
  links: [Link!]!
}

type Link {
  linkType: String!
  linkKey: String!    # Base64 CBOR
  toThingKey: String! # Base64 CBOR
}

type ThingData {
  key: String!
  thingType: String!
  triggerable: Boolean!
  links: [Link!]!
  data: String!       # JSON-encoded full data
}

type ThingSnapshot {
  tick: Int!          # When this snapshot was valid
  key: String!
  thingType: String!
  data: String!       # JSON-encoded
  links: [Link!]!
}

enum ChangeKind {
  ADDED
  MODIFIED
  REMOVED
}

type ThingChange {
  kind: ChangeKind!
  thing: Thing!       # For REMOVED, last known state
}

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

## REST Endpoints

- `POST /tick` - Body: `{"level": "...", "desc": "..."}` - Manually trigger tick
- `POST /debug_trigger/{name}` - Body: CBOR payload - Fire named debug event

## Open Questions

1. **Link-only changes**: If a thing's data is unchanged but links changed, is that MODIFIED?
   - Proposal: Yes, because the graph changed

2. **thingHistory**: Is this useful, or overkill for v1?
   - Could defer to later if not needed

3. **Pagination**: Skip for v1, add later if needed?

4. **Error handling**: What if thing doesn't exist at tick? Return null or error?
   - Proposal: Return null for thingData, empty list for thingHistory
