"""TSV-file fxtr namespace implementation.

This module provides a tsv namespace backend for fxtr, allowing pin name -> id
mappings to be version-controlled alongside code as git-tracked TSV files, while
the actual entity data lives in a repo-level entity store (SQLite).

Main API:
- TsvNamespaceBackend: The main class for working with tsv namespaces
- TsvNamespaceBackend.init(): Initialize a new tsv namespace
- TsvNamespaceBackend.open(): Open an existing tsv namespace

Supporting utilities:
- read_tsv, write_tsv: Read/write TSV pin mapping files
- FxtrTSVError: Exception for TSV-related errors
- parse_pin_key, validate_pin_key, make_full_key: Pin key utilities
- FxtrKeyError: Exception for key-related errors
- async_file_lock_acquire, LockFileNotFoundError: Async file locking with inode verification
- TsvNamespaceConfig, load_config, init_namespace, namespace_exists: Config management
- FxtrConfigError: Exception for config-related errors
- DirtyMarkManager, ClaimResult, FlushResult: Dirty mark management
- FxtrSyncError: Exception for sync errors (missing entities)
"""

from fxtr2.data.file_util.lock import LockFileNotFoundError, async_file_lock_acquire
from fxtr2.data.tsv_namespace.config import (
    FxtrConfigError,
    TsvNamespaceConfig,
    TsvNamespaceMarker,
    init_namespace,
    load_config,
    load_namespace_marker,
    namespace_exists,
)
from fxtr2.data.tsv_namespace.dirty import (
    ClaimResult,
    DirtyMarkManager,
    FlushResult,
    dirty_dir_for_tsv,
    sync_lock_path_for_tsv,
)
from fxtr2.data.tsv_namespace.key_utils import (
    FxtrKeyError,
    make_full_key,
    parse_pin_key,
    validate_pin_key,
)
from fxtr2.data.tsv_namespace.sync import FxtrSyncError
from fxtr2.data.tsv_namespace.tsv_file import FxtrTSVError, read_tsv, validate_tsv_key, write_tsv
from fxtr2.data.tsv_namespace.tsv_namespace import TsvNamespaceBackend

__all__ = [
    # Main API (Phase 4)
    "TsvNamespaceBackend",
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
    "TsvNamespaceConfig",
    "TsvNamespaceMarker",
    "load_config",
    "load_namespace_marker",
    "init_namespace",
    "namespace_exists",
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
