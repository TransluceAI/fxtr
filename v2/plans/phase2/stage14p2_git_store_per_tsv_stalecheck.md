# Per-TSV Staleness Check for Git Store

This document describes the design and implementation of time-based staleness checking for individual TSV file syncs in the git store, optimizing the common case of rapid successive pin retrievals.

## Motivation

Currently, the git store has two levels of staleness checking:

1. **Full sync staleness** (`full_sync_staleness_threshold`, default 5.0s): Used by `list_pins()`, `try_lookup()`, and `backrefs()` to avoid scanning all TSVs on every call
2. **Mtime/checksum caching**: Used by `ensure_synced()` to skip re-reading unchanged TSV files (requires `stat()` syscall)

When retrieving individual pins via `retrieve_pinned()`, we currently do mtime checking on every call. While `stat()` calls are fast, we can do better: if we've synced a TSV within a very short time window (e.g., 100ms), we can skip even the `stat()` call and trust our in-memory state.

This is particularly beneficial for:
- Rapid successive loads of entities from the same TSV
- Interactive workflows where multiple operations happen in quick succession
- Batch processing that loads many entities from the same namespace

## Current State

### Existing Staleness Mechanisms

**Full Sync Staleness** (in `GitNamespaceBackend`):
- Tracks `_last_full_sync_time` as a monotonic timestamp
- `_ensure_full_sync_if_stale()` checks if `(now - last_sync) > threshold`
- Used by operations that scan all TSVs: `list_pins()`, `try_lookup()`, `backrefs()`
- Default threshold: 5.0 seconds

**Per-TSV Mtime/Checksum Caching** (in `DirtyMarkManager`):
- Tracks `TsvSyncState` per TSV with `tsv_mtime`, `tsv_checksum`, `reference_mtime`
- `ensure_synced()` checks:
  1. Is TSV dirty? (instant check, no I/O)
  2. Has mtime changed? (requires `stat()` syscall)
  3. If mtime suspicious, do checksum comparison
- No time-based staleness - always checks mtime

### Sync Flow: Two Critical Paths

There are **two distinct code paths** that sync TSV files to SQLite:

#### Path 1: Read Path (retrieve_pinned)
```
retrieve_pinned()
  └─> ensure_tsv_synced()
       └─> dirty_manager.ensure_synced(tsv_path, sync_callback)
            └─> [checks dirty state, mtime, checksum]
            └─> if needed: sync_callback()
                 └─> sync_tsv_to_sqlite()
```

#### Path 2: Write Path (pin_entity_from_arena)
```
pin_entity_from_arena()
  └─> hold_dirty(tsv_path, sync_if_first)
       └─> _claim_mark()
            └─> if sentinel doesn't exist (first writer):
                 sync_if_first()  # DIRECT CALL, bypasses ensure_synced!
                  └─> sync_tsv_to_sqlite()
```

**Key Insight**: The write path (`hold_dirty` → `_claim_mark`) calls `sync_if_first()` **directly** when first marking a TSV as dirty. It does **not** go through `ensure_synced()`. This is critical for safety.

## Design Rationale

### Why Add Time-Based Staleness for Individual TSVs?

1. **Performance**: Eliminates `stat()` syscalls for rapid successive reads
2. **Different use patterns**: Reading individual pins should have lower latency than full scans
3. **Short window is safe**: With a 100ms window, the risk of missing external changes is negligible in normal workflows
4. **Progressive enhancement**: Doesn't change behavior at boundaries - just skips work in the middle

### Why Not Reuse Full Sync Staleness?

Full sync staleness (5s) is too long for individual TSV reads:
- 5 seconds is appropriate for "scan all TSVs in the directory" operations
- Individual TSV reads should have lower latency since `stat()` is fast
- Different operations have different latency/freshness tradeoffs

### Architecture: Three Layers of Caching

With this change, `ensure_synced()` will have three progressive layers:

```
1. Dirty check (instant, no I/O)
   ↓ if clean
2. Time-based staleness check (instant, no I/O) ← NEW
   ↓ if stale
3. Mtime/checksum check (requires stat, maybe read)
   ↓ if changed
4. Full sync (read TSV, update SQLite)
```

Each layer is faster than the next, progressively filtering out unnecessary work.

## Safety Analysis

### Why This Design Is Safe

The critical safety property is: **When transitioning a TSV to dirty state, we must always sync the latest TSV contents to SQLite before writing**.

This design preserves that property because:

