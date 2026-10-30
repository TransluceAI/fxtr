# TSV Namespace Module Overview

This module provides the tsv namespace backend for fxtr: a namespace defined via git-tracked TSV files, allowing pin mappings to be version-controlled alongside code. The TSV files track pin name → FxtrID mappings, while a shared repo-level entity store (SQLite) holds the actual entity data.

## Modules

- `tsv_file.py`: TSV file reading/writing with sorted order validation
- `key_utils.py`: Pin key parsing and validation
- `config.py`: **Namespace-marker** configuration (`fxtr_namespace.toml`,
  reserved-prefix/worktree settings, `TsvNamespaceConfig`, `load_config`,
  `init_namespace`/`namespace_exists`). Also `load_namespace_marker` →
  `TsvNamespaceMarker`: a **marker-only** parse (reserved_prefix + name/description
  from `fxtr_namespace.toml` alone, no `.fxtr/` discovery / VCS probing / local
  config) — for contexts with only the committed flake source, e.g. a pure/FOD
  nix build sandbox (the `namespace_export` / `entity_from_tsv_pin` writers). The
  **repo-store / VCS-discovery** layer
  (`RepoStoreConfig`, `find_vcs_root`/`find_fxtr_dir`, `.fxtr/` repo-config
  parsing, `load_repo_store_config`, `ensure_repo_config_initialized`,
  `FxtrConfigError`) lives in the sibling module
  `fxtr2.data.repo_storage_config`; `config.py` composes it and re-exports those
  names for back-compat. This split keeps `fxtr2.data.repo_storage` (the lower
  layer) importable without a circular import (`tsv_namespace → repo_storage →
  repo_storage_config`).
- `dirty.py`: Dirty mark management for batched writes (with mtime/checksum tracking)
- `sync.py`: TSV ↔ SQLite synchronization
- `tsv_namespace.py`: Main user-facing API implementing `IndexablePinningBackend` and `DeepFetchable`. Backend methods take no session lease; cross-arena methods still take a source-arena `src_lease`. `register_deep_fetch_source` from `TsvFxtrNamespace` uses `session.root_lease.clone()` as the key-lease.

The `fxtr-tsv-ns` CLI lives in `fxtr2/cli/tsv_ns_cli.py`.

## Key Concepts

### Namespace Structure

Configuration is layered across three levels:

1. **User-level** (`~/.fxtr/config.toml`): Global settings like VCS root detection
2. **Per-repo** (`.fxtr/config.toml`): Repo-wide settings shared across worktrees
3. **Per-namespace** (`fxtr_namespace.toml` + `fxtr_namespace.local.toml`): Namespace-specific settings

A tsv namespace consists of:

```
~/.fxtr/                          # User-level fxtr directory (optional)
└── config.toml                   # User-level config (optional, for VCS customization)

my-project/
├── .git/
├── .fxtr/                        # Sibling to VCS root, shared across worktrees
│   ├── .gitignore                # Ignores everything in this dir
│   ├── repo_backing_storage.sqlite  # SQLite store (default location)
│   └── fxtr_store_repo_config.toml  # Per-repo config: index_namespace_id
├── fxtr_namespace.toml               # Marker file, may contain config
├── fxtr_namespace.local.toml         # Gitignored, worktree-specific config (optional)
├── .gitignore                    # Ignores local files and dirty folder contents
├── root.fxtr.tsv                 # Default TSV for keys without path prefix (legacy: index.fxtr.tsv)
├── fxtr_namespace_indexes.tsv        # Index registration file (git-tracked, optional)
└── data/
    └── models.fxtr.tsv           # TSV for keys with "data/models:" prefix
```

### TSV File Format

TSV files are two-column files mapping keys to FxtrIDs:

```
encoder	dw5d7nxxxxxxx...
decoder	dn3k2myyyyyyy...
```

Requirements:
- Keys must be sorted lexicographically (unsorted files indicate bad git merges)
- Keys cannot contain tabs, newlines, or colons
- No header row
- Empty lines are ignored

### Pin Key Format

Pin keys map to TSV file locations:

| Key | TSV File | Key in TSV |
|-----|----------|------------|
| `mykey` | `index.fxtr.tsv` | `mykey` |
| `data/models:encoder` | `data/models.fxtr.tsv` | `encoder` |
| `foo/bar:baz` | `foo/bar.fxtr.tsv` | `baz` |

Rules:
- Keys with no colon go into the reserved prefix TSV (default: `index.fxtr.tsv`)
- Keys with exactly one colon: part before colon is the path, part after is the key
- Keys with more than one colon are an error
- The reserved prefix cannot be used as a path prefix (to avoid ambiguity)

### Repo entity store vs. tsv namespace

The tsv namespace is a **view** over a standalone **repo entity store**
(`fxtr2.data.repo_storage.RepoEntityStore`): repo-associated, by-`FxtrID` entity
storage (the artifact formerly known as the index namespace). A
`TsvNamespaceBackend` = a repo entity store + a worktree namespace + a committed
TSV-root marker. The by-id store is exposed via `tsv_ns.store` so consumers that
only need by-id reads/writes (e.g. `fxtr_nix_runner`) don't have to stand up the
full backend. Internally, `TsvNamespaceBackend` keeps `_index_ns` (the raw
`SQLiteNamespaceBackend` used by sync/indexing/import machinery) and wraps it in
a `RepoEntityStore` (`_store`, shares the session); by-id entity-bytes writes
(`pin_entity_from_arena`, `register_indexer`) go through the store.

### Namespaces

The tsv namespace uses two SQLite namespaces:

1. **Index namespace** (= the repo entity store): Accumulates all entities ever referenced by any git version. Shared across all worktrees of the same VCS root. ID stored as `index_namespace_id` in `.fxtr/fxtr_store_repo_config.toml` (format: `index:{uuid}`). Now lifted out as `RepoEntityStore`; `TsvNamespaceBackend` is a view over it.

2. **Worktree namespace**: Mirrors the current worktree's TSV state. One per worktree. Default ID is `worktree:{abspath}` where `{abspath}` is the resolved absolute path to the directory containing `fxtr_namespace.toml`. This can be overridden via `worktree_namespace_id` in `fxtr_namespace.local.toml`.

### Dirty Mark System

The dirty mark system allows batching writes to sqlite and lazily flushing to TSV files. This avoids O(n) TSV rewrites on every pin operation.

**TSV States:**
- **Clean** (default): The TSV file on disk is the source of truth. SQLite may be stale.
- **Dirty** (transient): SQLite is the source of truth. TSV file is out of date.

**Dirty state file structure:**
```
{name}.fxtr.tsv.dirty/
├── DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md  # Sentinel + warning message
├── dirtymark-{uuid1}                         # Process 1's dirty mark
├── dirtymark-{uuid2}                         # Process 2's dirty mark
├── dirtymark_flag                            # Set on every claim, for staleness detection
└── state.lock                                # State lock

{name}.fxtr.tsv.sync_lock                     # Sync lock (ephemeral - deleted after each use)
```

The sentinel file is the authoritative indicator of dirty state. Multiple processes can write to the same TSV via sqlite concurrently; the last one to flush writes the TSV file.

### Sync Detection

`DirtyMarkManager` tracks mtime, checksum, and sync timestamps for each TSV to avoid unnecessary syncs:

**State tracked per TSV (in memory):**
- `tsv_mtime`: mtime of TSV file after last sync
- `tsv_checksum`: SHA-256 hash of TSV content after last sync
- `reference_mtime`: filesystem timestamp captured after last sync (for race detection)
- `last_sync_time`: monotonic timestamp of last sync (for time-based staleness)

**Sync detection algorithm (in ensure_synced):**
1. If TSV is dirty (sentinel exists) → no sync needed (SQLite is authoritative)
2. If synced within staleness window → no sync needed (time-based optimization)
3. If TSV mtime unchanged from cached `tsv_mtime` → no sync needed
4. If TSV mtime changed:
   - If TSV mtime > cached `reference_mtime` → sync needed
   - If TSV mtime == cached `reference_mtime` → potential race, do checksum check
   - If TSV mtime < cached `reference_mtime` → shouldn't happen, but sync to be safe
