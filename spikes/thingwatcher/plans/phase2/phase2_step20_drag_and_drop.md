# Phase 2 Step 20: Drag and Drop Ability

## Overview

Add a `useThingDropRegion` hook for custom renderers that enables them to create drop zones that can receive dragged things. The hook provides:
1. A `ThingDropRegion` component that suppresses dockview's drop handling within its bounds
2. Mime type helpers for filtering which things to accept
3. A function to extract dropped thing metadata from a DataTransfer

The key design principle: `ThingDropRegion` only suppresses dockview - it doesn't handle drops itself. The renderer uses standard HTML5 drag/drop events for actual handling, and the helpers make it easy to work with thingwatcher's drag data.

## Current State

- `DraggableIDMarker` makes things draggable with `application/x-thingwatcher-thing` mime type
- `NodeRef` also uses the same drag mechanism
- `DockviewLayout` handles drops via `onUnhandledDragOverEvent` and `onDidDrop`
- Drag data is just the thing key (base64 string)

## Design

### Mime Types

Expand the drag data to include multiple mime types for filtering:

```
application/x-thingwatcher-thing          # Always set, data = JSON metadata
application/x-thingwatcher-thing-type/{thingType}   # For type filtering (no data)
application/x-thingwatcher-thing-key/{base64key}    # For specific-key filtering (no data)
```

The main mime type's data will be JSON containing full metadata:
```json
{
  "key": "base64-encoded-key",
  "keyJSON": { ... },  // or null if not JSON-serializable
  "shortId": 42,
  "thingType": "MyType"
}
```

This allows receivers to:
- Check `dataTransfer.types.includes(mimeTypeForThingType("Foo"))` during dragover
- Extract full metadata via `extractDroppedThing(dataTransfer)` on drop

### ThingDropRegion Suppression

Use an app-level context to track whether a ThingDropRegion is currently being dragged over:

1. `ThingDropRegionContext` provides `{ isDropRegionActive, setDropRegionActive }`
2. `ThingDropRegion` calls `setDropRegionActive(true)` on dragenter, `false` on dragleave/drop
3. `DockviewLayout`'s `onUnhandledDragOverEvent` checks `isDropRegionActive` and declines if true

Need to handle the dragenter/dragleave counting problem (nested elements trigger multiple events). Use a counter or check `e.relatedTarget`.

### Hook API

```typescript
interface ThingDropInfo {
  key: string;           // base64 key
  keyJSON: unknown;      // JSON key or null
  shortId: number;
  thingType: string;
}

interface UseThingDropRegionResult {
  /** Component that suppresses dockview drop handling within its bounds */
  ThingDropRegion: React.FC<{ children: React.ReactNode }>;

  /** Mime type that matches any thing */
  mimeTypeForAnyThing: string;

  /** Returns mime type for things of a specific type */
  mimeTypeForThingType: (thingType: string) => string;

  /** Returns mime type for a specific thing key */
  mimeTypeForSpecificThing: (thingKey: string) => string;

  /** Extract dropped thing metadata from DataTransfer, or null if not a thing */
  extractDroppedThing: (dataTransfer: DataTransfer) => ThingDropInfo | null;
}

function useThingDropRegion(handle: FullRendererHandle): UseThingDropRegionResult;
```

### Demo Structure

`examples/demo_drag_drop/` with:

**Python side:**
- `DraggableThing`: Simple things of different types (e.g., `Apple`, `Orange`, `Banana`)
- `DropRegion`: A thing that:
  - Has `await watcher.receive(...)` waiting for a trigger
  - Has a filter mode: "any", "type:Apple", or "key:{specific_key}"
  - When triggered with a thing key payload, creates a link to that thing
  - Shows all accumulated links

**Frontend renderer:**
- Shows a bordered box as the drop zone
- Uses `ThingDropRegion` to suppress dockview
- Checks mime types during dragover to show accept/reject visual feedback
- On valid drop, saves to persistent state and shows a `NodeRef`
- Has a "Confirm" trigger button that sends the key back to Python
- Shows all links that have been confirmed

## Implementation Steps

### 1. Update drag constants and metadata
- [x] Add new mime type constants to `dragConstants.ts`:
  - `DRAG_MIME_THING` (existing)
  - `DRAG_MIME_THING_TYPE_PREFIX = 'application/x-thingwatcher-thing-type/'`
  - `DRAG_MIME_THING_KEY_PREFIX = 'application/x-thingwatcher-thing-key/'`
- [x] Add `ThingDropInfo` interface to `dragConstants.ts`
- [x] Add helper functions:
  - `buildThingDragTypes(key, keyJSON, shortId, thingType)` - returns array of mime types to set
  - `buildThingDragData(key, keyJSON, shortId, thingType)` - returns JSON string for main mime type
  - `parseThingDragData(dataTransfer)` - extracts `ThingDropInfo` or null