1. **Write path bypasses `ensure_synced()`**: When `hold_dirty()` is called and the TSV is not yet dirty, `_claim_mark()` calls `sync_if_first()` **directly** (line 388 in dirty.py). This direct call bypasses all staleness checking in `ensure_synced()`.

2. **Read path uses `ensure_synced()`**: When `retrieve_pinned()` is called, it goes through `ensure_tsv_synced()` which calls `dirty_manager.ensure_synced()`. This is where staleness checking happens.

3. **Staleness only affects reads**: Adding time-based staleness to `ensure_synced()` only makes reads more permissive (skip sync more often). It does **not** affect writes, which always get the latest TSV state.

### Detailed Write Path Analysis

When `pin_entity_from_arena()` marks a TSV as dirty:

```python
# In dirty.py, _claim_mark()
async with async_file_lock_acquire(state_lock_path):
    if os.path.exists(sentinel_path):
        # Sentinel exists - TSV already dirty, just join
        # SQLite is already authoritative, no sync needed
        self._create_mark_files(dirty_dir)
        return ClaimResult.ACQUIRED
    else:
        # No sentinel - we're FIRST to go dirty
        # MUST sync TSV to SQLite before allowing writes
        async with async_file_lock_acquire(sync_lock_path):
            await sync_if_first()  # ← DIRECT call, bypasses ensure_synced!
            self._create_sentinel(dirty_dir)
            return ClaimResult.SYNCED
```

The `sync_if_first` callback is defined in `GitNamespaceBackend`:

```python
def _get_sync_callback(self, tsv_path: str):
    async def sync_if_first() -> None:
        # Direct call to sync_tsv_to_sqlite, NOT ensure_tsv_synced!
        await sync_tsv_to_sqlite(tsv_path, self._config, self._worktree_ns, self._index_ns)
    return sync_if_first
```

This directly calls `sync_tsv_to_sqlite()` which directly calls `read_tsv(tsv_path)`, getting the latest TSV state regardless of any staleness settings.

### Potential Race Condition (and why it's safe)

Consider this timeline:
1. T=0.00s: `retrieve_pinned()` syncs TSV, sets `_last_sync_time`
2. T=0.05s: External process modifies TSV (e.g., `git checkout`)
3. T=0.06s: `pin_entity_from_arena()` calls `hold_dirty()`

Is the modification at T=0.05s missed? **No**, because:
- At T=0.06s, `hold_dirty()` → `_claim_mark()` sees no sentinel (TSV is clean)
- Calls `sync_if_first()` which **directly reads the TSV file**
- Gets the modified state from T=0.05s
- Only then creates sentinel and marks as dirty

The staleness check in `ensure_synced()` is never consulted on the write path.

## Implementation Plan

### 1. Extend `DirtyMarkManager.__init__`

Add staleness threshold parameter and tracking dict:

```python
class DirtyMarkManager:
    def __init__(
        self,
        store_root: str,
        sync_staleness_threshold: float | None = None
    ) -> None:
        self.manager_uuid = uuid.uuid4()
        self._store_root = store_root
        self._dirty_tsvs: set[str] = set()
        self._tsv_state: dict[str, TsvSyncState] = {}

        # NEW: Time-based staleness tracking
        self._last_sync_time: dict[str, float] = {}
        self._sync_staleness_threshold = sync_staleness_threshold
```

### 2. Update `ensure_synced()` with staleness check

Add staleness check after dirty check, before mtime check:

```python
async def ensure_synced(
    self,
    tsv_path: str,
    sync_fn: Callable[[], Awaitable[None]],
) -> bool:
    sync_lock_path = sync_lock_path_for_tsv(tsv_path)

    # Step 1: If dirty, SQLite is authoritative
    if self.is_dirty(tsv_path):
        logger.debug(f"ensure_synced({tsv_path}): already dirty, skipping")
        return False

    # Step 1.5: NEW - Check time-based staleness (READ PATH ONLY)
    # NOTE: This check does NOT affect writes - when marking a TSV as dirty via
    # hold_dirty(), _claim_mark() calls sync_if_first directly, bypassing this method.
    if self._sync_staleness_threshold is not None and self._sync_staleness_threshold > 0:
        last_sync = self._last_sync_time.get(tsv_path)
        if last_sync is not None:
            import time
            elapsed = time.monotonic() - last_sync
            if elapsed < self._sync_staleness_threshold:
                logger.debug(
                    f"ensure_synced({tsv_path}): synced {elapsed:.3f}s ago, "
                    f"within staleness window ({self._sync_staleness_threshold}s)"
                )
                return False

    # Step 2: Quick mtime check without lock
    # ... (existing code)

    # ... later, after successful sync:
    if need_sync:
        # ... perform sync ...
        await sync_fn()

        # Update staleness timestamp
        import time
        self._last_sync_time[tsv_path] = time.monotonic()

        # Update mtime/checksum cache
        if existed:
            self._tsv_state[tsv_path] = TsvSyncState(...)
        # ...
```

