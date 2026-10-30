# Phase 1 Step 4: MCP Server - Findings and Decision

## Original Goal

The original plan was to build a separate MCP server process that would wrap the thingwatcher GraphQL API, providing tools like `get_ticks`, `get_things_at_tick`, `get_thing_data`, etc.

## What We Did Instead

Before implementing the MCP server, we tested whether an AI agent (Claude) could effectively interact with thingwatcher using existing CLI tools: `graphqurl` for GraphQL queries and `curl` for REST endpoints.

### Experiment Setup

Created a demo server (`demo_server.py`) that:
1. Starts a thingwatcher on port 8765 with in-memory storage
2. Creates sample Task and Message objects with links between them
3. Pauses at each phase using `debug_receive("continue")` to allow inspection
4. Advances when triggered via `curl -X POST http://localhost:8765/debug_trigger/continue`

### Queries Tested

```bash
# Basic state
graphqurl http://localhost:8765/graphql -q '{ latestTick }'
graphqurl http://localhost:8765/graphql -q '{ ticks { tick level data } }'

# Point-in-time view
graphqurl http://localhost:8765/graphql -q '{ thingsAtTick(tick: 2) { key thingType links { linkType toThingKey } } }'

# Delta queries
graphqurl http://localhost:8765/graphql -q '{ changedThings(fromTick: 1, toTick: 2) { kind thing { key thingType } } }'

# Detail view with data
graphqurl http://localhost:8765/graphql -q '{ thingData(tick: 3, key: "gmR0YXNraHdvcmtlci0y") { thingType dataChunks { data } } }'

# History
graphqurl http://localhost:8765/graphql -q '{ thingHistory(key: "gmR0YXNraHdvcmtlci0y") { tick dataChunks { data } } }'

# Combined queries with aliases
graphqurl http://localhost:8765/graphql -q '{
  message: thingData(tick: 3, key: "gmdtZXNzYWdlAQ==") { thingType dataChunks { data } links { linkType toThingKey } }
  worker2: thingData(tick: 3, key: "gmR0YXNraHdvcmtlci0y") { thingType dataChunks { data } }
}' 2>/dev/null | jq '.'

# Triggering continuation
curl -X POST http://localhost:8765/debug_trigger/continue
```

### Findings

**What works well:**
- Full GraphQL flexibility - can write exactly the query needed, combine multiple queries, use aliases
- `jq` integration for readable output formatting
- Query → trigger → query workflow is smooth
- `thingHistory` and `changedThings` enable understanding state evolution
- No additional infrastructure or dependencies needed

**Minor friction points:**
- Keys are base64-encoded CBOR (e.g., `"gmR0YXNraHdvcmtlci0y"` for `("task", "worker-2")`) - must discover them from `thingsAtTick` before using in `thingData`
- URL repeated in every command (minor)
- graphqurl prints "Executing query... done" to stderr (use `2>/dev/null` when piping to jq)

**What MCP would have added:**
- Automatic URL configuration (minor convenience)
- Key encoding/decoding helpers (nice-to-have)
- Predefined high-level operations (but less flexible than raw GraphQL)

## Decision

**Skip the MCP server for now.** The `graphqurl` + `curl` + `jq` workflow provides:
- Full access to all GraphQL queries and mutations
- More flexibility than predefined MCP tools would offer
- Zero additional code to maintain
- Works today without implementation effort

The MCP server can be revisited later if:
- Key encoding becomes a significant pain point
- We want to provide a more polished integration for non-technical users
- There's a specific use case that benefits from MCP's tool discovery

## Files Created

- `demo_server.py` - Interactive demo script with debug pauses (can be deleted or kept as example)
- `docs/querying_thingwatcher_as_an_agent.md` - Concise reference guide for agent use
