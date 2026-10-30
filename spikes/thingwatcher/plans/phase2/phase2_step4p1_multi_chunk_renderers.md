# Phase 2 Step 4.1: Multi-Chunk Data in Renderers

Custom renderers currently only receive the first data chunk. This step adds support for multiple data chunks with client-side caching to efficiently handle incremental updates.

## Problem

Things can have multiple data chunks that form a chain (via `extends_chunk_id`). Currently:
- `ThingRendererProps.payload` only contains the first chunk's data
- Renderers have no way to access the full chunk history
- Each re-render would naively re-fetch all chunk data

## Solution Overview

1. Expose `dataChunks` to renderers alongside `payload`
2. Use urql's graphcache with a two-query strategy for efficient chunk caching
3. Create a demo showing incremental chunk accumulation via ThingDelta

## Design

### Two-Query Caching Strategy

Use `@urql/exchange-graphcache` with a two-query approach:

**Query 1: Get chunk ID manifest (always fresh)**
```graphql
query GetThingDetails($shortId: Int!, $tick: Int!) {
  thing(shortId: $shortId, atTick: $tick) {
    shortId
    key
    keyJSON
    thingType
    triggerable
    data
    dataChunks {
      id          # Just IDs, lightweight
    }
    links { ... }
  }
}
```

**Query 2: Get individual chunk data (cached by graphcache)**
```graphql
query GetDataChunk($id: Int!) {
  dataChunk(id: $id) {
    id
    data
  }
}
```

### How It Works

1. Fetch thing details including `dataChunks { id }` - returns list of chunk IDs
2. For each chunk ID, query `dataChunk(id: $id)`
3. graphcache automatically caches `DataChunk` entities by ID
4. Subsequent requests for same chunk ID → served from cache
5. New chunks (from ThingDelta appends) → only new IDs hit network

```
First render of thing with 5 chunks:
  - Query 1: GET_THING_DETAILS → [id:1, id:2, id:3, id:4, id:5]
  - Query 2: GET_DATA_CHUNK(1), (2), (3), (4), (5) → 5 network requests
  - All 5 chunks now in graphcache

Thing gets new data via ThingDelta(append):
  - Query 1: GET_THING_DETAILS → [id:1, id:2, id:3, id:4, id:5, id:6]
  - Query 2: GET_DATA_CHUNK(1..5) → cache hits, (6) → 1 network request
  - Only chunk 6 fetched from network
```

### graphcache Configuration

```typescript
import { cacheExchange } from '@urql/exchange-graphcache';

const cache = cacheExchange({
  keys: {
    // DataChunk entities keyed by 'id'
    DataChunk: (data) => data.id?.toString() ?? null,
    // Other types that don't have natural keys
    Tick: () => null,
    Link: () => null,
  },
});
```

### ThingRendererProps Changes

```typescript
interface ThingRendererProps {
  payload: unknown;              // First chunk data (single-chunk things)
  dataChunks: unknown[];         // All chunk data in order, oldest first (multi-chunk things)
  links: LinkInfo[];
  triggerable: boolean;
  trigger: (payload: unknown | null) => Promise<TriggerResult>;
  args: Record<string, unknown>;
}
```

**Usage pattern**: Renderers typically use one or the other, not both:
- `payload` - for simple things with a single data chunk (most common)
- `dataChunks` - for things that accumulate data over time via ThingDelta appends

The chunk IDs are an internal implementation detail for caching and are not exposed to renderers.

### Chunk Resolution Hook

```typescript
// hooks/useChunkResolver.ts
import { useClient } from 'urql';

const GET_DATA_CHUNK = gql`
  query GetDataChunk($id: Int!) {
    dataChunk(id: $id) {
      id
      data
    }
  }
`;

/**
 * Resolves chunk IDs to their data.
 * graphcache automatically caches DataChunk entities by ID,
 * so repeated calls for the same chunk ID are cache hits.
 */
export function useChunkResolver() {
  const client = useClient();

  async function resolveChunks(chunkIds: number[]): Promise<unknown[]> {
    // Fetch all chunks in parallel - graphcache handles caching
    const results = await Promise.all(
      chunkIds.map(id =>
        client.query(GET_DATA_CHUNK, { id }).toPromise()
      )
    );

    // Extract just the data (IDs are internal implementation detail)
    return results.map(result => result.data?.dataChunk?.data ?? null);
  }

  return { resolveChunks };
}
```

### Demo: Incremental Log Accumulator

Create `examples/demo_multi_chunk.py` showing:
- A "LogAccumulator" thing that adds log entries over time
- Uses ThingDelta with `data_diff_kind="append"` to extend the data chain
- Custom renderer that displays all log entries with timestamps

