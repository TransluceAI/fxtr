# @fxtr/react-dispatch — Code Overview

Type-driven dispatch system for rendering fxtr entities with React. Given an entity ID and a "dispatch key" (a rendering slot like `MainView` or `SummaryCard`), the system looks up the entity's type, finds the matching component, and renders it.

## How It Works

1. **Declare** dispatch keys with `declareDispatchKey<Props>(description)` — each key is a branded symbol carrying its props type.
2. **Register** handlers mapping `(key, entityCodec) → component` via `createDispatchRegistry()`.
3. **Provide** the handler to the tree with `<DispatchProvider handler={...}>`.
4. **Render** with `<Render entity={entityId} as={dispatchKey} {...props} />` — loads the entity, extracts its type, dispatches to the registered component.

The system caches dispatch resolutions (keyed by dispatch key symbol → type ID string) to avoid repeated lookups. Supports both eager and async (Promise-based) resolutions with loading fallbacks.

## Files

```
src/
├── index.ts        # Public API exports
├── types.ts        # DispatchKey, DispatchResolution, DispatchHandler, declareDispatchKey()
├── context.tsx     # DispatchProvider, DispatchContext, dispatch cache
├── Render.tsx      # <Render> component — entity load → type dispatch → render
└── registry.tsx    # createDispatchRegistry(), entry() helper
```

- **types.ts**: Core type definitions (framework-agnostic). `DispatchKey<Props>` is a branded symbol. `DispatchHandler` is the interface for custom dispatch logic (`lookup` method + optional `loadingFallback`).
- **context.tsx**: `DispatchProvider` supplies the handler and a two-level cache (`dispatch key → typeId → cache entry`) via React context.
- **Render.tsx**: The `<Render>` component implements two-phase loading: load entity → extract type ID → dispatch lookup → render component. Handles async resolutions with Suspense or loading fallbacks.
- **registry.tsx**: `createDispatchRegistry(entries, options)` builds a `DispatchHandler` from declarative `entry(key, codec, component)` registrations. Tries codecs in order until a match is found, wraps the matched component with `EntityRef<T>`.

## Dependencies

- `@fxtr/core` — FxtrID, Entity, EntityRef, EntityLoadCodec, FxtrScope, Eager
- `@fxtr/react` — useFxtrScope hook
- `react` (peer, >=18)
