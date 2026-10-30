"""Git-backed fxtr namespace implementation.

This module provides GitNamespaceBackend, a persistent storage backend that uses
TSV files for pin mappings (version-controlled in git) and SQLite for
actual entity storage.

Key features:
- Pin mappings stored in TSV files (human-readable, git-friendly)
- Entity data stored in SQLite (shared across repos by default)
- Dirty mark system for batched writes (avoids O(n) TSV rewrites)
- Automatic sync between TSV and SQLite on read/write
- Periodic auto-flush to write changes back to TSV files
"""

from __future__ import annotations

import contextlib
import logging
import time
from pathlib import Path
from typing import AsyncIterator

import anyio

from fxtr2.data.abstract_arena import (
    CopyTargetArena,
    EntityBackref,
    ObservableStore,
    OtherBackref,
    PersistentPinningBackend,
    ReadableArena,
)
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.sqlite_store import SQLiteFxtrSession, SQLiteNamespaceBackend
from fxtr2.util import anyio_util

from .config import GitStoreConfig, init_store, load_config
from .dirty import DirtyMarkManager, FlushResult
from .key_utils import parse_pin_key, validate_pin_key
from .sync import (
    FxtrSyncError,
    dump_sqlite_to_tsv,
    ensure_full_sync,
    ensure_tsv_synced,
    flush_tsv_to_disk,
    sync_tsv_to_sqlite,
)

logger = logging.getLogger(__name__)


