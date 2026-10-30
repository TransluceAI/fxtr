# Phase 2 Step 10.2: Dynamic Renderer API Redesign

## Overview

Refactor the custom renderer API to align with the panel_spike prototype, making each renderer a self-contained component responsible for rendering its data in multiple contexts. This is prep work for eventually integrating the full panel_spike view system.

**Key changes:**
- Rename `ThingLink` → `NodeRef` and `ThingView` → `ChildNodeView`
- Change from single default export to named `FullRenderer` and `RefRenderer` exports
- Update props interface to include `DraggableIDMarker`, `isTopLevel`, etc.
- Use `RefRenderer` wherever we show thing references (ThingsList, RootsPanel, NodeRef)
- Separate fallback renderers for Full and Ref variants

**Future work (not in this plan):**
- `InlineRefRenderer` and `PreviewRenderer` variants (4 total like panel_spike)
- Full drag-and-drop integration with dockview panels
- Hover preview cards

## Current State

### Current Props (ThingWatcherBindings.tsx)
```typescript
interface ThingRendererProps {
  payload: unknown;
  dataChunks: unknown[];
  links: LinkInfo[];
  triggerable: boolean;
  trigger: (payload: unknown | null) => Promise<TriggerResult>;
  args: Record<string, unknown>;
}
```

### Current Child Components
- `ThingLink`: Clickable badge that navigates to another thing
- `ThingView`: Renders another thing inline with its full custom renderer

### Current Usage
- `DynamicRenderer` compiles TSX and renders with a single default export
- DetailsPanel, ThingViewPanel, EmbedApp all use DynamicRenderer for custom renderers
- ThingsList shows a simple list item (shortId + thingType), not using custom renderers

## Target State

### New Renderer Exports (named, not default)
```typescript
// Custom renderers export these named functions
export function FullRenderer(props: FullRendererProps): JSX.Element { ... }
export function RefRenderer(props: RefRendererProps): JSX.Element { ... }
```

### New Props Interfaces
```typescript
interface FullRendererProps {
  /** First chunk data - use for simple single-chunk things */
  payload: unknown;
  /** All chunk data in order (oldest first) */
  dataChunks: unknown[];
  /** Links from this thing to others */
  links: LinkInfo[];
  /** Whether this thing can be triggered */
  triggerable: boolean;
  /** Trigger function */
  trigger: (payload: unknown | null) => Promise<TriggerResult>;
  /** Custom args from parent (including potentially isOwned, etc.) */
  args: Record<string, unknown>;
  /** True if this is a top-level panel view, false if nested */
  isTopLevel: boolean;
  /** Component to wrap the draggable ID element */
  DraggableIDMarker: React.FC<{ children: React.ReactNode }>;
}

interface RefRendererProps {
  /** First chunk data */
  payload: unknown;
  /** All chunk data in order */
  dataChunks: unknown[];
  /** Links from this thing */
  links: LinkInfo[];
  /** Whether this thing can be triggered */
  triggerable: boolean;
  /** Custom args from parent */
  args: Record<string, unknown>;
  // Note: No DraggableIDMarker - the entire ref is draggable, handled by NodeRef wrapper
}
```

### Renamed Child Components
- `ThingLink` → `NodeRef`: Clickable reference to another thing, uses RefRenderer
- `ThingView` → `ChildNodeView`: Renders another thing inline with FullRenderer

```typescript
interface NodeRefProps {
  /** Thing key as base64 string */
  thingKey?: string;
  /** Thing key as JSON value */
  keyJSON?: unknown;
  /** Custom args to pass to the RefRenderer */
  args?: Record<string, unknown>;
}

interface ChildNodeViewProps {
  /** Thing key as base64 string */
  thingKey?: string;
  /** Thing key as JSON value */
  keyJSON?: unknown;
  /** Custom args to pass to the FullRenderer */
  args?: Record<string, unknown>;
}
```

