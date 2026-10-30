# Phase 2f Scratch Notes: Drag Consistency

## Current State Analysis

### Drag Data Types
Two drag data types exist (`ViewRegistry.ts:44-48`):
- `type: 'ref'` - just `{ entityId }` - used by refs and hover preview DraggableIDs
- `type: 'view'` - `{ viewId, entityId }` - used by in-view DraggableIDs

### What Sets Each Type

**DraggableID** (`DraggableID.tsx:43-56`):
- If `canMove` (has viewId AND not in hover preview): sets `type: 'view'`
- If `!canMove` (in hover preview OR no view context): sets `type: 'ref'`

**RefLink / InlineRefLink** (`EntityRenderer.tsx:113-118, 186-191`):
- Always sets `type: 'ref'`

### Current Drop Handling

**Panel drops** (`RenderManager.tsx:124-151`):
| Drag type | Alt held? | Action |
|-----------|-----------|--------|
| ref | either | `createNewViewInPanel` (always makes new view) |
| view | no | `moveViewToNewPanel` (moves the view) |
| view | yes | `duplicateViewInPanel` (copies view + children state) |

**Placeholder drops** (`Child.tsx:88-107`):
- Only accepts `type: 'view'` drags matching the placeholder's entityId
- Without alt: `moveViewToSlot`
- With alt: `duplicateViewInSlot`

## Desired Behavior

### Panel drops (new behavior for refs):
| Drag type | Alt held? | Action |
|-----------|-----------|--------|
| ref | no | **Find existing view and move it, OR create new if none** |
| ref | yes | `createNewViewInPanel` (no duplication context) |
| view | no | `moveViewToNewPanel` (unchanged) |
| view | yes | `duplicateViewInPanel` (unchanged) |

Key insight from spec:
> we should find whatever other view we would have found if a ref was clicked, and then move _that_ view into the desired location as if it was the one being dragged

So ref+no-alt does the same thing as: click ref → view opens/jumps → drag that view to target panel.

### Placeholder drops:
The spec focuses on panel drops. Placeholder behavior likely stays the same since:
1. Placeholders are for owned children (same entity, different location)
2. Refs are for cross-entity references (different entities)
3. You wouldn't typically drag a ref to a placeholder expecting them to match

## Implementation Approach

### Option 1: Add new ViewRegistry method
Add a method that handles ref drop semantics:
```typescript
handleRefDrop(entityId: EntityId, position: PanelPosition, isAltHeld: boolean): void
```

Logic:
- If alt held: `createNewViewInPanel(entityId, position)`
- If not alt:
  - Get views: `getViewsForEntity(entityId)`
  - If views exist: `chooseBestViewToJump(views)` then `moveViewToNewPanel(viewId, position)`
  - If no views: `createNewViewInPanel(entityId, position)`

### Option 2: Handle in RenderManager.onDidDrop
Keep logic in the event handler, but need to expose `getViewsForEntity` (already public) and a way to pick the best view.

**Recommendation**: Option 1 is cleaner - encapsulates the logic in ViewRegistry where related methods live.

## Files to Modify

1. **ViewRegistry.ts**:
   - Add `handleRefDrop(entityId, position, isAltHeld)` method
   - Need to use internal `chooseBestViewToJump` function
   - Add to interface and return object

2. **RenderManager.tsx**:
   - Change `onDidDrop` handler for `type: 'ref'`:
     - Old: `viewRegistry.createNewViewInPanel(dragData.entityId, position)`
     - New: `viewRegistry.handleRefDrop(dragData.entityId, position, event.nativeEvent.altKey)`

## Edge Cases to Consider

1. **Multiple views exist**: Use `chooseBestViewToJump` logic (prefers active panel, then visible panels, then any)

2. **Dragged from hover preview of an existing view**: Still works - it's a ref drag, so we find the view and move it

3. **Dragged from hover preview when no view exists**: Creates a new view (same as clicking the ref)

4. **Alt+drag when views exist**: Creates a FRESH new view (not duplicating any specific view) - this is different from view+alt which duplicates with children state

5. **Moving a view that's slot-owned**: `moveViewToNewPanel` handles this - detaches from slot, creates panel

## Questions for User

None identified - the spec seems clear on the desired behavior.