5. For checksum check: compare to cached `tsv_checksum`; sync if different

The time-based staleness check (step 2) is controlled by `sync_staleness_threshold` parameter (default 100ms) and only applies to the read path (retrieve_pinned). The write path (pin_entity_from_arena) always does a full sync when first marking as dirty, bypassing all staleness checks.

**Sync locking:** The sync lock file (`{name}.fxtr.tsv.sync_lock`) is used for mutual exclusion during TSV↔SQLite sync operations. It is created when acquired and deleted while still holding the lock (via `delete_on_release=True`). Before deletion, its mtime is captured as `reference_mtime` - this uses the filesystem's mtime granularity for accurate race detection.

### Flush Backup System (Concurrent Edit Detection)

When flushing dirty state to TSV, the system detects if someone externally modified the TSV while it was dirty (e.g., via `git checkout` or manual edit). If detected, the old content is preserved in a backup file.

**Backup filename format:** `{name}.fxtr.tsv.backup-{YYYYMMDD-HHMMSS-ffffff}.tsv`

**Flush flow with backup:**
1. Call `dump_fn(temp_path)` to write new content to temp file inside dirty directory
2. If old TSV exists:
   - Move old TSV to backup location
   - Atomic rename temp → TSV
   - Compute checksum of backup and compare against cached checksum:
     - **Checksum matches**: Delete backup (no concurrent edit detected)
     - **Checksum differs**: Keep backup, log warning (concurrent edit detected!)
     - **No cached state**: Keep backup, log warning (unexpected state)
3. If old TSV doesn't exist:
   - Atomic rename temp → TSV (no backup needed)
4. Update mtime/checksum cache

This protects against silent data loss when concurrent edits occur.

### Orphaned Temp Directory Cleanup

During dirty directory cleanup, the system atomically renames the directory to `{name}.fxtr.tsv.dirty.removing-{uuid}` before deleting it. If a process crashes after the rename but before deletion, these orphaned directories are left behind.

`full_sync_state_to_git()` cleans up these orphaned directories by scanning for `*.removing-*` patterns and deleting them.

### Async File Locking

`AsyncFileLock` provides coordinated file locking that works both within a single process (using anyio.Lock) and across processes (using fcntl.flock).

Key features:
- **Python-level async lock per path**: Prevents intra-process races
- **OS-level flock**: Coordinates across processes
- **Inode verification**: Detects and retries if lock file was moved/replaced during wait
- **Optional create_if_not_exists=False**: Raises `LockFileNotFoundError` if file doesn't exist

## Module Details

### tsv_file.py

```python
class FxtrTSVError(Exception):
    """Error related to TSV file format or content."""

def validate_tsv_key(key: str) -> None:
    """Validate a key for use in a TSV file."""

def read_tsv(path: str | Path) -> dict[str, FxtrID]:
    """Read TSV file, validate sorted order, return dict of key -> FxtrID.

    Returns empty dict if file doesn't exist.
    Raises FxtrTSVError if file is unsorted or has invalid format.
    Accepts str or Path; str is faster (no resolve() call).
    """

def write_tsv(path: str | Path, entries: dict[str, FxtrID]) -> None:
    """Write entries to TSV atomically (temp file + rename).

    Entries are sorted by key before writing.
    Deletes file if entries is empty.
    Creates parent directories if needed.
    Accepts str or Path; str is faster (no resolve() call).
    """
```

### key_utils.py

```python
class FxtrKeyError(Exception):
    """Error related to pin key format or validation."""

def validate_pin_key(full_key: str, reserved_prefix: str = "index") -> None:
    """Validate a pin key, raising FxtrKeyError if invalid."""

def parse_pin_key(full_key: str, reserved_prefix: str = "index") -> tuple[str, str]:
    """Parse a full pin key into (rel_tsv_path, tsv_key).

    Examples:
        "mykey" -> ("index.fxtr.tsv", "mykey")
        "data/models:encoder" -> ("data/models.fxtr.tsv", "encoder")

    Returns relative TSV path as string (not Path) for performance.
    """

def make_full_key(rel_tsv_path: str, tsv_key: str, reserved_prefix: str = "index") -> str:
    """Construct a full pin key from TSV path and TSV key.

    This is the inverse of parse_pin_key.
    Accepts rel_tsv_path as string for performance.
    """
```

