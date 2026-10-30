# Git-Backed Namespace Implementation Plan

This document describes the design and implementation plan for a git-backed fxtr namespace, allowing fxtr entities to be version-controlled alongside code.

## Overview

The git namespace layers fxtr semantics on top of git by:
- Using TSV files in the git working tree as the source of truth for pin names → FxtrID mappings
- Using a SQLite backing store (shared across the machine) to store actual entity data
- Providing sync mechanisms to keep the SQLite state consistent with git state

This is the "simpler version" described in the sketch - no custom git protocol or remote arena sync yet.

## Key Concepts

### Store Structure

A git-backed fxtr store is identified by a `fxtr_store.toml` file at its root:

```
~/.fxtr/                          # User-level fxtr directory
├── config.toml                   # User-level config (optional, for VCS customization)
└── default_git_store.sqlite      # Default sqlite store (shared across all repos)

my-project/
├── .git/
├── .fxtr/                        # Sibling to VCS root, shared across worktrees
│   ├── .gitignore                # Ignores everything in this directory
│   └── fxtr_store_repo_config.toml  # Per-repo config: index_namespace_id, sqlite_store_path
├── fxtr_store.toml               # Marker file, may contain config
├── fxtr_store.local.toml         # Gitignored, worktree-specific config
├── index.fxtr.tsv                # Default TSV for keys without path prefix
├── data/
│   ├── models.fxtr.tsv           # TSV for keys with "data/models:" prefix
│   ├── models.fxtr.tsv.sync_lock # Sync lock (for TSV→SQLite sync)
│   └── models.fxtr.tsv.dirty/    # Dirty directory (when TSV is being written)
│       ├── DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md
│       └── dirtymark-{uuid}
└── ...
```

Notes:
- The sqlite store lives at `~/.fxtr/default_git_store.sqlite` by default, shared across all repos
- Each repo's `.fxtr/fxtr_store_repo_config.toml` contains `index_namespace_id` identifying its namespace, and can optionally override `sqlite_store_path`
- The per-project `.fxtr/` directory is located as a sibling to the VCS root directory
- For git repos, VCS root is found via `git rev-parse --git-common-dir` then walking up one directory
- For other VCS or custom setups, users can configure `vcs_root_command` in `~/.fxtr/config.toml` or set `repo_config_path` explicitly in `fxtr_store.local.toml`
- For linked git worktrees, this ensures the index namespace is shared across all worktrees attached to the same `.git` folder
- The `.fxtr/` directory contains a `.gitignore` that ignores everything in it

### Pin Key Format

Pin keys map to TSV file locations:

| Key | TSV File | Key in TSV |
|-----|----------|------------|
| `mykey` | `index.fxtr.tsv` | `mykey` |
| `data/models:encoder` | `data/models.fxtr.tsv` | `encoder` |
| `foo/bar:baz` | `foo/bar.fxtr.tsv` | `baz` |

Rules:
- Keys with no colon go into the reserved prefix TSV (default: `index.fxtr.tsv`)
- Keys with exactly one colon: part before colon is the path (with `/` separators), part after is the key in that TSV
- Keys with more than one colon are an error
- The reserved prefix (default `"index"`) cannot be used as a path prefix to avoid ambiguity
- Keys cannot contain tabs or newlines

### TSV File Format

Simple two-column TSV:
```
encoder	d7a8fbe2c4f7...
decoder	d9b3c5e1a2f8...
```

Requirements:
- Keys must be sorted lexicographically
- If a file is found unsorted, it's an error (likely bad git merge)
- No header row

### Namespaces

Two SQLite namespaces in the backing store:

1. **Index namespace**: Accumulates all entities ever referenced by any git version. Shared across all worktrees of the same `.git` directory. UUID stored in `.fxtr/index_namespace_uuid`.

2. **Worktree namespace**: Mirrors the current worktree's TSV state. One per worktree. UUID stored in `fxtr_store.local.toml`.

### Worktree Sync

When the TSV files change (e.g., after `git checkout`), worktree sync:
1. Creates a new arena in the worktree namespace
2. Reads all TSV files under the store root
3. For each FxtrID found, copies from index namespace into the new arena (errors if missing)
4. Drops all existing pins in worktree namespace
5. Re-pins all entities from the new arena
6. Closes the arena (entities stay pinned)

This effectively rebuilds the worktree namespace from scratch, but reuses existing entity data.

### Sync Detection

To avoid unnecessary syncs, `DirtyMarkManager` tracks mtime and checksum state for each TSV:

**State tracked per TSV (in memory):**
- `tsv_mtime`: mtime of TSV file after last sync
- `tsv_checksum`: hash of TSV content after last sync
- `sync_lock_mtime`: mtime of sync lock file after we last touched it

**Sync detection algorithm:**
1. If TSV is dirty (sentinel exists) → no sync needed (SQLite is authoritative)
2. If TSV mtime unchanged from cached `tsv_mtime` → no sync needed
3. If TSV mtime changed:
   - If TSV mtime > cached `sync_lock_mtime` → sync needed (TSV is newer than our reference)
   - If TSV mtime == cached `sync_lock_mtime` → potential race condition, do checksum check
   - If TSV mtime < cached `sync_lock_mtime` → shouldn't happen, but sync to be safe
4. For checksum check: compute TSV checksum, compare to cached `tsv_checksum`
   - If match → no sync needed, update mtime cache
   - If differ → sync needed

**Important:** We save the sync lock mtime in memory rather than reading it dynamically, because if another process touched the sync lock, there's no guarantee they actually synced. We only trust the mtime we set ourselves.

**Detecting new/deleted TSV files:**
- During full sync, do a fresh directory scan (no mtime caching for directories)
- Compare files on disk against known TSVs in our cache
- New files: sync them to SQLite
- Deleted files: sync them (reads as empty, removing all pins), then clean up .sync_lock file
- Full scan is only done at open time or via CLI, so performance is acceptable

### Dirty Mark System

To avoid O(n) TSV rewrites on every pin operation, we use a "dirty mark" system that allows batching writes to sqlite and lazily flushing to TSV files.

**TSV States:**

Each TSV file can be in one of two states:

1. **Clean** (default, at rest): The TSV file on disk is the source of truth. Sqlite may be stale.
2. **Dirty** (transient, during writes): Sqlite is the source of truth. TSV file is out of date.

**Dirty state file structure:**
```
{name}.fxtr.tsv.dirty/
├── DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md   # Sentinel + informative message
├── dirtymark-{uuid1}                          # Process 1's dirty mark (also a lock!)
├── dirtymark-{uuid2}                          # Process 2's dirty mark (also a lock!)
├── dirtymark_flag                             # Set on every claim, used for staleness detection
└── state.lock                                 # State lock (inside dir)
```

**Dirtymark files as locks:**

Each `dirtymark-{uuid}` file serves two purposes:
1. **Existence** indicates the process has claimed the TSV for writing
2. **Lock** (via flock) indicates the process is actively writing to sqlite

When a process writes to sqlite (pin/unpin), it holds an exclusive flock on its dirtymark file. When idle (between writes), the file exists but is unlocked. This allows `flush(force=True)` to safely remove marks from idle processes while waiting for active writers to finish.

**Three-lock system:**
- **State lock** (`{name}.fxtr.tsv.dirty/state.lock`): Protects the dirty directory contents (creating/removing marks, flag). Inside the dirty dir.
- **Sync lock** (`{name}.fxtr.tsv.sync_lock`): Protects ALL TSV sync operations in both directions (TSV→SQLite reads AND SQLite→TSV flushes). Sibling to TSV file (not inside dirty dir).
- **Mark lock** (`{name}.fxtr.tsv.dirty/dirtymark-{uuid}`): Each process's dirtymark file is also a lock, held while actively writing to sqlite. Protects against force-removal during writes.
  - **IMPORTANT UPDATE:** Mark lock must also be held during flushing to prevent concurrent writes while flushes are occurring and ensure flushes follow in-progress writes.
- Sqlite handles its own locking for concurrent writes

**Sync lock purpose:** Prevents interleaved sync operations in both directions. Used during:
- Reads (mtime miss): TSV→SQLite sync
- Initial dirty claim (sync_if_first callback): TSV→SQLite sync
- Flush (dump_fn): SQLite→TSV sync

**Why one lock works for both directions:** Readers check dirty state first - if dirty, they read directly from SQLite without acquiring sync_lock. Flushers keep the TSV dirty (sentinel exists) until flush completes. So there's no contention between readers and flushers on sync_lock.

**Async file lock manager:**

All file locking is handled through a global `AsyncFileLock` utility that provides:
- **Python-level async lock per path**: Same event loop acquiring the same lock twice blocks in Python until the first releases. This prevents intra-process races.
- **FS-level flock**: Actual file locking via `fcntl.flock()` (wrapped in `anyio.to_thread.run_sync()`).
- **Inode verification**: After acquiring, verify the lock file's inode matches the path. If the file was moved/replaced during wait, retry. Applied to all locks for safety.
- **Async context manager interface**: `async with AsyncFileLock.acquire(path): ...`

```python
# Usage - all locks use inode verification
async with AsyncFileLock.acquire(state_lock_path):
    # Holding state lock (inside dirty dir)
    ...

async with AsyncFileLock.acquire(sync_lock_path):
    # Holding sync lock (sibling to TSV)
    ...
```

**Lock acquisition with inode verification:**

Since locks are inside the directory, we need to verify we hold the "current" lock (not a lock on a moved-away directory):

```python
def acquire_state_lock(dirty_dir: Path) -> FileDescriptor:
    while True:
        # Create dir if needed
        try:
            fd = open(dirty_dir / "state.lock", "a")
        except FileNotFoundError:
            os.makedirs(dirty_dir, exist_ok=True)
            continue

        # Acquire exclusive lock
        fcntl.flock(fd, fcntl.LOCK_EX)

        # Verify inode matches (dir wasn't moved while we waited)
        try:
            path_stat = os.stat(dirty_dir / "state.lock")
            fd_stat = os.fstat(fd)
        except FileNotFoundError:
            unlock_and_close(fd)
            continue  # Dir was deleted/moved, retry

        if (path_stat.st_ino, path_stat.st_dev) != (fd_stat.st_ino, fd_stat.st_dev):
            unlock_and_close(fd)
            continue  # Inode mismatch - dir was moved and recreated, retry

        return fd  # Success - we hold the current lock
```

**Directory cleanup via atomic rename:**

