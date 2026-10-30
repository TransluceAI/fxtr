# Phase 2 Step 15: Custom Renderer Handle System

## Overview

Refactor the custom renderer API to use an opaque handle + hooks pattern instead of passing data directly as props. This provides better encapsulation, enables backwards-compatible future extensions, and creates a cleaner separation between thing data and renderer configuration.

**This is a clean break** - the old props-based API will stop working immediately. All renderers must be updated.

## Current API

Renderers receive data directly as props:

```tsx
export function FullRenderer({ payload, dataChunks, links, triggerable, trigger, args, isTopLevel, DraggableIDMarker }: FullRendererProps) {
  const data = payload as MyType;
  return (
    <DraggableIDMarker>
      <span>{data.name}</span>
    </DraggableIDMarker>
  );
}
```

## New API

Renderers receive an opaque handle and use hooks to access data:

```tsx
export const thingwatcherDialect = "0.1";

export function FullRenderer({ handle, args, isTopLevel }: FullRendererProps) {
  const data = usePayload<MyType>(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  return (
    <DraggableIDMarker>
      <span>{data.name}</span>
    </DraggableIDMarker>
  );
}
```

## Handle Types

Three distinct opaque handle types using unique symbol branding:

```typescript
declare const fullRendererBrand: unique symbol;
declare const refRendererBrand: unique symbol;
declare const previewRendererBrand: unique symbol;

export interface FullRendererHandle {
  readonly [fullRendererBrand]: true;
}

export interface RefRendererHandle {
  readonly [refRendererBrand]: true;
}

export interface PreviewRendererHandle {
  readonly [previewRendererBrand]: true;
}
```

Each handle type supports different hook subsets:

| Handle Type | Supported Hooks |
|-------------|-----------------|
| `FullRendererHandle` | All hooks (payload, dataChunks, links, metadata, triggerable, trigger, draggable, persistentState) |
| `RefRendererHandle` | usePayload, useDataChunks, useLinks, useThingMetadata, useTriggerable |
| `PreviewRendererHandle` | usePayload, useDataChunks, useLinks, useThingMetadata, useDraggableIdMarker |

TypeScript enforces correct usage at development time - calling `useTrigger` with a `RefRendererHandle` is a compile error. **At runtime, hooks validate their input and throw if given an invalid handle type.**

## New Hooks

All hooks take the handle as their first argument:

```typescript
// Data access - all handles
function usePayload<T = unknown>(
  handle: FullRendererHandle | RefRendererHandle | PreviewRendererHandle,
  options?: { chunk?: 'first' | 'latest' }  // defaults to 'first'
): T;

function useDataChunks<T = unknown>(
  handle: FullRendererHandle | RefRendererHandle | PreviewRendererHandle
): T[];

function useLinks(
  handle: FullRendererHandle | RefRendererHandle | PreviewRendererHandle
): LinkInfo[];

// Metadata - all handles
function useThingMetadata<K = unknown>(
  handle: FullRendererHandle | RefRendererHandle | PreviewRendererHandle
): { key: string; keyJSON: K; shortId: number; thingType: string };

// Triggerable status - Full and Ref handles
// Returns [triggerable, triggerRecordAtCurrentTick]
function useTriggerable(
  handle: FullRendererHandle | RefRendererHandle
): [
  triggerable: boolean,
  triggerRecord: TriggerRecord | null
];

// Trigger function - FullRendererHandle only
function useTrigger<T = unknown>(
  handle: FullRendererHandle
): (payload: T) => Promise<TriggerResult>;

// Draggable ID - Full and Preview only
function useDraggableIdMarker(
  handle: FullRendererHandle | PreviewRendererHandle
): FC<{ children: ReactNode }>;
```

The existing `usePersistentThingState` hook now takes the handle as first argument (FullRenderer only):

```typescript
function usePersistentThingState<T>(
  handle: FullRendererHandle,
  key: string
): [T | undefined, (value: T | undefined) => void];
```

## New Props Interfaces

```typescript
interface FullRendererProps {
  handle: FullRendererHandle;
  args: Record<string, unknown>;
  isTopLevel: boolean;
}

interface RefRendererProps {
  handle: RefRendererHandle;
  args: Record<string, unknown>;
}

interface PreviewRendererProps {
  handle: PreviewRendererHandle;
  // No args - previews are read-only
}
```

## Dialect Version Export

All renderers must export a `thingwatcherDialect` constant:

```typescript
export const thingwatcherDialect = "0.1";
```

This is validated immediately after esbuild compilation when extracting the module exports. Missing or incorrect dialect throws an error with a clear message.

## Internal Implementation

### Handle Structure

