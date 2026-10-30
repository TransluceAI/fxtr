# Phase 2b DND - Scratch Notes

## Understanding the Current System

### Existing ViewRegistry Methods
- `moveViewToSlot(viewId, targetSlotId)` - Moves a view to be owned by a slot (closes panel if was panel-owned)
- `moveViewToNewPanel(viewId, direction)` - Moves slot-owned view to new panel relative to active panel
- `duplicateViewInPanel(sourceViewId, direction)` - Duplicates view with children to new panel
- `createNewViewInPanel(entityId, direction)` - Creates fresh view (no child duplication) for refs

### Dockview DND API (from example)
1. **`onUnhandledDragOverEvent`** - Called when external drag hovers over dockview
   - Must call `event.accept()` to enable drop zones
2. **`onDidDrop`** - Called when drop occurs
   - Provides `event.position`, `event.group` for positioning
   - Need `positionToDirection()` helper to convert position

### Key Insight: Two Drop Targets
1. **Dockview panels** - Creates/moves view into a new panel at drop position
2. **Placeholder slots** - Moves view into the slot (no panel created)

## Data Transfer Format

Need to encode what's being dragged:

```typescript
type DragData =
  | { type: 'ref'; entityId: EntityId }  // Reference link - always creates new view
  | { type: 'view'; viewId: ViewId; entityId: EntityId }  // Existing view - move or duplicate
```

Serialize to JSON in dataTransfer with a custom MIME type: `application/x-fxtr-drag`

## Component Design: DraggableID

Replace the hardcoded entity ID display in EntityRenderer header with:

```tsx
<DraggableID entityId={entityId} viewId={viewId} />
```

This component:
1. Shows entity ID (truncated)
2. Has `draggable={true}`
3. Sets up dataTransfer on dragStart
4. Changes cursor to copy (+) when alt held

For RefLink, similar but with `type: 'ref'` (no viewId).

## Dockview Integration

### onUnhandledDragOverEvent
```typescript
api.onUnhandledDragOverEvent((event) => {
  // Check if this is our drag format
  const types = event.nativeEvent.dataTransfer?.types ?? [];
  if (types.includes('application/x-fxtr-drag')) {
    event.accept();
  }
});
```

### onDidDrop
```typescript
const onDidDrop = (event: DockviewDidDropEvent) => {
  const data = parseDragData(event.nativeEvent.dataTransfer);
  if (!data) return;

  const isAltHeld = event.nativeEvent.altKey;
  const position = {
    direction: positionToDirection(event.position),
    referenceGroup: event.group ?? undefined,
  };

  if (data.type === 'ref') {
    // Always create new view at position
    viewRegistry.createNewViewInPanelAtPosition(data.entityId, position);
  } else {
    // data.type === 'view'
    if (isAltHeld) {
      // Duplicate with children
      viewRegistry.duplicateViewInPanelAtPosition(data.viewId, position);
    } else {
      // Move the view
      viewRegistry.moveViewToPanelAtPosition(data.viewId, position);
    }
  }
};
```

## ViewRegistry Changes

Current methods use `PanelDirection` relative to active panel. DND needs absolute positioning with group reference.

### Option A: New position parameter type
```typescript
type DropPosition = {
  direction: 'left' | 'right' | 'above' | 'below' | 'within';
  referenceGroup?: DockviewGroupPanel;
};
```

Add new methods that take DropPosition, or overload existing ones.

### Option B: Expose dockviewApi
Let the drop handler build the full addPanel config and call it directly after ViewRegistry creates the view.

**Prefer Option A** - keeps dockview details encapsulated in ViewRegistry.

## Placeholder Drop Handling

Placeholder needs to:
1. Accept drops via `onDragOver` / `onDrop` handlers
2. Show visual feedback when dragging a matching view over it
3. On drop: call `viewRegistry.moveViewToSlot(viewId, slotId)`

### Visual Feedback
- Highlight border when valid drop (same entityId)
- Different highlight when dropping causes duplicate (alt held)

### Edge Cases
- Dropping a ref onto placeholder: Should this work? Probably not - refs don't have a view to move
- Dropping view of different entityId: Ignore/reject

## Alt Key Detection

### During drag (for cursor)
Listen to keydown/keyup on window, track alt state.
Or: Check `event.altKey` on each dragover.

### During drop
Use `event.nativeEvent.altKey` in onDidDrop handler.

### Cursor Display
Set `event.dataTransfer.dropEffect = 'copy'` when alt held, `'move'` otherwise.

## Implementation Steps

1. Define DragData type and serialize/parse helpers
2. Create DraggableID component
3. Update EntityRenderer header to use DraggableID
4. Update RefLink to be draggable
5. Add dockview DND handlers in RenderManager
6. Add DropPosition type and new ViewRegistry methods
7. Update Placeholder to accept drops
8. Test all scenarios:
   - Ref drag → drop as panel
   - View drag → drop as panel (move)
   - View drag → drop as panel with alt (duplicate)
   - View drag → drop on placeholder (move)
   - View drag → drop on placeholder with alt (duplicate into slot? or reject?)

## Questions / Decisions

1. **Duplicate into slot?** When alt+dropping a view onto a placeholder, should we:
   - (a) Create a duplicate view owned by that slot (not a panel)
   - (b) Reject and do nothing
   - (c) Create duplicate in a panel as if dropped there

   Probably (a) - user wants a copy there specifically.

2. **Ref onto placeholder?** When dropping a ref onto a placeholder:
   - The placeholder is for a specific entity
   - The ref might be for a different entity
   - If same entity: Create new view in that slot (like "Open new entity view here")
   - If different: Reject

3. **Multiple views of same entity?** When dragging a view and there are multiple views of that entity:
   - The drag data includes viewId, so we know exactly which view
   - This is already handled

4. **Drag from panel root vs child?** Both should work the same - the viewId identifies which view.

## positionToDirection Helper

Need to implement or borrow from dockview example:

```typescript
function positionToDirection(position: Position): string {
  // Position enum or similar from dockview
  // Returns 'left' | 'right' | 'above' | 'below' | 'center'
}
```

Check dockview typings for the exact type.
