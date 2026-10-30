# Git Namespace Indexing

## Goal

Make `GitNamespaceBackend` support indexing and querying, so that git-backed namespaces
have the same `register_indexer` / `unregister_indexer` / `list_indexers` / `iter_matches`
capabilities as `SQLiteNamespaceBackend`.

Key constraint: the state of a `GitNamespaceBackend` is fully described by the git-tracked
folder rooted at `fxtr_store.toml`. Indexer registrations must be part of that git-tracked
state, not ephemeral SQLite-only state.

Also includes a config cleanup: rename the default reserved prefix from `"index"` to
`"root"` to avoid confusion with the indexing feature.

## Design

### Config Cleanup: Reserved Prefix Rename

The current `DEFAULT_RESERVED_PREFIX` is `"index"`, which creates an `index.fxtr.tsv` file.
This is confusing alongside the new `fxtr_store_indexes.tsv`. Rename:

- `DEFAULT_RESERVED_PREFIX` → `"root"` (new stores get `root.fxtr.tsv`)
- `LEGACY_RESERVED_PREFIX` → `"index"` (old stores keep `index.fxtr.tsv`)
- `init_store` always writes `reserved_prefix` to `fxtr_store.toml`
- `load_config`: if `reserved_prefix` is missing from the config, use `LEGACY_RESERVED_PREFIX`
  for backwards compatibility, and **write it back** to `fxtr_store.toml` so the key is
  present on future loads. This makes the legacy compat self-healing — once every existing
  store has been opened once, the fallback codepath becomes dead code and can be removed.

### Index Registration File

A new git-tracked file `fxtr_store_indexes.tsv` at the store root. Format:

```
{indexer_fxtr_id}\t{json_encoded_key_prefix}
```

Examples:
```
dR87f2abc123...	""
dR87f2abc123...	"data/models:"
dw5d7ndef456...	"turns/"
```

- One line per (indexer, prefix) registration
- Key prefix is JSON-encoded (empty prefix becomes `""`, avoids trailing-tab ambiguity)
- Sorted lexicographically by full line before writing
- Not a unique-key TSV — same indexer can appear with multiple prefixes
- File may not exist (equivalent to empty = no registrations)

### File Locking

Use `AsyncFileLock.acquire(index_file_path)` for mutual exclusion during modifications.
The lock is held for the entire register/unregister operation (including the building loop
for register). This is acceptable because index registration is infrequent.

### Sync Timing

Index registrations are synced:
- **On open**: as part of the initial full sync (see ordering below)
- **On access**: before `list_indexers`, `iter_matches`, `register_indexer`, `unregister_indexer`

The sync-on-access check compares the file state against the **live worktree_ns state**
(via `worktree_ns.list_indexers()`), not a cached in-memory snapshot. This is important for
correctness when multiple connections share the same backing store — one connection may have
modified the worktree_ns registrations, and we need to detect that. Since the number of
indexers is small, querying the live state is cheap.

### Sync Order During Full Sync

When `ensure_full_sync` runs (on open, or on stale access for list_pins/try_lookup/backrefs),
the order is:

1. **Remove dead indexers**: registrations in worktree_ns but not in the file → `unregister_indexer`
   (cheap — just marks GARBAGE, no data deletion)
2. **Sync pin TSVs**: existing flow. Only surviving indexers participate in CAS loop during
   `pin_entity_from_arena`. Dead indexers (GARBAGE) are ignored.
3. **Add new indexers**: registrations in the file but not in worktree_ns → `register_indexer`
   (building loop runs on the final pin state, one pass per new indexer)

This ordering minimizes wasted work: dead indexers don't run on newly synced pins, and new
indexers build once over the final state.

### Backing Store for Indexer Entities

When registering an indexer on the git namespace, the indexer entity is copied to the
backing ("index") namespace — same pattern as pin entities. This ensures the entity data
is available for future sessions and can be retrieved during sync.

During sync, when adding a new indexer from the file, the indexer entity is retrieved from
the backing namespace and passed to `worktree_ns.register_indexer()`. If the entity is
missing from the backing store, this is an error analogous to `FxtrSyncError` for missing
pin entities.

### Export/Import