When removing the dirty directory (in finalize_flush COMPLETE), we atomically rename it while holding both locks:
1. Hold both state.lock and sync_lock
2. `rename(dirty_dir, temp_dir)` - atomic on same filesystem
3. Release both locks (state.lock now points to moved-away file)
4. Delete temp_dir and contents

Anyone waiting on state.lock will acquire it on the moved-away inode, fail the inode check, and retry - creating a fresh directory if needed.

**Key invariants:**
- Before writing to sqlite, a process must verify its dirty mark still exists. If removed (by force-flush), the process must re-claim before writing.
- Moving the directory requires holding BOTH state.lock and sync_lock, ensuring no race between state operations and cleanup.
- The sentinel file (DO_NOT_COMMIT) is the authoritative indicator of dirty state. The dirty directory may exist without the sentinel (during initial sync), so readers must check for the sentinel, not just the directory.

### Claiming and Holding a Dirty Mark

Before a manager can write pins to a TSV (via sqlite), it must claim a dirty mark. The **`hold_dirty` context manager** combines claiming and holding into a single atomic operation:

```python
class ClaimResult(Enum):
    SYNCED = auto()        # First to go dirty, sync_if_first was called
    ACQUIRED = auto()      # Sentinel existed, mark created
    ALREADY_HELD = auto()  # We already had a mark (fast path)
```

```
async with hold_dirty(tsv_path, sync_if_first) as result:
├── Loop (retry if mark was externally removed):
│   ├── Fast path: If we have a mark (in-memory):
│   │   ├── Try to lock our mark file (create_if_not_exists=False)
│   │   │   ├── Success: yield ALREADY_HELD, return
│   │   │   └── LockFileNotFoundError: mark was removed, fall through
│   │
│   ├── Slow path: Claim a new mark:
│   │   ├── Acquire state lock
│   │   ├── Check if sentinel file exists
│   │   │   ├── If sentinel EXISTS:
│   │   │   │   ├── Create dirtymark-{manager_uuid} file
│   │   │   │   ├── Create dirtymark_flag file
│   │   │   │   ├── Record dirty mark in-memory
│   │   │   │   └── result = ACQUIRED
│   │   │   └── If NO sentinel:
│   │   │       ├── Acquire sync lock
│   │   │       ├── Call sync_if_first() callback
│   │   │       ├── Create sentinel file
│   │   │       ├── Create dirtymark-{manager_uuid} file
│   │   │       ├── Create dirtymark_flag file
│   │   │       ├── Release sync lock
│   │   │       ├── Record dirty mark in-memory
│   │   │       └── result = SYNCED
│   │   └── Release state lock
│   │
│   ├── Try to lock our newly created mark (create_if_not_exists=False)
│   │   ├── Success: yield result, return
│   │   └── LockFileNotFoundError: race with force-flush, retry loop

# Lock held while in context, released when exiting
```

**GitNamespaceBackend usage:**
```python
async def pin_entity_from_arena(self, key: str, entity_id: FxtrID, source: Arena):
    tsv_path, tsv_key = parse_pin_key(key, self._config.reserved_prefix)

    async with self._dirty_manager.hold_dirty(
        tsv_path,
        sync_if_first=lambda: sync_tsv_to_sqlite(tsv_path, self._worktree_ns)
    ):
        # Lock held - protected from force-removal during write
        await self._worktree_ns.pin(key, entity_id)
```

**Key design points:**
- The sync must complete while holding the state lock. If the process crashes before creating the sentinel, no sentinel exists, so the next process will sync.
- Only the FIRST manager to go dirty triggers a sync. Subsequent managers just add their mark and trust sqlite.
- The mark file is locked during writes, protecting against `flush(force=True)` removing our mark during the write.
- If our mark is externally removed (by force-flush), `hold_dirty` automatically re-claims.

### Detailed Pin Flow

When `pin_entity_from_arena(key, entity_id, source_arena)` is called:

```
Phase 1: Prepare (no lock)
├── 1. Copy entity and all transitive deps from source_arena into index namespace
│   └── Uses sqlite's own locking
└── 2. Entity is now durably stored in index namespace

Phase 2: Write to sqlite (holding dirty mark via hold_dirty)
├── 3. Parse key to determine TSV file path
├── 4. Enter hold_dirty(tsv_path, sync_if_first=...) context manager
│   ├── If ALREADY_HELD: we had a mark, locked it
│   ├── If ACQUIRED: sentinel existed, mark created and locked
│   └── If SYNCED: no sentinel, callback ran (sync'd TSV→sqlite), mark created and locked
├── 5. Copy entity from index namespace into worktree namespace
├── 6. Create/update pin in worktree namespace
└── 7. Exit hold_dirty context (mark lock released)
```

Note: No TSV file I/O in the hot path! Writes go directly to sqlite. The sync in SYNCED only happens for the first claim on a TSV. The mark lock protects against force-removal during the write.

### Detailed Unpin Flow

When `unpin_entity(key)` is called:

```
├── 1. Parse key to determine TSV file path
├── 2. Enter hold_dirty(tsv_path, sync_if_first=...) context manager
│   └── Claims and locks dirty mark (syncs if first, auto-retries if mark removed)
├── 3. Remove pin from worktree namespace in sqlite
└── 4. Exit hold_dirty context (mark lock released)
```

### Retrieve Flow

When `retrieve_pinned(key)` is called:

```
├── 1. Parse key to determine TSV file path
├── 2. Check if sentinel file exists (DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md)
│   ├── If DIRTY (sentinel exists): read directly from sqlite (authoritative)
│   └── If CLEAN (no sentinel): check if sync needed
│       ├── mtime check on TSV file
│       └── If mtime changed:
│           ├── Acquire sync lock (with inode verification)
│           ├── Re-check if sentinel exists (someone may have gone dirty while we waited)
│           │   └── If now DIRTY: release sync lock, read from sqlite
│           ├── Sync TSV → sqlite for this file
│           └── Release sync lock
├── 3. Look up pin in worktree namespace (sqlite)
└── 4. Return (entity_id, arena)
```

Note: We check for the sentinel file, not just the dirty directory. The directory may exist without the sentinel during initial sync (another process is syncing TSV→sqlite before creating the sentinel). The re-check after acquiring sync lock is critical - while waiting, another process may have completed their sync and created the sentinel, meaning sqlite is now authoritative.

### Flush Flow

Flushing writes dirty sqlite state back to TSV files. Can happen:
- Explicitly via `flush()` method
- Automatically on GitNamespaceBackend close
- Automatically after K seconds since last write (configurable, via background task)
- Via CLI `flush-to-git` command

The flush uses a **callback-based API** that handles the RESTART loop internally:

```python
class FlushResult(Enum):
    COMPLETE = auto()   # Successfully flushed
    NO_MARK = auto()    # We didn't have a dirty mark
    DEFERRED = auto()   # Other writers exist, removed our mark, let them flush
```

```
flush(tsv_path, dump_fn: Callable, force=False) -> FlushResult:
├── Loop (internal RESTART handling):
│   ├── 1. Acquire state lock (with inode verification, retry loop)
│   ├── 2. Check if our dirty mark still exists
│   │   └── If not: release lock, return NO_MARK
│   ├── 3. Check if we are the ONLY dirty mark
│   │   ├── If OTHER marks exist (and not force):
│   │   │   ├── Remove our dirty mark
│   │   │   ├── Release lock
│   │   │   └── Return DEFERRED (let last writer flush)
│   │   └── If ALONE (or force=True): continue
│   ├── 4. Release state lock
│   ├── 5. Acquire sync lock (with inode verification, retry loop)
│   ├── **EDIT:** 5.5. Acquire our own dirty mark to protect against writes
│   │   └── If the mark does not exist: release both locks, return NO_MARK
│   ├── 6. Re-acquire state lock (with inode verification)
│   │   └── If inode mismatch on either lock: release both, continue loop
│   ├── ~~7. Re-check our mark exists~~ (obsoleted by 5.5)
│   │   └── ~~If not: release both locks, return NO_MARK~~
│   ├── 8. Re-check if alone (only if not force)
│   │   └── If not alone: release all locks, return DEFERRED
│   ├── 9. Remove dirtymark_flag
│   ├── 10. Release state lock (keep sync lock)
│   │
│   ├── 11. Call dump_fn() (dumps sqlite → TSV atomically, holding sync lock)
│   │   └── May be called multiple times if RESTART occurs (documented)
│   │
│   ├── 12. Acquire state lock (with inode verification)
│   │   └── If inode mismatch: release sync lock, continue loop (RESTART)
│   ├── 13. Check if dirtymark_flag exists
│   │   └── If yes: release all locks, continue loop (RESTART)
│   ├── 14. Atomically rename dirty dir to temp location (holding both locks)
│   │   └── e.g., {name}.fxtr.tsv.dirty → {name}.fxtr.tsv.dirty.removing-{uuid}
│   ├── 15. Release state lock (now points to moved-away file)
│   ├── 16. Release dirtymark lock and sync lock
│   ├── 17. Delete temp directory and all contents
│   └── 18. Return COMPLETE
```

**GitNamespaceBackend usage:**
```python
async def _flush_tsv(self, tsv_path: Path):
    result = await self._dirty_manager.flush(
        tsv_path,
        dump_fn=lambda: dump_sqlite_to_tsv(tsv_path, self._worktree_ns)
    )
    # result is COMPLETE, NO_MARK, or DEFERRED
    # RESTART loop handled internally; dump_fn may be called multiple times
```

**Staleness detection:** The `dirtymark_flag` is set on every claim. Before dumping, we remove it. If it reappears during dump (checked after dump), someone claimed and possibly wrote to sqlite - our dump is stale, so we restart and call dump_fn again.

### Full Sync on Open

When opening a GitNamespaceBackend, we do a full sync to ensure consistent state:

```
FullSync():
├── 1. Scan all TSV files under store root
├── 2. For each TSV file:
│   ├── Check if sentinel exists (DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md)
│   │   ├── If DIRTY (sentinel exists): skip (sqlite is authoritative)
│   │   └── If CLEAN (no sentinel): sync TSV → sqlite if needed (mtime check)
│   └── Verify all referenced entities exist in index namespace
└── 3. Mark store as consistent (or inconsistent if entities missing)
```

### CLI: flush-to-git (Force Flush)

The `flush-to-git` command forcefully flushes all dirty TSVs:

```
force_flush(tsv_path, dump_fn: Callable) -> FlushResult:
├── 1. If is_dirty(tsv_path) is False: return NO_MARK (TSV is already clean)
├── 2. Ensure we have a dirty mark via hold_dirty (sync_if_first provided)
│   └── This claims our mark (we can release the lock immediately after)
├── 3. Call flush(tsv_path, dump_fn, force=True)
│   └── flush(force=True) internally removes other marks, then flushes
│   └── Returns COMPLETE or NO_MARK (lost mark to another force-flusher)
└── 4. Return result

flush-to-git command:
├── 1. Scan for and delete orphaned temp directories (*.fxtr.tsv.dirty.removing-*)
├── 2. Scan for all *.fxtr.tsv.dirty directories
├── 3. For each dirty TSV: force_flush(tsv_path, dump_fn)
└── 4. Report results
```

**Why force-removing other marks is safe:**

1. **Active writers are protected**: `flush(force=True)` acquires an exclusive flock on each other mark file before deleting it. If a process is actively writing (holding its mark lock via `hold_dirty`), the force-flush waits until the write completes.

2. **Idle writers will re-claim**: After an idle process's mark is removed, their next `hold_dirty` call will detect the missing mark (via `LockFileNotFoundError`) and automatically re-claim (which syncs TSV→sqlite first). This ensures no writes are lost.

The mark-as-lock pattern ensures we never delete a mark while its owner is mid-write.

This is useful for:
- Recovering from crashed processes that left stale dirty marks
- Cleaning up orphaned temp directories from interrupted cleanup operations
- Preparing to commit (ensuring TSV files are up to date)
- Could be integrated into a pre-commit hook (future extension)

### Locking Summary

| Operation | state.lock | sync_lock | mark.lock | Sqlite |
|-----------|------------|-----------|-----------|--------|
| hold_dirty (ACQUIRED) | HELD (briefly) | - | HELD (duration) | - |
| hold_dirty (SYNCED) | HELD | HELD (during callback) | HELD (duration) | WRITE |
| hold_dirty (ALREADY_HELD) | - | - | HELD (duration) | - |
| Pin (inside hold_dirty) | - | - | HELD | WRITE |
| Unpin (inside hold_dirty) | - | - | HELD | WRITE |
| Retrieve (clean TSV, mtime ok) | - | - | - | READ |
| Retrieve (clean TSV, mtime miss) | - | HELD (during sync) | - | READ/WRITE |
| Retrieve (dirty TSV) | - | - | - | READ |
| flush (during dump_fn) | - | HELD | - | READ |
| flush (finalize/cleanup) | HELD | HELD → move dir → RELEASED | - | - |
| flush (force=True, removing) | HELD | - | HELD (each other mark) | - |

**Lock ordering:** When state.lock and sync_lock are both needed: release state lock → acquire sync_lock → re-acquire state lock. This prevents deadlocks. sync_lock is always acquired/released within state.lock (for claims) or independently (for reads/flushes).

**Inode verification:** All locks verify inode after acquiring. If the lock file was moved/replaced while waiting, the acquisition retries. This is essential for locks inside the dirty dir (which may be atomically renamed during cleanup) and provides extra safety for sync_lock.

