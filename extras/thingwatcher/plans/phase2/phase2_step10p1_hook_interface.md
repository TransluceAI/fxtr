# Phase 2 Step 10.1: Consistent Hook Interface for Thing Data

## Overview

Refactor the frontend to use a consistent hook-based interface for all GraphQL data access. The key goals are:

1. **b64 key as canonical identifier**: Internally use the base64-encoded CBOR key (`key` field) as the single source of truth for identifying things
2. **Hooks-only GraphQL access**: Only hooks touch GraphQL - components never call `useQuery`/`useMutation` directly
3. **shortId for display only**: shortId is output by hooks for rendering purposes only - never used for requests or indexing
4. **Cache-aware architecture**: Smart invalidation with DataChunk caching for efficient data loading
5. **Automatic refresh**: Centralized subscription with cache invalidation - components just consume hooks and get fresh data automatically

## Current State Analysis

### Problems with Current Approach

1. **Direct GraphQL usage**: Components like `App.tsx`, `ThingViewPanel.tsx`, and `EmbedApp.tsx` directly use `useQuery` and `useMutation`
2. **shortId as primary key**: `AppState.selectedThingId` uses shortId, `GET_THING_DETAILS` queries by shortId
3. **Inconsistent key handling**: Some places use shortId, some use key, no clear convention
4. **Scattered GraphQL definitions**: Queries defined in both `graphql/queries.ts` and inline in components (EmbedApp)
5. **Manual refresh logic**: Each component manages its own refetch logic after subscription events
6. **Thing not cache-friendly**: Thing entities don't have stable cache keys, so same data is refetched when navigating ticks
7. **Triggerable conflated with Thing**: `triggerable` field on Thing doesn't make sense - it's ephemeral, current-tick-only, not stored in DB

### Current Hooks
- `useGraphQL.ts`: Just creates the urql client
- `useChunkResolver.ts`: Resolves chunk IDs to data

## Proposed Architecture

### Separate Triggerability from Thing

**Problem**: `triggerable` is currently a field on `Thing`, but it's fundamentally different:
- It's ephemeral - only valid for current tick
- Not stored in DB (cleared when tick advances)
- Doesn't make sense to query for past ticks
- Conflates ephemeral watcher state with persistent thing state

**Solution**: New separate query for triggerable keys:

```graphql
type Query {
  # Returns b64 keys of things that are currently triggerable
  # Only valid for latest tick - always refetched, never cached
  triggerableKeys: [String!]!
}
```

Client-side: check if a thing's key is in the `triggerableKeys` set.

### Centralized Data Management

**ThingWatcherProvider**: Central context that manages tick state and cache invalidation.

```typescript
interface ThingWatcherContextValue {
  // Tick state (read by hooks)
  currentTick: number | 'live';  // 'live' means follow latest tick
  latestTick: number;

  // Computed from currentTick
  isLive: boolean;               // true when currentTick === 'live'
  resolvedTick: number;          // actual tick number (latestTick when live)

  // Triggerability (separate from Thing cache)
  triggerableKeys: Set<string>;  // b64 keys

  // Actions
  setCurrentTick: (tick: number | 'live') => void;
  // No separate setLiveMode - use setCurrentTick('live') to enter live mode,
  // setCurrentTick(someNumber) to exit and view a specific tick
}
```

The provider:
1. Subscribes to `newTick`
2. On new tick: graphcache handler updates simple queries and invalidates tick data
3. Mounted queries for tick data refetch automatically
4. DataChunk entities remain cached (immutable)

### Key Types

```typescript
// The canonical identifier - base64-encoded CBOR key
type ThingKey = string;

// Input for queries - allows either key or keyJSON
type ThingKeyInput =
  | { key: ThingKey }
  | { keyJSON: unknown };
```

### GraphQL Schema Changes