**Export** is handled by `SQLiteNamespaceBackend.export_archive` itself — it owns its own
metadata. When exporting, the backend:
1. Creates a `"metadata"` namespace in the archive alongside the existing `"archive"` namespace
2. Calls `self.list_indexers()` to get current registrations
3. Builds a `NamespaceIndexMetadata` entity with strong links to all indexer entities
4. Pins it under key `"indexers"` in the metadata namespace

This is built-in behavior for any `SQLiteNamespaceBackend` export, not specific to git stores.
The archive does NOT contain index tables (NS_INDEX__* etc.) — those are derived state
rebuilt locally on registration.

**Import** (git store specific) reads the metadata namespace from the archive:
1. Check for a `"metadata"` namespace in the archive
2. If it exists, retrieve the `"indexers"` pin
3. Load the `NamespaceIndexMetadata` entity
4. For each indexer entity referenced: pin it in the backing (index) namespace

The indexer entities are now in the backing store. The git-tracked `fxtr_store_indexes.tsv`
controls whether they're actually registered — the next sync will pick them up.

### GitFxtrNamespace Update

`GitFxtrNamespace` in `fxtr_context_api.py` currently extends `FxtrNamespace`. It needs to
extend `FxtrQueryableNamespace` instead, which adds `register_indexer`, `unregister_indexer`,
`list_indexers`, and `iter_matches` at the user-facing API level.

## Implementation Plan

### Part 0: Config Cleanup

Modify `git_store/config.py`:

- Add `LEGACY_RESERVED_PREFIX = "index"`
- Change `DEFAULT_RESERVED_PREFIX = "root"`
- In `init_store`: always write `reserved_prefix` into `fxtr_store.toml` (currently it's
  only written if non-default)
- In `load_config`: if `reserved_prefix` key is missing from the parsed config, use
  `LEGACY_RESERVED_PREFIX` instead of `DEFAULT_RESERVED_PREFIX`, and write it back to
  `fxtr_store.toml` so the key is present on future loads
- Update tests that depend on the default prefix

### Part 1: Index File I/O

New file: `git_store/index_file.py`

- `INDEX_FILE_NAME = "fxtr_store_indexes.tsv"`
- `read_index_file(path: str) -> frozenset[tuple[FxtrID, str]]`
  - Returns empty frozenset if file doesn't exist
  - Parses each line as `{id_string}\t{json.loads(prefix)}`
- `write_index_file(path: str, registrations: frozenset[tuple[FxtrID, str]]) -> None`
  - Sorts by `(id_string, prefix)`, writes atomically (temp file + rename)
  - Deletes file if empty set
- `index_file_path_for_config(config: GitStoreConfig) -> str`
  - Returns `"{store_root_str}/fxtr_store_indexes.tsv"`

Small, self-contained module. Easy to test in isolation.

### Part 2: Index Sync Logic

New file: `git_store/index_sync.py`

Functions for comparing file state vs live worktree namespace state and applying diffs:

- `async def get_live_registrations(worktree_ns) -> frozenset[tuple[FxtrID, str]]`
  - Calls `worktree_ns.list_indexers()`, converts to frozenset of `(id, prefix)` pairs
  - Handles arena lifecycle (close after extracting IDs)

- `async def sync_index_registrations(index_file_path, worktree_ns, index_ns) -> None`
  - Full sync: read file, get live worktree_ns registrations, compute diff, apply
  - Remove dead (unregister from worktree_ns), add new (register on worktree_ns using
    entity from backing namespace)
  - Raises `FxtrSyncError` if indexer entities missing from backing store

- Split variant for the first phase of full sync:
  - `async def remove_dead_index_registrations(index_file_path, worktree_ns) -> None`
    - Removes registrations in worktree_ns but not in file
    - Used before pin sync so dead indexers don't run on newly synced pins
  - (No separate `add_new_index_registrations` — `sync_index_registrations` handles
    both adds and removes, and is used for the third phase of full sync)

- Write-back helpers for register/unregister (ns changed, update file):
  - `async def add_registration_to_file(index_file_path, indexer_id, key_prefix) -> None`
    - Reads current file, adds entry, writes sorted file
  - `async def remove_registration_from_file(index_file_path, indexer_id, key_prefix) -> None`
    - Reads current file, removes entry, writes sorted file

