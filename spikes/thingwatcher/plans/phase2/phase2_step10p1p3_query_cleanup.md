# Phase 2 Step 10.1.3: Query Cleanup - Migrate All Components to Hooks

## Problem

Several components still use direct `useQuery` calls instead of the hooks created in Phase 4. The goal of step 10p1 is to have all GraphQL access go through hooks, with `queries.ts` eventually being removed (queries become private constants inside hook files).

## Current State

### Direct useQuery Usage in Components

**EmbedApp.tsx**:
- `GET_LATEST_TICK` - should use ThingWatcherProvider
- `GET_RENDERING_CONFIG` - only needs single type, use `useThingTypeConfig()`
- Custom `GET_THING_BY_KEY` with full chunk data - should use `useThing`
- Custom subscription - should use ThingWatcherProvider
- Custom mutation - should use `useTriggerThing`
- Builds `renderersMap` manually - no longer needed

**App.tsx**:
- `GET_TICKS` - hook exists: `useTicks` (not using it)
- `GET_TRIGGER_RECORDS` - hook exists: `useTriggerRecords` (not using it)
- `GET_TICK_DATA` - hook exists: `useTickData` (not using it)
- `GET_THING_DETAILS` - should use `useThing({ includeData: true })`

**ThingViewPanel.tsx**:
- `GET_THING_DETAILS` - should use `useThing({ includeData: true })`
- `GET_TRIGGER_RECORDS` - hook exists: `useTriggerRecords` (not using it)

**DockviewLayout.tsx**:
- Builds `renderersMap` from props - no longer needed after ThingView uses hook directly

**ThingWatcherBindings.tsx** (lib):
- Has inline `GET_THING_FOR_RENDER` query - should use `useThing`
- Has inline `GET_DATA_CHUNK_FOR_RENDER` query - will be removed (useThing includes data)
- Looks up renderer from `ctx.renderers` map - should use `useThingTypeConfig()` directly

### The Core Issue: useThing Doesn't Include Chunk Data

The current `useThing` hook fetches:
```graphql
dataChunks {
  id  # IDs only, no data
}
```

But components need:
```graphql
data          # First chunk data
dataChunks {
  id
  data        # Full chunk data included
}
```

### Redundancy: useTickData Returns renderingConfig

`useTickData` currently returns `renderingConfig` alongside things/changes/roots. This is redundant since we have `useThingTypeConfig`. The rendering config should be fetched separately by components that need it.

### Redundancy: renderersMap Passed Through Context

Currently DockviewLayout and EmbedApp both:
1. Query all rendering configs
2. Build a `Map<thingType, rendererTSX>`
3. Pass it through `RendererContext`
4. `ThingView` looks up renderers from the map

This can be simplified: `ThingView` can just call `useThingTypeConfig(thingType)` directly.

## Proposed Solution

### 1. Enhance `useThing` with `includeData` Option

Add an option to include full data in the query:

```typescript
interface UseThingOptions {
  keyInput: ThingKeyInput;
  tick?: number;
  pause?: boolean;
  includeData?: boolean;  // NEW: include data field and full dataChunks
}
```

