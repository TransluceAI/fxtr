# Phase 2 Step 10.1.2: Simplify ThingContextMenu

## Overview

Refactor ThingContextMenu to be a simple wrapper component that any element can use, eliminating the need for parent components to manage context menu state or pass callbacks.

## Goals

1. **ThingContextMenu as a wrapper**: Wraps children and intercepts right-click to show context menu
2. **Panel IDs use string key**: No more shortId in panel IDs
3. **Self-contained**: ThingContextMenu owns `handleOpenInPanel` internally using `useDockviewApiFromContext()`
4. **No groupId**: Panels open in an arbitrary position (simplify for now)
5. **ThingLink owns its own context menu**: Each ThingLink wraps itself in ThingContextMenu

## Current State

- `ThingContextMenu` is a floating menu component that requires:
  - Parent to manage `ThingContextMenuState` (open, x, y, thingKey, shortId, groupId)
  - Parent to pass `onOpenChange` and `onOpenInPanel` callbacks
  - Parent to build lookup maps (key → shortId)
- `GraphPanelWrapper`, `DetailsPanelWrapper`, and `DockviewLayout` each maintain their own context menu state
- `ThingLink` fires `ctx.onContextMenu(event, key, shortId)` upward to be handled by RendererProvider
- Panel IDs use shortId: `thing-view-${shortId}`

## New Design

### ThingContextMenu Interface

```typescript
interface ThingContextMenuProps {
  /** The thing key this menu is for */
  thingKey: string;
  /** Children to wrap - right-click triggers the menu */
  children: React.ReactNode;
}

function ThingContextMenu({ thingKey, children }: ThingContextMenuProps): JSX.Element;
```

Usage:
```tsx
<ThingContextMenu thingKey={thing.key}>
  <div className="node">...</div>
</ThingContextMenu>
```

### Implementation

```tsx
function ThingContextMenu({ thingKey, children }: ThingContextMenuProps) {
  const dockviewApi = useDockviewApiFromContext();

  // If not in a dockview context, just render children unchanged
  if (!dockviewApi) {
    return <>{children}</>;
  }

  const handleOpenInPanel = useCallback(() => {
    openThingPanel(dockviewApi, thingKey);
  }, [dockviewApi, thingKey]);

  return (
    <ContextMenu.Root>
      <ContextMenu.Trigger style={{ display: 'contents' }}>
        {children}
      </ContextMenu.Trigger>
      <ContextMenu.Portal>
        <ContextMenu.Positioner>
          <ContextMenu.Popup className={styles.popup}>
            <ContextMenu.Item onClick={handleOpenInPanel}>
              Open in new panel
            </ContextMenu.Item>
          </ContextMenu.Popup>
        </ContextMenu.Positioner>
      </ContextMenu.Portal>
    </ContextMenu.Root>
  );
}
```

### Panel ID Changes

Change `openThingPanel` to use the full key:

```typescript
// Before
export function openThingPanel(api: DockviewApi, key: string, shortId: number, referenceGroupId?: string)
const panelId = `thing-view-${shortId}`;
title: `Thing #${shortId}`,

// After
export function openThingPanel(api: DockviewApi, key: string)
const panelId = `thing-view-${key}`;
title: `Thing View`,  // Or query for shortId if we want it in title
params: { key },
```

Note: Panel title could stay generic ("Thing View") or we could query for shortId just for display. The key point is the panel ID and params only use `key`.

### ThingLink Changes

ThingLink wraps itself in ThingContextMenu:

```tsx
function ThingLink({ thingKey, keyJSON, shortId, label }: ThingLinkProps) {
  // ... fetch thingInfo as before ...

  return (
    <ThingContextMenu thingKey={thingInfo.key}>
      <span className="thinglink" onClick={handleClick}>
        {displayLabel}
      </span>
    </ThingContextMenu>
  );
}
```

### RendererContextValue Changes

Remove context menu handling from RendererContextValue:

```typescript
// Before
interface RendererContextValue {
  // ...
  onContextMenu: (event, key, shortId) => void;
}

