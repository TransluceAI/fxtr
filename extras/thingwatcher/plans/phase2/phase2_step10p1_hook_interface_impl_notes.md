# Implementation Notes: Phase 2 Step 10.1 - Hook Interface

## Notes for Phase 1: Backend Schema Changes

Phase 1 went smoothly overall. The main work was updating the GraphQL schema and propagating the changes through the resolvers and tests.

### Key Decisions

**thingstateId for "no state exists" cases**: When a Thing is requested but doesn't exist at a tick (e.g., following a link to a thing that was deleted), we return `thingstateId: 0`. This is a sentinel value indicating no real state exists. The alternative was to make thingstateId nullable, but since it's used for change detection, having a consistent integer type seemed cleaner.

**Keeping the `things(triggerable: Boolean)` filter**: Although we removed `triggerable` as a field on Thing, we kept the filter parameter on `Tick.things(triggerable: Boolean)`. This allows clients to still filter for triggerable things without having to cross-reference with `triggerableKeys`. The filter logic uses a local computation that doesn't get exposed in the response.

**Server-side change detection retained**: The original plan suggested moving change detection client-side, but this was corrected. The `changes(since:)` field on Tick, along with `ChangeKind` enum and `ThingChange` type, were all retained. The resolver was updated to use `thingstateId` instead of `triggerable` in the returned Thing objects.

### Changes Made

Schema changes:
- Added `thingstateId: Int!` to Thing type
- Removed `triggerable: Boolean!` from Thing type
- Added `triggerableKeys: [String!]!` query
- Added `NewTickPayload` type with `tick` and `triggerableKeys`
- Updated `newTick` subscription to return `NewTickPayload`

Resolver changes:
- All Thing resolvers updated to include `thingstateId` instead of `triggerable`
- Added `resolve_triggerable_keys` query resolver
- Updated `new_tick_source` subscription to return `NewTickPayload` format
- Updated `resolve_tick_changes` to use `thingstateId` in returned Things

### thingstateId Invariant Fix

The thingstateId invariant (thingstateId must change when links change) was not initially enforced. This was fixed by modifying `_process_thing_state` in `watcher.py`:

1. Added `_links_changed()` helper method that compares old links (`dict[bytes, CurrentLinkInfo]`) with new links (`dict[bytes, ThingLink]`) and returns True if any link was added, removed, or modified (different link_type or to_thing_key).

2. Modified `_process_thing_state` to check for link changes in addition to data changes:
   ```python
   data_changed = new_data_bytes != prev.data_bytes
   links_changed = self._links_changed(prev.links, new_links)

   if data_changed or links_changed:
       # Invalidate old state, write new state
       # This ensures thingstateId changes when links change
   ```

3. Updated `test_link_change_without_data_change` in `tests/test_persistence.py` to expect the new behavior (4 thing_state rows instead of 3, since c1 now gets a new state when its link changes).

Relevant files:
- `thingwatcher/schema.graphql` - GraphQL schema with new types
- `thingwatcher/server.py` - All resolver changes
- `thingwatcher/db.py` - Added invariant comment
- `thingwatcher/watcher.py` - thingstateId invariant enforcement
- `tests/test_graphql.py` - Updated tests
- `tests/test_persistence.py` - Updated test for new behavior

All 156 tests pass.

(Implementation note: Discovered additional issues with the thingstateId invariant, fixed in the separate plan doc plans/phase2_step10p1p1_fix_data_chunks.md.)

## Notes for Phase 2: Frontend Cache Configuration

Phase 2 went smoothly. The main work was updating the graphcache configuration and queries to match the new backend schema.

### Key Changes

1. **graphcache config updates**:
   - Added `Thing: () => null` to prevent caching Things by ID (since links/backlinks can change independently)
   - Added `Root`, `ThingChange`, `NewTickPayload` to the keys config (all return null)

2. **Subscription handler implementation**:
   - Added `updates.Subscription.newTick` handler in graphcache config
   - Handler updates `latestTick`, `triggerableKeys`, and `ticks` list directly via `cache.updateQuery`
   - Handler invalidates `Query.tick` for the current tick index via `cache.invalidate`

