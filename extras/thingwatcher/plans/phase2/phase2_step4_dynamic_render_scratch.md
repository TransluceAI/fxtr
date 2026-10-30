# Dynamic Render - Scratch Notes

**Note:** Step 3 (keyJSON) adds `keyJSON` and `linkKeyJSON` fields to the GraphQL API.
This step builds on that by including those fields in the SDK interface.

## Summary of Requirements

From user clarifications:
- **Trigger form**: SDK exposes a callback `trigger(payload: JSON | null)` - renderer writes their own form
- **Meta info**: Standard header stays (thingType, shortId, key) above custom renderer area
- **Child rendering**: Recursive - child things use their own custom renderers
- **Updates**: Re-render on tick change only
- **Link behavior**: Left=select, Right=context menu (same as existing)
- **Child args**: Fresh args only - parent explicitly passes to child
- **Python API**: TSX source as string

## ThingWatcherBindings API Design

What the custom renderer receives and can do:

```tsx
// Props passed to every custom renderer
interface ThingRendererProps {
  // The thing's data payload (decoded JSON from CBOR)
  payload: unknown;

  // The thing's links (includes keyJSON fields from step 3)
  links: Array<{
    linkType: string;
    linkKey: string;           // base64
    linkKeyJSON: unknown;      // decoded key or null
    toThingKey: string;        // base64
    toThingKeyJSON: unknown;   // decoded key or null
    toThingShortId: number;
    toThingType: string;
  }>;

  // Whether this thing is triggerable (paused, waiting for input)
  triggerable: boolean;

  // Trigger the thing with a payload (only works if triggerable)
  trigger: (payload: unknown | null) => Promise<{ ok: boolean; error?: string }>;

  // Freeform args passed by parent renderer (or empty object at top level)
  args: Record<string, unknown>;
}

// Components provided by ThingWatcherBindings
import { ThingView, ThingLink } from 'thingwatcher-bindings';

// Render another thing inline (uses that thing's custom renderer)
<ThingView
  thingKey={key}           // base64 key OR
  keyJSON={{ id: 1 }}      // JSON key (from step 3) OR
  shortId={123}            // shortId
  args={{ foo: 'bar' }}    // optional args to pass
/>

// Render a clickable badge that links to a thing
<ThingLink
  thingKey={key}           // base64 key OR
  keyJSON={{ id: 1 }}      // JSON key (from step 3) OR
  shortId={123}            // shortId
  label="My Link"          // optional custom label (default: #shortId)
/>
// Left-click selects, right-click shows context menu
```

## Data Flow

1. **GraphQL**: Need to serve renderer TSX source per thing_type
   - New query: `renderer(thingType: String!): String` (returns TSX source or null)
   - Or add to `ThingTypeConfig`: `renderer: String` field

2. **Frontend DynamicRenderer**:
   - Adapted from spike - uses esbuild-wasm to compile TSX in browser
   - Injects `thingwatcher-bindings` module with React, components, hooks
   - Caches compiled components by thingType (not per-thing)

3. **ThingWatcherBindings module**:
   - Provides `ThingView`, `ThingLink` components
   - Uses React context to access: urql client, current tick, selection handlers, dockview API
   - `ThingView` fetches thing data and recursively renders

4. **Integration into DetailsPanel / ThingViewPanel**:
   - Check if thing has a custom renderer (query renderer by thingType)
   - If yes: render with DynamicRenderer, passing props
   - If no: show existing JSON tree view (fallback)
   - Standard header (thingType, shortId, key) always shown above

## Python Side

```python
# In registry.py, add:
_renderers: dict[str, str] = {}  # thing_type -> TSX source

def register_renderer(thing_type: str, tsx_source: str) -> None:
    """Register a custom TSX renderer for a thing type."""
    _renderers[thing_type] = tsx_source

def get_renderer(thing_type: str) -> str | None:
    """Get the renderer TSX source for a thing type, or None."""
    return _renderers.get(thing_type)

def get_all_renderers() -> dict[str, str]:
    """Get all registered renderers."""
    return dict(_renderers)
```

GraphQL schema addition:
```graphql
type ThingTypeConfig {
  thingType: String!
  color: String!
  linkColors: [LinkColorConfig!]!
  renderer: String  # NEW: TSX source code, or null
}

# Or separate query:
extend type Query {
  renderer(thingType: String!): String
}
```

## Frontend File Structure

```
frontend/
├── lib/
│   └── ThingWatcherBindings.tsx    # The SDK library
├── components/
│   └── DynamicRenderer/
│       ├── index.ts
│       ├── DynamicRenderer.tsx     # Main component (adapted from spike)
│       ├── DynamicRenderer.module.css
│       └── esbuildSetup.ts         # esbuild init + injected modules
```

## Key Implementation Details

### esbuild-wasm Initialization
- Initialize once on first render
- Use esm.sh for wasm file
- Plugin to inject our modules (react, thingwatcher-bindings)

### Renderer Caching
- Cache compiled components by thingType
- Invalidate if TSX source changes (compare hash?)
- Store in module-level Map or React context

### ThingView Component
- Needs access to current tick to fetch thing data
- Fetches thing by key/shortId at current tick
- Looks up that thing's renderer
- Recursively renders (with depth limit to prevent infinite loops?)

### ThingLink Component
- Renders as a styled badge (like existing link styling)
- onClick: call selection handler from context
- onContextMenu: show context menu from context

### Context Needs
- urql client (for GraphQL queries)
- current tick number
- selection handler (shortId) => void
- context menu handler (event, shortId) => void
- dockview API (for opening panels)
- renderingConfig (for colors)

## Questions Resolved
- Trigger: callback only, no form component
- Args: explicit passing only, no inheritance
- Meta header: always shown, not part of renderer
- Child rendering: recursive with custom renderers
- Updates: on tick change only

## Implementation Order

1. Python: Add renderer registry + GraphQL endpoint
2. Frontend: Create ThingWatcherBindings library
3. Frontend: Create DynamicRenderer component (adapt from spike)
4. Frontend: Create context for bindings (RendererContext)
5. Frontend: Integrate into DetailsPanel/ThingViewPanel
6. Testing: Add example renderers, verify fallback works
7. Update CODE_OVERVIEW.md
