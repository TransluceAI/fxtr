# Phase 2 Step 4: Dynamic Custom Renderers

Custom TSX renderers for things, compiled in-browser via esbuild-wasm, with an SDK library ("ThingWatcherBindings") for rendering linked things and triggering.

**Prerequisite:** Step 3 (keyJSON) should be completed first, as this step uses keyJSON fields.

**Canonical example:** `examples/demo_pause_trigger.py` - add custom renderers here.

## Overview

Currently, thing details are shown as raw JSON. This step adds:
1. A Python API to register TSX renderer code per thing_type
2. GraphQL endpoint to serve renderer source
3. A frontend `DynamicRenderer` that compiles TSX in-browser
4. A `ThingWatcherBindings` SDK library that renderers import
5. Integration into DetailsPanel/ThingViewPanel with fallback to JSON view

## ThingWatcherBindings SDK API

Custom renderers import from `thingwatcher-bindings`:

```tsx
import React from 'react';
import { ThingView, ThingLink } from 'thingwatcher-bindings';

// Every renderer receives these props:
interface ThingRendererProps {
  payload: unknown;                    // Decoded thing data
  links: LinkInfo[];                   // Outgoing links
  triggerable: boolean;                // Can this thing be triggered?
  trigger: (payload: unknown | null) => Promise<TriggerResult>;
  args: Record<string, unknown>;       // Freeform args from parent
}

interface LinkInfo {
  linkType: string;
  linkKey: string;         // base64
  linkKeyJSON: unknown;    // decoded key (from step 3), null if not JSON-compatible
  toThingKey: string;      // base64
  toThingKeyJSON: unknown; // decoded key (from step 3), null if not JSON-compatible
  toThingShortId: number;
  toThingType: string;
}

interface TriggerResult {
  ok: boolean;
  error?: string;
}
```

### ThingView Component

Renders another thing inline using its custom renderer (recursive):

```tsx
<ThingView
  thingKey="base64..."       // Specify by base64 key OR
  keyJSON={{ id: 1 }}        // by JSON key (from step 3) OR
  shortId={123}              // by shortId
  args={{ custom: 'data' }}  // Optional args passed to child renderer
/>
```

- Fetches thing data at current tick
- Uses that thing's custom renderer (or JSON fallback)
- Recursive rendering with same ThingWatcherBindings context

### ThingLink Component

Renders a clickable badge linking to a thing:

```tsx
<ThingLink
  thingKey="base64..."       // Specify by base64 key OR
  keyJSON={{ id: 1 }}        // by JSON key (from step 3) OR
  shortId={123}              // by shortId
  label="Custom Label"       // Optional (default: "#shortId (thingType)")
/>
```

- Left-click: selects the thing (updates global selection)
- Right-click: shows context menu with "Open in new panel"
- Styled as a badge similar to existing link display

## Python API

```python
from thingwatcher import register_renderer

# Register TSX source for a thing type
register_renderer("my_thing_type", '''
import React from 'react';
import { ThingLink } from 'thingwatcher-bindings';

export default function MyRenderer({ payload, links, triggerable, trigger, args }) {
  return (
    <div>
      <h3>{payload.title}</h3>
      {triggerable && (
        <button onClick={() => trigger({ action: 'approve' })}>
          Approve
        </button>
      )}
      {links.map(link => (
        <ThingLink key={link.linkKey} shortId={link.toThingShortId} />
      ))}
    </div>
  );
}
''')
```

## GraphQL Changes

Add `renderer` field to `ThingTypeConfig`:

```graphql
type ThingTypeConfig {
  thingType: String!
  color: String!
  linkColors: [LinkColorConfig!]!
  renderer: String          # NEW: TSX source code, or null if no custom renderer
}
```

The renderer is served alongside existing rendering config.

## Frontend Architecture

### New Files