### 2. Update DraggableIDMarker to set full drag data
- [x] `DraggableIDMarker` needs access to thing metadata (thingType, shortId, keyJSON)
- [x] Update `createDraggableIDMarker` signature to accept metadata
- [x] Update `handleFactory.ts` to pass metadata to the marker creator
- [x] Set all mime types in `handleDragStart`:
  - `DRAG_MIME_THING` with JSON metadata
  - `DRAG_MIME_THING_TYPE_PREFIX + thingType`
  - `DRAG_MIME_THING_KEY_PREFIX + key`
  - Keep `text/plain` with just the key for fallback

### 3. Update NodeRef drag handling
- [x] `NodeRef` in `ThingRenderLib.tsx` also sets drag data
- [x] Update to use the same multi-mime-type approach
- [x] Will need to fetch/pass metadata (already has thingKey, needs type/shortId/keyJSON)
- [x] May need to query or receive this info from context/props

### 3b. Update ThingNode (graph) alt+drag handling
- [x] `ThingNode.tsx` has alt+drag support that creates panels
- [x] Update `handleDragStart` to use the same multi-mime-type approach
- [x] Already has `thingKey`, `shortId`, `thingType` from node data
- [x] Need to add `keyJSON` to `ThingNodeData` (in `useColaLayout.ts`) if not already present
- [x] This enables alt+drag from graph nodes to custom drop regions

### 4. Create ThingDropRegionContext
- [x] Create `lib/ThingDropRegionContext.tsx`
- [x] Context provides `{ isDropRegionActive: boolean, setDropRegionActive: (active: boolean) => void }`
- [x] Provider component that tracks the active state
- [x] Export hook `useThingDropRegionContext()`

### 5. Integrate context into app
- [x] Add `ThingDropRegionProvider` to `DockviewLayout.tsx` (wrap the dockview area)
- [x] Update `onUnhandledDragOverEvent` to check `isDropRegionActive` and skip `e.accept()` if true
- [x] Add `ThingDropRegionProvider` to `EmbedApp.tsx` as well (suppression is no-op but hook works consistently)

### 6. Create useThingDropRegion hook
- [x] Create `lib/thingDropRegion.tsx` (contains JSX for ThingDropRegion component)
- [x] Implement `useThingDropRegion(handle: FullRendererHandle)`:
  - Returns `ThingDropRegion` component that:
    - Wraps children in a div
    - On dragenter: increment counter, if counter becomes 1, call `setDropRegionActive(true)`
    - On dragleave: decrement counter, if counter becomes 0, call `setDropRegionActive(false)`
    - On drop: reset counter, call `setDropRegionActive(false)`
  - Returns mime type helpers (pure functions, no state needed)
  - Returns `extractDroppedThing` (delegates to `parseThingDragData`)

### 7. Export from thingwatcher-bindings
- [x] Add `useThingDropRegion` to `ThingWatcherBindingsExternal.tsx` exports
- [x] Add types to `thingwatcher-bindings.d.ts`:
  - `ThingDropInfo` interface
  - `UseThingDropRegionResult` interface
  - `useThingDropRegion` declaration

### 8. Create demo
- [x] Create `examples/demo_drag_drop/` directory
- [x] Create `demo.py`:
  - Register snapshotters for `Apple`, `Orange`, `Banana` (simple things with a name)
  - Register anchor capturer for `DropRegion`
  - Create a few fruit things
  - Create three drop regions with different filters
  - Each drop region runs `await watcher.receive(region)` in a loop, creating links when triggered
- [x] Create `Fruit.tsx` and `DropRegion.tsx`:
  - `FruitFull/Ref/Preview` - simple colored display based on fruit type
  - `DropRegionFull` - the main drop zone renderer:
    - Uses `useThingDropRegion` to get helpers
    - Wraps drop area in `ThingDropRegion`
    - Tracks drag-over state for visual feedback (border color change)
    - Checks appropriate mime type based on filter mode during dragover
    - On drop, saves to `usePersistentThingState`
    - Shows `NodeRef` for pending drop
    - "Confirm" button triggers with the key payload
    - Shows list of confirmed links
- [x] Create `deno.json` for typechecking
- [ ] Test the demo end-to-end

### 9. Update documentation
- [x] Update `CODE_OVERVIEW.md` with new hook and demo
- [x] Update `LOCAL_CODE_OVERVIEW.md` with new files and ThingDropRegion explanation
- [x] Update `thingwatcher-bindings.d.ts` JSDoc comments

## File Changes Summary