### Component Architecture for Reducing Duplication

Currently, DetailsPanel, ThingViewPanel, EmbedApp, and ThingView (soon ChildNodeView) all have similar logic for rendering a thing with its custom renderer. We want to factor this into a layered architecture:

```
┌─────────────────────────────────────────────────────────────────┐
│                     User-Facing Components                       │
│  (exported to custom renderers via thingwatcher-bindings)       │
├─────────────────────────────────────────────────────────────────┤
│  ChildNodeView                    NodeRef                        │
│  - Takes thingKey/keyJSON         - Takes thingKey/keyJSON      │
│  - Resolves key                   - Resolves key                │
│  - Calls LowLevelNodeView         - Renders RefRenderer         │
│    with isTopLevel: false         - Entire ref is draggable     │
└───────────────┬─────────────────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Internal Components                           │
│  (used by our app, not exported to custom renderers)            │
├─────────────────────────────────────────────────────────────────┤
│  LowLevelNodeView                                                │
│  - Takes: thingKey, isTopLevel, args                            │
│  - Fetches thing data                                           │
│  - Creates DraggableIDMarker bound to thing's key               │
│  - Gets renderer source for thing type                          │
│  - Builds FullRendererProps                                     │
│  - Renders with DynamicFullRenderer (or FallbackFullRenderer)   │
│  - Just the renderer output, no panel chrome                    │
├─────────────────────────────────────────────────────────────────┤
│  ThingPanelContent                                               │
│  - Takes: thingKey, onSelectThing, triggerRecords, etc.         │
│  - Fetches thing for meta/backlinks (cached via graphcache)     │
│  - Renders: meta header (shortId, key, type badge)              │
│  - Renders: LowLevelNodeView with isTopLevel: true              │
│  - Renders: backlinks section                                   │
│  - Renders: trigger records section                             │
└─────────────────────────────────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Panel Wrappers                              │
│  (thin wrappers for dockview integration)                       │
├─────────────────────────────────────────────────────────────────┤
│  DetailsPanel                     ThingViewPanel                 │
│  - Panel header "Details"         - Gets thingKey from params   │
│  - Gets selectedThingKey          - Dynamic panel title         │
│    from context                   - Renders ThingPanelContent   │
│  - Renders ThingPanelContent                                    │
├─────────────────────────────────────────────────────────────────┤
│  EmbedApp                                                        │
│  - Gets thingKey from postMessage                               │
│  - Renders ThingPanelContent (or just LowLevelNodeView?)        │
│  - No dockview, standalone embed                                │
└─────────────────────────────────────────────────────────────────┘
```

**Key design decisions:**

1. **LowLevelNodeView** is the core internal component that handles:
   - Data fetching (via hooks)
   - DraggableIDMarker creation
   - Renderer props building
   - Actual rendering (dynamic or fallback)
   - It does NOT include panel chrome (meta header, backlinks, etc.)

2. **ChildNodeView** is the user-facing wrapper with a stable public interface:
   - Accepts `thingKey` or `keyJSON` (resolves to key)
   - Passes `isTopLevel: false` to LowLevelNodeView
   - This is what custom renderers import from `thingwatcher-bindings`

3. **ThingPanelContent** combines LowLevelNodeView with panel chrome:
   - Meta header (type badge, shortId, key display)
   - LowLevelNodeView with `isTopLevel: true`
   - Backlinks section
   - Trigger records section
   - Used by both DetailsPanel and ThingViewPanel

4. **DetailsPanel and ThingViewPanel** become thin wrappers:
   - DetailsPanel: gets key from selection context, renders ThingPanelContent
   - ThingViewPanel: gets key from panel params, renders ThingPanelContent

5. **EmbedApp** can either use ThingPanelContent (with full chrome) or LowLevelNodeView (minimal), depending on desired UX.