```python
from thingwatcher import ThingState, ThingDelta

class LogAccumulator:
    def __init__(self, name: str):
        self.name = name
        self.entries: list[dict] = []

    def add_entry(self, message: str):
        self.entries.append({
            "timestamp": time.time(),
            "message": message
        })

class LogAccumulatorSnapshotter:
    """Snapshotter that tracks state and emits deltas for new entries."""

    def __init__(self):
        # Track last seen entry count per object
        self._last_count: dict[int, int] = {}

    def snapshot(self, obj: LogAccumulator) -> ThingState | ThingDelta:
        obj_id = id(obj)
        last_count = self._last_count.get(obj_id, 0)
        current_count = len(obj.entries)

        if last_count == 0:
            # First snapshot: emit full ThingState
            self._last_count[obj_id] = current_count
            return ThingState(
                key=("log", obj.name),
                thing_type="LogAccumulator",
                thing_data={"entries": obj.entries},
                links=[],
            )
        elif current_count > last_count:
            # New entries: emit ThingDelta with append
            new_entries = obj.entries[last_count:]
            self._last_count[obj_id] = current_count
            return ThingDelta(
                key=("log", obj.name),
                data_diff_kind="append",
                new_data={"entries": new_entries},  # Only new entries
                added_links=[],
                removed_link_keys=[],
            )
        else:
            # No changes
            return ThingDelta(
                key=("log", obj.name),
                data_diff_kind="no_diff",
                new_data=None,
                added_links=[],
                removed_link_keys=[],
            )
```

The renderer would show:
```tsx
// LogAccumulator uses dataChunks because it accumulates entries over time.
// Simple single-chunk things would use `payload` instead.
export default function LogRenderer({ dataChunks }) {
  // Flatten all entries from all chunks (each chunk has {entries: [...]})
  const allEntries = dataChunks.flatMap(chunk => chunk?.entries || []);

  return (
    <div className="log-viewer">
      <div className="log-header">
        {allEntries.length} entries across {dataChunks.length} chunk(s)
      </div>
      {allEntries.map((entry, i) => (
        <div key={i} className="log-entry">
          <span className="timestamp">{formatTime(entry.timestamp)}</span>
          <span className="message">{entry.message}</span>
        </div>
      ))}
    </div>
  );
}
```

## Implementation Steps

### Phase A: Switch to graphcache

- [x] **A1.** Install `@urql/exchange-graphcache` package
- [x] **A2.** Update `useGraphQL.ts` to use graphcache with entity keys config
- [x] **A3.** Add `GET_DATA_CHUNK` query for fetching individual chunks

### Phase B: GraphQL & Types

- [x] **B1.** Update `GET_THING_DETAILS` to include `dataChunks { id }` (IDs only)
- [x] **B2.** Update `ThingRendererProps` in ThingWatcherBindings.tsx to include `dataChunks: unknown[]`

### Phase C: Chunk Resolution

- [x] **C1.** Create `hooks/useChunkResolver.ts` hook that:
  - Takes array of chunk IDs
  - Queries each via `GET_DATA_CHUNK` (graphcache handles caching)
  - Returns resolved chunk data array
- [x] **C2.** Integrate chunk resolver into DetailsPanel
- [x] **C3.** Integrate chunk resolver into ThingWatcherBindings (for ThingView)

### Phase D: Demo & Testing

- [x] **D1.** Create `examples/demo_multi_chunk.py` with LogAccumulator using ThingDelta
- [x] **D2.** Add LogAccumulator renderer showing chunk history
- [x] **D3.** Test incremental chunk accumulation
- [x] **D4.** Verify cache behavior in browser devtools (no duplicate fetches)

### Phase E: Documentation

- [x] **E1.** Update CODE_OVERVIEW.md with chunk caching info
- [x] **E2.** Update frontend/LOCAL_CODE_OVERVIEW.md with graphcache setup
- [x] **E3.** Add comments to demo explaining ThingDelta pattern

## Notes

- Chunk IDs are globally unique and immutable - graphcache handles caching automatically
- `payload` remains as first-chunk data for backwards compatibility
- Renderers that don't need history can ignore `dataChunks`
- graphcache stores entities in memory; page refresh clears cache (acceptable)

## Open Questions

1. Should we expose a helper in ThingWatcherBindings for common chunk operations?
   - e.g., `flattenChunks(dataChunks, 'entries')` that concatenates array fields
   - Decision: Start simple, add helpers if patterns emerge

2. Parallel vs sequential chunk fetches?
   - graphcache should handle this efficiently, but we could batch with `Promise.all`
   - Decision: Use `Promise.all` for parallel fetches, measure if needed

## Phase F: Bug Fixes (Post-Implementation)

### Issue 1: Maximum update depth exceeded

**Symptom:** Console shows "Maximum update depth exceeded" error, infinite re-render loop.

**Root cause:** In `useChunkResolver`, the `resolveChunks` function is created fresh on every render (not memoized). When used as a useEffect dependency in DetailsPanel:

```javascript
const { resolveChunks } = useChunkResolver();

useEffect(() => {
  // ...
  resolveChunks(chunkIds).then(...);
}, [thing?.dataChunks, resolveChunks]); // resolveChunks changes every render!
```

