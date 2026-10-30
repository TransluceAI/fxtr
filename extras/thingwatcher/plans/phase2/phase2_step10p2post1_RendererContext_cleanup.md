# Phase 2 Step 10.2 Post-1: RendererContext Cleanup & ThingUIEventsContext

## Overview

Refactor the internal context system used by ThingWatcherBindings components (NodeRef, ChildNodeView) to:
1. Clean up ThingWatcherBindings exports - only expose public API for renderers
2. Replace RendererContext with a new ThingUIEventsContext that handles UI events
3. Make EmbedApp use URL query params for initial thing selection
4. Update ThingContextMenu to use ThingUIEventsContext

## Current Problems

### 1. ThingWatcherBindings exports too much
Currently exports internal implementation details that renderers shouldn't use:
- `RendererProvider` / `RendererContextValue` - internal context setup
- `ThingProvider` - internal context for persistent state

These should be internal, not part of the public `thingwatcher-bindings` API.

### 2. RendererContext has unused fields
- `currentTick` is never read (can be removed)
- Only `onSelectThing` is used (by NodeRef)

### 3. ThingContextMenu is tightly coupled to dockview
- Directly imports `useDockviewApiFromContext` and `openThingPanel`
- Falls back to "no menu" when not in dockview context
- Can't work in EmbedApp

### 4. EmbedApp has no URL-based navigation
- Only receives thing keys via postMessage
- Can't open a link to a specific thing
- "Open in new panel" can't work

## Target Architecture

### ThingUIEventsContext (new, internal)

A unified context for UI event handlers that thing-related components need:

```typescript
interface ThingUIEventsContextValue {
  /** Called when clicking a thing reference */
  onClickThing: (key: string) => void;
  /** Called for "open in new panel/window" action */
  onOpenThingInNewPanel: (key: string) => void;
  /** Label for the open action: "new panel" (App) or "new window" (EmbedApp) */
  openInNewPanelLabel: 'panel' | 'window';
}
```

**Behavior by context:**

| Action | App (main) | EmbedApp |
|--------|-----------|----------|
| `onClickThing` | Select thing (update selectedThingKey) | Copy key to clipboard + show toast |
| `onOpenThingInNewPanel` | Open thing in new dockview panel | Open new browser window with `?key=<b64>` |
| `openInNewPanelLabel` | `'panel'` | `'window'` |

### File Structure

```
lib/
├── ThingWatcherBindings.tsx      # Public API only (NodeRef, ChildNodeView, etc.)
├── ThingWatcherBindings.module.css
├── ThingUIEventsContext.tsx      # NEW: Internal context for UI events
├── ThingContext.tsx              # NEW: Internal context for persistent state (moved from ThingWatcherBindings)
├── LowLevelNodeView.tsx          # Internal
├── DraggableIDMarker.tsx         # Internal
└── persistentStateStore.ts       # Internal
```

### Public API (thingwatcher-bindings)

Only these should be accessible to custom renderers:

**Components:**
- `NodeRef` - clickable thing reference
- `ChildNodeView` - inline thing view

**Hooks:**
- `usePersistentThingState` - persistent state for renderers

**Types:**
- `FullRendererProps`, `RefRendererProps` - renderer prop interfaces
- `LinkInfo`, `TriggerResult` - data types
- `NodeRefProps`, `ChildNodeViewProps` - component prop interfaces

**NOT exported (internal):**
- `ThingUIEventsContext`, `ThingUIEventsProvider`, `useThingUIEvents`
- `ThingProvider`, `useThingContext`
- Any context value types

## Implementation Plan

### 1. Create ThingUIEventsContext

**File: `lib/ThingUIEventsContext.tsx`** (new)

- [x] Define `ThingUIEventsContextValue` interface
- [x] Create context with `createContext`
- [x] Create `ThingUIEventsProvider` component
- [x] Create `useThingUIEvents` hook (throws if not in context)
- [x] Export provider and hook (but NOT from ThingWatcherBindings)

```typescript
export interface ThingUIEventsContextValue {
  onClickThing: (key: string) => void;
  onOpenThingInNewPanel: (key: string) => void;
  openInNewPanelLabel: 'panel' | 'window';
}
```

### 2. Create ThingContext (move from ThingWatcherBindings)

**File: `lib/ThingContext.tsx`** (new)

- [x] Move `ThingContextValue`, `ThingContext`, `ThingProvider`, `useThingContext` from ThingWatcherBindings
- [x] Keep as internal (not exported from ThingWatcherBindings)

### 3. Update ThingWatcherBindings

**File: `lib/ThingWatcherBindings.tsx`**

- [x] Remove `RendererContext`, `RendererProvider`, `RendererContextValue`, `useRendererContext`
- [x] Remove `ThingContext`, `ThingProvider`, `useThingContext` (moved to ThingContext.tsx)
- [x] Import `useThingUIEvents` from ThingUIEventsContext
- [x] Import `ThingProvider`, `useThingContext` from ThingContext
- [x] Update `NodeRef` to use `useThingUIEvents().onClickThing` instead of `ctx.onSelectThing`
- [x] Keep only public exports: NodeRef, ChildNodeView, usePersistentThingState, prop/type interfaces

