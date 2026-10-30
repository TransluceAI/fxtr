# Phase 2e: Group Sidebar - Design Notes

## Goal Summary
- Sidebar OUTSIDE dockview (to the left)
- Shows all open panel groups
- Lists all panels within each group
- Entity panels show using `ref` renderer (but not actual RefLinks - no preview behavior)
- Clicking focuses the corresponding panel
- Scrollable
- Groups separated with "Group N" header
- Updates as dockview updates

## Key Questions

1. **How to get group/panel info from dockview?**
   - `dockviewApi.groups` returns all groups
   - Each group has `panels` property
   - Need to subscribe to changes for updates

2. **Dockview events available:**
   - `onDidRemovePanel` - already used in RenderManager
   - `onDidMovePanel` - already used in RenderManager
   - `onDidAddGroup`, `onDidRemoveGroup` - likely available
   - Need to find: event for panel added, active panel change, group order change

3. **Where should sidebar state live?**

   Option A: ViewRegistry tracks layout separately
   - Pro: Consistent with existing pattern
   - Con: Duplicates info already in dockview

   Option B: Sidebar subscribes directly to dockview API
   - Pro: Single source of truth
   - Con: Sidebar component needs direct API access

   Option C: ViewRegistry provides a `getLayout()` method that reads from dockview
   - Pro: Clean interface, ViewRegistry already has the API
   - Con: Need to trigger re-renders on dockview changes

   **Recommendation: Option C** - ViewRegistry holds dockviewApi, add method to get layout,
   and subscribe to relevant dockview events to call `notify()` when layout changes.

4. **What info do we need per panel?**
   - panelId
   - Panel type (entity vs debug/entrypoint)
   - For entity panels: entityId, entity type (to pick renderer)
   - isPreview flag (to show differently?)
   - isActive (to highlight current?)

5. **What info do we need per group?**
   - groupId
   - List of panels
   - Maybe: isActive (has focused panel?)

## Design Questions for User

1. Should preview panels be shown differently in the sidebar (e.g., italicized, "(preview)" suffix)?

2. Should the currently active panel be highlighted in the sidebar?

3. Should the sidebar be resizable, or fixed width?

4. What should non-entity panels (Debug, Collections) show? Just their title?

5. Should clicking a panel also activate its group if it's not already active?

## Implementation Approach (Draft)

### 1. ViewRegistry Changes
- Add method `getGroupLayout(): GroupLayout[]` that returns current group/panel structure
- Subscribe to dockview events and call `notify()` when layout changes
- Events to subscribe: onDidAddPanel, onDidRemovePanel, onDidAddGroup, onDidRemoveGroup, onDidMovePanel

### 2. New Component: GroupSidebar
- Simple React component that renders the sidebar
- Uses useSyncExternalStore to subscribe to ViewRegistry
- Renders groups with headers, panels with ref-style display

### 3. RenderRoot Changes
- Wrap dockview in a flex container
- Add GroupSidebar to the left

### 4. Panel Display Component
- For entity panels: use registered `ref` renderer but wrap in a non-RefLink clickable div
- For non-entity panels: just show title
- Add click handler to focus panel

## Types

```typescript
interface GroupLayout {
  groupId: string;
  isActive: boolean;
  panels: PanelLayout[];
}

interface PanelLayout {
  panelId: string;
  title: string;
  isActive: boolean;
  isPreview: boolean;
  // For entity panels:
  entityId?: EntityId;
  entityType?: string;
}
```