```graphql
type Thing {
  thingstateId: Int!    # For client-side change detection (NOT for caching)
  shortId: Int!
  key: String!
  keyJSON: JSON
  thingType: String!
  # triggerable: Boolean!  # REMOVED - use triggerableKeys query instead
  links: [Link!]!
  backlinks: [Link!]!
  dataChunks: [DataChunk!]
  data: JSON
}

type Query {
  # Existing queries...

  # NEW: Current triggerable keys (always refetched, never cached)
  triggerableKeys: [String!]!
}
```

**Backend requirement:** `thingstateId` must increment whenever:
- Thing data changes
- Thing links change

This ensures client-side change detection by comparing `thingstateId` is accurate.
Add a comment in the SQL schema noting this invariant.

### graphcache Configuration

```typescript
const cache = cacheExchange({
  keys: {
    // Thing is NOT cached by ID - links/backlinks can change independently
    // Thing data is invalidated and refetched when tick changes
    Thing: () => null,
    // DataChunk IS cached by id - chunk data is immutable
    DataChunk: (data) => data.id ? String(data.id) : null,
    // Types without natural keys
    Tick: () => null,
    Link: () => null,
    // ...
  },
});
```

### Cache Update Flow (Invalidation + Partial Update)

When a new tick arrives, we use a combination of:
1. **Invalidation** for tick-dependent queries (forces refetch)
2. **`cache.updateQuery`** for simple data we already have (latestTick metadata, triggerableKeys)

Thing entities are NOT cached by ID because links and backlinks can change independently of the thing's own data. Instead, tick-related queries are invalidated and refetched.

**Subscription schema change:**
```graphql
type Subscription {
  newTick(includeStreamingUpdates: Boolean = false): NewTickPayload!
}

type NewTickPayload {
  tick: Tick!                   # Full Tick type - frontend selects fields it needs
  triggerableKeys: [String!]!   # Current triggerable b64 keys
}
```

The frontend subscription query only requests the metadata fields it needs:
```graphql
subscription OnNewTick($includeStreaming: Boolean!) {
  newTick(includeStreamingUpdates: $includeStreaming) {
    tick {
      index
      timestamp
      level
      type
      desc
    }
    triggerableKeys
  }
}
```

**graphcache subscription handler:**
```typescript
updates: {
  Subscription: {
    newTick: (result, _args, cache, _info) => {
      const payload = result?.newTick;
      if (!payload) return;

      const { tick, triggerableKeys } = payload;

      // FIRST: Invalidate to ensure stale data is cleared
      // Only invalidate queries at THIS tick - historical ticks are unaffected
      cache.invalidate('Query', 'tick', { index: tick.index });
      cache.invalidate('Query', 'thing', { atTick: tick.index });

      // THEN: Update simple queries with data we have
      cache.updateQuery({ query: LATEST_TICK_QUERY }, () => ({
        latestTick: tick,
      }));

      cache.updateQuery({ query: TRIGGERABLE_KEYS_QUERY }, () => ({
        triggerableKeys,
      }));

      // Append or replace tick in ticks list
      // (streaming updates may update an existing tick rather than adding new)
      cache.updateQuery({ query: TICKS_LIST_QUERY }, (data) => {
        const ticks = data?.ticks || [];
        const existingIndex = ticks.findIndex(t => t.index === tick.index);
        if (existingIndex >= 0) {
          // Replace existing tick (streaming update)
          const updated = [...ticks];
          updated[existingIndex] = tick;
          return { ticks: updated };
        } else {
          // Append new tick
          return { ticks: [...ticks, tick] };
        }
      });
    },
  },
}
```

**Flow:**
```
1. newTick subscription fires with tick metadata + triggerableKeys
2. graphcache handler:
   - Updates latestTick, triggerableKeys, ticks list directly
   - Invalidates tick(index) and thing queries
3. Mounted queries for tick data refetch automatically
4. DataChunk entities remain cached (immutable)
5. Components re-render with fresh data
```

### Client-Side Changes Computation

Instead of fetching `changes(since: $prevTick)` from server, compute changes client-side by comparing cached tick data.