When `includeData` is true, the hook uses a query that includes:
- `data` field (first chunk's data)
- `dataChunks { id, data }` (all chunks with data)

Implementation: Have two query constants and select based on the option.

### 2. Remove renderingConfig from useTickData

The `GET_TICK_DATA` query and `useTickData` hook should NOT include rendering config. Components that need type-specific config use `useThingTypeConfig()`.

### 3. Simplify RendererContext

Remove `renderers: Map<string, string>` from `RendererContextValue`. `ThingView` will call `useThingTypeConfig()` directly to get the renderer for the thing type it's rendering.

### 4. Clean Up ThingWatcherBindings.tsx

- Remove inline `GET_THING_FOR_RENDER` query - use `useThing({ includeData: true })`
- Remove inline `GET_DATA_CHUNK_FOR_RENDER` query - no longer needed
- `ThingView` uses `useThingTypeConfig(thingData.thingType)` instead of `ctx.renderers.get()`
- `ThingLink` uses `useThing()` (without data) instead of inline query

### 5. Delete useChunkResolver

No longer needed since `useThing({ includeData: true })` fetches full chunk data.

### 6. Migrate All Components to Hooks

**App.tsx**:
- Replace `useQuery(GET_TICKS)` with `useTicks()`
- Replace `useQuery(GET_TRIGGER_RECORDS)` with `useTriggerRecords()`
- Replace `useQuery(GET_TICK_DATA)` with `useTickData()`
- Replace `useQuery(GET_THING_DETAILS)` with `useThing({ includeData: true })`

**ThingViewPanel.tsx**:
- Replace `useQuery(GET_THING_DETAILS)` with `useThing({ includeData: true })`
- Replace `useQuery(GET_TRIGGER_RECORDS)` with `useTriggerRecords()`

**DockviewLayout.tsx**:
- Remove `renderersMap` building logic
- Remove `renderers` from RendererContext value

**EmbedApp.tsx**:
- Wrap in `ThingWatcherProvider` (in embed.tsx)
- Remove all local tick state management - use `useThingWatcher()`
- Replace thing query with `useThing({ includeData: true })`
- Remove rendering config query and `renderersMap` building
- Replace trigger mutation with `useTriggerThing()`
- Remove custom subscription - ThingWatcherProvider handles it

### 7. Clean Up queries.ts

After migration, move remaining queries into their respective hook files as private constants. Goal: `queries.ts` should be empty or removed.

## Implementation Steps

### Phase A: Enhance Hooks ✅
- [x] Add `includeData` option to `useThing` hook (two query variants)
- [x] Remove `renderingConfig` from `GET_TICK_DATA` query and `useTickData` hook
- [x] Delete `useChunkResolver.ts`
- [x] Update hooks/index.ts exports

### Phase B: Simplify RendererContext ✅
- [x] Remove `renderers` from `RendererContextValue` interface
- [x] Update `ThingView` to use `useThingTypeConfig()` for renderer lookup
- [x] Update `ThingView` to use `useThing({ includeData: true })` for thing data
- [x] Update `ThingLink` to use `useThing()` for thing info
- [x] Remove inline GraphQL queries from ThingWatcherBindings.tsx
- [x] Update DockviewLayout.tsx - remove renderersMap building

### Phase C: Migrate App.tsx ✅
- [x] Replace GET_TICKS with useTicks()
- [x] Replace GET_TRIGGER_RECORDS with useTriggerRecords()
- [x] Replace GET_TICK_DATA with useTickData()
- [x] Replace GET_THING_DETAILS with useThing({ includeData: true })
- [x] Remove direct useQuery imports

### Phase D: Migrate ThingViewPanel.tsx ✅
- [x] Replace GET_THING_DETAILS with useThing({ includeData: true })
- [x] Replace GET_TRIGGER_RECORDS with useTriggerRecords()
- [x] Remove direct useQuery imports

### Phase E: Migrate EmbedApp.tsx ✅
- [x] Update embed.tsx to wrap EmbedApp in ThingWatcherProvider
- [x] Remove all inline GraphQL query definitions
- [x] Remove local tick state - use useThingWatcher()
- [x] Replace thing query with useThing({ includeData: true })
- [x] Remove renderersMap building (no longer needed)
- [x] Replace trigger mutation with useTriggerThing()
- [x] Remove custom subscription handling

### Phase F: Cleanup ✅
- [x] Move remaining queries from queries.ts into respective hook files
- [x] Clean up queries.ts (kept shared queries used by multiple hooks)
- [x] Verify no components import directly from queries.ts
- [x] Update LOCAL_CODE_OVERVIEW.md
- [ ] Update CODE_OVERVIEW.md (if needed - check if frontend changes need reflecting)
- [ ] Add implementation notes to phase2_step10p1_hook_interface_impl_notes.md

## Notes

- `useThingTypeConfig(thingType)` is the single way to get rendering config for a type
- The `includeData` option defaults to `false` to avoid fetching unnecessary data for simple use cases
- `ThingView` now independently fetches both thing data and renderer config via hooks
