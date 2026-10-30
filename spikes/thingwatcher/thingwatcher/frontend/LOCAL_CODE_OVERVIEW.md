# Thingwatcher Frontend

React-based web UI for visualizing tracked objects over time.

## Directory Structure

```
frontend/
├── LOCAL_CODE_OVERVIEW.md   # This file
├── package.json             # Dependencies and scripts
├── tsconfig.json            # TypeScript configuration
├── index.html               # Main app entry HTML
├── main.tsx                 # Main app entry: creates urql client, imports dockview CSS
├── App.tsx                  # Main layout: header + DockviewLayout + Timeline
├── App.module.css           # App-level styles (dark theme)
├── embed.html               # Embed view entry HTML (minimal, for VS Code webviews)
├── embed.tsx                # Embed view entry: creates urql client for EmbedApp
├── EmbedApp.tsx             # Single-thing viewer with postMessage handling
├── debug-embed.html         # Debug UI entry HTML
├── debug-embed.tsx          # Debug UI entry: renders DebugEmbedApp
├── DebugEmbedApp.tsx        # Debug UI with iframe + controls for testing embed
├── dist/                    # Build output (generated)
│   ├── main.js              # Main app bundle
│   ├── main.css             # Main app CSS
│   ├── embed.js             # Embed view bundle
│   ├── embed.css            # Embed view CSS
│   ├── debug-embed.js       # Debug UI bundle
│   ├── debug-embed.css      # Debug UI CSS
│   └── thingwatcher-bindings.js  # SDK bundle for dynamic renderers
├── lib/
│   ├── ThingRenderLib.tsx              # Internal rendering components (NodeRef, ChildNodeView, Port) for app code
│   ├── ThingRenderLib.module.css       # Styles for internal components (bundled into app CSS)
│   ├── ThingWatcherBindingsExternal.tsx # SDK for custom renderers (wraps ThingRenderLib in ShadowSlot)
│   ├── handleFactory.ts                # Creates opaque handles for custom renderers (Full, Ref, Preview)
│   ├── rendererHooks.ts                # Hooks for custom renderers (usePayload, useLinks, etc.)
│   ├── Registry.tsx                    # Generic subscription-based registry (useSyncExternalStore pattern)
│   ├── ShadowDOM.tsx                   # Shadow DOM components (ShadowRootDiv, ShadowSlot)
│   ├── ThingUIEventsContext.tsx        # Internal context for UI events (click, open in panel)
│   ├── LowLevelNodeView.tsx            # Internal component for rendering a thing with FullRenderer
│   ├── DraggableIDMarker.tsx           # Wrapper for draggable thing IDs (drop on dockview to create panel)
│   ├── HoverContext.tsx                # Context for hover preview cards (close-on-navigation, nested hovers)
│   ├── HoverPreview.tsx                # HoverPreviewCard (entity preview) and BacklinksHoverCard components
│   ├── HoverPreview.module.css         # Styles for hover preview cards
│   ├── dragConstants.ts                # MIME types, ThingDropInfo, and helpers for drag-drop operations
│   ├── ThingDropRegionContext.tsx      # Context for managing ThingDropRegion suppression of dockview drops
│   ├── thingDropRegion.tsx             # useThingDropRegion hook for custom renderer drop zones
│   ├── persistentStateStore.ts         # In-memory store for persistent renderer state
│   └── LinkEdgeConfig.tsx              # LinkEdgeConfig component (declarative edge configuration)
├── components/
│   ├── DockviewLayout/      # Dockview panel management
│   │   ├── index.ts
│   │   ├── DockviewContext.tsx       # Dockview API context + openThingPanel/openThingPanelWithPosition helpers
│   │   ├── DockviewLayout.tsx        # Main dockview container + sidebar + ThingUIEventsProvider + drag-drop handling
│   │   ├── DockviewLayout.module.css
│   │   ├── usePanelLayout.ts         # Hook for tracking dockview panel layout (for sidebar)
│   │   ├── GraphPanelWrapper.tsx     # Wraps Graph for dockview
│   │   ├── DetailsPanelWrapper.tsx   # Wraps DetailsPanel for dockview
│   │   ├── ThingsListPanelWrapper.tsx # Wraps ThingsList for dockview
│   │   ├── RootsPanelWrapper.tsx     # Wraps RootsPanel for dockview
│   │   └── ThingViewPanel.tsx        # Standalone panel for specific thing
│   ├── Sidebar/             # Panel sidebar (left of dockview)
│   │   ├── index.ts
│   │   ├── ResizableSidebar.tsx      # Resizable container with drag handle
│   │   ├── ResizableSidebar.module.css
│   │   ├── PanelSidebar.tsx          # Panel list with groups, thing RefRenderers, hover preview
│   │   └── PanelSidebar.module.css
│   ├── DynamicRenderer/     # Dynamic TSX compilation
│   │   ├── index.ts
│   │   ├── DynamicRenderer.tsx       # DynamicFullRenderer and DynamicRefRenderer components
│   │   ├── DynamicRenderer.module.css
│   │   ├── FallbackRenderers.tsx     # Fallback Full and Ref renderers
│   │   ├── esbuildSetup.ts           # esbuild-wasm initialization + plugins
│   │   └── rendererCache.ts          # Cache compiled components by thingType
│   ├── ThingPanelContent/   # Shared panel content with chrome
│   │   ├── index.ts
│   │   ├── ThingPanelContent.tsx     # Meta header + LowLevelNodeView + backlinks + breadcrumbs
│   │   ├── ThingPanelContent.module.css
│   │   ├── BreadcrumbCard.tsx        # Collapsible card showing breadcrumbs pointing to this thing
│   │   └── BreadcrumbCard.module.css
│   ├── ThingContextMenu/    # Context menu for opening things in panels
│   │   ├── index.ts
│   │   ├── ThingContextMenu.tsx
│   │   └── ThingContextMenu.module.css
│   ├── Timeline/            # Bottom timeline scrubber
│   │   ├── index.ts
│   │   ├── Timeline.tsx
│   │   └── Timeline.module.css
│   ├── Graph/               # React Flow graph visualization with cola.js layout
│   │   ├── index.ts
│   │   ├── Graph.tsx                    # React Flow graph with selection, change state, edge configs
│   │   ├── Graph.module.css
│   │   ├── ThingNode.tsx                # Wrapper node with styling (delegates to DynamicGraphNodeRenderer)
│   │   ├── DynamicGraphNodeRenderer.tsx # Loads custom graph renderers or fallback
│   │   ├── FallbackGraphNode.tsx        # Default node (colored square with shortId)
│   │   ├── GraphEdgeConfigContext.tsx   # Context for port/edge config registration
│   │   ├── useColaLayout.ts             # Cola.js force-directed layout hook
│   │   └── ColaLayoutManager.ts         # Cola.js layout state manager
│   ├── DetailsPanel/        # Selected thing details + trigger UI
│   │   ├── index.ts
│   │   ├── DetailsPanel.tsx
│   │   ├── DetailsPanel.module.css
│   │   └── JsonTree.tsx     # Collapsible JSON viewer
│   ├── ThingsList/          # List of things with filters
│   │   ├── index.ts
│   │   ├── ThingsList.tsx
│   │   └── ThingsList.module.css
│   ├── RootsPanel/          # Named root entry points
│   │   ├── index.ts
│   │   ├── RootsPanel.tsx
│   │   └── RootsPanel.module.css
│   └── common/              # Shared components (not yet used)
├── hooks/
│   ├── index.ts              # Re-exports all hooks
│   ├── useGraphQL.ts         # urql client factory with HTTP + WebSocket + graphcache
│   ├── ThingWatcherProvider.tsx  # Central tick state + subscription + triggerableKeys
│   ├── useThing.ts           # Single thing by key (with optional includeData)
│   ├── ticks.ts              # useLatestTick, useTicks, useTickData
│   ├── useTrigger.ts         # useTriggerThing, useTriggerTick
│   ├── useTriggerRecords.ts  # useTriggerRecords, useTriggerRecordsAtTick
│   ├── useRenderingConfig.ts # useThingTypeConfig, useColorMapsForGraphOnly
│   ├── useRendererRefresh.tsx # RendererRefreshProvider, useRendererRefreshTrigger, useOnRendererRefresh
│   ├── useActiveMarks.tsx    # ActiveMarksProvider + registry-based active thing tracking + stability classification
│   ├── usePinnedThings.tsx   # PinnedThingsProvider + usePinnedThings (pins + breadcrumbs)
│   ├── useBreadcrumbSource.tsx # BreadcrumbSourceContext for tracking navigation chains
│   └── useAdditionalActiveThings.ts  # Batch-fetches active things not in snapshot (for Graph/ThingsList)
├── graphql/
│   └── queries.ts           # Shared GraphQL queries (hooks import from here)
└── types/
    ├── index.ts             # TypeScript types for GraphQL data
    └── css.d.ts             # CSS module type declarations
```