**Benefits:**
- Works uniformly for live and history modes
- Enables "changes since previous *visible* tick" for filtered views
- No additional network requests
- Simpler subscription payload

**Implementation:**
```typescript
function computeChanges(
  currentThings: Thing[],
  previousThings: Thing[],
): ThingChange[] {
  const prevByKey = new Map(previousThings.map(t => [t.key, t]));
  const currByKey = new Map(currentThings.map(t => [t.key, t]));

  const changes: ThingChange[] = [];

  // Find ADDED and MODIFIED
  for (const [key, thing] of currByKey) {
    const prev = prevByKey.get(key);
    if (!prev) {
      changes.push({ kind: 'ADDED', thing });
    } else if (prev.thingstateId !== thing.thingstateId) {
      // thingstateId changes when data OR links change
      changes.push({ kind: 'MODIFIED', thing });
    }
  }

  // Find REMOVED
  for (const [key, thing] of prevByKey) {
    if (!currByKey.has(key)) {
      changes.push({ kind: 'REMOVED', thing });
    }
  }

  return changes;
}
```

Note: `thingstateId` comparison is efficient (single integer compare) and accurate because the backend guarantees thingstateId increments on any data or link change. Backlinks are intentionally excluded from change detection since they reflect changes in *other* things.

**Filtered tick support:**
When tick filtering is active (e.g., only showing "info" level), changes are computed relative to the previous *visible* tick, not the previous tick in absolute order. This provides more intuitive change visualization when filtering.

```typescript
// In useTickData or similar hook
const previousVisibleTick = useMemo(() => {
  // Find the previous tick that passes the current filter
  return filteredTicks.findLast(t => t.index < currentTick);
}, [filteredTicks, currentTick]);

const changes = useMemo(() => {
  if (!previousVisibleTick) return currentThings.map(t => ({ kind: 'ADDED', thing: t }));
  return computeChanges(currentThings, previousTickThings);
}, [currentThings, previousTickThings]);
```

### New Hook Structure

```
hooks/
├── useGraphQL.ts              # urql client factory (existing)
├── useChunkResolver.ts        # Chunk resolution (existing)
├── ThingWatcherProvider.tsx   # NEW - central subscription + cache invalidation
├── useThing.ts                # Single thing by key
├── useTickData.ts             # Things/changes/roots at a tick
├── useTicks.ts                # Tick list for timeline
├── useLatestTick.ts           # Latest tick info
├── useTriggerableKeys.ts      # NEW - current triggerable keys
├── useRenderingConfig.ts      # Rendering config (cached)
├── useTriggerRecords.ts       # Trigger history
├── useTrigger.ts              # Trigger mutations (pause + tick)
└── index.ts                   # Re-exports
```

### Hook Interfaces

#### `ThingWatcherProvider`
```typescript
// Wrap app in this provider - handles subscription and cache invalidation
function ThingWatcherProvider({ children }: { children: React.ReactNode }): JSX.Element;

// Access context
function useThingWatcher(): ThingWatcherContextValue;
```

#### `useThing`
```typescript
interface UseThingOptions {
  keyInput: ThingKeyInput;  // Either { key } or { keyJSON }
  tick?: number;            // Defaults to resolvedTick from context
  pause?: boolean;
}

interface UseThingResult {
  thing: Thing | null;      // No triggerable field - use useTriggerableKeys
  loading: boolean;
  error: Error | null;
}

// No refetch needed - cache invalidation handles it
```

#### `useTickData`
```typescript
interface UseTickDataResult {
  things: Thing[];          // All things at tick (no triggerable field)
  changes: ThingChange[];
  roots: Root[];
  loading: boolean;
  error: Error | null;
}

function useTickData(tickIndex: number, prevTickIndex?: number): UseTickDataResult;
```

#### `useTriggerableKeys`
```typescript
interface UseTriggerableKeysResult {
  triggerableKeys: Set<string>;  // b64 keys
  loading: boolean;
  error: Error | null;
}

// Helper to check if a thing is triggerable
function isThing Triggerable(thingKey: string): boolean;
```

