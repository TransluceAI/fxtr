
# Goal: Improve consistency of drag interactions.

Current state:
- When dragging a DraggableID:
    - dragging moves the entity to a new panel
    - dragging with alt makes a duplicate of the entity in a new panel
- When clicking a ref
    - if a view is open, we jump to it
    - if no view is open, we open one in a preview panel
- HOWEVER: When dragging a ref, or dragging a DraggableID that is nested inside a preview card:
    - we ALWAYS create a duplicate of the entity in a new panel

Desired behavior: We should unify the behavior so that:
- when dragging anything WITHOUT holding alt, we don't make a copy, instead we move. special case: if there is _no_ open view, _then_ we open one, like what happens when you click a ref.
- when dragging WITH holding alt, we _always_ open a new view even if one exists, _without_ moving.

This means that if you drag a ref or a preview-anchored DraggableID _without_ holding alt, and drop it into a panel, we need to perform a _move_. To do this we should find whatever other view we would have found if a ref was clicked, and then move _that_ view into the desired location as if it was the one being dragged.

If you drag a ref or a preview-anchored DraggableID _while_ holding alt, it should have the same behavior as now: it should create a new view. This version should not do the duplication context handling in this case, because we aren't copying any specific view, we are just making a new view of the entity. (Dragging a view-anchored DraggableID _should_ engage duplication context handling, the same way it does now, because there we have an explicit view we are duplicating.)

---

## Implementation Plan

### Summary
The key change is handling `type: 'ref'` drags (from RefLink, InlineRefLink, or hover-preview DraggableIDs) differently based on alt key and whether a view exists.

### Current vs Desired Behavior

| Drag source | Without alt (current) | Without alt (desired) | With alt (unchanged) |
|-------------|----------------------|----------------------|---------------------|
| View DraggableID | Move view | Move view | Duplicate view (with children) |
| Ref / Preview DraggableID | Create new view | **Move existing view OR create new if none** | Create new view (no dup context) |

### Changes Required

**1. ViewRegistry.ts** - Add new method:

Add `handleRefDrop(entityId, position, isAltHeld)` to the interface and implementation:

```typescript
// Interface addition (~line 193):
handleRefDrop(entityId: EntityId, position?: PanelPosition, isAltHeld?: boolean): void;

// Implementation:
function handleRefDrop(entityId: EntityId, position?: PanelPosition, isAltHeld: boolean = false): void {
  if (isAltHeld) {
    // Alt+drag: always create fresh new view (no duplication context)
    createNewViewInPanel(entityId, position);
    return;
  }

  // No alt: try to move existing view, or create new if none exists
  const existingViews = getViewsForEntity(entityId);
  if (existingViews.length > 0) {
    // Move the best view (same logic as click-to-jump)
    const viewToMove = chooseBestViewToJump(existingViews);
    if (viewToMove) {
      moveViewToNewPanel(viewToMove.viewId, position);
      return;
    }
  }

  // No existing view - create new
  createNewViewInPanel(entityId, position);
}
```

**2. RenderManager.tsx** - Update onDidDrop handler:

Change the ref handling in `onDidDrop` (~line 136-139):

```typescript
// Before:
if (dragData.type === 'ref') {
  viewRegistry.createNewViewInPanel(dragData.entityId, position);
  return;
}

// After:
if (dragData.type === 'ref') {
  viewRegistry.handleRefDrop(dragData.entityId, position, event.nativeEvent.altKey);
  return;
}
```

### Files Changed
1. `ts/src/render-manager/ViewRegistry.ts` - Add `handleRefDrop` method
2. `ts/src/render-manager/RenderManager.tsx` - Use `handleRefDrop` for ref drops

### Testing Scenarios
1. **Drag ref (no view exists, no alt)**: Should create new view ✓
2. **Drag ref (view exists, no alt)**: Should move existing view to drop location
3. **Drag ref (view exists, alt)**: Should create fresh new view (not duplicate existing)
4. **Drag hover-preview DraggableID (view exists, no alt)**: Should move existing view
5. **Drag in-view DraggableID (no alt)**: Should move that specific view (unchanged)
6. **Drag in-view DraggableID (alt)**: Should duplicate with children state (unchanged)