// After
interface RendererContextValue {
  // ...
  // onContextMenu removed
}
```

## Implementation Steps

### Step 1: Update ThingContextMenu component ✅
- [x] Change from `Menu` to `ContextMenu` (base-ui)
- [x] Change interface to wrapper pattern (thingKey + children props)
- [x] Use `useDockviewApiFromContext()` internally
- [x] Return children unchanged if not in dockview context
- [x] Remove `ThingContextMenuState` and `initialContextMenuState` exports

### Step 2: Update openThingPanel ✅
- [x] Change signature to `openThingPanel(api, key)` - remove shortId and groupId params
- [x] Change panel ID to use key: `thing-view-${key}`
- [x] Update panel params to only include `key`
- [x] Update panel title (can be generic "Thing View" or we can query for shortId)

### Step 3: Update ThingViewPanel ✅
- [x] Update to only use `params.key` (remove shortId from params)
- [x] Panel title comes from the thing data after query (updates dynamically)

### Step 4: Remove context menu from Graph entirely ✅
- [x] Remove `onNodeContextMenu` prop from Graph component
- [x] Remove right-click handler from cytoscape nodes

### Step 5: Update GraphPanelWrapper ✅
- [x] Remove context menu state management
- [x] Remove `handleNodeContextMenu` callback
- [x] Remove `handleOpenInPanel` callback
- [x] Remove `ThingContextMenu` render
- [x] Remove `onNodeContextMenu` prop passed to Graph

### Step 6: Update DetailsPanel ✅
- [x] Links/backlinks wrap themselves in ThingContextMenu
- [x] Remove `onLinkContextMenu` prop
- [x] Changed `isLive` prop to `isTriggerable` (pre-computed by caller)

### Step 6b: Add ThingContextMenu to ThingsList ✅
- [x] Wrap each list item in ThingContextMenu
- [x] No other changes needed (ThingsList doesn't currently have context menu support)

### Step 7: Update DetailsPanelWrapper ✅
- [x] Remove context menu state management
- [x] Remove `handleLinkContextMenu` callback
- [x] Remove `handleOpenInPanel` callback
- [x] Remove `ThingContextMenu` render
- [x] Remove `onLinkContextMenu` prop passed to DetailsPanel
- [x] Compute `isTriggerable` and pass to DetailsPanel

### Step 8: Update ThingLink ✅
- [x] Wrap itself in ThingContextMenu
- [x] Remove `handleContextMenu` and `onContextMenu` call

### Step 9: Update RendererContextValue ✅
- [x] Remove `onContextMenu` from interface
- [x] Remove `handleRendererContextMenu` from DockviewLayout
- [x] Remove floating `ThingContextMenu` from DockviewLayout's render

### Step 10: Cleanup ✅
- [x] Remove unused imports
- [x] Update LOCAL_CODE_OVERVIEW.md
- [x] Verify TypeScript compiles
- [x] Also updated EmbedApp.tsx to remove onContextMenu

## Files Modified

1. ✅ `components/ThingContextMenu/ThingContextMenu.tsx` - Complete rewrite to wrapper pattern
2. ✅ `components/ThingContextMenu/index.ts` - Simplified exports
3. ✅ `components/DockviewLayout/DockviewContext.tsx` - NEW: Extracted dockview context to avoid circular imports
4. ✅ `components/DockviewLayout/DockviewLayout.tsx` - Remove context menu handling, use DockviewContext
5. ✅ `components/DockviewLayout/ThingViewPanel.tsx` - Update params usage, compute isTriggerable
6. ✅ `components/DockviewLayout/GraphPanelWrapper.tsx` - Simplified, removed context menu
7. ✅ `components/DockviewLayout/DetailsPanelWrapper.tsx` - Simplified, compute isTriggerable
8. ✅ `components/Graph/Graph.tsx` - Remove onNodeContextMenu prop and handler
9. ✅ `components/DetailsPanel/DetailsPanel.tsx` - Wrap links in ThingContextMenu, isTriggerable prop
10. ✅ `components/ThingsList/ThingsList.tsx` - Wrap list items in ThingContextMenu
11. ✅ `lib/ThingWatcherBindings.tsx` - Update ThingLink, remove onContextMenu from context
12. ✅ `EmbedApp.tsx` - Remove onContextMenu from renderer context
13. ✅ `frontend/LOCAL_CODE_OVERVIEW.md` - Updated docs

## Design Decisions

### Why remove groupId?
The groupId was used to open panels adjacent to the right-clicked element. This adds complexity (parent needs to know its group ID, pass it down, etc.) for marginal benefit. Panels opening in an arbitrary location is fine for now.

### Why remove context menu from Graph?
Graph uses Cytoscape.js which doesn't render React elements for nodes. Integrating the new wrapper-based ThingContextMenu would require either:
- Rendering a floating context menu and managing state (what we're trying to eliminate)
- Some complex integration between Cytoscape events and React

Neither is worth the complexity. Users can still click nodes to select them, then use the DetailsPanel links to open things in new panels.

### Why use ContextMenu instead of Menu?
base-ui's `ContextMenu` is specifically designed for right-click menus with a trigger wrapper pattern. `Menu` is more general-purpose and doesn't have the built-in right-click handling.

### Why ThingLink owns its own context menu?
This is the most self-contained approach. Each ThingLink knows its own key and can handle its own context menu without threading callbacks through multiple layers.

### What about the panel title?
Options:
1. Generic "Thing View" - simplest
2. Query for shortId on panel open - adds a query but shows "#42" in title
3. ThingViewPanel queries and updates its own title - panel updates itself after load

For now, go with option 1 (generic title). We can enhance later if needed.

## Implementation Notes

### Circular Import Resolution
To avoid circular imports between ThingContextMenu and ThingWatcherBindings, we extracted `useDockviewApiFromContext` and `openThingPanel` to a new file `DockviewContext.tsx`. The import chain is now:
- `ThingWatcherBindings.tsx` → `ThingContextMenu.tsx` → `DockviewContext.tsx` (no cycle)
- `DockviewLayout.tsx` imports from `DockviewContext.tsx` and re-exports for convenience

### isLive vs isTriggerable
Changed `DetailsPanel` to receive `isTriggerable` as a pre-computed prop instead of `isLive`. This is cleaner because:
- The caller already has access to `triggerableKeys` and can compute this
- DetailsPanel doesn't need to import `useThingWatcher` anymore (though it still does for chunk resolution)
- More explicit about what the prop represents

### Panel Title Updates
ThingViewPanel updates its title dynamically after the thing query completes using `props.api.setTitle()`. This gives us the best of both worlds - generic initial title, then specific title like "#42 MyThingType" once loaded.

### ThingsList Now Has Context Menu
This was a new addition - ThingsList items now have context menus. This compensates for removing the context menu from the graph, giving users another easy way to open things in panels.


### Also
Fixed ThingView to compute triggerable correctly:

1. Added useThingWatcher import
2. Called useThingWatcher() at the top of the component (before any conditional returns, per React hooks rules)
3. Computed isTriggerable = isLive && triggerableKeys.has(thingData.key)
4. Passed isTriggerable to renderer props instead of hardcoded true

Now nested ThingView components will correctly reflect whether the rendered thing is triggerable based on the current live state and triggerable keys.
