# fxtr-react-dispatch: Design Notes

Design decisions made while sketching the implementation. Companion to `overview_sketch.md`.

## Layered Architecture

Three layers, each usable independently:

1. **Core types** (`types.ts`): `DispatchKey<Props>`, `DispatchResolution<Props, Meta>`, `DispatchHandler<Meta>` interface
2. **React components** (`context.tsx`, `Render.tsx`): `<DispatchProvider>`, `<Render>` component, dispatch cache
3. **Registry builder** (`registry.tsx`): Convenience for assembling a `DispatchHandler` from a list of codec+component entries

## DispatchKey<Props>

A branded symbol. `Props` is the **extra props type** that rendered components accept beyond `entityId: FxtrID`. This means the dispatch key defines *what kind of rendering slot* something fills (e.g. "main view" vs "figure" vs "sidebar annotation"), not what entity type it handles.

```ts
declare const __DISPATCH_KEY_BRAND: unique symbol;
type DispatchKey<Props extends object = object> = symbol & {
  readonly [__DISPATCH_KEY_BRAND]: Props;
};
```

Created via `declareDispatchKey<Props>(description)`.

Children support: `Props` can include `children` (e.g. function-as-child), since it's just the props type.

## DispatchResolution<Props, Meta>

The result of dispatch: a component + optional metadata.

```ts
interface DispatchResolution<Props extends object = object, Meta = unknown> {
  Component: ComponentType<{ entityId: FxtrID } & Props>;
  metadata?: Meta | undefined;
}
```

Generic over both `Props` and `Meta`, so the Component's signature is fully typed through the dispatch chain. `Props` flows from the `DispatchKey` through `DispatchHandler.lookup` into the resolution.

`Meta` is handler-defined (e.g. `{ label: string; source: string }` for debug info). `<Render>` ignores it; the provider can expose it for debug UI.

The dispatch cache stores Props-erased entries (keyed by dispatch key symbol). On retrieval, `<Render>` casts back to the key's Props type — sound because entries are grouped by key.

## DispatchHandler<Meta>

The interface the provider takes:

```ts
interface DispatchHandler<Meta = unknown> {
  lookup<Props extends object>(
    key: DispatchKey<Props>,
    typeId: FxtrID,
    scope: FxtrScope,
  ): PromiseOrEager<DispatchResolution<Props, Meta>>;

  getLoadingFallback?<Props extends object>(
    key: DispatchKey<Props>,
  ): ComponentType<{ entityId: FxtrID } & Props> | undefined;
}
```

- **`lookup`** returns `PromiseOrEager` — returns `Eager` when the result can be determined synchronously (e.g. cached codec checks), allowing `<Render>` to skip Suspense. Generic over Props so the returned Component is typed to match the key.
- **`getLoadingFallback`** is sync and optional — returns a placeholder component for a given key. If present, `<Render>` shows this instead of suspending while waiting. The loading fallback receives the full Props (not just `entityId`) so it can render a shaped placeholder appropriate for the rendering slot. If absent, `<Render>` suspends.