This architecture reduces duplication by:
- Single place for data fetching + renderer props building (LowLevelNodeView)
- Single place for panel chrome (ThingPanelContent)
- Thin panel wrappers that just wire up the key source

## Implementation Plan

### 1. Update Type Definitions

**File: `lib/ThingWatcherBindings.tsx`**

- [x] Add new interfaces: `FullRendererProps`, `RefRendererProps`
- [x] Update `NodeRefProps` (renamed from ThingLinkProps) with `args`
- [x] Update `ChildNodeViewProps` (renamed from ThingViewProps) - already has `args`
- [x] Remove old `ThingRendererProps` (clean break, no deprecated types)
- [x] Remove `renderDepth` from `RendererContextValue` (depth handled differently in future)

### 2. Create DraggableIDMarker Component

**File: `lib/ThingWatcherBindings.tsx`** (or new file `lib/DraggableIDMarker.tsx`)

Create a stub component that:
- Wraps children with `display: contents` (no extra DOM node affecting layout)
- Attaches drag handlers (dragstart sets data, but no drop targets yet)
- Styled to look draggable (cursor: grab)
- Passes thing key via drag data for future use

```typescript
interface DraggableIDMarkerProps {
  children: React.ReactNode;
}

// Created by the renderer system, bound to current thing's key
function createDraggableIDMarker(thingKey: string): React.FC<DraggableIDMarkerProps> {
  return function DraggableIDMarker({ children }) {
    // PLACEHOLDER: This drag handler is a stub. The actual drag-drop behavior
    // will be implemented differently when we integrate the panel system.
    // For now, this just makes the element draggable without functional drops.
    const handleDragStart = (e: React.DragEvent) => {
      e.dataTransfer.setData('application/x-thingwatcher-key', thingKey);
      e.dataTransfer.setData('text/plain', thingKey);
      e.dataTransfer.effectAllowed = 'copyMove';
    };

    return (
      <span draggable onDragStart={handleDragStart} style={{ display: 'contents', cursor: 'grab' }}>
        {children}
      </span>
    );
  };
}
```

### 3. Update Renderer Compilation

**File: `components/DynamicRenderer/rendererCache.ts`**

- [x] Change compilation to expect named exports (`FullRenderer`, `RefRenderer`)
- [x] Return object with `{ full, ref }` where each can be null if not exported
- [x] Cache structure: `Map<string, { full: Component | null, ref: Component | null }>`

**File: `components/DynamicRenderer/DynamicRenderer.tsx`**

- [x] Split into two components: `DynamicFullRenderer` and `DynamicRefRenderer`
- [x] Each takes `thingType`, `tsxCode`, and their respective props
- [x] Each has its own error boundary and fallback handling

### 4. Create Fallback Renderers

**File: `components/DynamicRenderer/FallbackRenderers.tsx`** (new file)

Built-in fallback components used when custom renderers don't export a variant:

```typescript
// FallbackFullRenderer - shows JSON tree, links, trigger form
export function FallbackFullRenderer(props: FullRendererProps): JSX.Element { ... }

// FallbackRefRenderer - simple: type badge + keyJSON
export function FallbackRefRenderer(props: RefRendererProps & { shortId: number; thingType: string; keyJSON: unknown }): JSX.Element { ... }
```

The fallback ref renderer should be simple: just show a type badge and keyJSON (or shortId if keyJSON is null). Can expand later if needed.

### 5. Create Internal Components (LowLevelNodeView, ThingPanelContent)

**File: `lib/LowLevelNodeView.tsx`** (new file, internal)

- [x] Create `LowLevelNodeView` component
- [x] Props: `{ thingKey: string, isTopLevel: boolean, args?: Record<string, unknown> }`
- [x] Fetches thing data using existing hooks
- [x] Creates DraggableIDMarker bound to thing's key
- [x] Gets renderer source for thing type
- [x] Builds FullRendererProps
- [x] Renders using DynamicFullRenderer (or FallbackFullRenderer)
- [x] Returns just the renderer output, no panel chrome

