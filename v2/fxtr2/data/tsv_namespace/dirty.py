"""Dirty mark management for tsv namespaces.

The dirty mark system allows batching writes to sqlite and lazily flushing
to TSV files. This avoids O(n) TSV rewrites on every pin operation.

TSV States:
- Clean (default): The TSV file on disk is the source of truth. SQLite may be stale.
- Dirty (transient): SQLite is the source of truth. TSV file is out of date.

Dirty state is indicated by a sentinel file in a `.dirty/` directory:
    {name}.fxtr.tsv.dirty/
    ├── DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md  # Sentinel + warning message
    ├── dirtymark-{uuid1}                         # Process 1's dirty mark
    ├── dirtymark-{uuid2}                         # Process 2's dirty mark
    ├── dirtymark_flag                            # Set on every claim, for staleness detection
    └── state.lock                                # State lock

The primary API is the `hold_dirty` context manager, which atomically claims
a dirty mark and holds an exclusive lock on it for the duration of writes.

For reads, `ensure_synced` checks if a TSV needs to be synced from disk to SQLite.

See the plan document for detailed state machine and locking documentation.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

from fxtr2.data.file_util.lock import LockFileNotFoundError, async_file_lock_acquire

logger = logging.getLogger(__name__)


class ClaimResult(Enum):
    """Result of claiming a dirty mark via hold_dirty()."""

    SYNCED = auto()
    """First to go dirty - sync_if_first callback was called."""

    ACQUIRED = auto()
    """Joined existing dirty state - sentinel already existed."""

    ALREADY_HELD = auto()
    """We already had a mark from a previous call (fast path)."""


class FlushResult(Enum):
    """Result of flushing a dirty TSV."""

    COMPLETE = auto()
    """Successfully flushed."""

    NO_MARK = auto()
    """We didn't have a dirty mark."""

    DEFERRED = auto()
    """Other writers exist, removed our mark, let them flush."""


# File names within the dirty directory
SENTINEL_FILENAME = "DO_NOT_COMMIT_WHILE_THIS_FILE_EXISTS.md"
DIRTYMARK_FLAG_FILENAME = "dirtymark_flag"
STATE_LOCK_FILENAME = "state.lock"
DIRTYMARK_PREFIX = "dirtymark-"

# Sentinel file content
SENTINEL_CONTENT = """\
# DO NOT COMMIT WHILE THIS FILE EXISTS

This file indicates that the adjacent `.fxtr.tsv` file is out of date.
The current data is in the SQLite store, and will be written back to the
TSV file when all writers have finished.

**Do not commit changes while this file exists.** The TSV file does not
reflect the current state.

To flush all dirty TSVs and remove this file, run:

    fxtr-tsv-ns flush-to-tsv <store-path>

If this file persists after all fxtr processes have exited, it may indicate
a crashed process. Running `flush-to-tsv` will recover the data.
"""


@dataclass
class TsvSyncState:
    """Cached state for sync detection on a single TSV file.

    This tracks the state of a TSV file after the last sync, allowing us to
    detect changes without re-reading the file every time.
    """

    tsv_mtime: float
    """mtime of TSV file after last sync (or 0 if file didn't exist)."""

    tsv_checksum: str
    """SHA-256 hash of TSV content after last sync (or empty string if file didn't exist)."""

    reference_mtime: float
    """Filesystem timestamp captured after last sync (from touching a temp file).

    Used to detect if TSV changed: if tsv_mtime < reference_mtime, TSV is older
    than our sync. We use a file's mtime rather than time.time() to ensure the
    timestamp has the same granularity as the filesystem's mtime."""


def dirty_dir_for_tsv(tsv_path: str) -> str:
    """Get the dirty directory path for a TSV file.

    Args:
        tsv_path: Absolute path to the TSV file.

    Returns:
        Path to the dirty directory (e.g., foo.fxtr.tsv.dirty/).
    """
    return f"{tsv_path}.dirty"


def sync_lock_path_for_tsv(tsv_path: str) -> str:
    """Get the sync lock path for a TSV file.

    The sync lock is a sibling to the TSV file, not inside the dirty directory.

    Args:
        tsv_path: Absolute path to the TSV file.

    Returns:
        Path to the sync lock file.
    """
    return f"{tsv_path}.sync_lock"


