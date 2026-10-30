# Thingwatcher Implementation Meta-Plan

This document outlines the high-level milestones for implementing thingwatcher.

## Design Decisions

These decisions were made during planning:

- **Log format**: Structured JSON output on each tick
- **SQLite**: Use SQLAlchemy Core with `aiosqlite` driver for async operations
- **Debug triggers**: Implement as REST routes alongside GraphQL (Step 3)
- **Tick numbering**: Auto-incrementing per session (starts at 1)
- **MCP server**: Separate process that connects to GraphQL as a client
- **Global watcher**: Single global watcher, not multiple
- **Error handling**: Catch snapshotter errors, emit a "broken thing" state keyed by `("__broken__", id(obj), tick_number)` with error message in data
  - **[Step 5 update]**: For conflicts during snapshot unification (duplicate keys with different data, mixed states/deltas), we instead keep the original key but set `thing_type="__broken__"` and `thing_data={"error": "description"}`. This preserves the key for triggerability.
- **Dependencies**: Use `uv add` for latest versions (cbor2, sqlalchemy[aiosqlite], anyio, starlette, ariadne, uvicorn)
- **tick() is always sync**: Polls snapshotters synchronously, queues DB writes to a background worker task
- **GraphQL key encoding**: Base64-encode CBOR bytes when transmitting keys over GraphQL
- **Thread safety**: `track()`, `untrack()`, `tick()` are thread-safe via RLock. Configuration methods (`register_snapshotter`, `set_tick_interceptor`) should be called before multi-threaded use. Snapshotters run under the lock; user is responsible for their own objects' thread safety. Future async operations (`pause`, `receive`, DB writes) are assumed to be run from the single event loop thread and will NOT hold the lock during async operations (although `pause` and `receive` may need to briefly acquire it synchronously to update internal state that may conflict with `track()`, `untrack()`, `tick()`).

---

## Milestone Overview

### Step 1: Core Tracking + Log Output

**Goal**: A working Watcher that can track objects and print snapshots as structured JSON logs.

**Deliverables**:
- `Watcher` class with:
  - Tracked objects registry (WeakValueDictionary keyed by id)
  - Snapshotter registry (dict mapping typename -> StateSnapshotter)
  - Tick counter
  - `track(obj, typename)` method
  - `untrack(obj)` method
  - `tick(level, desc)` method
- Global watcher context:
  - Context variable holding current watcher (or None)
  - Module-level `track`/`untrack`/`tick` functions delegate to context
- Typename resolution: `type(obj).__module__ + "." + type(obj).__qualname__`
- On `tick()`:
  - Iterate all tracked objects
  - Call snapshotter for each
  - Catch exceptions -> emit broken thing state
  - Output JSON to logger
- Toy example: simple `Counter` class with a snapshotter
- Tests: track objects, tick, verify log output

**Files to create/modify**:
- `thingwatcher/watcher.py` - Watcher class implementation
- `thingwatcher/context.py` - Global context management
- `thingwatcher/api.py` - Wire up to context
- `tests/test_tracking.py` - Tests for tracking and ticking

---

### Step 2: SQLite Persistence

**Goal**: Ticks and thing states are persisted to SQLite via SQLAlchemy Core (aiosqlite driver).

**Deliverables**:
- SQLite schema:
  ```sql
  ticks(tick INTEGER PRIMARY KEY, timestamp REAL, level TEXT, type TEXT, data BLOB)
  thing_states(id INTEGER PRIMARY KEY, valid_from INTEGER, valid_until INTEGER,
               thing_key BLOB, thing_type TEXT, data BLOB, data_extends INTEGER, triggerable INTEGER)
  links(id INTEGER PRIMARY KEY, from_thing_key BLOB, valid_from INTEGER, valid_until INTEGER,
        link_type TEXT, link_key BLOB, to_thing_key BLOB)
  ```
  - **[Step 5 update]**: The `triggerable INTEGER` column should be removed from `thing_states`. Triggerability is now computed dynamically from watcher state (which anchors are currently paused), not stored in DB. Only the current tick can have triggerable things.