**File: `components/ThingPanelContent/ThingPanelContent.tsx`** (new file, internal)

- [x] Create `ThingPanelContent` component
- [x] Props: `{ thingKey: string, onSelectThing, triggerRecords, onTrigger }`
- [x] Fetches thing for meta info and backlinks (cached via graphcache)
- [x] Renders meta header (type badge, shortId, key display)
- [x] Renders `LowLevelNodeView` with `isTopLevel: true`
- [x] Renders backlinks section
- [x] Renders trigger records section

### 6. Create User-Facing Components (NodeRef, ChildNodeView)

**Note:** These are exported via `thingwatcher-bindings` for use in custom renderers.

**File: `lib/ThingWatcherBindings.tsx`**

- [x] Rename `ThingLink` → `NodeRef`
- [x] Rename `ThingView` → `ChildNodeView`
- [x] Remove old names entirely (clean break, updating all usages in one go)

**NodeRef behavior:**
1. Fetches thing data using existing hooks
2. Gets renderer source for thing type
3. Renders using DynamicRefRenderer (or fallback)
4. Wraps in ThingContextMenu for right-click
5. Click navigates/selects the thing
6. Entire component is draggable (no internal DraggableIDMarker)

**ChildNodeView behavior:**
1. Resolves thingKey/keyJSON to a key
2. Calls `LowLevelNodeView` with `isTopLevel: false`
3. Simple wrapper with stable public interface

### 7. Update Places That Render Things

#### ThingsList
**File: `components/ThingsList/ThingsList.tsx`**

- [x] Import `NodeRef`
- [x] Replace the current inline rendering with `NodeRef` for each item
- [x] Data is cached via graphcache, so re-fetching in NodeRef is fine

#### RootsPanel
**File: `components/RootsPanel/RootsPanel.tsx`**

- [x] Use `NodeRef` for root thing display
- [x] Roots already resolve to things, data cached via graphcache

#### DetailsPanel
**File: `components/DetailsPanel/DetailsPanel.tsx`**

- [x] Refactor to be a thin wrapper around `ThingPanelContent`
- [x] Gets selectedThingKey from context (or props)
- [x] Renders panel header "Details" + `ThingPanelContent`
- [x] Most logic moves to ThingPanelContent

#### ThingViewPanel
**File: `components/DockviewLayout/ThingViewPanel.tsx`**

- [x] Refactor to be a thin wrapper around `ThingPanelContent`
- [x] Gets thingKey from panel params
- [x] Updates panel title dynamically
- [x] Renders `ThingPanelContent`

#### EmbedApp
**File: `EmbedApp.tsx`**

- [x] Update to use `ThingPanelContent`
- [x] Gets thingKey from postMessage
- [x] Shows full chrome (meta header, backlinks, trigger records)

### 8. Update Existing Custom Renderers

Any existing custom renderers in examples need to be updated to use the new export format:

```typescript
// Old format
export default function MyRenderer({ payload, links, triggerable, trigger }) { ... }

// New format
export function FullRenderer({ payload, links, triggerable, trigger, isTopLevel, DraggableIDMarker }) {
  return (
    <div>
      <DraggableIDMarker>
        <span className="thing-id">#{shortId}</span>
      </DraggableIDMarker>
      {/* rest of renderer */}
    </div>
  );
}

export function RefRenderer({ payload, args }) {
  // The entire ref is made draggable by NodeRef wrapper, no marker needed
  return (
    <span className="thing-ref">{payload.title}</span>
  );
}
```

### 9. Update CODE_OVERVIEW.md

- [x] Document the new renderer API
- [x] Update component descriptions in LOCAL_CODE_OVERVIEW.md
- [x] Note the rename of ThingLink/ThingView

## Migration Strategy

This is a clean break - we update everything in one go:

