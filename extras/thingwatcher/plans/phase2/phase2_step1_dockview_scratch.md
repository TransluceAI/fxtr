# Dockview Integration Scratch

## Requirements Summary

From phase2_overview.md + user answers:

1. **Three main panels** (Graph, DetailsPanel, ThingsList) become non-closeable dockview panels
2. **Timeline stays outside** dockview (fixed at bottom)
3. **Single dockview instance** - all panels freely arrangeable, can stack in tabs
4. **Right-click context menu**:
   - On nodes in Graph → opens thing in new panel
   - On link items in DetailsPanel → opens linked thing in new panel
5. **New panels are closeable** (unlike the three default panels)
6. **Layout persisted** to localStorage
7. **Panel data tracks current tick** (not pinned)
8. **New panel placement**: new tab in a new split of the group that was right-clicked

## Dockview Concepts

- **DockviewReact**: Main container component
- **IDockviewPanelProps**: Props for panel components
- **DockviewApi**: Controls for adding/removing panels programmatically
- Panels have `id`, `title`, `component`
- Groups contain multiple panels (as tabs)
- Can serialize/deserialize layout

## Current Structure Analysis

App.tsx has:
```
container
├── header (fixed)
├── main
│   ├── graphArea (Graph)
│   └── sidebar
│       ├── panelTop (DetailsPanel)
│       └── panelBottom (ThingsList)
└── timelineArea (Timeline - stays fixed)
```

After dockview:
```
container
├── header (fixed)
├── dockviewContainer (DockviewReact)
│   └── [panels managed by dockview]
└── timelineArea (Timeline - stays fixed)
```

## Key Implementation Decisions

### Panel Component Architecture

Need to extract panel content from current components so they work as dockview panels:

1. **GraphPanel**: Wraps Graph component
2. **DetailsPanelDock**: The current DetailsPanel becomes a "main" one showing selectedThing
3. **ThingsListPanel**: Wraps ThingsList
4. **ThingViewPanel**: New component for right-click-opened things (reuses DetailsPanel internals)

### Panel Identification

- Main panels: `graph-main`, `details-main`, `thingslist-main` (non-closeable)
- User-opened panels: `thing-view-${shortId}` (closeable)

### Context Menu Implementation

Need a custom context menu (not browser default). Options:
1. Simple div positioned at click location, dismissed on click outside
2. Use a library like @radix-ui/react-context-menu

For simplicity, start with a custom simple context menu.

### Layout Persistence

```typescript
const LAYOUT_KEY = 'thingwatcher-dockview-layout';

// On layout change
api.onDidLayoutChange(() => {
  const layout = api.toJSON();
  localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout));
});

// On init
const savedLayout = localStorage.getItem(LAYOUT_KEY);
if (savedLayout) {
  api.fromJSON(JSON.parse(savedLayout));
}
```

### Sharing Selection State

Current AppContext provides `selectedThingId` which DetailsPanel uses.
- The "main" DetailsPanel continues to use this
- User-opened ThingViewPanels have their own fixed shortId (but track current tick)

Need to pass:
- `shortId` to each ThingViewPanel
- Current tick context still flows through AppContext

### Non-closeable Panels

Dockview supports `locked` groups and individual panel settings.
Can use `panel.setCloseable(false)` or configure at creation.

## Refactoring Needed

### DetailsPanel → reusable ThingView

Current DetailsPanel:
- Receives `thing`, `loading`, `isLive`, `onSelectThing`, `triggerRecords`, `onTrigger`
- Shows: header, meta, trigger form, data tree, links

Extract core viewing logic into `ThingView` that:
- Accepts `shortId` and fetches its own data
- OR accepts `thing` data directly (for main details panel)

Actually, simpler approach:
- Keep DetailsPanel as-is for the main panel (uses selectedThingId)
- Create ThingViewPanel that takes shortId prop and does its own query

### Graph Context Menu

Need to:
1. Listen for right-click on nodes
2. Show context menu at click position
3. Context menu has "Open in new panel" option
4. On click, call dockview API to add panel

### DetailsPanel Link Context Menu

Need to:
1. Modify link items to have right-click handler
2. Show same context menu
3. Use toThing.shortId to open panel

## Files to Create/Modify

**New files:**
- `components/ThingViewPanel/ThingViewPanel.tsx` - Self-contained thing viewer for dockview
- `components/ThingViewPanel/ThingViewPanel.module.css`
- `components/ContextMenu/ContextMenu.tsx` - Reusable context menu
- `components/ContextMenu/ContextMenu.module.css`

**Modify:**
- `App.tsx` - Replace flex layout with DockviewReact
- `App.module.css` - Adjust styles for dockview container
- `components/Graph/Graph.tsx` - Add right-click handler for nodes
- `components/DetailsPanel/DetailsPanel.tsx` - Add right-click handler for links
- `hooks/` - Maybe add useDockviewApi hook

**Package.json:**
- Add dockview dependency (check if already there)

## Default Layout

```json
{
  "grid": {
    "root": {
      "type": "branch",
      "data": [
        { "type": "leaf", "data": { "id": "graph-main", "size": 600 } },
        {
          "type": "branch",
          "data": [
            { "type": "leaf", "data": { "id": "details-main" } },
            { "type": "leaf", "data": { "id": "thingslist-main" } }
          ],
          "size": 400
        }
      ]
    }
  }
}
```

## Open Questions

1. How to handle dockview initialization without a saved layout vs with one?
2. What happens if saved layout references a thing-view panel for a thing that no longer exists?
   - Probably just let it render with "thing not found" state
3. Should we add panel titles that update (e.g., "Details: #5 MyThing")?
   - Yes, for ThingViewPanels, title should show shortId and maybe type
