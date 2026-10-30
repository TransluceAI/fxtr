# Phase 2 Step 21: Immutable Object Stores

## Overview

Add "immutable object stores" as a concept to thingwatcher. An immutable store is a read-time extension that provides lazy access to things that are assumed to be constant over time (e.g., content-addressed IPLD blobs, git commits). These things are not captured during ticks but can be queried on-demand and displayed in the UI alongside captured things.

**Key properties:**
- Things in stores are assumed immutable (no temporal tracking needed)
- Store lookup happens at **query time** (GraphQL resolution), not during tick capture
- Snapshot things **override** store things if the same key exists in both
- Store-to-store conflicts require identical states (like stateless snapshotter unification)
- No iteration/enumeration - stores are purely key-based lookup
- **Stores are async** - methods can be async to support disk/network-backed stores

**Primary use case:** Visualizing content-addressed data (IPLD-style CBOR blobs, git commits, provenance graphs) where active things are like GC roots/branch heads, and you follow links backward to explore history.

## Python-Side Design

### Store Protocol

```python
from typing import Protocol, Any
from thingwatcher.api import ThingState

@dataclass
class BacklinkInfo:
    """Information about a backlink from a thing in an immutable store."""
    from_thing_key: Any  # Key of the thing that has this link
    link_type: str
    link_key: Any

class ImmutableStore(Protocol):
    """Protocol for immutable object stores.

    Stores provide lazy access to things that are assumed constant over time.
    Things are looked up by key at query time, not captured during ticks.

    All methods are async to support disk/network-backed stores.
    """

    async def get_thing_state(self, key: Any) -> ThingState | None:
        """Look up a thing by its key.

        Args:
            key: The thing key (CBOR-serializable)

        Returns:
            ThingState if found, None if not in this store.
            The returned ThingState should have single-chunk data (no multi-chunk).
        """
        ...

    async def get_backlinks(self, key: Any) -> list[BacklinkInfo]:
        """Get backlinks to a thing from other things in this store.

        The contract:
        - Not required to return anything (default: empty list)
        - If returning backlinks, the SOURCE of each backlink must be in this store
        - Used to find things in the store that link to a given key

        Args:
            key: The thing key to find backlinks for

        Returns:
            List of BacklinkInfo for things in this store that link to the given key.
        """
        return []
```

### Registration API

```python
# In thingwatcher/api.py

def register_immutable_store(name: str, store: ImmutableStore) -> None:
    """Register an immutable object store with the active watcher.

    Args:
        name: Unique name for this store (shown in UI as source)
        store: The store instance implementing ImmutableStore protocol

    Raises:
        NoWatcherConnected: If no watcher is currently active
        ValueError: If a store with this name is already registered
    """
    ...

def unregister_immutable_store(name: str) -> None:
    """Unregister an immutable object store.

    Args:
        name: Name of the store to unregister

    Raises:
        NoWatcherConnected: If no watcher is currently active
        KeyError: If no store with this name exists
    """
    ...
```

### Watcher Integration

The `Watcher` class gets:
- `_immutable_stores: dict[str, ImmutableStore]` - registered stores
- `register_immutable_store(name, store)` - instance method
- `unregister_immutable_store(name)` - instance method
- `lookup_in_stores(key_bytes) -> list[tuple[str, ThingState]]` - query all stores for a key
- `get_store_backlinks(key_bytes) -> list[tuple[str, BacklinkInfo]]` - query all stores for backlinks

## GraphQL Schema Changes

### New Types

```graphql
enum ThingSourceKind {
  SNAPSHOT    # Thing captured in a tick snapshot
  STORE       # Thing retrieved from an immutable store
}

type ThingSource {
  kind: ThingSourceKind!
  storeName: String    # Present when kind=STORE
  overridden: Boolean! # True if this source's state differs from the resolved state
}
```

### Modified Thing Type

```graphql
type Thing {
  # ... existing fields ...

  # Where this thing came from (can have multiple sources)
  # First source is always the "winning" source that provides the resolved state.
  # Additional sources may be overridden (their state differs from resolved).
  sources: [ThingSource!]!
}
```