### lock.py

```python
class LockFileNotFoundError(Exception):
    """Raised when trying to acquire a lock on a non-existent file.

    Only raised when create_if_not_exists=False and file doesn't exist.
    """
    path: str | Path

class AsyncFileLock:
    """Global async file lock manager with inode verification."""

    @classmethod
    @asynccontextmanager
    async def acquire(
        cls,
        lock_path: str | Path,
        create_if_not_exists: bool = True
    ) -> AsyncIterator[None]:
        """Acquire exclusive file lock with inode verification.

        Args:
            lock_path: Path to the lock file (str is faster, no resolve() call)
            create_if_not_exists: If True (default), create file and parent
                directories if needed. If False, raise LockFileNotFoundError
                if the file doesn't exist.

        Usage:
            # Default: create file if needed
            async with AsyncFileLock.acquire(lock_path):
                # Holding lock
                ...

            # Require file to exist
            try:
                async with AsyncFileLock.acquire(mark_path, create_if_not_exists=False):
                    ...
            except LockFileNotFoundError:
                # File was removed
        """
```

### config.py

```python
class FxtrConfigError(Exception):
    """Error related to fxtr namespace configuration."""

@dataclass(frozen=True)
class RepoStoreConfig:
    """Store-only config subset, loadable standalone (no namespace marker)."""
    fxtr_dir: Path                # .fxtr directory path
    sqlite_store_path: Path       # Default: .fxtr/repo_backing_storage.sqlite
    store_namespace_id: str       # "index:{uuid}"; on-disk key stays index_namespace_id

@dataclass(frozen=True)
class TsvNamespaceConfig:
    """Parsed configuration for a tsv namespace.

    Composes a RepoStoreConfig (the store-only fields); fxtr_dir /
    sqlite_store_path / index_namespace_id are forwarded via properties so
    existing call sites keep working.
    """
    namespace_root: Path              # Directory containing fxtr_namespace.toml
    namespace_root_str: str           # Pre-resolved str(namespace_root) for performance
    reserved_prefix: str          # Default: "index"
    worktree_namespace_id: str    # Default: "worktree:{namespace_root}", or override from local config
    repo_store: RepoStoreConfig   # composed store-only config
    # properties: fxtr_dir, sqlite_store_path, index_namespace_id (→ repo_store)

async def load_repo_store_config(repo_path: Path, *, create: bool = False) -> RepoStoreConfig:
    """Load store-only config for a repo (discovers .fxtr/, optionally inits it).

    The entry point the standalone repo entity store uses — does NOT require a
    fxtr_namespace.toml marker. With create=True, mints .fxtr/ + repo config if absent.
    """

def ensure_repo_config_initialized(fxtr_dir: Path) -> None:
    """Create .fxtr/ + fxtr_store_repo_config.toml (fresh store ns id) + .gitignore
    if absent. Idempotent; shared by init_namespace and load_repo_store_config(create=True)."""

async def find_vcs_root(path: Path, user_config: dict | None = None) -> Path:
    """Find VCS root directory.

    Priority:
    1. If vcs_root_command is set in user config, run it
    2. Otherwise, use git rev-parse --git-common-dir → parent dir

    The custom command must output an absolute path.
    """

async def find_fxtr_dir(namespace_root: Path, user_config: dict | None = None) -> Path:
    """Find .fxtr directory (sibling to VCS root)."""

async def load_config(namespace_root: Path) -> TsvNamespaceConfig:
    """Load configuration for an existing tsv namespace.

    Reads from all config layers:
    - ~/.fxtr/config.toml (user-level, optional)
    - .fxtr/fxtr_store_repo_config.toml (per-repo)
    - fxtr_namespace.toml (store)
    - fxtr_namespace.local.toml (store local, optional)

    Worktree namespace defaults to "worktree:{namespace_root}".
    Can be overridden via worktree_namespace_id in fxtr_namespace.local.toml.
    Does NOT create fxtr_namespace.local.toml automatically.
    """

async def init_namespace(namespace_root: Path, *, reserved_prefix: str = "index", ...) -> None:
    """Initialize a new tsv namespace.

    Creates fxtr_namespace.toml, .gitignore, and .fxtr/fxtr_store_repo_config.toml.
    Raises FxtrConfigError if a namespace already exists or VCS detection fails.
    """

def namespace_exists(namespace_root: Path) -> bool:
    """Check if fxtr_namespace.toml exists at the path."""
```

