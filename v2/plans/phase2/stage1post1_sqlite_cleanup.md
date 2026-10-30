# SQLite Session Crash Recovery Plan

This document describes a mechanism for detecting and cleaning up SQLite sessions from crashed processes.

## Problem

Currently, when a `SQLiteFxtrSession` exits normally (via the context manager), it marks itself as garbage in the database. The garbage cleanup worker then cleans up the session's arenas and eventually deletes the session record.

However, if the process **crashes** (SIGKILL, power failure, etc.), the session is never marked as garbage. It remains in the database indefinitely as a "live" session, holding references to entities that should be garbage collected. The only way to clean these up currently is manual database intervention.

## Solution Overview

Add a file-based liveness detection mechanism using `fcntl.flock`:

1. **On session startup**: Create a session file in a sibling directory to the SQLite database, acquire an exclusive flock on it, and hold that lock for the session's lifetime.

2. **On session shutdown**: Release the flock, delete the session file, and clean up the directory if empty.

3. **During garbage cleanup**: Before checking for database garbage, scan the session files directory for files that are NOT our own session. Attempt a non-blocking flock on each. If we successfully acquire the lock, that session's process is dead - mark it as garbage in the DB, then delete the file.

### Key Insight

A process holds an `fcntl.flock` as long as it's alive. When the process dies (even via SIGKILL), the kernel automatically releases all its flocks. So if we can acquire a non-blocking lock on another session's file, we know that session's process is dead.

## File Structure

For a database at `/path/to/mydb.sqlite`:

```
/path/to/mydb.sqlite
/path/to/mydb.sqlite-fxtrsessions/
├── {session_uuid_1}     # Content: "{pid}\n{timestamp_iso}\n"
├── {session_uuid_2}     # Each file is flock'd by its owning process
└── ...
```

**File naming**: Session UUID (the `_external_sid` from `SQLiteFxtrSession`).

**File contents**:
- Line 1: Process PID
- Line 2: ISO timestamp of session creation

The contents are purely informational/debugging - the flock is what matters for liveness detection.

## Implementation Plan

### Phase 1: Move and refactor lock utilities

- [x] Create `fxtr2/data/file_util/` directory with `__init__.py`
- [x] Move `fxtr2/data/git_store/lock.py` to `fxtr2/data/file_util/lock.py`
- [x] Refactor `AsyncFileLock` class to top-level functions (the class only has classmethods anyway):
  - `async_file_lock_acquire(path, create_if_not_exists=True) -> AsyncContextManager[int]`
  - Yields the fd (even though callers may not use it)
  - Keep the `_locks` dict as a module-level `_async_locks: dict[Path, anyio.Lock]`
- [x] Add `sync_file_lock_acquire_nonblocking(path) -> ContextManager[int]`:
  - Opens file (must exist)
  - Attempts non-blocking `fcntl.flock(fd, LOCK_EX | LOCK_NB)`
  - Raises `LockNotAcquiredError` if the lock is held by another process
  - Yields the fd on success
  - On exit, releases the lock and closes the fd
- [x] Update imports in `fxtr2/data/git_store/` modules to use new function name
- [x] Add tests for `sync_file_lock_acquire_nonblocking` (lock held vs not held, file not found)

### Phase 2: Session file management in SQLiteFxtrSession

- [x] Add helper to parse database path from SQLAlchemy URL (returns `None` for `:memory:`)
- [x] Add `_session_files_dir_path()` method that returns `Path(db_path + "-fxtrsessions")` or `None`
- [x] Add `_session_file_path()` method that returns `_session_files_dir / str(_external_sid)` or `None`
- [x] Add `_hold_session_file()` async context manager:
  - Creates the session files directory if it doesn't exist
  - Creates the session file with PID and timestamp
  - Acquires exclusive flock via `async_file_lock_acquire(path, create_if_not_exists=False)`
  - Yields (lock is held)
  - On exit: releases flock, closes fd (file deletion happens separately)
- [x] In `_lifecycle_handler`:
  - Use `contextlib.AsyncExitStack` to conditionally enter `_hold_session_file()` based on whether we have a file path
  - The lock context manager stays open for the session lifetime
  - In the `finally` block, after marking session as garbage in DB:
    - Delete the session file (if it exists)
    - If the session files directory is empty, delete it (ignore errors if not empty)
  - The AsyncExitStack exit handles releasing the lock automatically

### Phase 3: Dead session detection in garbage cleanup

