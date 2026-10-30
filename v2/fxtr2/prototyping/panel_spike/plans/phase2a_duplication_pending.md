# Phase 2a: Duplication with Child Preservation

## Goal

When duplicating a view, also duplicate any child views it owned (recursively), so the new view is a true copy with its own independent child views.

## Current Behavior

1. User right-clicks view V1 (entity E1) and selects "duplicate right"
2. `createNewViewInPanel(E1, 'right')` creates new view V2 for E1
3. V2 renders, its `<Child entityId={E2}>` components mount
4. Each Child calls `registerSlotInternal(slotId, E2, V2)`
5. Since a view already exists for E2, the slot becomes a placeholder

## Desired Behavior

Step 5 should instead create a NEW view for E2 (a duplicate), not become a placeholder.

## Data Structure

```typescript
// Map: new parent view ID -> (child entity ID -> old child view ID)
const duplicatingViews = new Map<ViewId, Map<EntityId, ViewId>>();
```

This tracks which children should be duplicated when they mount.

## Algorithm

### Setting up duplication context

When duplicating view `oldViewId` to create `newViewId`:

```typescript
function setupDuplicationContext(oldViewId: ViewId, newViewId: ViewId) {
  const childMap = new Map<EntityId, ViewId>();

  // Find all slots owned by the old view
  for (const slot of state.slots.values()) {
    if (slot.parentViewId === oldViewId) {
      // Find if this slot owns a view
      const ownedView = Array.from(state.views.values())
        .find(v => v.ownerSlotId === slot.slotId);
      if (ownedView) {
        childMap.set(slot.entityId, ownedView.viewId);
      }
    }
  }

  if (childMap.size > 0) {
    duplicatingViews.set(newViewId, childMap);
  }
}
```

### In registerSlotInternal

Before the normal "first slot vs placeholder" logic:

```typescript
// Check for duplication context
const dupContext = duplicatingViews.get(parentViewId);
if (dupContext?.has(entityId)) {
  const oldChildViewId = dupContext.get(entityId)!;

  // Remove from pending (cleanup)
  dupContext.delete(entityId);
  if (dupContext.size === 0) {
    duplicatingViews.delete(parentViewId);
  }

  // Create new view (duplicate)
  const viewId = generateViewId();
  const portalNode = createHtmlPortalNode();
  const view: View = {
    viewId,
    entityId,
    portalNode,
    ownerSlotId: slotId,
    panelId: null,
  };

  // Recursively set up duplication for grandchildren
  setupDuplicationContext(oldChildViewId, viewId);

  // Add view to state...
  return { viewCreated: true };
}
```

### User-triggered duplication

When user explicitly duplicates via context menu:

1. Context menu already knows which view it's acting on (via `context.viewId`)
2. Call `duplicateViewInPanel(sourceViewId, entityId, direction)` - new method
3. This method:
   - Clears all entries in `duplicatingViews` (cleanup stale entries)
   - Creates the new view
   - Calls `setupDuplicationContext(sourceViewId, newViewId)`

Note: `createNewViewInPanel` remains for "open" actions (first view for an entity) - no duplication context needed there.

## Edge Cases

1. **No existing view to duplicate from**: First view for entity, no duplication context needed
2. **Old view had no owned children**: `childMap` is empty, nothing added to `duplicatingViews`
3. **Interrupted duplication** (panel closed before children mount): Stale entries cleaned up on next explicit duplication
4. **Deeply nested children**: Recursive `setupDuplicationContext` handles this

## Changes Required

1. Add `duplicatingViews` map to ViewRegistry state
2. Add `setupDuplicationContext` helper function
3. Modify `registerSlotInternal` to check duplication context
4. Add new `duplicateViewInPanel(sourceViewId, entityId, direction)` method
5. Update context menu handlers to call `duplicateViewInPanel` instead of `createNewViewInPanel` for duplicate actions
6. Optionally: clean up `duplicatingViews` entries when views are destroyed

## Clarifications

- `moveViewToNewPanel`: No duplication (it's a move, not copy)
- `replacePlaceholderWithNewView`: No duplication (creating fresh, not copying)
- `createNewViewInPanel`: No duplication (for "open" when no view exists yet)
- `duplicateViewInPanel` (new): Yes, sets up duplication context

## Context Menu Changes

Need to check which contexts have `viewId` available:
- `child-owning`: has `viewId` ✓ - use for "Duplicate entity view"
- `root`: has `viewId` ✓ - use for "Duplicate entity view"
- `ref`: no `viewId` - need to find/add one, or call `createNewViewInPanel` (no duplication for refs since we're opening from a reference, not from an existing view)
