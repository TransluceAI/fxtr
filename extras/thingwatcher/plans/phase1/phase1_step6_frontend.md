# Step 6: Frontend UI

## Goal

Build an interactive web visualization for exploring tracked objects over time.

## Design Decisions

Decisions made during planning:

- **Build system**: esbuild, following the pattern in `fxtr2/gql_spike`. Python controls compilation, caches result, recompiles on request in dev mode.
- **Styling**: CSS Modules via `.module.css` files (native esbuild support)
- **GraphQL client**: urql with subscriptions
- **UI components**: base-ui for tooltips, pickers, etc.
- **Graph library**: Cytoscape.js with fcose layout
- **Panel layout**: dockview for resizable panel system
- **Trigger UI**: Always show payload input (button + textarea) for triggerable things, in details panel
- **Data display**: Collapsible JSON tree viewer
- **Node labels**: Show shortId as label, full type+key in tooltip

---

## Architecture Overview

```
thingwatcher/
├── frontend/
│   ├── LOCAL_CODE_OVERVIEW.md
│   ├── package.json
│   ├── index.html              # Entry HTML with root div
│   ├── main.tsx                # Entry point, sets up urql client + renders App
│   ├── App.tsx                 # Main layout: graph + panels + timeline
│   ├── App.module.css
│   ├── components/
│   │   ├── Timeline/
│   │   │   ├── Timeline.tsx          # Bottom timeline scrubber
│   │   │   └── Timeline.module.css
│   │   ├── Graph/
│   │   │   ├── Graph.tsx             # Cytoscape.js wrapper
│   │   │   └── Graph.module.css
│   │   ├── DetailsPanel/
│   │   │   ├── DetailsPanel.tsx      # Selected thing details + trigger UI
│   │   │   ├── DetailsPanel.module.css
│   │   │   └── JsonTree.tsx          # Collapsible JSON viewer
│   │   ├── ThingsList/
│   │   │   ├── ThingsList.tsx        # List of things with filters
│   │   │   └── ThingsList.module.css
│   │   └── common/
│   │       └── Tooltip.tsx           # base-ui tooltip wrapper
│   ├── hooks/
│   │   ├── useGraphQL.ts             # urql client setup
│   │   ├── useTicks.ts               # Tick navigation state
│   │   └── useKeyboardShortcuts.ts   # Keyboard navigation
│   ├── graphql/
│   │   └── queries.ts                # GraphQL query/mutation/subscription definitions
│   └── types/
│       └── index.ts                  # TypeScript types for GraphQL data
└── server.py                   # Add frontend serving routes
```

---

## Component Specifications

### 1. Timeline (bottom)

**Features:**
- Scrubber/slider showing tick indices
- Step forward/back buttons
- Level filter dropdown (show: all, info, debug, etc.)
- "Live" toggle - when enabled, auto-advances to new ticks
- Current tick info display: index, level, timestamp, description

**Data:**
```graphql
query GetTicks($levels: [String!]) {
  ticks(levels: $levels) {
    index
    timestamp
    level
    desc
  }
}

subscription OnNewTick {
  newTick {
    index
    timestamp
    level
    desc
  }
}
```

**Keyboard shortcuts:**
- `←` / `→`: Step back/forward one tick
- `Home` / `End`: Go to first/latest tick
- `L`: Toggle live mode

---

### 2. Graph (main area)

**Features:**
- Cytoscape.js canvas with fcose layout
- Nodes: colored circles based on `thingType` (from `renderingConfig`)
- Node labels: shortId number (small, centered)
- Edges: links between things, colored by link type
- Node states indicated by border:
  - New (ADDED): green border
  - Modified: yellow border
  - Removed: red dashed border, semi-transparent
  - Normal: no special border
- Hover: tooltip showing `thingType: key` (full, decoded key)
- Click: select node, show in details panel
- Re-layout when node set changes significantly

**Data:**
```graphql
query GetTickData($tick: Int!, $prevTick: Int!) {
  tick(index: $tick) {
    things {
      shortId
      key
      thingType
      triggerable
      links {
        linkType
        linkKey
        toThing { shortId }
      }
    }
    changes(since: $prevTick) {
      kind
      thing { shortId }
    }
  }
  renderingConfig {
    thingTypes {
      thingType
      color
      linkColors { linkType color }
    }
  }
}
```