### Resolution Logic

For `thing(key, atTick)` queries:

```python
async def resolve_thing(key, at_tick):
    # 1. Check snapshot at the requested tick
    db_thing = await db.get_thing_at_tick(key, at_tick)

    # 2. Query all immutable stores (async)
    store_results = []  # list of (store_name, ThingState)
    for name, store in watcher.immutable_stores.items():
        state = await store.get_thing_state(key)
        if state is not None:
            store_results.append((name, state))

    # 3. Determine resolved state and sources
    if db_thing:
        # Snapshot wins - it provides the resolved state
        resolved_state = db_thing
        sources = [ThingSource(kind="SNAPSHOT", overridden=False)]

        # Check if stores have the same state or are overridden
        for name, store_state in store_results:
            is_same = _thing_states_equal(resolved_state, store_state)
            sources.append(ThingSource(
                kind="STORE",
                storeName=name,
                overridden=not is_same
            ))

        return Thing(...db_thing, sources=sources)

    if not store_results:
        return None

    # 4. No snapshot - verify all store results are identical (stateless conflict check)
    first_state = store_results[0][1]
    for name, state in store_results[1:]:
        if not _thing_states_equal(first_state, state):
            # Conflict - return broken thing with error info
            return make_broken_thing(key, "Store conflict: inconsistent states")

    # 5. Return thing with all store sources (none overridden since they're identical)
    return Thing(
        ...from_thing_state(first_state),
        sources=[ThingSource(kind="STORE", storeName=name, overridden=False)
                 for name, _ in store_results]
    )
```

### Backlinks Resolution

For the `backlinks` field on `Thing`:

```python
async def resolve_backlinks(thing, at_tick):
    key_bytes = thing.key_bytes

    # 1. Get backlinks from DB (captured things linking to this key)
    db_backlinks = await db.get_backlinks_at_tick(key_bytes, at_tick)

    # 2. Get backlinks from all immutable stores
    store_backlinks = []
    for name, store in watcher.immutable_stores.items():
        for backlink_info in await store.get_backlinks(key_bytes):
            # backlink_info has: from_thing_key, link_type, link_key
            store_backlinks.append((name, backlink_info))

    # 3. Combine results
    # DB backlinks are already Link objects
    # Store backlinks need to be converted to Link objects
    # For each store backlink, resolve the from_thing using the unified resolver
    all_links = list(db_backlinks)
    for name, backlink_info in store_backlinks:
        # Note: The from_thing will be resolved when Link.fromThing is accessed
        all_links.append(Link(
            link_type=backlink_info.link_type,
            link_key=backlink_info.link_key,
            from_thing_key=backlink_info.from_thing_key,
            to_thing_key=key_bytes,  # The thing we're getting backlinks for
        ))

    return all_links
```

### Unified Thing Resolution Helper

To reduce complexity across resolvers, we create a single helper that handles DB-then-store lookup:

```python
async def resolve_thing_by_key(
    watcher: Watcher,
    db: Database,
    key_bytes: bytes,
    at_tick: int
) -> dict | None:
    """Resolve a thing by key, checking DB first then stores.

    Returns a dict with thing data + sources list, or None if not found.
    This helper is used by:
    - thing() query
    - Link.toThing resolver
    - Link.fromThing resolver
    - Root.thing resolver
    - Any other place that needs to resolve a thing by key
    """
    # Check DB first
    db_thing = await db.get_thing_at_tick(key_bytes, at_tick)

    # Query all stores
    store_results = await watcher.lookup_in_stores(key_bytes)

    # ... same logic as in Resolution Logic section ...
```

This eliminates duplication and ensures consistent behavior across all resolvers.

## Frontend Design

### Unified Active Things System

Instead of tracking "active immutable things" separately, we use a **unified active things system** with ref-counting. Any part of the app can mark a thing as active, and it stays active as long as at least one mark is held.