## Build System

Uses esbuild, compiled from Python server on startup (dev mode: recompile per request).

- CSS Modules via `.module.css` files (native esbuild support)
- TypeScript/TSX compilation
- Multiple entry points compiled separately:
  - `main.tsx` -> `/main.js`, `/main.css` (full app)
  - `embed.tsx` -> `/embed.js`, `/embed.css` (single-thing embed view)
  - `debug-embed.tsx` -> `/debug-embed.js`, `/debug-embed.css` (embed debug UI)
  - `lib/ThingWatcherBindingsExternal.tsx` -> `/thingwatcher-bindings.js` (SDK for dynamic renderers)

### Import Maps

HTML entry points include import maps for external package resolution:
```html
<script type="importmap">
{
  "imports": {
    "react": "https://esm.sh/react@19",
    "react/": "https://esm.sh/react@19/",
    "react-dom": "https://esm.sh/react-dom@19",
    "react-dom/": "https://esm.sh/react-dom@19/",
    "thingwatcher-bindings": "/thingwatcher-bindings.js"
  }
}
</script>
```

These are used by external packages from esm.sh that have React externalized (see DynamicRenderer section).

## Key Libraries

- **React 19**: UI framework
- **urql + graphql-ws**: GraphQL client with subscriptions
- **@urql/exchange-graphcache**: Normalized entity caching (DataChunk entities cached by ID for efficient multi-chunk loading)
- **@xyflow/react + webcola**: Graph visualization (React Flow) with force-directed layout (cola.js)
- **dockview**: Resizable, rearrangeable panel layout
- **@base-ui/react**: Context menu component
- **esbuild-wasm**: In-browser TypeScript/TSX compilation for custom renderers

