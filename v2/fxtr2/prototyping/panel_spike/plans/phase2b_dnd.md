# Drag and drop

The app should support dragging and dropping of entities and references.

Drag-and-drop functionality can be implemented using Dockview's external drag and drop interactions `onDidDrop` and `onUnhandledDragOverEvent` (via `accept`).
example: https://github.com/mathuo/dockview/blob/dbf74c3cbf1e78263e00ea03e3fb5e425c8f47ea/
docs: packages/docs/templates/dockview/dnd-external/react/src/app.tsx#L116

We will need to create a new component <MyID /> which emits the ID of the currently-rendered element. All entity renderings should use this to render their ID.

Dragging references:
- When you drag a reference or link, it should be droppable as a panel. When dropped, we create a new panel for the referenced object as if we had used the "handleOpenInNewTab" action, but into the dropped location.
- When you drag the ID of an entity (from either a child rendering or from a top-level rendering):
    - You can drag and drop it into a placeholder that matches the same entity ID. This should be shown visually by highlighting the placeholder when the drop goes over it.
    - Otherwise, you can drag and drop it as a panel in Dockview. This acts like you _moved_ the view of this entity into a new panel at that location.
- If you press and hold "option / alt" while you drop the ID of an entity, it has the same behavior except (a) the mouse cursor shows a plus icon, (b) when you drop, it _duplicates_ the entity into a new panel using the existing duplication context system.

---

## Implementation Plan

### 1. Data Types

```typescript
// Drag payload encoded in dataTransfer
type DragData =
  | { type: 'ref'; entityId: EntityId }     // From RefLink - creates new view
  | { type: 'view'; viewId: ViewId; entityId: EntityId }  // From rendered entity - move/duplicate

// MIME type for our drag data
const DRAG_MIME = 'application/x-fxtr-drag';
```

### 2. New Components

**DraggableID** - Replaces inline entity ID in EntityRenderer header:
- `draggable={true}`
- `onDragStart`: Sets `dataTransfer` with serialized DragData (`type: 'view'`)
- `onDrag`: Updates `dropEffect` based on alt key (`'copy'` vs `'move'`)
- Renders entity ID (truncated) with drag handle styling

### 3. Component Changes

**EntityRenderer.tsx**:
- Header uses `<DraggableID entityId={entityId} viewId={viewId} />` instead of plain text

**RefLink** (in EntityRenderer.tsx):
- Add drag handlers with `type: 'ref'` payload (separate from DraggableID)
- Refs can only drop to dockview panels, not placeholders

**Placeholder** (in Child.tsx):
- Add `onDragOver`, `onDragEnter`, `onDragLeave`, `onDrop` handlers
- Only accept drops where `dragData.type === 'view'` AND `dragData.entityId === placeholder.entityId`
- Visual feedback: highlight border when valid drop target
- On drop: `viewRegistry.moveViewToSlot(dragData.viewId, slotId)`
- Alt+drop: `viewRegistry.duplicateViewInSlot(sourceViewId, slotId)` (new method)

**RenderManager.tsx**:
- Add `onUnhandledDragOverEvent` handler calling `event.accept()` for our MIME type
- Add `onDidDrop` handler (see below)

### 4. ViewRegistry Changes

**Unified Position Type** - Refactor existing `getPanelPosition` to use a unified type:

```typescript
import { positionToDirection } from 'dockview';

// Unified position type for all panel creation/movement
type PanelPosition = {
  referencePanel?: string;           // Panel ID (from context menu direction)
  referenceGroup?: DockviewGroupPanel; // Group (from DND drop)
  direction?: 'left' | 'right' | 'above' | 'below'; // undefined = within/center
};
```

**Refactored methods** - Existing methods now accept optional `PanelPosition`:

```typescript
// Existing (refactored to accept position)
moveViewToNewPanel(viewId: ViewId, position?: PanelPosition): void;
duplicateViewInPanel(sourceViewId: ViewId, position?: PanelPosition): void;
createNewViewInPanel(entityId: EntityId, position?: PanelPosition): void;

// New method for alt+drop on placeholder
duplicateViewInSlot(sourceViewId: ViewId, targetSlotId: SlotId): void;
```

**Helper to build position from context menu**:
```typescript
function getPositionFromDirection(direction?: PanelDirection): PanelPosition | undefined {
  if (!direction || !dockviewApi?.activePanel) return undefined;
  if (direction === 'within') {
    return { referencePanel: dockviewApi.activePanel.id };
  }
  return {
    referencePanel: dockviewApi.activePanel.id,
    direction: direction as 'left' | 'right' | 'above' | 'below',
  };
}
```

**Helper to build position from drop event**:
```typescript
function getPositionFromDrop(event: DockviewDidDropEvent): PanelPosition {
  const dir = positionToDirection(event.position);
  return {
    referenceGroup: event.group ?? undefined,
    direction: dir === 'center' ? undefined : dir,
  };
}
```

### 5. Dockview Drop Handler

```typescript
const onDidDrop = (event: DockviewDidDropEvent) => {
  const json = event.nativeEvent.dataTransfer?.getData(DRAG_MIME);
  if (!json) return;
  const data: DragData = JSON.parse(json);
  const position = getPositionFromDrop(event);

  if (data.type === 'ref') {
    viewRegistry.createNewViewInPanel(data.entityId, position);
  } else if (event.nativeEvent.altKey) {
    viewRegistry.duplicateViewInPanel(data.viewId, position);
  } else {
    viewRegistry.moveViewToNewPanel(data.viewId, position);
  }
};
```

### 6. Implementation Order

1. Add DragData type and DRAG_MIME constant
2. Refactor `PanelPosition` type to support both referencePanel and referenceGroup
3. Import `positionToDirection` from dockview
4. Refactor existing methods to use unified PanelPosition
5. Add `duplicateViewInSlot` method
6. Create DraggableID component
7. Update EntityRenderer header to use DraggableID
8. Add drag handlers to RefLink
9. Add DND handlers to DockviewReact in RenderManager
10. Add drop handling to Placeholder component
11. Test scenarios:
    - Ref → panel (creates new view)
    - View → panel (move)
    - View → panel + alt (duplicate with children)
    - View → placeholder (move)
    - View → placeholder + alt (duplicate into slot)

---

Adjustments:
- dropping a ref should only work if holding option; don't show the dnd panel overlay if not holding option. if dropped without option don't open anything