**VCS Root Detection:**

The module supports non-git VCS through configurable root detection:

- **Default (git)**: Runs `git rev-parse --git-common-dir` and returns its parent directory
- **Custom command**: Set `vcs_root_command` in `~/.fxtr/config.toml` as a list of args (e.g., `["hg", "root"]`)
- **Explicit path**: Set `repo_config_path` in `fxtr_namespace.local.toml` to bypass VCS detection entirely

If VCS detection fails and no override is configured, an error is raised with suggestions for resolution.

### dirty.py

```python
class ClaimResult(Enum):
    SYNCED = auto()        # First to go dirty, sync_if_first was called
    ACQUIRED = auto()      # Sentinel existed, mark created
    ALREADY_HELD = auto()  # We already had a mark (fast path)

class FlushResult(Enum):
    COMPLETE = auto()   # Successfully flushed
    NO_MARK = auto()    # We didn't have a dirty mark
    DEFERRED = auto()   # Other writers exist, removed our mark, let them flush

@dataclass
class TsvSyncState:
    """Cached state for sync detection on a single TSV file."""
    tsv_mtime: float         # mtime of TSV after last sync
    tsv_checksum: str        # SHA-256 hash of TSV content after last sync
    sync_lock_mtime: float   # mtime of sync lock after we last touched it

class DirtyMarkManager:
    """Manages dirty marks and sync state for a TsvNamespaceBackend instance.

    The primary APIs are:
    - hold_dirty(): Claim a dirty mark for writing to SQLite
    - flush(): Write SQLite state back to TSV
    - ensure_synced(): Ensure TSV → SQLite is current before reads
    - discover_tsvs_needing_sync(): Find TSVs that may have changed on disk
    """

    manager_uuid: UUID                      # Unique ID for this manager
    _store_root: str                        # Store root directory (pre-resolved)
    _tsv_state: dict[str, TsvSyncState]     # Cached mtime/checksum per TSV

    @asynccontextmanager
    async def hold_dirty(
        self,
        tsv_path: str,
        sync_if_first: Callable[[], Awaitable[None]],
    ) -> AsyncIterator[ClaimResult]:
        """Claim and hold a dirty mark for writing.

        This is the primary API for writers. Atomically claims a mark
        (calling sync_if_first if first) and holds a lock on it.

        If the mark is externally removed, automatically re-claims and retries.

        Usage:
            async with dirty_manager.hold_dirty(tsv_path, sync_callback) as result:
                await write_to_sqlite()  # Protected from force-removal
        """

    def may_have_dirty_mark(self, tsv_path: str) -> bool:
        """Check if we might have a dirty mark (conservative in-memory check)."""

    def is_dirty(self, tsv_path: str) -> bool:
        """Check if TSV is dirty (sentinel file exists)."""

    async def ensure_synced(
        self,
        tsv_path: str,
        sync_fn: Callable[[], Awaitable[None]],
    ) -> bool:
        """Ensure TSV is synced to SQLite, running sync_fn if needed.

        This is the primary API for reads. Returns True if sync was performed.
        """

    def discover_tsvs_needing_sync(self) -> list[str]:
        """Scan for TSV files that may need syncing.

        Returns absolute TSV paths (as strings) that are new, possibly modified,
        or deleted (in cache but missing).
        """

    async def flush(
        self,
        tsv_path: str,
        dump_fn: Callable[[str], Awaitable[None]],
        force: bool = False,
    ) -> FlushResult:
        """Flush dirty TSV to disk. dump_fn may be called multiple times.

        dump_fn receives a temp file path to write to. The dirty manager handles
        the atomic swap from temp to TSV, with backup protection for concurrent edits.

        If force=True, removes other marks (waiting for active writers to finish)
        before flushing. Used by force-flush CLI command.

        Updates mtime/checksum cache after successful flush.
        """

    def get_dirty_tsvs(self) -> set[str]:
        """Get all TSV paths we may have dirty marks for (conservative)."""

def dirty_dir_for_tsv(tsv_path: str) -> str:
    """Get the dirty directory path for a TSV file.

    Simply appends .dirty to the path (e.g., foo.fxtr.tsv.dirty).
    """

def sync_lock_path_for_tsv(tsv_path: str) -> str:
    """Get the sync lock path for a TSV file.

    Simply appends .sync_lock to the path (e.g., foo.fxtr.tsv.sync_lock).
    """
```

