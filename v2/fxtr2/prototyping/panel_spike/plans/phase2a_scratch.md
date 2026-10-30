# Phase 2a Design Scratch

## Current Component Structure

```
EntityPanel (dockview panel)
  └── OutPortal → view.portalNode
        └── EntityRenderer
              ├── type-specific content
              ├── <Child entityId=X> (for owned children)
              │     └── OutPortal (if owns view) OR Placeholder (if elsewhere)
              └── <RefLink entityId=X> (for refs)
```

## Four Contexts for Right-Click Menus

| Context | What it is | Possible actions |
|---------|-----------|------------------|
| **Placeholder** | Child showing "open elsewhere" | Move here, Open new here |
| **Child (owning)** | Child showing OutPortal | Move to new tab, Open another in tab |
| **Root entity** | Top of EntityPanel | Open another in tab |
| **RefLink** | Reference link | Open in tab, Jump to existing |

## Questions/Clarifications

1. **"Move entity view here"** on placeholder - this means the view currently exists somewhere (panel or another slot), and we want to move it to THIS slot. So the view's owner changes. If the view was in a panel, that panel closes?

2. **"Move entity view to new tab"** on child - the child currently owns a view inline. We move that view to a new panel? So the child becomes a placeholder (or goes away if it was the only slot?).

3. **What happens when a view "moves"?** The InPortal stays (same portalNode), but the OutPortal unmounts from one place and mounts in another. Need to coordinate this carefully.

4. **Focus panel** - what API does dockview provide to focus/activate a panel?

5. **Scroll into view** - views can be deeply nested. Need ref to DOM element. Also scrolling happens within the panel's overflow container.

## Navigation: Finding a View's Location

```typescript
function findViewPanel(viewId: ViewId): PanelId | null {
  const view = state.views.get(viewId);
  if (!view) return null;

  // Panel-owned
  if (view.panelId) return view.panelId;

  // Slot-owned - trace up through parent views
  if (view.ownerSlotId) {
    const slot = state.slots.get(view.ownerSlotId);
    if (slot) {
      // Recursively find panel for parent view
      return findViewPanel(slot.parentViewId);  // This doesn't work - parentViewId is a ViewId, not viewId of view that owns the slot...
    }
  }

  return null;
}
```

Wait, `slot.parentViewId` IS the view ID of the view that contains this slot. So we can recursively look that up:

```typescript
function findViewPanel(viewId: ViewId): PanelId | null {
  const view = state.views.get(viewId);
  if (!view) return null;

  if (view.panelId) return view.panelId;

  if (view.ownerSlotId) {
    const slot = state.slots.get(view.ownerSlotId);
    if (slot) {
      // The slot is inside another view - find that view's panel
      return findViewPanel(slot.parentViewId);
    }
  }

  return null;  // Orphaned view?
}
```

## Highlight State

Registry tracks:
```typescript
let highlightedViewId: ViewId | null = null;
let highlightTimeoutId: number | null = null;

function highlightView(viewId: ViewId, durationMs: number = 1500) {
  if (highlightTimeoutId) clearTimeout(highlightTimeoutId);
  highlightedViewId = viewId;
  notifyViews();  // or separate highlight listeners?

  highlightTimeoutId = setTimeout(() => {
    highlightedViewId = null;
    notifyViews();
    highlightTimeoutId = null;
  }, durationMs);
}
```

Components check `highlightedViewId` and add CSS class.

Alternative: manage highlight purely in CSS with animation that auto-clears?

## DOM Refs for Scrolling

Options:
1. **Ref registry in ViewRegistry** - components register their DOM element
2. **data-view-id attribute** - query selector to find element
3. **React refs via context** - more complex

Option 2 seems simplest:
```typescript
// In EntityRenderer wrapper
<div data-view-id={viewId}>
  ...content...
</div>

// To scroll:
const element = document.querySelector(`[data-view-id="${viewId}"]`);
element?.scrollIntoView({ behavior: 'smooth', block: 'center' });
```

## New ViewRegistry Methods Needed

```typescript
interface ViewRegistry {
  // Existing...

  // Phase 2a additions:

  // Navigation
  jumpToView(viewId: ViewId): void;  // Focus panel, scroll, highlight

  // Query
  findViewPanel(viewId: ViewId): PanelId | null;
  getViewsForEntity(entityId: EntityId): View[];  // Make public

  // Highlight
  getHighlightedViewId(): ViewId | null;
  subscribeHighlight(listener: () => void): () => void;

  // View manipulation for context menus
  moveViewToSlot(viewId: ViewId, slotId: SlotId): void;
  moveViewToNewPanel(viewId: ViewId): void;
  createViewInSlot(entityId: EntityId, slotId: SlotId): void;
  createViewInNewPanel(entityId: EntityId): void;  // essentially openEntity
}
```

## Context Menu Component Structure

Using base-ui ContextMenu:

```tsx
import { Menu, Trigger, Positioner, Popup, Item } from '@base-ui-components/react/menu';

interface ViewContextMenuProps {
  children: React.ReactNode;
  context:
    | { type: 'placeholder'; entityId: EntityId; slotId: SlotId }
    | { type: 'child'; entityId: EntityId; viewId: ViewId }
    | { type: 'root'; entityId: EntityId; viewId: ViewId }
    | { type: 'ref'; entityId: EntityId };
}

function ViewContextMenu({ children, context }: ViewContextMenuProps) {
  const viewRegistry = useViewRegistry();

  const handleContextMenu = (e: React.MouseEvent) => {
    if (e.shiftKey) {
      // Let browser handle it
      return;
    }
    // base-ui handles this automatically if we use Menu with trigger="contextMenu"?
  };

  return (
    <Menu>
      <Trigger render={<span onContextMenu={handleContextMenu}>{children}</span>} />
      <Positioner>
        <Popup>
          {/* Dynamic items based on context */}
          {context.type === 'placeholder' && (
            <>
              <Item onClick={() => handleMoveHere()}>Move entity view here</Item>
              <Item onClick={() => handleOpenNewHere()}>Open new entity view here</Item>
            </>
          )}
          {/* ... other context types ... */}
          <Item disabled style={{ opacity: 0.5 }}>
            (hold shift for browser's context menu)
          </Item>
        </Popup>
      </Positioner>
    </Menu>
  );
}
```

## Open Questions for Daniel

1. When "Move entity view here" on a placeholder, if the view is currently in a panel, should that panel close? Or should a placeholder appear there?

2. For "Jump to existing view" on a ref - if multiple views exist for the entity, which one? First one? Closest one? Should show submenu?

3. Should highlighting affect just the border? Background flash? What color/style?

4. Are there any other actions beyond what's listed?