## State Management

- **urql + graphcache**: GraphQL data fetching, caching, and subscription updates
- **React context (ThingWatcherProvider)**: Central tick state, triggerableKeys, subscription
- **React context (AppContext)**: UI state (selected thing, level filter)
- **React context (DockviewPropsContext)**: Props passed to panel wrapper components
- **React context (DockviewApiContext)**: Dockview API for opening panels (used by ThingContextMenu)
- **localStorage**: Panel layout persistence (via dockview serialization)

## Hooks Architecture

The hooks system provides a clean interface for data access:

- **ThingWatcherProvider**: Central context with tick state and subscription
  - `useThingWatcher()`: Get `resolvedTick`, `isLive`, `isSubscriptionActive`, `triggerableKeys`, `setCurrentTick`
- **Data hooks**: Components use hooks, never direct `useQuery` calls
  - `useThing({ keyInput, includeData })`: Fetch thing by key
  - `useTickData({ tick, prevTick?, nextTick? })`: Get things/changeInfo/roots at a tick
    - Returns `changeInfo: { addedKeys, modifiedKeys, disappearingKeys }` (Sets for O(1) lookup)
    - `disappearingKeys` contains things that will be REMOVED in nextTick (so they can still be selected)
  - `useTicks()`: Get tick list for timeline
  - `useTriggerRecords()`: Get trigger history
  - `useThingTypeConfig(thingType)`: Get rendering config for type
- **Mutation hooks**:
  - `useTriggerThing(keyInput)`: Returns `[triggering, trigger]`
  - `useTriggerTick()`: Returns `[triggering, trigger]`

**Triggerable status pattern:**
```typescript
const { triggerableKeys, isLive } = useThingWatcher();
const isTriggerable = isLive && triggerableKeys.has(thing.key);
```

## Breadcrumbs System

Breadcrumbs track navigation history between things. When a user navigates from thing A to thing B (via a link or backlink), a breadcrumb is recorded on B pointing back to A. As long as B is active, A stays active via the breadcrumb.

**Key concepts:**
- **Breadcrumb**: A record that thing B was reached from thing A via a link/backlink
- **Stable vs Transient things**:
  - Stable: Things with stable marks (snapshot, pin, panel)
  - Transient: Things only kept active by breadcrumbs or selection
- **Breadcrumb chains**: Navigation accumulates pending breadcrumbs that are all created when user commits (clicks NodeRef)

**Hooks:**
- `useActiveKeys()`: Returns `Map<string, 'stable' | 'transient'>` of all active thing keys
- `useBreadcrumbsFor(thingKey)`: Returns array of breadcrumbs pointing to this thing
- `useBreadcrumbSource()`: Returns current breadcrumb context (pending breadcrumbs + current key)
- `usePinnedThings().addBreadcrumbs(pending)`: Creates breadcrumbs from pending chain
- `usePinnedThings().removeBreadcrumb(id)`: Removes a specific breadcrumb

**Components:**
- `BreadcrumbSourceProvider`: Context provider for breadcrumb chains
- `BreadcrumbCard`: Collapsible UI showing breadcrumbs pointing to a thing
- `BreadcrumbMark`: Internal component that registers active marks for breadcrumb sources

**Behavior:**
- Clicking a NodeRef creates breadcrumbs from the accumulated pending chain
- Dragging a NodeRef to create a panel creates breadcrumbs from the pending chain
- Graph nodes show reduced opacity (0.6) for transient things
- Opposite-direction breadcrumbs are prevented to avoid cycles (A→B followed by B→A)
- When a thing becomes inactive, all breadcrumbs TO that thing are automatically removed

## Components

### App.tsx
Main application shell orchestrating all components.

**Layout:**
- Header bar with title and tick info
- Main area: DockviewLayout (contains Graph, DetailsPanel, ThingsList panels)
- Footer: Timeline controls (fixed, outside dockview)

**AppContext state:**
- `selectedThingKey`: string | null - selected thing key (b64)
- `levelFilter`: string[] - which tick levels to show

**Tick state from ThingWatcherProvider:**
- `resolvedTick`, `latestTick`, `isLive`, `isSubscriptionActive`, `setCurrentTick`
- Subscription handled automatically by provider; `isSubscriptionActive` indicates websocket health

**Data via hooks:**
- `useTicks()`: Tick list for timeline
- `useTickData()`: Things/changes/roots at current tick
- `useTriggerRecords()`: Trigger history
- `useThing({ includeData: true })`: Selected thing details

**Keyboard shortcuts:**
- `←`/`→`: Step through ticks
- `Home`/`End`/`L`: Jump to first/latest tick
- `Escape`: Deselect current thing

