# Phase 2a: Close Subview

## Goal

Add a context menu option for child-owning views to close the subview and convert it to a placeholder pointing to another existing view.

## Use Case

User has duplicated a view hierarchy. They want to close one of the nested subviews without closing the whole panel. The subview should become a placeholder that points to the remaining copy.

## UI

- Menu item: "Close this subview (point to external copy)"
- Location: Bottom of `child-owning` context menu
- Condition: Only show if entity has other views (so we're not closing the last one)

## ViewRegistry Changes

New method: `closeSubview(viewId: ViewId): boolean`

```typescript
function closeSubview(viewId: ViewId): boolean {
  const view = state.views.get(viewId);
  if (!view) return false;

  // Must be slot-owned (not a panel root)
  if (!view.ownerSlotId) return false;

  const entityId = view.entityId;

  // Must have other views (don't close the last one)
  const allViews = getViewsForEntity(entityId);
  if (allViews.length <= 1) return false;

  // Destroy the view
  const newViews = new Map(state.views);
  newViews.delete(viewId);
  state = { ...state, views: newViews };

  notifyViews();
  notifySlotsForEntity(entityId); // Slot will re-render as placeholder

  return true;
}
```

## Behavior

1. View is removed from `state.views`
2. Slot remains in `state.slots` (unchanged)
3. `getSlotContentSnapshot` is called on re-render:
   - No owned view found for slot
   - Finds another existing view for entity
   - Returns `{ type: 'placeholder', viewId: existingView.viewId }`
4. Child component renders as placeholder with "click to jump" UI

## Context Menu Changes

In `ViewContextMenu.tsx`:

```typescript
// Handler
const handleCloseSubview = useCallback(() => {
  if (context.type !== 'child-owning') return;
  viewRegistry.closeSubview(context.viewId);
}, [context, viewRegistry]);

// Check if can close (has other views)
const canCloseSubview = context.type === 'child-owning'
  && viewRegistry.getViewsForEntity(context.entityId).length > 1;

// In JSX (child-owning context, after DirectionGroups)
{canCloseSubview && (
  <>
    <Separator />
    <MenuItem onClick={handleCloseSubview}>
      Close this subview (point to external copy)
    </MenuItem>
  </>
)}
```

## Edge Cases

1. **Last view**: Menu item hidden, can't close
2. **Panel-owned view**: This menu item only appears in `child-owning` context, not `root`
3. **Recursive children**: When view is destroyed, its owned slots are unregistered by React cleanup, which triggers normal `unregisterSlotInternal` logic for any grandchildren

## Why Children Are Handled Automatically

When we destroy view V1, we don't need special logic for V1's children because:

1. Destroying V1 causes its OutPortal content to unmount
2. Any `<Child>` components inside V1 unmount, triggering their cleanup
3. Cleanup calls `unregisterSlotInternal` for each child slot
4. `unregisterSlotInternal` already has the right logic:
   - If child view is the **last view** for its entity → rebind to another slot (if available)
   - If child view is **not the last** → destroy it (other copies exist)

This cascades recursively through all descendants. The existing rebind-on-last-view logic ensures we never accidentally lose the only view of an entity - it gets preserved by rebinding to a slot elsewhere in the tree.