### 4. Update ThingContextMenu

**File: `components/ThingContextMenu/ThingContextMenu.tsx`**

- [x] Remove dockview imports (`useDockviewApiFromContext`, `openThingPanel`)
- [x] Import `useThingUIEvents` from ThingUIEventsContext
- [x] Use `ctx.onOpenThingInNewPanel(thingKey)` for menu action
- [x] Use `ctx.openInNewPanelLabel` for menu item text ("Open in new panel" vs "Open in new window")
- [x] Remove fallback behavior - context is now required

### 5. Update DockviewLayout (App)

**File: `components/DockviewLayout/DockviewLayout.tsx`**

- [x] Remove `RendererProvider` import
- [x] Import `ThingUIEventsProvider` from ThingUIEventsContext
- [x] Create context value with:
  - `onClickThing`: `props.onSelectThing`
  - `onOpenThingInNewPanel`: `(key) => openThingPanel(api, key)`
  - `openInNewPanelLabel`: `'panel'`
- [x] Replace `<RendererProvider>` with `<ThingUIEventsProvider>`

### 6. Update EmbedApp

**File: `EmbedApp.tsx`**

- [x] Remove `RendererProvider` import
- [x] Import `ThingUIEventsProvider` from ThingUIEventsContext

**URL query param support:**
- [x] On mount, read `?key=<b64>` from URL and set as initial thing
- [x] When thing changes, update URL with `history.replaceState`

**Context value:**
- [x] `onClickThing`: Copy key to clipboard + show toast notification
- [x] `onOpenThingInNewPanel`: Open new window with `window.open('/embed.html?key=' + key)`
- [x] `openInNewPanelLabel`: `'window'`

**Toast notification (using @base-ui/react/toast):**
- [x] Wrap content with `<Toast.Provider>`
- [x] Add `<Toast.Viewport>` with styling for toast display
- [x] Use `Toast.useToastManager().add({ title: 'Copied to clipboard', timeout: 2000 })` in onClickThing

### 7. Update embed.tsx entry point

**File: `embed.tsx`**

- [x] No changes needed (EmbedApp handles URL parsing internally)

### 8. Update LowLevelNodeView

**File: `lib/LowLevelNodeView.tsx`**

- [x] Import `ThingProvider` directly from `./ThingContext` instead of via ThingWatcherBindings re-export

### 9. Clean up imports across codebase

- [x] `components/RootsPanel/RootsPanel.tsx` - only imports `NodeRef` (no change needed)
- [x] `components/ThingsList/ThingsList.tsx` - only imports `NodeRef` (no change needed)
- [x] `components/DynamicRenderer/DynamicRenderer.tsx` - only imports types (no change needed)
- [x] `components/DynamicRenderer/FallbackRenderers.tsx` - only imports types (no change needed)

### 10. Update CODE_OVERVIEW

- [x] Update LOCAL_CODE_OVERVIEW.md with new file structure
- [x] Document ThingUIEventsContext

## EmbedApp URL Behavior

### URL Format
```
/embed.html?key=<base64-encoded-key>
```

### Behavior
1. **On mount**: Parse `?key` from URL, set as initial thing (before any postMessage)
2. **On postMessage**: Update thing as before, also update URL
3. **On internal navigation**: Update URL with new key
4. **Copy to clipboard**: When clicking a NodeRef, copy the thing's key

### Example Flow
1. User opens `/embed.html?key=abc123`
2. EmbedApp shows thing with key `abc123`
3. User right-clicks a linked thing, clicks "Open in new window"
4. New browser window opens at `/embed.html?key=def456`

## Testing Considerations

1. **ThingContextMenu** - verify it shows correct label and triggers correct action in both App and EmbedApp
2. **NodeRef click** - verify App selects thing, EmbedApp copies to clipboard
3. **EmbedApp URL** - verify initial load from URL, URL updates on navigation
4. **Toast** - verify toast appears and disappears after copying
5. **Custom renderers** - verify they can still import from `thingwatcher-bindings` and use NodeRef/ChildNodeView

## File Changes Summary

### New Files
- `lib/ThingUIEventsContext.tsx` - UI events context
- `lib/ThingContext.tsx` - Thing context for persistent state (moved)

### Modified Files
- `lib/ThingWatcherBindings.tsx` - remove internal exports, use new contexts
- `lib/LowLevelNodeView.tsx` - update ThingProvider import
- `components/ThingContextMenu/ThingContextMenu.tsx` - use ThingUIEventsContext
- `components/DockviewLayout/DockviewLayout.tsx` - provide ThingUIEventsContext
- `EmbedApp.tsx` - URL params, toast, provide ThingUIEventsContext
- `LOCAL_CODE_OVERVIEW.md` - document new structure
