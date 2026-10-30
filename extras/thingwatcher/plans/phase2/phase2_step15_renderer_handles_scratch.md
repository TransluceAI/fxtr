# Step 15 Scratch Notes - Renderer Handle System

## Current Props Structure

**FullRendererProps:**
- payload: unknown (first chunk data)
- dataChunks: unknown[] (all chunks)
- links: LinkInfo[]
- triggerable: boolean
- trigger: (payload) => Promise<TriggerResult>
- args: Record<string, unknown>
- isTopLevel: boolean
- DraggableIDMarker: FC<{children}>

**RefRendererProps:**
- payload, dataChunks, links (same)
- triggerable (boolean only, no trigger fn)
- args

**PreviewRendererProps:**
- payload, dataChunks, links (same)
- shortId, keyJSON, thingType (metadata)
- DraggableIDMarker

## Design Decisions

1. **Handle types**: Three distinct opaque types from TS perspective
   - `FullRendererHandle` - full capabilities
   - `RefRendererHandle` - lighter, no trigger/draggable
   - `PreviewRendererHandle` - has draggable, no trigger

2. **Hooks accept appropriate handle types via union types**:
   - `usePayload(handle)` - accepts all three
   - `useDataChunks(handle)` - accepts all three
   - `useLinks(handle)` - accepts all three
   - `useThingMetadata(handle)` - accepts all three
   - `useTriggerable(handle)` -> `[boolean, triggerFn]` - FullRendererHandle only
   - `useDraggableIdMarker(handle)` - Full + Preview only

3. **usePayload options**:
   - `{ chunk: 'first' | 'latest' }` - defaults to 'first'

4. **useTriggerable returns tuple**: `[triggerable, doTrigger]`

5. **thingwatcherDialect validation**: At module extraction after esbuild compile

## Internal Implementation

Internally, handles could be the same object with all data. The type system just restricts what hooks accept them.

```typescript
// Internal (not exported)
interface InternalHandle {
  payload: unknown;
  dataChunks: unknown[];
  links: LinkInfo[];
  metadata: { key, keyJSON, shortId, thingType };
  triggerable?: boolean;
  trigger?: (payload) => Promise<TriggerResult>;
  makeDraggableIdMarker?: () => FC<{children}>;
}

// The hooks just read from the handle, casting as needed
function usePayload(handle: AnyHandle, opts?) {
  const h = handle as InternalHandle;
  return opts?.chunk === 'latest'
    ? h.dataChunks[h.dataChunks.length - 1]
    : h.payload;
}
```

## Props After Refactor

**FullRendererProps:**
- handle: FullRendererHandle
- args: Record<string, unknown>
- isTopLevel: boolean

**RefRendererProps:**
- handle: RefRendererHandle
- args: Record<string, unknown>

**PreviewRendererProps:**
- handle: PreviewRendererHandle
- (no args - previews are read-only)

## Files to Modify

### thingwatcher/
- `thingwatcher-bindings.ts` - new types + hook declarations

### frontend/
- `lib/ThingRenderLib.tsx` - internal hook implementations
- `lib/ThingWatcherBindingsExternal.tsx` - export wrapped hooks
- `lib/LowLevelNodeView.tsx` - create handles instead of building props
- `components/DynamicRenderer/DynamicRenderer.tsx` - pass handles, validate dialect
- `components/DynamicRenderer/FallbackRenderers.tsx` - update to use hooks
- `lib/ThingContext.tsx` - may need updates for handle context

### examples/demo_dynamic_rendering/
- Container.tsx
- Item.tsx
- StyledThing.tsx
- StyledThingLib.tsx (if it has renderers)
- BrokenCompile.tsx
- BrokenRuntime.tsx

### clydra/
- renderers/ClydraRoot.tsx
- renderers/InterruptButton.tsx
- renderers/PermissionDialog.tsx
- renderers/PermissionMode.tsx

## Migration Pattern

Old:
```tsx
export function FullRenderer({ payload, links, DraggableIDMarker }: FullRendererProps) {
  const data = payload as MyType;
  return (
    <DraggableIDMarker>
      <span>{data.name}</span>
    </DraggableIDMarker>
  );
}
```

New:
```tsx
export const thingwatcherDialect = "0.1";

export function FullRenderer({ handle, args }: FullRendererProps) {
  const data = usePayload(handle) as MyType;
  const DraggableIDMarker = useDraggableIdMarker(handle);

  return (
    <DraggableIDMarker>
      <span>{data.name}</span>
    </DraggableIDMarker>
  );
}
```

## Dialect Validation

After esbuild compiles the bundle, when we extract the module:
```typescript
const mod = await loadCompiledModule(code);
if (mod.thingwatcherDialect !== "0.1") {
  throw new Error(`Invalid or missing thingwatcherDialect. Expected "0.1", got "${mod.thingwatcherDialect}"`);
}
// Then extract FullRenderer, RefRenderer, etc.
```

## Edge Cases

1. What if renderer doesn't use a hook? That's fine, hooks are optional.
2. What if renderer calls useTriggerable on RefRendererHandle? TypeScript error at dev time.
3. What if dataChunks is empty? usePayload returns undefined, useDataChunks returns [].
