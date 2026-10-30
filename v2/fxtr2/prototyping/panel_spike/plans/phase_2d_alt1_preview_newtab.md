# Goal

Make it so that RefLink clicks don't open in the same panel group as clicked, but instead:

- If we are clicking a regular RefLink, it should open in a panel group to the right. Ideally finding an existing group that is to the right of this one, but if one isn't found, make a new group to the right of this one.
- If we are clicking a backlink or a container link (as shown in either the entity panel or the backlink preview hover card), it should open in a panel group to the left. Again ideally finding an existing one, but maybe making a new one.

When finding an existing one, we will use a heuristic because dockview doesn't provide a direct API for this. Roughly, it should walk all the groups in the dockview API, and look at their bounding client rects, then (assuming we are going right) look for a group whose leftmost edge aligns with our rightmost edge. If there are multiple, pick the one whose vertical center is closest to ours. If there are none, assume we need to create a new panel.

Additionally, we will add support for preview panels. A preview panel is an entity panel with a preview flag set, and its tab title should render in italics. There can be at most one preview panel in any group. When we open a ref by clicking it, after finding a group by the above procedure, we will locate the preview panel in that group if it exists, and replace its contents with the given entity. If there is no preview panel in that group we will create one.

Preview panels get upgraded to regular panels if their content is clicked, if the ref that generated them is double-clicked, or if the panel is moved.

Preview panels should show as preview panels in the debug panel.

---

# Implementation Plan