### 3. Update `_claim_mark()` to set staleness timestamp

After calling `sync_if_first()`, update the staleness timestamp:

```python
async def _claim_mark(
    self,
    tsv_path: str,
    dirty_dir: str,
    sync_if_first: Callable[[], Awaitable[None]],
) -> ClaimResult:
    # ... existing code ...

    # No sentinel - we're first, need to sync
    async with async_file_lock_acquire(sync_lock_path, delete_on_release=True):
        # ... capture state ...

        await sync_if_first()

        # Update staleness timestamp (we just synced)
        import time
        self._last_sync_time[tsv_path] = time.monotonic()

        # Update mtime/checksum cache
        if existed:
            self._tsv_state[tsv_path] = TsvSyncState(...)
        # ...
```

### 4. Add `clear_staleness_cache()` method

```python
def clear_staleness_cache(self, tsv_path: str | None = None) -> None:
    """Clear staleness cache to force fresh reads on next access.

    Clears both time-based staleness and mtime/checksum caches. Use this when
    you know external changes have occurred and want to force re-sync.

    Args:
        tsv_path: Clear cache for specific TSV (absolute path), or None to clear all.
    """
    if tsv_path is None:
        logger.debug("clear_staleness_cache: clearing all caches")
        self._last_sync_time.clear()
        self._tsv_state.clear()
    else:
        logger.debug(f"clear_staleness_cache: clearing cache for {tsv_path}")
        self._last_sync_time.pop(tsv_path, None)
        self._tsv_state.pop(tsv_path, None)
```

### 5. Wire through `GitNamespaceBackend.open()`

Add parameter and pass to `DirtyMarkManager`:

```python
@classmethod
@asynccontextmanager
async def open(
    cls,
    store_root: Path,
    *,
    auto_flush_seconds: float | None = 10.0,
    full_sync_staleness_threshold: float | None = 5.0,
    sync_staleness_threshold: float | None = 0.1,  # NEW: default 100ms
    skip_sync: bool = False,
) -> AsyncIterator[GitNamespaceBackend]:
    """Open a git namespace at the given store root.

    Args:
        store_root: Path containing fxtr_store.toml
        auto_flush_seconds: Flush dirty TSVs periodically at this interval.
            Set to None to disable auto-flush. Default: 10.0
        full_sync_staleness_threshold: For list_pins(), try_lookup(), and
            backrefs(), skip full sync if done within this many seconds.
            Set to None to always sync, or 0 to never auto-sync. Default: 5.0
        sync_staleness_threshold: For retrieve_pinned(), skip TSV sync if
            synced within this many seconds. Set to None or 0 to always check
            mtime. Default: 0.1 (100ms)
        skip_sync: If True, skip the initial full sync on open.
    """
    # ...

    # Create dirty mark manager
    dirty_manager = DirtyMarkManager(
        config.store_root_str,
        sync_staleness_threshold=sync_staleness_threshold
    )
    # ...
```

### 6. Add public method to `GitNamespaceBackend`

```python
def clear_staleness_cache(self, key: str | None = None) -> None:
    """Clear staleness cache to force fresh reads on next access.

    Use this when you know external changes have occurred (e.g., after running
    a git command that might have modified TSV files) and want to ensure the
    next read gets fresh data.

    Args:
        key: Clear cache for specific pin key (derives TSV path), or None to
            clear cache for all TSVs. Examples:
            - "mykey" → clears cache for index.fxtr.tsv
            - "data/models:encoder" → clears cache for data/models.fxtr.tsv
            - None → clears all caches
    """
    if key is None:
        self._dirty_manager.clear_staleness_cache()
    else:
        rel_tsv_path, _ = parse_pin_key(key, self._config.reserved_prefix)
        tsv_path = f"{self._config.store_root_str}/{rel_tsv_path}"
        self._dirty_manager.clear_staleness_cache(tsv_path)
```

### 7. Add safety documentation comments

Add comments at critical points to document the safety invariant:

In `dirty.py`, at the top of `_claim_mark()`:
```python
async def _claim_mark(
    self,
    tsv_path: str,
    dirty_dir: str,
    sync_if_first: Callable[[], Awaitable[None]],
) -> ClaimResult:
    """Claim a dirty mark, creating it if needed.

    SAFETY INVARIANT: sync_if_first is called directly (not via ensure_synced)
    to ensure we ALWAYS do a full sync when first marking as dirty, regardless
    of any staleness caching. This prevents missing external TSV modifications
    that occurred between a read and a subsequent write.

    Args:
        tsv_path: Absolute path to the TSV file.
        dirty_dir: Path to the dirty directory.
        sync_if_first: Callback for first claim. Called directly without any
            staleness checks.

    Returns:
        SYNCED if we were first (called sync_if_first), ACQUIRED otherwise.
    """
```

In `ensure_synced()`, at the staleness check:
```python
# Step 1.5: Check time-based staleness (READ PATH ONLY)
# SAFETY NOTE: This staleness check does NOT affect writes. When marking a TSV
# as dirty via hold_dirty(), _claim_mark() calls sync_if_first directly,
# bypassing this method entirely. This ensures writes always get the latest
# TSV state, regardless of staleness settings.
```

### 8. Update LOCAL_CODE_OVERVIEW.md

Add documentation of the new feature to the git_store overview:

```markdown
### Sync Detection

`DirtyMarkManager` tracks mtime, checksum, and sync timestamps for each TSV
to avoid unnecessary syncs:

**State tracked per TSV (in memory):**
- `tsv_mtime`: mtime of TSV file after last sync
- `tsv_checksum`: SHA-256 hash of TSV content after last sync
- `reference_mtime`: filesystem timestamp captured after last sync (for race detection)
- `last_sync_time`: monotonic timestamp of last sync (for time-based staleness)

**Sync detection algorithm (in ensure_synced):**
1. If TSV is dirty (sentinel exists) → no sync needed (SQLite is authoritative)
2. If synced within staleness window → no sync needed (time-based optimization)
3. If TSV mtime unchanged from cached → no sync needed (mtime-based optimization)
4. If TSV mtime changed:
   - If TSV mtime > cached reference_mtime → sync needed
   - If TSV mtime == cached reference_mtime → potential race, do checksum check
   - If TSV mtime < cached reference_mtime → shouldn't happen, but sync to be safe
5. For checksum check: compare to cached tsv_checksum; sync if different

The time-based staleness check (step 2) is controlled by `sync_staleness_threshold`
parameter (default 100ms) and only applies to the read path (retrieve_pinned).
The write path (pin_entity_from_arena) always does a full sync when first marking
as dirty, bypassing all staleness checks.
```

## Testing Plan

### Unit Tests (test_dirty.py)

1. **Test staleness threshold parameter**:
   - Create `DirtyMarkManager` with various thresholds (None, 0, 0.1, 1.0)
   - Verify threshold is stored correctly

2. **Test staleness prevents sync within window**:
   - Set threshold to 1.0s
   - Call `ensure_synced()` which syncs
   - Modify TSV externally (change content)
   - Within 1s, call `ensure_synced()` again
   - Verify sync is skipped (returns False)
   - Verify SQLite state is stale (old content)

3. **Test staleness expires after window**:
   - Set threshold to 0.05s (50ms)
   - Call `ensure_synced()` which syncs
   - Modify TSV externally
   - Sleep 0.06s
   - Call `ensure_synced()` again
   - Verify sync happens (returns True)
   - Verify SQLite state is fresh

4. **Test threshold=None always syncs**:
   - Set threshold to None
   - Sync, modify TSV, sync again immediately
   - Verify sync happens on second call

5. **Test threshold=0 always syncs**:
   - Same as threshold=None test

6. **Test write path bypasses staleness**:
   - Set threshold to 10.0s (long window)
   - Call `ensure_synced()` which syncs
   - Modify TSV externally
   - Within window, call `hold_dirty()` as first writer
   - Verify sync happens in `_claim_mark()` (callback called)
   - Verify SQLite has fresh content from modified TSV
   - **This is the critical safety test**

7. **Test `clear_staleness_cache()` for single TSV**:
   - Sync TSV, modify it
   - Within staleness window, verify `ensure_synced()` skips
   - Call `clear_staleness_cache(tsv_path)`
   - Call `ensure_synced()` again, verify it syncs

8. **Test `clear_staleness_cache()` for all TSVs**:
   - Sync multiple TSVs, modify them
   - Verify all skip within staleness window
   - Call `clear_staleness_cache(None)`
   - Verify all sync on next `ensure_synced()`