3. **Query updates**:
   - Added `GET_TRIGGERABLE_KEYS` query for triggerableKeys
   - Updated `GET_TICK_DATA` to use `thingstateId` instead of `triggerable`
   - Updated `GET_THING_DETAILS` to use `thingstateId` instead of `triggerable`
   - Updated `NEW_TICK_SUBSCRIPTION` to expect `NewTickPayload` format with `tick` and `triggerableKeys`

4. **Type updates**:
   - Removed `triggerable: boolean` from Thing interface
   - Added `thingstateId: number` to Thing interface
   - Added `NewTickPayload` interface

### Notes

- The subscription now takes a `$includeStreaming` variable instead of hardcoding `true`. Components will need to pass this explicitly.
- The cache invalidation for `tick` queries uses the specific tick index as args, so only queries for the current tick are invalidated - historical tick data remains cached.
- DataChunk continues to be cached by ID as before (immutable content).

Relevant files:
- `thingwatcher/frontend/hooks/useGraphQL.ts` - graphcache config + subscription handler
- `thingwatcher/frontend/graphql/queries.ts` - Updated queries
- `thingwatcher/frontend/types/index.ts` - Updated types

## Notes for Phase 3: Create ThingWatcherProvider

Phase 3 went smoothly. Created the central ThingWatcherProvider with all the planned functionality.

### Implementation Details

1. **ThingWatcherProvider.tsx**:
   - Created context with `currentTick`, `latestTick`, `isLive`, `resolvedTick`, `triggerableKeys`
   - Provider subscribes to `newTick` - the graphcache handler in useGraphQL.ts does the cache updates
   - Provider queries `GET_LATEST_TICK` and `GET_TRIGGERABLE_KEYS` which are auto-updated by graphcache
   - Exposes `setCurrentTick(tick | 'live')` action

2. **computeChanges utility**:
   - Uses thingstateId for efficient comparison (single integer vs deep comparison)
   - Returns ADDED/MODIFIED/REMOVED changes
   - Note: The plan mentioned "filtered tick changes" for computing changes relative to previous visible tick - this will be implemented in useTickData when we have access to the filtered ticks list

3. **useTriggerableKeys hook**:
   - Returns `triggerableKeys` Set and `isThingTriggerable(key)` helper
   - Also returns `isTriggerableMeaningful` flag (only true when viewing live/latest tick)
   - This replaces the `triggerable` field that was on Thing

4. **hooks/index.ts**:
   - Created index file that re-exports all hooks
   - Documents the hook architecture and data flow

### Design Decisions

- **Separated `computeChanges` from provider**: The utility is exported separately so it can be used by useTickData or any component that needs to compute changes. The provider doesn't compute changes itself - that's left to the consumer hooks.

- **`isTriggerableMeaningful` flag**: Added this to help components know whether triggerable badges should be shown at all. When viewing historical ticks, triggerableKeys from the subscription doesn't apply.

Relevant files:
- `thingwatcher/frontend/hooks/ThingWatcherProvider.tsx` - Provider + hooks + computeChanges
- `thingwatcher/frontend/hooks/index.ts` - Re-exports

## Notes for Phase 4: Create Data Hooks

Phase 4 went smoothly. Created all the data hooks specified in the plan.

### Hooks Created

1. **useThing.ts** - Single thing lookup
   - Supports key, keyJSON, or shortId input (ThingKeyInput union type)
   - Uses resolvedTick from context by default
   - Uses `cache-and-network` request policy for freshness

2. **useTickData.ts** - Tick data with things/changes/roots
   - Returns things, thingsWithRemoved (for showing REMOVED state), changes, roots, renderingConfig
   - Uses server-side changes (from `tick.changes(since:)`) for efficiency
   - Also exports `useComputedChanges` for client-side change computation

3. **useTicks.ts** - Tick list for timeline
   - Supports from/to/levels filtering
   - Auto-updated by graphcache subscription handler

4. **useLatestTick.ts** - Latest tick info
   - Returns full Tick object (not just index)
   - Auto-updated by graphcache subscription handler

5. **useTrigger.ts** - Trigger mutations
   - `triggerPause(keyInput, payload)` for triggering paused things
   - `triggerTick(level, desc)` for manual ticks
   - Returns `triggering` state for loading indicators

6. **useTriggerRecords.ts** - Trigger history
   - Base `useTriggerRecords(options)` hook
   - Helper hooks: `useTriggerRecordsAtTick`, `useTriggeredShortIds`, `useTriggeredTicks`
   - Filtering done client-side (could be optimized with server-side params later)

