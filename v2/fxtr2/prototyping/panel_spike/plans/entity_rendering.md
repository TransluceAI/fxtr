# Panel Spike: Entity Rendering

This phase adds actual entity rendering, view management, and interactions.
Depends on `basics.md` being complete.

**Note:** This plan needs more detailed thought before implementation.
The sections below capture the requirements from the spec but may need refinement.

---

## Overview

After basics are complete, we add:
1. View management (tracking rendered entity instances)
2. Portal-based rendering (InPortal/OutPortal for movable views)
3. Renderer registry (Full, Preview, Ref renderers per type)
4. RenderContext for renderers to use
5. EntityPanel for viewing entities
6. Interactions (click to open, hover previews, right-click menus)

---

## View Management

### Types

```typescript
type ViewId = string;       // unique per view instance
type EntityId = string;     // from data layer
type PanelId = string;      // dockview panel ID

type ViewLocation =
  | { type: 'panel'; panelId: PanelId }
  | { type: 'inline'; parentViewId: ViewId };

interface ViewInstance {
  viewId: ViewId;
  entityId: EntityId;
  location: ViewLocation;
  portalNode: HtmlPortalNode;
}

interface RenderManagerState {
  views: Map<ViewId, ViewInstance>;
  viewsByEntity: Map<EntityId, ViewId[]>;
  inlineChildren: Map<ViewId, Set<ViewId>>;
}
```

### Key Behaviors

- One entity can have multiple views (when user explicitly copies)
- By default, if entity is already rendered, new render attempts show placeholder
- Closing a panel may trigger orphan adoption (view moves to placeholder location)
- Views can be "moved" (same portal, different OutPortal location) or "copied" (new portal)

---

## Portal-Based Rendering

```
RenderManager
├── InPortalContainer (hidden, renders all view content)
│   ├── InPortal[view1] → EntityRenderer(entity1)
│   ├── InPortal[view2] → EntityRenderer(entity2)
│   └── ...
│
└── DockviewContainer
    ├── EntityPanel(viewId: view1) → OutPortal[view1]
    └── ...
```

Benefits:
- Views preserve React state when moved between locations
- Single source of truth for each view's content
- Clean separation between content and location

---

## RenderContext

```typescript
interface RenderContextValue {
  // Components for renderers to use
  Child: React.FC<{ entityId: EntityId; customArgs?: Record<string, unknown> }>;
  Ref: React.FC<{ entityId: EntityId; customArgs?: Record<string, unknown> }>;
  Link: React.FC<{ entityId: EntityId; children: ReactNode }>;

  // Current render context
  currentViewId: ViewId;
  currentEntityId: EntityId;
  isTopLevel: boolean;

  // Actions
  openEntity: (entityId: EntityId) => void;
  focusView: (viewId: ViewId) => void;

  // For debug panel
  getState: () => RenderManagerState;
}
```

### Child Component Behavior

When `<Child entityId={X} />` renders:
1. Check if entity X has existing view elsewhere
2. If yes → render Placeholder (link to existing view)
3. If no → request inline view, render OutPortal

### Placeholder Behavior

- Shows as one-line indicator: "[Turn abc123...] open elsewhere"
- Click → focus the existing view
- Right-click → menu: "Move entity here" / "Open copy here"

---

## Renderer Registry

```typescript
interface FullRendererProps {
  id: EntityId;
  data: Entity;
  isTopLevel: boolean;
  customArgs?: Record<string, unknown>;
}

interface PreviewRendererProps {
  id: EntityId;
  data: Entity;
}

interface RefRendererProps {
  id: EntityId;
  data: Entity;
  isOwned: boolean;
  fillSpace: boolean;
  customArgs?: Record<string, unknown>;
}

interface RendererSet {
  Full: React.FC<FullRendererProps>;
  Preview: React.FC<PreviewRendererProps>;
  Ref: React.FC<RefRendererProps>;
}

const rendererRegistry: Map<EntityType, RendererSet>;
```

### Renderer Guidelines

**Full Renderer:**
- Renders complete card view of entity
- Uses `<Child>` for owned children (renders inline or placeholder)
- Uses `<Ref>` for references (always one-line link)
- Can have React state (one copy visible at a time via portal)

**Preview Renderer:**
- Smaller card for hover previews
- Shows truncated content
- Children shown as Refs, not inline

**Ref Renderer:**
- One-line inline element
- Shows type, truncated ID, maybe key info
- Click opens entity, hover shows preview

---

## EntityPanel

```typescript
interface EntityPanelParams {
  viewId: ViewId;
}

function EntityPanel({ params }: IDockviewPanelProps<EntityPanelParams>) {
  // Renders:
  // - Parent refs (owners) above content
  // - OutPortal for the view's content
  // - Backrefs (entities that reference this one) below content
}
```

### Preview Panel Behavior

- At most one panel is the "preview panel"
- Preview panel has italic title
- Clicking refs opens in preview panel (replacing content)
- Clicking the preview panel itself promotes it to regular panel

---

## Interactions

### Click Ref → Open Entity

1. Call `openEntity(entityId)`
2. If entity has existing view → focus it
3. If no existing view → create new view in panel

### Hover Ref → Preview

1. PreviewCard popup appears
2. Contains PreviewRenderer for entity
3. Preview can contain Refs which can also be hovered (recursive)

### Right-Click Entity/Placeholder → Context Menu

Options:
- "Open in new panel" (creates copy)
- "Move entity here" (on placeholder - adopts existing view)
- "Open copy here" (on placeholder - creates new view)

### Panel Close → Orphan Handling

1. Find view(s) that were in this panel
2. For each view that was the last view for its entity:
   - Check if any placeholder wants this entity
   - If yes → reparent view to that location
   - If no → destroy view

---

## Mismatch Detection

RenderManager tracks actual ownership (from data layer) and detects when:
- A renderer tries to render something as owned child that isn't actually owned
- A renderer tries to render something as ref that is actually owned

Display warning box around mismatched renders.

---

## File Additions

```
ts/src/
├── render-manager/
│   ├── RenderManager.tsx   # Full implementation
│   ├── context.ts          # RenderContext + provider
│   ├── types.ts            # ViewInstance, ViewLocation, etc.
│   └── hooks.ts            # useChildView, etc.
│
├── panels/
│   ├── EntityPanel.tsx     # Full implementation
│   └── Placeholder.tsx     # "Open elsewhere" component
│
└── renderers/
    ├── registry.ts
    └── components/
        ├── TranscriptRenderer.tsx
        ├── TurnRenderer.tsx
        ├── JudgementRenderer.tsx
        ├── JudgementSetRenderer.tsx
        ├── CollectionRenderer.tsx
        └── MisconfiguredRenderer.tsx
```

---

## Open Questions

- Exact behavior when multiple placeholders exist for orphaned view?
- How to handle preview panel state across dockview layout changes?
- Should inline views have any visual indicator that they're "live" (vs static)?
- Performance considerations for many InPortals?