No separate fallback method on the interface — the handler is expected to always return *something* from `lookup` (even if it's a generic fallback). The registry builder handles this by accepting a fallback component.

## Provider Reactivity

Both `<DispatchProvider>` and `<FxtrProvider>` react to prop changes by reference identity:

- **`<FxtrProvider>`**: When `scope` changes, closes the old child scope, creates a fresh one, and clears all load caches.
- **`<DispatchProvider>`**: When `handler` changes, creates a fresh context with an empty cache, forcing all `<Render>` components to re-resolve.

## Eager Support

`DispatchHandler.lookup` returns `PromiseOrEager<DispatchResolution>`. When the handler can determine the result synchronously (e.g. cached codec checks in a static registry), it returns `Eager`, and `<Render>` skips Suspense entirely. The registry builder uses `.then()` chaining through `cachedSupportsEntityType`, so it returns Eager when all codec checks are cached.

### Promise-Only Cache Entries

Cache entries (both for entity loads and dispatch lookups) always store a `Promise<void>` as their notification signal — never an `Eager`. For Eager resolutions, we use `Promise.resolve()`. This guarantees by construction that we never throw an Eager at React Suspense. The natural check order works everywhere:

```ts
if (entry.error) throw entry.error;
if (entry.value) return entry.value;
throw entry.promise; // always a real Promise<void>
```

See `eager_cache_pattern.md` for full details on this pattern.

## <Render> Flow

```tsx
<Render entity={fxtrId} as={myKey} extraProp={...} />
```

`<Render>` manages the entity load directly (not via `useFxtrLoad`) so that the loading fallback covers the full lifecycle — entity load, dispatch lookup, and rendering.

1. Load entity as `UnknownEntity` via `scope.load()` to extract `$fxtr.type`
   - Eager → proceed immediately
   - Promise + loading fallback → show fallback, subscribe for re-render
   - Promise + no fallback → suspend
2. Extract `$fxtr.type` from `raw_cbor` → `typeId: FxtrID`
3. Look up `(key, typeId)` in dispatch cache
4. **Cache hit (resolved)** → render `<Component entityId={entity} {...extraProps} />`
5. **Cache hit (pending)** → show loading fallback or suspend
6. **Cache miss** → call `handler.lookup(key, typeId, scope)`
   - **Eager result** → cache and render immediately (no Suspense)
   - **Promise result** → cache, show loading fallback or suspend
7. **Error** → throw (caught by React error boundary)

The dispatched Component receives `entityId: FxtrID` plus whatever extra Props the key defines. It does NOT receive a pre-loaded entity — the component is responsible for loading (typically via `useFxtrLoad` with whatever codec it knows about internally).

## Registry Builder

Convenience layer that produces a `DispatchHandler` from a list of entries:

```ts
interface RegistryEntry<Props, T extends Entity, Meta> {
  key: DispatchKey<Props>;
  codec: EntityLoadCodec<T>;
  Component: ComponentType<{ entityRef: EntityRef<T> } & Props>;
  metadata?: Meta | undefined;
}
```

Key design decisions:
- Each entry provides a **codec** and a **Component that takes `EntityRef<T>`** (not `entityId`).
- The registry builder wraps each Component: given an `entityId: FxtrID`, it creates `new EntityRef(entityId, codec)` and passes that as `entityRef` to the inner Component.
- For a given `(key, typeId)`, the registry tries codecs in declaration order via sequential `.then()` chaining through `cachedSupportsEntityType`. Returns `Eager` when all codec checks are cached.
- A `fallback` component can be provided. If no entry matches, the fallback renders.
- Loading components can be provided per dispatch key via `options.loading`.

```ts
function createDispatchRegistry<Meta = unknown>(
  entries: RegistryEntry<any, any, Meta>[],
  options?: {
    fallback?: ComponentType<{ entityId: FxtrID }>;
    loading?: Map<symbol, ComponentType<{ entityId: FxtrID } & any>>;
    fallbackMetadata?: Meta;
  },
): DispatchHandler<Meta>;
```

Helper function `entry(key, codec, Component, metadata?)` provides type-safe entry construction, inferring `T` from the codec and `Props` from the key.

## Relationship to Existing Systems

- **`@fxtr/react`**: `<Render>` uses `useFxtrScope()` to get a scope for entity loading and handler lookups. The dispatched component also uses `useFxtrLoad` for its typed load. Both go through the same `<FxtrProvider>` context/cache.
- **`@fxtr/core` codecs**: The registry builder reuses `cachedSupportsEntityType` for codec matching. Entity data fetched via the unknown load is already in the scope's arena, so the typed re-load only re-decodes (no network round trip).
- **`unionLoadCodec`**: Similar concept but at the codec level. The dispatch system adds the dimension of *which React component* to use, keyed by interface type.

## extractTypeId

`extractTypeId` (extracting FxtrID from `$fxtr.type` field in raw CBOR) is currently internal to `unionLoadCodec.ts`. For the dispatch package, we duplicate this small helper (~5 lines). Could consider exporting it from `@fxtr/core` later if it becomes a common need.

## Implementation Steps

- [x] Create `packages/fxtr-react-dispatch/` package skeleton (package.json, tsconfig.json)
- [x] Wire into workspace (root tsconfig.json references)
- [x] `src/types.ts`: DispatchKey, DispatchResolution, DispatchHandler
- [x] `src/context.tsx`: DispatchProvider, dispatch cache, internal context
- [x] `src/Render.tsx`: `<Render>` component
- [x] `src/registry.tsx`: createDispatchRegistry
- [x] `src/index.ts`: public exports
- [ ] Update CODE_OVERVIEW.md