Handles contain hook method functions - ALL data access goes through hooks, even "static" data like payload. This allows for lazy loading and keeps the pattern consistent.

Each handle has exactly ONE brand symbol set to `true`, identifying its type. Runtime validation checks for the appropriate brand(s).

```typescript
// Internal only - not exported to renderers
interface InternalHandle {
  // Brand - exactly one is true per handle instance
  [fullRendererBrand]?: true;
  [refRendererBrand]?: true;
  [previewRendererBrand]?: true;

  // Metadata (used by multiple hooks)
  _metadata: { key: string; keyJSON: unknown; shortId: number; thingType: string };

  // Hook methods - ALL data access goes through hooks (present on all handles)
  _usePayload: (opts?: { chunk?: 'first' | 'latest' }) => unknown;
  _useDataChunks: () => unknown[];
  _useLinks: () => LinkInfo[];

  // Optional hook methods - only present on certain handle types
  _useTriggerable?: () => [boolean, TriggerRecord | null];  // Full + Ref
  _useTrigger?: () => (payload: unknown) => Promise<TriggerResult>;  // Full only
  _useDraggableIdMarker?: () => FC<{ children: ReactNode }>;  // Full + Preview
  _usePersistentThingState?: <T>(key: string) => [T | undefined, (v: T | undefined) => void];  // Full only
}
```

### Hook Delegation Pattern

The exported hooks are thin wrappers that validate the brand and delegate:

```typescript
export function useTriggerable(handle: FullRendererHandle | RefRendererHandle) {
  const h = handle as unknown as InternalHandle;
  if (!h[fullRendererBrand] && !h[refRendererBrand]) {
    throw new Error('useTriggerable requires FullRendererHandle or RefRendererHandle');
  }
  return h._useTriggerable!();  // This is the actual React hook call
}

export function useTrigger<T>(handle: FullRendererHandle) {
  const h = handle as unknown as InternalHandle;
  if (!h[fullRendererBrand]) {
    throw new Error('useTrigger requires a FullRendererHandle');
  }
  return h._useTrigger!() as (payload: T) => Promise<TriggerResult>;
}

export function useDraggableIdMarker(handle: FullRendererHandle | PreviewRendererHandle) {
  const h = handle as unknown as InternalHandle;
  if (!h[fullRendererBrand] && !h[previewRendererBrand]) {
    throw new Error('useDraggableIdMarker requires FullRendererHandle or PreviewRendererHandle');
  }
  return h._useDraggableIdMarker!();
}

export function usePayload<T>(handle: AnyHandle, opts?: { chunk?: 'first' | 'latest' }): T {
  // All handles support usePayload, no brand check needed (or check all three)
  const h = handle as unknown as InternalHandle;
  return h._usePayload(opts) as T;
}
```

### Why Internal Methods Are Hooks

The key insight is that ALL internal methods (`_usePayload`, `_useTriggerable`, etc.) are React hooks. When the renderer calls `usePayload(handle)`, it ultimately calls `handle._usePayload()`, which can:
- Return cached data (for simple cases)
- Subscribe to context for live updates
- Fetch data lazily if needed

For `_useTriggerable`, the hook subscribes to `ThingWatcherProvider` context:

1. Handle is created with hook method closures that capture the thing key
2. When renderer calls `useTriggerable(handle)`, the internal hook subscribes to context
3. If triggerable state changes (new trigger, live mode toggle), the renderer re-renders
4. The hook also fetches the trigger record at the current tick (if any)

This keeps the subscription logic pushed down into the hook, not hoisted to handle creation time.

### ThingContext Replacement

The current `ThingContext` (which provides shortId for `usePersistentThingState`) will be removed. Instead, `usePersistentThingState` gets shortId from the handle's metadata.

### Handle Creation Locations

Handles are created close to where they're used, via helper functions:

| Renderer Type | Creation Location |
|---------------|-------------------|
| FullRenderer | `LowLevelNodeView.tsx` |
| RefRenderer | `NodeRef` in `ThingRenderLib.tsx` |
| InlineRefRenderer | `InlineNodeRef` in `ThingRenderLib.tsx` |
| PreviewRenderer | `HoverPreview.tsx` (where PreviewRenderer is rendered) |

Helper factory functions in a new `lib/handleFactory.ts`:

```typescript
export function createFullRendererHandle(
  thingKey: string,
  metadata: ThingMetadata,
): FullRendererHandle {
  const handle: InternalHandle = {
    [fullRendererBrand]: true,
    _metadata: metadata,

    _usePayload: (opts) => {
      // Hook that fetches/returns payload data
      const thing = useThing({ keyInput: { key: thingKey }, includeData: true });
      if (opts?.chunk === 'latest') {
        const chunks = thing?.dataChunks ?? [];
        return chunks[chunks.length - 1]?.data;
      }
      return thing?.data;
    },

    _useDataChunks: () => {
      const thing = useThing({ keyInput: { key: thingKey }, includeData: true });
      return (thing?.dataChunks ?? []).map(c => c.data);
    },

    _useLinks: () => {
      const thing = useThing({ keyInput: { key: thingKey }, includeData: true });
      return thing?.links ?? [];
    },

    _useTriggerable: () => {
      // Subscribes to context for live triggerable state
      const { triggerableKeys, isLive, resolvedTick } = useThingWatcher();
      const triggerable = isLive && triggerableKeys.has(thingKey);

      // Get trigger record at current tick (if any)
      const { records: triggerRecords } = useTriggerRecordsAtTick(resolvedTick);
      const triggerRecord = triggerRecords.find(r => r.thing?.key === thingKey) ?? null;

      return [triggerable, triggerRecord];
    },

    _useTrigger: () => {
      const [, triggerMutation] = useTriggerThing({ key: thingKey });
      return useCallback((payload: unknown) => triggerMutation(payload), [triggerMutation]);
    },

    _useDraggableIdMarker: () => {
      return useMemo(() => {
        return ({ children }: { children: ReactNode }) => (
          <DraggableIDMarkerInternal thingKey={thingKey} thingType={metadata.thingType}>
            {children}
          </DraggableIDMarkerInternal>
        );
      }, []);
    },

    _usePersistentThingState: (key) => {
      return usePersistentThingStateInternal(metadata.shortId, key);
    },
  };
  return handle as unknown as FullRendererHandle;
}

// createRefRendererHandle - same but without _useTrigger, _useDraggableIdMarker, _usePersistentThingState
// createPreviewRendererHandle - same but without _useTriggerable, _useTrigger, _usePersistentThingState
```

## Implementation Steps

### 1. Update Type Definitions
- [x] Add handle types with unique symbol branding to `thingwatcher/thingwatcher-bindings.ts`
- [x] Add hook type declarations (usePayload, useDataChunks, useLinks, useThingMetadata, useTriggerable, useTrigger, useDraggableIdMarker)
- [x] Add `TriggerRecord` type (for useTriggerable return value)
- [x] Update props interfaces (FullRendererProps, RefRendererProps, PreviewRendererProps) to use handles
- [x] Update usePersistentThingState signature to take handle as first arg
- [x] Remove old prop types (payload, dataChunks, etc. from props)

### 2. Create Handle Factory
- [x] Create new file `lib/handleFactory.ts`
- [x] Define `InternalHandle` interface (internal only) with brand symbols
- [x] Implement `createFullRendererHandle(thingKey, metadata)` - includes all hook methods
- [x] Implement `createRefRendererHandle(thingKey, metadata)` - no `_useTrigger`, `_useDraggableIdMarker`, `_usePersistentThingState`
- [x] Implement `createPreviewRendererHandle(thingKey, metadata)` - no `_useTriggerable`, `_useTrigger`, `_usePersistentThingState`

