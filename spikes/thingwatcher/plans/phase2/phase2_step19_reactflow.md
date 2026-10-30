# Phase 2 Step 19: Switch to React Flow

Replace Cytoscape.js with React Flow for the graph visualization, and add a new "graph" renderer type that allows custom node and edge rendering.

## End State

### Overview

The Graph component uses React Flow instead of Cytoscape.js. From React Flow's perspective there is one node type ("thing"), but its implementation delegates to our dynamic renderer system. A new `graph` renderer type allows thing types to define custom graph nodes with React Flow ports (handles), plus optional edge label renderers.

### New Renderer Type: `graph`

The `graph` renderer is added to `RendererSet` alongside `full`, `ref`, `inline`, `preview`:

```tsx
export interface GraphRendererConfig {
  /** React component for the graph node */
  node: FC<GraphNodeRendererProps>;
  /** Optional React component for edge labels FROM this thing */
  edge?: FC<GraphEdgeRendererProps>;
  /** Determine which source port to use for an outgoing edge. Returns port ID or null. */
  getSourcePort?: (link: LinkInfo) => string | null;
  /** Determine which target port to use for an incoming edge. Returns port ID or null. */
  getTargetPort?: (parentType: string, edgeType: string) => string | null;
}

export interface RendererSet {
  full?: FC<FullRendererProps>;
  ref?: FC<RefRendererProps>;
  inline?: FC<RefRendererProps>;
  preview?: FC<PreviewRendererProps>;
  graph?: GraphRendererConfig;  // NEW
}
```

### Graph Node Renderer

The graph node renderer receives a `PreviewRendererHandle` (same hooks as preview renderer):
- `usePayload`, `useDataChunks`, `useLinks`, `useThingMetadata`, `useDraggableIdMarker`

Props:
```tsx
export interface GraphNodeRendererProps {
  handle: PreviewRendererHandle;
}
```

The renderer should:
- Render a compact, squarish node (simpler than preview)
- Define React Flow `<Handle>` components (we call these "ports") for its connection points
- Optionally wrap draggable content in `DraggableIDMarker` to enable drag-to-panel

Note: When `DraggableIDMarker` is used inside a graph node renderer, the system automatically applies the `nodrag` class to prevent conflict with React Flow's node dragging. The custom renderer doesn't need to do anything special.

Example custom graph node:
```tsx
import { Handle, Position } from '@xyflow/react';
import { usePayload, useThingMetadata, useDraggableIdMarker } from 'thingwatcher-bindings';

function ContainerGraphNode({ handle }: GraphNodeRendererProps) {
  const payload = usePayload(handle);
  const { shortId } = useThingMetadata(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  return (
    <div className="container-node">
      <Handle type="target" position={Position.Top} id="this" />

      <DraggableIDMarker>
        <span style={{ cursor: 'grab' }}>#{shortId} {payload?.name}</span>
      </DraggableIDMarker>

      <Handle type="source" position={Position.Bottom} id="item" />
    </div>
  );
}
```

### Graph Edge Renderer

The edge renderer receives link info plus a `RefRendererHandle` for the source thing:

```tsx
export interface GraphEdgeRendererProps {
  /** The link type (e.g., "item", "parent") */
  linkType: string;
  /** The link key as base64 string */
  linkKey: string;
  /** The link key as JSON (or null if not JSON-serializable) */
  linkKeyJSON: unknown;
  /** Handle to access source thing data (RefRenderer-level hooks) */
  sourceHandle: RefRendererHandle;
}
```

Example:
```tsx
function ContainerEdgeLabel({ linkType, sourceHandle }: GraphEdgeRendererProps) {
  const payload = usePayload(sourceHandle);
  return <div className="edge-label">{linkType}</div>;
}
```

### Port Mapping Functions

The `getSourcePort` and `getTargetPort` functions determine which React Flow ports (handles) edges connect to:

```tsx
graph: {
  node: ContainerGraphNode,
  edge: ContainerEdgeLabel,
  // Outgoing edges from this thing use the link type as port ID
  getSourcePort: (link) => link.linkType,
  // Incoming edges connect to the "this" port
  getTargetPort: (parentType, edgeType) => "this",
}
```

If not provided or returns `null`, React Flow uses its default behavior (connects to node center).

### Fallback Graph Renderer

When a thing type doesn't define a custom `graph` renderer:
- Simple rounded square node showing the shortId
- Node background colored by thingType (from `configure_rendering()`)
- No named ports - edges connect to node center
- No edge labels

