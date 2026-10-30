# Panel Spike Codebase Overview

This document provides a guide to the panel spike codebase for understanding the architecture and locating code for modifications.

## High-Level Architecture

This is a React+TypeScript app using **dockview** for panel management and **react-reverse-portal** for decoupled rendering. The core concept is:

- **Entities** are data objects (transcripts, turns, judgements, etc.) with ownership/reference relationships
- **Views** are rendered instances of entities, managed by a central `ViewRegistry`
- **Slots** are locations within parent views where child entities render (inline)
- **Panels** are dockview tabs that display top-level views
- **Portals** decouple where content is rendered (InPortals in hidden container) from where it's displayed (OutPortals in panels/slots)

## Directory Structure

```
panel_spike/
├── spec.md              # Design specification document
├── overview.md          # This file
├── plans/               # Implementation plans (historical context)
└── ts/
    ├── main.tsx         # React entry point
    └── src/
        ├── App.tsx      # Root component, sets up ViewRegistry context
        ├── data/        # Entity types and example data
        ├── panels/      # Dockview panel components
        ├── render-manager/  # Core view/portal management
        └── renderers/   # Entity-type-specific UI components
```

## Key Concepts

### Entity System
- Entities have unique IDs and types (transcript, turn, judgement, etc.)
- Entities can **own** other entities (parent-child) or **reference** them
- Ownership is declared in `data/schema.ts` via `ownershipDeclarations`

### View System
- A **View** is a rendered instance of an entity with a portal node
- Views can be owned by a **Panel** (top-level) or a **Slot** (inline in parent)
- Multiple views can exist for the same entity (explicit duplication)
- The `ViewRegistry` manages all views, slots, and panels

### Portal System
- Content is rendered to hidden **InPortals** in `RenderRoot`
- **OutPortals** in panels/slots pull content for display
- This allows views to be moved/duplicated without re-rendering

## File Guide

### Data Layer (`ts/src/data/`)

| File | Purpose |
|------|---------|
| `schema.ts` | Entity type definitions (`Transcript`, `Turn`, `Judgement`, etc.), `ownershipDeclarations` mapping |
| `ownership.ts` | Computes ownership graph, exports `DataBundle`, helper functions (`getOwners`, `getRefs`, etc.) |
| `example-data.ts` | Test dataset with simple and overlapping collections |

**When to look here:** Adding new entity types, changing ownership semantics, modifying example data.

### Render Manager (`ts/src/render-manager/`)

| File | Purpose |
|------|---------|
| `ViewRegistry.ts` | **Core state store** - manages views, slots, panels. Handles navigation, duplication, drag/drop, preview panels. External store pattern with `useSyncExternalStore`. Key methods: `openEntityInPreview()`, `upgradePreviewToRegular()`, `isPanelPreview()`, `findViewPanel()`, `findAdjacentGroup()`, `getGroupLayout()` |
| `RenderManager.tsx` | **Main component** (`RenderRoot`) - renders InPortals, sets up dockview with sidebar, handles external drops, provides contexts |
| `contexts.tsx` | React contexts and hooks: `useViewRegistry()`, `useSlotContent()`, `useIsHighlighted()`, `useDataBundle()`, `useViewContext()`, `useGroupLayout()`, `useSetKeptOpen()`. `CurrentViewContext` provides `{ viewId, inHoverPreview }` |
| `EntityRenderer.tsx` | Renders entity content via registered renderer. Exports `RefLink` and `InlineRefLink` components (both wrapped with hover previews) |
| `Child.tsx` | Slot component for owned children. Shows view OutPortal or placeholder (with hover preview). Handles drop-to-move/duplicate |
| `registry.tsx` | Renderer registry context, `useRegisteredEntityRenderer()` hook (supports `full`, `ref`, `inline`, `preview` types) |
| `ViewContextMenu.tsx` | Right-click context menus for views, placeholders, refs |
| `DraggableID.tsx` | Draggable entity ID badge with backlinks count and hover preview |
| `HoverPreview.tsx` | Hover preview card wrappers: `EntityPreviewCard` (entity preview) and `BacklinksPreviewCard` (backlinks list) |
| `GroupSidebar.tsx` | Sidebar component showing all groups and panels. Uses ref renderers for entity panels, click to focus |
| `ResizableSidebar.tsx` | Resizable container with drag handle for the sidebar |