### Integration Tests (test_git_namespace.py)

1. **Test rapid successive retrieve_pinned**:
   - Open namespace with `sync_staleness_threshold=0.1`
   - Pin entity to TSV
   - Flush to disk
   - Call `retrieve_pinned()` 10 times rapidly
   - Verify only first call syncs (via log inspection or mock)
   - Verify all calls return correct entity

2. **Test retrieve then pin within window**:
   - Open namespace with threshold=1.0s
   - Pin entity, flush, retrieve it (syncs TSV)
   - Externally modify TSV (e.g., add another entry manually)
   - Within 1s, pin a new entity to same TSV
   - Verify new pin sees the external modification
   - Flush and verify TSV contains both entities

3. **Test `clear_staleness_cache()` by key**:
   - Pin to "data/models:encoder"
   - Retrieve (syncs)
   - Modify TSV externally
   - Within window, call `clear_staleness_cache("data/models:encoder")`
   - Retrieve again, verify sees external modification

4. **Test `clear_staleness_cache()` for all**:
   - Pin to multiple TSVs
   - Retrieve from all (syncs)
   - Modify all externally
   - Call `clear_staleness_cache(None)`
   - Retrieve from all, verify all see modifications

### Benchmark (optional)

Create a microbenchmark to measure the impact:

```python
async def benchmark_rapid_retrieval():
    # Measure retrieve_pinned latency with/without staleness

    # Baseline: threshold=None (always stat)
    times_no_cache = []
    for _ in range(100):
        start = time.perf_counter()
        await ns.retrieve_pinned("data/models:encoder")
        times_no_cache.append(time.perf_counter() - start)

    # With caching: threshold=0.1
    times_with_cache = []
    for _ in range(100):
        start = time.perf_counter()
        await ns.retrieve_pinned("data/models:encoder")
        times_with_cache.append(time.perf_counter() - start)

    print(f"Without cache: p50={percentile(times_no_cache, 50):.3f}ms")
    print(f"With cache: p50={percentile(times_with_cache, 50):.3f}ms")
```

Expected results:
- Without cache: ~0.1-0.5ms (stat syscall overhead)
- With cache: ~0.01-0.05ms (pure Python dict lookup)
- Speedup: 5-10x for rapid successive reads

## Implementation Checklist

- [x] Extend `DirtyMarkManager.__init__` with threshold parameter
- [x] Add staleness check to `ensure_synced()` before mtime check
- [x] Update `ensure_synced()` to set `_last_sync_time` after sync
- [x] Update `_claim_mark()` to set `_last_sync_time` after sync
- [x] Add `clear_staleness_cache()` method to `DirtyMarkManager`
- [x] Wire `sync_staleness_threshold` through `GitNamespaceBackend.open()`
- [x] Add `clear_staleness_cache()` public method to `GitNamespaceBackend`
- [x] Add safety documentation comments to `_claim_mark()` and `ensure_synced()`
- [x] Update `LOCAL_CODE_OVERVIEW.md` with new feature documentation
- [x] Write unit tests for staleness behavior (8 tests in test_dirty_staleness.py)
- [x] Write integration tests for GitNamespaceBackend (4 tests in test_git_namespace_staleness.py)
- [x] Run all existing git_store tests to verify no regressions (266 tests pass)
- [x] Update CODE_OVERVIEW.md if needed
- [ ] (Optional) Create microbenchmark to measure impact

## Default Values

Recommended defaults for the three staleness parameters:

- `full_sync_staleness_threshold`: **5.0s** (existing)
  - For operations that scan all TSVs (list_pins, try_lookup, backrefs)
  - Longer window since full scan is more expensive

- `sync_staleness_threshold`: **0.1s** (new)
  - For individual TSV retrieve_pinned operations
  - Short window since stat is cheap
  - Balances freshness vs performance

- Both can be set to `None` for "always sync" or `0` for "never cache" behavior

## Future Enhancements

Potential improvements not included in this phase:

1. **Per-TSV staleness tracking for full sync**: Currently `full_sync_staleness_threshold` is global (all TSVs or none). Could track per-TSV to allow partial re-syncs.

2. **Adaptive staleness**: Adjust thresholds based on observed TSV modification patterns (e.g., longer window if TSV rarely changes).

3. **Filesystem change notifications**: Use inotify/FSEvents to invalidate cache when TSVs actually change, instead of relying on time windows.

4. **Staleness metrics**: Track cache hit rates, expose via observability API for tuning.
