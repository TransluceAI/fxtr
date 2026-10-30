# Phase 2 Step 19D: Graph Interactivity and Cleanup

Final phase of the React Flow migration. Add deferred interactivity features and clean up.

## Goal

- Alt+drag on graph nodes to drag-to-panel (like NodeRef)
- Hover preview cards on graph nodes (like NodeRef)
- Verify/clean up any remaining Cytoscape code
- Final testing and documentation pass

---

## Implementation Steps

### 1. Alt+drag for drag-to-panel

**Problem**: React Flow uses mouse dragging for node movement. We want Alt+drag to trigger our drag-to-panel behavior instead.

**Approach**: Track Alt key state at the Graph level. When Alt is held:
1. Set React Flow's `nodesDraggable={false}` to disable React Flow dragging
2. Enable HTML drag on nodes
3. Show a tooltip hint on hovered nodes

**Files:**
- `thingwatcher/frontend/components/Graph/Graph.tsx` - Alt key tracking, pass to ReactFlow
- `thingwatcher/frontend/components/Graph/ThingNode.tsx` - HTML drag handler, tooltip

**Implementation - Graph.tsx:**

```tsx
function GraphInner({ ... }: GraphProps) {
  const [altHeld, setAltHeld] = useState(false);

  // Track Alt key state at Graph level
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Alt') setAltHeld(true);
    };
    const handleKeyUp = (e: KeyboardEvent) => {
      if (e.key === 'Alt') setAltHeld(false);
    };
    // Also reset on blur (in case Alt is released while window unfocused)
    const handleBlur = () => setAltHeld(false);

    window.addEventListener('keydown', handleKeyDown);
    window.addEventListener('keyup', handleKeyUp);
    window.addEventListener('blur', handleBlur);
    return () => {
      window.removeEventListener('keydown', handleKeyDown);
      window.removeEventListener('keyup', handleKeyUp);
      window.removeEventListener('blur', handleBlur);
    };
  }, []);

  return (
    <ReactFlow
      // Disable React Flow's node dragging when Alt is held
      nodesDraggable={!altHeld}
      ...
    />
  );
}
```

**Implementation - ThingNode.tsx:**

Need to pass `altHeld` to ThingNode. Options:
- Add to node data (requires updating node data when altHeld changes)
- Use a context
- Use a global store/signal

Context is cleanest:

```tsx
// In Graph.tsx - create and provide context
const AltKeyContext = createContext(false);

// In ThingNode.tsx
function ThingNode({ data }: NodeProps<ThingNodeType>) {
  const altHeld = useContext(AltKeyContext);
  const [hovered, setHovered] = useState(false);

  // HTML drag handler (same as NodeRef)
  const handleDragStart = useCallback((e: React.DragEvent) => {
    e.dataTransfer.setData(DRAG_MIME_THING, data.thingKey);
    e.dataTransfer.setData('text/plain', data.thingKey);
    e.dataTransfer.effectAllowed = 'copy';
  }, [data.thingKey]);

  return (
    <div
      className={classNames.join(' ')}
      draggable={altHeld}
      onDragStart={altHeld ? handleDragStart : undefined}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      {/* Tooltip hint when Alt held and hovered */}
      {altHeld && hovered && (
        <div className={styles.dragHint}>
          Drag to open in panel
        </div>
      )}

      <DynamicGraphNodeRenderer ... />
    </div>
  );
}
```

**Styling for drag hint (Graph.module.css):**

```css
.dragHint {
  position: absolute;
  bottom: 100%;
  left: 50%;
  transform: translateX(-50%);
  margin-bottom: 4px;
  padding: 4px 8px;
  background: rgba(0, 0, 0, 0.8);
  color: white;
  font-size: 11px;
  border-radius: 4px;
  white-space: nowrap;
  pointer-events: none;
  z-index: 10;
}
```

**Key points:**
- Alt key tracking at Graph level (single listener)
- React Flow's `nodesDraggable` prop controls whether nodes are draggable
- Context passes altHeld to ThingNode components
- ThingNode enables HTML drag when altHeld
- Tooltip appears above node when Alt held + hovered
- Reset altHeld on window blur (handles Alt released while unfocused)

- [x] Add Alt key tracking to Graph.tsx
- [x] Create AltKeyContext and provide it around ReactFlow
- [x] Pass `nodesDraggable={!altHeld}` to ReactFlow
- [x] Update ThingNode to consume AltKeyContext
- [x] Add HTML drag handler to ThingNode
- [x] Add hover state tracking to ThingNode
- [x] Add drag hint tooltip (styled, positioned above node)
- [x] Test: Alt+drag node to panel opens it
- [x] Test: Tooltip appears on hover while Alt held

### 2. Hover preview cards on graph nodes