### Change State Visualization

Change state (added/modified/disappearing) is shown via a colored outline around the node:
- Added: green outline
- Modified: yellow outline
- Disappearing: red dashed outline
- Added+Disappearing: purple dashed outline
- Modified+Disappearing: purple dashed outline

This outline is rendered by the wrapper component, not the custom renderer itself. Custom renderers don't need to handle change state unless they want to (we could add a hook later if needed).

### Layout

Use dagre for automatic graph layout:
- Hierarchical top-to-bottom layout
- Semi-stable positions: existing nodes keep their positions when new nodes are added
- Fit viewport when changed nodes are off-screen (similar to current behavior)
- Animated transitions for node position changes

### Interaction

- **Click node**: Select it (updates DetailsPanel, same as today)
- **Drag node**: Move it (React Flow default)
- **Drag DraggableIDMarker**: Drag to dockview to open in panel (existing functionality, `nodrag` applied automatically)
- **Right-click**: No context menu on graph (use ThingsList or DetailsPanel for that, same as today)

### What's NOT Changing

- Cannot create new edges by connecting ports (view-only graph)
- Selection, panels, Timeline, etc. all work the same way
- Custom renderers for full/ref/inline/preview unchanged
- The graph is still just one panel in dockview

---

## Implementation Phases

The implementation is broken into 4 phases, each building on the previous. Each phase has its own detailed subplan.

### Phase A: Basic React Flow Swap

**Goal**: Replace Cytoscape with React Flow in the simplest possible way.

- Add React Flow dependencies, remove Cytoscape
- Create a new Graph component using React Flow
- Single hardcoded node type: colored square with shortId (same colors as current Cytoscape nodes)
- Basic edges connecting nodes
- Simple dagre layout computed fresh each tick (no position persistence)
- No interactions beyond basic pan/zoom

**Exit criteria**: Graph renders with React Flow, looks similar to current Cytoscape graph (colored squares, edges), but no selection or change-state visualization yet. LOCAL_CODE_OVERVIEW.md updated.

### Phase B: Feature Parity with Cytoscape

**Goal**: Match current Cytoscape functionality.

- Improved layout with position persistence across ticks (semi-stable positioning)
- Colored outlines for change state (green=added, yellow=modified, red=disappearing, etc.)
- Click node to select (updates DetailsPanel)
- Viewport fitting when changed nodes are off-screen
- Animated transitions for node position changes
- Remove `GraphOld.tsx` and `useColorMapsForGraphOnly` hook (no longer needed)

**Exit criteria**: Graph behaves like current Cytoscape graph - selection works, change state is visible, layout is stable when scrubbing through ticks. Old Cytoscape code removed. Documentation updated.

### Phase C: Custom Graph Renderers

**Goal**: Add the new `graph` renderer type to the renderer system.

- Add TypeScript types for `GraphNodeRendererProps`, `LinkEdgeConfigProps`
- Update `RendererSet` to include optional `graph` field
- Create `DynamicGraphNodeRenderer` component
- Export `Port`, `PortPosition`, `LinkEdgeConfig` from thingwatcher-bindings
- Declarative edge configuration via `LinkEdgeConfig` component
- Propagate custom node dimensions to cola layout
- Update Container and Item demo with custom graph renderers

**Exit criteria**: Custom graph renderers work - Container/Item show custom nodes with ports, edge labels display, custom-sized nodes don't overlap. Documentation updated.

### Phase D: Interactivity and Polish

**Goal**: Full interactivity and cleanup.

- Alt+drag on graph nodes for drag-to-panel (deferred from Phase C)
- Hover preview cards on graph nodes (deferred from Phase C)
- Remove old Cytoscape code and dependencies (if any remain)
- Final testing across all demos

**Exit criteria**: Full end state achieved, old code removed, final documentation pass complete.

---

## Detailed Subplans

Each phase will have its own detailed subplan created when we begin that phase:

- [x] `phase2_step19a_reactflow_basic.md` - Phase A details (completed)
- [x] `phase2_step19b_reactflow_parity.md` - Phase B details (completed)
- [x] `phase2_step19c_custom.md` - Phase C details (completed)
- [x] `phase2_step19d_cleanup.md` - Phase D details (completed)

---

## Open Questions / Future Work

- **Hook for change state**: If custom renderers need to style based on change state, we could add `useChangeState(handle)` hook
- **elkjs**: If dagre proves too limiting, consider switching to elkjs for more layout options
