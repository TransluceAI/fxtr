# Phase 2 Step 11 Part 1: Drag IDs and refs to spawn new panels

## Overview

Implement the ability to drag thing IDs and refs from renderers and drop them onto the dockview layout to create a new panel containing that single thing. This is the first UI feature from the panel_spike prototype being ported to thingwatcher.

## User Decisions

- **Drop behavior**: Always create a new panel, even if one already exists for that thing key
- **Alt modifier**: Not supported - simple drag-to-open only
- **Position handling**: Respect drop position to create panel where dropped (left/right/above/below)
- **Context menu**: Keep existing "focus if exists" behavior - only drag creates duplicates

## Current State

### What exists

1. **DraggableIDMarker** (`lib/DraggableIDMarker.tsx`): A stub component that wraps children and makes them draggable. Sets `application/x-thingwatcher-key` MIME type with the thing key. Currently non-functional for drops.

2. **NodeRef** (`lib/ThingWatcherBindings.tsx`): Renders clickable refs to things. Already draggable with same MIME type. Clicking navigates; right-click opens context menu.

3. **DockviewLayout** (`components/DockviewLayout/DockviewLayout.tsx`): Sets up DockviewReact with panel components. Does not currently handle external drops.

4. **openThingPanel** (`components/DockviewLayout/DockviewContext.tsx`): Helper that creates a ThingViewPanel for a thing key. Currently focuses existing panel if one exists (we'll change this).

### Panel_spike reference

The panel_spike uses:
- `serializeDragData` / `parseDragData` for structured drag payloads
- `onUnhandledDragOverEvent` on DockviewReact to accept external drags
- `onDidDrop` to handle the drop and create/move panels
- `positionToDirection` to convert drop position to direction

## Design

### Drag Data Format

We'll use a simple approach: a single MIME type with the thing key as data.

```typescript
// MIME types for drag operations
const DRAG_MIME_THING = 'application/x-thingwatcher-thing';

// The drag payload is just the thing key (b64 string)
// dataTransfer.setData(DRAG_MIME_THING, thingKey);
```

This is simpler than panel_spike's approach because:
- We don't have the concept of moving views between slots (yet)
- We always create new panels
- We don't need view IDs for this feature

### Components to Modify

#### 1. DraggableIDMarker.tsx

Update to use our standardized MIME type:

```typescript
const handleDragStart = (e: React.DragEvent) => {
  e.dataTransfer.setData(DRAG_MIME_THING, thingKey);
  e.dataTransfer.setData('text/plain', thingKey);
  e.dataTransfer.effectAllowed = 'copy';
};
```

The component is already structured correctly - just need to ensure consistent MIME type.

#### 2. NodeRef in ThingWatcherBindings.tsx

Already draggable. Update to use the same MIME type for consistency:

```typescript
const handleDragStart = (e: React.DragEvent) => {
  e.dataTransfer.setData(DRAG_MIME_THING, thing.key);
  e.dataTransfer.setData('text/plain', thing.key);
  e.dataTransfer.effectAllowed = 'copy';
};
```

#### 3. DockviewContext.tsx

Add a new function `openThingPanelWithPosition` that:
- Always creates a new panel (even if one exists for this key)
- Accepts an optional position parameter

```typescript
export function openThingPanelWithPosition(
  api: DockviewApi,
  key: string,
  position?: {
    referenceGroup?: DockviewGroupPanel;
    direction?: 'left' | 'right' | 'above' | 'below';
  }
): void {
  // Generate unique panel ID with timestamp to allow duplicates
  const panelId = `thing-view-${key}-${Date.now()}`;

  api.addPanel({
    id: panelId,
    component: 'thingView',
    title: 'Thing View',
    params: { key },
    position: position?.referenceGroup
      ? {
          referenceGroup: position.referenceGroup,
          direction: position.direction,
        }
      : undefined,
  });
}
```

#### 4. DockviewLayout.tsx

Add handlers for external drops:

```typescript
// In onReady callback, add:
event.api.onUnhandledDragOverEvent((e) => {
  // Check if this is our drag type
  const types = Array.from(e.nativeEvent.dataTransfer?.types ?? []);
  if (types.includes(DRAG_MIME_THING)) {
    e.accept();
  }
});

// Add onDidDrop prop to DockviewReact:
const onDidDrop = useCallback((event: DockviewDidDropEvent) => {
  const thingKey = event.nativeEvent.dataTransfer?.getData(DRAG_MIME_THING);
  if (!thingKey) return;

  const direction = positionToDirection(event.position);
  openThingPanelWithPosition(apiRef.current!, thingKey, {
    referenceGroup: event.group ?? undefined,
    direction: direction === 'within' ? undefined : direction,
  });
}, []);
```

### Shared Constants

Create a new file `lib/dragConstants.ts` (or add to existing file) with:

```typescript
export const DRAG_MIME_THING = 'application/x-thingwatcher-thing';
```

## Implementation Steps

- [x] 1. Create `lib/dragConstants.ts` with the MIME type constant
- [x] 2. Update `DraggableIDMarker.tsx` to use the standardized MIME type
- [x] 3. Update `NodeRef` in `ThingWatcherBindings.tsx` to use the standardized MIME type
- [x] 4. Add `openThingPanelWithPosition` to `DockviewContext.tsx` (keep existing `openThingPanel` for context menu usage)
- [x] 5. Update `DockviewLayout.tsx`:
   - Add `onUnhandledDragOverEvent` handler in `onReady`
   - Add `onDidDrop` callback
   - Pass `onDidDrop` to DockviewReact
- [ ] 6. Test manually:
   - Drag from DraggableIDMarker (custom renderer's ID) to dockview → creates new panel
   - Drag NodeRef to dockview → creates new panel
   - Drop on different edges (left/right/above/below) → panel appears in correct position
   - Multiple drops for same thing → creates multiple panels
- [x] 7. Update CODE_OVERVIEW.md

## Files Changed

- `lib/dragConstants.ts` (new)
- `lib/DraggableIDMarker.tsx`
- `lib/ThingWatcherBindings.tsx`
- `components/DockviewLayout/DockviewContext.tsx`
- `components/DockviewLayout/DockviewLayout.tsx`
- `CODE_OVERVIEW.md`

## Testing Notes

Manual testing is sufficient for this feature. Key scenarios:
1. Drag a DraggableIDMarker from a custom renderer to an empty area
2. Drag a NodeRef from a renderer to a panel edge
3. Verify panel title updates correctly after load
4. Verify dropped panel shows correct thing data
5. Verify dropping same thing twice creates two panels

## Future Considerations

This implementation lays groundwork for future panel_spike features:
- **Phase 2 Step 11 Part 3**: Hover preview cards will use similar drag infrastructure
- **Phase 2 Step 11 Part 2**: InlineNodeRef will also be draggable using same MIME type
- Future ViewRegistry integration: The MIME type can be extended to support view-level operations when we add that system
