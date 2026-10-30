# Stage 9: Git Namespace Configuration Tweaks

## Summary

This plan adjusts the git namespace configuration to be simpler and more predictable:

1. **SQLite database location**: Store in `.fxtr/repo_backing_storage.sqlite` by default (shared across worktrees), remove the `~/.fxtr/` fallback
2. **Worktree namespace naming**: Use path-based names `worktree:{abspath}` by default instead of UUIDs; stop auto-creating `fxtr_store.local.toml`
3. **Index namespace naming**: Use `index:{uuid}` format instead of `git_store_index_{uuid}`

## Detailed Changes

### 1. SQLite Database Location

**Before:**
- Default: `~/.fxtr/default_git_store.sqlite`
- Could override via `sqlite_store_path` in `.fxtr/fxtr_store_repo_config.toml`

**After:**
- Default: `.fxtr/repo_backing_storage.sqlite` (relative to VCS root)
- Can still override via `sqlite_store_path` in repo config
- Remove `get_default_sqlite_store_path()` function
- Remove `get_user_fxtr_dir()` function (no longer needed)

### 2. Worktree Namespace Naming

**Before:**
- `fxtr_store.local.toml` stores `worktree_namespace_id = "git_store_worktree_{uuid}"`
- If file doesn't exist, we create it with a new UUID
- Validated that the ID starts with `git_store_worktree_`

**After:**
- Default namespace is `worktree:{abspath}` where abspath is the resolved path to the directory containing `fxtr_store.toml`
- Check `fxtr_store.local.toml` for:
  - `repo_config_path` - explicit path to repo config (bypasses VCS detection)
  - `worktree_namespace_id` - override for namespace name (any string allowed)
- Do NOT create `fxtr_store.local.toml` ourselves
- No validation on override values

### 3. Index Namespace Naming

**Before:** `git_store_index_{uuid}`
**After:** `index:{uuid}`

Only affects newly initialized stores. Existing stores keep their IDs.

When loading existing configs, we'll accept both formats (no prefix validation needed since we just read what's in the file).

## Implementation Steps

### Step 1: Update constants in `config.py`

- [x] Change `DEFAULT_SQLITE_STORE_FILENAME` from `"default_git_store.sqlite"` to `"repo_backing_storage.sqlite"`
- [x] Change `INDEX_NAMESPACE_PREFIX` from `"git_store_index_"` to `"index:"`
- [x] Remove `WORKTREE_NAMESPACE_PREFIX` constant (no longer used for generation)
- [x] Remove `get_user_fxtr_dir()` function
- [x] Remove `get_default_sqlite_store_path()` function

### Step 2: Update `load_config()` in `config.py`

- [x] Remove code that creates `fxtr_store.local.toml`
- [x] Compute default worktree namespace as `f"worktree:{store_root}"` (store_root is already resolved)
- [x] Check local config for `worktree_namespace_id` override (accept any string, no validation)
- [x] Update sqlite path resolution:
  - If `sqlite_store_path` in repo config: use it (relative to .fxtr dir if not absolute)
  - Otherwise: use `fxtr_dir / DEFAULT_SQLITE_STORE_FILENAME`
- [x] Remove validation that index_namespace_id starts with specific prefix (just check it exists and is a string)
- [x] Remove validation that worktree_namespace_id starts with specific prefix

### Step 3: Update `init_store()` in `config.py`

- [x] Use new `INDEX_NAMESPACE_PREFIX` (`index:`) when generating index namespace ID
- [x] Don't write `sqlite_store_path` to repo config (use default) - already wasn't written

### Step 4: Update `_STORE_ROOT_GITIGNORE`

- [x] Keep ignoring `fxtr_store.local.toml` (users may still create it for overrides) - no change needed

### Step 5: Update tests in `tests/test_git_store/test_config.py`

- [x] Update tests that check for `git_store_index_` prefix to use `index:`
- [x] Update tests that check for `git_store_worktree_` prefix
- [x] Update tests that verify `fxtr_store.local.toml` creation (it shouldn't be created anymore)
- [x] Update tests that check default sqlite path
- [x] Add test for path-based worktree namespace default
- [x] Add test for worktree namespace override via local config
- [x] Verify tests for `repo_config_path` override still work
- [x] Also update test in `test_git_namespace.py` that expected local config creation

### Step 6: Update documentation

- [x] Update `LOCAL_CODE_OVERVIEW.md` for git_store:
  - Update namespace naming section
  - Update store structure diagram
  - Update config.py documentation
- [x] Update `CODE_OVERVIEW.md` if needed (minimal changes expected - no changes needed)

## Files to Modify

1. `fxtr2/data/git_store/config.py` - main implementation
2. `tests/test_git_store/test_config.py` - test updates
3. `fxtr2/data/git_store/LOCAL_CODE_OVERVIEW.md` - documentation

## Backwards Compatibility

- **Existing stores**: Will continue to work. The index namespace ID is read from config (accepts any string now). If local config has a `worktree_namespace_id`, it's used; otherwise path-based default is used.
- **sqlite_store_path**: If explicitly set in repo config, still respected. Only new stores skip writing it.

## Additional Fix: Import with Missing Backing Store

During implementation, we discovered that the `import` CLI command couldn't work when the backing store was deleted but TSVs remained. This is a common scenario (e.g., cloning a repo on a new machine where only TSVs are committed).

**Problem:** `GitNamespaceBackend.open()` does a sync on open that validates all TSV entities exist in the index namespace. If the sqlite is missing/empty, this fails before `import_archive()` can run.

**Solution:**
- Added `skip_sync: bool = False` parameter to `GitNamespaceBackend.open()`
- Added `sync_from_git()` public method to trigger TSV → SQLite sync
- Updated CLI `import` command to use `skip_sync=True`, import, then call `sync_from_git()`
- Added test `TestImportWithMissingBackingStore` in `test_cli.py`

## Cleanup: Delete Sync Lock Files After Use

Previously, each TSV file had a corresponding `.sync_lock` file that persisted indefinitely. These files served two purposes:
1. **Locking**: Mutual exclusion during TSV ↔ SQLite sync
2. **Mtime reference**: After syncing, we touch the file and cache its mtime to detect filesystem-level changes

**Problem:** Leftover `.sync_lock` files clutter the directory (even though gitignored).

**Solution:** Delete the sync_lock file at the end of each lock block, *while still holding the lock* (before releasing). Still use its mtime as a reference.

**Why this works:**
- The mtime value is captured in memory (`TsvSyncState.reference_mtime`) before deletion
- The filesystem mtime granularity is what matters for comparisons - by touching a file on the same filesystem, we get a timestamp in the same "units" as the TSV's mtime
- Deleting while holding the lock ensures no other process acquired it between our work and the delete
- The next process to acquire will create a fresh file

**Changes:**
- [x] Add `delete_on_release` parameter to `async_file_lock_acquire` in `lock.py`
- [x] Rename `sync_lock_mtime` to `reference_mtime` in `TsvSyncState`
- [x] In `ensure_synced`: use `delete_on_release=True` for sync_lock
- [x] In `_claim_mark`: use `delete_on_release=True` for sync_lock
- [x] In `flush`: use `delete_on_release=True` for sync_lock
- [x] Update test that checked sync_lock file existence
- [x] Update LOCAL_CODE_OVERVIEW.md