**When to look here:**
- Changing view lifecycle: `ViewRegistry.ts`
- Modifying how views are rendered: `RenderManager.tsx`, `EntityRenderer.tsx`
- Changing slot/placeholder behavior: `Child.tsx`
- Context menu options: `ViewContextMenu.tsx`
- Adding new hooks: `contexts.tsx`

### Panels (`ts/src/panels/`)

| File | Purpose |
|------|---------|
| `EntityPanel.tsx` | Displays a single entity view with parents/backlinks sections. Provides `CurrentViewContext` for RefLinks. Handles click-to-upgrade for preview panels |
| `EntrypointPanel.tsx` | Lists top-level entities as clickable navigation |
| `DebugPanel.tsx` | Shows ViewRegistry state (views, slots, panels) and entity relationships |

**When to look here:** Modifying panel UI, adding panel types.

### Renderers (`ts/src/renderers/`)

| File | Purpose |
|------|---------|
| `types.ts` | Prop interfaces: `FullRendererProps`, `RefRendererProps`, `InlineRefRendererProps`, `PreviewRendererProps` |
| `helpers.ts` | `getTypeColor()`, `truncateMiddle()`, `truncateEnd()` |
| `index.ts` | Builds `defaultRegistry` mapping entity types to renderers |
| `by_entity_type/*.tsx` | Type-specific renderers (one file per entity type) |

**Renderer types per entity:**
- `full` - Card display with complete content
- `ref` - Block reference (one-line, clickable)
- `inline` - Minimal inline reference
- `preview` - Compact preview for hover cards (children shown as RefLinks)

**When to look here:** Changing how specific entity types display, adding renderers for new types.

### Entity Renderers (`ts/src/renderers/by_entity_type/`)

| File | Entity Type | Notes |
|------|-------------|-------|
| `collection.tsx` | Collection | Shows items as `<Child>` components |
| `transcript.tsx` | Transcript | Shows topic, turns as `<Child>` |
| `turn.tsx` | Turn | Shows role, message, previous turn ref |
| `judgement.tsx` | Judgement | Shows score, explanation, rubric/turn refs |
| `judgement-set.tsx` | JudgementSet | Shows judgements as `<Child>` |
| `rubric.tsx` | Rubric | Shows name, description |
| `misconfigured.tsx` | MisconfiguredObject | Intentionally wrong ownership for testing edge cases |

Each entity type has a unique color (defined in `helpers.ts`)

## Key Patterns

### External Store Pattern
`ViewRegistry` is an external store that notifies React via `useSyncExternalStore`. This prevents re-render loops and enables granular subscriptions.

### Slot Registration Flow
1. `Child` component calls `useSlotContent()` with slotId, entityId, parentViewId
2. Slot registers with ViewRegistry
3. If first slot for entity: creates new view, slot owns it
4. If view exists: slot becomes placeholder pointing to existing view
5. On panel close: view may rebind to waiting slot

### Renderer Context Usage
Renderers use these patterns:
- `<Child entityId={...}>` - render owned children (may expand or show placeholder)
- `<RefLink entityId={...} direction="right|left">` - block reference link (clickable, with hover preview). Direction controls which side preview opens.
- `<InlineRefLink entityId={...} direction="right|left">` - inline reference in text (with hover preview)
- `useViewContext()` - get `{ viewId, inHoverPreview }` for source panel lookup
- `getData(data, entityId)` - fetch entity from DataBundle