**Files:**
- `thingwatcher/frontend/components/Graph/ThingNode.tsx`

**Implementation:**

Wrap ThingNode content in HoverPreviewCard:

```tsx
import { HoverPreviewCard } from '../../lib/HoverPreview';

function ThingNode({ data }: NodeProps<ThingNodeType>) {
  // ... existing code ...

  return (
    <HoverPreviewCard thingKey={data.thingKey}>
      <div className={classNames.join(' ')} ...>
        <DynamicGraphNodeRenderer ... />
      </div>
    </HoverPreviewCard>
  );
}
```

**Considerations:**
- HoverPreviewCard uses mouse enter/leave events with a delay
- Should work fine with React Flow's pan/zoom since those use different events
- Preview card renders in a portal, won't interfere with graph layout

- [x] Import HoverPreviewCard in ThingNode
- [x] Wrap node content with HoverPreviewCard
- [x] Test: hover over node shows preview card
- [x] Test: hover doesn't interfere with click/drag

### 3. Verify/remove Cytoscape code

Search for any remaining Cytoscape references:

```bash
# Search for cytoscape imports/references
grep -r "cytoscape" --include="*.ts" --include="*.tsx" --include="*.json"
grep -r "Cytoscape" --include="*.ts" --include="*.tsx"
```

**Expected cleanup:**
- Remove any Cytoscape imports
- Remove Cytoscape from package.json dependencies
- Remove any unused GraphOld.tsx or similar files
- Remove useColorMapsForGraphOnly if still present

- [x] Search codebase for Cytoscape references
- [x] Remove any found references (only in docs/plans - OK to leave as historical)
- [x] Remove Cytoscape from package.json if present (already removed)
- [x] Verify build still works

### 4. Verify Phase C functionality

Test items from Phase C that weren't fully verified:

- [x] Fallback works for types without graph renderer (add a thing type with no graph renderer)
- [x] Change state styling on custom graph nodes (green/yellow/red borders)
- [x] Selection styling on custom graph nodes (white box-shadow)
- [x] Custom node sizes respected by cola layout (no overlapping)
- [x] targetPort picker function form works (test with function instead of string)

### 5. Final documentation pass

**Files:**
- `CODE_OVERVIEW.md` - mention Registry pattern
- `thingwatcher/frontend/LOCAL_CODE_OVERVIEW.md` - ensure Graph section is complete
- Review inline docs in new files

- [x] Add Registry pattern to CODE_OVERVIEW.md (lib/Registry.tsx, docs/registry_pattern.md)
- [x] Review LOCAL_CODE_OVERVIEW.md Graph section
- [x] Verify thingwatcher-bindings.d.ts docs are complete

### 6. Update main plan checklist

**Files:**
- `plans/phase2_step19_reactflow.md`

- [x] Mark Phase D subplan as complete
- [x] Check off any remaining items in main plan

---

## Technical Notes

### Alt Key Detection

Using window-level keydown/keyup listeners at the Graph level ensures we catch Alt regardless of focus. The state is shared via context to all ThingNode components.

Important: Also listen for window `blur` event to reset altHeld - this handles the case where the user Alt-tabs away and releases Alt while the window is unfocused.

### nodesDraggable and panOnDrag Props

React Flow's `nodesDraggable` and `panOnDrag` props globally control whether nodes can be dragged and whether the canvas can be panned. By setting both to `!altHeld`, we disable React Flow's drag/pan handling when Alt is held, allowing our HTML drag to work.

### Alt Key Detection During HTML Drag

During an HTML drag operation, the browser enters a special "drag mode" where normal keyboard events (keydown/keyup) may not fire to window listeners. To detect Alt key state changes during drag:

- Listen for the `drag` event on window (fires repeatedly during drag)
- Check `e.altKey` on each drag event to sync the actual modifier state
- Also listen for `blur` to reset when window loses focus

```typescript
const handleDrag = (e: DragEvent) => setAltHeld(e.altKey);
window.addEventListener('drag', handleDrag);
```

This ensures that releasing Alt mid-drag is detected immediately.

### HoverPreviewCard Positioning

HoverPreviewCard uses a portal and positions itself relative to the hovered element. It should work fine with React Flow's coordinate system since it uses screen coordinates.

---

## Exit Criteria

- [x] Alt+drag on graph nodes triggers drag-to-panel (opens in dockview)
- [x] Hovering over graph nodes shows preview card
- [x] No Cytoscape code/dependencies remain (only historical mentions in plans/specs)
- [x] All Phase C test items verified
- [x] Documentation complete (CODE_OVERVIEW.md, LOCAL_CODE_OVERVIEW.md)
- [x] Main plan (phase2_step19_reactflow.md) fully checked off