### EmbedApp.tsx
Lightweight single-thing viewer designed for embedding in VS Code webviews or iframes.

**Architecture:**
- Wrapped in `ThingWatcherProvider` (in embed.tsx)
- Uses hooks: `useThingWatcher`, `useThing`, `useThingTypeConfig`, `useTriggerThing`
- No direct GraphQL queries - all data via hooks

**Features:**
- Receives thing key + args via `postMessage` from parent window
- Always shows latest tick with streaming updates (no timeline scrubbing)
- Full custom renderer support with DynamicRenderer
- Full trigger support for triggerable things
- Passes `args` from postMessage to custom renderer's props

**postMessage protocol:**
```typescript
// Send to /embed iframe to show a thing
{
  type: 'showThing',
  key?: string,      // base64-encoded CBOR key (one of key/keyJSON required)
  keyJSON?: unknown, // JSON-compatible key value
  args?: Record<string, unknown>, // Passed to renderer's args prop
}
```

**Routes:**
- `/embed` - HTML page
- `/embed.js` - Compiled JavaScript
- `/embed.css` - Compiled CSS

### DebugEmbedApp.tsx
Debug UI for testing the embed view.

**Features:**
- Embeds `/embed` in an iframe
- Form controls: key type selector, key input, args JSON textarea
- "Send to Embed" button sends postMessage to iframe
- Preset buttons for demo widgets (alpha, beta)
- Status display showing sent messages

**Routes:**
- `/debug-embed` - HTML page
- `/debug-embed.js` - Compiled JavaScript
- `/debug-embed.css` - Compiled CSS

### DockviewLayout
Main container for dockview panels. Manages:
- Panel registration (graph, details, thingsList, thingView components)
- Default layout: Graph left (60%), Details/ThingsList right split (40%)
- Layout persistence to localStorage
- Auto-reopening of main panels if closed
- Passing props to panels via `DockviewPropsContext` React context
- External drag-drop handling: accepts things dragged from DraggableIDMarker/NodeRef and creates new panels
- Panel sidebar integration (uses `usePanelLayout` hook and Sidebar components)

**Panel types:**
- `graph-main`: Graph panel (auto-reopens if closed)
- `details-main`: DetailsPanel showing selected thing (auto-reopens)
- `thingslist-main`: ThingsList panel (auto-reopens)
- `roots-main`: RootsPanel showing named entry points (auto-reopens)
- `thing-view-${key}`: User-opened panel via context menu (closeable, key is b64 identifier, reuses if exists)
- `thing-view-${key}-${timestamp}`: User-opened panel via drag-drop (closeable, always creates new panel)

### Sidebar (PanelSidebar + ResizableSidebar)
Collapsible sidebar to the left of dockview showing all open panels.

**Features:**
- Shows all dockview groups with their panels
- Thing panels display using RefRenderer (type-colored, compact) with hover preview
- Non-thing panels (Graph, Details, Things, Roots) show just their title
- Click a panel to focus it in dockview
- Close button (X) on each panel item
- Active/focused panel highlighted with blue left border
- Resizable (150-400px range) via drag handle
- Toggle button in app header to show/hide sidebar

**Architecture:**
- `usePanelLayout`: Hook that subscribes to dockview events and returns `GroupLayout[]`
- `ResizableSidebar`: Container with drag handle for resizing
- `PanelSidebar`: Main sidebar content rendering groups and panels
- `ThingPanelLabel`: Internal component that renders RefRenderer for thing panels

### GraphPanelWrapper / DetailsPanelWrapper / ThingsListPanelWrapper / RootsPanelWrapper
Thin wrappers that render their respective components inside dockview panels.
- Retrieve props via `useDockviewProps()` React context hook
- DetailsPanelWrapper computes `isTriggerable` for DetailsPanel

### ThingViewPanel
Standalone panel for viewing a specific thing, opened via context menu.
- Receives `key` from panel params (b64 identifier)
- Queries thing data independently (GET_THING_DETAILS by key)
- Tracks current tick from global props
- Computes `isTriggerable` from ThingWatcherProvider context
- Updates panel title dynamically after query (e.g., "#42 MyThingType")

### ThingContextMenu
Wrapper component using base-ui ContextMenu for right-click menus.
- Takes `thingKey` prop and wraps children
- Shows "Open in new panel" option on right-click
- Uses `useDockviewApiFromContext()` internally - no callbacks needed from parent
- Returns children unchanged if not in a dockview context
- Used by: DetailsPanel (links/backlinks), ThingsList (items), ThingLink

```tsx
// Usage example
<ThingContextMenu thingKey={thing.key}>
  <div className="item">...</div>
</ThingContextMenu>
```

### Timeline
Bottom bar for navigating through ticks.

**Features:**
- Custom tick track with individual tick markers positioned by percentage
- Tick markers colored by level (cyan=info, gray=debug, yellow=warn, red=error, purple dotted=stream)
- Stream ticks (from `streaming_update()`) shown with dotted border style to distinguish from checkpoint ticks
- Blue trigger dots below ticks where triggers occurred
- Range slider overlaid for smooth scrubbing
- Prev/Next step buttons (respect level filter)
- Live mode as distinct position at end of track with LIVE button
- LIVE badge shows red-outlined "OFFLINE" when websocket subscription is disconnected
- Current tick info display (level badge, timestamp, description)
- Level filter checkboxes - filters ticks from track, positions remaining ticks contiguously

