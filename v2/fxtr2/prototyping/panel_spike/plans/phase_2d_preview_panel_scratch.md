OUTDATED - DO NOT USE

# Preview Panel Design Scratch Notes

## Understanding the Requirements

From the spec and plan file:

1. **Preview panel** - a special panel where clicked refs open, temporary
2. **Visual distinction** - dotted box, italicized tab title, message saying "drag entity out if you want to keep it"
3. **At most one preview panel** at a time
4. **Opening behavior:**
   - Click ref → first try to find/show existing panel with entity
   - If not found but preview panel exists (and usable) → replace contents
   - If no usable preview panel → split active panel to right, create new preview panel
5. **Same-group constraint** - never put clicked ref entity into preview panel that's in the same group as the triggering ref
6. **Pinning** - clicking ON the preview panel makes it permanent (no longer preview)
7. **Empty preview panel** - when entity is moved out, panel stays as empty preview (doesn't close)

## Current System Analysis

**Current Panel type:**
```typescript
interface Panel {
  panelId: PanelId;
  viewId: ViewId;  // Required!
}
```

**Current openEntity behavior:**
1. If views exist → jump to best one (active panel > visible panel > any panel)
2. If no views → create new panel + view

**Problem:** No concept of preview panel. Need to add:
- Tracking which panel (if any) is the preview panel
- Logic to use/create preview panel
- Logic to replace preview panel content
- "Empty preview panel" state (Panel with no viewId)

## Key Design Decisions

### 1. How to track preview panel?

**Option A:** Store `isPreview: boolean` on Panel type
- Pro: Each panel knows its own state
- Con: Have to search all panels to find preview

**Option B:** Store `previewPanelId: PanelId | null` separately in ViewRegistry
- Pro: O(1) lookup of preview panel
- Pro: Enforces "at most one" constraint naturally
- Con: Slight duplication of state

**Recommendation:** Option B (single previewPanelId)

### 2. How to handle empty preview panel?

Current assumption: Panel always has a viewId. But requirement says:
> "if the entity is moved from the preview panel, the panel does NOT close; instead it remains as an empty preview panel"

**Options:**
- Make viewId optional: `viewId: ViewId | null`
- Track empty preview separately
- Create a "dummy" empty view

**Recommendation:** Make viewId optional on Panel. EntityPanel handles null case.

### 3. How to detect "same group"?

Need to pass source context when opening entity. The ref click happens somewhere - we need to know which dockview group that somewhere is in.

**Approach:**
- Modify `openEntity` to accept optional `{ sourceGroupId?: string }`
- RefLink needs to determine its current group when clicked
- Can use `viewRegistry.findViewPanel(currentViewId)` then `api.getPanel(panelId)?.group.id`

**Challenge:** RefLink may be in:
- A panel (has group)
- A hover preview card (no group)
- Entrypoint panel (has group)

When in hover preview, sourceGroupId could be null (allow any preview panel).

### 4. What triggers pinning?

Spec says: "if you click on the preview panel, it stops being the preview panel"

**Options:**
- Any click in panel content pins it
- Only clicks on interactive elements (refs, buttons) pin
- Explicit "pin" button
- Double-click to pin (like VS Code files)

**Recommendation:** Any mousedown on the panel content pins it. Simple and predictable.

### 5. How to style the tab?

Dockview options:
- Custom `ITabRenderer` component
- Use `panel.api.updateParameters` to pass data, custom render
- CSS-only with `[data-panel-id="..."]` selector

**Recommendation:** Use parameters to pass `isPreview` flag, and/or CSS class on the panel.

Actually dockview has `tabComponent` option per panel. We could use different component for preview panels.

## Proposed State Changes

### ViewRegistry additions

```typescript
interface ViewRegistryState {
  // ...existing...
  previewPanelId: PanelId | null;
}

interface Panel {
  panelId: PanelId;
  viewId: ViewId | null;  // null for empty preview panel
}
```

### New/Modified API Methods

```typescript
interface ViewRegistry {
  // Existing (modified)
  openEntity(entityId: EntityId, options?: { sourceGroupId?: string }): void;

  // New
  getPreviewPanelId(): PanelId | null;
  subscribePreviewPanel(listener: () => void): () => void;
  pinPreviewPanel(): void;  // Convert preview to permanent
  isPreviewPanel(panelId: PanelId): boolean;
}
```

### openEntity New Logic

```
openEntity(entityId, { sourceGroupId }):
  1. Check if entity has existing views
     → If yes: jump to best one, RETURN

  2. Check if usable preview panel exists:
     - previewPanelId !== null
     - preview panel's group !== sourceGroupId (or sourceGroupId is null)

     → If usable:
       a. If preview panel has a view, destroy/rebind it
       b. Create new view for entityId
       c. Assign new view to preview panel
       d. Focus preview panel
       RETURN

  3. No usable preview panel - create one:
     a. Create new panel (as preview)
     b. Create new view
     c. Position: split active panel to right
     d. Set previewPanelId = new panel id
```

## Panel Component Changes

### EntityPanel modifications

```tsx
// Handle empty preview panel
if (!view) {
  if (isPreviewPanel) {
    return <EmptyPreviewPanelUI />;
  }
  return <ErrorState />;
}

// Add pinning handler
const handleMouseDown = () => {
  if (isPreviewPanel) {
    viewRegistry.pinPreviewPanel();
  }
};

// Visual styling
const style = {
  border: isPreviewPanel ? '2px dashed gray' : undefined,
};

// Message
{isPreviewPanel && (
  <div>Preview panel - drag entity out to keep it</div>
)}
```

### Tab styling

Need to make tab italic when isPreview. Options:
1. Custom tab component
2. CSS targeting panel ID
3. dockview className option

## Flow Diagrams

### Clicking a ref

```
User clicks RefLink
    ↓
RefLink.handleClick()
    ↓
Get current group ID (if in a panel)
    ↓
viewRegistry.openEntity(entityId, { sourceGroupId })
    ↓
[openEntity logic above]
```

### Dragging entity out of preview panel

```
User drags entity from preview panel to slot/other panel
    ↓
moveViewToSlot/moveViewToNewPanel
    ↓
View is moved out, preview panel now has no view
    ↓
Preview panel stays open, shows "empty" state
    ↓
Panel.viewId = null (but panel still exists)
```

### User clicks in preview panel

```
User clicks anywhere in preview panel content
    ↓
EntityPanel.handleMouseDown
    ↓
viewRegistry.pinPreviewPanel()
    ↓
previewPanelId = null
    ↓
Tab updates to non-italic
Border updates to solid (or no special border)
```

## Open Questions

1. **Should preview panel have a close button?** Currently panels have close button. Closing should just clear previewPanelId and close panel normally.

2. **What if user manually moves preview panel to another group?** Should it stay as preview? (Probably yes)

3. **Should dragging a ref create a new permanent panel, or use preview?** Currently dragging creates new panel. Could have modifier (Alt) to use preview? Or keep simple - drag = permanent, click = preview.

4. **Focus vs Click for pinning:** Should just focusing the panel (via keyboard) pin it? Or only mouse clicks? (Leaning toward clicks only for less accidental pinning)

5. **Multiple refs clicked quickly:** Each replaces preview. Is there any debounce needed?

## Implementation Order

1. **ViewRegistry state changes:**
   - Add previewPanelId
   - Make Panel.viewId optional
   - Add preview-related methods

2. **openEntity logic update:**
   - Add sourceGroupId parameter
   - Implement preview panel usage/creation logic

3. **EntityPanel updates:**
   - Handle viewId: null
   - Add pinning click handler
   - Add visual styling (dotted border, message)

4. **Tab styling:**
   - Figure out how to make tab italic for preview panel

5. **RefLink updates:**
   - Pass sourceGroupId to openEntity

6. **Move operations:**
   - When moving from preview panel, leave panel empty instead of closing

7. **Handle panel close:**
   - Clear previewPanelId if preview panel is closed

## Edge Cases to Test

1. Click ref when no panels open (besides debug/entrypoint)
2. Click ref in same group as only preview panel
3. Click ref from hover preview card (no group)
4. Drag entity from preview panel to slot
5. Drag entity from preview panel to new panel
6. Close preview panel manually
7. Click in preview panel to pin, then click another ref
8. Preview panel in group A, click ref in group B (should use preview)
9. Multiple views of same entity exist, click ref (should jump, not use preview)

## Dockview API Notes

Getting group from panel:
```typescript
const panel = api.getPanel(panelId);
const groupId = panel?.group.id;
```

Adding panel to right of another:
```typescript
api.addPanel({
  id: newPanelId,
  component: 'entity',
  title: entityId,
  params: { viewId },
  position: {
    referencePanel: activePanelId,
    direction: 'right',
  },
});
```

Panel focus event:
```typescript
panel.api.onDidFocus(() => { ... });
// or
api.onDidActivePanelChange((e) => { ... });
```

Custom tab styling - need to investigate dockview options for this.