def _compute_file_checksum(path: str) -> str:
    """Compute SHA-256 checksum of a file's contents.

    Args:
        path: Path to the file.

    Returns:
        Hex-encoded SHA-256 hash, or empty string if file doesn't exist.
    """
    try:
        with open(path, "rb") as f:
            content = f.read()
        return hashlib.sha256(content).hexdigest()
    except FileNotFoundError:
        return ""


def _get_mtime(path: str) -> float:
    """Get mtime of a file, or 0 if it doesn't exist."""
    try:
        return os.stat(path).st_mtime
    except FileNotFoundError:
        return 0.0


def _make_backup_path(tsv_path: str) -> str:
    """Create a backup path for a TSV file with current timestamp.

    Format: {name}.fxtr.tsv.backup-{YYYYMMDD-HHMMSS-ffffff}.tsv
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    return f"{tsv_path}.backup-{timestamp}.tsv"


def _touch(path: str) -> None:
    """Create a file if it doesn't exist, or update its mtime if it does."""
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o666)
    os.close(fd)
    os.utime(path, None)


class DirtyMarkManager:
    """Manages dirty marks and sync state for a TsvNamespaceBackend instance.

    Each DirtyMarkManager tracks which TSV files this process has claimed
    for writing. Multiple processes can have dirty marks on the same TSV
    simultaneously - they all write to sqlite, and the last one to flush
    writes to the TSV file.

    The primary APIs are:
    - `hold_dirty()`: Claim a dirty mark for writing to SQLite
    - `flush()`: Write SQLite state back to TSV
    - `ensure_synced()`: Ensure TSV → SQLite is current before reads
    - `discover_tsvs_needing_sync()`: Find TSVs that may have changed on disk

    Thread safety: This class is designed for use within a single event loop.
    Cross-process coordination is handled by file locks.
    """

    def __init__(self, namespace_root: str, sync_staleness_threshold: float | None = None) -> None:
        """Create a new dirty mark manager.

        Args:
            namespace_root: Root directory of the fxtr store (must be pre-resolved).
            sync_staleness_threshold: Time window (in seconds) for time-based staleness
                checking. If a TSV was synced within this window, ensure_synced() will
                skip re-checking it. Set to None or 0 to always check mtime. Default: None.
        """
        self.manager_uuid = uuid.uuid4()
        self._namespace_root = namespace_root
        self._dirty_tsvs: set[str] = set()
        self._tsv_state: dict[str, TsvSyncState] = {}

        # Time-based staleness tracking
        self._last_sync_time: dict[str, float] = {}
        self._sync_staleness_threshold = sync_staleness_threshold

    def _dirtymark_path(self, dirty_dir: str) -> str:
        """Get the path to our dirty mark file."""
        return f"{dirty_dir}/{DIRTYMARK_PREFIX}{self.manager_uuid}"

    def _sentinel_path(self, dirty_dir: str) -> str:
        """Get the path to the sentinel file."""
        return f"{dirty_dir}/{SENTINEL_FILENAME}"

    def _flag_path(self, dirty_dir: str) -> str:
        """Get the path to the dirty mark flag file."""
        return f"{dirty_dir}/{DIRTYMARK_FLAG_FILENAME}"

    def _state_lock_path(self, dirty_dir: str) -> str:
        """Get the path to the state lock file."""
        return f"{dirty_dir}/{STATE_LOCK_FILENAME}"

    def may_have_dirty_mark(self, tsv_path: str) -> bool:
        """Check if we might have a dirty mark for a TSV.

        This is a conservative in-memory check. Returns True if we claimed
        a mark and didn't clean it up ourselves. However, another process
        may have force-removed our mark, so this can return True even when
        we no longer have a valid mark.

        Use `hold_dirty()` for authoritative verification - it will re-claim
        if our mark was removed.

        Args:
            tsv_path: Absolute path to the TSV file.

        Returns:
            True if we may have a dirty mark, False if we definitely don't.
        """
        return tsv_path in self._dirty_tsvs

    def is_dirty(self, tsv_path: str) -> bool:
        """Check if a TSV is in dirty state (sentinel file exists).

        This checks for the sentinel file, not individual marks. The sentinel
        indicates that sqlite is the source of truth for this TSV.

        Args:
            tsv_path: Absolute path to the TSV file.

        Returns:
            True if the TSV has a sentinel file indicating dirty state.
        """
        dirty_dir = dirty_dir_for_tsv(tsv_path)
        return os.path.exists(self._sentinel_path(dirty_dir))

    @asynccontextmanager
    async def hold_dirty(
        self,
        tsv_path: str,
        sync_if_first: Callable[[], Awaitable[None]],
    ) -> AsyncIterator[ClaimResult]:
        """Claim and hold a dirty mark for writing.

        This is the primary API for writers. It atomically:
        1. Claims a dirty mark if we don't have one (calling sync_if_first if first)
        2. Acquires an exclusive lock on our mark file
        3. Yields the ClaimResult

        The lock is held for the duration of the context, protecting against
        flush(force=True) deleting our mark while we're writing.

        If our mark was externally removed (by force-flush), this will
        automatically re-claim and retry.

        Args:
            tsv_path: Absolute path to the TSV file.
            sync_if_first: Async callback to run if this is the first claim
                (no sentinel exists). Called while holding state lock and
                sync lock. Should sync TSV contents to sqlite.

        Yields:
            ClaimResult indicating what happened:
            - SYNCED: First to go dirty, sync_if_first was called
            - ACQUIRED: Joined existing dirty state
            - ALREADY_HELD: We already had a mark (fast path)

        Example:
            async with dirty_manager.hold_dirty(tsv_path, sync_callback) as result:
                # result tells us if we synced, acquired, or already held
                await write_to_sqlite()
        """
        dirty_dir = dirty_dir_for_tsv(tsv_path)
        mark_path = self._dirtymark_path(dirty_dir)

        logging.debug(f"Attempting to claim dirty mark for {tsv_path}")

        while True:
            # Fast path: if we think we have a mark, try to lock it
            if tsv_path in self._dirty_tsvs:
                try:
                    async with async_file_lock_acquire(mark_path, create_if_not_exists=False):
                        logging.debug(
                            f"_hold_dirty({tsv_path}): reclaimed dirty mark (still present)"
                        )
                        yield ClaimResult.ALREADY_HELD
                        logging.debug(f"_hold_dirty({tsv_path}): no longer holding dirty mark")
                        return
                except LockFileNotFoundError:
                    logging.debug(f"_hold_dirty({tsv_path}): previous dirty mark was removed!")
                    # Our mark was removed (by force-flush), clear in-memory state
                    self._dirty_tsvs.discard(tsv_path)
                    # Fall through to claim a new mark

            # Slow path: need to claim a mark
            result = await self._claim_mark(tsv_path, dirty_dir, sync_if_first)

            # Now try to lock our newly created mark
            try:
                async with async_file_lock_acquire(mark_path, create_if_not_exists=False):
                    logging.debug(f"_hold_dirty({tsv_path}): holding dirty mark (new mark)")
                    yield result
                    logging.debug(f"_hold_dirty({tsv_path}): no longer holding dirty mark")
                    return
            except LockFileNotFoundError:
                logging.debug(
                    f"_hold_dirty({tsv_path}): race: couldn't acquire new claimed lock, retrying"
                )
                # Race: force-flush deleted our mark between claim and lock
                self._dirty_tsvs.discard(tsv_path)
                # Retry the whole thing
                continue

    async def _claim_mark(
        self,
        tsv_path: str,
        dirty_dir: str,
        sync_if_first: Callable[[], Awaitable[None]],
    ) -> ClaimResult:
        """Claim a dirty mark, creating it if needed.

        Must be called when we don't have a mark. Creates the mark file
        and returns the result. Caller should then lock the mark.

        SAFETY INVARIANT: sync_if_first is called directly (not via ensure_synced)
        to ensure we ALWAYS do a full sync when first marking as dirty, regardless
        of any staleness caching. This prevents missing external TSV modifications
        that occurred between a read and a subsequent write.

        Args:
            tsv_path: Absolute path to the TSV file.
            dirty_dir: Path to the dirty directory.
            sync_if_first: Callback for first claim. Called directly without any
                staleness checks.

        Returns:
            SYNCED if we were first (called sync_if_first), ACQUIRED otherwise.
        """
        state_lock_path = self._state_lock_path(dirty_dir)
        sync_lock_path = sync_lock_path_for_tsv(tsv_path)
        sentinel_path = self._sentinel_path(dirty_dir)

        async with async_file_lock_acquire(state_lock_path):
            if os.path.exists(sentinel_path):
                # Sentinel exists - another process already went dirty
                # Just create our mark and join
                logging.debug(f"_claim_mark({tsv_path}): is already dirty, creating new mark")
                self._create_mark_files(dirty_dir)
                self._dirty_tsvs.add(tsv_path)
                return ClaimResult.ACQUIRED
            else:
                # No sentinel - we're first, need to sync
                # Use delete_on_release=True to clean up the sync_lock file after use
                async with async_file_lock_acquire(sync_lock_path, delete_on_release=True):
                    logging.debug(f"_claim_mark({tsv_path}): was not dirty, syncing")

                    # Capture TSV state before sync (same pattern as ensure_synced)
                    existed = os.path.exists(tsv_path)
                    pre_sync_mtime = _get_mtime(tsv_path)
                    pre_sync_checksum = _compute_file_checksum(tsv_path)

                    # Touch sync_lock to get a reference mtime with filesystem granularity
                    _touch(sync_lock_path)
                    reference_mtime = _get_mtime(sync_lock_path)

                    await sync_if_first()

                    # Update staleness timestamp (we just synced)
                    self._last_sync_time[tsv_path] = time.monotonic()

                    # Update cache so flush knows what state we synced from
                    if existed:
                        self._tsv_state[tsv_path] = TsvSyncState(
                            tsv_mtime=pre_sync_mtime,
                            tsv_checksum=pre_sync_checksum,
                            reference_mtime=reference_mtime,
                        )
                    else:
                        # File didn't exist - remove any stale cache
                        self._tsv_state.pop(tsv_path, None)

                    self._create_mark_files(dirty_dir)
                    self._create_sentinel(dirty_dir)
                    logging.debug(
                        f"_claim_mark({tsv_path}): synced to SQLite, created sentinel and new mark"
                    )
                    self._dirty_tsvs.add(tsv_path)
                    return ClaimResult.SYNCED

    def _create_mark_files(self, dirty_dir: str) -> None:
        """Create our dirty mark file and the flag file.

        Must be called while holding state lock.
        """
        os.makedirs(dirty_dir, exist_ok=True)
        mark_path = self._dirtymark_path(dirty_dir)
        flag_path = self._flag_path(dirty_dir)

        # Touch our mark file
        _touch(mark_path)
        # Touch the flag file (set on every claim, used for staleness detection)
        _touch(flag_path)

    def _create_sentinel(self, dirty_dir: str) -> None:
        """Create the sentinel file.

        Must be called while holding state lock.
        """
        os.makedirs(dirty_dir, exist_ok=True)
        sentinel_path = self._sentinel_path(dirty_dir)
        with open(sentinel_path, "w") as f:
            f.write(SENTINEL_CONTENT)

    async def ensure_synced(
        self,
        tsv_path: str,
        sync_fn: Callable[[], Awaitable[None]],
    ) -> bool:
        """Ensure TSV is synced to SQLite, running sync_fn if needed.

        This is the primary API for reads. Checks if sync is needed based on
        dirty state, time-based staleness, and mtime/checksum, acquires sync
        lock if needed, and runs sync_fn.

        Algorithm:
        1. If dirty → return False (SQLite is authoritative)
        1.5. If synced within staleness window → return False (time-based optimization)
        2. If mtime unchanged and mtime < reference_mtime → return False
        3. Acquire sync lock
        4. Re-check dirty state (may have changed while waiting)
        5. Do checksum check if mtime matches reference_mtime
        6. If sync needed:
           - call sync_fn()
           - update staleness timestamp
           - update mtime/checksum cache
           - if file was removed, also remove .sync_lock file

        Args:
            tsv_path: Absolute path to the TSV file.
            sync_fn: Async callback to sync TSV → SQLite.

        Returns:
            True if sync was performed, False if skipped.
        """
        sync_lock_path = sync_lock_path_for_tsv(tsv_path)

        # Step 1: If dirty, SQLite is authoritative
        if self.is_dirty(tsv_path):
            logging.debug(f"ensure_synced({tsv_path}): SQLite is already authoritative (dirty)")
            return False

        # Step 1.5: Check time-based staleness (READ PATH ONLY)
        # SAFETY NOTE: This staleness check does NOT affect writes. When marking a TSV
        # as dirty via hold_dirty(), _claim_mark() calls sync_if_first directly,
        # bypassing this method entirely. This ensures writes always get the latest
        # TSV state, regardless of staleness settings.
        if self._sync_staleness_threshold is not None and self._sync_staleness_threshold > 0:
            last_sync = self._last_sync_time.get(tsv_path)
            if last_sync is not None:
                elapsed = time.monotonic() - last_sync
                if elapsed < self._sync_staleness_threshold:
                    logging.debug(
                        f"ensure_synced({tsv_path}): synced {elapsed:.3f}s ago, "
                        f"within staleness window ({self._sync_staleness_threshold}s)"
                    )
                    return False

        # Step 2: Quick mtime check without lock
        cached = self._tsv_state.get(tsv_path)
        current_mtime = _get_mtime(tsv_path)

        if cached is not None:
            # If mtime unchanged and less than our reference_mtime, no sync needed
            if current_mtime == cached.tsv_mtime and current_mtime < cached.reference_mtime:
                logging.debug(f"ensure_synced({tsv_path}): mtime unchanged, skipping sync")
                return False
            # If mtime unchanged but equal to reference_mtime, need checksum check
            # (potential race - TSV and sync_lock touched at same instant)

        # Step 3-6: Need to acquire sync lock and potentially sync
        # Use delete_on_release=True to clean up the sync_lock file after use
        async with async_file_lock_acquire(sync_lock_path, delete_on_release=True):
            # Step 4: Re-check dirty state (someone may have gone dirty while we waited)
            if self.is_dirty(tsv_path):
                logging.debug(f"ensure_synced({tsv_path}): newly dirty, skipping sync")
                return False

            # Re-read mtime after acquiring lock
            current_mtime = _get_mtime(tsv_path)
            current_checksum = None  # Lazy compute
            existed = os.path.exists(tsv_path)

            # Check if sync is needed
            if cached is None:
                # No cached state - need to sync
                need_sync = True
            elif current_mtime != cached.tsv_mtime or current_mtime == cached.reference_mtime:
                # mtime changed - likely need sync, but check edge cases
                if current_mtime > cached.reference_mtime:
                    # TSV is newer than our reference - sync needed
                    need_sync = True
                elif current_mtime == cached.reference_mtime:
                    # Potential race - do checksum check
                    current_checksum = _compute_file_checksum(tsv_path)
                    if current_checksum == cached.tsv_checksum:
                        need_sync = False
                    else:
                        need_sync = True
                else:
                    # mtime < reference_mtime but mtime changed from cached
                    # Shouldn't normally happen, but sync to be safe
                    need_sync = True
            else:
                # Everything up to date.
                need_sync = False

            if not need_sync:
                logging.debug(f"ensure_synced({tsv_path}): no sync needed")
                # Update mtime cache in case it drifted
                if cached is not None and current_mtime != cached.tsv_mtime:
                    cached.tsv_mtime = current_mtime
                return False

            # Step 6: Sync needed
            # Capture state before sync (sync_fn will update SQLite to match this content)
            pre_sync_mtime = current_mtime
            pre_sync_checksum = current_checksum or _compute_file_checksum(tsv_path)

            # Touch sync_lock to get a reference mtime with filesystem granularity
            _touch(sync_lock_path)
            reference_mtime = _get_mtime(sync_lock_path)

            logging.debug(f"ensure_synced({tsv_path}): syncing")

            # Run the sync
            await sync_fn()

            # Update staleness timestamp (we just synced)
            self._last_sync_time[tsv_path] = time.monotonic()

            # Update cache if the file existed when we captured state
            if existed:
                logging.debug(f"ensure_synced({tsv_path}): updating cache")
                self._tsv_state[tsv_path] = TsvSyncState(
                    tsv_mtime=pre_sync_mtime,
                    tsv_checksum=pre_sync_checksum,
                    reference_mtime=reference_mtime,
                )
            else:
                logging.debug(f"ensure_synced({tsv_path}): file was deleted, cleaning up")
                self._tsv_state.pop(tsv_path, None)

            return True

    def discover_tsvs_needing_sync(self) -> list[str]:
        """Scan for TSV files that may need syncing.

        Does a fresh directory scan (no directory mtime caching) and compares
        against cached state. Returns TSVs that are:
        - New (not in cache)
        - Possibly modified (mtime changed or mtime == reference_mtime)
        - Deleted (in cache but file missing)

        Returns:
            List of absolute paths to TSV files that should be checked/synced.
        """
        result: list[str] = []
        found_tsvs: set[str] = set()

        # Scan for all .fxtr.tsv files under store root
        for p in Path(self._namespace_root).rglob("*.fxtr.tsv"):
            tsv_path = str(p.resolve())
            found_tsvs.add(tsv_path)

            # Skip if dirty (SQLite is authoritative)
            if self.is_dirty(tsv_path):
                continue

            cached = self._tsv_state.get(tsv_path)
            if cached is None:
                # New file - needs sync
                result.append(tsv_path)
            else:
                # Check if mtime changed
                current_mtime = _get_mtime(tsv_path)
                if current_mtime != cached.tsv_mtime:
                    result.append(tsv_path)
                elif current_mtime == cached.reference_mtime:
                    # Potential race condition - include for checksum check
                    result.append(tsv_path)

        # Check for deleted TSVs (in cache but not on disk)
        for cached_path in list(self._tsv_state.keys()):
            if cached_path not in found_tsvs:
                # File was deleted - need to sync (will remove pins)
                # But skip if dirty (shouldn't happen for deleted files, but be safe)
                if not self.is_dirty(cached_path):
                    result.append(cached_path)

        return result

    async def flush(
        self,
        tsv_path: str,
        dump_fn: Callable[[str], Awaitable[None]],
        force: bool = False,
    ) -> FlushResult:
        """Flush dirty TSV to disk.

        Writes sqlite state back to the TSV file using the provided callback.
        The callback may be called multiple times if a RESTART condition is
        detected (another process claimed while we were dumping).

        The dump_fn receives a temp file path to write to. This module handles
        the atomic swap from temp to final TSV, with backup protection against
        concurrent edits.

        Args:
            tsv_path: Absolute path to the TSV file.
            dump_fn: Async callback that writes content to the given temp file path.
                May be called multiple times if RESTART occurs.
            force: If True, remove other marks (waiting for active writers)
                and flush. Used by force-flush CLI command.

        Returns:
            FlushResult indicating what happened:
            - COMPLETE: Successfully flushed
            - NO_MARK: We didn't have a dirty mark (or lost it during force)
            - DEFERRED: Other writers exist, removed our mark, let them flush
        """
        # Check we have a mark
        if tsv_path not in self._dirty_tsvs:
            logging.debug(f"flush({tsv_path}): no dirty mark, nothing to flush")
            return FlushResult.NO_MARK

        dirty_dir = dirty_dir_for_tsv(tsv_path)
        state_lock_path = self._state_lock_path(dirty_dir)
        sync_lock_path = sync_lock_path_for_tsv(tsv_path)
        our_dirty_mark_path = self._dirtymark_path(dirty_dir)

        while True:  # RESTART loop
            # Step 1-3: Check marks under state lock
            async with async_file_lock_acquire(state_lock_path):
                # Check our mark still exists
                if not os.path.exists(our_dirty_mark_path):
                    logging.debug(f"flush({tsv_path}): our mark is gone, nothing to flush")
                    self._dirty_tsvs.discard(tsv_path)
                    return FlushResult.NO_MARK

                # Check other marks
                other_marks = self._list_other_marks(dirty_dir)

                if other_marks and force:
                    # Force mode: remove other marks while holding state lock
                    logging.debug(
                        f"flush({tsv_path}): force mode, removing {len(other_marks)} other marks"
                    )
                    for mark_path in other_marks:
                        # Lock the mark to wait for any active writer to finish
                        try:
                            async with async_file_lock_acquire(mark_path):
                                try:
                                    os.unlink(mark_path)
                                    logging.debug(f"flush({tsv_path}): removed mark {mark_path}")
                                except FileNotFoundError:
                                    pass
                        except LockFileNotFoundError:
                            pass  # Already removed

                    # Re-check our mark (race with another force-flusher)
                    if not os.path.exists(our_dirty_mark_path):
                        logging.debug(
                            f"flush({tsv_path}): our mark was removed during force, "
                            "another process must have force-flushed"
                        )
                        self._dirty_tsvs.discard(tsv_path)
                        return FlushResult.NO_MARK

                elif other_marks and not force:
                    # Normal mode: defer to other writers
                    logging.debug(f"flush({tsv_path}): other writers exist, deferring flush")
                    self._remove_our_mark(dirty_dir)
                    self._dirty_tsvs.discard(tsv_path)
                    return FlushResult.DEFERRED

            # Step 5-8: Acquire sync lock, then re-acquire state lock
            # Use delete_on_release=True to clean up the sync_lock file after use
            async with async_file_lock_acquire(sync_lock_path, delete_on_release=True):
                # Acquire our own dirty mark lock. We need this in order to ensure that this process
                # is not simultaneously writing to sqlite.
                try:
                    async with async_file_lock_acquire(
                        our_dirty_mark_path, create_if_not_exists=False
                    ):

                        async with async_file_lock_acquire(state_lock_path):
                            # Re-check if alone (only if not force)
                            if not force:
                                other_marks = self._list_other_marks(dirty_dir)
                                if other_marks:
                                    logging.debug(
                                        f"flush({tsv_path}) (with sync lock): other writers exist,"
                                        " deferring flush"
                                    )
                                    self._remove_our_mark(dirty_dir)
                                    self._dirty_tsvs.discard(tsv_path)
                                    return FlushResult.DEFERRED

                            # Step 9: Remove flag before dumping
                            flag_path = self._flag_path(dirty_dir)
                            if os.path.exists(flag_path):
                                logging.debug(f"flush({tsv_path}): clearing dirtymark flag")
                                os.unlink(flag_path)

                        # Step 10: Release state lock, keep sync lock.

                        # Step 11: Create temp file and call dump_fn (holding sync lock)
                        temp_path = f"{dirty_dir}/dump-{uuid.uuid4()}"
                        logging.debug(
                            f"flush({tsv_path}): calling dump_fn with temp_path={temp_path}"
                        )
                        await dump_fn(temp_path)
                        logging.debug(f"flush({tsv_path}): dump_fn completed")

                        # Step 12: Atomic swap with backup protection
                        backup_path: str | None = None
                        old_tsv_existed = os.path.exists(tsv_path)

                        if old_tsv_existed:
                            # Move old TSV to backup location
                            backup_path = _make_backup_path(tsv_path)
                            os.rename(tsv_path, backup_path)
                            logging.debug(
                                f"flush({tsv_path}): moved old TSV to backup {backup_path}"
                            )

                        # Atomic rename temp → TSV
                        if os.path.exists(temp_path):
                            os.rename(temp_path, tsv_path)
                            logging.debug(f"flush({tsv_path}): renamed temp to TSV")
                        else:
                            # dump_fn didn't write anything (empty content case)
                            # TSV should remain deleted
                            logging.debug(
                                f"flush({tsv_path}): temp file not created (empty content)"
                            )

                        # Step 13: Check backup checksum and decide whether to keep it
                        if backup_path is not None:
                            backup_checksum = _compute_file_checksum(backup_path)
                            cached = self._tsv_state.get(tsv_path)

                            if cached is not None:
                                # We have cached state - compare checksums
                                if backup_checksum == cached.tsv_checksum:
                                    # Checksum matches - no concurrent edit, delete backup
                                    try:
                                        os.unlink(backup_path)
                                        logging.debug(
                                            f"flush({tsv_path}): deleted backup (checksum matched)"
                                        )
                                    except FileNotFoundError:
                                        pass
                                else:
                                    # Checksum mismatch - concurrent edit detected!
                                    logger.warning(
                                        f"flush({tsv_path}): CONCURRENT EDIT DETECTED! TSV was"
                                        " modified while dirty (expected checksum"
                                        f" {cached.tsv_checksum[:16]}..., got"
                                        f" {backup_checksum[:16]}...). Backup saved to"
                                        f" {backup_path}"
                                    )
                            else:
                                # No cached state but file existed - unexpected, keep backup
                                logger.warning(
                                    f"flush({tsv_path}): TSV existed but no cached state. "
                                    f"Backup saved to {backup_path}"
                                )

                        # Update sync_lock mtime by touching it
                        _touch(sync_lock_path)
                        reference_mtime = _get_mtime(sync_lock_path)

                        # Update mtime/checksum cache
                        if os.path.exists(tsv_path):
                            logging.debug(f"flush({tsv_path}): updating cache")
                            self._tsv_state[tsv_path] = TsvSyncState(
                                tsv_mtime=_get_mtime(tsv_path),
                                tsv_checksum=_compute_file_checksum(tsv_path),
                                reference_mtime=reference_mtime,
                            )
                        else:
                            logging.debug(f"flush({tsv_path}): TSV was deleted, removing cache")
                            self._tsv_state.pop(tsv_path, None)

                        # Step 14-15: Re-acquire state lock and check for RESTART
                        async with async_file_lock_acquire(state_lock_path):
                            # Check if flag reappeared (someone claimed while we were dumping)
                            flag_path = self._flag_path(dirty_dir)
                            if os.path.exists(flag_path):
                                # RESTART - someone claimed, our dump may be stale
                                logging.debug(
                                    f"flush({tsv_path}): flag reappeared, restarting flush"
                                )
                                continue  # Restart the loop

                            # Step 16-17: Atomic cleanup
                            # Rename dirty dir to temp location
                            removing_dir = f"{dirty_dir}.removing-{uuid.uuid4()}"
                            try:
                                os.rename(dirty_dir, removing_dir)
                            except FileNotFoundError:
                                # Already removed by someone else
                                logging.warning(
                                    f"flush({tsv_path}): dirty dir was already removed?"
                                )
                            else:
                                logging.debug(f"flush({tsv_path}): removed dirty dir")
                                # Delete the removing dir
                                shutil.rmtree(removing_dir, ignore_errors=True)

                        # Successful flush. Mark it as clean.
                        self._dirty_tsvs.discard(tsv_path)

                except LockFileNotFoundError:
                    logging.debug(
                        f"flush({tsv_path}) (with sync lock): our mark is gone, nothing to flush"
                    )
                    self._dirty_tsvs.discard(tsv_path)
                    return FlushResult.NO_MARK

                return FlushResult.COMPLETE

    def _list_other_marks(self, dirty_dir: str) -> list[str]:
        """List dirty mark files from other managers.

        Must be called while holding state lock.
        """
        our_mark_name = f"{DIRTYMARK_PREFIX}{self.manager_uuid}"
        other_marks = []
        if os.path.exists(dirty_dir):
            for name in os.listdir(dirty_dir):
                if name.startswith(DIRTYMARK_PREFIX) and name != our_mark_name:
                    other_marks.append(f"{dirty_dir}/{name}")
        return other_marks

    def _remove_our_mark(self, dirty_dir: str) -> None:
        """Remove our dirty mark file.

        Must be called while holding state lock.
        """
        mark_path = self._dirtymark_path(dirty_dir)
        try:
            os.unlink(mark_path)
        except FileNotFoundError:
            pass

    def get_dirty_tsvs(self) -> set[str]:
        """Get the set of TSV paths we may have dirty marks for.

        This is a conservative estimate - returns TSVs where we claimed
        a mark and didn't clean it up ourselves. However, another process
        may have force-removed our marks, so some of these may no longer
        be valid.

        Returns:
            A copy of the set of potentially dirty TSV paths.
        """
        return set(self._dirty_tsvs)

    def clear_staleness_cache(self, tsv_path: str | None = None) -> None:
        """Clear staleness cache to force fresh reads on next access.

        Clears both time-based staleness and mtime/checksum caches. Use this when
        you know external changes have occurred and want to force re-sync.

        Args:
            tsv_path: Clear cache for specific TSV (absolute path), or None to clear all.
        """
        if tsv_path is None:
            logger.debug("clear_staleness_cache: clearing all caches")
            self._last_sync_time.clear()
            self._tsv_state.clear()
        else:
            logger.debug(f"clear_staleness_cache: clearing cache for {tsv_path}")
            self._last_sync_time.pop(tsv_path, None)
            self._tsv_state.pop(tsv_path, None)