7. **useRenderingConfig.ts** - Rendering configuration
   - Returns thingTypes array and thingTypeMap for quick lookup
   - Helper hooks: `useThingTypeColor`, `useThingTypeRenderer`

### Design Decisions

- **ThingKeyInput union type**: Used discriminated union for type-safe key input. Components can pass `{ key: '...' }`, `{ keyJSON: {...} }`, or `{ shortId: 42 }`.

- **Server-side vs client-side changes**: Kept using server-side `changes(since:)` in useTickData for efficiency. The `computeChanges` utility and `useComputedChanges` hook are available for cases where client-side computation is needed (e.g., filtered tick changes).

- **Helper hooks**: Initially added convenience hooks like `useTriggeredShortIds` and `useThingTypeColor`, but later removed them as trivial (see simplifications below).

Relevant files:
- `thingwatcher/frontend/hooks/useThing.ts`
- `thingwatcher/frontend/hooks/ticks.ts` (combined useTickData, useTicks, useLatestTick)
- `thingwatcher/frontend/hooks/useTrigger.ts`
- `thingwatcher/frontend/hooks/useTriggerRecords.ts`
- `thingwatcher/frontend/hooks/useRenderingConfig.ts`
- `thingwatcher/frontend/hooks/index.ts`

## Simplifications Made After Initial Implementation

After initial implementation, we reviewed and simplified several hooks based on user feedback:

### 1. Removed `useTriggerableKeys` hook
**Original plan**: Separate hook to get triggerable keys and check if a thing is triggerable.
**Simplified**: Components just use `useThingWatcher()` directly:
```typescript
const { triggerableKeys, isLive } = useThingWatcher();
const isTriggerable = isLive && triggerableKeys.has(thing.key);
```
**Rationale**: `isTriggerableMeaningful` was redundant with `isLive`, and the hook added no real value.

### 2. Combined tick hooks into `hooks/ticks.ts`
**Original plan**: Separate files for `useTicks.ts`, `useLatestTick.ts`, `useTickData.ts`.
**Simplified**: All combined into single `hooks/ticks.ts` file.
**Rationale**: These were very small related hooks - consolidating reduces file clutter.

### 3. Split `useTrigger` into two hooks with tuple returns
**Original plan**: Single `useTrigger()` returning `{ triggerPause, triggerTick, triggering }`.
**Simplified**: Two hooks:
- `useTriggerThing(keyInput)` → `[triggering, trigger(payload?)]`
- `useTriggerTick()` → `[triggering, trigger(level?, desc?)]`
**Rationale**: Cleaner API - bind the identity upfront. Derive `triggering` from mutation state instead of manual tracking.

### 4. Server-side filtering for trigger records
**Original plan**: Client-side filtering for `from`/`to` parameters.
**Simplified**: Use server-side filtering via GraphQL variables.
**Rationale**: Server already supports these params - no need to fetch all and filter client-side.

### 5. Removed trivial helper hooks from useTriggerRecords
**Original plan**: `useTriggeredShortIds(tick)` and `useTriggeredTicks()` helper hooks.
**Simplified**: Removed - callers can easily do `new Set(records.map(...))` themselves.
**Rationale**: These were trivial one-liners that didn't warrant separate exports.

### 6. Renamed `useRenderingConfig` to `useThingTypeConfig(thingType)`
**Original plan**: Fetch all configs, provide map for lookup.
**Simplified**: Query single type via new `thingTypeConfig(thingType: String!)` GraphQL query.
**Rationale**: Query just what you need - more efficient for components that only care about one type.

### 7. Removed `RenderingConfig` wrapper type from schema
**Original plan**: `renderingConfig: RenderingConfig!` with `RenderingConfig { thingTypes: [ThingTypeConfig!]! }`.
**Simplified**: `renderingConfig: [ThingTypeConfig!]!` returns array directly.
**Rationale**: Unnecessary wrapper type - just inline the array.