### sync.py

```python
class FxtrSyncError(Exception):
    """Error during sync between TSV and SQLite.

    Raised when entities referenced in TSV files are missing from the index namespace.
    """

async def sync_tsv_to_sqlite(
    tsv_path: str,
    config: TsvNamespaceConfig,
    worktree_ns: SQLiteNamespaceBackend,
    index_ns: SQLiteNamespaceBackend,
) -> None:
    """Sync a single TSV file into the worktree namespace.

    Reads TSV (empty dict if missing), verifies entities exist in index_ns,
    creates/updates pins in worktree_ns, and removes pins not in TSV.

    Raises FxtrSyncError if any entities are missing from index namespace.
    tsv_path must be an absolute path (as string for performance).
    """

async def dump_sqlite_to_tsv(
    tsv_path: str,
    dest_path: str,
    config: TsvNamespaceConfig,
    worktree_ns: SQLiteNamespaceBackend,
) -> None:
    """Dump worktree namespace pins to a file.

    Queries pins matching the TSV's key prefix and writes them to dest_path.
    The caller is responsible for atomicity (e.g., via dirty manager backup system).
    Writes nothing to dest_path if no matching pins.
    Both paths must be absolute strings for performance.
    """

async def ensure_tsv_synced(
    tsv_path: str,
    config: TsvNamespaceConfig,
    dirty_manager: DirtyMarkManager,
    worktree_ns: SQLiteNamespaceBackend,
    index_ns: SQLiteNamespaceBackend,
) -> bool:
    """Ensure a single TSV is synced to SQLite.

    Uses dirty_manager.ensure_synced() for mtime/checksum checking and locking.
    Returns True if sync was performed.
    tsv_path must be an absolute path (as string for performance).
    """

async def ensure_full_sync(
    config: TsvNamespaceConfig,
    dirty_manager: DirtyMarkManager,
    worktree_ns: SQLiteNamespaceBackend,
    index_ns: SQLiteNamespaceBackend,
) -> None:
    """Ensure all TSVs are synced to SQLite.

    Discovers TSVs needing sync (new, modified, deleted) and also checks
    for orphan pins whose TSVs no longer exist. Syncing a non-existent TSV
    removes all pins with that prefix, handling orphan cleanup.
    """

async def flush_tsv_to_disk(
    tsv_path: str,
    config: TsvNamespaceConfig,
    dirty_manager: DirtyMarkManager,
    worktree_ns: SQLiteNamespaceBackend,
) -> FlushResult:
    """Flush a dirty TSV from SQLite to disk.

    Wraps dirty_manager.flush() with the actual dump logic.
    tsv_path must be an absolute path (as string for performance).
    """
```

**Key design note:** For the reserved prefix TSV (e.g., `index.fxtr.tsv`), keys have no colon. When querying pins for this TSV, we query all pins and filter to those without colons.

### tsv_namespace.py

