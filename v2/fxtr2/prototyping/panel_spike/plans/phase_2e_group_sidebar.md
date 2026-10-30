# Goal

Add a sidebar to the left of the dockview (outside it) that shows all of the open panel groups, and lists all the panels. Entity panels should show using the unowned-ref formatter for each node type but should NOT be actual reflinks. Clicking them should focus the corresponding panel. The sidebar should be scrollable, and groups should be separated from each other with a "Group N" header.

This should update as the dockview component updates. Possibly state should be managed by the ViewRegistry if needed, so that the sidebar panel itself is just a react component that gets the info it needs as props from the RenderRoot.

---

# Implementation Plan

## Design Decisions
- Preview panels shown in *italics*
- Currently active panel highlighted
- Sidebar is resizable
- Non-entity panels (Debug, Collections) show just their title
- Clicking a panel focuses it and makes it active in dockview

## Types

```typescript
interface GroupLayout {
  groupId: string;
  panels: PanelLayout[];
}

interface PanelLayout {
  panelId: string;
  title: string;
  isActive: boolean;      // Currently focused panel
  isPreview: boolean;
  entityId?: EntityId;    // For entity panels only
  entityType?: string;    // For entity panels only
}
```

## Implementation Steps

### Step 1: ViewRegistry - Add layout tracking

Add to `ViewRegistry.ts`:

1. Add `getGroupLayout(): GroupLayout[]` method that reads from dockviewApi:
   - Iterate `dockviewApi.groups`
   - For each group, iterate `group.panels`
   - Look up panel info from our `state.panels` map
   - For entity panels, get entityId from view, then entity type from data
   - Track which panel is active via `dockviewApi.activePanel`

2. Subscribe to dockview layout events in `setDockviewApi()`:
   - `onDidAddPanel` - call `notify()`
   - `onDidRemovePanel` - already handled, ensure it calls `notify()`
   - `onDidAddGroup` - call `notify()`
   - `onDidRemoveGroup` - call `notify()`
   - `onDidActivePanelChange` - call `notify()`

3. Export types `GroupLayout` and `PanelLayout`.

### Step 2: New hook in contexts.tsx

Add `useGroupLayout(viewRegistry: ViewRegistry): GroupLayout[]` hook:
- Uses `useSyncExternalStore` to subscribe
- Calls `viewRegistry.getGroupLayout()`

### Step 3: Create GroupSidebar component

New file: `ts/src/render-manager/GroupSidebar.tsx`

```tsx
interface GroupSidebarProps {
  layout: GroupLayout[];
  onPanelClick: (panelId: string) => void;
}

function GroupSidebar({ layout, onPanelClick }: GroupSidebarProps) {
  // Render scrollable container
  // For each group: render "Group N" header
  // For each panel: render SidebarPanelItem
}

function SidebarPanelItem({ panel, onClick }: { panel: PanelLayout; onClick: () => void }) {
  // If entity panel: use ref renderer (via useRegisteredEntityRenderer)
  // Else: just show title
  // Apply italic style if isPreview
  // Apply highlight style if isActive
  // onClick calls the passed handler
}
```

Key points:
- For entity panels, get the `ref` renderer from registry and render it
- Wrap in a clickable div (NOT RefLink - no hover preview, no drag)
- Style: italic for preview, background highlight for active

### Step 4: Update RenderRoot layout

Modify `RenderManager.tsx`:

1. Add a resizable split container wrapping everything:
   ```tsx
   <div style={{ display: 'flex', height: '100%' }}>
     <ResizableSidebar defaultWidth={200} minWidth={150} maxWidth={400}>
       <GroupSidebar layout={layout} onPanelClick={handlePanelClick} />
     </ResizableSidebar>
     <div style={{ flex: 1 }}>
       <DockviewReact ... />
     </div>
   </div>
   ```

2. Add `handlePanelClick` that calls `dockviewApi.getPanel(panelId)?.focus()`

3. Use `useGroupLayout` hook to get layout data

### Step 5: Create ResizableSidebar component

New file: `ts/src/render-manager/ResizableSidebar.tsx`

Simple resizable container:
- Stores width in state
- Renders a drag handle on the right edge
- Handles mouse drag to resize

### Step 6: Styling

- Sidebar background: dark theme matching dockview
- Group headers: slightly lighter background, bold text
- Panel items: hover state, active highlight (e.g., left border or background)
- Preview panels: italic text
- Entity type colors: reuse `getTypeColor()` from helpers

## File Changes Summary

| File | Changes |
|------|---------|
| `ViewRegistry.ts` | Add `getGroupLayout()`, subscribe to more dockview events, export types |
| `contexts.tsx` | Add `useGroupLayout()` hook |
| `GroupSidebar.tsx` | New file - sidebar component |
| `ResizableSidebar.tsx` | New file - resizable container |
| `RenderManager.tsx` | Wrap dockview in flex layout with sidebar |
| `overview.md` | Document new components |