**State:**
- Tracks drag state locally to prevent losing slider control during React updates
- Maps slider positions to filtered tick indices (no gaps when filtering)

### Graph
React Flow-based graph visualization with cola.js force-directed layout and custom graph renderers.

**Features:**
- Custom graph node renderers via `graph` field in `RendererSet`
- Fallback nodes colored by `thingType` (via `useThingTypeConfig` hook)
- Port components for custom connection points
- Declarative edge configuration via `LinkEdgeConfig` component
- Edges connect nodes with arrow markers, support custom ports/labels/styles
- Force-directed layout using cola.js (organic, settles naturally)
- Custom node sizes propagated to cola for overlap avoidance
- Position persistence: nodes keep positions when scrubbing through ticks
- Pan/zoom enabled, node dragging enabled with physics simulation
- Click node to select (white box-shadow), click background to deselect
- Change state visualization via colored borders:
  - Added: green solid border
  - Modified: yellow solid border
  - Disappearing: red dashed border
  - Added+Disappearing / Modified+Disappearing: purple dashed border
- Selection and change state can combine (box-shadow + border)
- Transient nodes (things only kept active by breadcrumbs or selection) shown with reduced opacity (0.6)
- Viewport tracking: auto-fits to show changed nodes when off-screen
- Max zoom clamped to 1.5

**Architecture:**
- `Graph.tsx`: Main component with ReactFlowProvider + GraphEdgeConfigProvider
- `ThingNode.tsx`: Wrapper that applies change state/selection styling, delegates to DynamicGraphNodeRenderer
- `DynamicGraphNodeRenderer.tsx`: Loads custom graph renderer or falls back to FallbackGraphNode
- `FallbackGraphNode.tsx`: Default node (colored square with shortId)
- `GraphEdgeConfigContext.tsx`: Context for port/edge config registration
- `useColaLayout.ts`: Cola.js force-directed layout hook with position persistence and node pinning
- `ColaLayoutManager.ts`: Cola.js layout state manager with dimension support

**Custom Graph Renderers:**
Custom renderers can define a `graph` field in their `RendererSet` to provide a custom node component:
```tsx
import { Port, PortPosition, LinkEdgeConfig, usePayload, useLinks, useThingMetadata } from 'thingwatcher-bindings';
import type { GraphNodeRendererProps } from 'thingwatcher-bindings';

function MyGraphNode({ handle }: GraphNodeRendererProps) {
  const payload = usePayload(handle);
  const links = useLinks(handle);
  const { shortId } = useThingMetadata(handle);

  return (
    <div style={{ padding: 8, background: '#1a3a3a', borderRadius: 6 }}>
      <Port type="target" position={PortPosition.Top} id="in" />
      <div>#{shortId}</div>
      <Port type="source" position={PortPosition.Bottom} id="out" />

      {/* Configure outgoing edges */}
      {links.map(link => (
        <LinkEdgeConfig
          key={link.linkKey}
          linkKey={link.linkKey}
          sourcePort="out"
          targetPort="in"
          label={link.linkType}
        />
      ))}
    </div>
  );
}
```

**Interactivity:**
- **Click node**: Select it (white box-shadow)
- **Normal drag**: Move node in graph (React Flow default)
- **Alt+drag**: Drag node to dockview panel to open (shows "Drag to open in panel" tooltip)
- **Hover**: Shows preview card (like NodeRef/InlineNodeRef)

### DetailsPanel
Thin wrapper for showing the currently selected thing.

**Props:**
- `thingKey`: Selected thing key (null if nothing selected)
- `onSelectThing`: Callback when clicking links
- `triggerRecords`: For trigger records UI
- `onTrigger`: Callback after trigger

**Architecture:**
- Shows "Details" header
- Renders ThingPanelContent for the actual content
- Handles no-selection case

### RootsPanel
List of named root entry points at the current tick.

**Features:**
- Displays root name, resolved thing's shortId, and thingType
- Click to select the resolved thing (updates selection context)
- Styled consistently with other panels

### ThingsList
Scrollable list of all things at current tick.

**Features:**
- Search filter (by type or key)
- "Changes only" checkbox
- "Triggerable only" checkbox
- Sort by: ID, Type, or Change status
- Click to select (syncs with Graph)
- Right-click to open in new panel (via ThingContextMenu wrapper)

**Badges:**
- Change badges: `+` (green) for added, `M` (yellow) for modified
- Disappearing badge: `-` (red) for things about to disappear (can show alongside + or M)
- Trigger badge: red `T` for triggerable things (grayed when not in live mode)
- Triggered badge: blue `T` for things triggered at current tick

**Visual indicators:**
- Left border color indicates change status:
  - Green: added
  - Yellow: modified
  - Red dashed: disappearing only
  - Purple dashed: added + disappearing (ephemeral) or modified + disappearing