- [x] Add `_detect_and_mark_dead_sessions()` method to `SQLiteFxtrSession`:
  - If `_session_files_dir_path()` returns `None`, return early (in-memory mode)
  - List files in session files dir (if dir doesn't exist, return early)
  - For each file that is NOT our own session UUID:
    - Try `sync_file_lock_acquire_nonblocking(path)`
    - If acquired (session is dead):
      - Parse session UUID from filename
      - In a transaction: look up session by public_id, mark as garbage if found
      - Delete the session file (while still holding lock)
      - Context manager exit releases the lock
    - If `LockNotAcquiredError`: skip (session is alive)
    - If `FileNotFoundError`: skip (another process cleaned it up)
  - After processing all files, try to remove session files dir if empty
- [x] Call `_detect_and_mark_dead_sessions()` at the start of each garbage cleanup worker iteration, right before the "check if there's any garbage to collect" database query (around line 1046)
- [x] Make the dead session detection non-fatal (wrap in try/except, log errors)

### Phase 4: Tests

- [x] Test session file creation on startup
- [x] Test session file cleanup on normal shutdown
- [x] Test dead session detection when a session file exists but no process holds the lock
- [x] Test that live sessions are not incorrectly marked as dead
- [x] Test in-memory database skips session file mechanism
- [x] Test race condition: two sessions trying to clean up same dead session (covered by live session test)
- [x] Test directory cleanup when empty
- [x] Test invalid session file cleanup

### Phase 5: Documentation

- [x] Update CODE_OVERVIEW.md with new file_util module
- [x] Document the crash recovery mechanism in sqlite_arena.py (via CODE_OVERVIEW)

## API Changes

### New in `fxtr2/data/file_util/lock.py`

```python
class LockFileNotFoundError(Exception):
    """Raised when trying to acquire a lock on a non-existent file."""
    path: Path

class LockNotAcquiredError(Exception):
    """Raised when a non-blocking lock acquisition fails (lock held by another)."""
    path: Path

@asynccontextmanager
async def async_file_lock_acquire(
    lock_path: Path,
    create_if_not_exists: bool = True
) -> AsyncIterator[int]:
    """Acquire exclusive file lock with inode verification.

    Replaces the old AsyncFileLock.acquire() classmethod.
    Yields the fd (caller may ignore it).
    """

@contextmanager
def sync_file_lock_acquire_nonblocking(lock_path: Path) -> Iterator[int]:
    """Attempt non-blocking exclusive lock acquisition.

    Usage:
        try:
            with sync_file_lock_acquire_nonblocking(path) as fd:
                # Lock held, fd is open
                ...
        except LockNotAcquiredError:
            # Another process holds the lock
            ...

    Raises:
        LockNotAcquiredError: If lock is held by another process.
        FileNotFoundError: If the file doesn't exist.
    """
```

### Changes to `SQLiteFxtrSession`

No public API changes. The crash recovery is transparent to users.

## Edge Cases

### Multiple sessions to the same database

Each session creates its own file. The garbage cleanup in each session scans for dead sessions from *any* process, not just its own previous sessions.

### Concurrent dead session detection

If sessions A and B both try to clean up dead session X:
1. A acquires the lock on X's file first
2. B's acquire attempt fails (raises `LockNotAcquiredError`)
3. B skips X, continues to next file
4. A marks X as garbage, deletes file, releases lock
5. Next iteration, B won't see X's file anymore

### Session file exists but session not in DB

Possible if someone manually deleted the session from the DB. When we acquire the lock and try to mark it as garbage, the UPDATE will affect 0 rows. We still delete the file.

### Session in DB but no session file

This is the "old crashed session" case before this feature existed. These won't be auto-cleaned. Could add a future enhancement to detect sessions that have been marked non-garbage for a very long time with no recent activity.

### Database path parsing edge cases

- `sqlite+aiosqlite:///path/to/db.sqlite` → `/path/to/db.sqlite`
- `sqlite+aiosqlite:////absolute/path.sqlite` → `/absolute/path.sqlite`
- `sqlite+aiosqlite:///:memory:` → skip session files
- `sqlite+aiosqlite://` (empty path) → skip session files

## Testing Strategy

Use multiprocessing to simulate crash scenarios:
1. Start a session in a subprocess
2. Have it create the session file and acquire the lock
3. Kill the subprocess with SIGKILL (simulates crash)
4. Start a new session in the main process
5. Verify the garbage cleanup detects and marks the dead session