### Final hook structure:
```
hooks/
├── ThingWatcherProvider.tsx   # Provider + useThingWatcher + computeChanges
├── ticks.ts                   # useLatestTick, useTicks, useTickData, useComputedChanges
├── useThing.ts                # Single thing by key/shortId
├── useTrigger.ts              # useTriggerThing, useTriggerTick
├── useTriggerRecords.ts       # useTriggerRecords, useTriggerRecordsAtTick
├── useRenderingConfig.ts      # useThingTypeConfig
├── useChunkResolver.ts        # Existing chunk resolver
├── useGraphQL.ts              # GraphQL client factory + graphcache config
└── index.ts                   # Re-exports
```

## Notes for Phase 5, 6, 7: AppContext Migration + Component Updates

These three phases were done together because they're tightly coupled - changing AppContext to use `key` requires updating all components that consume it.

### Phase 5: Remove shortId from ThingKeyInput

- Updated `ThingKeyInput` union type in `useThing.ts` to only allow `{ key }` or `{ keyJSON }`
- Updated `useTrigger.ts` mutation to use key instead of shortId
- Updated JSDoc comments and examples

### Phase 6: Update AppContext

**Key changes:**
1. Removed tick state from AppContext - now managed by ThingWatcherProvider
2. Changed `selectedThingId: number | null` to `selectedThingKey: string | null`
3. Changed `setSelectedThing(id)` to `setSelectedThing(key)`
4. Removed `toggleLiveMode` and `setCurrentTick` from AppContext - use `setCurrentTick` from `useThingWatcher()` instead

**New AppContext interface:**
```typescript
interface AppState {
  selectedThingKey: string | null;  // b64 key - canonical identifier
  levelFilter: string[];
}

interface AppContextValue {
  state: AppState;
  setSelectedThing: (key: string | null) => void;
  setLevelFilter: (levels: string[]) => void;
}
```

### Phase 7: Migrate Components

**App.tsx:**
- Wrapped in ThingWatcherProvider
- Uses `useThingWatcher()` for tick state (`resolvedTick`, `latestTick`, `isLive`, `setCurrentTick`)
- Updated GET_THING_DETAILS to use key instead of shortId
- Updated trigger record filtering to use key

**DockviewLayout.tsx:**
- Updated `DockviewLayoutProps` interface: `selectedThingKey` instead of `selectedThingId`
- Updated `onSelectThing` signature to accept key
- Updated `openThingPanel` to take both key and shortId (key for params, shortId for panel ID readability)
- Updated context menu handlers to pass key

**Graph.tsx:**
- Updated `selectedThingKey` prop
- Updated click/context menu handlers to use key
- Updated change detection to use key

**ThingsList.tsx:**
- Uses `useThingWatcher()` to get `triggerableKeys`
- Updated selection and filtering to use key

**RootsPanel.tsx:**
- Updated props to use `selectedThingKey`

**DetailsPanel.tsx:**
- Uses `useThingWatcher()` to get `triggerableKeys`
- Updated `onSelectThing` and `onLinkContextMenu` to use key
- Updated trigger mutation to use key

**ThingViewPanel.tsx:**
- Params now include both `key` (for query) and `shortId` (for display)
- Query uses key instead of shortId

**ThingContextMenu:**
- State now includes both `thingKey` and `shortId`
- `onOpenInPanel` callback now passes both

**Timeline.tsx:**
- Gets tick state from `useThingWatcher()` instead of `useAppContext()`

**EmbedApp.tsx:**
- Added `triggerableKeys` to the GET_THING_BY_KEY query
- Computes `isTriggerable` from triggerableKeys Set
- Updated TRIGGER_PAUSE mutation to use key
- Updated handlers to use key signatures

**ThingWatcherBindings.tsx (lib):**
- Updated `RendererContextValue` interface:
  - `onSelectThing: (key: string) => void`
  - `onContextMenu: (event, key, shortId) => void`
  - `triggerPause: (key, payload) => Promise<TriggerResult>`
- Updated ThingLink component to store key in state and use it for selection
- Updated trigger function to use key

### Design Decisions

**Panel IDs vs Keys:** Panel IDs still use shortId for readability (`thing-view-42`) while the panel params contain the full key for querying. This matches the plan's design note.

**EmbedApp doesn't use ThingWatcherProvider:** EmbedApp is a separate entry point that manages its own tick state. Instead of wrapping it in ThingWatcherProvider, it fetches `triggerableKeys` alongside the thing query to maintain similar functionality.