- CBOR serialization using `cbor2` with `canonical=True` for keys
- `Database` class wrapping SQLAlchemy async engine:
  - `create_schema()`
  - `write_tick(tick, timestamp, level, type, data)`
  - `write_thing_state(...)` / `write_thing_delta(...)`
  - `write_links(...)` / `invalidate_links(...)`
- Watcher integration:
  - Opens DB on `watch_and_serve_until` entry
  - Background worker task consumes a queue and writes to DB
  - `tick()` (sync) collects snapshots, enqueues write job, returns immediately
  - Tracks previous state to compute valid_until
- In-memory mode (`:memory:`) and file mode

**Files to create/modify**:
- `thingwatcher/db.py` - Database class
- `thingwatcher/watcher.py` - Add DB integration
- `tests/test_persistence.py` - Tests for DB read/write

---

### Step 3: GraphQL Backend + Debug Triggers

**Goal**: Query watcher state via GraphQL; enable test sequencing via debug triggers.

**Deliverables**:
- Starlette HTTP server started by `watch_and_serve_until`
- Ariadne GraphQL at `/graphql` (WebSocket subscriptions):
  - Queries:
    - `latestTick` -> current tick number
    - `ticks(fromTick, toTick, levels)` -> list of ticks with filtering
    - `thingsAtTick(tick)` -> things with embedded links (for graph drawing)
    - `changedThings(fromTick, toTick)` -> ADDED/MODIFIED/REMOVED things (includes link-only changes)
    - `thingData(tick, key)` -> thing with dataChunks (list of {id, data})
    - `thingHistory(key, fromTick?, toTick?)` -> list of ThingData over time
    - `dataChunk(id)` -> fetch single data chunk by thingstate_id (for caching)
    - `renderingConfig` -> color mappings
  - Subscriptions:
    - `newTick` -> pushes tick metadata when tick occurs
  - Mutations:
    - `triggerTick(level, desc)` -> manually create tick (type="remote_client")
- REST endpoints:
  - `POST /debug_trigger/{name}` - accepts CBOR body, signals waiters
- `watcher.debug_receive(name, timeout)` - async method, waits for debug_trigger
- Key encoding: Base64 CBOR, Data encoding: JSON

**Files to create/modify**:
- `thingwatcher/server.py` - HTTP/GraphQL server with resolvers
- `thingwatcher/schema.graphql` - GraphQL schema
- `thingwatcher/db.py` - Add read methods for queries
- `thingwatcher/watcher.py` - Add server integration, subscriptions, debug_receive
- `tests/test_graphql.py` - GraphQL query tests

**[Step 5.2 update]**: This design was _significantly_ redesigned at step 5.2. The original plan is no longer up to date, see `plans/phase1_step5p2_graphql_cleanup.md` for the current state of the graphql system.

---

### Step 4: MCP Server

**Goal**: AI agents can query and interact with thingwatcher via MCP protocol.

**Deliverables**:
- Separate Python entrypoint/process
- Connects to thingwatcher's GraphQL endpoint as a client
- MCP tools:
  - `get_ticks` - query ticks
  - `get_things_at_tick` - query things at a tick
  - `get_thing_data` - get full thing data
  - `get_links` - get links
  - `trigger` - POST to debug_trigger endpoint
  - `manual_tick` - POST to /tick endpoint
- Configuration: takes watcher URL as argument

**Files to create**:
- `thingwatcher/mcp_server.py` - MCP server implementation
- `tests/test_mcp.py` - MCP integration tests

**Update**: We tested using `graphqurl` + `curl` + `jq` as an alternative to building an MCP server. This approach provides full access to the GraphQL API with more flexibility than predefined tools. Decision: skip MCP server for now; the CLI-based workflow is sufficient for agent interaction. See `plans/phase1_step4_note.md` for details and `docs/querying_thingwatcher_as_an_agent.md` for the agent reference guide.

---

### Step 5: Pause/Trigger Mechanism (Production)

**Goal**: Real `pause()` and `receive()` for stepping through async code.

