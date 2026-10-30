"""Sync logic between TSV files and SQLite for git-backed fxtr stores.

This module provides functions to synchronize data between TSV files (the git-tracked
source of truth when clean) and SQLite (the authoritative store when dirty).

Sync directions:
- TSV → SQLite: Used when reading from a clean TSV, to populate SQLite with current state
- SQLite → TSV: Used when flushing dirty state back to disk

The DirtyMarkManager handles all locking and change detection. This module provides
the actual data transfer logic.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fxtr2.data.entity_core import FxtrID

from .config import GitStoreConfig
from .dirty import DirtyMarkManager, FlushResult
from .key_utils import parse_pin_key
from .tsv_file import read_tsv, write_tsv_content

if TYPE_CHECKING:
    from fxtr2.data.sqlite_store import SQLiteNamespaceBackend


logger = logging.getLogger(__name__)


class FxtrSyncError(Exception):
    """Error during sync between TSV and SQLite.

    Raised when entities referenced in TSV files are missing from the index namespace.
    """


def _get_tsv_path_prefix(tsv_path: Path, store_root: Path) -> str:
    """Extract the path prefix from a TSV file path.

    For example:
    - index.fxtr.tsv -> "index"
    - data/models.fxtr.tsv -> "data/models"

    Args:
        tsv_path: Absolute path to the TSV file.
        store_root: Absolute path to the store root.

    Returns:
        The path prefix (what would appear before the colon in a full key,
        or the reserved prefix for keys without colons).
    """
    # Get relative path from store root
    rel_path = tsv_path.relative_to(store_root)

    # Extract path prefix from TSV name
    suffix = ".fxtr.tsv"
    stem = rel_path.name[: -len(suffix)]

    if rel_path.parent == Path("."):
        return stem
    else:
        return str(rel_path.parent / stem)


def _compute_pin_prefix_for_tsv(tsv_path: Path, config: GitStoreConfig) -> str | None:
    """Compute the pin key prefix for querying pins in a TSV file.

    For non-reserved TSVs (e.g., data/models.fxtr.tsv), returns "data/models:"
    which can be used with list_pins() prefix filtering.

    For the reserved prefix TSV (e.g., index.fxtr.tsv), returns None because
    those keys have no colon and can't be prefix-filtered. Callers should
    query all pins and filter to those without colons.

    Args:
        tsv_path: Absolute path to the TSV file.
        config: Store configuration.

    Returns:
        The prefix string for list_pins(), or None for the reserved prefix TSV.
    """
    path_prefix = _get_tsv_path_prefix(tsv_path, config.store_root)

    if path_prefix == config.reserved_prefix:
        # Reserved prefix - keys have no colon, need special handling
        return None
    else:
        # Regular TSV - keys have prefix followed by colon
        return f"{path_prefix}:"


async def _get_pins_for_tsv(
    tsv_path: Path,
    config: GitStoreConfig,
    worktree_ns: SQLiteNamespaceBackend,
) -> dict[str, FxtrID]:
    """Get all pins from SQLite that belong to a specific TSV file.

    Args:
        tsv_path: Absolute path to the TSV file.
        config: Store configuration.
        worktree_ns: The worktree namespace to query.

    Returns:
        Dict mapping full pin keys to FxtrIDs for all pins belonging to this TSV.
    """
    pin_prefix = _compute_pin_prefix_for_tsv(tsv_path, config)

    if pin_prefix is None:
        # Reserved prefix TSV - query all and filter to keys without colons
        all_pins = await worktree_ns.list_pins("")
        return {k: v for k, v in all_pins.items() if ":" not in k}
    else:
        # Regular TSV - use prefix filter
        return await worktree_ns.list_pins(pin_prefix)


async def sync_tsv_to_sqlite(
    tsv_path: Path,
    config: GitStoreConfig,
    worktree_ns: SQLiteNamespaceBackend,
    index_ns: SQLiteNamespaceBackend,
) -> None:
    """Sync a single TSV file into the worktree namespace.

    Reads the TSV file (or empty dict if missing), computes the full pin keys,
    and updates the worktree namespace to match. For each entry:
    - If entity exists in index namespace, create/update pin in worktree namespace
    - If entity missing from index namespace, raises FxtrSyncError

    Also removes pins that exist in worktree namespace but not in TSV.

    Args:
        tsv_path: Absolute path to the TSV file.
        config: Store configuration.
        worktree_ns: The worktree namespace to update.
        index_ns: The index namespace (for entity verification).

    Raises:
        FxtrSyncError: If any entities referenced in TSV are missing from index namespace.
    """
    tsv_path = tsv_path.resolve()

    # Read TSV (empty dict if missing)
    tsv_entries = read_tsv(tsv_path)

    # Get the path prefix for constructing full keys
    path_prefix = _get_tsv_path_prefix(tsv_path, config.store_root)

    # Determine if this is the reserved prefix TSV
    is_reserved = path_prefix == config.reserved_prefix

    # Convert TSV entries to full keys
    # For reserved prefix: full_key = tsv_key (no colon)
    # For others: full_key = "path_prefix:tsv_key"
    tsv_full_keys: dict[str, FxtrID] = {}
    for tsv_key, fxtr_id in tsv_entries.items():
        if is_reserved:
            full_key = tsv_key
        else:
            full_key = f"{path_prefix}:{tsv_key}"
        tsv_full_keys[full_key] = fxtr_id

    # Get current pins from worktree namespace for this TSV
    current_pins = await _get_pins_for_tsv(tsv_path, config, worktree_ns)

    logger.debug(
        f"sync_tsv_to_sqlite({tsv_path}): got {len(current_pins)} sqlite pins,"
        f" {len(tsv_full_keys)} tsv pins"
    )

    # Verify all entities exist in index namespace and pin them
    missing_entities: list[tuple[str, FxtrID]] = []
    for full_key, fxtr_id in tsv_full_keys.items():
        try:
            retrieved_id, arena = await index_ns.retrieve_pinned(fxtr_id.id_string())
            try:
                if retrieved_id != fxtr_id:
                    raise FxtrSyncError(
                        f"Retrieved ID {retrieved_id.id_string()} does not match expected ID"
                        f" {fxtr_id.id_string()}! Index namespace appears to be corrupted."
                    )
                # Pin the entity (copies from index to worktree if needed)
                await worktree_ns.pin_entity_from_arena(full_key, fxtr_id, arena, overwrite=True)
            finally:
                arena.close()
            logger.debug(
                f"sync_tsv_to_sqlite({tsv_path}): pinned {full_key} -> {fxtr_id.id_string()}"
            )
        except KeyError:
            missing_entities.append((full_key, fxtr_id))

    if missing_entities:
        # Build error message
        details = "\n".join(
            f"  {key} -> {fxtr_id.id_string()}" for key, fxtr_id in missing_entities
        )
        raise FxtrSyncError(
            "Missing entities in index namespace\n\n"
            "The following entities are referenced in TSV files but not found in the\n"
            "index namespace:\n\n"
            f"{details}\n\n"
            "To restore consistency, import an archive containing these entities:\n\n"
            f"  fxtr-git-store import {config.store_root} archive.sqlite\n\n"
            "Then retry the operation."
        )

    # Remove pins that are in worktree but not in TSV
    for full_key in current_pins:
        if full_key not in tsv_full_keys:
            await worktree_ns.unpin_entity(full_key)
            logger.debug(f"sync_tsv_to_sqlite({tsv_path}): unpinned {full_key}")


async def dump_sqlite_to_tsv(
    tsv_path: Path,
    dest_path: Path,
    config: GitStoreConfig,
    worktree_ns: SQLiteNamespaceBackend,
) -> None:
    """Dump worktree namespace pins to a file.

    Queries all pins matching the TSV's key prefix and writes them to dest_path.
    The caller is responsible for atomicity (e.g., via dirty manager backup system).

    If no pins match the TSV, writes nothing to dest_path (caller handles deletion).

    Args:
        tsv_path: Absolute path to the TSV file (used to determine key prefix).
        dest_path: Path to write the content to (typically a temp file).
        config: Store configuration.
        worktree_ns: The worktree namespace to read from.
    """
    tsv_path = tsv_path.resolve()

    # Get all pins for this TSV
    full_key_pins = await _get_pins_for_tsv(tsv_path, config, worktree_ns)

    logger.debug(
        f"dump_sqlite_to_tsv({tsv_path}): dumping {len(full_key_pins)} pins to {dest_path}"
    )

    # Convert full keys to TSV keys
    tsv_entries: dict[str, FxtrID] = {}
    for full_key, fxtr_id in full_key_pins.items():
        # Use parse_pin_key to extract the TSV key
        _, tsv_key = parse_pin_key(full_key, config.reserved_prefix)
        tsv_entries[tsv_key] = fxtr_id

    # Write TSV content (caller handles atomicity and deletion)
    write_tsv_content(dest_path, tsv_entries)


async def ensure_tsv_synced(
    tsv_path: Path,
    config: GitStoreConfig,
    dirty_manager: DirtyMarkManager,
    worktree_ns: SQLiteNamespaceBackend,
    index_ns: SQLiteNamespaceBackend,
) -> bool:
    """Ensure a single TSV is synced to SQLite.

    Uses dirty_manager.ensure_synced() to check if sync is needed and
    acquire appropriate locks. If sync is needed, calls sync_tsv_to_sqlite().

    Args:
        tsv_path: Absolute path to the TSV file.
        config: Store configuration.
        dirty_manager: The dirty mark manager.
        worktree_ns: The worktree namespace to update.
        index_ns: The index namespace (for entity verification).

    Returns:
        True if sync was performed, False if skipped.

    Raises:
        FxtrSyncError: If sync was performed but entities were missing.
    """
    tsv_path = tsv_path.resolve()

    async def sync_callback() -> None:
        await sync_tsv_to_sqlite(tsv_path, config, worktree_ns, index_ns)

    return await dirty_manager.ensure_synced(tsv_path, sync_callback)


async def ensure_full_sync(
    config: GitStoreConfig,
    dirty_manager: DirtyMarkManager,
    worktree_ns: SQLiteNamespaceBackend,
    index_ns: SQLiteNamespaceBackend,
) -> None:
    """Ensure all TSVs are synced to SQLite.

    Discovers TSVs needing sync via dirty_manager (new, modified, deleted files),
    and also checks for orphaned pins whose TSVs no longer exist.

    The algorithm:
    1. Query all existing pins from SQLite
    2. Compute the TSV path each pin should belong to
    3. Call discover_tsvs_needing_sync() for disk changes
    4. Merge both sets of TSV paths
    5. Sync each TSV via ensure_tsv_synced

    For TSV paths that don't exist on disk AND aren't dirty, syncing them
    will read empty content, causing sync_tsv_to_sqlite to remove all pins
    with that prefix (this handles orphaned pins from deleted TSVs).

    Args:
        config: Store configuration.
        dirty_manager: The dirty mark manager.
        worktree_ns: The worktree namespace to update.
        index_ns: The index namespace (for entity verification).

    Raises:
        FxtrSyncError: If any TSV references missing entities.
    """
    # Step 1-2: Get TSV paths implied by existing pins
    all_pins = await worktree_ns.list_pins("")
    pin_implied_tsvs: set[Path] = set()

    for full_key in all_pins:
        rel_tsv_path, _ = parse_pin_key(full_key, config.reserved_prefix)
        abs_tsv_path = (config.store_root / rel_tsv_path).resolve()
        pin_implied_tsvs.add(abs_tsv_path)

    logger.debug(f"ensure_full_sync: found {len(pin_implied_tsvs)} TSV implied by sqlite pins")

    # Step 3: Get TSVs that changed on disk
    disk_changed_tsvs = set(dirty_manager.discover_tsvs_needing_sync())
    logger.debug(f"ensure_full_sync: found {len(disk_changed_tsvs)} TSV changed on disk")

    # Step 4: Merge both sets
    all_tsvs_to_check = pin_implied_tsvs | disk_changed_tsvs

    # Step 5: Sync each TSV
    for tsv_path in all_tsvs_to_check:
        await ensure_tsv_synced(tsv_path, config, dirty_manager, worktree_ns, index_ns)


async def flush_tsv_to_disk(
    tsv_path: Path,
    config: GitStoreConfig,
    dirty_manager: DirtyMarkManager,
    worktree_ns: SQLiteNamespaceBackend,
) -> FlushResult:
    """Flush a dirty TSV from SQLite to disk.

    Wraps dirty_manager.flush() with the actual dump logic.

    Args:
        tsv_path: Absolute path to the TSV file.
        config: Store configuration.
        dirty_manager: The dirty mark manager.
        worktree_ns: The worktree namespace to read from.

    Returns:
        FlushResult indicating what happened:
        - COMPLETE: Successfully flushed
        - NO_MARK: We didn't have a dirty mark
        - DEFERRED: Other writers exist, let them flush
    """
    tsv_path = tsv_path.resolve()

    async def dump_callback(temp_path: Path) -> None:
        await dump_sqlite_to_tsv(tsv_path, temp_path, config, worktree_ns)

    return await dirty_manager.flush(tsv_path, dump_callback)