### Hover Previews
- **Entity preview**: Shows when hovering `RefLink`, `InlineRefLink`, or `Placeholder` - displays compact entity preview
- **Backlinks preview**: Shows when hovering `DraggableID` (if entity has backlinks) - displays list of referencing entities
- Nested hovers supported: hovering items inside a preview card keeps the parent open
- Uses `HoverKeptOpenContext` to propagate open state up the component tree
- Uses `@base-ui/react/preview-card` for the popover UI
- `CurrentViewContext` provides `{ viewId, inHoverPreview }` - viewId is preserved in hovers for source panel lookup

### Preview Panels & Directional Opening
RefLink clicks open entities in **preview panels** with directional positioning:
- **Forward refs** (owned children, regular refs) → open in a group to the **right**
- **Backward refs** (parents, backlinks) → open in a group to the **left**
- Uses `direction` prop on `RefLink`/`InlineRefLink` (defaults to `'right'`)

**RefLink click flow** (in `openEntityInPreview`):
1. If entity already has a view → jump to it and upgrade if it's a preview (this is how clicking twice "pins")
2. Otherwise, find adjacent group in the specified direction
3. If adjacent group has a preview panel → replace its contents
4. Otherwise, create new preview panel (in adjacent group or new group)

**Preview panel behavior:**
- Tab title prefixed with "(preview)"
- Shown with `[preview]` tag in DebugPanel
- At most one preview panel per dockview group
- Clicking another ref in that direction reuses/replaces the preview panel
- **Upgrade triggers** (preview → regular): clicking panel content, moving panel, or jumping to existing view in preview

**Group finding algorithm** (in `findAdjacentGroup`):
1. Get source panel's group bounding rect
2. Find adjacent groups whose edge aligns (within 5px tolerance)
3. Pick the one with closest vertical center
4. If none found, create new group in that direction

### Group Sidebar
A resizable sidebar on the left side of the dockview shows all panel groups and their contents:
- Groups labeled "Group 1", "Group 2", etc.
- Entity panels display using their `ref` renderer (type-colored, compact)
- Non-entity panels (Debug, Collections) show just their title
- Preview panels shown in *italics*
- Active/focused panel highlighted with blue left border
- Click any panel to focus it
- Hover over entity panels to see preview tooltip (uses `EntityPreviewCard`)
- Sidebar is resizable via drag handle (150-400px range)
- Collapse button (◀/▶) shrinks sidebar to icon-only width (~32px)

**Implementation:**
- `GroupSidebar.tsx` - renders groups and panels, collapse button, wraps entity items with `EntityPreviewCard`
- `ResizableSidebar.tsx` - resizable container with `collapsed` prop for minimal width mode
- `RenderManager.tsx` - manages `sidebarCollapsed` state, passes to both sidebar components
- `ViewRegistry.getGroupLayout()` - returns current group/panel structure (cached for React)
- `useGroupLayout()` hook subscribes to layout changes via `useSyncExternalStore`
- Dockview events (`onDidAddGroup`, `onDidRemoveGroup`, `onDidAddPanel`, `onDidActivePanelChange`) invalidate cache and trigger updates

## Plans Folder Reference

The `plans/` folder contains implementation plans. Key ones:

| File | Topic |
|------|-------|
| `portal_management_v2.md` | **ViewRegistry system design** - external store, slot management, view lifecycle |
| `entity_rendering.md` | View management, renderer registry, EntityPanel |
| `phase2b_dnd.md` | Drag-and-drop implementation |
| `phase2a_duplication_pending.md` | Recursive view duplication |
| `phase2a_close_subview.md` | Closing nested subviews |
| `phase2b_ent_panel_show_backlinks.md` | Parents/backlinks in panels |
| `phase2b_renderer_registry.md` | Dynamic renderer registry system |
| `phase_2d_alt1_preview_newtab.md` | **Preview panels** - directional opening, group finding algorithm |
| `phase_2e_group_sidebar.md` | **Group sidebar** - navigable panel list outside dockview |

Other files are scratch notes or earlier iterations.