**New files:**
- `thingwatcher/frontend/lib/ThingDropRegionContext.tsx`
- `thingwatcher/frontend/lib/thingDropRegion.tsx`
- `examples/demo_drag_drop/demo.py`
- `examples/demo_drag_drop/Fruit.tsx`
- `examples/demo_drag_drop/DropRegion.tsx`
- `examples/demo_drag_drop/deno.json`

**Modified files:**
- `thingwatcher/frontend/lib/dragConstants.ts` - add mime type constants and helpers
- `thingwatcher/frontend/lib/DraggableIDMarker.tsx` - set full drag data
- `thingwatcher/frontend/lib/ThingRenderLib.tsx` - update NodeRef drag handling
- `thingwatcher/frontend/lib/handleFactory.ts` - pass metadata to marker creator
- `thingwatcher/frontend/lib/thingDropRegion.tsx` - useThingDropRegion hook
- `thingwatcher/frontend/lib/ThingWatcherBindingsExternal.tsx` - export new hook
- `thingwatcher/frontend/components/DockviewLayout/DockviewLayout.tsx` - add provider, check context
- `thingwatcher/frontend/components/Graph/ThingNode.tsx` - use multi-mime-type drag
- `thingwatcher/frontend/components/Graph/useColaLayout.ts` - add keyJSON to ThingNodeData
- `thingwatcher/frontend/EmbedApp.tsx` - add ThingDropRegionProvider
- `thingwatcher/thingwatcher-bindings.d.ts` - add types
- `CODE_OVERVIEW.md` - document new feature
- `thingwatcher/frontend/LOCAL_CODE_OVERVIEW.md` - document new files

## Design Decisions (Resolved)

1. **Nested ThingDropRegions**: The counter-based approach handles this naturally - we just need "any region active" semantics, and inner regions firing dragenter after outer regions works correctly.

2. **EmbedApp support**: Provide `ThingDropRegionContext` in EmbedApp too for consistency. The suppression is a no-op (no dockview) but the hook works the same way.

3. **Graph node alt+drag**: Alt+drag from graph nodes uses the same multi-mime-type approach and is suppressed by ThingDropRegion. This allows dragging things from the graph directly into custom drop regions. Normal (non-alt) drag just moves React Flow nodes and doesn't trigger HTML drag events.

## Implementation Issues Encountered

### 1. Re-render loop when incrementing context

**Problem**: When `ThingDropRegion` called `incrementDropRegion()`, the context's counter state changed, which caused the context value to change, which caused `useMemo` (that had `dropRegionContext` as a dependency) to recreate the `ThingDropRegion` component. React then unmounted the old component and mounted a new one, and since the drag was still over the region, the browser fired `dragenter` on the new DOM element → infinite loop.

**Solution**: Extract the stable callbacks (`incrementDropRegion`, `decrementDropRegion`) from the context and use those as dependencies instead of the whole context object. The callbacks are created with `useCallback` and have no dependencies, so they're stable across renders.

### 2. Counter needed for nested element drag events

**Problem**: Initially tried to simplify to a boolean (`isInside`), but this doesn't work because HTML5 drag events fire for nested elements. When you drag over a child element inside the ThingDropRegion:
- `dragleave` fires on the parent (you "left" the parent's direct surface)
- `dragenter` fires on the child (which bubbles to parent)

With a boolean, we'd see `dragleave` and set `isInside=false`, incorrectly thinking we left.

**Solution**: Use a counter at the `ThingDropRegion` level. Increment on every `dragenter`, decrement on every `dragleave`. Only call context's `incrementDropRegion()` when counter goes 0→1, and `decrementDropRegion()` when counter goes 1→0.

### 3. Unmount during drag

**Problem**: If a `ThingDropRegion` unmounts while the drag is inside it (e.g., panel closes), we need to decrement the global context to avoid leaving it in an "active" state.

**Solution**: Added a `useEffect` cleanup that checks if the counter is > 0 on unmount and calls `decrementDropRegion()`.

### 4. Browser lowercases MIME types

**Problem**: Browsers automatically lowercase MIME type strings when you call `setData()`. This broke case-sensitive matching - we'd set `application/x-thingwatcher-thing-type/Apple` but the browser stored it as `application/x-thingwatcher-thing-type/apple`.

**Solution**: Hex-encode the thingType and key portions of the MIME type. For example, "Apple" becomes "4170706c65" (hex codes for each character). Since hex uses only `0-9a-f`, lowercasing has no effect, and we preserve case sensitivity.

Added `hexEncode()` and `hexDecode()` helper functions to `dragConstants.ts`.

### 5. ThingDropRegion needs styling

**Problem**: The `ThingDropRegion` wrapper div needed to be styleable for layout purposes.

**Solution**: Added an optional `style` prop to the `ThingDropRegion` component.
