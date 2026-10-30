# Phase 2 Step 11 Part 4: Panel Sidebar

## Overview

Add a collapsible sidebar to the left of the dockview that shows all open panel groups and their panels. This provides a quick overview of open panels and allows focusing/closing them without navigating through tabs.

**Features:**
- Sidebar shows all dockview groups with their panels
- Thing panels show using their RefRenderer (type-colored, compact) with hover preview
- Non-thing panels (Graph, Details, Things, Roots) show just their title
- Click a panel to focus it in dockview
- Close button (X) on each panel item
- Active/focused panel highlighted with left border
- Resizable sidebar (150-400px range)
- Collapse button in the main app header to hide/show sidebar

## User Decisions

- **Panel labels**: Use RefRenderer for thing panels (consistent with NodeRef), with preview on hover
- **Click behavior**: Focus panel only (don't change selection state)
- **Close button**: Yes, X button to close panels directly from sidebar
- **Toggle location**: Header button in the main app header bar

## Current State

### What exists

1. **DockviewLayout** (`components/DockviewLayout/DockviewLayout.tsx`): Main dockview container. Manages panels, layout persistence, external drag-drop.

2. **App.tsx**: Main app shell with header bar and DockviewLayout. Header shows title and tick info.

3. **DynamicRefRenderer** (`components/DynamicRenderer/DynamicRenderer.tsx`): Renders RefRenderer for thing types.

4. **HoverPreviewCard** (`lib/HoverPreview.tsx`): Shows preview on hover (used by NodeRef).

5. **Panel types**:
   - `graph-main`, `details-main`, `thingslist-main`, `roots-main`: Main panels (auto-reopen)
   - `thing-view-${key}`: User-opened thing panel via context menu
   - `thing-view-${key}-${timestamp}`: User-opened thing panel via drag-drop

### Panel_spike reference

The panel_spike implements:
- `GroupSidebar.tsx`: Sidebar component with groups, panels, close buttons
- `ResizableSidebar.tsx`: Resizable container with drag handle
- `ViewRegistry.getGroupLayout()`: Returns current group/panel structure
- `useGroupLayout()` hook: Subscribes to layout changes
- `EntitySidebarTooltip`: Preview on hover over entity panels
- Dockview events for layout updates

## Design

### Types

```typescript
// Types for sidebar layout
interface GroupLayout {
  groupId: string;
  panels: PanelLayout[];
}

interface PanelLayout {
  panelId: string;
  title: string;
  isActive: boolean;       // Currently focused panel
  thingKey?: string;       // For thing panels only (b64 key)
}
```

### Layout Hook

Create a hook to track dockview layout changes:

```typescript
// components/DockviewLayout/usePanelLayout.ts

export function usePanelLayout(api: DockviewApi | null): GroupLayout[] {
  // Uses useSyncExternalStore to subscribe to dockview changes
  // Returns current layout structure
}

// Internal helper to build layout from dockview
function buildLayout(api: DockviewApi): GroupLayout[] {
  const groups: GroupLayout[] = [];

  for (const group of api.groups) {
    const panels: PanelLayout[] = [];

    for (const panel of group.panels) {
      // Use panel.api.component to determine panel type (cleaner than parsing ID)
      // component is 'thingView', 'graph', 'details', 'thingsList', 'roots'
      const isThingPanel = panel.api.component === 'thingView';
      const thingKey = isThingPanel ? (panel.params?.key as string | undefined) : undefined;

      panels.push({
        panelId: panel.id,
        title: panel.title ?? panel.id,
        isActive: api.activePanel?.id === panel.id,
        thingKey,
      });
    }

    groups.push({ groupId: group.id, panels });
  }

  return groups;
}
```

### Sidebar Components

**ResizableSidebar** - Container with drag handle for resizing:

```typescript
// components/Sidebar/ResizableSidebar.tsx

interface ResizableSidebarProps {
  children: React.ReactNode;
  defaultWidth?: number;  // 200
  minWidth?: number;      // 150
  maxWidth?: number;      // 400
}

// - Drag handle on right edge for resizing
// - Stores width in state (could persist to localStorage)
// - Parent conditionally renders this component for show/hide
```

**PanelSidebar** - Main sidebar content:

```typescript
// components/Sidebar/PanelSidebar.tsx

interface PanelSidebarProps {
  layout: GroupLayout[];
  onPanelFocus: (panelId: string) => void;
  onPanelClose: (panelId: string) => void;
}

// - Renders header "Panels"
// - Maps layout to GroupSection components
// - Scrollable container
```

**GroupSection** - Individual group with its panels:

```typescript
interface GroupSectionProps {
  group: GroupLayout;
  index: number;
  onPanelFocus: (panelId: string) => void;
  onPanelClose: (panelId: string) => void;
}

// - "Group N" header
// - List of PanelItem components
```

**PanelItem** - Individual panel in sidebar:

```typescript
interface PanelItemProps {
  panel: PanelLayout;
  onFocus: () => void;
  onClose: () => void;
}

// For thing panels:
// - Wrap with HoverPreviewCard for hover preview
// - Render RefRenderer in a non-interactive wrapper (pointerEvents: none on inner)
// - Click whole row to focus
// - X button to close

// For non-thing panels:
// - Just show title text
// - Click to focus, X to close

// Styling:
// - Blue left border when isActive
// - Hover highlight
```

**ThingPanelLabel** - Renders RefRenderer for thing panels:

```typescript
interface ThingPanelLabelProps {
  thingKey: string;
}

// - Query thing data via useThing hook
// - Render DynamicRefRenderer
// - Wrap in pointerEvents: none to prevent interactions
```

### Integration

**App.tsx** - Owns visibility state and toggle button:

```typescript
// In App.tsx

const [sidebarVisible, setSidebarVisible] = useState(true);

// Header button
<button onClick={() => setSidebarVisible(v => !v)}>
  {sidebarVisible ? 'Hide Panels' : 'Show Panels'}
</button>

// Pass visibility as prop
<DockviewLayout sidebarVisible={sidebarVisible} ... />
```

**DockviewLayout.tsx** - Contains sidebar next to DockviewReact:

```typescript
// In DockviewLayout.tsx

export interface DockviewLayoutProps {
  sidebarVisible: boolean;
  // ... existing props
}

// Inside the component, after API is available:
const layout = usePanelLayout(apiState);

const handlePanelFocus = useCallback((panelId: string) => {
  apiState?.getPanel(panelId)?.api.setActive();
}, [apiState]);

const handlePanelClose = useCallback((panelId: string) => {
  const panel = apiState?.getPanel(panelId);
  if (panel) {
    apiState?.removePanel(panel);
  }
}, [apiState]);

// Layout - sidebar inside DockviewLayout, has access to API context
return (
  <DockviewApiContext.Provider value={apiState}>
    {/* ... other providers ... */}
    <div className={styles.container}>
      {props.sidebarVisible && (
        <ResizableSidebar>
          <PanelSidebar
            layout={layout}
            onPanelFocus={handlePanelFocus}
            onPanelClose={handlePanelClose}
          />
        </ResizableSidebar>
      )}
      <DockviewReact ... />
    </div>
  </DockviewApiContext.Provider>
);
```

### Layout Flow

1. `usePanelLayout` hook subscribes to dockview API events
2. When layout changes, hook rebuilds layout structure
3. Sidebar re-renders with new layout
4. Click handlers in DockviewLayout call `api.getPanel(id)?.api.setActive()` or `api.removePanel(panel)`

### Event Subscriptions

The layout hook needs to subscribe to these dockview events:
- `onDidAddPanel` - panel added
- `onDidRemovePanel` - panel removed
- `onDidAddGroup` - group added
- `onDidRemoveGroup` - group removed
- `onDidActivePanelChange` - active panel changed

### Styling

```css
/* Sidebar container */
.sidebarContainer {
  height: 100%;
  overflow: auto;
  background: #1a1a2e;
  border-right: 1px solid #333;
  padding: 8px;
}

/* Group header */
.groupHeader {
  font-size: 11px;
  color: #666;
  padding: 4px 8px;
  background: #252540;
  border-radius: 4px;
  margin-bottom: 4px;
}

/* Panel item */
.panelItem {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 8px;
  cursor: pointer;
  border-radius: 4px;
  border-left: 3px solid transparent;
}

.panelItem:hover {
  background: #252540;
}

.panelItem.active {
  background: #333355;
  border-left-color: #58a6ff;
}

/* Close button */
.closeButton {
  background: transparent;
  border: none;
  padding: 2px;
  cursor: pointer;
  color: #888;
  opacity: 0;
}

.panelItem:hover .closeButton {
  opacity: 1;
}
```

## Implementation Steps

- [x] 1. Create `components/DockviewLayout/usePanelLayout.ts` with layout tracking hook and types
- [x] 2. Create `components/Sidebar/` directory
- [x] 3. Create `components/Sidebar/ResizableSidebar.tsx` - resizable container
- [x] 4. Create `components/Sidebar/ResizableSidebar.module.css` - resize handle styles
- [x] 5. Create `components/Sidebar/PanelSidebar.tsx` - main sidebar component with ThingPanelLabel
- [x] 6. Create `components/Sidebar/PanelSidebar.module.css` - sidebar styles
- [x] 7. Create `components/Sidebar/index.ts` - exports
- [x] 8. Update `DockviewLayout.tsx`:
    - Add `sidebarVisible` prop
    - Add usePanelLayout hook
    - Add focus/close handlers
    - Integrate ResizableSidebar + PanelSidebar in layout
- [x] 9. Update `DockviewLayout.module.css` - flex layout for sidebar + dockview
- [x] 10. Update `App.tsx`:
    - Add sidebar visibility state
    - Add toggle button to header
    - Pass `sidebarVisible` prop to DockviewLayout
- [ ] 11. Manual testing:
    - Sidebar shows all groups and panels
    - Thing panels show RefRenderer with type colors
    - Non-thing panels show title text
    - Click panel to focus it
    - X button closes panel
    - Active panel has blue left border
    - Hover over thing panel shows preview
    - Drag resize handle resizes sidebar
    - Header button toggles sidebar visibility
    - Main panels (Graph, Details, etc.) reopen after closing from sidebar
- [x] 12. Update `LOCAL_CODE_OVERVIEW.md` with Sidebar component documentation
- [x] 13. Update `CODE_OVERVIEW.md`

## Files Changed

**New files:**
- `components/DockviewLayout/usePanelLayout.ts` - Layout tracking hook and types
- `components/Sidebar/index.ts` - Exports
- `components/Sidebar/ResizableSidebar.tsx` - Resizable container
- `components/Sidebar/ResizableSidebar.module.css` - Resize handle styles
- `components/Sidebar/PanelSidebar.tsx` - Main sidebar with groups/panels/ThingPanelLabel
- `components/Sidebar/PanelSidebar.module.css` - Sidebar styles

**Modified files:**
- `components/DockviewLayout/DockviewLayout.tsx` - Add sidebarVisible prop, integrate sidebar
- `components/DockviewLayout/DockviewLayout.module.css` - Flex layout for sidebar + dockview
- `App.tsx` - Sidebar visibility state, toggle button in header
- `frontend/LOCAL_CODE_OVERVIEW.md` - Documentation
- `CODE_OVERVIEW.md` - High-level documentation

## Testing Notes

Manual testing scenarios:

1. **Basic visibility**: Sidebar visible by default, shows groups and panels
2. **Panel labels**: Thing panels show colored RefRenderer, other panels show title
3. **Focus click**: Clicking panel focuses it in dockview (tab becomes active)
4. **Close button**: X button closes panel; main panels reopen automatically
5. **Active indicator**: Focused panel has blue left border
6. **Hover preview**: Hovering thing panel shows preview card (500ms delay)
7. **Resize**: Drag right edge to resize (150-400px range)
8. **Toggle**: Header button hides/shows sidebar
9. **Layout persistence**: Sidebar width could persist (optional enhancement)
10. **Group structure**: Groups shown with "Group N" headers
11. **Scroll**: Sidebar scrollable when many panels

## Future Considerations

- Persist sidebar width to localStorage
- Double-click panel to select that thing
- Drag panels in sidebar to reorder (complex - would require dockview API work)
- Collapse individual groups
- Search/filter panels