**Context menu needs both key and shortId:** ThingContextMenu needs the key for `onOpenInPanel` (to pass to ThingViewPanel) and the shortId for the panel ID. Components build lookup maps or store both values in context menu state.

### Files Modified

- `App.tsx`
- `components/DockviewLayout/DockviewLayout.tsx`
- `components/DockviewLayout/GraphPanelWrapper.tsx`
- `components/DockviewLayout/ThingsListPanelWrapper.tsx`
- `components/DockviewLayout/RootsPanelWrapper.tsx`
- `components/DockviewLayout/DetailsPanelWrapper.tsx`
- `components/DockviewLayout/ThingViewPanel.tsx`
- `components/Graph/Graph.tsx`
- `components/ThingsList/ThingsList.tsx`
- `components/RootsPanel/RootsPanel.tsx`
- `components/DetailsPanel/DetailsPanel.tsx`
- `components/ThingContextMenu/ThingContextMenu.tsx`
- `components/Timeline/Timeline.tsx`
- `EmbedApp.tsx`
- `lib/ThingWatcherBindings.tsx`
- `hooks/useThing.ts`
- `hooks/useTrigger.ts`
- `hooks/index.ts`
- `hooks/useGraphQL.ts`
- `graphql/queries.ts`
- `types/index.ts`

All TypeScript compilation passes.

**REVIEWER NOTES**
These design decisions will need to be reversed to get back to the intended spec.
- [fixed] Panel IDs should be based on the full thing ID
- [fixed] ThingContextMenu should not need short ID passed; component lookup maps should be removed
- [fixed] EmbedApp should use ThingWatcherProvider and the existing hooks, it should NOT use its own system.

Additionally:

- [fixed] in ThingView, triggerable should NOT be forced to true; instead check the thingwatcher context to see if (a) we are live and also (b) this key is in the triggerable keys

## Notes for Phase 8: Query Cleanup (step 10p1p3)

Implemented the comprehensive query cleanup as detailed in `phase2_step10p1p3_query_cleanup.md`.

### Key Changes

1. **`useThing` enhanced with `includeData` option**:
   - When `includeData: true`, fetches thing data then resolves chunk data in a second step
   - Two-step process leverages graphcache's DataChunk caching - old chunks stay cached when new ones are appended
   - When `includeData: false` (default), only fetches `dataChunks { id }` for simple lookups

2. **`renderingConfig` removed from `useTickData`**:
   - Components now use `useThingTypeConfig(thingType)` to get rendering config per type
   - Added `useColorMapsForGraphOnly()` hook for Graph to get pre-built node/link color maps

3. **RendererContext simplified**:
   - Removed `client` and `renderers` map from `RendererContextValue`
   - `ThingView` and `ThingLink` now use hooks directly (`useThing`, `useThingTypeConfig`)
   - Removed `shortId` prop support from ThingView/ThingLink (use `thingKey` instead)

4. **All components migrated to hooks**:
   - App.tsx: Uses `useTicks`, `useTriggerRecords`, `useTickData`, `useThing`
   - ThingViewPanel.tsx: Uses `useThing`, `useTriggerRecords`
   - EmbedApp.tsx: Wrapped in ThingWatcherProvider, uses all hooks
   - Timeline.tsx: Uses `useTriggerTick` hook
   - Graph.tsx: Uses `useColorMapsForGraphOnly` hook
   - DetailsPanel.tsx: Uses `useThingTypeConfig` hook

5. **queries.ts cleaned up**:
   - Removed: `GET_TICK_DATA`, `GET_THING_DETAILS`, `GET_DATA_CHUNK`, `TRIGGER_TICK`, `TRIGGER_PAUSE`, `GET_RENDERING_CONFIG`
   - Kept: Shared queries used by multiple hooks (`GET_LATEST_TICK`, `GET_TICKS`, `GET_TRIGGERABLE_KEYS`, `GET_TRIGGER_RECORDS`, `NEW_TICK_SUBSCRIPTION`)
   - No components import from queries.ts anymore - only hooks do

6. **Deleted `useChunkResolver.ts`**:
   - Functionality now built into `useThing({ includeData: true })`

### Design Decisions

- **useColorMapsForGraphOnly hook added**: Graph needs color maps for node/edge coloring. Created `useColorMapsForGraphOnly()` that returns pre-built `nodeColorMap` and `linkColorMap` without exposing the raw configs. Named to make it clear this hook is Graph-specific and should be updated if Graph is refactored.