This module owns the full bidirectional sync between the file and the worktree_ns state.
All functions that read or write the index file should be called under the file lock
(callers are responsible for acquiring `AsyncFileLock` on the index file path). This
serializes all index state operations and eliminates races between concurrent sync and
register/unregister.

### Part 3: GitNamespaceBackend Indexing Methods

Modify `git_namespace.py`:

- Change class to implement `IndexablePinningBackend` (extends `PersistentPinningBackend`)
- Add `_index_file_path: str` instance variable (computed from config)

**`register_indexer(indexer_id, source, key_prefix)`:**
1. Acquire file lock
2. Copy indexer entity to backing (index) namespace (same pattern as pin)
3. Register on worktree namespace (building loop runs)
4. `add_registration_to_file(...)` — update the git-tracked file
5. Release file lock

**`unregister_indexer(indexer_id, key_prefix)`:**
1. Acquire file lock
2. Unregister from worktree namespace (marks GARBAGE)
3. `remove_registration_from_file(...)` — update the git-tracked file
4. Release file lock

**`list_indexers()`:**
1. Acquire file lock
2. Call `sync_index_registrations` (reads file, compares to live worktree_ns state, applies
   diffs if any)
3. Release file lock
4. Delegate to `worktree_ns.list_indexers()`

**`iter_matches(first, *rest, key_prefix, where)`:**
1. Acquire file lock
2. Call `sync_index_registrations` (same as list_indexers)
3. Release file lock
4. Delegate to `worktree_ns.iter_matches(first, *rest, key_prefix=key_prefix, where=where)`

### Part 4: Full Sync Integration

Modify the initial full sync flow. Currently `ensure_full_sync` in `sync.py` discovers
and syncs TSVs. The git namespace backend orchestrates the three-phase flow:

```python
# In GitNamespaceBackend, called during open and _ensure_full_sync_if_stale:
async def _full_sync(self):
    async with AsyncFileLock.acquire(self._index_file_path):
        # Phase 1: Remove dead indexers (before pin sync, so they don't run on new pins)
        await remove_dead_index_registrations(self._index_file_path, self._worktree_ns)
    # Phase 2: Sync pin TSVs (no index file lock needed — pin sync doesn't touch the file)
    await ensure_full_sync(self._config, self._dirty_manager, self._worktree_ns, self._index_ns)
    async with AsyncFileLock.acquire(self._index_file_path):
        # Phase 3: Sync index registrations (adds new, re-checks removes — idempotent)
        await sync_index_registrations(self._index_file_path, self._worktree_ns, self._index_ns)
```

The existing `_ensure_full_sync_if_stale` and the on-open sync path both call `_full_sync`
instead of calling `ensure_full_sync` directly.

### Part 5: NamespaceIndexMetadata Entity

New entity type for export metadata, added to `store_indexing/indexing_types.py`:

```python
@dataclass(frozen=True)
class NamespaceIndexMetadata(DataclassEntity, fxtr_type=define_type(STORE_INDEXING_UUID, "index_metadata")):
    indexer_ids: tuple[ChildFxtrID, ...]   # strong links to indexer entities
    key_prefixes: tuple[str, ...]          # parallel: key_prefixes[i] for indexer_ids[i]
```

`ChildFxtrID` creates strong links, so the indexer entities (and anything they reference,
like type entities in `supported_types`) are kept alive in the archive.

### Part 6: Export Changes

Modify `SQLiteNamespaceBackend.export_archive` to save its own indexer metadata as built-in
behavior:

1. After copying all pins to the archive namespace (existing flow)
2. Call `self.list_indexers()` to get current registrations
3. If there are registrations:
   a. Build `NamespaceIndexMetadata` entity (store into a scope)
   b. Create a `"metadata"` namespace in the archive session
   c. Pin the metadata entity under key `"indexers"`

This is self-contained in `SQLiteNamespaceBackend` — no special handling needed in the git
store's export (it just flushes/syncs first, then delegates as before).

### Part 7: Import Changes

Modify `GitNamespaceBackend._do_import_from_sqlite`:
1. After importing pin entities from the `"archive"` namespace (existing flow)
2. Check for a `"metadata"` namespace in the archive
3. If it exists, retrieve the `"indexers"` pin
4. Load the `NamespaceIndexMetadata` entity
5. For each indexer entity referenced: pin it in the backing (index) namespace