**Cytoscape config:**
```javascript
{
  layout: {
    name: 'fcose',
    animate: true,
    animationDuration: 300,
    nodeRepulsion: 4500,
    idealEdgeLength: 50,
  },
  style: [
    { selector: 'node', style: { 'background-color': 'data(color)', 'label': 'data(shortId)' } },
    { selector: 'node.added', style: { 'border-color': '#22c55e', 'border-width': 3 } },
    { selector: 'node.modified', style: { 'border-color': '#eab308', 'border-width': 3 } },
    { selector: 'node.removed', style: { 'border-color': '#ef4444', 'border-style': 'dashed', 'opacity': 0.5 } },
    { selector: 'edge', style: { 'line-color': 'data(color)', 'target-arrow-color': 'data(color)' } },
  ]
}
```

---

### 3. Details Panel (right, top)

**Features:**
- Shows selected thing's data
- Header: `thingType` badge, shortId, full key
- Body: collapsible JSON tree for `data`
- Links section: list of outgoing links
- Trigger section (if `triggerable`):
  - "Trigger" button
  - Payload textarea (JSON)
  - Calls `triggerPause` mutation on click

**JSON Tree component:**
- Expand/collapse nested objects/arrays
- Syntax highlighting for values (string=green, number=blue, etc.)
- Copy value on click (optional)

**Data:**
```graphql
query GetThingDetails($shortId: Int!, $tick: Int!) {
  thing(shortId: $shortId, atTick: $tick) {
    shortId
    key
    thingType
    triggerable
    data
    links {
      linkType
      linkKey
      toThing { shortId thingType }
    }
  }
}

mutation TriggerPause($shortId: Int!, $payload: JSON) {
  triggerPause(shortId: $shortId, payload: $payload) {
    ok
    error
  }
}
```

---

### 4. Things List (right, bottom)

**Features:**
- Scrollable list of all things at current tick
- Each row: shortId, thingType badge (colored), triggerable indicator
- Click to select (same as clicking in graph)
- Filter options:
  - Text search (matches key or type)
  - "Changes only" checkbox (show only ADDED/MODIFIED/REMOVED)
  - "Triggerable only" checkbox
- Sort options: by shortId, by type, by last modified

**Data:** Reuse tick data from Graph component (shared state)

---

### 5. Panel Layout (dockview)

**Original plan:**
- Main area: Graph (not closeable)
- Right dock:
  - Top: DetailsPanel
  - Bottom: ThingsList
- Bottom dock: Timeline (not closeable)
- Panels resizable via drag
- Layout persisted to localStorage

**Implementation note:** For v1, we used CSS flexbox instead of dockview. Rationale:
- The fixed layout (graph + sidebar + timeline) matches the spec without needing drag-to-reorder
- Keeps bundle size smaller (~200KB savings)
- Simpler styling integration with our dark theme
- dockview can be added later if resizable/detachable panels become important

See "Future Improvements" for dockview migration path.

---

## State Management

Use React context + urql cache for state:

```typescript
// AppContext.tsx
interface AppState {
  currentTick: number | 'live';
  selectedThingId: number | null;
  levelFilter: string[];
  liveMode: boolean;
}
```