```
frontend/
├── lib/
│   └── ThingWatcherBindings.tsx    # SDK: ThingView, ThingLink, context
├── components/
│   └── DynamicRenderer/
│       ├── index.ts
│       ├── DynamicRenderer.tsx     # Compiles and renders TSX
│       ├── DynamicRenderer.module.css
│       ├── esbuildSetup.ts         # esbuild-wasm init + plugins
│       └── rendererCache.ts        # Cache compiled components by type
```

### DynamicRenderer Component

Adapted from the spike at `/Users/daniel/prototypes/scratch2/dynamic_react`:

```tsx
interface DynamicRendererProps {
  tsxCode: string;
  rendererProps: ThingRendererProps;
}

function DynamicRenderer({ tsxCode, rendererProps }: DynamicRendererProps) {
  // 1. Compile TSX with esbuild-wasm (cached by content hash)
  // 2. Inject modules: react, thingwatcher-bindings
  // 3. Load compiled module, get default export
  // 4. Render with ErrorBoundary for runtime errors
}
```

Key adaptations from spike:
- Replace `foo` module with `thingwatcher-bindings`
- Add caching by thingType (recompile only if source changes)
- Pass full `ThingRendererProps` instead of just `payload`

### RendererContext

React context providing SDK functionality to ThingWatcherBindings:

```tsx
interface RendererContextValue {
  // For ThingView
  currentTick: number | 'live';
  client: Client;              // urql client for fetching thing data
  renderers: Map<string, string>;  // thingType -> TSX source

  // For ThingLink
  onSelectThing: (shortId: number) => void;
  onContextMenu: (event: MouseEvent, shortId: number) => void;

  // For trigger callback
  triggerPause: (shortId: number, payload: unknown) => Promise<TriggerResult>;
}
```

### Integration Points

**DetailsPanel changes:**
- Query renderer for selected thing's type (from renderingConfig)
- If renderer exists: wrap in DynamicRenderer
- If not: show existing JsonTree (fallback)
- Standard header (thingType, shortId, key) always shown above

**ThingViewPanel changes:**
- Same logic as DetailsPanel
- Already has its own GraphQL query for thing data

## UI Layout

```
┌─────────────────────────────────────────┐
│ Details                    [thingType]  │  <- Standard header (always)
├─────────────────────────────────────────┤
│ #123  {"id": 1}                         │  <- Meta info (keyJSON or base64 fallback)
├─────────────────────────────────────────┤
│                                         │
│   ┌─────────────────────────────────┐   │
│   │                                 │   │
│   │   Custom Renderer Content       │   │  <- DynamicRenderer OR JsonTree
│   │   (or JSON tree fallback)       │   │
│   │                                 │   │
│   └─────────────────────────────────┘   │
│                                         │
└─────────────────────────────────────────┘
```

## Implementation Steps

### Phase A: Python Backend

- [x] **A1.** Add renderer registry to `registry.py`:
  - `_renderers: dict[str, str]` mapping thing_type to TSX source
  - `register_renderer(thing_type, tsx_source)`
  - `get_renderer(thing_type) -> str | None`
  - `get_all_renderers() -> dict[str, str]`

- [x] **A2.** Update `api.py` to re-export `register_renderer`

- [x] **A3.** Update GraphQL schema - add `renderer: String` to `ThingTypeConfig`

- [x] **A4.** Update server.py resolver - include renderer in renderingConfig response

### Phase B: Frontend DynamicRenderer

- [x] **B1.** Add `esbuild-wasm` to package.json dependencies

- [x] **B2.** Create `lib/ThingWatcherBindings.tsx`:
  - `RendererContext` and `useRendererContext()` hook
  - `ThingView` component (fetches thing, recurses)
  - `ThingLink` component (badge with click handlers)
  - Export types for `ThingRendererProps`

- [x] **B3.** Create `components/DynamicRenderer/esbuildSetup.ts`:
  - `initEsbuild()` - one-time wasm initialization
  - `createInjectedDepsPlugin()` - inject react + thingwatcher-bindings
  - Adapted from spike's plugin approach

