# Phase 2 Step 1: Dockview Panel Layout

## Overview

Switch the thingwatcher frontend from a fixed flex layout to dockview, enabling user-rearrangeable panels with layout persistence.

## Requirements

1. **Three main panels** (Graph, DetailsPanel, ThingsList) become non-closeable dockview panels
2. **Timeline stays outside** dockview (fixed at bottom)
3. **Single dockview instance** - all panels freely arrangeable, can stack in tabs
4. **Right-click context menu**:
   - On nodes in Graph -> opens thing in new panel
   - On link items in DetailsPanel -> opens linked thing in new panel
5. **New panels are closeable** (unlike the three default panels)
6. **Layout persisted** to localStorage
7. **Panel data tracks current tick** (not pinned)
8. **New panel placement**: new tab in a new split of the group that was right-clicked

## Current Architecture

```
App.tsx
├── header (fixed)
├── main
│   ├── graphArea (Graph component)
│   └── sidebar
│       ├── panelTop (DetailsPanel)
│       └── panelBottom (ThingsList)
└── timelineArea (Timeline - stays fixed)
```

## Target Architecture

```
App.tsx
├── header (fixed)
├── dockviewContainer
│   └── DockviewReact
│       ├── GraphPanel (non-closeable)
│       ├── DetailsPanelWrapper (non-closeable) -- shows selectedThing
│       ├── ThingsListPanel (non-closeable)
│       └── ThingViewPanel* (closeable, user-opened)
└── timelineArea (Timeline - stays fixed)
```

## Design Decisions

### Panel Types and IDs

- **Main panels** (non-closeable):
  - `graph-main`: Graph panel
  - `details-main`: DetailsPanel showing selected thing
  - `thingslist-main`: ThingsList panel
- **User-opened panels** (closeable):
  - `thing-view-${shortId}`: Panel pinned to a specific thing

### Component Structure

Rather than heavily refactoring existing components, create thin wrapper components for dockview:

```typescript
// GraphPanelWrapper - wraps Graph for dockview
// DetailsPanelWrapper - wraps DetailsPanel for dockview
// ThingsListPanelWrapper - wraps ThingsList for dockview
// ThingViewPanel - new component for user-opened thing panels
```

### Context Menu

Use base-ui's Menu component for context menus. The menu appears on:
- Right-click on nodes in Graph
- Right-click on link items in DetailsPanel

```typescript
import { Menu } from '@base-ui-components/react/menu';

// Usage pattern - controlled open state with position tracking
const [menuState, setMenuState] = useState<{ open: boolean; x: number; y: number; shortId: number | null }>({
  open: false, x: 0, y: 0, shortId: null
});

// On right-click, capture position and shortId
const handleContextMenu = (e: React.MouseEvent, shortId: number) => {
  e.preventDefault();
  setMenuState({ open: true, x: e.clientX, y: e.clientY, shortId });
};

// Menu structure
<Menu.Root open={menuState.open} onOpenChange={(open) => setMenuState(s => ({ ...s, open }))}>
  <Menu.Positioner side="bottom" align="start" style={{ position: 'fixed', left: menuState.x, top: menuState.y }}>
    <Menu.Popup>
      <Menu.Item onClick={() => openThingPanel(menuState.shortId)}>
        Open in new panel
      </Menu.Item>
    </Menu.Popup>
  </Menu.Positioner>
</Menu.Root>
```

### Layout Persistence

```typescript
const LAYOUT_KEY = 'thingwatcher-dockview-layout';

// Save on layout change
api.onDidLayoutChange(() => {
  const layout = api.toJSON();
  localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout));
});

// Restore on init (with fallback to default layout)
const savedLayout = localStorage.getItem(LAYOUT_KEY);
```

### Default Layout

Graph on left (60%), sidebar split (Details + ThingsList) on right (40%):

```
┌────────────────────┬──────────────────┐
│                    │ [Details]        │
│     [Graph]        ├──────────────────┤
│                    │ [ThingsList]     │
└────────────────────┴──────────────────┘
```

### Non-closeable Implementation

Instead of using dockview's `closeable` API (which had TypeScript compatibility issues), main panels auto-reopen if closed. The `onDidRemovePanel` event listener detects when a main panel is closed and immediately re-adds it.

### Dockview API Context

Create a DockviewContext to share the dockview API with child components:

```typescript
const DockviewContext = createContext<DockviewApi | null>(null);

function useDockviewApi() {
  const api = useContext(DockviewContext);
  if (!api) throw new Error('useDockviewApi must be used within DockviewProvider');
  return api;
}
```

## Implementation Steps

### 1. Set up dockview infrastructure
- [x] Import dockview CSS in main.tsx
- [x] Create `hooks/useDockviewApi.ts` with context
- [x] Create `components/DockviewLayout/` directory structure