The effect runs → calls `setResolvedChunks` → triggers re-render → new `resolveChunks` reference → effect runs again → infinite loop.

**Fix:** Memoize `resolveChunks` with `useCallback` in `useChunkResolver`:

```typescript
export function useChunkResolver() {
  const client = useClient();

  const resolveChunks = useCallback(async (chunkIds: number[]): Promise<unknown[]> => {
    // ... implementation
  }, [client]); // client is stable

  return { resolveChunks };
}
```

### Issue 2: Details panel flashes on each tick

**Symptom:** Entire DetailsPanel visually flashes/resets on each new tick. Scrollbars reset to top.

**Root causes:**

1. **Infinite loop from Issue 1** causes rapid state changes that may cause React to unmount/remount.

2. **Array reference instability:** `thing?.dataChunks` is a new array on every GraphQL response, even if the chunk IDs are the same. This triggers the useEffect even when chunks haven't changed.

3. **Loading state during re-fetch:** Currently we set `chunksLoading = true` on every re-fetch, which could cause conditional rendering to flash loading states.

**Fixes:**

1. Fix Issue 1 first (memoize `resolveChunks`).

2. **Stabilize chunk ID comparison:** Instead of depending on `thing?.dataChunks` array, depend on a stable representation of the chunk IDs:

```typescript
// Create stable string key from chunk IDs
const chunkIdsKey = useMemo(() =>
  thing?.dataChunks?.map(c => c.id).join(',') ?? '',
  [thing?.dataChunks]
);

useEffect(() => {
  // ... resolve chunks
}, [chunkIdsKey, resolveChunks]); // Now only triggers when IDs actually change
```

3. **Keep old data while loading:** Don't clear `resolvedChunks` or show loading state if we already have data. Only show loading on initial load:

```typescript
const [resolvedChunks, setResolvedChunks] = useState<unknown[]>([]);
const [initialLoading, setInitialLoading] = useState(true);

useEffect(() => {
  // Don't clear existing chunks while loading new ones
  // Just fetch and update when ready
  resolveChunks(chunkIds).then((chunks) => {
    setResolvedChunks(chunks);
    setInitialLoading(false);
  });
}, [chunkIdsKey, resolveChunks]);
```

### Implementation Steps

- [x] **F1.** Add `useCallback` to memoize `resolveChunks` in `useChunkResolver.ts`
- [x] **F2.** Update DetailsPanel to use stable chunk ID key for useEffect dependency
- [x] **F3.** Update DetailsPanel to not flash loading state on re-fetch
- [x] **F4.** Apply same fixes to ThingWatcherBindings (ThingView component)
- [x] **F5.** Test that panel no longer flashes and scrollbars persist

---

### Issue 3: Loading flash from parent query (still present after F1-F4)

**Symptom:** Panel still flashes on each tick even after chunk loading fixes.

**Root cause:** The loading state comes from TWO sources:
1. ~~Chunk resolution (fixed in F1-F4)~~
2. **Parent GraphQL query `fetching` state** - this is the remaining issue

**Investigation findings:**

The flash happens because `urql`'s `fetching` property is `true` during ANY network request, including re-fetches when cached data already exists.

**Where this is set:**

1. **App.tsx (line 247):**
   ```tsx
   detailsLoading={thingDetailsResult.fetching}
   ```
   This is passed to DockviewLayout → DetailsPanelWrapper → DetailsPanel.
   When tick changes, `GET_THING_DETAILS` re-fetches, `fetching` becomes true.

2. **ThingViewPanel.tsx (line 88):**
   ```tsx
   loading={thingResult.fetching}
   ```
   Same pattern - re-fetch on tick change causes `fetching=true`.

3. **DetailsPanel.tsx (lines 146-157):**
   ```tsx
   if (loading) {
     return (
       <div className={styles.panel}>
         <div className={styles.loading}>Loading...</div>
       </div>
     );
   }
   ```
   When `loading` is true, the ENTIRE panel is replaced with loading message.

**The problem:**
- Initial load: `fetching=true, data=null` → want loading
- Re-fetch (have cached data): `fetching=true, data={...}` → DON'T want loading
- Currently both show loading because we only check `fetching`

**Fix:**

Change loading condition from `fetching` to `fetching && !data`:

```tsx
// App.tsx
detailsLoading={thingDetailsResult.fetching && !thingDetailsResult.data?.thing}

// ThingViewPanel.tsx
loading={thingResult.fetching && !thingResult.data?.thing}
```

This ensures loading only shows on initial load, not on re-fetches.

### Implementation Steps (continued)

- [x] **F6.** Update App.tsx: `detailsLoading={thingDetailsResult.fetching && !thingDetailsResult.data?.thing}`
- [x] **F7.** Update ThingViewPanel.tsx: `loading={thingResult.fetching && !thingResult.data?.thing}`
- [x] **F8.** Test that panel no longer flashes on tick changes