**Deliverables**:
- `pause(anchor, typename, tick_after)`:
  - Captures anchor via PauseCapturer
  - Creates thing state with `triggerable=True`
  - Ticks (if tick_after=True)
  - Awaits an asyncio.Event
  - On trigger: clears the thing state, returns
- `receive(anchor, typename, tick_after)`:
  - Same as pause but returns the trigger payload
- `POST /trigger/{thingstate_id}` REST endpoint:
  - Accepts CBOR payload
  - Writes trigger record to DB
  - Signals the waiting coroutine
- PauseCapturer registry (like snapshotter registry)
- Trigger records table in DB

**Files to create/modify**:
- `thingwatcher/watcher.py` - pause/receive implementation
- `thingwatcher/db.py` - trigger_records table
- `thingwatcher/server.py` - /trigger endpoint
- `thingwatcher/api.py` - wire up pause/receive
- `tests/test_pause.py` - pause/receive tests

**[Step 5 update]**: The design was significantly revised during detailed planning. Key changes:
- **GraphQL mutation instead of REST**: Use `triggerPause(key, payload)` mutation instead of `POST /trigger/{thingstate_id}`, for consistency with `triggerTick`. Trigger by thing_key (base64), not thingstate_id.
- **Deferred capturing**: Don't run PauseCapturer immediately in `pause()`. Store the anchor object (strong ref), run capturers during tick snapshot alongside snapshotters.
- **Snapshot unification**: During tick, unify results from snapshotters and capturers by key. Rules: one ThingDelta OR one or more identical ThingStates per key. Conflicts produce broken things.
- **Dynamic triggerability**: Not stored in DB. Computed from watcher state (`is_paused` flag on PauseInfo). Historical ticks always show `triggerable=False`.
- **Multiple anchors per key**: Different anchor objects can produce the same key; single trigger wakes all. Same anchor object cannot be paused twice.
- **TriggerResult dataclass**: Return type-safe dataclass from `trigger_pause()` method.

See `plans/phase1_step5_pause_trigger.md` for the full revised design.

---

### Step 6: Frontend UI

**Goal**: Interactive web visualization of tracked objects over time.

**Deliverables**:
- React/TSX frontend in `thingwatcher/frontend/`
- Build system: esbuild, dynamically compiled on server start
- Layout:
  - Bottom: Timeline scrubber (tick navigation, level filter, "live" mode)
  - Main: Cytoscape.js graph (fcose layout)
  - Right panels (dockview): Details view (top), Things list (bottom)
- Node rendering:
  - Colored circles by thing_type
  - Border states: green (new), yellow (changed), red dashed (removed), none (idle)
  - Hover tooltip: type + key
  - Click to select -> shows in details panel
- Data fetching: urql for GraphQL, subscriptions for live updates
- UI components: base-ui for tooltips, pickers, etc.
- Re-layout on node set changes

**Files to create**:
- `thingwatcher/frontend/` - React app
- `thingwatcher/frontend/package.json`
- `thingwatcher/frontend/src/App.tsx`
- `thingwatcher/frontend/src/Timeline.tsx`
- `thingwatcher/frontend/src/Graph.tsx`
- `thingwatcher/frontend/src/DetailsPanel.tsx`
- `thingwatcher/frontend/src/ThingsList.tsx`
- `thingwatcher/server.py` - serve static files + esbuild

---

## Implementation Order

```
Step 1: Core Tracking + Log Output
    │
    ▼
Step 2: SQLite Persistence
    │
    ▼
Step 3: GraphQL + Debug Triggers ◄── enables testing subsequent steps
    │
    ▼
Step 4: MCP Server
    │
    ▼
Step 5: Pause/Trigger (Production)
    │
    ▼
Step 6: Frontend UI
```

---

## Next Steps

For each step, create a detailed implementation plan at `plans/phase1_step{N}_{name}.md` with:
- Specific code changes
- Test cases
- Dependencies to add
- Any remaining design decisions

Ready to proceed to Step 1 detailed planning.
