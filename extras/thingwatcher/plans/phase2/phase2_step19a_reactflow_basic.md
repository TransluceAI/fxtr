# Phase 2 Step 19A: Basic React Flow Swap

Replace Cytoscape.js with React Flow in the simplest possible way.

## Goal

Get React Flow rendering the graph with basic functionality:
- Colored square nodes with shortId labels
- Edges with arrow markers
- Simple dagre layout (computed fresh each tick)
- Basic pan/zoom

No selection, no change-state visualization, no custom renderers yet.

## Implementation Steps

### 1. Update dependencies
- [x] Add `@xyflow/react` to package.json
- [x] Add `@dagrejs/dagre` for layout
- [x] Keep cytoscape for now (will remove at end of phase)

### 2. Preserve old Graph component
- [x] Rename `Graph.tsx` → `GraphOld.tsx`
- [x] Rename `Graph.module.css` → `GraphOld.module.css`
- [x] Update import in `GraphOld.tsx` to use renamed CSS file
- [x] Update `components/Graph/index.ts` to still export (temporarily point to old)

### 3. Create new React Flow graph component
- [x] Create `Graph.tsx` (new file in same folder)
- [x] Create `Graph.module.css` (new file)
- [x] Basic React Flow setup with `<ReactFlow>` component
- [x] Configure to disable node connections (view-only): `nodesConnectable={false}`, `connectOnClick={false}`
- [x] Enable pan/zoom (default behavior)

### 4. Implement node type
- [x] Create `ThingNode.tsx` - custom node component
- [x] Render as colored square with rounded corners
- [x] Show shortId as label (centered)
- [x] Use `useThingTypeConfig(thingType)` hook to get color (cleaner than old map approach)
- [x] Register as custom node type with React Flow

### 5. Implement edge type
- [x] Use React Flow's default edge or create simple custom edge
- [x] Add arrow marker on target end (markerEnd)
- [x] Use a single default edge color for now (gray, semi-transparent)
- [x] Link-specific colors can be added in a later phase

### 6. Implement dagre layout
- [x] Create `useGraphLayout.ts` hook or utility function
- [x] Take nodes and edges, return positioned nodes
- [x] Use dagre with top-to-bottom (TB) direction
- [x] Compute fresh layout each time data changes (no persistence yet)

### 7. Wire up to GraphPanelWrapper
- [x] Update `components/Graph/index.ts` to export new Graph component
- [x] Verify Graph receives same props: `things`, `changeInfo`, `selectedThingKey`, `onSelectThing`
- [x] For now, ignore `selectedThingKey` and `onSelectThing` (Phase B)
- [x] For now, ignore `changeInfo` (Phase B)

### 8. Basic styling
- [x] Dark background matching current graph
- [x] Node styling: ~40px square, rounded corners, white text
- [x] Edge styling: default gray color, semi-transparent, arrow markers

### 9. Documentation
- [x] Update LOCAL_CODE_OVERVIEW.md: Graph section mentions React Flow instead of Cytoscape
- [x] Note that this is Phase A (basic, no selection/change-state yet)
- [x] Keep note about GraphOld.tsx being preserved temporarily
- [x] Note: `useColorMapsForGraphOnly` kept for now (used by GraphOld) - will remove in Phase B

### 10. Test and verify
- [ ] Run demo_dynamic_rendering
- [ ] Verify nodes render with correct colors
- [ ] Verify edges connect correct nodes with arrows
- [ ] Verify pan/zoom works
- [ ] Verify layout is reasonable (hierarchical, top-to-bottom)

---

## Technical Notes

### React Flow Setup

```tsx
import { ReactFlow, ReactFlowProvider } from '@xyflow/react';
import '@xyflow/react/dist/style.css';

const nodeTypes = { thing: ThingNode };

function Graph({ things, changeInfo, selectedThingKey, onSelectThing }) {
  const { nodes, edges } = useGraphData(things);
  const layoutedNodes = useGraphLayout(nodes, edges);

  return (
    <ReactFlowProvider>
      <ReactFlow
        nodes={layoutedNodes}
        edges={edges}
        nodeTypes={nodeTypes}
        nodesConnectable={false}
        connectOnClick={false}
        fitView
      />
    </ReactFlowProvider>
  );
}
```

### Dagre Layout

```tsx
import dagre from '@dagrejs/dagre';

function getLayoutedNodes(nodes, edges) {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: 'TB', nodesep: 50, ranksep: 50 });
  g.setDefaultEdgeLabel(() => ({}));

  nodes.forEach(node => {
    g.setNode(node.id, { width: 40, height: 40 });
  });

  edges.forEach(edge => {
    g.setEdge(edge.source, edge.target);
  });

  dagre.layout(g);

  return nodes.map(node => {
    const pos = g.node(node.id);
    return { ...node, position: { x: pos.x - 20, y: pos.y - 20 } };
  });
}
```

### Node Data Shape

React Flow nodes need this shape:
```tsx
{
  id: string,           // unique node ID (use thing.key or shortId)
  type: 'thing',        // our custom node type
  position: { x, y },   // set by layout
  data: {
    thingKey: string,   // b64 key
    shortId: number,
    thingType: string,
    // Note: color is looked up by ThingNode via useThingTypeConfig(thingType)
  }
}
```

### Edge Data Shape

React Flow edges need this shape:
```tsx
{
  id: string,           // unique edge ID
  source: string,       // source node ID
  target: string,       // target node ID
  markerEnd: { type: MarkerType.ArrowClosed },
  style: { stroke: '#4b5563', opacity: 0.7 },  // default gray for Phase A
}
```

---

## Exit Criteria

- [x] Graph renders using React Flow instead of Cytoscape
- [x] Nodes are colored squares with shortId labels (color via `useThingTypeConfig`)
- [x] Edges connect nodes with arrow markers (default gray color)
- [x] Layout is hierarchical (top-to-bottom)
- [x] Pan/zoom works
- [x] No console errors
- [x] LOCAL_CODE_OVERVIEW.md updated

---

## Implementation Notes

Additional work done beyond the original plan:

1. **Added `key` field to GraphQL query** - The `toThing` selection in `hooks/ticks.ts` only fetched `shortId`. Added `key` since we use b64 keys as React Flow node IDs.

2. **Added Handle components to ThingNode** - React Flow custom nodes require `<Handle>` components for edges to connect. Added invisible source/target handles.

3. **Node dragging enabled** - Originally planned to disable dragging for Phase A simplicity, but enabled it. Required adding local state for nodes and `onNodesChange` handler with `applyNodeChanges`.

4. **CSS for invisible handles** - Added `.handle` class to hide the connection handles visually while keeping them functional.

5. **Node cursor styling** - Added `cursor: grab` to indicate nodes are draggable.