- Sort by change groups things: added → added+disappearing → modified → modified+disappearing → disappearing → unchanged
- Items show shortId and thingType

### JsonTree
Recursive collapsible JSON viewer used in DetailsPanel.

**Features:**
- Expand/collapse objects and arrays
- Color-coded values (string, number, boolean, null)
- Auto-collapse at depth > 2
- Shows item/key count when collapsed

### DynamicRenderer
Compiles and renders custom TSX renderers using esbuild-wasm.

**Architecture:**
- `esbuildSetup.ts`: Initializes esbuild-wasm (lazy load from esm.sh), creates plugins for injected deps and virtual filesystem, validates dialect version, compiles module to `CompiledModule` with shared stylesheet + renderer map
- `rendererCache.ts`: Caches compiled modules by moduleId (content-addressed); provides `getOrCompileModule()` and `getRendererFromModule()` functions
- `DynamicRenderer.tsx`: DynamicFullRenderer, DynamicRefRenderer, DynamicInlineRefRenderer, and DynamicPreviewRenderer with ErrorBoundary + ShadowRootDiv; fetches renderer modules via GraphQL query
- `FallbackRenderers.tsx`: FallbackFullRenderer, FallbackRefRenderer, FallbackInlineRefRenderer, and FallbackPreviewRenderer (use handle + hooks pattern)

**Module Resolution (hybrid approach):**
Dynamic renderers can import external packages from esm.sh. The module resolution uses a hybrid approach:

1. **Virtual module injection** (react, react/jsx-runtime, thingwatcher-bindings):
   - Injected at compile time via `window.__THINGWATCHER_INJECTED__`
   - Shares the same React instance as the main app
   - Ensures custom renderers work correctly with app context

2. **External packages** (bare imports like `lodash`, `dayjs`, etc.):
   - Rewritten to esm.sh URLs with `?external=react,react/jsx-runtime`
   - React is externalized so esm.sh doesn't bundle its own copy
   - Externalized React resolves via browser import map (points to esm.sh)

This means there are two React instances at runtime:
- **Bundled React**: Used by main app, thingwatcher-bindings, and dynamic renderer code
- **esm.sh React**: Used by third-party packages from esm.sh

This is fine because external packages don't need to share React state with our app.

**Required exports:**
All renderer modules must have a default export of type `ThingWatcherRenderConfig`:
```tsx
import type { ThingWatcherRenderConfig } from 'thingwatcher-bindings';

export default {
  dialect: "0.1",
  renderers: {
    "MyType": { full: MyFull, ref: MyRef, inline: MyInline, preview: MyPreview },
    "OtherType": { full: OtherFull },  // Can have multiple types in one module
  }
} as ThingWatcherRenderConfig;
```
If a renderer variant (full/ref/inline/preview) is not provided, the corresponding fallback is used.

**Multi-file bundling and module registration:**
Renderers are bundles with main TSX + optional imports. Python registers via:
```python
# Single type
register_renderer("MyType", tsx_source)

# Multiple types in one module
register_renderer(["Container", "Item"], tsx_source)

# With imports
register_renderer("MyType", tsx_source, imports={
    "./utils.tsx": utils_source,
    "./styles.module.css": css_source,
})
```
Module IDs are content-addressed (hash of bundle contents), enabling deduplication.

**CSS Modules:**
- CSS imports are compiled using esbuild's `local-css` loader
- Class names are scoped to avoid conflicts
- Compiled CSS attached to shadow root via `adoptedStyleSheets`

**Shadow DOM isolation:**
- All custom renderers are wrapped in `ShadowRootDiv` for style isolation
- CSS from custom renderers doesn't leak into app styles and vice versa
- Components like `NodeRef` are "slotted" out to light DOM via `ShadowSlot` so they can inherit app styles and show hover previews

**Injected modules** (via `window.__THINGWATCHER_INJECTED__`):
- `react`: Same React instance as main app
- `react/jsx-runtime`: JSX transform runtime
- `thingwatcher-bindings`: SDK components (NodeRef, InlineNodeRef, ChildNodeView, hooks)

**External packages from esm.sh:**
Custom renderers can import any npm package (e.g., `import ReactJson from "react-json-view"`).
These are rewritten to esm.sh URLs with React externalized. The externalized React
resolves via browser import map to esm.sh's React build.

### ThingRenderLib + ThingWatcherBindingsExternal (lib)
Rendering components split into internal (app) and external (dynamic renderers) versions.

**ThingRenderLib.tsx:** Internal implementations used by app code. Does NOT wrap in ShadowSlot.
- `NodeRef`, `InlineNodeRef`, `ChildNodeView`
- Creates handles and passes them to DynamicRenderer components
- Used by: FallbackRenderers (via hooks), HoverPreview

**ThingWatcherBindingsExternal.tsx:** Wrapper for dynamic renderers. ALWAYS wraps in ShadowSlot.
- Exports hooks from `rendererHooks.ts`
- Wraps `NodeRef`, `InlineNodeRef`, `ChildNodeView` in `<ShadowSlot>...</ShadowSlot>`
- Exports graph components: `Port`, `PortPosition`, `LinkEdgeConfig`
- Injected via esbuild as `thingwatcher-bindings` module
- Never imported directly by app code

