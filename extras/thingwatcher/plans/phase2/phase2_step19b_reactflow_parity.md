# Phase 2 Step 19B: Feature Parity with Cytoscape

Achieve feature parity with the old Cytoscape graph, using cola.js for iterative force-directed layout.

## Goal

Match current Cytoscape functionality:
- Iterative layout using cola.js (replaces dagre)
- Position persistence across ticks
- Selection (click to select, white border)
- Change state visualization (colored borders)
- Smart viewport management
- Animated transitions via simulation

## Key Design Decisions

### Layout: Cola.js

Replace dagre with [webcola](https://github.com/tgdwyer/WebCola) for force-directed layout:

- **Iterative simulation**: Layout settles naturally over time, providing smooth animation
- **Organic layout**: No hierarchy constraints (can add later if needed)
- **Position persistence**: Existing nodes keep positions, new nodes get pulled in by simulation
- **Always running until settled**: Simulation runs continuously until converged, then stops
- **Restart triggers**: Tick changes and drag events restart simulation (no-op if already settled)

### New Node Positioning

When a new node appears, its initial position before simulation:
- **Connected nodes**: Average position of connected neighbors
- **Orphan nodes**: Seeded random position based on shortId (deterministic)

### Node Dragging

- During drag: node is "pinned" (fixed position) and participates in simulation
- Other nodes move around the pinned node
- On release: node stays at new position, continues participating in simulation

### Viewport Management

- During simulation: viewport gently tracks changed nodes (added/modified/disappearing)
- Only adjusts if changed nodes are off-screen
- User interaction (pan/zoom) takes priority - stops automatic viewport tracking
- Max zoom clamped to 1.5

### Selection

- Click node to select (updates `selectedThingKey`, syncs with DetailsPanel)
- Click background to deselect
- Selected node has white border
- **Double border**: If a node is both selected AND has a change state, show both - change state as border, selection as box-shadow or outline

### Change State Visualization

Colored borders around nodes based on change status:
- **Added**: green solid border (`#22c55e`)
- **Modified**: yellow solid border (`#eab308`)
- **Disappearing**: red dashed border (`#ef4444`)
- **Added+Disappearing**: purple dashed border (`#a855f7`)
- **Modified+Disappearing**: purple dashed border (`#a855f7`)

---

## Implementation Steps

### 1. Add cola.js dependency
- [x] Add `webcola` to package.json
- [x] Remove `@dagrejs/dagre` (no longer needed)

### 2. Create ColaLayoutManager class (Stage 1)
- [ ] Create `ColaLayoutManager.ts` as a plain TypeScript class
- [ ] Manager owns: cola.Layout, colaNodes array, position map, animation loop
- [ ] Implement `setSize(width, height)` - update layout size
- [ ] Implement `syncNodes(nodes, edges)` - diff and update cola nodes/links
- [ ] Implement `getPositions()` - return current position map
- [ ] Implement animation loop with `requestAnimationFrame`
- [ ] Call `onPositionsChanged` callback when positions update
- [ ] Implement `dispose()` for cleanup

### 3. Refactor useColaLayout hook (Stage 1)
- [ ] Create manager once via `useRef` (not recreated on render)
- [ ] Simple useEffects that call manager methods:
  - nodes/edges changed → `manager.syncNodes()`
  - size changed → `manager.setSize()`
- [ ] Return `getPositions` and other methods from manager
- [ ] No complex state management in the hook itself

### 4. Update Graph component (Stage 1)
- [ ] Remove `nodes` useState entirely
- [ ] Compute `positionedNodes` directly: `baseNodes.map(n => ({ ...n, position: getPositions().get(n.id) }))`
- [ ] Pass `positionedNodes` directly to React Flow (fully controlled)
- [ ] Set `nodesDraggable={false}` temporarily
- [ ] Remove all drag handlers for now
- [ ] Verify: simulation runs, nodes animate, positions persist

### 5. Re-enable dragging via cola (Stage 2)
- [ ] Add to ColaLayoutManager:
  - `pinNode(id, x, y)` - mark node as fixed, start simulation
  - `unpinNode(id)` - unmark fixed, continue simulation
  - `updateDragPosition(id, x, y)` - update position map, trigger callback
- [ ] Add drag handlers to Graph.tsx:
  - `onNodeDragStart` → `pinNode()`
  - `onNodeDrag` → `updateDragPosition()`
  - `onNodeDragStop` → `unpinNode()`
- [ ] Set `nodesDraggable={true}`
- [ ] Verify: dragging works, other nodes move, node stays where dropped

### 5. Implement selection
- [x] Add `onClick` handler to ThingNode (via Graph.tsx onNodeClick)
- [x] Call `onSelectThing(thingKey)` on node click
- [x] Add click handler on ReactFlow background to deselect (onPaneClick)
- [x] Pass `selected` prop to ThingNode based on `selectedThingKey`
- [x] Style selected node with white box-shadow

### 6. Implement change state visualization
- [x] Pass `changeState` to ThingNode via node data
- [x] Compute change state from `changeInfo` prop (added, modified, disappearing, combinations)
- [x] Apply border styles based on change state
- [x] Handle dashed borders for disappearing states

### 7. Implement viewport management
- [x] Track whether user has interacted with viewport (pan/zoom)
- [x] During simulation (if user hasn't taken over):
  - Check if changed nodes are off-screen
  - If so, smoothly adjust viewport to include them
- [x] Reset "user took over" flag when data changes
- [x] Clamp zoom to max 1.5

### 8. Remove old code
- [x] Delete `GraphOld.tsx`
- [x] Delete `GraphOld.module.css`
- [x] Update `components/Graph/index.ts` (remove GraphOld export)
- [x] Delete `useColorMapsForGraphOnly` from `hooks/useRenderingConfig.ts`
- [x] Update `hooks/index.ts` to remove the export
- [x] Delete `useGraphLayout.ts` (dagre hook, no longer needed)
- [x] Remove `cytoscape` and `cytoscape-fcose` from package.json

### 9. Update documentation
- [x] Update `LOCAL_CODE_OVERVIEW.md`: Graph section reflects cola.js, selection, change state
- [x] Update `CODE_OVERVIEW.md`: mention cola.js instead of dagre
- [x] Update main plan `phase2_step19_reactflow.md`: check off Phase B

### 10. Test Stage 1 (cola as source of truth)
- [ ] Verify nodes render with correct colors
- [ ] Verify edges connect properly
- [ ] Verify layout runs continuously until settled (see nodes animate)
- [ ] Verify position persistence (scrub ticks, nodes stay roughly in place)
- [ ] Verify selection still works (click node, white border, DetailsPanel updates)
- [ ] Verify change state borders (scrub through ticks, see green/yellow/red/purple)

### 11. Test Stage 2 (dragging)
- [ ] Verify dragging works smoothly (no snap-back, no disappearing nodes)
- [ ] Verify other nodes move while dragging (simulation runs during drag)
- [ ] Verify node stays where dropped
- [ ] Verify viewport tracking (add nodes off-screen, viewport adjusts)

---

## Technical Notes

### Cola.js Setup

```typescript
import * as cola from 'webcola';

const layout = new cola.Layout()
  .size([width, height])
  .nodes(nodes)
  .links(edges)
  .jaccardLinkLengths(100) // or linkDistance()
  .avoidOverlaps(true)
  .handleDisconnected(true)
  .on('tick', () => {
    // Update React Flow node positions
  })
  .on('end', () => {
    // Simulation settled
  });

// Start simulation
layout.start(10, 15, 20); // initial, converge, refine iterations

// Or use startWithoutUnconstrainedIterations() for ongoing simulation
// and stop() when done
```

### Settled Detection

Use cola's built-in `end` event which fires when the simulation converges. Cola automatically detects when node movement is negligible and stops.

### Pinning Nodes

Cola.js supports fixed nodes via the `fixed` property:

```typescript
// Pin a node
node.fixed = true;
node.x = dragX;
node.y = dragY;

// Unpin
node.fixed = false;
```

### Position Persistence Strategy

```typescript
// Persistent position storage (survives data changes)
const positionsRef = useRef<Map<string, {x: number, y: number}>>(new Map());

// When things change:
// 1. Keep positions for nodes that still exist
// 2. New nodes start at center or neighbor average
// 3. Removed nodes are cleaned up from positionsRef
```

### Change State Computation

```typescript
function getChangeState(key: string, changeInfo: ChangeInfo): ChangeState {
  const isAdded = changeInfo.addedKeys.has(key);
  const isModified = changeInfo.modifiedKeys.has(key);
  const isDisappearing = changeInfo.disappearingKeys.has(key);

  if (isAdded && isDisappearing) return 'addedDisappearing';
  if (isModified && isDisappearing) return 'modifiedDisappearing';
  if (isAdded) return 'added';
  if (isModified) return 'modified';
  if (isDisappearing) return 'disappearing';
  return 'unchanged';
}
```

### ThingNode Border Styles

```css
/* Change state borders */
.thingNode.added {
  border: 3px solid #22c55e;
}
.thingNode.modified {
  border: 3px solid #eab308;
}
.thingNode.disappearing {
  border: 2px dashed #ef4444;
}
.thingNode.addedDisappearing,
.thingNode.modifiedDisappearing {
  border: 2px dashed #a855f7;
}

/* Selection - uses box-shadow so it can combine with change state border */
.thingNode.selected {
  box-shadow: 0 0 0 3px #fff;
}

/* If no change state, selection can use border instead */
.thingNode.selected.unchanged {
  border: 3px solid #fff;
  box-shadow: none;
}
```

---

## Refactor: Cola Layout Manager Architecture

The initial implementation had issues with node dragging due to tangled state between React Flow and cola. This section describes the refactored architecture.

### Problem with Initial Implementation

The original `useColaLayout` hook had several issues:

1. **Giant useEffect recreates cola state** - Every time `nodes` or `edges` changed, we rebuilt the entire cola Layout instance, losing simulation state.

2. **Two sources of truth** - React Flow maintains internal node positions for dragging, while cola maintains its own positions. Syncing these via `setNodes()` caused conflicts.

3. **State replacement breaks React Flow** - Calling `setNodes(positionedNodes)` during drag replaced React Flow's internal node array, breaking its drag tracking.

### New Architecture: Persistent Manager + Controlled React Flow

**Core principle**: Cola is the single source of truth for positions. React Flow is fully controlled and just renders whatever cola says.

```
┌─────────────────────────────────────────────────────────────┐
│  ColaLayoutManager (plain TypeScript class)                 │
│                                                             │
│  - Owns cola.Layout instance (persists across renders)      │
│  - Owns position map                                        │
│  - Runs animation loop via requestAnimationFrame            │
│  - Methods: setSize, syncNodes, syncEdges, pinNode,         │
│             unpinNode, updateDragPosition, getPositions     │
│  - Calls onPositionsChanged callback when positions update  │
└─────────────────────────────────────────────────────────────┘
                         ↑
                         │ created once via useRef
                         │
┌─────────────────────────────────────────────────────────────┐
│  useColaLayout hook (thin React bridge)                     │
│                                                             │
│  - Creates manager once (useRef, not recreated on render)   │
│  - useEffects diff props and call manager methods:          │
│    - nodes changed → manager.syncNodes(added, removed)      │
│    - edges changed → manager.syncEdges(edges)               │
│    - size changed → manager.setSize(w, h)                   │
│  - Returns: getPositions, pinNode, unpinNode, etc.          │
└─────────────────────────────────────────────────────────────┘
                         ↑
                         │
┌─────────────────────────────────────────────────────────────┐
│  Graph component                                            │
│                                                             │
│  - Computes positionedNodes directly from getPositions()    │
│  - NO useState for nodes - fully derived state              │
│  - Passes positionedNodes directly to React Flow            │
│  - React Flow is fully controlled (nodesDraggable=false     │
│    initially, then enabled with drag events → cola)         │
└─────────────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────────┐
│  React Flow                                                 │
│                                                             │
│  - Renders nodes at positions from cola                     │
│  - Drag events sent back to cola (when enabled)             │
│  - No internal position state conflicts                     │
└─────────────────────────────────────────────────────────────┘
```

### Data Flow

**Cola simulation tick:**
```
cola.tick()
  → update colaNodes positions
  → sync to positionMap
  → call onPositionsChanged()
  → React re-renders
  → positionedNodes recomputed from positionMap
  → React Flow renders new positions
```

**Node dragging (Stage 2):**
```
User drags in React Flow
  → onNodeDrag fires
  → call manager.updateDragPosition(id, x, y)
  → positionMap updated
  → onPositionsChanged()
  → React re-renders with new position
```

### ColaLayoutManager Class

```typescript
class ColaLayoutManager {
  private layout: cola.Layout | null = null;
  private colaNodes: ColaNode[] = [];
  private colaLinks: cola.Link<ColaNode>[] = [];
  private nodeIndexMap: Map<string, number> = new Map();
  private positions: Map<string, {x: number, y: number}> = new Map();
  private size = { width: 800, height: 600 };
  private isRunning = false;
  private isDragging = false;
  private onPositionsChanged: () => void;

  constructor(onPositionsChanged: () => void);

  setSize(width: number, height: number): void;
  syncNodes(currentNodes: NodeInfo[], edges: EdgeInfo[]): void;
  pinNode(id: string, x: number, y: number): void;
  unpinNode(id: string): void;
  updateDragPosition(id: string, x: number, y: number): void;
  getPositions(): Map<string, {x: number, y: number}>;
  dispose(): void;

  private rebuildLayout(): void;
  private ensureRunning(): void;
  private tick(): void;
}
```

### Refactor Stages

#### Stage 1: Cola as Source of Truth (no dragging)

Goal: Get cola continuously updating React Flow positions without dragging.

- [ ] Create `ColaLayoutManager.ts` class
- [ ] Refactor `useColaLayout.ts` to use the manager
- [ ] Update `Graph.tsx`:
  - Remove `nodes` useState entirely
  - Compute `positionedNodes` directly from manager.getPositions()
  - Set `nodesDraggable={false}` temporarily
  - Remove drag handlers
- [ ] Verify: nodes animate smoothly when simulation runs, positions persist across ticks

#### Stage 2: Re-enable Dragging via Cola

Goal: Dragging works by sending events to cola, not by React Flow managing positions.

- [ ] Add drag handlers back to Graph.tsx:
  - `onNodeDragStart`: call `manager.pinNode(id, x, y)`
  - `onNodeDrag`: call `manager.updateDragPosition(id, x, y)` AND trigger re-render
  - `onNodeDragStop`: call `manager.unpinNode(id)`
- [ ] Set `nodesDraggable={true}`
- [ ] Key insight: `updateDragPosition` updates positionMap directly and calls `onPositionsChanged()`, so React Flow immediately re-renders with the new position
- [ ] Verify: dragging works, other nodes move during drag, node stays where dropped

---

## Exit Criteria

- [ ] Graph uses cola.js for iterative force-directed layout
- [ ] ColaLayoutManager class owns all cola state
- [ ] Nodes keep positions across tick changes (position persistence)
- [ ] Dragging pins node, others move around it
- [x] Selection works (click node, white box-shadow, DetailsPanel syncs)
- [x] Change state borders visible (green/yellow/red/purple)
- [ ] Viewport tracks changed nodes during simulation
- [x] Old Cytoscape code removed (GraphOld.tsx, useColorMapsForGraphOnly, useGraphLayout.ts)
- [ ] Documentation updated