1. Update compilation to expect named exports (`FullRenderer`, `RefRenderer`)
2. Update all internal components to use new props/components
3. Update all example renderers to new format
4. Update documentation

No backwards compatibility with old default export format - all renderers must be updated to the new named export format.

## File Changes Summary

### New Files
- `lib/LowLevelNodeView.tsx` - internal component for rendering a thing with FullRenderer
- `lib/DraggableIDMarker.tsx` (or inline in LowLevelNodeView)
- `components/ThingPanelContent/ThingPanelContent.tsx` - shared panel chrome + LowLevelNodeView
- `components/DynamicRenderer/FallbackRenderers.tsx` - fallback Full and Ref renderers
- `components/DynamicRenderer/DynamicRefRenderer.tsx` (or fold into DynamicRenderer.tsx)

### Modified Files
- `lib/ThingWatcherBindings.tsx` - new types, NodeRef, ChildNodeView (wraps LowLevelNodeView)
- `components/DynamicRenderer/DynamicRenderer.tsx` - split into Full/Ref
- `components/DynamicRenderer/rendererCache.ts` - handle named exports
- `components/DetailsPanel/DetailsPanel.tsx` - thin wrapper around ThingPanelContent
- `components/ThingsList/ThingsList.tsx` - use NodeRef
- `components/RootsPanel/RootsPanel.tsx` - use NodeRef
- `components/DockviewLayout/ThingViewPanel.tsx` - thin wrapper around ThingPanelContent
- `EmbedApp.tsx` - use ThingPanelContent
- `frontend/LOCAL_CODE_OVERVIEW.md` - update component docs

### Example Files to Update
- Any demo renderers in `examples/`

## Testing Considerations

1. Test fallback rendering when custom renderer is missing Full or Ref export
2. Test NodeRef and ChildNodeView with various key formats (thingKey vs keyJSON)
3. Test in ThingsList, RootsPanel, DetailsPanel contexts
4. Test DraggableIDMarker has draggable appearance/behavior (even without drops)
5. Test in EmbedApp context
6. Verify all example renderers updated and working

## Open Questions / Future Work

- **Inline and Preview variants**: Will add `InlineRefRenderer` and `PreviewRenderer` after proving out this design
- **Drop targets**: DraggableIDMarker will have drag handlers but no working drops until panel integration
- **Direction prop on NodeRef**: Panel_spike has `direction` for which side to open panels - defer until panel integration
- **isOwned styling**: Currently removed; if needed, pass via `args` from parent renderer

## Post-Implementation Notes

Issues identified during implementation - **addressed**:

1. **JsonTree location**: ✅ Moved from `components/DetailsPanel/` to `components/common/` since it's now shared by ThingPanelContent.

2. **EmbedApp args not passed through**: ✅ EmbedApp now uses LowLevelNodeView directly (instead of ThingPanelContent) so it can pass args to the renderer. ThingPanelContent is for panel chrome; EmbedApp shows its own header since it shows a single thing.

3. **Unused import cleanup**: ✅ No unused imports found in ThingWatcherBindings.tsx (useTriggerThing is only used in LowLevelNodeView where it's needed).

4. **BINDINGS_STYLES → CSS modules**: ✅ Converted NodeRef/ChildNodeView styles from runtime string injection to `lib/ThingWatcherBindings.module.css`. This fixes a bug where NodeRef was unstyled in the main app (ThingsList, RootsPanel) because BINDINGS_STYLES was only injected in EmbedApp. CSS modules are bundled into main.css/embed.css automatically.

Issues noted for future consideration:

- **Double data fetching in ThingPanelContent + LowLevelNodeView**: Both fetch the same thing independently. Works due to graphcache, but conceptually redundant. Could consider passing data down instead of fetching twice, but would complicate the API.

- **RendererContext**: Still has `currentTick` and `onSelectThing`. Could potentially be simplified further. → See `phase2_step10p2post1_RendererContext_cleanup.md` for full refactor plan.