```python
class TsvNamespaceBackend(IndexablePinningBackend, DeepFetchable):
    """TSV-file fxtr namespace.

    The main user-facing API for the tsv namespace. Wraps git-tracked TSV files
    and SQLite namespaces to provide persistent, version-controlled entity storage.
    Leased APIs mirror ``SQLiteNamespaceBackend`` (first parameter after ``self`` is a
    ``Lease`` validated against the open SQLite session).
    """

    @classmethod
    @asynccontextmanager
    async def open(
        cls,
        namespace_root: Path,
        *,
        auto_flush_seconds: float | None = 10.0,
        full_sync_staleness_threshold: float | None = 5.0,
        skip_sync: bool = False,
    ) -> AsyncIterator[TsvNamespaceBackend]:
        """Open a tsv namespace at the given namespace root.

        Args:
            namespace_root: Path containing fxtr_namespace.toml
            auto_flush_seconds: Periodic flush interval (None to disable)
            full_sync_staleness_threshold: Skip full sync if done within this time
            skip_sync: Skip initial sync (for import scenarios with missing backing store)
        """

    @classmethod
    async def init(
        cls,
        namespace_root: Path,
        *,
        reserved_prefix: str = "index",
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        """Initialize a new tsv namespace at the given path."""

    # PersistentPinningBackend / DeepFetchable (no backend lease; source-arena lease preserved)
    async def pin_entity_from_arena(
        self,
        key: str,
        entity_id: FxtrID,
        source: ReadableArena,
        src_lease: Lease,
        *,
        overwrite: bool = False,
    ) -> None:
        """Pin an entity. Copies to index namespace first, then writes to worktree."""

    async def unpin_entity(self, key: str) -> None:
        """Unpin an entity. Keeps entity in index namespace for git history."""

    async def retrieve_pinned(self, key: str) -> tuple[FxtrID, ReadableArena]:
        """Retrieve a pinned entity. Syncs TSV if needed."""

    async def list_pins(self, prefix: str = "") -> dict[str, FxtrID]:
        """List pins matching prefix. Does full sync if stale."""

    async def try_lookup(self, node_id: FxtrID) -> ReadableArena | None:
        """Deep-fetch: look up an entity. Does full sync if stale."""

    # Additional methods
    @property
    def store(self) -> RepoEntityStore:
        """The backing repo entity store (by-id facade over the index namespace,
        shares this backend's session). Hand to consumers needing by-id reads/writes."""

    async def flush(self, *, defer_to_other_writers: bool = False) -> None:
        """Flush all TSVs we have dirty marks for."""

    async def sync_from_git(self) -> None:
        """Sync TSV files to SQLite (git → SQLite). Used after import."""

    async def full_sync_state_to_git(self) -> None:
        """Force-flush ALL dirty TSVs (for CLI flush-to-tsv)."""

    async def export_archive(self, output_path: Path, *, tar: bool = True, gz: bool = False) -> None:
        """Flush TSVs, sync, then delegate to SQLiteNamespaceBackend.export_archive."""

    async def import_archive_into_backing_store(self, archive_path: Path) -> None:
        """Import entities into the index (backing) namespace. Handles tar/sqlite detection."""

    @property
    def config(self) -> TsvNamespaceConfig:
        """Get the tsv namespace configuration."""
```

**Usage example:**

```python
from fxtr2.data.tsv_namespace import TsvNamespaceBackend

# Initialize a new tsv namespace (one-time)
await TsvNamespaceBackend.init(Path("/path/to/project"))

# Open and use the namespace
async with TsvNamespaceBackend.open(Path("/path/to/project")) as ns:
    # Pin an entity
    await ns.pin_entity_from_arena("models:encoder", entity_id, memory_arena)

    # Retrieve a pinned entity
    entity_id, arena = await ns.retrieve_pinned("models:encoder")

    # List all pins
    pins = await ns.list_pins()

    # Explicit flush (optional - happens automatically on close)
    await ns.flush()
```

**Key behaviors:**

- **Pin/unpin writes go to SQLite immediately**, TSV files are updated lazily via flush
- **Retrieve checks dirty state first** - if dirty, reads from SQLite; if clean, syncs from TSV
- **Auto-flush runs periodically** (default 10s) with `defer_to_other_writers=True`
- **Full sync happens on open** and periodically for `list_pins` / `try_lookup`
- **Index namespace accumulates all entities** for git history support
- **Worktree namespace mirrors current TSV state**