- **queries.ts not fully eliminated**: Some queries are used by multiple hooks (e.g., `GET_LATEST_TICK` used by both `ThingWatcherProvider` and `ticks.ts`). Keeping them in queries.ts avoids duplication. Added clear documentation that components should NOT import from this file.

### Files Modified

- `hooks/useThing.ts` - Added `includeData` option with two-step chunk resolution
- `hooks/ticks.ts` - Moved GET_TICK_DATA here, removed renderingConfig
- `hooks/useRenderingConfig.ts` - Added `useColorMapsForGraphOnly()` hook
- `hooks/useTrigger.ts` - TRIGGER_PAUSE now private (see refactor notes below)
- `hooks/index.ts` - Updated exports
- `lib/ThingWatcherBindings.tsx` - ThingView/ThingLink now use hooks
- `components/DockviewLayout/DockviewLayout.tsx` - Removed renderersMap
- `components/DockviewLayout/GraphPanelWrapper.tsx` - Removed renderingConfig prop
- `components/DockviewLayout/DetailsPanelWrapper.tsx` - Removed renderingConfig prop
- `components/DockviewLayout/ThingViewPanel.tsx` - Uses hooks
- `components/DetailsPanel/DetailsPanel.tsx` - Uses useThingTypeConfig hook
- `components/Graph/Graph.tsx` - Uses useColorMapsForGraphOnly hook
- `components/Timeline/Timeline.tsx` - Uses useTriggerTick hook
- `App.tsx` - Full migration to hooks
- `EmbedApp.tsx` - Full migration to hooks
- `embed.tsx` - Wrapped in ThingWatcherProvider
- `graphql/queries.ts` - Cleaned up, removed unused queries

All TypeScript compilation passes.

## Notes for TRIGGER_PAUSE Refactoring

After completing the main query cleanup, we refactored to eliminate all direct `useMutation` usage in components.

### Problem

After the initial cleanup, `TRIGGER_PAUSE` was still exported from `hooks/useTrigger.ts` because DockviewLayout and DetailsPanel needed to trigger with dynamic keys passed as a parameter. This didn't fit the `useTriggerThing(keyInput)` pattern which binds the key upfront.

However, upon analysis, we found that `triggerPause(key, payload)` in RendererContext was only ever called with the known thing's key:
- `ThingView` called `ctx.triggerPause(thing.key, payload)` - it always knows its own thing
- `DetailsPanel` called it with the selected thing's key - also known at call time

### Solution

Since the key is always known where triggering happens, components can use `useTriggerThing` directly:

1. **ThingView** now uses `useTriggerThing({ key: thing.key })` directly
2. **DetailsPanel** now uses `useTriggerThing({ key: thing?.key ?? '' })`
3. **RendererContextValue** removed `triggerPause` property
4. **DockviewLayout/EmbedApp** no longer create triggerPause callbacks
5. **TRIGGER_PAUSE** made private (no longer exported)

### Changes Made

- `lib/ThingWatcherBindings.tsx`:
  - Removed `triggerPause` from `RendererContextValue` interface
  - `ThingView` uses `useTriggerThing({ key: thing?.key ?? '' })` and creates trigger function from hook

- `components/DetailsPanel/DetailsPanel.tsx`:
  - Replaced `useMutation(TRIGGER_PAUSE)` with `useTriggerThing({ key: thing?.key ?? '' })`
  - Added local `triggerResultState` state to track trigger results

- `components/DockviewLayout/DockviewLayout.tsx`:
  - Removed `useMutation(TRIGGER_PAUSE)` and triggerPause callback
  - Simplified rendererContextValue (no longer includes triggerPause)

- `EmbedApp.tsx`:
  - Removed triggerPause callback
  - Simplified rendererContextValue (no longer includes triggerPause)

- `hooks/useTrigger.ts`:
  - Made `TRIGGER_PAUSE` const private (removed `export`)

- `hooks/index.ts`:
  - Removed `TRIGGER_PAUSE` from exports

### Benefits

- No components use `useMutation` directly anymore
- All GraphQL access goes through hooks
- Cleaner RendererContext interface
- Better encapsulation of GraphQL mutations
