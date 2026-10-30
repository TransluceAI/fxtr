# Scratch Notes: Preview Panels with Directional Opening

## Understanding the Current System

### Key Components
- **ViewRegistry.ts**: External store managing views, slots, panels. Has `openEntity()` which jumps to existing view or creates new panel
- **EntityRenderer.tsx**: `RefLink` calls `viewRegistry.openEntity(entityId)` on click
- **EntityPanel.tsx**: Displays entity with "Contained in" (parents) and "Referenced by" (backlinks) sections, using RefLink
- **HoverPreview.tsx**: `BacklinksPreviewCard` shows parents/backlinks on hover, also uses RefLink
- **RenderManager.tsx**: Sets up dockview, handles `onDidDrop` for external DnD

### Current Flow
1. Click RefLink -> `viewRegistry.openEntity(entityId)`
2. If view exists -> jump to it
3. If no view -> create panel with new view (no position control)

---

## New Behavior Required

### 1. Directional Opening
- **Forward refs** (owned children, regular refs) -> open to the RIGHT
- **Backward refs** (parents, backlinks) -> open to the LEFT
- This requires knowing:
  - Direction (left/right) - based on context
  - Source panel - to find adjacent groups

### 2. Group Finding Algorithm
```typescript
function findAdjacentGroup(sourceGroup: DockviewGroupPanel, direction: 'left' | 'right'): DockviewGroupPanel | null {
  const sourceRect = sourceGroup.element.getBoundingClientRect();
  const sourceEdge = direction === 'right' ? sourceRect.right : sourceRect.left;
  const sourceVCenter = sourceRect.top + sourceRect.height / 2;

  const candidates = dockviewApi.groups.filter(g => {
    if (g.id === sourceGroup.id) return false;
    const rect = g.element.getBoundingClientRect();
    const targetEdge = direction === 'right' ? rect.left : rect.right;
    // Edges adjacent within tolerance
    return Math.abs(targetEdge - sourceEdge) < 5;
  });

  if (candidates.length === 0) return null;

  // Pick closest by vertical center
  return candidates.reduce((best, g) => {
    const rect = g.element.getBoundingClientRect();
    const vCenter = rect.top + rect.height / 2;
    const bestRect = best.element.getBoundingClientRect();
    const bestVCenter = bestRect.top + bestRect.height / 2;
    return Math.abs(vCenter - sourceVCenter) < Math.abs(bestVCenter - sourceVCenter) ? g : best;
  });
}
```

### 3. Preview Panel Semantics
- `Panel` type gets `isPreview: boolean`
- At most one preview panel per group
- Preview panel tab title in italics
- When opening entity:
  1. Check if view already exists -> jump to it
  2. Find/create group in target direction
  3. Find preview panel in that group
  4. If found -> replace contents
  5. If not -> create new preview panel

### 4. Upgrade from Preview to Regular
Triggers:
- Content of panel is clicked
- Ref that generated it is double-clicked
- Panel is moved (via dockview's `onDidMovePanel`)

---

## Context Threading Problem

RefLink needs to know:
1. **Direction** - left or right
2. **Source panel** - which panel to measure from

### Problem
- EntityPanel provides panel context
- OutPortal content is rendered in RenderRoot (different React tree)
- HoverPreview sets `CurrentViewContext = null` inside popup

### Solution: Two new contexts

```typescript
// Direction context - defaults to 'right'
const DirectionContext = createContext<'left' | 'right'>('right');

// Source panel context - computed before hover popup clears view context
const SourcePanelContext = createContext<PanelId | null>(null);
```

**EntityPanel.tsx:**
```typescript
<SourcePanelContext.Provider value={panelId}>
  {/* parents section */}
  <DirectionContext.Provider value="left">
    {parents.map(p => <RefLink entityId={p} />)}
  </DirectionContext.Provider>

  {/* main content - keeps default 'right' */}
  <OutPortal node={view.portalNode} />

  {/* backlinks section */}
  <DirectionContext.Provider value="left">
    {backRefs.map(r => <RefLink entityId={r} />)}
  </DirectionContext.Provider>
</SourcePanelContext.Provider>
```

**HoverPreview.tsx (BacklinksPreviewCard):**
- Compute sourcePanelId BEFORE clearing view context
- Provide SourcePanelContext with that value
- Wrap content in DirectionContext.Provider value="left"

---

## New ViewRegistry Methods

```typescript
interface ViewRegistry {
  // New
  openEntityInPreview(entityId: EntityId, direction: 'left' | 'right', sourcePanelId: PanelId | null): void;
  upgradePreviewToRegular(panelId: PanelId): void;

  // Internal helpers
  findAdjacentGroup(sourcePanelId: PanelId, direction: 'left' | 'right'): DockviewGroupPanel | null;
  findPreviewPanelInGroup(group: DockviewGroupPanel): PanelId | null;
  replacePreviewContents(panelId: PanelId, entityId: EntityId): void;
}
```

---

## Preview Panel Tab Styling

Options:
1. Custom `tabComponent` in dockview - most flexible but complex
2. Update title with `(preview)` prefix or Unicode italics - simpler
3. CSS class on panel - if dockview supports it

For MVP: Option 2 or skip styling initially

---

## Implementation Order

1. **ViewRegistry changes**
   - Add `isPreview: boolean` to Panel type
   - Add `findAdjacentGroup()` helper
   - Add `findPreviewPanelInGroup()` helper
   - Add `openEntityInPreview()` method
   - Add `replacePreviewContents()` internal helper
   - Add `upgradePreviewToRegular()` method
   - Update debug snapshot to include preview status

2. **New contexts**
   - Add `DirectionContext` to contexts.tsx
   - Add `SourcePanelContext` to contexts.tsx
   - Add `useDirection()` and `useSourcePanelId()` hooks

3. **EntityPanel changes**
   - Provide SourcePanelContext
   - Wrap parents/backlinks in DirectionContext
   - Add click handler to upgrade preview

4. **RefLink changes**
   - Use new contexts
   - Call `openEntityInPreview` instead of `openEntity`
   - Add double-click handler

5. **HoverPreview changes**
   - Compute source panel before popup
   - Provide both contexts in popup
   - BacklinksPreviewCard wraps in DirectionContext.Provider

6. **RenderManager changes**
   - Hook up `onDidMovePanel` for preview upgrade

7. **DebugPanel changes**
   - Show [preview] tag

---

## Questions/Clarifications

1. **Existing view behavior**: If entity already has a view, just jump to it (don't use directional logic for existing views) - seems right?

2. **Preview replacement**: When replacing preview contents, what happens to the old view? Destroy it (and potentially rebind to slots)?

3. **Empty preview**: If we drag the entity out of a preview panel, should the preview panel close or stay empty? The old plan says "remains as empty preview panel" - interesting UX choice.

4. **Multiple preview panels**: Spec says "at most one preview panel per group". If we have group A with preview, then try to open to the left of group B which also has preview, we'd use group B's preview. Correct?

5. **Source panel null**: What if RefLink is clicked from a context with no source panel (e.g., debug panel, entrypoint panel)? Probably just use current active panel or default to creating new group?