### 3. Implement Hooks
- [x] Create new file `lib/rendererHooks.ts` for hook implementations
- [x] Implement `usePayload` with first/latest option (delegates to handle's `_usePayload`, brand check optional)
- [x] Implement `useDataChunks` (delegates to handle's `_useDataChunks`)
- [x] Implement `useLinks` (delegates to handle's `_useLinks`)
- [x] Implement `useThingMetadata` (reads from handle's `_metadata`)
- [x] Implement `useTriggerable` (validates `fullRendererBrand` or `refRendererBrand`, delegates, returns 2-tuple)
- [x] Implement `useTrigger` (validates `fullRendererBrand`, delegates, returns trigger function)
- [x] Implement `useDraggableIdMarker` (validates `fullRendererBrand` or `previewRendererBrand`, delegates)
- [x] Update `usePersistentThingState` to take handle, validate `fullRendererBrand`, delegate to handle's `_usePersistentThingState`

### 4. Update ThingWatcherBindingsExternal
- [x] Export all new hooks: usePayload, useDataChunks, useLinks, useThingMetadata, useTriggerable, useTrigger, useDraggableIdMarker, usePersistentThingState
- [x] Export handle types (for type annotations)
- [x] Remove old prop-based exports

### 5. Remove ThingContext
- [x] Remove `lib/ThingContext.tsx` (shortId now comes from handle)
- [x] Remove ThingContext.Provider usage from LowLevelNodeView, etc.

### 6. Update Handle Creation Sites
- [x] `LowLevelNodeView.tsx` - create FullRendererHandle, pass to renderer
- [x] `ThingRenderLib.tsx` NodeRef - create RefRendererHandle for RefRenderer
- [x] `ThingRenderLib.tsx` InlineNodeRef - create RefRendererHandle for InlineRefRenderer
- [x] `HoverPreview.tsx` - create PreviewRendererHandle for PreviewRenderer

### 7. Add Dialect Validation
- [x] In `rendererCache.ts`, after esbuild compile and before caching:
  - Check for `thingwatcherDialect` export on compiled module
  - Validate it equals "0.1"
  - Throw descriptive error if missing: "Renderer must export `thingwatcherDialect = \"0.1\"`"
  - Throw descriptive error if wrong version

### 8. Update Fallback Renderers
Fallback renderers receive handles and use hooks just like custom renderers, so the parent components don't need to precompute all data - they just create a handle and pass it to either the dynamic or fallback component.

- [x] Update `FallbackFullRenderer` to receive handle, use hooks pattern
- [x] Update `FallbackRefRenderer` to receive handle, use hooks pattern
- [x] Update `FallbackInlineRefRenderer` to receive handle, use hooks pattern
- [x] Update `FallbackPreviewRenderer` to receive handle, use hooks pattern

### 9. Update demo_dynamic_rendering Examples
- [x] Container.tsx - add `thingwatcherDialect = "0.1"`, migrate to hooks
- [x] Item.tsx - add dialect, migrate to hooks
- [x] StyledThing.tsx - add dialect, migrate to hooks
- [x] StyledThingLib.tsx - check if has renderers, update if so
- [x] Keep BrokenCompile.tsx as-is (tests compile errors)
- [x] Keep BrokenRuntime.tsx as-is (tests runtime errors)
- [x] Add new `BrokenDialect.tsx` - renderer missing dialect export (tests dialect validation)

### 10. Update Documentation
- [x] Update CODE_OVERVIEW.md - document new handle/hooks API
- [ ] Update frontend/LOCAL_CODE_OVERVIEW.md - document hooks, handle factory
- [ ] Update example snippets throughout docs

### 11. Update Clydra Renderers
- [x] renderers/ClydraRoot.tsx - add dialect, migrate to hooks
- [x] renderers/InterruptButton.tsx - add dialect, migrate to hooks
- [x] renderers/PermissionDialog.tsx - add dialect, migrate to hooks
- [x] renderers/PermissionMode.tsx - add dialect, migrate to hooks

### 12. Testing
- [ ] Manual test: demo_dynamic_rendering works end-to-end
- [ ] Manual test: dialect validation catches missing export (BrokenDialect.tsx)
- [ ] Manual test: clydra renderers work
- [ ] Manual test: usePersistentThingState still works with new signature
- [ ] Manual test: hover previews still work
- [ ] Manual test: drag-to-panel still works

### 13. Update CODE_OVERVIEW.md
- [x] Final pass on CODE_OVERVIEW.md after all implementation complete

## Migration Example

### Before (Container.tsx)

```tsx
import { ChildNodeView, NodeRef, InlineNodeRef } from 'thingwatcher-bindings';
import type { FullRendererProps, RefRendererProps, PreviewRendererProps } from 'thingwatcher-bindings';

export function FullRenderer({ payload, links, DraggableIDMarker }: FullRendererProps) {
  const itemLinks = links.filter(l => l.linkType === 'item');
  const data = payload as { name?: string; item_count?: number } | null;

  return (
    <div style={{ padding: '12px' }}>
      <DraggableIDMarker>
        <span style={{ cursor: 'grab' }}>Container: {data?.name}</span>
      </DraggableIDMarker>
      {itemLinks.map((link, i) => (
        <NodeRef key={i} thingKey={link.toThingKey} />
      ))}
    </div>
  );
}

export function RefRenderer({ payload }: RefRendererProps) {
  const data = payload as { name?: string } | null;
  return <div>Container: {data?.name}</div>;
}
```

### After

```tsx
import {
  ChildNodeView, NodeRef, InlineNodeRef,
  usePayload, useLinks, useDraggableIdMarker
} from 'thingwatcher-bindings';
import type { FullRendererProps, RefRendererProps } from 'thingwatcher-bindings';

export const thingwatcherDialect = "0.1";

export function FullRenderer({ handle }: FullRendererProps) {
  const payload = usePayload(handle);
  const links = useLinks(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  const itemLinks = links.filter(l => l.linkType === 'item');
  const data = payload as { name?: string; item_count?: number } | null;

  return (
    <div style={{ padding: '12px' }}>
      <DraggableIDMarker>
        <span style={{ cursor: 'grab' }}>Container: {data?.name}</span>
      </DraggableIDMarker>
      {itemLinks.map((link, i) => (
        <NodeRef key={i} thingKey={link.toThingKey} />
      ))}
    </div>
  );
}

export function RefRenderer({ handle }: RefRendererProps) {
  const payload = usePayload(handle);
  const data = payload as { name?: string } | null;
  return <div>Container: {data?.name}</div>;
}
```

### Triggerable Example (Before)

```tsx
export function FullRenderer({ triggerable, trigger, DraggableIDMarker }: FullRendererProps) {
  return (
    <div>
      <DraggableIDMarker><span>My Thing</span></DraggableIDMarker>
      {triggerable && <button onClick={() => trigger({ action: 'go' })}>Go</button>}
    </div>
  );
}
```

### Triggerable Example (After)

```tsx
export const thingwatcherDialect = "0.1";

export function FullRenderer({ handle }: FullRendererProps) {
  const [triggerable, triggerRecord] = useTriggerable(handle);
  const doTrigger = useTrigger(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  return (
    <div>
      <DraggableIDMarker><span>My Thing</span></DraggableIDMarker>
      {triggerRecord && <div>Triggered with: {JSON.stringify(triggerRecord.payload)}</div>}
      {triggerable && <button onClick={() => doTrigger({ action: 'go' })}>Go</button>}
    </div>
  );
}
```

### Persistent State Example (Before)

```tsx
import { usePersistentThingState } from 'thingwatcher-bindings';

export function FullRenderer({ payload, DraggableIDMarker }: FullRendererProps) {
  const [expanded, setExpanded] = usePersistentThingState<boolean>('expanded');
  // ...
}
```

### Persistent State Example (After)

```tsx
import { usePersistentThingState, usePayload, useDraggableIdMarker } from 'thingwatcher-bindings';

export const thingwatcherDialect = "0.1";

export function FullRenderer({ handle }: FullRendererProps) {
  const payload = usePayload(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);
  const [expanded, setExpanded] = usePersistentThingState<boolean>(handle, 'expanded');
  // ...
}
```

## Notes

- **Clean break**: Old props-based API stops working. All renderers must migrate.
- `args` and `isTopLevel` remain as direct props since they're renderer configuration, not thing data
- Handle types use unique symbol branding - from the renderer's perspective they have no accessible properties
- All existing `ChildNodeView`, `NodeRef`, `InlineNodeRef` components continue to work unchanged (they don't take handles)
- Runtime validation checks brand symbols and throws clear errors if hooks are called with wrong handle type
- Dialect validation happens at module load time, before any rendering
- ALL data access goes through hooks (even payload/dataChunks/links) - no static data on handles except metadata
- `useTriggerable` returns 2-tuple: `[triggerable, triggerRecord]` where `triggerRecord` is the trigger at current tick (if any)
- `useTrigger` returns the trigger function (FullRenderer only)
- RefRenderers can check triggerable status via `useTriggerable` but cannot trigger (no `useTrigger`)
- Need to export `TriggerRecord` type from thingwatcher-bindings for useTriggerable return type
- Fallback renderers use the same handle/hooks pattern as custom renderers for consistency

## Files Changed Summary

| File | Change |
|------|--------|
| `thingwatcher/thingwatcher-bindings.ts` | New types, hook declarations |
| `frontend/lib/handleFactory.ts` | **New** - handle creation helpers |
| `frontend/lib/rendererHooks.ts` | **New** - hook implementations |
| `frontend/lib/ThingRenderLib.tsx` | Update to create handles in NodeRef/InlineNodeRef |
| `frontend/lib/ThingWatcherBindingsExternal.tsx` | Export new hooks |
| `frontend/lib/ThingContext.tsx` | **Delete** |
| `frontend/lib/LowLevelNodeView.tsx` | Create FullRendererHandle |
| `frontend/lib/HoverPreview.tsx` | Create PreviewRendererHandle |
| `frontend/components/DynamicRenderer/rendererCache.ts` | Dialect validation |
| `frontend/components/DynamicRenderer/FallbackRenderers.tsx` | Migrate to hooks |
| `examples/demo_dynamic_rendering/*.tsx` | Migrate to hooks + dialect |
| `examples/demo_dynamic_rendering/BrokenDialect.tsx` | **New** - test case |
| `clydra/renderers/*.tsx` | Migrate to hooks + dialect |
| `CODE_OVERVIEW.md` | Document new API |
| `frontend/LOCAL_CODE_OVERVIEW.md` | Document hooks, factory |