The indexer entities are now in the backing store. The git-tracked `fxtr_store_indexes.tsv`
controls whether they're actually registered — the next sync will pick them up.

### Part 8: GitFxtrNamespace → FxtrQueryableNamespace

In `fxtr_context_api.py`:
- Change `GitFxtrNamespace` to extend `FxtrQueryableNamespace` instead of `FxtrNamespace`
- `_get_backend()` already returns `GitNamespaceBackend`, which will now implement
  `IndexablePinningBackend`, so the types align

### Part 9: Tests

#### Config cleanup tests (test_git_store/test_config.py)
- New store gets `reserved_prefix = "root"` in config, creates `root.fxtr.tsv`
- Old store without `reserved_prefix` in config uses `"index"` (legacy compat)
- Old store with explicit `reserved_prefix = "index"` keeps using `"index"`

#### Index file I/O tests (test_git_store/test_index_file.py)
- Read/write roundtrip with various registrations
- Empty file / missing file → empty frozenset
- Sorted output
- JSON encoding of prefixes (empty string, normal string, string with special chars)

#### Index sync tests (test_git_store/test_index_sync.py)
- Sync with no changes (no-op)
- Sync adds new indexers
- Sync removes dead indexers
- Sync handles both adds and removes
- Missing indexer entity in backing store → error

#### GitNamespaceBackend indexing integration (test_git_store/test_git_namespace.py)
- Register indexer → file updated, worktree_ns has registration
- Unregister indexer → file updated, worktree_ns registration removed
- list_indexers returns current registrations
- iter_matches queries work end-to-end
- Sync-on-access: modify file externally, next query picks up change
- Full sync ordering: pin sync sees correct set of active indexers
- File locking: concurrent register doesn't corrupt file

#### Export/import with indexer metadata
- SQLiteNamespaceBackend export includes metadata when indexers registered
- SQLiteNamespaceBackend export omits metadata namespace when no indexers
- GitNamespaceBackend import loads indexer entities into backing store
- Round-trip: export → import into fresh store → register from file → query works

### Part 10: Update CODE_OVERVIEW.md and LOCAL_CODE_OVERVIEW.md
- Add `fxtr_store_indexes.tsv` to store structure diagram
- Document `index_file.py` and `index_sync.py` in git_store module listing
- Update GitNamespaceBackend description to mention IndexablePinningBackend
- Update GitFxtrNamespace description to mention FxtrQueryableNamespace
- Document reserved prefix rename (root vs legacy index)
- Update export/import documentation to mention indexer metadata
- Update test counts
- Update store_indexing LOCAL_CODE_OVERVIEW if NamespaceIndexMetadata is added there

## File Changes Summary

| File | Change |
|------|--------|
| `git_store/config.py` | **Modify** — rename DEFAULT_RESERVED_PREFIX, always write to config, legacy compat |
| `git_store/index_file.py` | **New** — index file read/write |
| `git_store/index_sync.py` | **New** — index registration sync logic |
| `git_store/git_namespace.py` | **Modify** — implement IndexablePinningBackend, add indexing methods |
| `store_indexing/indexing_types.py` | **Modify** — add NamespaceIndexMetadata entity |
| `sqlite_store/sqlite_namespace.py` | **Modify** — export_archive saves own indexer metadata |
| `fxtr_context_api.py` | **Modify** — GitFxtrNamespace extends FxtrQueryableNamespace |
| `git_store/LOCAL_CODE_OVERVIEW.md` | **Modify** — document new modules, config changes |
| `store_indexing/LOCAL_CODE_OVERVIEW.md` | **Modify** — document metadata entity |
| `CODE_OVERVIEW.md` | **Modify** — update descriptions and test counts |

## Implementation Order

1. [x] Part 0: Config cleanup (reserved prefix rename) + tests
   - Changed DEFAULT_RESERVED_PREFIX to "root", added LEGACY_RESERVED_PREFIX = "index"
   - Updated config.py: always write reserved_prefix to config, backfill on load
   - Updated test files: test_git_namespace.py (11 refs), test_cli.py (5 refs), test_config.py (added legacy test + import)
   - Updated source files: git_namespace.py, git_store_cli.py (to use DEFAULT_RESERVED_PREFIX)
   - All 296 tests pass