```typescript
// hooks/useActiveThings.tsx

interface ActiveThingsContext {
  // Set of b64 keys for all currently active things
  activeKeys: Set<string>;

  // Mark a thing as active. Returns an unmark function.
  // Multiple callers can mark the same thing; it stays active until all unmark.
  markActive(key: string): () => void;

  // Check if a thing is currently active (has any marks)
  isActive(key: string): boolean;
}
```

**Sources of marks:**
1. **Panels**: Each panel marks its thing on mount, unmarks on unmount
2. **Pins**: Pinning marks the thing, unpinning unmarks it
3. **Current tick snapshot**: All things captured at the current tick are marked (changes when tick changes)
4. **Selection**: The currently selected thing is marked (changes when selection changes)

This unifies the model - whether a thing is "immutable" or "snapshot" is about where data comes from, not how we track activity.

### Pinning System

Pinning is a **separate context** that provides persistent marks:

```typescript
// hooks/usePinnedThings.tsx

interface PinnedThingsContext {
  pinnedKeys: Set<string>;
  togglePin(key: string): void;
  isPinned(key: string): boolean;
}
```

**Pinning behavior:**
- Pinning a thing adds it to the pinned set AND calls `markActive(key)` (storing the unmark fn)
- Unpinning removes from pinned set AND calls the stored unmark fn
- Pinned things appear in a **separate region at the top of ThingsList**
- Pinning works for ANY thing (snapshot or immutable) - useful for keeping important things visible
- For immutable things, pinning also ensures they stay loaded even if no panels are open
- Pin state is session-only (not persisted to localStorage)

### Panel Behavior for Disappeared Things

When a panel is open for a thing that doesn't exist at the current tick:
- The key stays in the active set (panel holds its mark)
- Panel shows "Not present at tick N" message instead of content
- If user navigates to a tick where the thing exists, content reappears
- This handles both: navigating away from a snapshot thing's valid ticks, and edge cases with immutable things

### ThingsList Changes

ThingsList shows things in sections:

1. **Pinned section** (at top, if any pinned things exist):
   - All pinned things, regardless of source
   - Visual indicator for pin status
   - Things that don't exist at current tick shown as "unavailable" (grayed out, "not present" marker)

2. **Active section** (main list):
   - All things captured at the current tick
   - Plus any other active things that exist at current tick (e.g., immutable things with open panels)
   - Things active but not present at current tick are **hidden** from this section (only visible in pinned section if pinned)
   - Existing filtering/sorting applies

### Source Display in Panels

`ThingPanelContent` shows source info above the thing content:

```
┌─────────────────────────────────────────┐
│ 📍 Captured in snapshot           [📌]  │  ← snapshot source, pin button
│ ─────────────────────────────────────── │
│ [Thing content...]                      │
└─────────────────────────────────────────┘

┌─────────────────────────────────────────┐
│ 📦 From store: ipld_store         [📌]  │  ← store source, pin button
│ ─────────────────────────────────────── │
│ [Thing content...]                      │
└─────────────────────────────────────────┘

┌─────────────────────────────────────────┐
│ 📍 Captured in snapshot           [📌]  │  ← snapshot wins
│ 📦 Also in: ipld_store (overridden)     │  ← store has different state
│ ─────────────────────────────────────── │
│ [Thing content...]                      │
└─────────────────────────────────────────┘

┌─────────────────────────────────────────┐
│ 📍 Captured in snapshot           [📌]  │  ← snapshot and store match
│ 📦 Also in: ipld_store                  │  ← not marked as overridden
│ ─────────────────────────────────────── │
│ [Thing content...]                      │
└─────────────────────────────────────────┘

┌─────────────────────────────────────────┐
│ ⚠️ Not present at tick 42         [📌]  │  ← thing doesn't exist here
│ ─────────────────────────────────────── │
│ (No content available)                  │
└─────────────────────────────────────────┘
```

The pin button `[📌]` toggles pin state. When pinned, the icon is filled/highlighted.

### Graph Changes

