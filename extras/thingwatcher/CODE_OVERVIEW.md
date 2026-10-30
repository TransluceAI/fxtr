# Thingwatcher Code Overview

A viewer for visualizing the state of interacting objects and async tasks over time.

## Directory Structure

```
thingwatcher/
├── pyproject.toml        # Package config (uv/hatch, pytest)
├── specs/                # Design specifications
│   └── overview.md       # Main API and architecture spec
├── plans/                # Implementation plans
│   ├── phase1_overview.md           # Meta-plan for all steps
│   └── phase1_step{k}    # Plan for each individual step
├── docs/                 # Documentation
│   ├── querying_thingwatcher_as_an_agent.md  # Agent interaction guide
│   └── registry_pattern.md  # React pattern for child-to-parent state propagation
├── examples/             # Example scripts
│   ├── demo_server.py    # Interactive demo with debug pauses
│   ├── demo_dynamic_rendering/  # Custom renderer demo (see README.md inside)
│   ├── demo_drag_drop/   # Drag and drop with custom drop regions
│   ├── demo_embed/       # Embed view with triggerable widgets
│   ├── demo_fxtr/        # fxtr2 namespace visualization demo
│   ├── demo_multi_chunk/ # Streaming updates, multi-chunk data, and async updates
│   └── demo_pause_trigger/  # Pause/trigger with concurrent workflows
├── thingwatcher/         # Python package
│   ├── __init__.py       # Re-exports public API
│   ├── api.py            # Public API definitions and types
│   ├── context.py        # Global watcher context (ContextVar)
│   ├── fxtr/             # fxtr2 integration module (see fxtr/LOCAL_CODE_OVERVIEW.md)
│   │   ├── __init__.py   # Re-exports: fxtr_namespace_view, register_fxtr_renderer
│   │   ├── cbor_transform.py    # Transform fxtr entities to ThingStates
│   │   ├── immutable_store.py   # ImmutableStore for fxtr entities
│   │   ├── snapshotter.py       # Namespace snapshotter/anchor capturer
│   │   ├── namespace_view.py    # Main context manager
│   │   ├── renderers.py         # Renderer registration for fxtr types
│   │   └── components/          # TSX renderer components
│   ├── thingwatcher-bindings.d.ts  # TypeScript types for custom renderers (for Deno typechecking)
│   ├── db.py             # SQLite persistence layer (SQLAlchemy Core)
│   ├── registry.py       # TypeRegistry class + global/local registries
│   ├── schema.graphql    # GraphQL schema definition
│   ├── server.py         # HTTP server with GraphQL + frontend serving
│   ├── watcher.py        # Watcher class implementation
│   └── frontend/         # React web UI (see frontend/LOCAL_CODE_OVERVIEW.md)
│       ├── package.json  # Dependencies (React, urql, React Flow, dockview, base-ui)
│       ├── main.tsx      # Main app entry point (populates __THINGWATCHER_INJECTED__)
│       ├── App.tsx       # Main layout with DockviewLayout + Timeline
│       ├── embed.tsx     # Embed view entry point (populates __THINGWATCHER_INJECTED__)
│       ├── EmbedApp.tsx  # Embed view component with postMessage handling
│       ├── debug-embed.tsx    # Debug UI entry point (populates __THINGWATCHER_INJECTED__)
│       ├── DebugEmbedApp.tsx  # Debug UI with iframe + controls
│       ├── scripts/      # Build scripts
│       │   ├── generate-shim.mjs       # Auto-generates shims for npm packages
│       │   └── generate-local-shim.mjs # Auto-generates shims for local TypeScript modules
│       ├── components/   # UI components
│       │   ├── DockviewLayout/  # Dockview panel container + wrappers
│       │   ├── ThingContextMenu/  # Context menu for "Open in panel"
│       │   ├── Timeline/        # Timeline scrubber
│       │   ├── Graph/           # React Flow graph visualization (Phase A)
│       │   ├── DetailsPanel/    # Selected thing details
│       │   ├── ThingsList/      # Filterable things list
│       │   └── RootsPanel/      # Named root entry points
│       ├── hooks/        # React hooks (useGraphQL)
│       ├── graphql/      # GraphQL queries/mutations/subscriptions
│       └── types/        # TypeScript type definitions
└── tests/
    ├── conftest.py       # Shared test fixtures (find_free_port, wait_for_server, graphql_query)
    ├── test_basic.py     # Basic import and smoke tests
    ├── test_graphql.py   # GraphQL API tests
    ├── test_tracking.py  # Core tracking functionality tests
    ├── test_persistence.py # Database persistence tests
    ├── test_pause.py     # Pause/trigger mechanism tests
    ├── test_roots.py     # Roots functionality tests
    ├── test_short_id.py  # ShortId functionality tests
    ├── test_ownership.py # Snapshot ownership and conflict tests
    ├── test_streaming.py # Streaming subtick tests
    ├── test_hard_recapture.py # Hard recapture and post-trigger tick tests
    ├── test_pop_chunks.py # ThingDelta push_chunk/pop_chunks functionality tests
    ├── test_async_update.py # Async update (AsyncThingUpdate) tests
    └── test_fxtr/        # fxtr2 integration tests (49 tests)
```

## Key Concepts

- **Things**: Objects being tracked, identified by a key
- **Ticks**: Points in time when state snapshots are captured. Types:
  - Regular ticks (`api_tick`): Full snapshots of all tracked objects
  - Stream ticks (`stream`): Mutable ticks for streaming updates to individual objects
  - Post-trigger ticks (`post_trigger`): Automatically created after a pause trigger, before the task wakes
  - Hard recapture ticks (`hard_recapture`): Reset all state and take fresh snapshot