**handleFactory.ts:** Creates opaque handles for custom renderers.
- `createFullRendererHandle(thingKey, metadata)` - All hooks available
- `createRefRendererHandle(thingKey, metadata)` - No `useTrigger`, `useDraggableIdMarker`, `usePersistentThingState`
- `createPreviewRendererHandle(thingKey, metadata)` - No `useTriggerable`, `useTrigger`, `usePersistentThingState`
- Handles contain internal hook methods that are called by the exported hooks
- Brand symbols used for runtime type validation

**rendererHooks.ts:** Hooks for custom renderers (exported via ThingWatcherBindingsExternal).
- `usePayload(handle, { chunk?: 'first' | 'latest' })` - Get payload data
- `useDataChunks(handle)` - Get all data chunks
- `useLinks(handle)` - Get links from this thing
- `useThingMetadata(handle)` - Get key, shortId, thingType
- `useTriggerable(handle)` - Get [triggerable, triggerRecord] (Full + Ref handles)
- `useTrigger(handle)` - Get trigger function (Full handle only)
- `useDraggableIdMarker(handle)` - Get draggable marker component (Full + Preview handles)
- `usePersistentThingState(handle, key)` - Persistent state storage (Full handle only)

**thingDropRegion.ts:** `useThingDropRegion(handle)` hook for custom renderer drop zones.
- Returns `ThingDropRegion` component that suppresses dockview drop handling
- Returns `mimeTypeForAnyThing`, `mimeTypeForThingType(type)`, `mimeTypeForSpecificThing(key)` for filtering
- Returns `extractDroppedThing(dataTransfer)` to get dropped thing metadata
- Full handle only

**Important:** The type definitions in `thingwatcher/thingwatcher-bindings.d.ts` (used for Deno typechecking of custom renderers) must be kept in sync with ThingWatcherBindingsExternal exports.

**Renderer Exports (named, not default):**
All renderers must export `thingwatcherDialect = "0.1"` and named functions:
- `FullRenderer({ handle, args, isTopLevel })` - Full (card) view
- `RefRenderer({ handle, args })` - Block reference view
- `InlineRefRenderer({ handle, args })` - Inline reference view
- `PreviewRenderer({ handle })` - Hover preview (no args)

**Handle types and available hooks:**
| Handle Type | Hooks |
|-------------|-------|
| `FullRendererHandle` | All hooks |
| `RefRendererHandle` | usePayload, useDataChunks, useLinks, useThingMetadata, useTriggerable |
| `PreviewRendererHandle` | usePayload, useDataChunks, useLinks, useThingMetadata, useDraggableIdMarker |

**Renderer conventions:**
- `RefRenderer` must return a block element (`<div>`). It will fill its container width naturally via standard block layout.
- `InlineRefRenderer` must return an inline element (`<span>`). It will flow with surrounding text.
- `PreviewRenderer` should return a compact preview suitable for hover cards (max ~300px wide).

**Component Exports (public API for custom renderers):**
- `ChildNodeView`: Render another thing inline with its FullRenderer
- `NodeRef`: Block-level clickable ref using RefRenderer (block layout, fills width, shows hover preview)
- `InlineNodeRef`: Inline clickable ref using InlineRefRenderer (flows with text, minimal, shows hover preview)

**Hover Preview Behavior:**
- `NodeRef` and `InlineNodeRef` show a preview card on hover (500ms delay)
- Preview card uses the thing's `PreviewRenderer` (or fallback if not exported)
- Nested hovers supported: hovering a NodeRef inside a preview opens another preview
- Clicking a link inside a preview card closes all hovers and navigates
- `DraggableIDMarker` (in custom renderers) shows backlinks on hover

**Internal (not exported to renderers):**
- `ThingUIEventsContext`: UI events context (in `lib/ThingUIEventsContext.tsx`)

**Usage in custom renderers (all three exports):**
```tsx
import React from 'react';
import { NodeRef, InlineNodeRef } from 'thingwatcher-bindings';

export function FullRenderer({ payload, links, triggerable, trigger, DraggableIDMarker }) {
  return (
    <div>
      <DraggableIDMarker>
        <h3 style={{ cursor: 'grab' }}>{payload.title}</h3>
      </DraggableIDMarker>
      {/* NodeRef for block-level refs */}
      {links.map(link => (
        <NodeRef key={link.linkKey} thingKey={link.toThingKey} />
      ))}
      {/* InlineNodeRef for inline refs in text */}
      <p>Related to <InlineNodeRef thingKey={payload.relatedKey} /></p>
      {triggerable && (
        <button onClick={() => trigger({ action: 'approve' })}>
          Approve
        </button>
      )}
    </div>
  );
}

// Block ref - fills container width, shows preview info
export function RefRenderer({ payload }) {
  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 8,
      padding: '4px 8px',
      background: '#2a4a2a',
      borderRadius: 4,
    }}>
      <span style={{ width: 8, height: 8, background: '#4a9', borderRadius: 2 }} />
      <span style={{ color: '#4a9' }}>{payload.title}</span>
      <span style={{ color: '#888', flex: 1 }}>(preview info)</span>
    </div>
  );
}

// Inline ref - minimal, flows with text
export function InlineRefRenderer({ payload }) {
  return (
    <span style={{ color: '#4a9' }}>
      <span style={{
        display: 'inline-block',
        width: 6,
        height: 6,
        background: '#4a9',
        borderRadius: 2,
        marginRight: 4,
      }} />
      {payload.title}
    </span>
  );
}

// Preview for hover cards - compact read-only summary with draggable ID and links
export function PreviewRenderer({ payload, links, shortId, thingType, DraggableIDMarker }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <DraggableIDMarker>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'grab' }}>
          <span style={{ width: 8, height: 8, background: '#4a9', borderRadius: 2 }} />
          <span style={{ fontWeight: 'bold', color: '#4a9' }}>{payload.title}</span>
          <span style={{ color: '#888', fontSize: 11 }}>#{shortId}</span>
        </div>
      </DraggableIDMarker>
      {links.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginTop: 4 }}>
          {links.map((link, i) => (
            <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11 }}>
              <span style={{ color: '#888' }}>{link.linkType}:</span>
              <InlineNodeRef thingKey={link.toThingKey} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
```