2. [x] Part 1: Index file I/O + tests
   - Created git_store/index_file.py with read/write functions
   - Created test_index_file.py with 21 tests (read/write/roundtrip)
   - All 21 tests pass
3. [x] Part 5: NamespaceIndexMetadata entity type
   - Added `NamespaceIndexMetadata` dataclass entity to `indexing_types.py`
   - Uses `ChildFxtrID` tuples for strong links to indexer entities, parallel `key_prefixes` tuple
   - Moved `STORE_INDEXING_UUID` from `indexers.py` to `indexing_types.py` (shared constant)
   - All 1103 tests pass
4. [x] Part 2: Index sync logic + tests
   - Created `git_store/index_sync.py` with: `get_live_registrations`, `remove_dead_index_registrations`, `sync_index_registrations`, `add_registration_to_file`, `remove_registration_from_file`
   - Created `test_index_sync.py` with 18 tests (live regs, remove dead, full sync, file helpers)
   - All 18 tests pass
5. [x] Part 3: GitNamespaceBackend indexing methods
   - Changed `GitNamespaceBackend` to extend `IndexablePinningBackend` (was `PersistentPinningBackend`)
   - Added `_index_file_path` instance variable (computed from config)
   - Implemented `register_indexer`, `unregister_indexer`, `list_indexers`, `iter_matches`
   - All methods use `async_file_lock_acquire` for file locking and sync before read operations
   - All 1121 tests pass
6. [x] Part 4: Full sync integration
   - Added `_full_sync` method with three-phase flow: remove dead → sync pins → add new
   - Updated `open`, `_ensure_full_sync_if_stale`, `sync_from_git`, `export_archive` to use `_full_sync`
   - All 1121 tests pass
7. [x] Part 8: GitFxtrNamespace → FxtrQueryableNamespace
   - Changed `GitFxtrNamespace` base class from `FxtrNamespace` to `FxtrQueryableNamespace`
   - `_get_backend()` returns `GitNamespaceBackend` which now implements `IndexablePinningBackend`
8. [x] Part 6: Export changes (SQLiteNamespaceBackend)
   - Added `_export_indexer_metadata` helper to `SQLiteNamespaceBackend`
   - Copies indexer entities to archive, builds `NamespaceIndexMetadata`, pins in "metadata" namespace
   - Called from `export_archive` after pin copying
   - All 1121 tests pass
9. [x] Part 7: Import changes (GitNamespaceBackend)
   - Added `_import_indexer_metadata` helper to `GitNamespaceBackend`
   - Checks for "metadata" namespace in archive, loads `NamespaceIndexMetadata`, pins indexer entities
   - Called from `_do_import_from_sqlite` after entity import
   - All 1121 tests pass
10. [x] Part 9: Integration tests (backend + export/import)
   - Created `test_git_namespace_indexing.py` with 11 tests covering:
     - register/unregister/list_indexers/iter_matches (4 basic tests)
     - iter_matches end-to-end with conditions (2 tests)
     - Sync-on-access: external file modification and addition (2 tests)
     - Export with indexer metadata, export without indexers, full import roundtrip (3 tests)
   - Fixed `_export_indexer_metadata`: use non-isolated FxtrScope, copy_from_arena for indexer entities
   - Fixed `_import_indexer_metadata`: retrieve indexer entities from archive namespace by key
   - All 1132 tests pass
11. [x] Part 10: Update CODE_OVERVIEW.md and LOCAL_CODE_OVERVIEW.md
   - Updated CODE_OVERVIEW.md: file listing (index_file.py, index_sync.py), git_store description (IndexablePinningBackend, three-phase sync), test counts (1132), GitFxtrNamespace
   - Updated git_store/LOCAL_CODE_OVERVIEW.md: store structure (root.fxtr.tsv, fxtr_store_indexes.tsv), IndexablePinningBackend, test listing
   - Updated store_indexing/LOCAL_CODE_OVERVIEW.md: NamespaceIndexMetadata, GitNamespaceBackend as implementor