- **Streaming Updates**: Mechanism to update individual objects without creating new ticks. Uses `streaming_update(obj)` to update a single object in the current stream tick. Multiple streaming updates can happen to the same stream tick.
- **Async Updates**: Mechanism for snapshotters to perform async work. A snapshotter can return `AsyncThingUpdate` containing:
  - `immediate_update`: State shown right away at the current tick
  - `async_updater`: Async function that computes final state, result appears in a streaming tick
  - Async updates are automatically cancelled if the object is snapshotted again before completion
- **Snapshotters**: Convert tracked objects to serializable ThingState/ThingDelta. Two types:
  - `StatelessSnapshotter`: Returns only `ThingState`. Multiple stateless sources may emit the same key if states are identical.
  - `StatefulOwningSnapshotter`: Returns `ThingState`, `ThingDelta`, or `AsyncThingUpdate`. Exclusively owns its output keys - conflicts with other sources produce broken things. Required for `streaming_update()`. Must implement both `snapshot_owned()` (normal ticks) and `stateless_snapshot()` (hard recapture). Both methods can return `AsyncThingUpdate` for async work.
- **AnchorCapturers**: Handle anchor objects (for pauses, receives, and roots) to create ThingState or ThingRef. Always treated as stateless.
- **Triggerable**: A thing that has a paused task waiting on it; can be triggered to resume
- **Roots**: Named entry points (e.g., "currentUser", "mainTask") that provide consistent starting points for exploring the state graph; tracked per-tick with valid_from/valid_until for historical queries
- **Watcher**: Central coordinator that manages tracking, pauses, roots, ownership maps, streaming updates, and serves the UI

## Module Breakdown

### `thingwatcher/context.py`
Global context management using `contextvars.ContextVar`. Holds the currently active `Watcher` instance so that module-level `track`/`tick` calls know where to route.

### `thingwatcher/registry.py`
Contains the `TypeRegistry` class and global registry instance:
- **TypeRegistry class** - holds all type-related configuration:
  - Snapshotters (typename -> StatelessSnapshotter | StatefulOwningSnapshotter)
  - Anchor capturers (typename -> AnchorCapturer) - used for pauses, receives, and roots
  - Rendering configs (thing_type -> color/link_colors)
  - Custom renderers (thing_type -> TSX source code)
  - Renderer builders (list of builder functions for dynamic renderer generation)
- **Global registry** - one global `TypeRegistry` instance (`_global_registry`)
- **Watcher-local registry** - each Watcher has its own `_local_registry`
- **Lookup order** - local registry first, then global registry

**Renderer Builders:**
- `register_renderer_builder(builder)` - Register a builder function
- `unregister_renderer_builder(builder)` - Unregister a builder
- Builder function: `(thing_type: str) -> RendererBuilderResult | None`
- `RendererBuilderResult` dataclass with `tsx_source: str` and `imports: dict[str, str]`
- Builders are tried in most-recent-first order for types without static registration
- Builders run on each lookup (not cached), but generated content gets content-addressed module_id

These can be configured before any watcher exists. Individual watchers can override with local registrations.

