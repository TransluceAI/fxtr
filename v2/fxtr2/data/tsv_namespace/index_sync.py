"""Index registration sync between the git-tracked index file and worktree namespace.

This module provides functions for comparing the state of the git-tracked
index registration file (fxtr_namespace_indexes.tsv) against the live worktree
namespace state, and applying diffs in either direction.

All functions that read or write the index file should be called under the
file lock (callers are responsible for acquiring AsyncFileLock on the index
file path). This serializes all index state operations and eliminates races
between concurrent sync and register/unregister.
"""

from __future__ import annotations

import logging
from typing import Any

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.sqlite_store import SQLiteFxtrSession, SQLiteNamespaceBackend

from .index_file import read_index_file, write_index_file
from .sync import FxtrSyncError

logger = logging.getLogger(__name__)


async def get_live_registrations(
    worktree_ns: SQLiteNamespaceBackend[Any],
    session: SQLiteFxtrSession[Any],
) -> frozenset[tuple[FxtrID, str]]:
    """Get the current indexer registrations from the live worktree namespace.

    Calls worktree_ns.list_indexers() and converts the result to a frozenset
    of (indexer_id, key_prefix) pairs.

    Args:
        worktree_ns: The worktree SQLite namespace backend.

    Returns:
        Frozenset of (indexer_id, key_prefix) pairs currently registered.
    """
    registrations, arena = await worktree_ns.list_indexers()
    arena.close()
    return frozenset((indexer_id, prefix) for indexer_id, prefix in registrations)


async def remove_dead_index_registrations(
    index_file_path: str,
    worktree_ns: SQLiteNamespaceBackend[Any],
    session: SQLiteFxtrSession[Any],
) -> None:
    """Remove indexer registrations that are in worktree_ns but not in the file.

    This is Phase 1 of full sync: remove dead indexers before pin sync so
    they don't run on newly synced pins.

    Must be called under the file lock.

    Args:
        index_file_path: Absolute path to the index registration file.
        worktree_ns: The worktree SQLite namespace backend.
    """
    file_regs = read_index_file(index_file_path)
    live_regs = await get_live_registrations(worktree_ns, session)

    dead = live_regs - file_regs
    for indexer_id, key_prefix in dead:
        logger.info(
            f"Removing dead indexer registration: {indexer_id.id_string()} prefix={key_prefix!r}"
        )
        await worktree_ns.unregister_indexer(indexer_id, key_prefix)


async def sync_index_registrations(
    index_file_path: str,
    worktree_ns: SQLiteNamespaceBackend[Any],
    index_ns: SQLiteNamespaceBackend[Any],
    session: SQLiteFxtrSession[Any],
) -> None:
    """Full bidirectional sync of index registrations.

    Compares file state vs live worktree_ns state and applies diffs:
    - Removes registrations in worktree_ns but not in the file (dead)
    - Adds registrations in the file but not in worktree_ns (new)

    For new registrations, the indexer entity is retrieved from the
    backing (index) namespace. If missing, raises FxtrSyncError.

    Must be called under the file lock.

    Args:
        index_file_path: Absolute path to the index registration file.
        worktree_ns: The worktree SQLite namespace backend.
        index_ns: The index (backing) SQLite namespace backend.

    Raises:
        FxtrSyncError: If indexer entities for new registrations are missing
            from the backing store.
    """
    lease = session.root_lease
    file_regs = read_index_file(index_file_path)
    live_regs = await get_live_registrations(worktree_ns, session)

    # Add new registrations
    new = file_regs - live_regs

    missing: list[str] = []
    for indexer_id, key_prefix in new:
        # Retrieve indexer entity from backing store
        try:
            _, arena = await index_ns.retrieve_pinned(indexer_id.id_string())
        except KeyError:
            missing.append(indexer_id.id_string())
            continue

        try:
            logger.info(
                f"Adding indexer registration from file: {indexer_id.id_string()} "
                f"prefix={key_prefix!r}"
            )
            await worktree_ns.register_indexer(indexer_id, arena, lease.clone(), key_prefix)
        finally:
            arena.close()

    if missing:
        raise FxtrSyncError(
            f"Indexer entities missing from backing store during sync: {missing}\n\n"
            "These indexers are referenced in fxtr_namespace_indexes.tsv but their "
            "entities are not in the index namespace. Try importing an archive "
            "that contains these entities."
        )

    # Remove dead registrations
    dead = live_regs - file_regs
    for indexer_id, key_prefix in dead:
        logger.info(
            f"Removing dead indexer registration: {indexer_id.id_string()} prefix={key_prefix!r}"
        )
        await worktree_ns.unregister_indexer(indexer_id, key_prefix)


async def add_registration_to_file(
    index_file_path: str,
    indexer_id: FxtrID,
    key_prefix: str,
) -> None:
    """Add a single registration to the index file.

    Reads the current file, adds the entry, and writes the sorted result.
    Must be called under the file lock.

    Args:
        index_file_path: Absolute path to the index registration file.
        indexer_id: The indexer entity ID.
        key_prefix: The key prefix for the registration.
    """
    current = read_index_file(index_file_path)
    updated = current | {(indexer_id, key_prefix)}
    write_index_file(index_file_path, updated)


async def remove_registration_from_file(
    index_file_path: str,
    indexer_id: FxtrID,
    key_prefix: str,
) -> None:
    """Remove a single registration from the index file.

    Reads the current file, removes the entry, and writes the sorted result.
    Must be called under the file lock.

    Args:
        index_file_path: Absolute path to the index registration file.
        indexer_id: The indexer entity ID.
        key_prefix: The key prefix for the registration.
    """
    current = read_index_file(index_file_path)
    updated = current - {(indexer_id, key_prefix)}
    write_index_file(index_file_path, updated)