- Edges to **inactive** things do NOT render (the target node doesn't exist)
- **Active** things (from any source) appear as nodes in the graph
- Things that are active but don't exist at current tick: **hidden** from graph (no ghost nodes)
- Active immutable things participate in layout like any other node
- No special visual distinction for immutable vs snapshot things in the graph

### Data Flow

1. User clicks a NodeRef/link pointing to an immutable thing
2. Frontend queries GraphQL for `thing(key: "...")`
3. Backend checks DB (not found), then queries stores
4. Returns Thing with `sources: [{kind: STORE, storeName: "ipld_store", overridden: false}]`
5. Frontend:
   - Opens panel with the thing
   - Panel mounts and calls `markActive(key)` (stores unmark fn)
   - Thing now appears in ThingsList and Graph (since it's active)
6. User closes panel:
   - Panel unmounts and calls its stored unmark fn
   - If pinned: thing stays active (pin's mark still held)
   - If not pinned: no more marks, removed from active set, disappears from graph/list

## Implementation Steps

### Backend (Python)

- [x] 1. Add `BacklinkInfo` dataclass to `api.py`
- [x] 2. Add `ImmutableStore` protocol to `api.py` (async methods)
- [x] 3. Add `register_immutable_store` and `unregister_immutable_store` to `api.py` (delegating to context)
- [x] 4. Add store management to `Watcher` class:
  - `_immutable_stores: dict[str, ImmutableStore]`
  - `register_immutable_store()` / `unregister_immutable_store()` methods
  - `async lookup_in_stores(key_bytes) -> list[tuple[str, ThingState]]` helper
  - `async get_store_backlinks(key_bytes) -> list[tuple[str, BacklinkInfo]]` helper
- [x] 5. Update GraphQL schema:
  - Add `ThingSourceKind` enum
  - Add `ThingSource` type with `kind`, `storeName`, `overridden`
  - Add `sources` field to `Thing`
- [x] 6. Refactor GraphQL resolvers - create unified thing resolution helper:
  - `async resolve_thing_by_key(key_bytes, at_tick) -> ThingWithSources | None`
  - This helper checks DB first, then stores, handles conflicts, builds sources list
  - Used by: `thing()` query, `Link.toThing`, `Link.fromThing`, `Root.thing`, etc.
- [x] 7. Update `Thing.backlinks` resolver to include store backlinks:
  - Query DB for captured things linking to this key
  - Query all stores via `get_store_backlinks()`
  - Combine and dedupe results
- [x] 8. Write tests for immutable store functionality

### Frontend (TypeScript)

- [x] 9. Update GraphQL queries/types:
  - Add `sources` field to Thing queries
  - Update `types/index.ts`: `Thing.thingstateId: number` → `string`, `DataChunk.id: number` → `string`
- [x] 10. Create `ActiveThingsContext` with ref-counted mark/unmark system:
  - `markActive(key) -> unmarkFn`
  - `activeKeys: Set<string>`
  - Internal ref-counting per key
- [x] 11. Create `PinnedThingsContext`:
  - `pinnedKeys: Set<string>`
  - `togglePin(key)`, `isPinned(key)`
  - Integrates with ActiveThingsContext (pin = mark, unpin = unmark)
- [x] 12. Update snapshot loading to mark captured things as active:
  - When tick data loads, mark all captured thing keys
  - When tick changes, unmark old keys and mark new keys
- [x] 13. Update `ThingPanelContent` to show source info and pin button:
  - Display sources list with appropriate icons
  - Show "overridden" indicator for overridden store sources
  - Show "Not present at tick N" for unavailable things
  - Pin toggle button
- [x] 14. Update `ThingViewPanel` to participate in active set:
  - Call `markActive(key)` on mount, store unmark fn
  - Call unmark on unmount
- [x] 15. Update `ThingsList`:
  - Add pinned section at top (if any pinned things)
  - Show active things (captured + others with marks)
  - Visual distinction for unavailable things
- [x] 16. Update Graph to show active things as nodes:
  - Note: Graph continues to show snapshot things; additional active things from stores would need individual queries (deferred)
- [x] 17. Update CODE_OVERVIEW.md

### Demo/Testing

- [x] 18. Create `examples/demo_immutable_store/` with a simple async in-memory store demo:
  - A few hardcoded immutable things
  - Demo thing that links to an immutable thing
  - Shows following links into the store
  - Shows backlinks from store things
  - Demonstrates pinning behavior

## Open Questions / Future Work

1. **Edges to inactive things**: Currently we hide them. Could later add "ghost edges" pointing to expandable placeholders.

2. **Store inspection**: No way to list what's in a store. Could add optional `describe()` method for debugging.

3. **Link resolution performance**: Resolving `Link.toThing` may trigger store lookups. For things with many links, this could be slow. May need batching/caching strategies later.

4. **EmbedApp**: The embed app shows refs to things but doesn't have ThingsList/Graph. Just needs to handle loading immutable things in panels normally - no special "active set" logic needed since there's only ever one thing shown.

## Resolved Design Decisions

- **Sources field scope**: Only add `sources` to `GET_THING` query (for panel display), not to `GET_TICK_DATA` (list queries don't need it)
- **Pinned section behavior**: Show all pinned things; if a pinned thing fails to resolve, show a fallback UI (not grayed out, just "not found" state)
- **Graph/ThingsList non-pinned**: Only show things that successfully resolve; if a thing can't be loaded, don't show it
- **Context placement**: `ActiveThingsContext` and `PinnedThingsContext` provided at App level, inside `ThingWatcherProvider` but wrapping `AppProvider`
- **Pin button location**: Primary location in ThingPanelContent header (source info area). Nice-to-have: also in ThingsList items as a small button on the right (revisit at end)
- **Pin icons**: Use lucide `Pin` (filled when pinned) vs `PinOff` (when not pinned)

- **Visual distinction for immutable things**: No special visual treatment in graph - source info in panels is sufficient
- **Store errors**: Exceptions during `get_thing_state()` produce broken things with error info
- **Things unavailable at current tick**: Hidden from graph and main ThingsList; pinned things shown grayed out in pinned section with "not present" marker

## Implementation Notes (Backend)

Things encountered during implementation that may need revisiting:

1. **Link comparison skipped in override detection**: `_thing_states_equal_for_sources` only compares `thing_type` and `thing_data`, not links. If snapshot and store have same type/data but different links, we'd incorrectly mark the store as "not overridden". Full link comparison would require loading links from DB for each comparison. Probably rare but worth knowing.

2. **Store thing `thingstateId` is always 0**: Since store things don't have a DB record, they return `thingstateId: 0`. Clients using thingstateId for change detection should be fine since store things are immutable anyway, but worth noting.

3. **`dataChunks` for store things uses `id: -1`**: We use -1 as a synthetic chunk ID for store things. Slightly hacky - clients shouldn't cache data chunks by ID if the ID is negative.

4. **No caching/batching of store lookups**: Every thing resolution queries all stores. If there are many stores or slow async stores, this could get expensive. May want to add caching or batching later.

5. **Store errors are silently skipped**: If a store's `get_thing_state()` raises an exception, we log a warning and skip that store rather than returning a broken thing. This could hide issues but seemed like the right behavior for resilience.

6. **Backlinks don't dedupe between DB and stores**: If somehow the same backlink exists in both DB and a store (unlikely), we'd return it twice.

## Fixes for Backend Issues (1, 2, 3, 6)

The following fixes address issues 1, 2, 3, and 6 identified above:

### Fix 1: Always mark snapshot as overriding store

**Problem**: Override detection compares type/data but not links, which could incorrectly mark stores as "not overridden".

**Solution**: Remove override comparison entirely. If a thing exists in both the snapshot DB and a store, always mark the store source as `overridden: true`. The snapshot is the authoritative source regardless of whether the state matches.

**Changes**:
- Remove `_thing_states_equal_for_sources()` function from server.py
- In `resolve_thing_with_sources()`, when building sources list: if DB state exists, all store sources get `overridden: true`

### Fix 2 & 3: String IDs for thingstateId and DataChunk.id

**Problem**: Store things use `thingstateId: 0` and `dataChunk.id: -1` which could conflict with real DB IDs or cause caching issues.

**Solution**: Change ID types from `Int!` to `String!` with namespace prefixes:
- DB things: `"db:{numeric_id}"` (e.g., `"db:42"`)
- Store things: `"ext:{thing_key}"` where `thing_key` is the base64-encoded CBOR key (same as `Thing.key`)

This ensures:
- No ID collisions between DB and store things
- Store things have stable, unique IDs based on their immutable keys
- IDs are still suitable for caching/deduplication

**Backend changes**:
- Update schema.graphql: `Thing.thingstateId: Int!` → `String!`, `DataChunk.id: Int!` → `String!`
- Update `resolve_thing_thingstate_id()` to return `"db:{id}"` for DB things, `"ext:{key}"` for store things
- Update `resolve_thing_data_chunks()` to return `"db:{chunk_id}"` for DB chunks, `"ext:{key}"` for store chunks

**Frontend changes** (add to existing frontend steps):
- Update `types/index.ts`: `Thing.thingstateId: number` → `string`, `DataChunk.id: number` → `string`
- No other changes needed - frontend already converts DataChunk.id to string for caching

### Fix 6: Dedupe backlinks between DB and stores

**Problem**: Same backlink could appear twice if present in both DB and a store.

**Solution**: Dedupe the combined backlink list before returning from `resolve_thing_backlinks()`.

**Changes**:
- In `resolve_thing_backlinks()`, after combining DB and store backlinks, dedupe by `(from_key, link_type, link_key)` tuple
- Keep the DB version if duplicate (DB is authoritative)

### Implementation Order

- [x] 1. Fix 1: Remove `_thing_states_equal_for_sources`, simplify `resolve_thing_with_sources`
- [x] 2. Fix 2 & 3: Update schema and resolvers for string IDs
- [x] 3. Fix 6: Add deduplication to `resolve_thing_backlinks`
- [x] 4. Update tests to expect new behavior (string IDs, always-overridden)
- [x] 5. (Frontend) Update TypeScript types for string IDs

**Note**: The `dataChunk(id: String!)` query now supports both "db:{id}" and "ext:{key}" formats, allowing store chunks to be fetched by their ID.

## Implementation Notes (Frontend)

Things encountered during frontend implementation that may need revisiting:

1. **Graph doesn't show additional active things from stores**: The Graph currently only shows things from the snapshot (`useTickData`). To fully implement "active things" behavior, we'd need to:
   - Get active keys from `ActiveThingsContext`
   - Filter to keys not already in the snapshot things array
   - Query each of those keys individually (or add a batch query)
   - Merge resolved things into the graph data
   - This would allow store things with open panels to appear in the graph even when not captured in the snapshot

2. **PinnedThingItem makes individual queries**: Each pinned thing not in the snapshot triggers its own `useThing` query. If someone pins many things and navigates to a tick where they don't exist, that's many parallel queries. Could be optimized with a batch query (e.g., `things(keys: [String!]!)`) if this becomes a performance issue.

3. **The `sources` field is optional on Thing type**: Since only `GET_THING` fetches sources (not the tick data query), it's typed as `sources?: ThingSource[]`. Code accessing it handles undefined with `thing.sources ?? []`. This is fine but means sources info is only available in panel views, not in the things list or graph.

## Fix for Frontend Issue #1: Graph showing active things

### Problem

The Graph currently only shows things from the snapshot (`useTickData`). Active things from stores (e.g., things with open panels that aren't captured in the snapshot) don't appear in the graph even though they exist and could be displayed.

### Solution: Batch query for multiple keys

Add a new GraphQL query that fetches multiple things by key in a single request, then use it to fetch additional active things not in the snapshot.

### Backend Changes

**Schema addition** (`schema.graphql`):
```graphql
type Query {
  # ... existing queries ...

  # Batch fetch things by keys. Returns array with null for keys that don't resolve.
  things(keys: [String!]!, atTick: Int!): [Thing]!
}
```

**Resolver** (`server.py`):
```python
async def resolve_things(_, info, keys: list[str], at_tick: int) -> list[dict | None]:
    """Batch resolve multiple things by key."""
    results = []
    for key_b64 in keys:
        key_bytes = base64.b64decode(key_b64)
        thing = await resolve_thing_by_key(info.context, key_bytes, at_tick)
        results.append(thing)
    return results
```

Note: This is a simple sequential implementation. Could be optimized with `asyncio.gather` if needed, but store lookups are already async and the number of additional active keys is typically small.

### Frontend Changes

**1. Add GraphQL query** (`hooks/ticks.ts` or new file):
```typescript
const GET_THINGS_BY_KEYS = gql`
  query GetThingsByKeys($keys: [String!]!, $tick: Int!) {
    things(keys: $keys, atTick: $tick) {
      shortId
      key
      keyJSON
      thingType
      thingstateId
      links {
        linkType
        linkKey
        linkKeyJSON
        toThing {
          shortId
          key
          keyJSON
          thingType
        }
      }
    }
  }
`;
```

**2. Create `useAdditionalActiveThings` hook** (new file `hooks/useAdditionalActiveThings.ts`):
```typescript
interface UseAdditionalActiveThingsOptions {
  /** Keys already in snapshot (will be excluded from query) */
  snapshotKeys: Set<string>;
  /** Current tick for querying */
  tick: number;
}

interface UseAdditionalActiveThingsResult {
  /** Additional things that resolved successfully */
  things: Thing[];
  /** Whether query is in progress */
  loading: boolean;
}

function useAdditionalActiveThings(options: UseAdditionalActiveThingsOptions): UseAdditionalActiveThingsResult {
  const { activeKeys } = useActiveThings();

  // Compute keys to query: active but not in snapshot
  const additionalKeys = useMemo(() => {
    return Array.from(activeKeys).filter(key => !options.snapshotKeys.has(key));
  }, [activeKeys, options.snapshotKeys]);

  // Query additional keys (skip if none)
  const [result] = useQuery({
    query: GET_THINGS_BY_KEYS,
    variables: { keys: additionalKeys, tick: options.tick },
    pause: additionalKeys.length === 0,
  });

  // Filter out nulls (keys that didn't resolve)
  const things = useMemo(() => {
    if (!result.data?.things) return [];
    return result.data.things.filter((t): t is Thing => t !== null);
  }, [result.data]);

  return { things, loading: result.fetching };
}
```

**3. Update `App.tsx`** to merge snapshot + additional things:
```typescript
// In MainContent:
const snapshotKeys = useMemo(() => new Set(things.map(t => t.key)), [things]);

const { things: additionalThings } = useAdditionalActiveThings({
  snapshotKeys,
  tick: currentTickIndex,
});

// Merge snapshot things + additional active things that resolved
const allActiveThings = useMemo(() => {
  return [...things, ...additionalThings];
}, [things, additionalThings]);

// Pass allActiveThings to DockviewLayout for both Graph and ThingsList
```

**4. Update `DockviewLayout` props**:
- Replace `things` prop with `allActiveThings` (or just keep calling it `things`)
- Both Graph and ThingsList main section show the merged list
- ThingsList pinned section continues to show all pinned keys (queried individually if not in allActiveThings)

### Implementation Steps

- [x] 1. Add `things(keys: [String!]!, atTick: Int!): [Thing]!` to schema.graphql
- [x] 2. Add `resolve_things` resolver to server.py
- [x] 3. Add `GET_THINGS_BY_KEYS` query to frontend
- [x] 4. Create `useAdditionalActiveThings` hook
- [x] 5. Update App.tsx to compute and pass merged things to Graph/ThingsList
- [x] 6. DockviewLayout/Graph props unchanged (just receives merged `things` array)
- [x] 7. Test with demo that has store things + open panels

### Future Optimizations

- **Parallel resolution**: Use `asyncio.gather` in the backend resolver for concurrent store lookups
- **Caching**: graphcache should cache individual Thing entities, so repeated queries for the same keys are fast
- **Batch for pinned section**: ThingsList's PinnedThingItem could also use this batch query instead of individual `useThing` calls

## Fix: Active Things Registry Pattern

### Problem

The current `ActiveThingsContext` uses an imperative mark/unmark pattern that is error-prone:
1. **Selection doesn't mark things**: Following a link calls `onSelectThing` but this only updates React state - it never calls `markActive`. Store things don't appear in graph when selected.
2. **Cleanup is unreliable**: ThingViewPanel stores the unmark function in a ref and calls it on unmount, but this can fail if:
   - The panel isn't properly unmounted by dockview
   - The useEffect dependencies cause re-registration without cleanup
   - The `markActive` callback identity changes

### Solution: Registry Pattern

Use the existing registry pattern implementation in `lib/Registry.tsx`. Instead of imperative mark/unmark calls, use declarative registration via `useRegister` hook or `<Registration>` component.

**Existing infrastructure** (in `lib/Registry.tsx`):
- `Registry` class - stores values by key, uses `useSyncExternalStore` for subscriptions
- `useRegister(registry, key, value)` - hook for registration on mount/unmount
- `useRegistrySnapshot(registry, key)` - hook for subscribing to values
- `<Registration registry={} registryKey={} value={} />` - declarative component for registration

**New code needed:**

1. **Rich mark values for debugging**:
   ```typescript
   type MarkSource = 'snapshot' | 'selection' | 'panel' | 'pin';

   interface ActiveMark {
     key: string;        // The thing key (b64)
     source: MarkSource;
     label?: string;     // Optional debug info (e.g., panel ID)
   }
   ```

2. **ActiveMarksProvider** - creates and provides the Registry instance via context

3. **Convenience hooks**:
   - `useActiveMarksRegistry(): Registry` - get the registry (for passing to useRegister/Registration)
   - `useActiveKeys(): Set<string>` - returns set of all keys that have at least one mark
   - `useActiveMarks(): ActiveMark[]` - returns all marks (for debugging)

4. **Update components to use declarative registration**:
   - `ThingPanelContent`: Accept `markSource` prop, call `useRegister` with memoized mark
   - `ThingViewPanel`: Remove manual useEffect, pass `markSource="panel"` to ThingPanelContent
   - `DetailsPanel`: Pass `markSource="selection"` to ThingPanelContent
   - `App.tsx` snapshot marking: Use `<Registration>` components mapped over snapshot keys
   - `PinnedThingsContext`: Use `useRegister` for pinned keys

### Implementation Steps

- [x] 1. Create `hooks/useActiveMarks.tsx`:
   - Define `ActiveMark` and `MarkSource` types
   - `ActiveMarksContext` with Registry instance
   - `ActiveMarksProvider` component (creates registry in ref, provides via context)
   - `useActiveMarksRegistry()` hook
   - `useActiveKeys()` hook (subscribes to all marks, returns Set of keys)
   - `useActiveMarks()` hook (returns all marks for debugging)
- [x] 2. Update `ThingPanelContent` to accept `markSource` prop and register via `useRegister`
- [x] 3. Update `DetailsPanel` to pass `markSource="selection"`
- [x] 4. Update `ThingViewPanel` to pass `markSource="panel"` (remove manual useEffect)
- [x] 5. Update `App.tsx`:
   - Replace `ActiveThingsProvider` with `ActiveMarksProvider`
   - Replace snapshot marking useEffect with `<Registration>` components
- [x] 6. Update `PinnedThingsContext` to use `useRegister` instead of storing unmark fns
- [x] 7. Update `useAdditionalActiveThings` to use `useActiveKeys()` instead of old context
- [x] 8. Remove old `useActiveThings.tsx`
- [x] 9. Add debug logging to verify marks are being registered/unregistered correctly
- [ ] 10. Test with demo: selection, panel open/close, pinning, tick changes

### Key Design Points

- **Marks are per-key, ref-counted**: Multiple components can register marks for the same key. A key is "active" if it has at least one mark.
- **Registration is declarative**: Use `useRegister` hook or `<Registration>` component. Cleanup is automatic on unmount.
- **`<Registration>` for dynamic sets**: For snapshot things, render `<Registration>` components mapped over the array. No need for complex useEffect with cleanup tracking.
- **Memoize mark values**: The mark value should be memoized (useMemo) to avoid unnecessary re-registrations when other state changes.