**Directory cleanup:** Removing the dirty directory requires holding BOTH state.lock and sync_lock, then atomically renaming the directory. This invalidates state.lock for waiting processes (they'll fail inode verification and retry).

Benefits:
- Pin/unpin operations don't wait for TSV I/O
- Different TSV files can be modified concurrently
- Readers don't block writers (dirty check avoids sync_lock contention)
- Sync lock prevents interleaved sync operations in both directions
- Atomic rename ensures clean handoff during directory cleanup

### Lock Ordering

To prevent deadlocks, locks must be acquired in a consistent order:

**During hold_dirty (SYNCED path):**
```
state.lock → sync_lock → [sync callback + create sentinel + create mark] → release sync_lock → release state.lock → mark.lock (held for duration)
```
The sync_lock is innermost during claim. After releasing state.lock, we acquire our mark.lock which is held for the duration of the context.

**During hold_dirty (ACQUIRED/ALREADY_HELD path):**
```
state.lock (if ACQUIRED) → [create mark] → release state.lock → mark.lock (held for duration)
```
For ALREADY_HELD, we skip state.lock entirely and go straight to locking our existing mark.

**During pin/unpin (inside hold_dirty):**
```
[write to sqlite]
```
The mark.lock is already held by the surrounding hold_dirty context.

**During flush:**
```
state.lock (check marks) → release state.lock → sync_lock → state.lock (recheck) →
[dump callback] → state.lock (finalize) → [atomic rename] → release state.lock → release sync_lock
```
The sync_lock is acquired after releasing state.lock to avoid holding both simultaneously during the wait. State.lock is re-acquired after sync_lock to recheck conditions.

**During flush(force=True), removing other marks:**
```
state.lock → for each other mark: mark.lock → delete mark → release mark.lock → [continue with flush]
```
State lock held throughout removal to prevent new marks appearing. Each mark is locked before deletion to wait for active writers.

**During read (mtime miss):**
```
sync_lock → [check sentinel, sync if clean] → release sync_lock
```
Independent of state.lock since reads don't modify dirty state.

**Key rules:**
1. Never hold state.lock while waiting for sync_lock (release first, then acquire)
2. sync_lock is always innermost when combined with state.lock (for claims)
3. Sentinel creation happens while holding sync_lock to prevent reader races
4. mark.lock is held for the duration of hold_dirty context (after state.lock is released)
5. flush(force=True) holds state.lock while acquiring other mark locks (safe because mark.lock is acquired after state.lock by writers)

## Configuration Files

Configuration is layered across three levels:

1. **User-level** (`~/.fxtr/config.toml`): Global settings like VCS root detection
2. **Per-repo** (`.fxtr/fxtr_store_repo_config.toml`): Repo-wide settings shared across worktrees
3. **Per-store** (`fxtr_store.toml` + `fxtr_store.local.toml`): Store-specific settings

### `~/.fxtr/config.toml` (user-level, optional)

```toml
# User-level fxtr configuration
# All fields are optional

# Command to find VCS root directory (outputs absolute path to stdout)
# Default behavior (when not set): use git rev-parse --git-common-dir, then parent dir
# The command is a list of arguments, NOT a shell string
# vcs_root_command = ["hg", "root"]
# vcs_root_command = ["/bin/sh", "-c", "svn info --show-item wc-root"]
```

### `fxtr_store.toml` (git-tracked)

```toml
# Marker that this directory is a fxtr store root
# All fields are optional

# Reserved prefix for keys without colon (default: "index")
# Set to empty string to disable (then all keys must have colon)
reserved_prefix = "index"

# Human-readable name/description (not used programmatically yet)
name = "my-project-data"
description = "Fxtr store for my-project"
```

### `fxtr_store.local.toml` (gitignored)

```toml
# Worktree-specific configuration

# Namespace ID for this worktree (auto-generated on first load)
worktree_namespace_id = "git_store_worktree_550e8400-e29b-41d4-a716-446655440000"

# Path to per-repo config file (optional)
# If set, VCS root detection is skipped and this path is used directly
# If not set, VCS root is detected and .fxtr/fxtr_store_repo_config.toml is loaded from there
# repo_config_path = "/path/to/my-project/.fxtr/fxtr_store_repo_config.toml"
```

### `.fxtr/fxtr_store_repo_config.toml` (per-repo, gitignored)

Located as a sibling to the VCS root (e.g., `.git/` for git repos):

```toml
# Per-repo fxtr configuration
# Shared across all worktrees of the same repository

# Namespace ID for the index namespace (required, auto-generated on init)
index_namespace_id = "git_store_index_660e8400-e29b-41d4-a716-446655440001"

# Override sqlite store location (default: ~/.fxtr/default_git_store.sqlite)
# sqlite_store_path = "/custom/path/to/store.sqlite"
```

## Python API

### GitNamespaceBackend

```python
class GitNamespaceBackend(PersistentPinningBackend, ObservableStore):
    """Git-backed fxtr namespace.

    Wraps a SQLite backing store with git-tracked TSV files as the source
    of truth for pin mappings.
    """

    @classmethod
    async def open(
        cls,
        store_root: Path,
        auto_flush_seconds: float | None = 10.0,
    ) -> AsyncContextManager[GitNamespaceBackend]:
        """Open a git namespace at the given store root.

        The store_root must contain a fxtr_store.toml file. Creates local
        config files (fxtr_store.local.toml) and sqlite store if they don't exist.
        Errors if project .fxtr/ directory is missing (index_namespace_uuid lost).

        Performs full sync on open (syncs clean TSVs to sqlite).

        Args:
            store_root: Path containing fxtr_store.toml
            auto_flush_seconds: Flush dirty TSVs after this many seconds of
                inactivity. Set to None to disable auto-flush. Default: 10.0
        """
        ...

    @classmethod
    async def init(cls, store_root: Path, **config) -> None:
        """Initialize a new git namespace at the given path.

        Fails if fxtr_store.toml already exists (use open() instead).

        Creates:
        - fxtr_store.toml in store_root
        - .gitignore in store_root (ignores local files, dirty folder contents except sentinel)
        - .fxtr/ directory as sibling to git-common-dir
        - .fxtr/.gitignore to ignore everything in .fxtr/

        Does NOT open the store or sync - the first open() will do that.
        """
        ...

    # PersistentPinningBackend implementation
    async def pin_entity_from_arena(self, key: str, entity_id: FxtrID, source: ReadableArena):
        """Pin an entity.

        Copies entity to index namespace, claims dirty mark for the TSV,
        then writes pin to worktree namespace (sqlite). TSV file is NOT
        updated immediately - call flush() or close the namespace to
        write TSV.

        See "Detailed Pin Flow" section for full details.
        """
        ...

    async def unpin_entity(self, key: str):
        """Unpin an entity.

        Claims dirty mark for the TSV, then removes pin from worktree
        namespace (sqlite). TSV file is NOT updated immediately.

        See "Detailed Unpin Flow" section for full details.
        """
        ...

    async def retrieve_pinned(self, key: str) -> tuple[FxtrID, ReadableArena]:
        """Retrieve a pinned entity.

        If the TSV is dirty, reads from sqlite directly.
        If the TSV is clean, syncs from TSV if needed (mtime check).

        See "Retrieve Flow" section for full details.
        """
        ...

    # ObservableStore implementation
    async def try_lookup(self, node_id: FxtrID) -> ReadableArena | None:
        ...

    async def backrefs(self, node_id: FxtrID) -> list[EntityBackref | OtherBackref]:
        ...

    # Additional methods
    def new_empty_arena(self) -> CopyTargetArena:
        """Create a new arena in the worktree namespace for accumulating entities.

        Typical workflow:
        1. arena = namespace.new_empty_arena()
        2. await arena.copy_from_arena(entity_id, memory_arena)  # accumulate entities
        3. await namespace.pin_entity_from_arena("path:key", entity_id, arena)
           # This copies to index namespace and writes TSV

        The arena is backed by the worktree sqlite namespace. Entities copied
        into it are not yet in the index namespace until explicitly pinned.
        """
        ...

    async def list_pins(self, prefix: str = "") -> dict[str, FxtrID]:
        """List pins in the namespace matching the given prefix.

        Args:
            prefix: Only return pins whose keys start with this prefix.
                    Empty string (default) returns all pins.

        For dirty TSVs, reads from sqlite. For clean TSVs, syncs if needed.
        """
        ...

    async def flush(self, *, defer_to_other_writers: bool = False) -> None:
        """Flush all TSVs that this manager has dirty marks for.

        Args:
            defer_to_other_writers: If True and other managers have dirty marks,
                just remove our marks and let them handle flushing. If False,
                wait until we can flush (i.e., we're the last writer).

        Typical usage:
        - defer_to_other_writers=False (default): explicit flush, ensure TSVs are written
        - defer_to_other_writers=True: background cleanup, cooperative with other writers

        See "Flush Flow" section for details.
        """
        ...

    async def full_sync_state_to_git(self) -> None:
        """Sync ALL dirty TSVs to git, regardless of who wrote them.

        This writes sqlite state to TSV files and removes all dirty marks.
        Used by CLI 'flush-to-git' command to prepare for commits/branch switches.
        Other managers will need to re-claim dirty marks on their next write.
        """
        ...

    async def export_archive(self, output_path: Path) -> None:
        """Export current state to a standalone sqlite archive.

        Flushes all dirty TSVs first to ensure consistency.
        """
        ...

    @classmethod
    async def import_archive(cls, store_root: Path, archive_path: Path) -> None:
        """Import entities from an archive into the index namespace."""
        ...

    # Context manager handles cleanup
    async def aclose(self) -> None:
        """Close the namespace, flushing any dirty TSVs we own."""
        ...
```

### Internal Classes

```python
# From data/git_store/config.py
class GitStoreConfig:
    """Parsed configuration from all config layers."""
    store_root: Path                           # Where fxtr_store.toml lives
    reserved_prefix: str                       # Default: "index"
    worktree_namespace_id: str                 # Full ID from fxtr_store.local.toml
    index_namespace_id: str                    # Full ID from .fxtr/fxtr_store_repo_config.toml
    sqlite_store_path: Path                    # From .fxtr/fxtr_store_repo_config.toml or default
    fxtr_dir: Path                             # Path to .fxtr directory

# From data/git_store/tsv_file.py
def read_tsv(path: Path) -> dict[str, FxtrID]:
    """Read a TSV file, returning dict of tsv_key -> fxtr_id.

    Validates that keys are sorted; raises FxtrTSVError if not.
    Returns empty dict if file doesn't exist.
    """
    ...

def write_tsv(path: Path, entries: dict[str, FxtrID]) -> None:
    """Write entries to a TSV file atomically (temp file + rename).

    Entries are sorted by key before writing.
    Creates parent directories if needed.
    Deletes file if entries is empty.
    """
    ...

# From data/git_store/key_utils.py
def parse_pin_key(full_key: str, reserved_prefix: str) -> tuple[Path, str]:
    """Parse a full pin key into (tsv_path, tsv_key).

    Examples:
        "mykey" -> (Path("index.fxtr.tsv"), "mykey")  # using default reserved_prefix="index"
        "data/models:encoder" -> (Path("data/models.fxtr.tsv"), "encoder")
    """
    ...

def validate_pin_key(full_key: str, reserved_prefix: str) -> None:
    """Validate a pin key, raising FxtrKeyError if invalid."""
    ...

# From data/git_store/lock.py
class LockFileNotFoundError(Exception):
    """Raised when trying to acquire a lock on a non-existent file.

    Raised when AsyncFileLock.acquire() is called with create_if_not_exists=False
    and the lock file doesn't exist or is deleted while waiting.
    """
    path: Path

class AsyncFileLock:
    """Global async file lock manager with inode verification.

    Features:
    - Python-level async lock per path: same event loop acquiring twice blocks
    - FS-level flock via fcntl (wrapped in anyio.to_thread.run_sync)
    - Inode verification after acquiring (retries if file moved/replaced)
    - Async context manager interface
    - Optional create_if_not_exists=False mode for locking existing files only

    Usage:
        # Default: create file if needed
        async with AsyncFileLock.acquire(lock_path):
            # Holding lock
            ...

        # Require file to exist (for dirty mark files)
        try:
            async with AsyncFileLock.acquire(mark_path, create_if_not_exists=False):
                ...
        except LockFileNotFoundError:
            # File was removed
    """
    _locks: ClassVar[dict[Path, anyio.Lock]]  # Python locks per path

    @classmethod
    @asynccontextmanager
    async def acquire(
        cls,
        lock_path: Path,
        create_if_not_exists: bool = True
    ) -> AsyncIterator[None]:
        """Acquire exclusive file lock with inode verification.

        Args:
            lock_path: Path to lock file
            create_if_not_exists: If True (default), create file and parent
                directories if needed. If False, raise LockFileNotFoundError
                if file doesn't exist.

        After acquiring the flock, verifies the lock file's inode matches
        the path. If the file was moved/replaced while waiting, releases
        and retries (if create_if_not_exists=True) or raises
        LockFileNotFoundError (if create_if_not_exists=False).
        """
        ...

# From data/git_store/dirty.py
class ClaimResult(Enum):
    SYNCED = auto()        # First to go dirty, sync_if_first was called
    ACQUIRED = auto()      # Sentinel existed, mark created
    ALREADY_HELD = auto()  # We already had a mark (fast path)

class FlushResult(Enum):
    COMPLETE = auto()   # Successfully flushed
    NO_MARK = auto()    # We didn't have a dirty mark
    DEFERRED = auto()   # Other writers exist, removed our mark, let them flush

class DirtyMarkManager:
    """Manages dirty marks and TSV sync state for a GitNamespaceBackend instance.

    This class is the single source of truth for TSV file state:
    - Dirty mark management (sentinel files, dirty marks)
    - Sync lock acquisition
    - Mtime/checksum tracking for sync detection
    - Directory scanning for new/deleted TSV files

    The primary APIs are:
    - hold_dirty(): Claim a dirty mark for writing to SQLite
    - flush(): Write SQLite state back to TSV
    - ensure_synced(): Ensure TSV → SQLite is current before reads
    - discover_tsvs_needing_sync(): Find TSVs that may have changed on disk
    """
    manager_uuid: uuid.UUID        # Generated once at init, used for all dirty marks
    _dirty_tsvs: set[Path]         # TSV paths where we may have a mark (private, conservative)
    _store_root: Path              # Root directory of the store
    _tsv_state: dict[Path, TsvSyncState]  # Cached mtime/checksum per TSV

    def __init__(self, store_root: Path):
        self.manager_uuid = uuid.uuid4()
        self._dirty_tsvs = set()
        self._store_root = store_root
        self._tsv_state = {}
        ...

    @asynccontextmanager
    async def hold_dirty(
        self,
        tsv_path: Path,
        sync_if_first: Callable[[], Awaitable[None]]
    ) -> AsyncIterator[ClaimResult]:
        """Claim and hold a dirty mark for writing.

        This is the primary API for writers. Atomically claims a dirty mark
        (calling sync_if_first if first) and holds an exclusive lock on it
        for the duration of the context.

        If our mark was externally removed (by force-flush), this will
        automatically re-claim and retry.

        Args:
            tsv_path: Path to the TSV file
            sync_if_first: Async callback to run if this is the first claim
                          (no sentinel exists). Called while holding state lock
                          and sync lock.

        Yields:
            ClaimResult indicating what happened:
            - SYNCED: First to go dirty, sync_if_first was called
            - ACQUIRED: Joined existing dirty state
            - ALREADY_HELD: We already had a mark (fast path)

        Usage:
            async with dirty_manager.hold_dirty(tsv_path, sync_callback) as result:
                await worktree_ns.pin(key, entity_id)  # Protected from force-removal
        """
        ...

    def may_have_dirty_mark(self, tsv_path: Path) -> bool:
        """Check if we might have a dirty mark (conservative in-memory check).

        Returns True if we claimed a mark and didn't clean it up ourselves.
        May return True even if mark was force-removed. Use hold_dirty() for
        authoritative verification.
        """
        ...

    def is_dirty(self, tsv_path: Path) -> bool:
        """Check if TSV is in dirty state (sentinel file exists).

        Checks for the sentinel file, not just the dirty directory,
        since the directory may exist during initial sync before
        the sentinel is created.
        """
        ...

    async def flush(
        self,
        tsv_path: Path,
        dump_fn: Callable[[], Awaitable[None]],
        force: bool = False
    ) -> FlushResult:
        """Flush dirty TSV to disk.

        Args:
            tsv_path: Path to the TSV file
            dump_fn: Async callback to dump sqlite state to TSV.
                    May be called multiple times if RESTART occurs (documented).
            force: If True, remove other marks (waiting for active writers)
                   before flushing. Used by force-flush CLI command.

        Returns:
            COMPLETE: Successfully flushed
            NO_MARK: We didn't have a dirty mark (or lost it during force)
            DEFERRED: Other writers exist, removed our mark, let them flush

        If force=True:
        - Acquires exclusive flock on each other mark (waits for active writers)
        - Deletes each other mark
        - Re-checks our mark (returns NO_MARK if removed by another force-flusher)
        - Then proceeds with normal flush

        Handles all locking (including sync lock during dump) and RESTART loop
        internally. After dump_fn completes:
        - If TSV exists: updates mtime/checksum cache, touches sync_lock
        - If TSV missing: removes .sync_lock file, clears cache
        """
        ...

    def get_dirty_tsvs(self) -> set[Path]:
        """Get the set of TSV paths we may have dirty marks for.

        This is a conservative estimate - another process may have force-removed
        our marks, so some of these may no longer be valid.
        """
        ...

    # --- Sync detection and execution ---

    async def ensure_synced(
        self,
        tsv_path: Path,
        sync_fn: Callable[[], Awaitable[None]],
    ) -> bool:
        """Ensure TSV is synced to SQLite, running sync_fn if needed.

        This is the primary API for reads. Checks if sync is needed based on
        dirty state and mtime/checksum, acquires sync lock if needed, and
        runs sync_fn.

        Algorithm:
        1. If dirty → return False (SQLite is authoritative)
        2. If mtime unchanged and mtime < sync_lock_mtime → return False
        3. Acquire sync lock
        4. Re-check dirty state
        5. Do checksum check. If checksum matches, touch sync_lock and skip sync
        6. If sync needed:
            - call sync_fn()
            - update mtime/checksum cache to reflect the version we saw before calling sync_fn()
              (since all changes before this are guaranteed to be picked up by `sync_fn`)
            - if the file was removed, also remove the .sync_lock file right before we release it

        Args:
            tsv_path: Relative path to the TSV file
            sync_fn: Async callback to sync TSV → SQLite

        Returns:
            True if sync was performed, False if skipped.
        """
        ...

    def discover_tsvs_needing_sync(self) -> list[Path]:
        """Scan for TSV files that may need syncing.

        Does a fresh directory scan and compares against
        cached state. Returns TSVs that are:
        - New (not in cache)
        - Possibly modified (mtime changed or mtime == sync_lock_mtime)
        - Deleted (in cache but file missing)

        Returns relative paths to TSV files that should be checked/synced.
        """
        ...

# From data/git_store/sync.py
async def sync_tsv_to_sqlite(
    tsv_path: Path,
    config: GitStoreConfig,
    worktree_ns: SQLitePinningNamespace,
    index_ns: SQLitePinningNamespace,
) -> None:
    """Sync a single TSV file into worktree namespace.

    Reads the TSV file (or empty dict if missing), computes the full pin keys,
    and updates the worktree namespace to match. For each entry:
    - If entity exists in index namespace, create/update pin in worktree namespace
    - If entity missing from index namespace, raises FxtrSyncError

    Also removes pins that exist in worktree namespace but not in TSV.
    """
    ...

async def dump_sqlite_to_tsv(
    tsv_path: Path,
    config: GitStoreConfig,
    worktree_ns: SQLitePinningNamespace,
) -> None:
    """Dump worktree namespace pins to a TSV file (atomic write).

    Queries all pins matching the TSV's key prefix and writes them to disk.
    Uses atomic write (temp file + rename).
    """
    ...

async def ensure_tsv_synced(
    tsv_path: Path,
    config: GitStoreConfig,
    dirty_manager: DirtyMarkManager,
    worktree_ns: SQLitePinningNamespace,
    index_ns: SQLitePinningNamespace,
) -> bool:
    """Ensure a single TSV is synced to SQLite.

    Uses dirty_manager.ensure_synced() to check if sync is needed and
    acquire appropriate locks. Returns True if sync was performed.
    """
    ...

async def ensure_full_sync(
    config: GitStoreConfig,
    dirty_manager: DirtyMarkManager,
    worktree_ns: SQLitePinningNamespace,
    index_ns: SQLitePinningNamespace,
) -> None:
    """Ensure all TSVs are synced to SQLite.

    Discovers TSVs needing sync via dirty_manager, then syncs each one.
    Handles new files, modified files, and deleted files.
    """
    ...

async def flush_tsv_to_disk(
    tsv_path: Path,
    config: GitStoreConfig,
    dirty_manager: DirtyMarkManager,
    worktree_ns: SQLitePinningNamespace,
) -> FlushResult:
    """Flush a dirty TSV from SQLite to disk.

    Wraps dirty_manager.flush() with the actual dump logic.
    """
    ...
```

## CLI Interface

Entry point: `fxtr-git-store` (defined in pyproject.toml). Uses `typer` for CLI framework.

```bash
# Initialize a new store
fxtr-git-store init [--reserved-prefix PREFIX] PATH

# Sync TSV files into sqlite (git -> sqlite)
# Use after checkout/pull to pre-warm the sqlite store
fxtr-git-store sync-from-git PATH

# Flush all dirty TSVs to disk (sqlite -> git)
# Use before committing or switching branches
fxtr-git-store flush-to-git PATH

# Export current state to archive (flushes first)
fxtr-git-store export PATH OUTPUT_FILE

# Import archive into index namespace
fxtr-git-store import PATH ARCHIVE_FILE

# List all pins
fxtr-git-store list PATH

# Show store status (dirty TSVs, namespace UUIDs, etc.)
fxtr-git-store status PATH
```

**Notes:**

`sync-from-git`:
- Reads all clean TSV files and syncs them to sqlite
- Skips dirty TSVs (sqlite is already authoritative for those)
- Use after `git checkout` or `git pull` to pre-warm sqlite
- Makes subsequent `GitNamespaceBackend.open()` fast (no sync needed)

`flush-to-git`:
- Writes sqlite state to TSV files, removes all dirty marks
- Use before `git commit` or `git checkout` to ensure TSVs are up to date
- Recovers from crashed processes that left stale dirty marks
- Future: could be a pre-commit hook

## Error Handling

### Inconsistent State

The store enters an "inconsistent" state when worktree sync fails due to missing entities. In this state:

- `retrieve_pinned()` raises an error
- `pin_entity_from_arena()` raises an error
- `unpin_entity()` raises an error
- `list_pins()` raises an error
- `new_empty_arena()` still works (to allow building entities for import)
- `import_archive()` still works (to restore missing entities)

The inconsistent state is cleared when a successful sync completes (after importing the missing entities).

### Missing Entities on Sync

When worktree sync finds an ID in TSV that isn't in the index namespace:

```
FxtrSyncError: Missing entities in index namespace

The following entities are referenced in TSV files but not found in the
index namespace:

  data/models:encoder -> d7a8fbe2c4f7...
  data/models:decoder -> d9b3c5e1a2f8...

To restore consistency, import an archive containing these entities:

  fxtr-git-store import /path/to/store archive.sqlite

Then retry the operation.
```

The store enters an "inconsistent" state and refuses most operations until fixed.

### Unsorted TSV

```
FxtrTSVError: TSV file is not sorted

File: /path/to/data/models.fxtr.tsv

Keys appear out of order at line 5. This usually indicates a bad git merge.
Please manually sort the file and commit the fix.
```

### Invalid Key Format

```
FxtrKeyError: Invalid pin key 'index:foo'

The prefix 'index' is reserved. Use a different path prefix, or change
the reserved_prefix setting in fxtr_store.toml.
```

## File Organization

New files:
```
fxtr2/
├── data/
│   └── git_store/
│       ├── __init__.py           # Re-exports GitNamespaceBackend
│       ├── LOCAL_CODE_OVERVIEW.md # Detailed documentation for git_store module
│       ├── git_namespace.py      # Main GitNamespaceBackend class
│       ├── config.py             # Config parsing
│       ├── lock.py               # Per-TSV lock file handling
│       ├── dirty.py              # Dirty mark manager
│       ├── sync.py               # Sync detection and TSV<->sqlite sync
│       ├── tsv_file.py           # TSV file reading/writing
│       └── key_utils.py          # Key parsing and validation
└── cli/
    └── git_store_cli.py          # CLI implementation

tests/
└── test_git_store/
    ├── __init__.py
    ├── test_tsv_file.py
    ├── test_key_utils.py
    ├── test_config.py
    ├── test_lock.py
    ├── test_dirty.py
    ├── test_sync.py
    ├── test_git_namespace.py
    └── test_cli.py
```

The root `CODE_OVERVIEW.md` will have a high-level summary of the git_store module and direct readers to `data/git_store/LOCAL_CODE_OVERVIEW.md` for details. The LOCAL_CODE_OVERVIEW will contain:
- Detailed explanation of the store structure and TSV format
- Key concepts (index vs worktree namespace, dirty marks, sync)
- Module-by-module breakdown
- Usage examples

## Flush Backup and Concurrent Edit Detection

### Problem

When a TSV is marked dirty, SQLite is the source of truth. However, if someone externally edits the TSV file while it's dirty (e.g., via `git checkout`, manual edit, or another tool), we could blindly overwrite their changes when flushing.

### Solution: Backup with Checksum Verification

During flush, we detect concurrent edits by comparing the TSV file's checksum against what we expect from our cache. If it differs, we preserve the unexpected content in a backup file and log a warning.

**Backup filename format:** `{name}.fxtr.tsv.backup-{datetime}.tsv` where datetime is `YYYYMMDD-HHMMSS-ffffff` (e.g., `models.fxtr.tsv.backup-20260122-153045-123456.tsv`).

### Updated Flush Flow

The `dump_fn` signature changes from `Callable[[], Awaitable[None]]` to `Callable[[Path], Awaitable[None]]`, taking a temp file path to write to. The dirty manager handles the atomic swap and backup logic.

**Flush flow with backup protection:**

```
1. Create temp file path inside dirty directory (e.g., {name}.fxtr.tsv.dirty/dump-{uuid})
2. Call dump_fn(temp_path) - writes new content to temp file
3. If old TSV exists:
   a. Move old TSV to backup location
   b. Atomic rename temp → TSV
   c. Compute checksum of the backup file
   d. Compare against cached checksum:
      - If cached state exists and checksum matches: delete backup (no concurrent edit)
      - If cached state exists but checksum differs: keep backup, log warning (concurrent edit!)
      - If no cached state but file existed: keep backup, log warning (unexpected state)
4. If old TSV doesn't exist:
   a. Atomic rename temp → TSV (no backup needed)
5. Update cache with new mtime/checksum
```

This ordering (move → rename → check) minimizes the window where the TSV doesn't exist and allows us to do the checksum computation after the new file is in place.

### Orphaned Temp Directory Cleanup

`full_sync_state_to_git()` cleans up orphaned `*.fxtr.tsv.dirty.removing-*` directories left behind by crashed processes during dirty directory cleanup.

## Implementation Steps

Each phase includes implementation, tests, and documentation. At the end of each phase, all tests should pass and docs should be up to date.

### Phase 1: TSV and Key Utilities

- [x] 1.1 Create `data/git_store/tsv_file.py`
  - `read_tsv(path)`: read TSV file, validate sorted order, return dict
  - `write_tsv(path, entries)`: write dict to TSV atomically (temp file + rename)
  - Validate TSV keys (no tabs, newlines, or colons)
  - Add comprehensive docstrings

- [x] 1.2 Create `data/git_store/key_utils.py`
  - Full pin key validation (no tabs, newlines; at most one colon)
  - `parse_pin_key(key, reserved_prefix)` -> (tsv_path, tsv_key)
  - `validate_pin_key(key, reserved_prefix)` -> raises on invalid
  - Add comprehensive docstrings

- [x] 1.3 Create `data/git_store/lock.py`
  - `AsyncFileLock` class: global async file lock manager
    - Class-level dict tracking Python async locks per path
    - `acquire(lock_path)` async context manager:
      - Acquire Python lock first (blocks same-event-loop reentrance)
      - Then acquire FS lock via `fcntl.flock()` (wrapped in `anyio.to_thread.run_sync()`)
      - Verify inode matches after acquiring, retry if mismatch (always enabled)
    - Creates lock file and parent dirs if needed
  - Add comprehensive docstrings and tests

- [x] 1.4 Write tests for TSV, key utilities, and lock
  - `tests/test_git_store/test_tsv_file.py`
  - `tests/test_git_store/test_key_utils.py`
  - `tests/test_git_store/test_lock.py`
  - Test valid/invalid keys, sorted order validation, atomic writes, edge cases
  - Test lock acquisition, release, contention, inode verification

- [x] 1.5 Create `data/git_store/LOCAL_CODE_OVERVIEW.md`
  - Document TSV file format and key format
  - Document tsv_file.py, key_utils.py, and lock.py modules

- [x] 1.6 Update root CODE_OVERVIEW.md
  - Add high-level summary of git_store module
  - Point to LOCAL_CODE_OVERVIEW.md for details

### Phase 2: Config and Dirty Marks

- [x] 2.1 Create `data/git_store/config.py`
  - Parse user-level config (`~/.fxtr/config.toml`): optional `vcs_root_command`
  - Parse store config (`fxtr_store.toml`): reserved_prefix, name, description
  - Parse/create store local config (`fxtr_store.local.toml`): worktree_namespace_uuid, repo_config_path
  - Parse/create per-repo config (`.fxtr/fxtr_store_repo_config.toml`): index_namespace_id, sqlite_store_path
  - VCS root detection:
    - If `repo_config_path` is set in local config, use it directly (skip VCS detection)
    - If `vcs_root_command` is set in user config, run it to find VCS root
    - Otherwise, use default git detection: `git rev-parse --git-common-dir` → parent dir
    - Error with helpful message if VCS detection fails and no override
  - Find/create user `~/.fxtr/` directory for default sqlite store
  - Resolve sqlite store path from per-repo config (default: `~/.fxtr/default_git_store.sqlite`)
  - Handle UUID generation and storage
  - Add comprehensive docstrings

- [x] 2.2 Create `data/git_store/dirty.py`
  - `ClaimResult` enum: SYNCED, ACQUIRED, ALREADY_HELD
  - `FlushResult` enum: COMPLETE, NO_MARK, DEFERRED
  - `DirtyMarkManager` class with simplified API:
    - `hold_dirty(tsv_path, sync_if_first: Callable) -> AsyncContextManager[ClaimResult]`:
      - Primary API for writers - atomically claims and holds a dirty mark
      - SYNCED: first to go dirty, sync_if_first was called
      - ACQUIRED: joined existing dirty state
      - ALREADY_HELD: we already had a mark (fast path)
      - Automatically re-claims if mark was externally removed
    - `may_have_dirty_mark(tsv_path) -> bool`: conservative in-memory check (may be stale)
    - `is_dirty(tsv_path) -> bool`: check sentinel file exists
    - `flush(tsv_path, dump_fn: Callable, force=False) -> FlushResult`:
      - Handles RESTART loop internally (may call dump_fn multiple times)
      - Returns COMPLETE, NO_MARK, or DEFERRED
      - If force=True: removes other marks (waits for active writers) before flushing
    - `get_dirty_tsvs() -> set[Path]`: conservative set of TSVs we may have marks for
  - Helper: create `DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md` (sentinel file)
  - Cleanup via atomic rename: `rename(dirty_dir, temp_dir)` then delete temp
  - Uses `AsyncFileLock` from lock.py for all locking
  - Add comprehensive docstrings

- [x] 2.2b Update `data/git_store/lock.py`
  - Add `LockFileNotFoundError` exception
  - Add `create_if_not_exists: bool = True` parameter to `AsyncFileLock.acquire()`
  - When `create_if_not_exists=False` and file doesn't exist, raise `LockFileNotFoundError`
  - Used by `hold_dirty` to detect when mark file was externally removed

- [x] 2.3 Write tests for config and dirty marks
  - `tests/test_git_store/test_config.py`
  - `tests/test_git_store/test_dirty.py`
  - `tests/test_git_store/test_lock.py` (additional tests for create_if_not_exists)
  - Test config parsing, UUID generation, git directory detection
  - Test hold_dirty context manager (first claim syncs, subsequent acquires)
  - Test hold_dirty re-claims if mark was externally removed
  - Test flush with callback (verify dump_fn called, handle RESTART)
  - Test atomic rename cleanup
  - Test flush(force=True) removes other marks and flushes
  - Test dirtymark_flag behavior (set on claim, checked during flush)
  - Test create_if_not_exists=False raises LockFileNotFoundError

- [x] 2.4 Update LOCAL_CODE_OVERVIEW.md with config and dirty mark details

### Phase 3: Sync Logic

- [x] 3.0 Add `list_pins()` method to `PersistentPinningBackend` and implement in `SQLitePinningNamespace`
  - Add abstract method to `PersistentPinningBackend` in `abstract_arena.py`:
    - `async def list_pins(self, prefix: str = "") -> dict[str, FxtrID]`
    - Lists all pins whose keys start with the given prefix (empty string = all pins)
    - Does NOT produce an arena or guarantee pins are loadable; just returns key→ID mapping
  - Implement in `SQLitePinningNamespace` in `sqlite_arena.py`
  - `GitNamespaceBackend` will also implement this interface (replacing its own `list_pins()` signature)
  - Needed for `dump_sqlite_to_tsv()` to efficiently query pins for a specific TSV file
  - Add tests to `test_sqlite_arena.py`

- [x] 3.1 Extend `DirtyMarkManager` with mtime/checksum tracking and sync lock management
  - Add `store_root` parameter to `__init__`
  - Add mtime/checksum tracking (in-memory state):
    - `TsvSyncState` dataclass: `tsv_mtime`, `tsv_checksum`, `sync_lock_mtime`
    - `_tsv_state: dict[Path, TsvSyncState]` for per-TSV state
    - Use SHA-256 for checksums (stdlib hashlib, fast enough for small TSV files)
  - Add `ensure_synced(tsv_path, sync_fn) -> bool`:
    - `tsv_path` should be an absolute/resolved path
    - Check dirty state and mtime/checksum to decide if sync needed
    - Acquire sync lock internally if sync needed
    - Call sync_fn, then update cache
    - If TSV missing after sync, while sync_lock is held: remove .sync_lock file
  - Add `discover_tsvs_needing_sync() -> list[Path]`:
    - Fresh directory scan (no directory mtime caching), compare against cached state
    - Returns absolute paths for new, modified, and deleted TSVs
  - Update `flush()`:
    - Hold sync lock while calling dump_fn (replaces flushing.lock)
    - After dump_fn: update mtime/checksum cache if TSV exists
    - After dump_fn: remove .sync_lock if TSV missing
  - Remove `flushing.lock` - sync_lock now handles both read and write sync directions

- [x] 3.2 Add tests for DirtyMarkManager mtime/sync extensions
  - `tests/test_git_store/test_dirty.py` (extend existing)
  - Test ensure_synced: skips sync when dirty
  - Test ensure_synced: skips sync when mtime unchanged
  - Test ensure_synced: syncs when mtime changed
  - Test ensure_synced: checksum verification when mtime == sync_lock_mtime
  - Test ensure_synced: removes .sync_lock when TSV missing after sync
  - Test discover_tsvs_needing_sync: finds new TSVs
  - Test discover_tsvs_needing_sync: finds modified TSVs
  - Test discover_tsvs_needing_sync: finds deleted TSVs
  - Test flush: updates mtime/checksum cache after dump
  - Test flush: removes .sync_lock when TSV deleted (dump writes empty)

- [x] 3.3 Create `data/git_store/sync.py` with sync functions
  - `sync_tsv_to_sqlite(tsv_path, config, worktree_ns, index_ns)`:
    - Read TSV (empty dict if missing)
    - For each entry: verify entity in index_ns, create/update pin in worktree_ns
    - Remove pins in worktree_ns that aren't in TSV (for this prefix)
    - Raise `FxtrSyncError` if entity missing from index_ns
  - `dump_sqlite_to_tsv(tsv_path, config, worktree_ns)`:
    - Query pins with prefix via `worktree_ns.list_pins(prefix)`
    - Write to TSV atomically
  - `ensure_tsv_synced(tsv_path, config, dirty_manager, worktree_ns, index_ns) -> bool`:
    - Wrapper that uses `dirty_manager.ensure_synced()` with sync callback
  - `ensure_full_sync(config, dirty_manager, worktree_ns, index_ns)`:
    - First, query `worktree_ns.list_pins("")` to get all existing pins
    - Compute the TSV path for each pin (via `parse_pin_key`)
    - Call `dirty_manager.discover_tsvs_needing_sync()` to get TSVs that changed on disk
    - Merge both sets: TSVs from disk scan + TSVs implied by existing pins
    - Sync each TSV via `ensure_tsv_synced` (handles new, modified, and deleted files)
    - For TSV paths that don't exist on disk AND aren't dirty, syncing them
      will read empty content, causing `sync_tsv_to_sqlite` to remove all pins
      with that prefix (this handles orphaned pins from deleted TSVs)
  - `flush_tsv_to_disk(tsv_path, config, dirty_manager, worktree_ns) -> FlushResult`:
    - Wrapper that uses `dirty_manager.flush()` with dump callback

- [x] 3.4 Write tests for sync.py
  - `tests/test_git_store/test_sync.py`
  - Test sync_tsv_to_sqlite: creates pins from TSV entries
  - Test sync_tsv_to_sqlite: removes pins not in TSV
  - Test sync_tsv_to_sqlite: handles missing TSV (removes all pins with prefix)
  - Test sync_tsv_to_sqlite: raises FxtrSyncError for missing entities
  - Test dump_sqlite_to_tsv: writes correct TSV content
  - Test dump_sqlite_to_tsv: atomic write (no partial files)
  - Test ensure_tsv_synced: skips dirty TSVs
  - Test ensure_tsv_synced: skips unchanged TSVs
  - Test ensure_tsv_synced: syncs changed TSVs
  - Test ensure_full_sync: syncs all clean TSVs
  - Test ensure_full_sync: skips dirty TSVs
  - Test ensure_full_sync: handles new/deleted TSV files
  - Test ensure_full_sync: removes orphaned pins when TSV is deleted externally

- [x] 3.5 Update LOCAL_CODE_OVERVIEW.md with sync details
  - Document sync detection algorithm (mtime/checksum)
  - Document dirty vs clean TSV handling
  - Document DirtyMarkManager's expanded responsibilities
  - Document sync.py functions and when to use each

### Phase 4: GitNamespaceBackend Class

- [x] 4.1 Create `data/git_store/git_namespace.py` with basic structure
  - `open()` async context manager
    - Opens SQLite session
    - Gets/creates index and worktree namespaces
    - Creates DirtyMarkManager
    - Performs full sync on open
  - `init()` class method for creating new stores
  - `aclose()`: flush owned dirty marks, close session
  - Internal state: config, sqlite session, namespaces, dirty manager, consistency flag
  - Add comprehensive docstrings

- [x] 4.2 Implement read operations
  - `retrieve_pinned()`: via sync.py
  - `list_pins(prefix)`: do full sync, then read from sqlite
  - `try_lookup()` and `backrefs()` delegating to worktree SQLite namespace, always doing a full sync first

- [x] 4.3 Implement write operations (see "Detailed Pin/Unpin Flow")
  - `pin_entity_from_arena()`:
    - Copy to index namespace (no lock)
    - Use `hold_dirty(tsv_path, sync_if_first=...)` context manager
    - Write pin to sqlite inside the context (protected from force-removal)
  - `unpin_entity()`:
    - Use `hold_dirty(tsv_path, sync_if_first=...)` context manager
    - Remove pin from sqlite worktree inside the context
    - Keep in index namespace (might restore the TSV later)
  - `new_empty_arena()` delegating to worktree namespace

- [x] 4.4 Implement flush operations (callback-based)
  - Use sync.flush_tsv_to_disk
  - `flush(defer_to_other_writers)`: flush all TSVs we have dirty marks for
  - `full_sync_state_to_git()`: force flush ALL dirty TSVs (for CLI flush-to-git)
    - Acquire dirty marks on each, then force flush each

- [x] 4.5 Implement export/import
  - `export_archive()` - sync first, then create new sqlite store, copy all pinned entities
  - `import_archive()` - copy all entities from archive into index namespace

- [x] 4.6 Create `data/git_store/__init__.py`
  - Re-export GitNamespaceBackend and key public classes/functions

- [x] 4.7 Write tests for GitNamespaceBackend
  - `tests/test_git_store/test_git_namespace.py`
  - Integration tests: init, open, pin, unpin, retrieve, list
  - Test dirty mark behavior: writes go to sqlite, flush commits to TSV
  - Test that edits to (non-dirty) TSVs (add, remove, modify) get reflected immediately in git namespace state as long as they were present in the backing index namespace
  - Test concurrent access:
    - two git namespaces pointing to same store see each other's writes immediately
    - if both write then one flushes it waits for the other
    - if A writes and B writes to same key prefix, then A force-flushes (so we are no longer dirty), then we edit the TSV manually while not dirty, then B writes, it must re-claim its dirty mark and does not clobber the manual TSV change
  - Test export/import round-trip
  - Test error cases: missing entities, inconsistent state
  - Test close flushes all dirty marks

- [x] 4.8 Update LOCAL_CODE_OVERVIEW.md with GitNamespaceBackend details
  - Document public API
  - Add usage examples
  - Document dirty mark system, flush vs full_sync_state_to_git

- [x] 4.9 Flush backup system and orphaned directory cleanup
  - [x] 4.9.1 Update `dirty.py` flush with backup system
    - Change `dump_fn` signature to `Callable[[Path], Awaitable[None]]`
    - Create temp file path inside dirty directory (`dump-{uuid}`)
    - Implement backup flow: move old TSV → rename temp → check backup checksum
    - Delete backup if checksum matches, keep and warn if mismatch
    - Handle case where no cached state exists but file was present
  - [x] 4.9.2 Update `sync.py` to use new dump_fn signature
    - Update `dump_sqlite_to_tsv` to take destination path parameter
    - Update `flush_tsv_to_disk` to pass temp path to dump function
  - [x] 4.9.3 Update `git_namespace.py` for orphaned directory cleanup
    - Add orphaned `*.removing-*` directory cleanup to `full_sync_state_to_git()`
    - Update lambda in `full_sync_state_to_git` for new dump_fn signature
  - [x] 4.9.4 Add concurrent edit detection test
    - Test: A writes, B writes to same prefix, A force-flushes, TSV edited manually, B writes
    - Verify B re-claims dirty mark and doesn't clobber manual TSV change
    - Verify backup file is created with warning logged
  - [x] 4.9.5 Update LOCAL_CODE_OVERVIEW.md with flush backup documentation

### Phase 5: CLI

- [x] 5.1 Create `cli/git_store_cli.py`
  - Use `typer` for CLI framework
  - Implement commands: init, sync-from-git, flush-to-git, export, import, list, status
  - Add help text and examples
  - Add comprehensive docstrings

- [x] 5.2 Add entry point to `pyproject.toml`
  - `fxtr-git-store = "fxtr2.cli.git_store_cli:main"`

- [x] 5.3 Write CLI tests
  - `tests/test_git_store/test_cli.py`
  - Test each command with various arguments
  - Test error handling and output formatting

- [x] 5.4 Update LOCAL_CODE_OVERVIEW.md with CLI documentation
  - Document each command with examples

- [x] 5.5 Final review of root CODE_OVERVIEW.md
  - Ensure high-level summary is accurate
  - Verify pointer to LOCAL_CODE_OVERVIEW.md is clear

## Design Decisions and Rationale

### Why TSV instead of JSON/YAML?

- Simpler diff/merge in git
- Easy to visually inspect
- Sorting requirement makes conflicts easier to resolve
- Two columns is all we need

### Why separate index and worktree namespaces?

- Index namespace accumulates history (needed for checking out old commits)
- Worktree namespace is a clean slate matching current checkout
- Separation allows multiple worktrees to share index data

### Why a shared sqlite store at `~/.fxtr/`?

- Entity data can be large; avoid duplicating across repos
- Multiple repos can share entity data (e.g., same model used in different projects)
- Namespaces (identified by UUID) provide isolation within the shared store
- Can override to a custom path in `.fxtr/fxtr_store_repo_config.toml` if needed (per-repo setting)

### Why store index namespace ID in project `.fxtr/fxtr_store_repo_config.toml` (sibling to VCS root)?

- Need to know UUIDs before opening sqlite
- Must be shared across all worktrees of the same repo
- Located as sibling to VCS root ensures linked worktrees share the same index namespace
- Using a TOML config file allows storing both `index_namespace_uuid` and `sqlite_store_path` in one place
- Worktree-specific UUID goes in `fxtr_store.local.toml` which is per-worktree

### Why support non-git VCS?

- The only git-specific feature is automatic VCS root detection
- Users of Mercurial, SVN, or other systems can configure `vcs_root_command` in `~/.fxtr/config.toml`
- Users outside any VCS can set `repo_config_path` explicitly in `fxtr_store.local.toml`
- Keeps the core design VCS-agnostic while defaulting to git for convenience

### Why mtime-based sync detection with checksum fallback?

- Fast to check in the common case (just stat calls)
- Checksum state kept in memory (lost on restart, but full sync on open handles this)
- Handles edge case where TSV mtime equals sync lock mtime (potential race)
- Sync lock mtime saved in memory rather than read dynamically (can't trust other processes' touches)
- Good enough for typical workflows while being robust against edge cases

### Why the dirty mark system?

- **Performance**: Avoids O(n) TSV rewrites on every pin operation
- **Batching**: Multiple pins can be written to sqlite, then flushed once
- **Concurrent access**: Multiple processes can write to the same TSV via sqlite
- **Visibility**: Dirty directories warn against committing mid-write state
- **Recovery**: `flush-to-git` CLI can recover from crashed processes

### Why not gitignore the dirty directories?

- They serve as a warning that the TSV is out of date
- If committed, the `DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md` message alerts reviewers
- Ensures users run `flush-to-git` before committing

## Future Extensions

These are NOT part of this implementation but inform the design:

1. **Remote arena sync**: Custom git protocol to push/pull entity data alongside git packs
2. **Custom merge driver**: Automatically sort TSV files on merge
3. **Pre-commit hook**: Run `flush-to-git` automatically, warn about dirty directories
4. **Pre-checkout hook**: Run `flush-to-git` before branch switches to avoid data loss
5. **Git LFS-like smudge/clean**: Alternative approach using git filters

## Open Questions / Uncertainties

*To be filled in during implementation if issues arise*

### Resolved Implementation Decisions

1. **SQLite integration**: GitNamespaceBackend will create and manage `SQLitePinningNamespace` objects internally (one for index namespace, one for worktree namespace). We'll add `list_pins(prefix)` to the `PersistentPinningBackend` abstract class and implement it in `SQLitePinningNamespace` as part of this work to support efficient TSV flushing.

2. **CLI framework**: Use `typer` (already a dependency) instead of argparse/click.

3. **Background auto-flush**: Use `anyio` task groups started when `open()` is called, with cancellation scopes for cleanup on close.

4. **File locking**: Use `anyio.to_thread.run_sync()` for `fcntl.flock()` calls, with a non-blocking retry loop using `anyio.sleep()` between attempts.

5. **TSV scanning**: Scan all subdirectories under store root with no exclusion mechanism for now.

6. **Test git repos**: Shell out to `git init` etc. in tests (no mocking). Use `anyio.run_process()` for async subprocess calls.

7. **SQLite namespace naming**: The namespace IDs stored in config files (`index_namespace_id`, `worktree_namespace_id`) are the full namespace names (e.g., `"git_store_index_{uuid}"` or `"git_store_worktree_{uuid}"`). We pass these directly to `SQLiteFxtrSession.create_or_retrieve_user_namespace(name)`. The sqlite module handles its own internal UUIDs; we don't need to care about them.

8. **Gitignore strategy**: Instead of editing `.gitignore` at repo root, create a `.gitignore` file in the same directory as `fxtr_store.toml`. This gitignore should ignore:
   - `fxtr_store.local.toml`
   - `*.sync_lock` files
   - Dirty folder contents EXCEPT the sentinel README file (`DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md`)

9. **Config module responsibilities**: `config.py` provides methods for both creating a new store (`init`) and opening/reading an existing store's config. The actual namespace creation in sqlite happens in Phase 4, but config.py handles all file I/O for config files.

10. **VCS root detection failure**: If VCS detection fails and no override is configured, error with a helpful message explaining the options: (1) run inside a git repo, (2) configure `vcs_root_command` in `~/.fxtr/config.toml`, or (3) set `repo_config_path` explicitly in `fxtr_store.local.toml`.

11. **sync_if_first callback**: Signature is `Callable[[], Awaitable[None]]`. The caller closes over any state it needs (tsv_path, namespace, etc.).

12. **DirtyMarkManager owns all TSV state**: DirtyMarkManager is the single source of truth for TSV file state, including:
    - Dirty marks and sentinel files
    - Sync lock acquisition
    - Mtime/checksum tracking for sync detection
    - Directory scanning for new/deleted TSV files
    - Cleanup of .sync_lock files for deleted TSVs
    This centralizes all state management in one place, making it easier to reason about.

13. **Mtime edge case handling**: To handle the case where TSV mtime equals sync lock mtime (potential race from same-instant writes):
    - Save sync lock mtime in memory (don't read dynamically - can't trust other processes' touches)
    - If TSV mtime == cached sync lock mtime, do checksum verification
    - Checksum is hash of TSV content, compared against cached checksum from last sync
    - Checksums kept in memory (full sync on open handles cold starts)

14. **Sync lock used for both directions**: We use a single `sync_lock` for both TSV→SQLite reads and SQLite→TSV flushes (no separate flushing lock). This works because readers check dirty state first - if dirty, they read from SQLite directly without needing sync_lock. Flushers keep things dirty until cleanup, so there's no contention. During flush, we hold sync_lock while calling dump_fn. After the atomic write completes, we touch the sync_lock file and record its mtime.

15. **Deleted TSV handling**: When a TSV file is deleted:
    - `sync_tsv_to_sqlite()` sees empty content (read_tsv returns {} for missing files)
    - This naturally removes all pins with that prefix
    - DirtyMarkManager removes the .sync_lock file to avoid orphaned lock files
    - If someone recreates the file, a new .sync_lock is created automatically

16. **Checksum algorithm**: Use SHA-256 via `hashlib.sha256()` for TSV content checksums. It's in the stdlib and fast enough for small TSV files. Checksums are only used for change detection, not security.

17. **Path format for ensure_synced**: All TSV paths passed to `DirtyMarkManager.ensure_synced()` should be absolute/resolved paths, consistent with the existing `hold_dirty` API.

18. **Orphan pin cleanup on full sync**: When `ensure_full_sync` runs (including on open), it must detect and remove pins whose TSV files no longer exist. The approach:
    - Query all existing pins from SQLite before syncing
    - Compute the TSV path each pin should belong to (via `parse_pin_key`)
    - Merge with TSVs discovered on disk
    - For each TSV path (whether on disk or implied by pins), call `ensure_tsv_synced`
    - Non-existent TSVs sync as empty, which removes their pins via `sync_tsv_to_sqlite`
    - This handles the case where a TSV was deleted externally (e.g., `git checkout` to another branch)

19. **GitNamespaceBackend SQLite session ownership**: `GitNamespaceBackend.open()` creates and owns its own `SQLiteFxtrSession` using the sqlite path from config. This keeps the API simple and ensures proper lifecycle management.

20. **Auto-flush timer behavior**: One global periodic timer (not per-TSV). Fires every `auto_flush_seconds` and flushes all dirty TSVs with `defer_to_other_writers=True` (cooperative with other writers). Timer runs regardless of write activity.

21. **No special "inconsistent" state tracking**: If sync fails due to missing entities, the sync operation raises `FxtrSyncError`. No internal state flag needed. Users fix the issue (e.g., import missing entities) and retry. Operations will succeed once the underlying problem is resolved.

22. **Arena for `retrieve_pinned()`**: Delegate directly to `worktree_ns.retrieve_pinned()` after ensuring the TSV is synced. Reuse the `SQLiteNamespaceArena` returned by the worktree namespace.

23. **Index namespace pinning strategy**: Pin entities to index_ns using their FxtrID string as the key (e.g., `index_ns.pin_entity_from_arena(entity_id.id_string(), entity_id, source)`). This matches what `sync_tsv_to_sqlite` expects when verifying entities exist.

24. **Export archive format**: `export_archive()` creates a standalone SQLite fxtr store (not a git store) at the output path. Uses public APIs to copy all entities reachable from pinned entries. The archive is a complete SQLite store with its own namespace.

25. **Import archive behavior**: `import_archive()` copies entities from the archive's namespace into our index namespace. For each entity:
    - If key doesn't exist: pin normally
    - If key exists with matching ID: no-op (already have the data)
    - If key exists with different ID: raise an error (conflict)

26. **Full sync staleness threshold**: `GitNamespaceBackend.open()` accepts an optional `full_sync_staleness_threshold: float | None` parameter (default: e.g., 5.0 seconds). For `list_pins()`, `try_lookup()`, and `backrefs()`, skip full sync if one was performed within the threshold. Set to `None` to always sync, or `0` to never auto-sync (caller must sync explicitly).

### Potential edge cases to consider during implementation

1. **Recursive TSV scanning**: Scan all subdirectories under the store root. No exclusion mechanism for now; can add later if needed.

2. **Concurrent git operations**: What if `git checkout` runs while a process has dirty marks? The TSV changes from git will be overwritten on flush. Document that users should `flush-to-git` before branch switches. Future: pre-checkout hook.

3. **Large number of TSV files**: Dirty mark system helps here - only need to sync clean TSVs, and dirty TSVs are read directly from sqlite.

4. **TSV file atomicity**: Yes, use write to temp file then rename for atomic updates.

5. **Stale dirty marks from crashed processes**: The `flush-to-git` CLI command handles cleanup. Could also add detection of likely-stale marks (e.g., PID no longer exists) as a warning.

6. **Auto-flush timing**: Configurable on GitNamespaceBackend (default 10 seconds). Background task runs periodically and flushes dirty TSVs with `defer_to_other_writers=True`.

7. **Missing `.fxtr/` directory**: If the project `.fxtr/` directory is deleted (losing index_namespace_uuid), `open()` should error and require re-init or manual UUID restoration.

8. **Listing pins by prefix for flush**: When flushing a TSV, we need to get all pins with matching key prefix from sqlite. **Resolved**: Will add `list_pins(prefix)` to `PersistentPinningBackend` and implement in `SQLitePinningNamespace` as part of this work.

### Testing notes

Integration tests will need temporary git repositories. Use pytest fixtures in `conftest.py` to create temp directories with `git init` and basic repo structure. Shelling out to actual `git` commands is acceptable (no mocking needed).

### Known Issues to Address Before Implementation

#### 1. ~~Race condition in `was_first` / claim_dirty_mark flow~~ (RESOLVED)

**Problem:** The original ClaimDirtyMark flow creates the sentinel, releases the lock, and then the caller syncs. If the process crashes after creating the sentinel but before syncing, the next process would see the sentinel, assume sync was done, and skip it. This leads to data loss.

**Resolution:** Implemented callback-based API via the `hold_dirty` context manager:
- `hold_dirty(tsv_path, sync_if_first=callback)` handles sync atomically
- If no sentinel exists, callback is invoked while holding state lock + sync lock
- Sentinel is only created after callback completes successfully
- If process crashes before callback completes, no sentinel exists, next process will sync
- Additionally, `hold_dirty` auto-retries if our mark is externally removed (via `LockFileNotFoundError`)

#### 2. Orphaned temp directories from cleanup

**Problem:** If a process crashes after renaming the dirty dir to temp location but before deleting it, or if deletion fails, orphaned `*.removing-{uuid}` directories are left behind.

**Resolution:** The `flush-to-git` CLI command should scan for and clean up these orphaned temp directories as part of its operation.

### Clarifications

1. **Per-TSV sync vs full sync:** The "Worktree Sync" section describes rebuilding the entire worktree namespace from all TSVs. In practice, `sync_tsv_to_sqlite()` operates on a single TSV file, updating only the pins with that TSV's key prefix. Full sync on open calls this for each clean TSV. The full-rebuild approach is conceptually what happens, but it's implemented incrementally per-TSV.

2. **`sync_tsv_to_sqlite` scope:** This function syncs a single TSV file's contents into the worktree namespace. It updates/creates/removes pins for keys matching that TSV's prefix (e.g., `data/models:*` for `data/models.fxtr.tsv`), leaving other pins untouched.

## Potential Future Improvements

These are not part of the initial implementation but could be added later:

### Batch Pin API

The current design calls `hold_dirty()` for each `pin_entity_from_arena()` call. For bulk operations (e.g., pinning 100 entities to the same TSV), this is wasteful.

**Potential improvement:**
```python
async def pin_entities_from_arena(
    self,
    items: list[tuple[str, FxtrID]],
    source: ReadableArena
) -> None:
    """Pin multiple entities efficiently.

    Groups items by TSV file and uses a single hold_dirty context per TSV,
    then writes all pins in a batch.
    """
    # Group by TSV path
    by_tsv: dict[Path, list[tuple[str, FxtrID]]] = {}
    for key, entity_id in items:
        tsv_path, tsv_key = parse_pin_key(key, self._config.reserved_prefix)
        by_tsv.setdefault(tsv_path, []).append((key, entity_id))

    # Process each TSV group
    for tsv_path, group in by_tsv.items():
        async with self._dirty_manager.hold_dirty(tsv_path, sync_if_first=...):
            for key, entity_id in group:
                # Copy to index namespace and write pin
                ...
```

This would significantly improve performance for bulk operations while maintaining the same guarantees.

## Phase 4 Implementation Notes

Notes from Phase 4 implementation that may be worth revisiting:

### Auto-flush error handling

The auto-flush background task catches and logs exceptions but keeps running. This prevents one failed flush from stopping all future flushes, but errors might be silently logged. Might want to surface these more prominently in the future (e.g., callback for error notification, or tracking failed flush attempts).

### Test coverage gap (addressed in 4.9.4)

The plan mentioned a specific concurrent access test case: "if A writes and B writes to same key prefix, then A force-flushes (so we are no longer dirty), then we edit the TSV manually while not dirty, then B writes, it must re-claim its dirty mark and does not clobber the manual TSV change". This is now covered by the test added in 4.9.4.
