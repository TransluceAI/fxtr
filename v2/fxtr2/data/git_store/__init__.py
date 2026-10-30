"""Git-backed fxtr namespace implementation.

This module provides a git-integrated storage backend for fxtr, allowing
entities to be version-controlled alongside code using TSV files for pin
mappings and SQLite for actual entity storage.

Main API:
- GitNamespaceBackend: The main class for working with git-backed stores
- GitNamespaceBackend.init(): Initialize a new store
- GitNamespaceBackend.open(): Open an existing store

Supporting utilities:
- read_tsv, write_tsv: Read/write TSV pin mapping files
- FxtrTSVError: Exception for TSV-related errors
- parse_pin_key, validate_pin_key, make_full_key: Pin key utilities
- FxtrKeyError: Exception for key-related errors
- async_file_lock_acquire, LockFileNotFoundError: Async file locking with inode verification
- GitStoreConfig, load_config, init_store, store_exists: Config management
- FxtrConfigError: Exception for config-related errors
- DirtyMarkManager, ClaimResult, FlushResult: Dirty mark management
- FxtrSyncError: Exception for sync errors (missing entities)
"""

from fxtr2.data.file_util.lock import LockFileNotFoundError, async_file_lock_acquire
from fxtr2.data.git_store.config import (
    FxtrConfigError,
    GitStoreConfig,
    init_store,
    load_config,
    store_exists,
)
from fxtr2.data.git_store.dirty import (
    ClaimResult,
    DirtyMarkManager,
    FlushResult,
    dirty_dir_for_tsv,
    sync_lock_path_for_tsv,
)
from fxtr2.data.git_store.git_namespace import GitNamespaceBackend
from fxtr2.data.git_store.key_utils import (
    FxtrKeyError,
    make_full_key,
    parse_pin_key,
    validate_pin_key,
)
from fxtr2.data.git_store.sync import FxtrSyncError
from fxtr2.data.git_store.tsv_file import FxtrTSVError, read_tsv, validate_tsv_key, write_tsv

__all__ = [
    # Main API (Phase 4)
    "GitNamespaceBackend",
    # TSV file operations
    "read_tsv",
    "write_tsv",
    "validate_tsv_key",
    "FxtrTSVError",
    # Key utilities
    "parse_pin_key",
    "validate_pin_key",
    "make_full_key",
    "FxtrKeyError",
    # Locking
    "async_file_lock_acquire",
    "LockFileNotFoundError",
    # Config
    "GitStoreConfig",
    "load_config",
    "init_store",
    "store_exists",
    "FxtrConfigError",
    # Dirty marks
    "DirtyMarkManager",
    "ClaimResult",
    "FlushResult",
    "dirty_dir_for_tsv",
    "sync_lock_path_for_tsv",
    # Sync
    "FxtrSyncError",
]