#### `useTicks`
```typescript
interface UseTicksResult {
  ticks: Tick[];
  loading: boolean;
  error: Error | null;
}

function useTicks(options?: { from?: number; to?: number; levels?: string[] }): UseTicksResult;
```

#### `useLatestTick`
```typescript
interface UseLatestTickResult {
  latestTick: Tick | null;
  loading: boolean;
  error: Error | null;
}

function useLatestTick(): UseLatestTickResult;
```

#### `useTrigger`
```typescript
interface UseTriggerResult {
  triggerPause: (keyInput: ThingKeyInput, payload?: unknown) => Promise<TriggerResult>;
  triggerTick: (level?: string, desc?: string) => Promise<Tick>;
  triggering: boolean;
}

function useTrigger(): UseTriggerResult;
```

#### `useTriggerRecords`
```typescript
interface UseTriggerRecordsResult {
  records: TriggerRecord[];
  loading: boolean;
  error: Error | null;
}

function useTriggerRecords(options?: {
  keyInput?: ThingKeyInput;
  from?: number;
  to?: number;
}): UseTriggerRecordsResult;
```

### AppContext Changes

Simplify AppContext - tick management moves to ThingWatcherProvider:

```typescript
interface AppState {
  selectedThingKey: ThingKey | null;  // Changed from shortId to b64 key
  levelFilter: string[];
}

// Tick state now in ThingWatcherProvider
```

### Component Changes

Components no longer manage refresh logic:

```typescript
// Before (App.tsx)
useSubscription({ query: NEW_TICK_SUBSCRIPTION }, (_prev, data) => {
  if (state.liveMode) {
    refetchLatest({ requestPolicy: 'network-only' });
    refetchTicks({ requestPolicy: 'network-only' });
    // ...manual refetch logic
  }
  return data;
});

// After
// ThingWatcherProvider handles subscription and cache invalidation
// Components just use hooks and get fresh data automatically
function MainContent() {
  const { resolvedTick, isLive } = useThingWatcher();
  const { things, changes, roots } = useTickData(resolvedTick, resolvedTick - 1);
  const { triggerableKeys } = useTriggerableKeys();
  // Data is always fresh - no manual refresh needed

  // To enter live mode:
  setCurrentTick('live');

  // To view a specific historical tick (exits live mode):
  setCurrentTick(5);
}
```

## Design Decisions

### Why Thing is NOT cached by ID
Thing entities cannot be reliably cached by `thingstateId` because:
- **Backlinks depend on other things** - they change when other things add/remove links to this thing
- A Thing's full state (including backlinks) can change without its `thingstateId` changing

Instead, we invalidate tick-related queries when new ticks arrive, forcing a refetch of Thing data. DataChunk remains cached by ID since chunk data is immutable.