## Design Decisions (Clarified)
- **Existing views**: If entity already has a view, jump to it (current behavior). Directional/preview logic only for new views.
- **Empty previews**: When preview content is moved out, close the panel (don't keep empty previews).
- **No source panel**: When clicked from Debug/Entrypoint (no clear source), fall back to current behavior.
- **Direction**: Pass as prop to RefLink (not context) - simpler and more explicit.
- **CurrentViewContext**: Change to object `{ viewId, inHoverPreview }` instead of adding separate SourcePanelContext.

## Core Changes

### 1. ViewRegistry.ts

**Extend Panel type:**
```typescript
interface Panel {
  panelId: PanelId;
  viewId: ViewId;
  isPreview: boolean;  // NEW
}
```

**New methods:**
- `findAdjacentGroup(sourcePanelId: PanelId, direction: 'left' | 'right'): DockviewGroupPanel | null`
  - Get source panel's group and bounding rect
  - Walk all groups, find ones whose edge aligns with source's opposite edge (within 5px)
  - Pick candidate with closest vertical center

- `findPreviewPanelInGroup(group: DockviewGroupPanel): PanelId | null`
  - Iterate group's panels, find one where `state.panels.get(id)?.isPreview`

- `openEntityInPreview(entityId, direction, sourcePanelId): void`
  - If views exist for entity -> `jumpToEntity()` and return
  - If no sourcePanelId -> fall back to `openEntity()` (current behavior)
  - Find adjacent group in direction
  - If no adjacent group found -> will create one
  - Look for preview panel in target group
  - If preview found -> `replacePreviewContents()`
  - Else -> create new preview panel in target group (or new group)

- `replacePreviewContents(panelId, entityId): void` (internal)
  - Get old view from panel
  - Destroy old view (trigger slot rebinding if applicable)
  - Create new view for entityId
  - Update panel's viewId
  - Update dockview panel params/title

- `upgradePreviewToRegular(panelId): void`
  - Set `isPreview = false` on panel
  - Update tab title styling

**Modify existing:**
- `handlePanelClose()`: Handle cleanup of preview panels
- Panel creation helpers: Accept `isPreview` parameter

### 2. contexts.tsx

**Change CurrentViewContext from `string | null` to object:**
```typescript
interface ViewContextValue {
  viewId: string | null;
  inHoverPreview: boolean;
}

export const CurrentViewContext = createContext<ViewContextValue | null>(null);
```

**Update hooks:**
```typescript
// Returns viewId (throws if not in view context and not in hover)
export function useCurrentViewId(): string;

// Returns viewId or null (for optional contexts)
export function useMaybeCurrentViewId(): string | null;

// Returns full context value (for checking inHoverPreview)
export function useViewContext(): ViewContextValue | null;
```

**No new contexts needed** - direction is a prop, source panel computed from viewId.

### 3. EntityRenderer.tsx

**Update EntityRenderer to provide object context:**
```tsx
<CurrentViewContext.Provider value={{ viewId, inHoverPreview: false }}>
  {/* ... */}
</CurrentViewContext.Provider>
```

**Update RefLink - add direction prop:**
```tsx
interface RefLinkProps {
  entityId: EntityId;
  isOwned?: boolean;
  direction?: 'left' | 'right';  // NEW - defaults to 'right'
}

function RefLink({ entityId, isOwned = false, direction = 'right' }: RefLinkProps) {
  const viewRegistry = useViewRegistry();
  const ctx = useViewContext();

  // Compute source panel from context's viewId (works even in hover previews)
  const sourcePanelId = ctx?.viewId ? viewRegistry.findViewPanel(ctx.viewId) : null;

  const handleClick = () => {
    viewRegistry.openEntityInPreview(entityId, direction, sourcePanelId);
  };

  const handleDoubleClick = () => {
    // If this opens a preview, upgrade it to regular
    // (Tracked via returned panelId or separate mechanism)
  };

  // ... rest unchanged
}
```

**Update InlineRefLink similarly** - add `direction` prop.

### 4. EntityPanel.tsx

```tsx
// Parents - pass direction="left"
{parents.map((parentId) => (
  <li key={parentId}>
    <RefLink entityId={parentId} direction="left" />
  </li>
))}

// Main content - RefLinks inside use default direction="right"
<OutPortal node={view.portalNode} />

// Backlinks - pass direction="left"
{backRefs.map((refId) => (
  <li key={refId}>
    <RefLink entityId={refId} direction="left" />
  </li>
))}
```

**Add click handler for preview upgrade:**
- If this panel is a preview, clicking content area calls `upgradePreviewToRegular()`

### 5. HoverPreview.tsx

**Both EntityPreviewCard and BacklinksPreviewCard:**
- Read current context BEFORE the popup
- Preserve viewId but set `inHoverPreview: true` inside popup

```tsx
// Read current context (may already be in a hover)
const ctx = useViewContext();
const preservedViewId = ctx?.viewId ?? null;

// In popup - preserve viewId, mark as in hover preview:
<CurrentViewContext.Provider value={{ viewId: preservedViewId, inHoverPreview: true }}>
  <HoverKeptOpenContext.Provider value={setKeptOpenByChild}>
    {/* content */}
  </HoverKeptOpenContext.Provider>
</CurrentViewContext.Provider>
```

**BacklinksPreviewCard renderBacklink:**
```tsx
renderBacklink={(refId) => <RefLink entityId={refId} direction="left" />}
```

**EntityPreviewCard:**
- PreviewRenderer's internal RefLinks use default `direction="right"` (forward navigation)

### 6. DraggableID.tsx

**Update to check inHoverPreview:**
```tsx
const ctx = useViewContext();
const viewId = ctx?.viewId ?? null;
const inHoverPreview = ctx?.inHoverPreview ?? false;

// Can only move if we have a viewId AND not in a hover preview
const canMove = viewId !== null && !inHoverPreview;

const handleDragStart = useCallback((e: React.DragEvent) => {
  if (canMove && viewId) {
    serializeDragData({ type: 'view', viewId, entityId }, e.dataTransfer);
  } else {
    serializeDragData({ type: 'ref', entityId }, e.dataTransfer);
  }
  // ...
}, [entityId, viewId, canMove]);
```

### 7. RenderManager.tsx

Hook up panel move detection:
```tsx
event.api.onDidMovePanel?.((panel) => {
  viewRegistry.upgradePreviewToRegular(panel.id);
});
```

### 8. DebugPanel.tsx

Show preview status in panel list:
```tsx
{p.isPreview && <span style={{ color: '#ff8' }}> [preview]</span>}
```

### 9. Tab Styling

For MVP: Prefix preview panel titles with "(preview) " or use Unicode italics.

Later: Custom `tabComponent` in dockview for proper italic styling.

## Implementation Order

1. contexts.tsx: Change CurrentViewContext to object type, update hooks
2. EntityRenderer.tsx: Update context provider, add direction prop to RefLink/InlineRefLink
3. DraggableID.tsx: Update to check inHoverPreview
4. HoverPreview.tsx: Preserve viewId with inHoverPreview flag, pass direction to RefLinks
5. ViewRegistry: Add `isPreview` to Panel, update creation/snapshot
6. ViewRegistry: Add `findAdjacentGroup()` helper
7. ViewRegistry: Add `findPreviewPanelInGroup()` helper
8. ViewRegistry: Add `openEntityInPreview()` main logic
9. ViewRegistry: Add `replacePreviewContents()` and `upgradePreviewToRegular()`
10. EntityPanel: Pass direction="left" to parent/backlink RefLinks, add preview upgrade handler
11. RenderManager: Hook up panel move detection
12. DebugPanel: Show preview status
13. Polish: Tab styling, edge cases

## Summary of Simplifications

| Original Plan | Simplified |
|---------------|------------|
| `DirectionContext` + `useDirection()` | `direction` prop on RefLink/InlineRefLink |
| `SourcePanelContext` + `useSourcePanelId()` | Compute from `CurrentViewContext.viewId` via `findViewPanel()` |
| `CurrentViewContext: string \| null` | `CurrentViewContext: { viewId: string \| null, inHoverPreview: boolean }` |
| HoverPreview sets context to `null` | HoverPreview sets `{ viewId: preserved, inHoverPreview: true }` |
| DraggableID checks `viewId !== null` | DraggableID checks `viewId !== null && !inHoverPreview` |

## Detailed scratch notes

See `phase_2d_alt_preview_newtab_scratch.md` for deeper analysis of group-finding algorithm and edge cases.