class GitNamespaceBackend(PersistentPinningBackend, ObservableStore):
    """Git-backed fxtr namespace.

    Wraps a SQLite backing store with git-tracked TSV files as the source
    of truth for pin mappings. Entities are stored in SQLite, while pin
    name -> FxtrID mappings are stored in TSV files that can be committed
    to git.

    The namespace uses a "dirty mark" system to batch writes: pin/unpin
    operations write directly to SQLite, and changes are flushed to TSV
    files either explicitly via flush() or automatically via a background
    task.

    Two SQLite namespaces are used internally:
    - Index namespace: Accumulates all entities ever pinned, shared across worktrees
    - Worktree namespace: Mirrors the current TSV state

    Usage:
        # Initialize a new store (one-time setup)
        await GitNamespaceBackend.init(Path("/path/to/project"))

        # Open an existing store
        async with GitNamespaceBackend.open(Path("/path/to/project")) as ns:
            # Pin an entity
            await ns.pin_entity_from_arena("models:encoder", entity_id, arena)

            # Retrieve a pinned entity
            entity_id, arena = await ns.retrieve_pinned("models:encoder")

            # List all pins
            pins = await ns.list_pins()

    See plans/phase2/stage1_git_namespace.md for design details.
    """

    def __init__(
        self,
        config: GitStoreConfig,
        session: SQLiteFxtrSession,
        index_ns: SQLiteNamespaceBackend,
        worktree_ns: SQLiteNamespaceBackend,
        dirty_manager: DirtyMarkManager,
        auto_flush_seconds: float | None,
        full_sync_staleness_threshold: float | None,
    ):
        """Internal constructor. Use GitNamespaceBackend.open() instead."""
        self._config = config
        self._session = session
        self._index_ns = index_ns
        self._worktree_ns = worktree_ns
        self._dirty_manager = dirty_manager
        self._auto_flush_seconds = auto_flush_seconds
        self._full_sync_staleness_threshold = full_sync_staleness_threshold
        self._last_full_sync_time: float | None = None
        self._cancel_scope: anyio.CancelScope | None = None

    @classmethod
    @contextlib.asynccontextmanager
    async def open(
        cls,
        store_root: Path,
        *,
        auto_flush_seconds: float | None = 10.0,
        full_sync_staleness_threshold: float | None = 5.0,
        skip_sync: bool = False,
    ) -> AsyncIterator[GitNamespaceBackend]:
        """Open a git namespace at the given store root.

        The store_root must contain a fxtr_store.toml file. Creates local
        config files (fxtr_store.local.toml) if they don't exist.

        Performs full sync on open (syncs clean TSVs to SQLite) unless skip_sync
        is True.

        Args:
            store_root: Path containing fxtr_store.toml
            auto_flush_seconds: Flush dirty TSVs periodically at this interval.
                Set to None to disable auto-flush. Default: 10.0
            full_sync_staleness_threshold: For list_pins(), try_lookup(), and
                backrefs(), skip full sync if one was performed within this many
                seconds. Set to None to always sync, or 0 to never auto-sync.
                Default: 5.0
            skip_sync: If True, skip the initial full sync on open. Use this
                when importing an archive to restore missing entities before
                syncing. Default: False.

        Raises:
            FxtrConfigError: If store doesn't exist or config is invalid.
            FxtrSyncError: If full sync fails due to missing entities.
        """
        store_root = store_root.resolve()

        # Load configuration
        config = await load_config(store_root)

        # Get SQLite database path from config
        sqlite_path = str(config.sqlite_store_path)
        # Open SQLite session
        async with SQLiteFxtrSession.open(sqlite_path) as session:
            # Get or create namespaces
            index_ns = await session.create_or_retrieve_user_namespace(config.index_namespace_id)
            worktree_ns = await session.create_or_retrieve_user_namespace(
                config.worktree_namespace_id
            )

            # Create dirty mark manager
            dirty_manager = DirtyMarkManager(store_root)

            # Create the namespace instance
            namespace = cls(
                config=config,
                session=session,
                index_ns=index_ns,
                worktree_ns=worktree_ns,
                dirty_manager=dirty_manager,
                auto_flush_seconds=auto_flush_seconds,
                full_sync_staleness_threshold=full_sync_staleness_threshold,
            )

            # Perform full sync on open (unless skipped for import scenarios)
            if not skip_sync:
                await ensure_full_sync(config, dirty_manager, worktree_ns, index_ns)
                namespace._last_full_sync_time = time.monotonic()

            # Start auto-flush background task if enabled
            if auto_flush_seconds is not None:
                async with anyio_util.create_task_group(collapse_exc_group=True) as tg:
                    namespace._cancel_scope = anyio.CancelScope()
                    tg.start_soon(namespace._auto_flush_task)
                    try:
                        yield namespace
                    finally:
                        # Cancel the background task
                        namespace._cancel_scope.cancel()
                        # Flush any remaining dirty TSVs
                        await namespace._flush_all_owned()
            else:
                try:
                    yield namespace
                finally:
                    # Flush any remaining dirty TSVs
                    await namespace._flush_all_owned()

    @classmethod
    async def init(
        cls,
        store_root: Path,
        *,
        reserved_prefix: str = "index",
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        """Initialize a new git namespace at the given path.

        Creates the necessary config files and directories for a new git-backed
        fxtr store. Does NOT open the store - use open() after init().

        Args:
            store_root: Directory to create the store in
            reserved_prefix: Prefix for keys without colon (default: "index")
            name: Optional human-readable name for the store
            description: Optional description for the store

        Raises:
            FxtrConfigError: If store already exists or directory is not in a VCS.
        """
        await init_store(
            store_root,
            reserved_prefix=reserved_prefix,
            name=name,
            description=description,
        )

    async def _auto_flush_task(self) -> None:
        """Background task that periodically flushes dirty TSVs."""
        assert self._cancel_scope is not None
        assert self._auto_flush_seconds is not None

        with self._cancel_scope:
            while True:
                await anyio.sleep(self._auto_flush_seconds)
                try:
                    await self._flush_all_owned(defer_to_other_writers=True)
                except Exception:
                    # Log but don't crash - we'll try again next interval
                    logger.exception("Auto-flush failed")

    async def _flush_all_owned(self, *, defer_to_other_writers: bool = False) -> None:
        """Flush all TSVs that this manager has dirty marks for.

        Args:
            defer_to_other_writers: If True and other managers have dirty marks,
                just remove our marks and let them handle flushing.
        """
        dirty_tsvs = self._dirty_manager.get_dirty_tsvs()
        for tsv_path in dirty_tsvs:
            result = await flush_tsv_to_disk(
                tsv_path, self._config, self._dirty_manager, self._worktree_ns
            )
            if result == FlushResult.COMPLETE:
                logger.debug(f"Flushed {tsv_path}")
            elif result == FlushResult.DEFERRED and defer_to_other_writers:
                logger.debug(f"Deferred flush of {tsv_path} to other writer")
            elif result == FlushResult.NO_MARK:
                logger.debug(f"No mark for {tsv_path} (already flushed?)")

    async def _ensure_full_sync_if_stale(self) -> None:
        """Perform full sync if we haven't synced recently.

        Used by list_pins(), try_lookup(), and backrefs() to ensure
        reasonably fresh data without syncing on every call.
        """
        if self._full_sync_staleness_threshold is None:
            # Always sync
            await ensure_full_sync(
                self._config, self._dirty_manager, self._worktree_ns, self._index_ns
            )
            self._last_full_sync_time = time.monotonic()
        elif self._full_sync_staleness_threshold == 0:
            # Never auto-sync
            pass
        else:
            # Sync if stale
            now = time.monotonic()
            if (
                self._last_full_sync_time is None
                or (now - self._last_full_sync_time) > self._full_sync_staleness_threshold
            ):
                await ensure_full_sync(
                    self._config, self._dirty_manager, self._worktree_ns, self._index_ns
                )
                self._last_full_sync_time = now

    def _get_sync_callback(self, tsv_path: Path):
        """Create a sync callback for hold_dirty that syncs TSV to SQLite."""

        async def sync_if_first() -> None:
            await sync_tsv_to_sqlite(tsv_path, self._config, self._worktree_ns, self._index_ns)

        return sync_if_first

    # =========================================================================
    # PersistentPinningBackend implementation
    # =========================================================================

    async def pin_entity_from_arena(
        self, key: str, entity_id: FxtrID, source: ReadableArena, *, overwrite: bool = False
    ) -> None:
        """Pin an entity from an arena into this namespace.

        Copies the entity to the index namespace (for persistence across git
        branches), then writes the pin to the worktree namespace.

        The TSV file is NOT updated immediately - changes are batched and
        flushed either explicitly via flush() or automatically via the
        background auto-flush task.

        Args:
            key: The pin key (e.g., "mykey" or "data/models:encoder")
            entity_id: The entity to pin
            source: The arena containing the entity
            overwrite: If True, overwrite existing pin. If False (default),
                raise KeyError if pin already exists.

        Raises:
            FxtrKeyError: If key format is invalid
            KeyError: If pin exists and overwrite=False
        """
        # Validate key
        validate_pin_key(key, self._config.reserved_prefix)

        # Parse key to get TSV path
        rel_tsv_path, _ = parse_pin_key(key, self._config.reserved_prefix)
        tsv_path = (self._config.store_root / rel_tsv_path).resolve()

        # Step 1: Copy entity to index namespace (no lock needed)
        # Pin using FxtrID string as key so sync_tsv_to_sqlite can retrieve it
        await self._index_ns.pin_entity_from_arena(
            entity_id.id_string(), entity_id, source, overwrite=True
        )

        # Step 2: Use hold_dirty to claim mark and write to worktree namespace
        async with self._dirty_manager.hold_dirty(tsv_path, self._get_sync_callback(tsv_path)):
            # Retrieve from index namespace to get arena
            _, index_arena = await self._index_ns.retrieve_pinned(entity_id.id_string())

            # Pin to worktree namespace
            try:
                await self._worktree_ns.pin_entity_from_arena(
                    key, entity_id, index_arena, overwrite=overwrite
                )
            finally:
                index_arena.close()
            logger.debug(f"pin_entity_from_arena({key}): pinned {entity_id} to {tsv_path}")

    async def unpin_entity(self, key: str) -> None:
        """Unpin an entity from this namespace.

        Removes the pin from the worktree namespace. The entity remains in
        the index namespace (in case the TSV is restored from git history).

        The TSV file is NOT updated immediately - changes are batched and
        flushed either explicitly via flush() or automatically.

        Args:
            key: The pin key to remove

        Raises:
            FxtrKeyError: If key format is invalid
            KeyError: If pin doesn't exist
        """
        # Validate key
        validate_pin_key(key, self._config.reserved_prefix)

        # Parse key to get TSV path
        rel_tsv_path, _ = parse_pin_key(key, self._config.reserved_prefix)
        tsv_path = (self._config.store_root / rel_tsv_path).resolve()

        # Use hold_dirty to claim mark and remove from worktree namespace
        async with self._dirty_manager.hold_dirty(tsv_path, self._get_sync_callback(tsv_path)):
            await self._worktree_ns.unpin_entity(key)

    async def retrieve_pinned(self, key: str) -> tuple[FxtrID, ReadableArena]:
        """Retrieve a pinned entity by key.

        If the TSV is dirty (we've written to it), reads directly from SQLite.
        If the TSV is clean, syncs from disk if needed (mtime check).

        Args:
            key: The pin key to retrieve

        Returns:
            Tuple of (entity_id, arena) where arena can be used to load the entity.

        Raises:
            FxtrKeyError: If key format is invalid
            KeyError: If pin doesn't exist
            FxtrSyncError: If sync fails due to missing entities
        """
        # Validate key
        validate_pin_key(key, self._config.reserved_prefix)

        # Parse key to get TSV path
        rel_tsv_path, _ = parse_pin_key(key, self._config.reserved_prefix)
        tsv_path = (self._config.store_root / rel_tsv_path).resolve()

        # Ensure TSV is synced (skips if dirty or mtime unchanged)
        await ensure_tsv_synced(
            tsv_path, self._config, self._dirty_manager, self._worktree_ns, self._index_ns
        )

        # Retrieve from worktree namespace
        return await self._worktree_ns.retrieve_pinned(key)

    async def list_pins(self, prefix: str = "") -> dict[str, FxtrID]:
        """List pins in the namespace matching the given prefix.

        Performs a full sync if one hasn't been done recently (controlled by
        full_sync_staleness_threshold parameter).

        Args:
            prefix: Only return pins whose keys start with this prefix.
                Empty string (default) returns all pins.

        Returns:
            Dict mapping pin name to entity ID for all matching pins.
        """
        # Ensure reasonably fresh data
        await self._ensure_full_sync_if_stale()

        # Delegate to worktree namespace
        return await self._worktree_ns.list_pins(prefix)

    # =========================================================================
    # ObservableStore implementation
    # =========================================================================

    async def try_lookup(self, node_id: FxtrID) -> ReadableArena | None:
        """Try to look up an entity in the namespace.

        Performs a full sync if one hasn't been done recently.

        Args:
            node_id: The entity ID to look up

        Returns:
            An arena that can load the entity, or None if not found.
        """
        # Ensure reasonably fresh data
        await self._ensure_full_sync_if_stale()

        # Delegate to worktree namespace
        return await self._worktree_ns.try_lookup(node_id)

    async def backrefs(self, node_id: FxtrID) -> list[EntityBackref | OtherBackref]:
        """Get backrefs to an entity.

        Performs a full sync if one hasn't been done recently.

        Args:
            node_id: The entity ID to get backrefs for

        Returns:
            List of entities/pins that reference this entity.
        """
        # Ensure reasonably fresh data
        await self._ensure_full_sync_if_stale()

        # Delegate to worktree namespace
        return await self._worktree_ns.backrefs(node_id)

    # =========================================================================
    # Additional methods
    # =========================================================================

    def new_empty_arena(self) -> CopyTargetArena:
        """Create a new arena in the worktree namespace for accumulating entities.

        Typical workflow:
        1. arena = namespace.new_empty_arena()
        2. await arena.copy_from_arena(entity_id, memory_arena)
        3. await namespace.pin_entity_from_arena("path:key", entity_id, arena)

        Returns:
            A new empty arena backed by the worktree SQLite namespace.
        """
        return self._worktree_ns.new_empty_arena()

    async def flush(self, *, defer_to_other_writers: bool = False) -> None:
        """Flush all TSVs that this namespace has dirty marks for.

        Writes pending changes from SQLite back to TSV files.

        Args:
            defer_to_other_writers: If True and other managers have dirty marks,
                just remove our marks and let them handle flushing. If False
                (default), wait until we can flush.
        """
        await self._flush_all_owned(defer_to_other_writers=defer_to_other_writers)

    async def sync_from_git(self) -> None:
        """Sync TSV files to SQLite (git → SQLite direction).

        Reads all TSV files and updates the worktree namespace to match.
        Used after importing an archive to restore consistency.

        Raises:
            FxtrSyncError: If any TSV references missing entities.
        """
        await ensure_full_sync(self._config, self._dirty_manager, self._worktree_ns, self._index_ns)
        self._last_full_sync_time = time.monotonic()

    async def full_sync_state_to_git(self) -> None:
        """Force-flush ALL dirty TSVs to disk, regardless of who wrote them.

        This writes SQLite state to TSV files and removes all dirty marks.
        Used by CLI 'flush-to-git' command to prepare for commits/branch switches.

        Also cleans up orphaned *.removing-* directories left behind by crashed
        processes during dirty directory cleanup.

        Other managers will need to re-claim dirty marks on their next write.
        """
        import shutil

        # Step 1: Clean up orphaned *.removing-* directories
        for removing_dir in self._config.store_root.rglob("*.fxtr.tsv.dirty.removing-*"):
            if removing_dir.is_dir():
                logger.info(f"Cleaning up orphaned temp directory: {removing_dir}")
                shutil.rmtree(removing_dir, ignore_errors=True)

        # Step 2: Find all dirty TSVs by scanning for dirty directories
        dirty_tsvs: set[Path] = set()

        # Scan store root recursively for .dirty directories
        for dirty_dir in self._config.store_root.rglob("*.fxtr.tsv.dirty"):
            # Check if sentinel exists (actually dirty)
            sentinel = dirty_dir / "DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md"
            if sentinel.exists():
                # Extract TSV path from dirty dir path
                tsv_path = dirty_dir.parent / dirty_dir.name.removesuffix(".dirty")
                dirty_tsvs.add(tsv_path.resolve())

        # Step 3: Force-flush each dirty TSV
        for tsv_path in dirty_tsvs:
            # Claim a mark if we don't have one
            if not self._dirty_manager.may_have_dirty_mark(tsv_path):
                async with self._dirty_manager.hold_dirty(
                    tsv_path, self._get_sync_callback(tsv_path)
                ):
                    pass  # Just claim the mark

            # Force flush (removes other marks)
            # Create closure to capture tsv_path
            def make_dump_fn(tsv: Path):
                async def dump_fn(temp_path: Path) -> None:
                    await dump_sqlite_to_tsv(tsv, temp_path, self._config, self._worktree_ns)

                return dump_fn

            result = await self._dirty_manager.flush(tsv_path, make_dump_fn(tsv_path), force=True)
            if result == FlushResult.COMPLETE:
                logger.info(f"Force-flushed {tsv_path}")
            elif result == FlushResult.NO_MARK:
                logger.warning(f"Lost mark for {tsv_path} during force-flush")

    async def export_archive(self, output_path: Path) -> None:
        """Export current state to a standalone SQLite archive.

        Creates a new SQLite fxtr store at output_path containing all entities
        reachable from currently pinned entries. The archive can be imported
        into another store to restore missing entities.

        Flushes all dirty TSVs first to ensure consistency.

        Args:
            output_path: Path to create the archive at (should end in .sqlite)
        """
        # Flush first to ensure TSV files are up to date
        await self.flush()

        # Perform full sync to ensure we have all current pins
        await ensure_full_sync(self._config, self._dirty_manager, self._worktree_ns, self._index_ns)

        # Create archive SQLite store
        archive_path_str = str(output_path.resolve())
        async with SQLiteFxtrSession.open(archive_path_str) as archive_session:
            archive_ns = await archive_session.create_or_retrieve_user_namespace("archive")

            # Get all pins from worktree namespace
            all_pins = await self._worktree_ns.list_pins()

            # Copy each pinned entity to the archive
            for key, entity_id in all_pins.items():
                # Retrieve from worktree namespace
                _, arena = await self._worktree_ns.retrieve_pinned(key)

                # Pin to archive namespace
                try:
                    await archive_ns.pin_entity_from_arena(key, entity_id, arena, overwrite=True)
                finally:
                    arena.close()

        logger.info(f"Exported {len(all_pins)} pins to {output_path}")

    async def import_archive(self, archive_path: Path) -> None:
        """Import entities from an archive into the index namespace.

        For each entity in the archive:
        - If key doesn't exist: pin normally
        - If key exists with matching ID: no-op (already have the data)
        - If key exists with different ID: raise an error (conflict)

        Args:
            archive_path: Path to the archive SQLite file

        Raises:
            FxtrSyncError: If there's a conflict (key exists with different ID)
        """
        archive_path_str = str(archive_path.resolve())
        async with SQLiteFxtrSession.open(archive_path_str) as archive_session:
            archive_ns = await archive_session.create_or_retrieve_user_namespace("archive")

            # Get all pins from archive
            archive_pins = await archive_ns.list_pins("")

            conflicts: list[tuple[str, FxtrID, FxtrID]] = []
            imported = 0
            skipped = 0

            for key, archive_entity_id in archive_pins.items():
                # Check if entity already exists in index namespace
                try:
                    existing_id, unused_arena = await self._index_ns.retrieve_pinned(
                        archive_entity_id.id_string()
                    )
                    unused_arena.close()
                    if existing_id == archive_entity_id:
                        # Already have this entity
                        skipped += 1
                        continue
                    else:
                        # Conflict - different entity with same key
                        conflicts.append((key, existing_id, archive_entity_id))
                except KeyError:
                    # Entity doesn't exist, import it
                    pass

                # Retrieve from archive and pin to index namespace
                _, archive_arena = await archive_ns.retrieve_pinned(key)
                try:
                    await self._index_ns.pin_entity_from_arena(
                        archive_entity_id.id_string(),
                        archive_entity_id,
                        archive_arena,
                        overwrite=True,
                    )
                finally:
                    archive_arena.close()
                imported += 1

            if conflicts:
                details = "\n".join(
                    f"  {key}: existing={existing.id_string()}, archive={archive.id_string()}"
                    for key, existing, archive in conflicts
                )
                raise FxtrSyncError(
                    f"Import conflicts detected:\n\n{details}\n\n"
                    "The archive contains entities with keys that already exist "
                    "but have different IDs."
                )

            logger.info(f"Imported {imported} entities, skipped {skipped} (already present)")

    @property
    def config(self) -> GitStoreConfig:
        """Get the store configuration."""
        return self._config