### 2. Create panel wrapper components
- [x] Create `components/DockviewLayout/GraphPanelWrapper.tsx`
- [x] Create `components/DockviewLayout/DetailsPanelWrapper.tsx`
- [x] Create `components/DockviewLayout/ThingsListPanelWrapper.tsx`
- [x] Create `components/DockviewLayout/ThingViewPanel.tsx` (new thing viewer panel)

### 3. Set up context menu with base-ui
- [x] @base-ui/react already installed
- [x] Create `components/ThingContextMenu/ThingContextMenu.tsx` using base-ui Menu
- [x] Create `components/ThingContextMenu/ThingContextMenu.module.css`
- [x] Create `components/ThingContextMenu/index.ts`

### 4. Integrate dockview into App.tsx
- [x] Replace flex layout with DockviewReact
- [x] Set up panel components map
- [x] Implement default layout
- [x] Add layout persistence (save/restore)
- [x] Wire up DockviewContext provider

### 5. Add right-click to Graph
- [x] Add `onContextMenu` handler to cytoscape nodes (cxttap event)
- [x] Show context menu at click position
- [x] On "Open in panel" click, call dockview API to add ThingViewPanel

### 6. Add right-click to DetailsPanel links
- [x] Add `onContextMenu` handler to link items
- [x] Reuse context menu component
- [x] On click, add ThingViewPanel for linked thing

### 7. Style adjustments
- [x] Update App.module.css for dockview container sizing
- [x] Apply dockview-theme-dark class
- [x] Ensure proper panel content sizing

### 8. Testing and polish
- [x] Build passes (npm run build)
- [x] TypeScript type check passes (npm run typecheck)
- [x] Main panels auto-reopen if closed (instead of non-closeable API)

### 9. Update CODE_OVERVIEW.md
- [x] Document new dockview components
- [x] Update frontend architecture description
- [x] Note new context menu component

### 10. Update LOCAL_CODE_OVERVIEW.md
- [x] Update frontend directory structure
- [x] Document new DockviewLayout components
- [x] Document ThingContextMenu component
- [x] Update component descriptions

## Files to Create

```
frontend/
├── components/
│   ├── DockviewLayout/
│   │   ├── index.ts
│   │   ├── DockviewLayout.tsx      # Main dockview container
│   │   ├── DockviewLayout.module.css
│   │   ├── GraphPanelWrapper.tsx
│   │   ├── DetailsPanelWrapper.tsx
│   │   ├── ThingsListPanelWrapper.tsx
│   │   └── ThingViewPanel.tsx
│   └── ThingContextMenu/
│       ├── index.ts
│       ├── ThingContextMenu.tsx    # Uses base-ui Menu component
│       └── ThingContextMenu.module.css
└── hooks/
    └── useDockviewApi.ts
```

## Files to Modify

- `main.tsx` - Import dockview CSS
- `App.tsx` - Replace layout with DockviewLayout
- `App.module.css` - Adjust for dockview container
- `components/Graph/Graph.tsx` - Add context menu handler
- `components/DetailsPanel/DetailsPanel.tsx` - Add context menu to links

## Notes

- Dockview provides its own dark theme via `dockview-theme-dark` class
- The existing components (Graph, DetailsPanel, ThingsList) receive the same props - they don't need to know they're in dockview
- ThingViewPanel needs to do its own GraphQL query since it's not using the shared selectedThingId state
- Context menu uses base-ui Menu component (`@base-ui/react`) - unstyled, needs custom CSS

## Rough Edges / Future Improvements

### Implementation deviations from plan
- **Context menu import**: Plan shows `@base-ui-components/react/menu` but actual import is `@base-ui/react/menu`
- **Props passing**: Initially used window globals, refactored to use React context. Both props (`useDockviewProps()`) and API (`useDockviewApiFromContext()`) now passed via `DockviewPropsContext`.

### Items needing manual testing
- [ ] Panel rearrangement (drag tabs, resize panels)
- [ ] Layout persistence across page refresh
- [ ] Opening multiple ThingViewPanels for different things
- [ ] Main panels auto-reopen when closed
- [ ] Context menu dismissal (click outside, Escape key)
- [ ] Context menu on graph nodes vs link items

### Potential improvements
- **Context menu styling**: Current styling is minimal dark theme; could match dockview theme better
- **Panel title**: ThingViewPanel updates title via useEffect after data loads; could show loading state
- **Reset layout button**: No way for user to reset to default layout (would need to clear localStorage)
- **Panel icons**: Dockview supports tab icons; could add icons to distinguish panel types
- **Keyboard navigation**: No keyboard shortcuts for panel management yet