### `thingwatcher/thingwatcher-bindings.d.ts`
TypeScript type definitions for the `thingwatcher-bindings` module that custom renderers import from. This file enables development-time typechecking of `.tsx` renderer files using [Deno](https://deno.land/). The types must be kept in sync with the actual runtime implementation in `frontend/lib/ThingWatcherBindingsExternal.tsx`.

**Custom Renderer API (Handle + Hooks Pattern):**
- Renderers receive an opaque `handle` and use hooks to access thing data
- All renderer modules must have a default export of type `ThingWatcherRenderConfig`
- Available hooks:
  - `usePayload(handle, { chunk?: 'first' | 'latest' })` - Get payload data
  - `useDataChunks(handle)` - Get all data chunks
  - `useLinks(handle)` - Get links from this thing
  - `useThingMetadata(handle)` - Get key, shortId, thingType
  - `useTriggerable(handle)` - Get [triggerable, triggerRecord] (Full + Ref)
  - `useTrigger(handle)` - Get trigger function (Full only)
  - `useDraggableIdMarker(handle)` - Get draggable marker component (Full + Preview)
  - `usePersistentThingState(handle, key)` - Per-thing persistent state storage (Full only)
  - `usePersistentGlobalState(handle, key)` - Module-scoped persistent state storage (Full only)
  - `useThingDropRegion(handle)` - Create drop zones for receiving dragged things (Full only)
- Both persistent state hooks support updater functions: `setValue(prev => newValue)`
- Available components:
  - `ChildNodeView` - Render another thing inline with its FullRenderer
  - `NodeRef` - Render a clickable block reference to another thing
  - `InlineNodeRef` - Render a compact inline reference
  - `AdoptStyleSheet` - Declaratively adopt a stylesheet into the shadow root
- Graph components (for custom graph node renderers):
  - `Port` - Connection point on a graph node (wraps React Flow Handle)
  - `PortPosition` - Enum: Top, Bottom, Left, Right
  - `LinkEdgeConfig` - Declarative edge configuration (source/target ports, labels, styles)
- Handle types: `FullRendererHandle`, `RefRendererHandle`, `PreviewRendererHandle`
- Props interfaces: `FullRendererProps` (handle, args, isTopLevel), `RefRendererProps` (handle, args), `PreviewRendererProps` (handle), `GraphNodeRendererProps` (handle)

**Custom Renderer Development:**
- Renderers are `.tsx` files with a default export of type `ThingWatcherRenderConfig`:
  ```tsx
  import type { ThingWatcherRenderConfig } from 'thingwatcher-bindings';

  function MyFull(props: FullRendererProps) { ... }
  function MyRef(props: RefRendererProps) { ... }
  function MyGraph(props: GraphNodeRendererProps) { ... }

  export default {
    dialect: "0.1",
    renderers: {
      "MyType": { full: MyFull, ref: MyRef, inline: MyInline, preview: MyPreview, graph: MyGraph },
    }
  } as ThingWatcherRenderConfig;
  ```
- A single module can export renderers for multiple thing types (multi-renderer bundles)
- At runtime, Python reads the TSX source and passes it to esbuild-wasm in the browser
- esbuild uses the automatic JSX transform (`jsx: 'automatic'`), so no `import React` is needed
- For development-time typechecking, each demo folder has a `deno.json` that maps `thingwatcher-bindings` to the types file
- Run `deno check *.tsx` in a demo folder to typecheck renderers
- See `examples/demo_dynamic_rendering/README.md` for detailed setup instructions

**Multi-file Bundling and CSS:**
- `register_renderer(thing_types, tsx_source, imports)` registers renderer modules:
  - `thing_types` can be a single string, a list of strings, or a mapping `{thing_type: renderer_key}`
  - Returns a content-addressed module ID (hash of the bundle contents)
  - Multiple thing types can share the same compiled module
- CSS support:
  - `.module.css` files use `local-css` loader (scoped class names, import as `styles` object)
  - `.css` files use `css` loader (global styles, bare classnames like `className="foo"`)
  - Remote CSS URLs (`https://.../*.css`) are fetched and bundled at compile time
- Import paths must include extensions (e.g., `./utils.tsx`, `./styles.module.css`)
- Renderers run inside shadow DOM for style isolation; CSS attached via `adoptedStyleSheets`
- CSS stylesheet is hoisted to module level and shared across all renderers in the module

**External Packages from esm.sh:**
- Renderers can import npm packages (e.g., `import ReactJson from "react-json-view"`)
- Bare imports are automatically rewritten to esm.sh URLs (e.g., `https://esm.sh/react-json-view`)
- React is externalized via `?external=react` to ensure external packages use the same React version
- This is added automatically for esm.sh imports that don't already include an `external` parameter
- Full URLs are also supported: `import Foo from "https://esm.sh/some-package@1.0.0"`

**React Instance Unification (Import Map Shims):**
- Problem: esm.sh dependencies with `external=react` resolve via browser import map, which could load a different React instance than the one bundled in main.js
- Solution: Import map points to auto-generated shims (e.g., `/shims/react.js`) that re-export from `window.__THINGWATCHER_INJECTED__`
- main.tsx/embed.tsx populate `window.__THINGWATCHER_INJECTED__` with bundled modules (React, react-dom, thingwatcher-bindings) at startup
- Shims for npm packages are generated by `frontend/scripts/generate-shim.mjs` (uses dynamic import to enumerate exports)
- Shims for local TypeScript modules (thingwatcher-bindings) are generated by `frontend/scripts/generate-local-shim.mjs` (uses esbuild metafile to enumerate exports without importing)
- This ensures all code (main app, dynamic renderers, esm.sh deps) uses the same React instance and thingwatcher-bindings

### `thingwatcher/db.py`
SQLite persistence layer using SQLAlchemy Core with async engine (aiosqlite driver):
- `ticks` table: tick number, timestamp, level, type, data, seq (global sequence number for streaming updates)
- `data_chunks` table: CBOR data chunks with optional `extends` backpointer for append chains (like git commits)
- `thing_states` table: state snapshots with `data_chunk_id` FK, valid_from/valid_until and valid_from_seq/valid_to_seq for temporal queries
- `links` table: links between things with valid_from/valid_until and valid_from_seq/valid_to_seq
- `trigger_records` table: history of pause triggers (key, thingstate_id, tick, payload)
- `short_ids` table: maps CBOR keys to short integer IDs for CLI convenience
- `roots` table: named entry points with valid_from/valid_until and valid_from_seq/valid_to_seq for temporal tracking
- `Database` class with:
  - Transaction-based write methods for persistence
  - Data chunk methods: `write_data_chunk`, `get_data_chunk_by_id`
  - Read methods for GraphQL resolvers: `get_latest_tick`, `get_ticks`, `get_things_at_tick`, `get_links_at_tick`, `get_thing_data`, `get_thing_state_by_id`, `get_thing_history`, `get_links_for_thing`, `get_backlinks_for_thing`
  - Trigger record methods: `write_trigger_record`, `get_trigger_records`, `get_trigger_record_by_id`
  - Short ID methods: `get_or_create_short_id`, `resolve_short_id`, `get_short_id`
  - Root methods: `write_root`, `invalidate_root`, `get_roots_at_tick`, `get_root_at_tick`, `get_root_history`
  - Bulk methods: `invalidate_all_live` (for hard recapture - invalidates all thing_states, links, roots with valid_until IS NULL)

Note: `triggerable` is NOT stored in the DB - it's computed dynamically from watcher state.

Data chunk design: Multiple thing_states can share the same data_chunk (avoiding duplication on link-only changes). ThingDelta with `push_chunk` creates new chunks. If `pop_chunks > 0`, it first walks back that many steps in the extends chain before appending. `POP_ALL` starts a fresh chain (no extends). ThingState after ThingDelta always creates a fresh chunk (no comparison).

Chain walking helper: `walk_chunk_chain(head_id, steps)` walks back N steps and returns the ancestor chunk ID (or None if walked exactly to end, raises if too many steps).

### `thingwatcher/watcher.py`
The `Watcher` class with:
- `_tracked`: WeakRef-based object tracking (auto-cleanup on GC)
- `_local_snapshotters`: Watcher-local snapshotter overrides
- `_local_anchor_capturers`: Watcher-local anchor capturer overrides
- `_paused_anchors`: Strong refs to anchor objects with PauseInfo (events, state)
- `_triggerable_at_tick`: Temp map rebuilt each tick: key_bytes -> pause info for trigger lookups
- `_tick_interceptor`: Testing hook to capture tick results
- `_current_things`: In-memory tracking for change detection (maps key_bytes -> CurrentThingInfo with thingstate_id, data_chunk_id, last_was_state flag, data_bytes for comparison, links)
- `_tick_callbacks`: Subscription support for GraphQL newTick subscription
- `_debug_triggers`: Debug trigger mechanism for test synchronization
- `_roots`: Maps root name -> (anchor, typename) for named entry points
- `_current_roots`: In-memory tracking for root change detection (name -> CurrentRootInfo)
- `_owned_things`: Maps object id -> set of thing key bytes it owns (for StatefulOwningSnapshotter)
- `_thing_owners`: Maps thing key bytes -> owning object id (reverse lookup)
- `_stateless_keys`: Set of thing key bytes produced by stateless sources (for streaming subtick conflict detection)
- `_pending_async_updates`: Maps object id -> CancelScope for pending async updates (cancelled on re-snapshot)
- `_async_update_send/receive`: Memory object streams for queueing async update jobs to the runner task
- `track(obj, typename)`: Register object for tracking (validates snapshotter exists)
- `untrack(obj)`: Remove object from tracking
- `tick(level, desc)`: Snapshot all tracked objects, pause anchors, and root anchors; unify by key; log JSON; enqueue DB write
- `streaming_update(obj)`: Update a single object in streaming fashion (requires StatefulOwningSnapshotter); creates or updates a stream tick
- `pause(anchor, typename, tick_after)`: Suspend until triggered via trigger_pause
- `receive(anchor, typename, tick_after)`: Like pause but returns trigger payload
- `trigger_pause(key_bytes, payload)`: Wake all tasks paused on this key, write trigger record, create post_trigger tick
- `get_triggerable_things()`: Get list of currently triggerable (key, typename) pairs
- `hard_recapture()`: Reset all in-memory state, invalidate all DB records, take fresh stateless snapshot
- `add_root(name, anchor, typename, replace)`: Add a named root entry point
- `remove_root(name)`: Remove a named root
- `_async_tick(level, desc, type)`: Async version that waits for write completion
- `debug_receive(name, timeout)`: Wait for a debug trigger (for testing)
- Background writer task that persists snapshots to SQLite with:
  - Change detection: skip writes for unchanged data
  - Link diffing: independent of data changes
  - Disappearance detection: invalidate things not seen in a tick
  - Root processing: diff root bindings against `_current_roots`, write/invalidate as needed
  - Tick callback notification after commit

Snapshot unification (in `_take_snapshot` and `_unify_snapshots`):
- Runs snapshotters on tracked objects AND capturers on pause/root anchors
- Separates results by snapshotter type: owning vs stateless
- Ownership validation: owned keys cannot overlap with any other source
- Stateless validation: identical ThingStates per key allowed, conflicts produce broken things
- Updates ownership maps (`_owned_things`, `_thing_owners`, `_stateless_keys`) after validation
- Builds triggerable map for trigger lookups
- Returns root_key_map for root binding tracking

### `thingwatcher/server.py`
HTTP server using Starlette + Ariadne + Hypercorn:
- GraphQL endpoint at `/graphql/` (HTTP POST and WebSocket subscriptions)
  - Note: Starlette mount redirects `/graphql` → `/graphql/` (trailing slash required)
- REST endpoint `/debug_trigger/{name}` for test synchronization
- Query resolvers:
  - `latestTick` → Returns Tick object with nested `things(triggerable: Boolean)`, `changes(since: Int!)`, and `roots`
  - `tick(index: Int!)` → Get specific tick by index
  - `ticks(from, to, levels)` → Timeline listing
  - `thing(shortId/key, atTick)` → Direct thing lookup with data
  - `things(keys, atTick)` → Batch fetch things by keys (returns array with nulls for not-found)
  - `thingHistory(shortId/key, from, to)` → Historical states
  - `dataChunk(id)` → Single data chunk by ID
  - `renderingConfig` → UI configuration
  - `triggerRecords`, `triggerRecord(id)` → Trigger history
  - `root(name, atTick)` → Look up a specific root by name
- Mutation resolvers: `triggerTick`, `triggerPause(shortId/key, payload)`
- Subscription resolver: `newTick` (uses legacy `graphql-ws` protocol for gql client compatibility)
- Field resolvers for nested types:
  - `Tick.things`, `Tick.changes`, and `Tick.roots` for nested queries
  - `Thing.data` (first chunk), `Thing.dataChunks`, `Thing.links`
  - `Link.toThing` for graph traversal
  - `Root.thing` for root target resolution
  - `TriggerRecord.thing` for trigger context
- Helper functions for base64 key encoding/decoding, CBOR→JSON conversion
- Triggerability computed dynamically from watcher state (only latest tick can be triggerable)
- Lifespan handler signals when server is ready (used for reliable startup in tests)

### `thingwatcher/frontend/`
React-based web UI for visualizing tracked objects. See `frontend/LOCAL_CODE_OVERVIEW.md` for detailed component documentation.

**Architecture:**
- Built with esbuild (TypeScript + CSS Modules), compiled on-demand by Python server
- urql + graphql-ws for GraphQL queries and live subscriptions
- graphcache (@urql/exchange-graphcache) for normalized entity caching (DataChunk entities cached by ID)
- React Flow (@xyflow/react) with webcola for graph visualization (force-directed layout with physics simulation)
- Dockview for resizable, rearrangeable panel layout with persistence
- base-ui Menu component for context menus
- Dark theme (header + dockview panels + timeline)

**Key components:**
- **DockviewLayout**: Main dockview container managing panel arrangement. Wraps Graph, DetailsPanel, ThingsList, and RootsPanel in dockview panels. Handles layout persistence to localStorage and auto-reopens main panels if closed. Provides RendererContext for custom renderers. Integrates panel sidebar.
- **Sidebar (PanelSidebar + ResizableSidebar)**: Collapsible sidebar to the left of dockview showing all open panels. Thing panels show using RefRenderer with hover preview. Groups labeled "Group N". Click to focus panel, X to close. Resizable (150-400px). Toggle button in app header.
- **Graph**: React Flow canvas showing things as nodes, links as edges. Uses cola.js force-directed layout with position persistence. Supports node selection (white box-shadow), change state visualization (colored borders for added/modified/disappearing), viewport tracking for changed nodes, and interactive node dragging with physics simulation.
- **Timeline**: Bottom bar (outside dockview) with tick scrubber, step buttons, live mode toggle. Shows current tick's level/timestamp/description. Includes a refresh button (↻) to reload custom renderers from disk.
- **DetailsPanel**: Selected thing info. If a custom renderer is registered for the thing type, renders it; otherwise shows JSON tree viewer. Standard header (thingType, shortId, key) always shown. Shows forward links and backlinks (things pointing to this thing). Right-click on links/backlinks opens context menu.
- **ThingsList**: Filterable/sortable list of all things at current tick. Features a **pinned section** at the top showing pinned things (with unpin button). Pinned things that don't exist at current tick show "Not present" state. Main list shows all snapshot things with pin indicator badge.
- **RootsPanel**: List of named root entry points. Click to select the resolved thing.
- **ThingContextMenu**: Context menu using base-ui Menu, shown on right-click for "Open in new panel" action.
- **ThingViewPanel**: Standalone panel for viewing a specific thing (opened via context menu), with its own GraphQL query. Participates in the active things system by marking its thing key on mount.
- **ThingPanelContent**: Shared panel content component showing: source info header (where the thing came from - snapshot or store, with pin button), meta info (type, shortId, key), renderer content, trigger records, and backlinks. Shows "Not present at tick N" fallback for things that don't exist at current tick.
- **DynamicRenderer**: Compiles and renders custom TSX renderers using esbuild-wasm. Caches compiled components by thingType. Validates dialect version on compile. Supports four renderer types: FullRenderer, RefRenderer (block), InlineRefRenderer (inline), and PreviewRenderer (hover cards).
- **ThingWatcherBindings (lib)**: SDK library for custom renderers. Uses handle + hooks pattern where renderers receive an opaque handle and use hooks (`usePayload`, `useLinks`, `useTriggerable`, `useTrigger`, `useDraggableIdMarker`, `usePersistentThingState`) to access data. Also provides `ChildNodeView`, `NodeRef`, and `InlineNodeRef` components for rendering nested things.
- **HoverPreview (lib)**: Hover preview cards using base-ui PreviewCard. Shows PreviewRenderer content on NodeRef/InlineNodeRef hover; shows backlinks on DraggableIDMarker hover. Supports nested hovers and closes on navigation.
- **useChunkResolver (hooks)**: Resolves chunk IDs to data using graphcache. Used by DetailsPanel and ThingView for multi-chunk things.
- **ActiveMarksContext (hooks)**: Registry-based system for tracking "active" thing keys with source classification. Components register active marks with a source (snapshot, selection, panel, pin, breadcrumb). `useActiveKeys()` returns a Map with stability info (stable vs transient). Keys stay active until all marks are released.
- **PinnedThingsContext (hooks)**: Manages both pinned things and breadcrumbs. Pinning registers an active mark; breadcrumbs transitively keep things active. Includes `addBreadcrumbs`, `removeBreadcrumb`, and `useBreadcrumbsFor` for navigation history tracking.
- **useAdditionalActiveThings (hooks)**: Batch-fetches active things not in the snapshot (e.g., store things with open panels). Uses `things(keys, atTick)` GraphQL query. Results merged with snapshot things for Graph and ThingsList.

**Data flow:**
1. App.tsx fetches tick list and current tick data via urql
2. Subscribes to `newTick` for live updates (auto-refetch when live mode enabled)
3. Selection state shared via React context; clicking node/list item updates DetailsPanel
4. Trigger button calls `triggerPause` mutation

**Server integration:**
- `FrontendHandler` in server.py compiles TypeScript/CSS via esbuild subprocess
- Serves `index.html` at `/`, compiled JS at `/main.js`, CSS at `/main.css`
- Generates and serves import map shims at `/shims/*.js` (e.g., `/shims/react.js`, `/shims/react/jsx-runtime.js`)
- Shims are auto-generated by running `scripts/generate-shim.mjs` which introspects actual package exports
- Dev mode recompiles on each request; production caches compiled output

### `thingwatcher/schema.graphql`
GraphQL schema with unified types (Step 5.2 cleanup):
- **Core types:**
  - `Tick` - Contains `index`, `things(triggerable)`, `changes(since)`, `roots` for hierarchical queries
  - `Thing` - Unified type with `shortId`, `key`, `keyJSON`, `data`, `dataChunks`, `links`, `backlinks`
  - `Link` - Bidirectional type with `linkKey`, `linkKeyJSON`, `fromThing: Thing!`, `toThing: Thing!` - use `toThing` for forward links, `fromThing` for backlinks
  - `Root` - Named entry point with `name` and `thing: Thing!`
  - `DataChunk` - Individual data chunk with `id` and `data`
  - `ThingChange` - Change tracking with `kind` (ADDED/MODIFIED/REMOVED) and `thing`
- **Trigger types:**
  - `TriggerResult` - Mutation result with `ok`, `triggerId`, `error`
  - `TriggerRecord` - Trigger history with `thing: Thing!` (state at trigger time)
- **Config types:** `RenderingConfig`, `ThingTypeConfig`, `LinkColorConfig`
- **Custom scalar:** `JSON` for arbitrary data objects
- **Key design:**
  - All thing lookups accept `key`, `shortId`, or `keyJSON` (exactly one required)
  - `keyJSON` fields provide human-readable JSON view of CBOR keys (nullable if non-JSON)
  - `Link.linkKeyJSON` similarly exposes link keys as JSON
  - `latestTick.things(triggerable: true)` replaces old `triggerableThings` query
  - `tick(index).things` and `tick(index).changes(since)` replace old top-level queries
  - `Thing.data` returns first chunk; `Thing.dataChunks` returns full chain
  - `Link.toThing` enables graph traversal in single queries
- Mutations for `triggerTick`, `triggerPause`, and `hardRecapture`
- Subscription `newTick` for live updates

### `thingwatcher/api.py`
Public API that delegates to context/registry:
- Module-level `track`/`untrack`/`tick` are no-ops when no watcher active
- `register_snapshotter` etc. delegate to global registry
- Re-exports `Watcher`, data types
- Defines snapshotter ABCs:
  - `StatelessSnapshotter`: ABC with `snapshot(obj) -> ThingState | list[ThingState]`
  - `StatefulOwningSnapshotter`: ABC with `snapshot_owned(obj) -> StatefulResult | AsyncThingUpdate[StatefulResult]` and `stateless_snapshot(obj) -> StatelessResult | AsyncThingUpdate[StatelessResult]`
- Type aliases: `StatelessResult` = `ThingState | Sequence[ThingState]`, `StatefulResult` = `ThingState | ThingDelta | Sequence[...]`
- `AsyncThingUpdate[T]`: Async snapshot result with `immediate_update: T` and `async_updater: () -> Awaitable[StatefulResult]`
- `AnchorCapturer` remains a Protocol (always stateless)

### `thingwatcher/fxtr/`
Integration module for visualizing [fxtr2](../../v2) entities in thingwatcher. Allows mounting fxtr namespaces as thingwatcher data sources with entity graph navigation.

**Key components:**
- **`namespace_view.py`**: `fxtr_namespace_view()` - main async context manager that mounts a namespace in thingwatcher
- **`cbor_transform.py`**: Transforms fxtr CBOR entities to `ThingState` objects with links extracted
- **`immutable_store.py`**: `FxtrImmutableStore` - implements `ImmutableStore` for lazy entity lookup
- **`snapshotter.py`**: `FxtrNamespaceSnapshotter` - tracks namespace pins using `AsyncThingUpdate`
- **`renderers.py`**: Dynamic renderer builder for fxtr entity types with fallback renderer
- **`track_helpers.py`**: `track_id()` - convenience function for linking to fxtr entities from snapshotters

**Two usage patterns:**

1. **Explicit namespace mounting** with `fxtr_namespace_view()`:
```python
from thingwatcher import Watcher
from thingwatcher.fxtr import fxtr_namespace_view
from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace

async with watcher.watching_and_serving():
    async with SQLiteFxtrNamespace(db_url, "demo") as ns:
        async with fxtr_namespace_view(ns, "demo", watcher):
            watcher.tick("info", "Initial state")
```

2. **Automatic entity tracking** with `track_id()` (simpler for snapshotters):
```python
from thingwatcher import Watcher, ThingState, ThingLink
from thingwatcher.fxtr import track_id

# Enable fxtr_support on the watcher
watcher = Watcher(db_path="demo.db", port=8765, fxtr_support=True)

# In a snapshotter, link to fxtr entities easily:
return ThingState(
    key=["my_thing"],
    thing_type="my_type",
    thing_data={},
    links=[ThingLink(link_type="ref", link_key=["data"], to_thing_key=track_id(fxtr_id))]
)
```

See `fxtr/LOCAL_CODE_OVERVIEW.md` for detailed documentation.

## Public API

### Instrumentation (used in code being watched)
- `track(obj, typename)` - Start tracking an object (raises if no snapshotter)
- `untrack(obj)` - Stop tracking an object
- `tick(level, desc)` - Record a state snapshot. **Design invariant: tick() must NEVER raise an exception when called with valid arguments.** Snapshotter errors and invalid ThingDelta values are captured as "broken things" rather than propagating to the caller.
- `streaming_update(obj)` - Update a single object in streaming fashion (requires StatefulOwningSnapshotter). May raise if `obj` is misconfigured (not tracked, no owning snapshotter), but snapshotter errors and invalid ThingDelta produce broken things rather than raising.
- `pause(anchor)` - Pause for manual stepping in UI (Step 5)
- `receive(anchor)` - Wait for user input from UI (Step 5)
- `add_root(name, anchor, typename, replace)` - Add a named root entry point (raises if no watcher)
- `remove_root(name)` - Remove a named root (raises if no watcher)

### Configuration (used in watcher setup)
- `register_snapshotter(type, snapshotter)` - Register how to serialize a type (global)
- `watcher.register_snapshotter(type, snapshotter)` - Register locally (overrides global)
- `register_anchor_capturer(type, capturer)` - Register how to serialize anchor objects (for pauses, receives, roots)
- `configure_rendering(type, color, ...)` - Configure UI appearance
- `register_renderer(thing_types, tsx_source, imports)` - Register a custom TSX renderer module for one or more thing types (accepts string, list, or mapping). Both `tsx_source` and `imports` values can be strings or `pathlib.Path` objects. When paths are provided, files are read lazily and changes are detected via mtime - use the frontend refresh button to pick up changes.
- `register_renderer_builder(builder)` - Register a dynamic renderer builder function that generates TSX on-demand for unrecognized types. Builder takes `thing_type: str` and returns `RendererBuilderResult(tsx_source, imports)` or `None`. Builders run on each lookup.
- `unregister_renderer_builder(builder)` - Remove a previously registered builder.

### Data Types
- `ThingState` - Complete state of a thing
- `ThingDelta` - Incremental change to a thing. Uses `push_chunk` (data to append, or `NO_PUSHED_CHUNK`) and `pop_chunks` (number to pop, or `POP_ALL`). Operations:
  - No change: `push_chunk=NO_PUSHED_CHUNK, pop_chunks=0` (default)
  - Append: `push_chunk=data, pop_chunks=0`
  - Replace: `push_chunk=data, pop_chunks=POP_ALL`
  - Pop N then append: `push_chunk=data, pop_chunks=N`
  - Pop N (repoint): `push_chunk=NO_PUSHED_CHUNK, pop_chunks=N` (if chunks remain)
- `NO_PUSHED_CHUNK` - Sentinel indicating no chunk should be pushed
- `POP_ALL` - Sentinel indicating all chunks should be popped
- `ThingLink` - Link between two things
- `ThingRef` - Reference to an existing thing
- `AsyncThingUpdate[T]` - Async snapshot result with `immediate_update` (type T) and `async_updater` (returns StatefulResult). The async_updater's result appears in a subsequent streaming tick.
- `StatelessResult` - Type alias for `ThingState | Sequence[ThingState]`
- `StatefulResult` - Type alias for `ThingState | ThingDelta | Sequence[ThingState | ThingDelta]`
- `SnapshotResult` - Result of a tick (tick_number, timestamp, level, desc, states)
- `RendererBuilderResult` - Result from a renderer builder (tsx_source, imports)
- `RendererBuilder` - Type alias for builder functions: `(str) -> RendererBuilderResult | None`

### Watcher Class
- `Watcher(db_path, port, fxtr_support)` - Create a watcher
  - `db_path`: Path to SQLite file, or None for in-memory database
  - `port`: Port for HTTP server, or None to disable
  - `fxtr_support`: If True, enables `track_id()` and automatic fxtr entity tracking
- `watcher.watch_and_serve_until(done)` - Run watcher until coroutine completes
- `watcher.track_fxtr_id(fxtr_id, from_scope)` - Track an fxtr entity (requires fxtr_support=True)
- `watcher.set_tick_interceptor(fn)` - Set callback for testing
- `watcher.debug_receive(name, timeout)` - Wait for debug trigger (for testing)

## Thread Safety

The Watcher is thread-safe for core tracking operations:

**Thread-safe operations:**
- `track(obj, typename)` - can be called from any thread
- `untrack(obj)` - can be called from any thread
- `tick(level, desc)` - can be called from any thread (serialized via RLock)
- `streaming_update(obj)` - can be called from any thread (serialized via RLock)

**NOT thread-safe (call before multi-threaded use):**
- `register_snapshotter()` - configuration only
- `set_tick_interceptor()` - configuration only

**Behavior:**
- Uses `threading.RLock` for reentrant locking (safe if GC triggers during lock hold)
- Only one tick runs at a time per watcher
- Snapshotters and AnchorCapturers run while lock is held - user is responsible for their objects' thread safety
- `pause()` and `receive()` briefly acquire the lock to register/unregister the anchor, but do NOT hold it while awaiting
- `add_root()` and `remove_root()` acquire the lock to modify root state

## Running Tests

```bash
# From this directory:
uv run pytest
```

## Implementation Status

### Phase 1
- [x] Step 1: Core tracking + log output
- [x] Step 1.5: Thread safety
- [x] Step 2: SQLite persistence (with change detection)
- [x] Step 3: GraphQL backend + debug triggers
- [~] Step 4: MCP server (skipped - using graphqurl instead, see docs/querying_thingwatcher_as_an_agent.md)
- [x] Step 5: Pause/trigger mechanism
- [x] Step 5.2: GraphQL API cleanup (98 tests passing)
- [~] Step 6: Frontend UI (implemented, needs end-to-end testing)

### Phase 2
- [x] Step 1: dockview integration (frontend)
- [x] Step 2: Roots - Named entry points for exploring the state graph (backend + frontend)
- [x] Step 3: keyJSON support - Human-readable JSON view of CBOR keys (backend + frontend)
- [x] Step 4: Dynamic custom renderers - TSX renderers compiled in-browser via esbuild-wasm
- [x] Step 5: Backlinks - Show things pointing to the selected thing in DetailsPanel
- [x] Step 4.1: Multi-chunk data in renderers - graphcache for chunk caching, dataChunks prop, ThingDelta append support
- [x] Step 6: Snapshot ownership - StatelessSnapshotter and StatefulOwningSnapshotter ABCs with ownership tracking maps
- [x] Step 7: Streaming subticks - `streaming_update()` for updating individual objects without full ticks; dual counter system (tick + seq) for full history; GraphQL subscription parameter for streaming updates
- [x] Step 8: Single-thing embed view - `/embed` route for embedding in VS Code webviews; receives thing key + args via postMessage; always shows latest tick with streaming; `/debug-embed` for testing
- [x] Step 9: Persistent renderer state - `usePersistentThingState` hook for custom renderers; state persists across re-mounts within session; same thing in multiple panels shares state
- [x] Step 11 Part 1: Drag-to-panel - Drag DraggableIDMarker or NodeRef onto dockview to create new panel; respects drop position
- [x] Step 11 Part 2: InlineNodeRef - Block vs inline references; NodeRef (block, fills width) and InlineNodeRef (inline, minimal); InlineRefRenderer export for custom renderers
- [x] Step 11 Part 3: Hover preview cards - Preview card on hover over NodeRef/InlineNodeRef (500ms delay); shows PreviewRenderer or fallback; backlinks hover on DraggableIDMarker; nested hovers supported; close on navigation
- [x] Step 11 Part 4: Panel sidebar - Collapsible sidebar showing all dockview groups and panels; thing panels show RefRenderer with hover preview; click to focus, X to close; resizable; toggle in header
- [x] Step 12: Hard recapture and post-trigger ticks - `hardRecapture` mutation to reset all state; `stateless_snapshot()` for StatefulOwningSnapshotter; post-trigger tick automatically after pause triggers; Alt/Option+click for hard recapture in UI; orange timeline styling for hard_recapture ticks
- [x] Step 13: Pop chunks for ThingDelta - New `push_chunk`/`pop_chunks` interface replaces old `data_diff_kind`/`new_data`. Enables popping recent chunks before appending, for rolling windows, undo/redo, compaction. Validation errors (POP_ALL without push, negative pop_chunks, pop more than chain length) produce broken things rather than raising.
- [x] Step 14: Multi-file bundling and shadow DOM - Renderers can import additional TSX/CSS modules; CSS modules compiled with scoped class names and attached via adoptedStyleSheets; all custom renderers wrapped in shadow DOM for style isolation; NodeRef/ChildNodeView slotted to light DOM; strict internal/external component separation (ThingRenderLib vs ThingWatcherBindingsExternal)
- [x] Step 15: Custom renderer handle system - Refactored renderer API to use opaque handles + hooks pattern; renderers receive handle and use hooks (usePayload, useLinks, useTriggerable, useTrigger, useDraggableIdMarker, usePersistentThingState) to access data; dialect version export required (`thingwatcherDialect = "0.1"`); removed ThingContext
- [x] Step 18post3: Module-level state and Twind - `usePersistentGlobalState` hook for module-scoped state shared across all things in a renderer module; updater function support for both `usePersistentThingState` and `usePersistentGlobalState`; `AdoptStyleSheet` component for declaratively adopting stylesheets into shadow DOM; TwindDemo example showing Tailwind CSS support via twind's observe mode with shadow DOM
- [x] Step 18post4: Reload renderers from disk - `register_renderer()` now accepts `pathlib.Path` objects for both main source and imports; files are read lazily with mtime-based change detection; frontend refresh button (↻) in Timeline and EmbedApp invalidates rendering config cache to pick up changed files; `RendererRefreshProvider` context enables refresh propagation to hooks
- [x] Step 20: Drag and drop with custom drop regions - `useThingDropRegion` hook for custom renderers to create drop zones; `ThingDropRegion` component suppresses dockview drop handling; multi-mime-type drag data for filtering by type or specific key; `extractDroppedThing` helper; demo with fruits and filterable drop regions
- [x] Step 21: Immutable object stores - Backend support for `ImmutableStore` protocol allowing external data sources; frontend contexts (`ActiveThingsContext`, `PinnedThingsContext`) for tracking active/pinned things; source info display in ThingPanelContent showing where thing data came from; pinned section in ThingsList
- [x] Step 22: Breadcrumbs - Navigation history tracking between things. When navigating from A to B, a breadcrumb is recorded on B pointing to A. As long as B is active, A stays active via the breadcrumb. Features: BreadcrumbSourceContext for tracking navigation chains, breadcrumb accumulation through hover previews and renderers, BreadcrumbCard UI for viewing/removing breadcrumbs, stable vs transient thing classification (transient things shown with reduced opacity in graph), cycle prevention for opposite-direction breadcrumbs, drag-to-panel breadcrumb integration
- [x] Step 23: TypeRegistry refactor + dynamic renderer builders - Refactored registry.py to use `TypeRegistry` class encapsulating all type-related config; each Watcher has local registry with local-first lookup; added `register_renderer_builder(fn)` for generating renderers dynamically; builders run on each lookup for unrecognized types; `DynamicDateThing` demo shows timestamp-in-renderer that changes on refresh
- [x] Step 24: Async owning snapshotters - `AsyncThingUpdate[T]` return type for snapshotters to perform async work; `immediate_update` appears at current tick, `async_updater` result appears in streaming tick; auto-cancellation on re-snapshot (tick, streaming_update, hard_recapture, untrack); generic over immediate type (StatelessResult for stateless_snapshot, StatefulResult for snapshot_owned); async_updater can always return StatefulResult