- [x] **B4.** Create `components/DynamicRenderer/rendererCache.ts`:
  - Cache compiled components by thingType
  - Invalidate when TSX source changes (hash comparison)

- [x] **B5.** Create `components/DynamicRenderer/DynamicRenderer.tsx`:
  - Main component that compiles and renders
  - ErrorBoundary for compile and runtime errors
  - Loading state while compiling

- [x] **B6.** Add CSS for DynamicRenderer (loading, error states)

### Phase C: Integration

- [x] **C1.** Update `graphql/queries.ts`:
  - Ensure renderingConfig query includes renderer field
  - Add any new queries if needed

- [x] **C2.** Create `RendererContext` provider in App.tsx or DockviewLayout:
  - Provide urql client, current tick, selection handlers
  - Provide renderers map from renderingConfig

- [x] **C3.** Update `DetailsPanel.tsx`:
  - Check if thing.thingType has a renderer
  - Conditionally render DynamicRenderer vs JsonTree
  - Pass correct props to renderer

- [x] **C4.** Update `ThingViewPanel.tsx`:
  - Same conditional rendering logic
  - Ensure it works in standalone panels

- [x] **C5.** Update `DetailsPanelWrapper.tsx` and context menu handling:
  - Ensure context menu works from within custom renderers

### Phase D: Testing & Polish

- [x] **D1.** Add example renderers to `examples/demo_pause_trigger.py`

- [x] **D2.** Test fallback behavior (things without renderers) - see `examples/demo_dynamic_rendering.py`

- [x] **D3.** Test recursive rendering (ThingView inside renderer) - see `examples/demo_dynamic_rendering.py`

- [x] **D4.** Test trigger functionality from custom renderer - see `examples/demo_pause_trigger.py`

- [x] **D5.** Test error handling (compile errors, runtime errors) - see `examples/demo_dynamic_rendering.py`

- [x] **D6.** Update CODE_OVERVIEW.md and frontend/LOCAL_CODE_OVERVIEW.md

## Error Handling

**Compile errors:**
- Show error message in renderer area
- Don't break the whole panel
- Display TSX source excerpt if helpful

**Runtime errors:**
- ErrorBoundary catches and displays
- Show "Render error: {message}"
- Allow retry or fall back to JSON view

## Dependencies

New npm packages:
- `esbuild-wasm` - in-browser TypeScript/JSX compilation

## Notes

- Renderers are cached by thingType, not per-thing instance
- esbuild-wasm is ~3MB but loads lazily on first custom renderer
- The spike already proves the approach works; main work is integration
- ThingView recursive rendering should have a depth limit (e.g., 10) to prevent infinite loops from circular references

## Known Issues / Future Work

1. ~~**Missing `toThingKey`/`toThingKeyJSON` in LinkInfo**~~: Fixed - `GET_THING_DETAILS` now fetches `key` and `keyJSON` for `link.toThing`.

2. **esbuild-wasm version coordination**: The wasm URL version must exactly match the npm package version. We pin the version in package.json and have a comment in `esbuildSetup.ts`, but this is fragile. If someone updates the package without updating the URL, it will fail at runtime.

3. **ThingView recursive rendering**: The mechanism for recursive rendering is indirect - ThingView accesses a `RenderComponent` from context that's injected by DockviewLayout. This works but the typing is loose (uses `as` cast). Could be cleaner.

4. ~~**Untested paths**~~: All paths now have demos in `examples/demo_dynamic_rendering.py`:
   - ThingView component (recursive rendering) - Container renders Items inline via ThingView
   - ThingLink context menu - Container uses ThingLink for item links
   - Compile error display - BrokenCompile type has invalid TSX
   - Runtime error display - BrokenRuntime type throws during render
   - Fallback to JSON - NoRenderer type has no custom renderer

5. **Bundle size**: Adding esbuild-wasm increases the main.js bundle from ~2MB to ~3.4MB. The wasm file itself (~10MB) loads lazily from CDN only when a custom renderer is used.