**Usage for multi-chunk things (using dataChunks):**
```tsx
import React from 'react';

export function FullRenderer({ dataChunks, DraggableIDMarker }) {
  const allEntries = dataChunks.flatMap(chunk => chunk?.entries || []);

  return (
    <div>
      <DraggableIDMarker>
        <div style={{ cursor: 'grab' }}>{allEntries.length} entries</div>
      </DraggableIDMarker>
      {allEntries.map((entry, i) => (
        <div key={i}>{entry.message}</div>
      ))}
    </div>
  );
}

export function RefRenderer({ dataChunks }) {
  const count = dataChunks.flatMap(c => c?.entries || []).length;
  return <span>Log ({count} entries)</span>;
}
```

**Usage with persistent state:**
```tsx
import React from 'react';
import { usePersistentThingState } from 'thingwatcher-bindings';

export function FullRenderer({ payload, DraggableIDMarker }) {
  const [expanded, setExpanded] = usePersistentThingState<Set<string>>('expanded');

  const toggleSection = (id: string) => {
    const next = new Set(expanded ?? []);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setExpanded(next.size > 0 ? next : undefined);
  };

  return (
    <div>
      <DraggableIDMarker>
        <span style={{ cursor: 'grab' }}>Sections</span>
      </DraggableIDMarker>
      {payload.sections.map((section) => (
        <div key={section.id}>
          <button onClick={() => toggleSection(section.id)}>
            {expanded?.has(section.id) ? '▼' : '▶'} {section.title}
          </button>
          {expanded?.has(section.id) && <div>{section.content}</div>}
        </div>
      ))}
    </div>
  );
}
```

### LowLevelNodeView (lib)
Internal component for rendering a thing with its FullRenderer.

**Responsibilities:**
- Fetches thing data via hooks
- Creates DraggableIDMarker bound to thing's key
- Gets renderer source for thing type
- Builds FullRendererProps
- Renders with DynamicFullRenderer (or FallbackFullRenderer)

**Props:**
- `thingKey`: Base64 thing key
- `isTopLevel`: True for panels, false for nested views
- `args`: Optional custom args for renderer

### ThingPanelContent (component)
Shared panel content with chrome, used by DetailsPanel, ThingViewPanel, and EmbedApp.

**Features:**
- BreadcrumbCard (collapsible card showing how user navigated to this thing)
- Source info header (where the thing came from + pin button)
- Meta header (type badge, shortId, key display)
- "Disappears next tick" badge when thing is disappearing
- LowLevelNodeView for the actual renderer (wrapped in link breadcrumb context)
- Backlinks section (wrapped in backlink breadcrumb context)
- Trigger records section

**Breadcrumb Contexts:**
- Renderer section uses `type: 'link'` context - clicking links creates link breadcrumbs
- Backlinks section uses `type: 'backlink'` context - clicking backlinks creates backlink breadcrumbs
- NodeRefs inside BreadcrumbCard use null context to prevent creating new breadcrumbs when navigating back

**Props:**
- `thingKey`: Base64 thing key
- `onSelectThing`: Navigation callback
- `triggerRecords`: Records at current tick
- `onTrigger`: Callback after trigger
- `isDisappearing`: Whether thing will disappear in next tick
- `markSource`: Source of the active mark for this panel (for tracking active things)
- `markLabel`: Optional label for debugging (e.g., panel ID)

### persistentStateStore.ts (lib)
In-memory store for renderer state that persists across component re-mounts.

- Global singleton store with pub/sub for reactivity
- State keyed by `${shortId}:${userKey}` - different things have separate state
- Setting value to `undefined` removes from storage (optimization)
- Multiple views of same thing share state via `useSyncExternalStore`

### ThingProvider
Context provider that wraps renderers to provide the current thing's shortId.

- Required for `usePersistentThingState` to work
- Automatically wrapped around DynamicRenderer in DetailsPanel, ThingViewPanel, EmbedApp
- Also wrapped around recursive ThingView renders
