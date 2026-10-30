"""TSV-file fxtr namespace implementation.

This module provides TsvNamespaceBackend, a persistent storage backend that uses
git-tracked TSV files for pin mappings (version-controlled in git) and a
repo-level entity store (SQLite) for the actual entity data.

Key features:
- Pin mappings stored in TSV files (human-readable, git-tracked)
- Entity data stored in SQLite (shared across repos by default)
- Dirty mark system for batched writes (avoids O(n) TSV rewrites)
- Automatic sync between TSV and SQLite on read/write
- Periodic auto-flush to write changes back to TSV files
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, Literal

import anyio

from fxtr2.data.abstract_arena import DeepFetchable, FlatEntitySource, ReadableArena
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.file_util.lock import async_file_lock_acquire
from fxtr2.data.repo_storage import RepoEntityStore
from fxtr2.data.sqlite_store import SQLiteFxtrSession, SQLiteNamespaceBackend
from fxtr2.data.store_indexing.indexing_types import (
    BindingConstraint,
    IndexablePinningBackend,
    IndexerColumnValue,
    IndexerConstraintByID,
    ParsedView,
)
from fxtr2.leases.lease_types import Lease, LifetimeBrand, LifetimeToken
from fxtr2.util import anyio_util

from .config import DEFAULT_RESERVED_PREFIX, TsvNamespaceConfig, init_namespace, load_config
from .dirty import DirtyMarkManager, FlushResult
from .index_file import index_file_path_for_config
from .index_sync import (
    add_registration_to_file,
    remove_dead_index_registrations,
    remove_registration_from_file,
    sync_index_registrations,
)
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


def _archive_tar_mode(path: Path) -> Literal["r:", "r:gz"] | None:
    """Return tarfile open mode if *path* looks like a tarball, else None."""
    name = path.name
    if name.endswith(".tar.gz") or name.endswith(".tgz"):
        return "r:gz"
    elif name.endswith(".tar"):
        return "r:"
    return None


class TsvNamespaceBackend(IndexablePinningBackend, DeepFetchable):
    """TSV-file fxtr namespace.

    Pin mappings are defined via git-tracked TSV files, which are the source of
    truth for pin name -> FxtrID mappings when clean. The actual entities live
    in a repo-level entity store (SQLite). The TSV files can be committed to git
    alongside code.

    The namespace uses a "dirty mark" system to batch writes: pin/unpin
    operations write directly to SQLite, and changes are flushed to TSV
    files either explicitly via flush() or automatically via a background
    task.

    Two SQLite namespaces are used internally:
    - Index namespace: Accumulates all entities ever pinned, shared across worktrees
    - Worktree namespace: Mirrors the current TSV state

    Usage:
        # Initialize a new tsv namespace (one-time setup)
        await TsvNamespaceBackend.init(Path("/path/to/project"))

        # Open an existing tsv namespace
        async with TsvNamespaceBackend.open(Path("/path/to/project")) as ns:
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
        config: TsvNamespaceConfig,
        session: SQLiteFxtrSession,
        index_ns: SQLiteNamespaceBackend,
        worktree_ns: SQLiteNamespaceBackend,
        dirty_manager: DirtyMarkManager,
        auto_flush_seconds: float | None,
        full_sync_staleness_threshold: float | None,
    ):
        """Internal constructor. Use TsvNamespaceBackend.open() instead."""
        self._config = config
        self._session = session
        # The index namespace is the tsv namespace's backing **repo entity
        # store**. ``_index_ns`` is the raw SQLiteNamespaceBackend used by the
        # sync / indexing / import machinery; ``_store`` is the by-id facade
        # over the same session+namespace that this view exposes as ``.store``
        # (e.g. for fxtr_nix_runner). By-id entity-bytes writes go through it.
        self._index_ns = index_ns
        self._store: RepoEntityStore[Any] = RepoEntityStore.from_session(session, index_ns)
        self._worktree_ns = worktree_ns
        self._dirty_manager = dirty_manager
        self._auto_flush_seconds = auto_flush_seconds
        self._full_sync_staleness_threshold = full_sync_staleness_threshold
        self._index_file_path = index_file_path_for_config(config)
        self._last_full_sync_time: float | None = None
        self._cancel_scope: anyio.CancelScope | None = None
        self._flush_event = anyio.Event()

    @property
    def token(self) -> LifetimeToken[Any]:
        return self._session.token

    def get_lease(self) -> Lease[Any]:
        """Return a fresh clone of the underlying session's root lease.

        Use this for arenas returned by this backend and for arena operations that need a
        ``Lease`` for this session's brand.
        """
        return self._session.root_lease.clone()

    @classmethod
    @contextlib.asynccontextmanager
    async def open(
        cls,
        namespace_root: Path,
        *,
        auto_flush_seconds: float | None = 10.0,
        full_sync_staleness_threshold: float | None = 5.0,
        sync_staleness_threshold: float | None = 0.1,
        skip_sync: bool = False,
    ) -> AsyncIterator[TsvNamespaceBackend]:
        """Open a tsv namespace at the given namespace root.

        The namespace_root must contain a fxtr_namespace.toml file. Creates local
        config files (fxtr_namespace.local.toml) if they don't exist.

        Performs full sync on open (syncs clean TSVs to SQLite) unless skip_sync
        is True.

        Args:
            namespace_root: Path containing fxtr_namespace.toml
            auto_flush_seconds: Flush dirty TSVs periodically at this interval.
                Set to None to disable auto-flush. Default: 10.0
            full_sync_staleness_threshold: For list_pins(), try_lookup(), and similar
                read paths, skip full sync if one was performed within this many
                seconds. Set to None to always sync, or 0 to never auto-sync.
                Default: 5.0
            sync_staleness_threshold: For retrieve_pinned(), skip TSV sync if
                synced within this many seconds. Set to None or 0 to always check
                mtime. Default: 0.1 (100ms)
            skip_sync: If True, skip the initial full sync on open. Use this
                when importing an archive to restore missing entities before
                syncing. Default: False.

        Raises:
            FxtrConfigError: If store doesn't exist or config is invalid.
            FxtrSyncError: If full sync fails due to missing entities.
        """
        namespace_root = namespace_root.resolve()

        # Load configuration
        config = await load_config(namespace_root)

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
            dirty_manager = DirtyMarkManager(
                config.namespace_root_str, sync_staleness_threshold=sync_staleness_threshold
            )

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
                await namespace._full_sync(show_tqdm=True)
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
        namespace_root: Path,
        *,
        reserved_prefix: str = DEFAULT_RESERVED_PREFIX,
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        """Initialize a new tsv namespace at the given path.

        Creates the necessary config files and directories for a new tsv
        namespace. Does NOT open the namespace - use open() after init().

        Args:
            namespace_root: Directory to create the store in
            reserved_prefix: Prefix for keys without colon (default: "root")
            name: Optional human-readable name for the store
            description: Optional description for the store

        Raises:
            FxtrConfigError: If store already exists or directory is not in a VCS.
        """
        await init_namespace(
            namespace_root,
            reserved_prefix=reserved_prefix,
            name=name,
            description=description,
        )

    def _notify_modification(self) -> None:
        """Signal that a pin was modified, triggering a debounced flush."""
        self._flush_event.set()

    async def _auto_flush_task(self) -> None:
        """Background task that flushes dirty TSVs after an idle period.

        Waits for a modification event, then debounces: sleeps for the flush
        delay and restarts the delay if another modification occurs during
        the sleep. Only flushes once no modifications have occurred for the
        full delay period.
        """
        assert self._cancel_scope is not None
        assert self._auto_flush_seconds is not None

        with self._cancel_scope:
            while True:
                # Wait until something is modified
                await self._flush_event.wait()
                # Debounce loop: keep resetting while modifications arrive
                while True:
                    self._flush_event = anyio.Event()
                    await anyio.sleep(self._auto_flush_seconds)
                    if self._flush_event.is_set():
                        # Modified during sleep, restart the delay
                        continue
                    else:
                        break
                # Idle period passed, flush
                try:
                    await self._flush_all_owned(defer_to_other_writers=True)
                except Exception:
                    # Log but don't crash - we'll try again next time
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
                tsv_path, self._config, self._dirty_manager, self._worktree_ns, self._session
            )
            if result == FlushResult.COMPLETE:
                logger.debug(f"Flushed {tsv_path}")
            elif result == FlushResult.DEFERRED and defer_to_other_writers:
                logger.debug(f"Deferred flush of {tsv_path} to other writer")
            elif result == FlushResult.NO_MARK:
                logger.debug(f"No mark for {tsv_path} (already flushed?)")

    async def _ensure_full_sync_if_stale(self) -> None:
        """Perform full sync if we haven't synced recently.

        Used by list_pins(), try_lookup(), and similar read paths to ensure
        reasonably fresh data without syncing on every call.
        """
        if self._full_sync_staleness_threshold is None:
            # Always sync
            await self._full_sync()
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
                await self._full_sync()
                self._last_full_sync_time = now

    def _get_sync_callback(self, tsv_path: str):
        """Create a sync callback for hold_dirty that syncs TSV to SQLite."""

        async def sync_if_first() -> None:
            await sync_tsv_to_sqlite(
                tsv_path, self._config, self._worktree_ns, self._index_ns, self._session
            )

        return sync_if_first

    async def _full_sync(self, *, show_tqdm: bool = False) -> None:
        """Three-phase full sync: index registrations + pin TSVs.

        Phase 1: Remove dead indexers (in worktree_ns but not in file).
                 Done before pin sync so dead indexers don't run on new pins.
        Phase 2: Sync pin TSVs (existing ensure_full_sync flow).
        Phase 3: Sync index registrations (adds new indexers from file,
                 re-checks removes — idempotent).
        """
        # Phase 1: Remove dead indexers
        async with async_file_lock_acquire(self._index_file_path):
            await remove_dead_index_registrations(
                self._index_file_path, self._worktree_ns, self._session
            )

        # Phase 2: Sync pin TSVs
        await ensure_full_sync(
            self._config,
            self._dirty_manager,
            self._worktree_ns,
            self._index_ns,
            self._session,
            show_tqdm=show_tqdm,
        )

        # Phase 3: Add new indexers and re-check removes
        async with async_file_lock_acquire(self._index_file_path):
            await sync_index_registrations(
                self._index_file_path, self._worktree_ns, self._index_ns, self._session
            )

    # =========================================================================
    # PersistentPinningBackend implementation
    # =========================================================================

    async def pin_entity_from_arena[
        B_Src: LifetimeBrand
    ](
        self,
        key: str,
        entity_id: FxtrID,
        source: FlatEntitySource[B_Src],
        src_lease: Lease[B_Src],
        *,
        overwrite: bool = False,
    ) -> None:
        """Pin an entity from a source into this namespace.

        Copies the entity to the index namespace (for persistence across git
        branches), then writes the pin to the worktree namespace.

        The TSV file is NOT updated immediately - changes are batched and
        flushed either explicitly via flush() or automatically via the
        background auto-flush task.

        Args:
            key: The pin key (e.g., "mykey" or "data/models:encoder")
            entity_id: The entity to pin
            source: The source containing the entity
            src_lease: Lease for ``source``'s token, consumed by the call.
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
        tsv_path = f"{self._config.namespace_root_str}/{rel_tsv_path}"

        # Step 1: Copy entity to the repo entity store (no lock needed).
        # The store keys by FxtrID so sync_tsv_to_sqlite can retrieve it.
        with src_lease.take() as src_held:
            await self._store.write_from_arena(entity_id, source, src_held.clone())

        # Step 2: Use hold_dirty to claim mark and write to worktree namespace
        async with self._dirty_manager.hold_dirty(tsv_path, self._get_sync_callback(tsv_path)):
            # Retrieve from the store to get an arena for the worktree pin
            index_arena = await self._store.try_lookup(entity_id)
            assert (
                index_arena is not None
            ), f"entity {entity_id} missing from repo store immediately after write"

            # Pin to worktree namespace
            try:
                await self._worktree_ns.pin_entity_from_arena(
                    key,
                    entity_id,
                    index_arena,
                    self._session.root_lease.clone(),
                    overwrite=overwrite,
                )
            finally:
                index_arena.close()
            logger.debug(f"pin_entity_from_arena({key}): pinned {entity_id} to {tsv_path}")

        self._notify_modification()

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
        tsv_path = f"{self._config.namespace_root_str}/{rel_tsv_path}"

        # Use hold_dirty to claim mark and remove from worktree namespace
        async with self._dirty_manager.hold_dirty(tsv_path, self._get_sync_callback(tsv_path)):
            await self._worktree_ns.unpin_entity(key)

        self._notify_modification()

    async def retrieve_pinned(self, key: str) -> tuple[FxtrID, ReadableArena[Any]]:
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
        tsv_path = f"{self._config.namespace_root_str}/{rel_tsv_path}"

        # Ensure TSV is synced (skips if dirty or mtime unchanged)
        await ensure_tsv_synced(
            tsv_path,
            self._config,
            self._dirty_manager,
            self._worktree_ns,
            self._index_ns,
            self._session,
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
    # IndexablePinningBackend implementation
    # =========================================================================

    async def register_indexer[
        B_Src: LifetimeBrand
    ](
        self,
        indexer_id: FxtrID,
        source: ReadableArena[B_Src],
        src_lease: Lease[B_Src],
        key_prefix: str = "",
    ) -> None:
        """Register an indexer on this tsv namespace.

        Copies the indexer entity to the backing (index) namespace, registers
        it on the worktree namespace (which runs the building loop), and
        updates the git-tracked index file.

        Args:
            indexer_id: The indexer entity ID.
            source: Arena containing the indexer entity.
            key_prefix: Key prefix to index.
        """
        async with async_file_lock_acquire(self._index_file_path):
            # Copy indexer entity to the repo entity store
            with src_lease.take() as src_held:
                await self._store.write_from_arena(indexer_id, source, src_held.clone())
            # Retrieve from the store to get a valid arena for worktree_ns
            index_arena = await self._store.try_lookup(indexer_id)
            assert (
                index_arena is not None
            ), f"indexer {indexer_id} missing from repo store immediately after write"
            try:
                await self._worktree_ns.register_indexer(
                    indexer_id, index_arena, self._session.root_lease.clone(), key_prefix
                )
            finally:
                index_arena.close()
            # Update the git-tracked file
            await add_registration_to_file(self._index_file_path, indexer_id, key_prefix)

    async def unregister_indexer(self, indexer_id: FxtrID, key_prefix: str = "") -> None:
        """Unregister an indexer from this tsv namespace.

        Unregisters from the worktree namespace (marks GARBAGE) and updates
        the git-tracked index file.

        Args:
            indexer_id: The indexer entity ID.
            key_prefix: Key prefix that was indexed.
        """
        async with async_file_lock_acquire(self._index_file_path):
            await self._worktree_ns.unregister_indexer(indexer_id, key_prefix)
            await remove_registration_from_file(self._index_file_path, indexer_id, key_prefix)

    async def list_indexers(
        self,
    ) -> tuple[list[tuple[FxtrID, str]], ReadableArena[Any]]:
        """List registered indexers, syncing from the index file first.

        Returns:
            Tuple of (registrations, arena) where registrations is a list of
            (indexer_id, key_prefix) pairs.
        """
        async with async_file_lock_acquire(self._index_file_path):
            await sync_index_registrations(
                self._index_file_path, self._worktree_ns, self._index_ns, self._session
            )
        return await self._worktree_ns.list_indexers()

    async def iter_matches(
        self,
        first: IndexerConstraintByID,
        /,
        *rest: IndexerConstraintByID,
        key_prefix: str = "",
        where: BindingConstraint | None = None,
    ) -> AsyncIterator[tuple[str, FxtrID, Mapping[str, IndexerColumnValue], ReadableArena[Any]]]:
        """Query indexes, syncing from the index file first.

        Args:
            first: The first indexer constraint.
            rest: Additional indexer constraints.
            key_prefix: Key prefix to restrict matches to.
            where: Optional binding constraint.

        Yields:
            (key, entity_id, bindings, arena) tuples for each match.
        """
        async with async_file_lock_acquire(self._index_file_path):
            await sync_index_registrations(
                self._index_file_path, self._worktree_ns, self._index_ns, self._session
            )
        async for result in self._worktree_ns.iter_matches(
            first, *rest, key_prefix=key_prefix, where=where
        ):
            yield result

    async def run_direct_sql(
        self,
        query: str,
        views: list[ParsedView],
    ) -> list[sqlite3.Row]:
        """Run an ad-hoc SQL query, syncing indexes first.

        Delegates to the worktree namespace's SQLiteNamespaceBackend.
        """
        async with async_file_lock_acquire(self._index_file_path):
            await sync_index_registrations(
                self._index_file_path, self._worktree_ns, self._index_ns, self._session
            )
        return await self._worktree_ns.run_direct_sql(query, views)

    # =========================================================================
    # DeepFetchable implementation
    # =========================================================================

    async def try_lookup(self, node_id: FxtrID) -> ReadableArena[Any] | None:
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

    # =========================================================================
    # Additional methods
    # =========================================================================

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
        Also syncs index registrations. Used after importing an archive.

        Raises:
            FxtrSyncError: If any TSV references missing entities.
        """
        await self._full_sync()
        self._last_full_sync_time = time.monotonic()

    async def full_sync_state_to_git(self) -> None:
        """Force-flush ALL dirty TSVs to disk, regardless of who wrote them.

        This writes SQLite state to TSV files and removes all dirty marks.
        Used by CLI 'flush-to-tsv' command to prepare for commits/branch switches.

        Also cleans up orphaned *.removing-* directories left behind by crashed
        processes during dirty directory cleanup.

        Other managers will need to re-claim dirty marks on their next write.
        """
        import shutil

        # Step 1: Clean up orphaned *.removing-* directories
        for removing_dir in self._config.namespace_root.rglob("*.fxtr.tsv.dirty.removing-*"):
            if removing_dir.is_dir():
                logger.info(f"Cleaning up orphaned temp directory: {removing_dir}")
                shutil.rmtree(removing_dir, ignore_errors=True)

        # Step 2: Find all dirty TSVs by scanning for dirty directories
        dirty_tsvs: set[str] = set()

        # Scan store root recursively for .dirty directories
        for dirty_dir in self._config.namespace_root.rglob("*.fxtr.tsv.dirty"):
            # Check if sentinel exists (actually dirty)
            sentinel = dirty_dir / "DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md"
            if sentinel.exists():
                # Extract TSV path from dirty dir path:
                # dirty_dir is e.g. /store/foo.fxtr.tsv.dirty → TSV is /store/foo.fxtr.tsv
                dirty_dir_str = str(dirty_dir.resolve())
                tsv_path_str = dirty_dir_str.removesuffix(".dirty")
                dirty_tsvs.add(tsv_path_str)

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
            def make_dump_fn(tsv: str):
                async def dump_fn(temp_path: str) -> None:
                    await dump_sqlite_to_tsv(
                        tsv, temp_path, self._config, self._worktree_ns, self._session
                    )

                return dump_fn

            result = await self._dirty_manager.flush(tsv_path, make_dump_fn(tsv_path), force=True)
            if result == FlushResult.COMPLETE:
                logger.info(f"Force-flushed {tsv_path}")
            elif result == FlushResult.NO_MARK:
                logger.warning(f"Lost mark for {tsv_path} during force-flush")

    async def export_archive(
        self, output_path: Path, *, tar: bool = True, gz: bool = False
    ) -> None:
        """Export current state to a standalone archive.

        Flushes dirty TSVs and performs a full sync first, then delegates to
        :meth:`SQLiteNamespaceBackend.export_archive` on the worktree namespace.

        Args:
            output_path: Path to create the archive at.
            tar: If True (default), package as a tarball.
            gz: If True, compress the tarball with gzip (ignored when tar=False).
        """
        # Flush first to ensure TSV files are up to date
        await self.flush()

        # Perform full sync to ensure we have all current pins and indexers
        await self._full_sync()

        await self._worktree_ns.export_archive(output_path, tar=tar, gz=gz)

    async def import_archive_into_backing_store(self, archive_path: Path) -> None:
        """Import entities from an archive into the index (backing) namespace.

        This does **not** modify the worktree namespace or TSV files — it only
        populates the backing SQLite store so that entities referenced by TSVs
        can be resolved.  Typically followed by :meth:`sync_from_git`.

        Accepts either a tarball (``.tar`` or ``.tar.gz``/``.tgz``) or a raw
        ``.sqlite`` file.  Tarballs are unpacked to a temporary directory first.

        For each entity in the archive, the entity is pinned in the index
        namespace using its ``id_string()`` as the key.  Entities that already
        exist with the same ID are skipped; conflicts (same key, different ID)
        raise an error.

        Args:
            archive_path: Path to the archive file (tarball or sqlite).

        Raises:
            FxtrSyncError: If there's a conflict (key exists with different ID).
            ValueError: If the archive format is not recognized.
        """
        import tarfile
        import tempfile

        tar_mode = _archive_tar_mode(archive_path)
        if tar_mode is not None:
            with tempfile.TemporaryDirectory() as tempdir:
                with tarfile.open(archive_path, tar_mode) as tf:
                    tf.extractall(tempdir, filter="data")
                sqlite_path = Path(tempdir) / "store.sqlite"
                if not sqlite_path.exists():
                    raise ValueError(
                        f"Archive tarball does not contain 'store.sqlite': {archive_path}"
                    )
                await self._do_import_from_sqlite(sqlite_path)
        elif archive_path.suffix == ".sqlite":
            await self._do_import_from_sqlite(archive_path)
        else:
            raise ValueError(
                f"Unrecognized archive format: {archive_path.name}. "
                "Expected .tar, .tar.gz, .tgz, or .sqlite"
            )

    async def _do_import_from_sqlite(self, sqlite_path: Path) -> None:
        """Import entities from an archive sqlite file into the index namespace."""
        archive_path_str = str(sqlite_path.resolve())
        async with SQLiteFxtrSession.open(archive_path_str) as archive_session:
            archive_ns = await archive_session.create_or_retrieve_user_namespace("archive")
            ar_lease = archive_session.root_lease

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
                        ar_lease.clone(),
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

            # Import indexer metadata if present
            await self._import_indexer_metadata(archive_session)

    async def _import_indexer_metadata(self, archive_session: SQLiteFxtrSession) -> None:
        """Import indexer entities from archive metadata into the backing store.

        Checks for a "metadata" namespace in the archive. If present, loads
        the NamespaceIndexMetadata entity and pins each referenced indexer
        entity into the index (backing) namespace.

        The indexer entities are then available for sync — the git-tracked
        fxtr_namespace_indexes.tsv controls whether they're actually registered.
        """
        from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope
        from fxtr2.data.store_indexing.indexing_types import NamespaceIndexMetadata

        # Check if "metadata" namespace exists
        ar_lease = archive_session.root_lease
        try:
            metadata_ns = await archive_session.create_or_retrieve_user_namespace("metadata")
            metadata_id, metadata_arena = await metadata_ns.retrieve_pinned("indexers")
        except KeyError:
            # No metadata namespace or no "indexers" pin — nothing to import
            return

        # Load the metadata entity to get indexer IDs
        # The metadata has strong links to indexer entities, so loading it
        # pulls in all indexer entities as transitive dependencies
        async with FxtrScope() as scope:
            await scope._main_arena.copy_from_arena(
                scope._current_auth_lease().clone(),
                metadata_arena,
                ar_lease.clone(),
                metadata_id,
            )
            meta_ref = EntityRef(entity_id=metadata_id, target_type=NamespaceIndexMetadata)
            metadata = await meta_ref.load(scope)

            # Pin each indexer entity in the backing store
            # They're already in scope._main_arena as transitive dependencies
            imported_indexers = 0
            for indexer_id, _key_prefix in metadata.registrations:
                await self._index_ns.pin_entity_from_arena(
                    indexer_id.id_string(),
                    indexer_id,
                    scope._main_arena,
                    scope._current_auth_lease().clone(),
                    overwrite=True,
                )
                imported_indexers += 1

            logger.info(f"Imported {imported_indexers} indexer entities from archive metadata")

        metadata_arena.close()

    def clear_staleness_cache(self, key: str | None = None) -> None:
        """Clear staleness cache to force fresh reads on next access.

        Use this when you know external changes have occurred (e.g., after running
        a git command that might have modified TSV files) and want to ensure the
        next read gets fresh data.

        Args:
            key: Clear cache for specific pin key (derives TSV path), or None to
                clear cache for all TSVs. Examples:
                - "mykey" → clears cache for index.fxtr.tsv
                - "data/models:encoder" → clears cache for data/models.fxtr.tsv
                - None → clears all caches
        """
        if key is None:
            self._dirty_manager.clear_staleness_cache()
        else:
            rel_tsv_path, _ = parse_pin_key(key, self._config.reserved_prefix)
            tsv_path = f"{self._config.namespace_root_str}/{rel_tsv_path}"
            self._dirty_manager.clear_staleness_cache(tsv_path)

    @property
    def config(self) -> TsvNamespaceConfig:
        """Get the store configuration."""
        return self._config

    @property
    def store(self) -> RepoEntityStore[Any]:
        """The backing repo entity store (the by-id facade over the index namespace).

        Callers holding a tsv namespace can hand this to consumers that only
        need by-id reads/writes (e.g. fxtr_nix_runner) without re-discovering or
        opening a second session — it shares this backend's session.
        """
        return self._store