### Why thingstateId is still exposed
Even though we don't cache by `thingstateId`, we expose it for **client-side change detection**:
- Efficient comparison (single integer vs JSON.stringify)
- Backend guarantees thingstateId increments on data OR link changes
- Enables accurate ADDED/MODIFIED/REMOVED detection without deep comparison
- Backlinks excluded from change detection (they reflect other things' changes)

### Why separate triggerableKeys?
- Triggerability is ephemeral, not stored in DB
- Only makes sense for current tick
- Doesn't belong on Thing type - it's about current watcher state, not thing state
- Clear separation of concerns

### Why cache invalidation for tick data?
- Uses graphcache as designed
- No manual coordination needed
- Mounted queries auto-refetch
- Simple and reliable - no stale data issues
- DataChunk still cached for efficiency (immutable content)

### Why b64 key as canonical identifier?
- Keys are globally unique (unlike shortId which is just for CLI convenience)
- Keys can be serialized and persisted (e.g., in URL params, localStorage)
- Backend already supports key-based queries
- keyJSON isn't always available (non-JSON keys return null)

### Why keep shortId in UI?
- Compact display (#42 vs. long base64 strings)
- Human-readable for debugging
- Already used throughout the UI

## Implementation Steps

As you go, you should remind yourself to take short breaks between steps to re-orient and track your state:
- Check off each step immediately after finishing the step. Don't check it off before starting work, wait until after. But also remember to check it off before moving on to the next step.
- Then, right after finishing each step and checking it off, go to `plans/phase2_step10p1_hook_interface_impl_notes.md`, and add an implementation notes section for this step describing anything weird you ran into, any ambiguous situations you had to make a judgement call about, any work left for later, or any things you ran into that you think deserve a closer look or deserve following up on. You can use `plans/impl_notes_template.md` as a template. The purpose of this file is to make sure important loose ends don't get lost. If you don't have any particular notable findings to write here, feel free to just comment briefly on your experience doing that step, and whether you felt like it went well or was tricky.
- Then look at the next step, make sure you understand what you need to do, and mark it as "in progress".

### Phase 1: Backend Schema Changes ✅
- [x] Add `thingstateId` field to Thing type (for client-side change detection)
- [x] Ensure thingstateId increments when links change (not just data) - add comment to SQL schema
- [x] Remove `triggerable` field from Thing type
- [x] Add `triggerableKeys` query to schema.graphql
- [x] Add `NewTickPayload` type with tick + triggerableKeys
- [x] Change `newTick` subscription to return `NewTickPayload`
- [x] Add resolver for thingstateId on Thing
- [x] Add resolver for triggerableKeys query
- [x] Update newTick subscription resolver to return NewTickPayload
- [x] Keep `changes(since:)` field on Tick type (server-side change detection retained)
- [x] Update tests to use thingstateId instead of triggerable

(Implementation note: Discovered issues with the thingstateId invariant, fixed in the separate plan doc plans/phase2_step10p1p1_fix_data_chunks.md.)

### Phase 2: Frontend Cache Configuration ✅
- [x] Update graphcache config: Thing returns null key (not cached by ID)
- [x] Add graphcache `updates.Subscription.newTick` handler
- [x] Implement cache.updateQuery for latestTick, triggerableKeys, ticks list
- [x] Implement cache.invalidate for tick data queries
- [x] Define virtual queries for cache updates (used existing GET_LATEST_TICK, GET_TICKS, GET_TRIGGERABLE_KEYS)
- [x] Verify DataChunk still cached by ID

### Phase 3: Create ThingWatcherProvider ✅
- [x] Create `ThingWatcherProvider.tsx` with context for tick state
- [x] Subscribe to newTick (graphcache handler does the cache updates)
- [x] Expose tick state and actions via context (currentTick, resolvedTick, isLive, setCurrentTick)
- [x] Implement `computeChanges` utility for client-side change detection
- [x] Support filtered tick changes (changes relative to previous visible tick) - deferred to useTickData hook
- [x] ~~Add `useTriggerableKeys` hook~~ - REMOVED: components use `useThingWatcher()` directly to get `triggerableKeys` and `isLive`

### Phase 4: Create Data Hooks ✅
- [x] Create `hooks/useThing.ts` - single thing by key
- [x] ~~Create separate tick hook files~~ - CHANGED: combined into `hooks/ticks.ts` (useTickData, useTicks, useLatestTick, useComputedChanges)
- [x] ~~Create `hooks/useTrigger.ts`~~ - CHANGED: split into `useTriggerThing(keyInput)` and `useTriggerTick()` returning `[triggering, trigger]` tuples
- [x] Create `hooks/useTriggerRecords.ts` - trigger history (uses server-side filtering, removed trivial helper hooks)
- [x] ~~Create `hooks/useRenderingConfig.ts`~~ - CHANGED: renamed to `useThingTypeConfig(thingType)` - queries single type via new `thingTypeConfig` query
- [x] Create `hooks/index.ts` - re-exports

**Additional schema changes made during Phase 4:**
- Added `thingTypeConfig(thingType: String!): ThingTypeConfig` query
- Removed `RenderingConfig` wrapper type - `renderingConfig` now returns `[ThingTypeConfig!]!` directly

### Phase 5: Update GraphQL Queries ✅
- [x] Remove shortId from ThingKeyInput - only allow key or keyJSON
- [x] Remove triggerable from Thing queries (done in Phase 2)
- [x] ~~Remove changes(since:) from tick queries~~ - NOT DOING: kept server-side change detection
- [x] Add triggerableKeys query (done in Phase 2)
- [x] Update newTick subscription to expect NewTickPayload (done in Phase 2)

### Phase 6: Update AppContext ✅
- [x] Remove tick state (now in ThingWatcherProvider)
- [x] Change `selectedThingId` to `selectedThingKey`
- [x] Update setSelectedThing to accept key

### Phase 7: Migrate Components ✅
- [x] Wrap app in ThingWatcherProvider
- [x] Update App.tsx to use new hooks (remove manual refresh logic)
- [x] Update ThingViewPanel.tsx to store/use key
- [x] Update EmbedApp.tsx - see Phase 8 / query_cleanup plan for full migration to hooks
- [x] Update DetailsPanel.tsx - get triggerable from useThingWatcher
- [x] Update DetailsPanelWrapper.tsx selection handling
- [x] Update Graph.tsx event handling (onSelectThing with key)
- [x] Update ThingsList.tsx - display triggerable from useThingWatcher
- [x] Update RootsPanel.tsx event handling
- [x] Update ThingContextMenu to use key

### Phase 8: Cleanup ✅

See [phase2_step10p1p3_query_cleanup.md](phase2_step10p1p3_query_cleanup.md) for detailed cleanup plan.

Summary:
- [x] Enhance `useThing` with `includeData` option for full chunk data
- [x] Remove `renderingConfig` from `useTickData` (use `useThingTypeConfig` instead)
- [x] Remove `renderers` map from RendererContext (`ThingView` uses `useThingTypeConfig` directly)
- [x] Clean up ThingWatcherBindings.tsx inline queries
- [x] Delete `useChunkResolver.ts`
- [x] Migrate App.tsx to use hooks (useTicks, useTriggerRecords, useTickData, useThing)
- [x] Migrate ThingViewPanel.tsx to use hooks
- [x] Migrate EmbedApp.tsx to use ThingWatcherProvider and hooks
- [x] Move queries from queries.ts into hook files, remove queries.ts
- [x] Verify shortId is only used for display
- [x] Update TypeScript types (Thing without triggerable)
- [x] Update LOCAL_CODE_OVERVIEW.md
- [x] Update CODE_OVERVIEW.md (frontend changes reflected in LOCAL_CODE_OVERVIEW)

## Testing Considerations

- Verify thing selection works with keys instead of shortIds
- Verify panel opening uses keys
- Verify trigger functionality works with keys
- Verify EmbedApp continues to work with postMessage
- Verify custom renderers receive correct data
- Verify DataChunk caching works (same chunk ID = no refetch)
- Verify triggerableKeys updates on new tick
- Verify cache invalidation works correctly in live mode

## Design Notes

1. **Panel IDs**: Use a separate integer counter for panel IDs (e.g., `thing-view-1`, `thing-view-2`), with the full b64 key stored in panel params. This keeps panel IDs short and readable while properly identifying the thing.

2. **URL deep links**: If we want to support URLs like `/thing/{key}`, the key works well since it's URL-safe base64.

3. **ThingContextMenu**: Pass key instead of shortId for "Open in panel" action.

4. **Selection synchronization**: When clicking a node in Graph or item in ThingsList, extract key from the thing object for selection.

5. **Triggerable display**: Components that show triggerable status (ThingsList badges, DetailsPanel trigger form) get it from `useTriggerableKeys()` and check if the thing's key is in the set.

6. **EmbedApp**: Always shows current tick, so triggerableKeys is always relevant. No special handling needed.