## CLI Interface

The `fxtr-tsv-ns` CLI provides commands for managing tsv namespaces. It is implemented in `fxtr2/cli/tsv_ns_cli.py`.

### Commands

```bash
# Initialize a new tsv namespace
fxtr-tsv-ns init [--reserved-prefix PREFIX] [--name NAME] [--description DESC] PATH

# Sync TSV files into sqlite (git -> sqlite)
# Use after checkout/pull to pre-warm the sqlite store
fxtr-tsv-ns sync-from-tsv PATH

# Flush all dirty TSVs to disk (sqlite -> git)
# Use before committing or switching branches
fxtr-tsv-ns flush-to-tsv PATH

# Export current state to tarball (default, flushes first)
fxtr-tsv-ns export PATH OUTPUT_FILE.tar
fxtr-tsv-ns export PATH OUTPUT_FILE.tar.gz --gz

# Export as raw sqlite + mdb files
fxtr-tsv-ns export PATH OUTPUT_FILE.sqlite --no-tar

# Import archive (auto-detects .tar, .tar.gz, or .sqlite)
fxtr-tsv-ns import PATH ARCHIVE_FILE

# List all pins (optionally filter by prefix)
fxtr-tsv-ns list [--prefix PREFIX] PATH

# Show namespace status (namespace IDs, dirty TSVs, etc.)
fxtr-tsv-ns status PATH
```

### Command Details

**`init`**: Creates `fxtr_namespace.toml`, `.gitignore`, and `.fxtr/` configuration directory. Requires the path to be inside a git repository (or VCS with configured root detection).

**`sync-from-tsv`**: Opens the namespace and performs a full sync from TSV files to SQLite. Skips dirty TSVs (SQLite is already authoritative). Use after `git checkout` or `git pull`.

**`flush-to-tsv`**: Force-flushes ALL dirty TSVs to disk, removing all dirty marks. Also cleans up orphaned temp directories from crashed processes. Use before `git commit` or branch switches.

**`export`**: Exports all pinned entities to an archive. By default produces a tarball (`store.sqlite` + `store.sqlite.data1.mdb`); use `--no-tar` for raw files, `--gz` for gzip compression. LMDB data is compacted during export (empty pages stripped). Flushes dirty TSVs first.

**`import`**: Imports entities from an archive into the index namespace. Auto-detects format by extension: `.tar`, `.tar.gz`/`.tgz` for tarballs, `.sqlite` for raw sqlite. Use to restore missing entities that are referenced by TSV files.

**`list`**: Lists all pins in the namespace. With `--prefix`, filters to pins matching that prefix. Outputs one line per pin: `key -> fxtr_id`.

**`status`**: Shows the namespace configuration and lists any dirty TSV files. Useful for checking if the namespace needs flushing before commit.

## Testing

Tests are in `tests/test_tsv_namespace/` (325 tests total):
- `test_tsv_file.py`: TSV reading, writing, validation (31 tests)
- `test_key_utils.py`: Key parsing and validation (33 tests)
- `test_lock.py`: Async locking, multiprocess locking (14 tests)
- `test_config.py`: Config parsing, git directory detection, init/load (37 tests)
- `test_dirty.py`: Dirty mark claiming, flushing, backup system (44 tests)
- `test_sync.py`: TSV ↔ SQLite sync functions (24 tests)
- `test_tsv_namespace.py`: TsvNamespaceBackend integration tests, concurrent access (31 tests)
- `test_cli.py`: CLI commands and error handling (33 tests)
- `test_index_file.py`: Index registration file I/O (21 tests)
- `test_index_sync.py`: Index registration sync logic (18 tests)
- `test_tsv_namespace_indexing.py`: TsvNamespaceBackend indexing integration (11 tests)
- `test_tsv_namespace_staleness.py`: Staleness cache tests (28 tests)

Run tests with:
```bash
uv run --group test pytest tests/test_tsv_namespace/ -v
```