**urql setup:**
- HTTP endpoint: `/graphql/`
- WebSocket endpoint: `ws://host/graphql/` for subscriptions
- Use `graphql-ws` protocol (matching server's GraphQLTransportWSHandler)

---

## Server Integration

Modify `server.py` to serve frontend:

```python
class FrontendHandler:
    """Serves compiled frontend assets."""

    def __init__(self, dev_mode: bool = False):
        self.dev_mode = dev_mode
        self.compiled_js: str | None = None
        self.compiled_css: str | None = None
        self.frontend_dir = Path(__file__).parent / "frontend"

    def _compile(self) -> None:
        """Compile TypeScript and CSS using esbuild."""
        # Run pnpm install if needed
        subprocess.run(["pnpm", "install"], cwd=self.frontend_dir, check=True)

        # Compile main.tsx -> main.js (includes CSS modules)
        result = subprocess.run(
            ["pnpm", "exec", "esbuild", "main.tsx",
             "--bundle", "--format=esm",
             "--loader:.tsx=tsx", "--loader:.ts=ts",
             "--outfile=/dev/stdout"],
            cwd=self.frontend_dir,
            capture_output=True, text=True, check=True
        )
        self.compiled_js = result.stdout

        # CSS is bundled automatically by esbuild

# Routes to add:
# GET /           -> serve index.html
# GET /main.js    -> serve compiled JS
# GET /main.css   -> serve compiled CSS (if separate)
```

---

## Implementation Steps

### Phase A: Foundation
- [x] 1. Create `frontend/` directory structure. In it, create a LOCAL_CODE_OVERVIEW.md containing a code overview of just the frontend.
- [x] 2. Set up `package.json` with dependencies
- [x] 3. Create basic `index.html` and `main.tsx`
- [x] 4. Add frontend serving routes to `server.py`
- [x] 5. Set up urql client with HTTP and WebSocket
- [x] 5.5 Update LOCAL_CODE_OVERVIEW.md

### Phase B: Core Components
- [x] 6. Implement Timeline component (scrubber, level filter)
- [x] 7. Implement Graph component with Cytoscape.js
- [x] 8. Implement DetailsPanel with JSON tree
- [x] 9. Implement ThingsList with filters
- [x] 9.5 Update LOCAL_CODE_OVERVIEW.md

### Phase C: Layout & Integration
- [x] 10. Set up dockview panel layout (used CSS flexbox instead - simpler for now)
- [x] 11. Wire up component communication (selection, navigation)
- [x] 12. Add keyboard shortcuts
- [x] 12.5 Update LOCAL_CODE_OVERVIEW.md

### Phase D: Polish
- [x] 13. Add node state styling (new/modified/removed)
- [x] 14. Add trigger UI in details panel
- [x] 15. Add live mode subscription handling
- [ ] 16. Test with demo_server.py
- [x] 17. Update LOCAL_CODE_OVERVIEW.md
- [ ] 18. Update root CODE_OVERVIEW.md

---

## Dependencies

```json
{
  "dependencies": {
    "react": "^19.2.3",
    "react-dom": "^19.2.3",
    "@urql/core": "^6.0.1",
    "urql": "^5.0.1",
    "graphql": "^16.12.0",
    "graphql-ws": "^6.0.6",
    "cytoscape": "^3.33.1",
    "cytoscape-fcose": "^2.2.0",
    "dockview": "^4.13.1",
    "@base-ui-components/react": "^1.0.0-rc.0"
  },
  "devDependencies": {
    "esbuild": "^0.27.2",
    "typescript": "^5.9.3",
    "@types/react": "^19.2.7",
    "@types/react-dom": "^19.2.3",
    "@types/cytoscape": "^3.31.0"
  }
}
```

---

## Open Questions / Future Improvements

1. **Vite migration**: Could improve DX with HMR. Would require separate dev server or proxy setup.
2. **Custom type renderers**: Eventually, `thingType` could have custom detail panel renderers.
3. **Pause vs Receive distinction**: Could add `expectsPayload` field to schema.
4. **Graph performance**: For very large graphs (100+ nodes), may need virtualization or level-of-detail.
5. **Theme support**: Light/dark mode toggle.
6. **History view**: Show thing state over time in details panel.
7. **dockview migration**: If resizable/detachable panels become important:
   - Install dockview (already in package.json but unused)
   - Wrap sidebar in `DockviewReact` with panel definitions
   - Add layout persistence to localStorage
   - Style panels to match dark theme
8. **base-ui components**: Currently unused. Could enhance tooltips, dropdowns, etc.

---

## Current Implementation Status

**As of: Step 6 initial implementation**

### What's Working
- **Full UI layout**: Header, graph area, sidebar (details + things list), timeline
- **Graph visualization**: Cytoscape.js with fcose layout, node coloring by type, change state borders
- **Timeline navigation**: Slider, step buttons, live mode toggle, level badges
- **Details panel**: JSON tree viewer, trigger form with payload input
- **Things list**: Search, filter by changes/triggerable, sort options
- **Keyboard shortcuts**: Arrow keys for stepping, L for live mode, Escape to deselect
- **Live subscriptions**: newTick subscription triggers data refetch
- **Server integration**: FrontendHandler compiles and serves JS/CSS

### What Needs Testing
- [ ] End-to-end test with demo_server.py
- [ ] Verify trigger functionality works
- [ ] Test with larger datasets (10+ things, many ticks)
- [ ] WebSocket reconnection behavior

### Known Limitations
- No panel resizing (fixed CSS layout)
- Level filter checkboxes present but not wired up
- No layout persistence
- Bundle is large (~2.4MB, mostly cytoscape)

### Next Steps
1. Test with demo_server.py to verify everything works end-to-end
2. Update root CODE_OVERVIEW.md
3. Consider: level filter implementation, bundle size optimization
