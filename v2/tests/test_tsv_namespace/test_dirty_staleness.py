"""Tests for tsv namespace dirty mark manager staleness checking."""

from pathlib import Path

import anyio
import pytest

from fxtr2.data.tsv_namespace.dirty import ClaimResult, DirtyMarkManager

pytestmark = pytest.mark.anyio


class TestStalenessThreshold:
    """Tests for time-based staleness checking."""

    def test_staleness_threshold_parameter(self, tmp_path: Path):
        """DirtyMarkManager should accept and store staleness threshold."""
        # Test with None
        manager = DirtyMarkManager(str(tmp_path), sync_staleness_threshold=None)
        assert manager._sync_staleness_threshold is None

        # Test with 0
        manager = DirtyMarkManager(str(tmp_path), sync_staleness_threshold=0)
        assert manager._sync_staleness_threshold == 0

        # Test with positive value
        manager = DirtyMarkManager(str(tmp_path), sync_staleness_threshold=0.5)
        assert manager._sync_staleness_threshold == 0.5

    async def test_staleness_prevents_sync_within_window(self, tmp_path: Path):
        """Staleness should prevent sync within the time window."""
        # Create a TSV file
        tsv_path = str(tmp_path / "test.fxtr.tsv")
        Path(tsv_path).write_text("key1\tid1\n")

        # Create manager with 1s staleness window
        manager = DirtyMarkManager(str(tmp_path), sync_staleness_threshold=1.0)

        # First sync - should actually sync
        sync_count = 0

        async def sync_callback():
            nonlocal sync_count
            sync_count += 1

        synced = await manager.ensure_synced(tsv_path, sync_callback)
        assert synced is True
        assert sync_count == 1

        # Modify TSV externally
        Path(tsv_path).write_text("key2\tid2\n")

        # Second sync within window - should skip due to staleness
        synced = await manager.ensure_synced(tsv_path, sync_callback)
        assert synced is False  # Skipped due to staleness
        assert sync_count == 1  # Callback not called again

    async def test_staleness_expires_after_window(self, tmp_path: Path):
        """Staleness should expire after the time window."""
        # Create a TSV file
        tsv_path = str(tmp_path / "test.fxtr.tsv")
        Path(tsv_path).write_text("key1\tid1\n")

        # Create manager with 50ms staleness window
        manager = DirtyMarkManager(str(tmp_path), sync_staleness_threshold=0.05)

        # First sync
        sync_count = 0

        async def sync_callback():
            nonlocal sync_count
            sync_count += 1

        synced = await manager.ensure_synced(tsv_path, sync_callback)
        assert synced is True
        assert sync_count == 1

        # Modify TSV externally
        Path(tsv_path).write_text("key2\tid2\n")

        # Sleep to let staleness expire
        await anyio.sleep(0.06)

        # Second sync after window - should sync again
        synced = await manager.ensure_synced(tsv_path, sync_callback)
        assert synced is True  # Synced because staleness expired
        assert sync_count == 2  # Callback called again

    async def test_threshold_none_always_syncs(self, tmp_path: Path):
        """threshold=None should always check mtime (no staleness)."""
        # Create a TSV file
        tsv_path = str(tmp_path / "test.fxtr.tsv")
        Path(tsv_path).write_text("key1\tid1\n")

        # Create manager with None threshold (always check mtime)
        manager = DirtyMarkManager(str(tmp_path), sync_staleness_threshold=None)

        # First sync
        sync_count = 0

        async def sync_callback():
            nonlocal sync_count
            sync_count += 1

        synced = await manager.ensure_synced(tsv_path, sync_callback)
        assert synced is True
        assert sync_count == 1

        # Modify TSV externally
        Path(tsv_path).write_text("key2\tid2\n")

        # Second sync immediately - should sync (mtime changed)
        synced = await manager.ensure_synced(tsv_path, sync_callback)
        assert synced is True
        assert sync_count == 2  # Callback called again

    async def test_threshold_zero_always_syncs(self, tmp_path: Path):
        """threshold=0 should always check mtime (no staleness)."""
        # Create a TSV file
        tsv_path = str(tmp_path / "test.fxtr.tsv")
        Path(tsv_path).write_text("key1\tid1\n")

        # Create manager with 0 threshold (always check mtime)
        manager = DirtyMarkManager(str(tmp_path), sync_staleness_threshold=0)

        # First sync
        sync_count = 0

        async def sync_callback():
            nonlocal sync_count
            sync_count += 1

        synced = await manager.ensure_synced(tsv_path, sync_callback)
        assert synced is True
        assert sync_count == 1

        # Modify TSV externally
        Path(tsv_path).write_text("key2\tid2\n")

        # Second sync immediately - should sync (mtime changed)
        synced = await manager.ensure_synced(tsv_path, sync_callback)
        assert synced is True
        assert sync_count == 2  # Callback called again

    async def test_write_path_bypasses_staleness(self, tmp_path: Path):
        """CRITICAL SAFETY TEST: Write path must always sync regardless of staleness."""
        # Create a TSV file
        tsv_path = str(tmp_path / "test.fxtr.tsv")
        Path(tsv_path).write_text("key1\tid1\n")

        # Create manager with long staleness window (10s)
        manager = DirtyMarkManager(str(tmp_path), sync_staleness_threshold=10.0)

        # First: read path sync
        read_sync_count = 0

        async def read_sync_callback():
            nonlocal read_sync_count
            read_sync_count += 1

        synced = await manager.ensure_synced(tsv_path, read_sync_callback)
        assert synced is True
        assert read_sync_count == 1

        # Modify TSV externally
        Path(tsv_path).write_text("key2\tid2\n")

        # Verify that ensure_synced would skip (within staleness window)
        synced = await manager.ensure_synced(tsv_path, read_sync_callback)
        assert synced is False  # Skipped due to staleness
        assert read_sync_count == 1

        # Now: write path via hold_dirty (first writer)
        # This should sync even though we're within staleness window
        write_sync_count = 0
        write_sync_content = None

        async def write_sync_callback():
            nonlocal write_sync_count, write_sync_content
            write_sync_count += 1
            # Read the file to verify we got the modified content
            write_sync_content = Path(tsv_path).read_text()

        result = None
        async with manager.hold_dirty(tsv_path, write_sync_callback) as claim_result:
            result = claim_result

        # Verify sync happened (first to go dirty)
        assert result == ClaimResult.SYNCED
        assert write_sync_count == 1  # Callback was called
        assert write_sync_content == "key2\tid2\n"  # Got the MODIFIED content

        # This is the critical assertion: despite being within staleness window,
        # the write path bypassed staleness and got the latest TSV state
        assert write_sync_content != "key1\tid1\n"  # Not the old content

    async def test_clear_staleness_cache_single_tsv(self, tmp_path: Path):
        """clear_staleness_cache should clear cache for specific TSV."""
        # Create a TSV file
        tsv_path = str(tmp_path / "test.fxtr.tsv")
        Path(tsv_path).write_text("key1\tid1\n")

        # Create manager with 1s staleness window
        manager = DirtyMarkManager(str(tmp_path), sync_staleness_threshold=1.0)

        # First sync
        sync_count = 0

        async def sync_callback():
            nonlocal sync_count
            sync_count += 1

        synced = await manager.ensure_synced(tsv_path, sync_callback)
        assert synced is True
        assert sync_count == 1

        # Modify TSV externally
        Path(tsv_path).write_text("key2\tid2\n")

        # Verify staleness prevents sync
        synced = await manager.ensure_synced(tsv_path, sync_callback)
        assert synced is False
        assert sync_count == 1

        # Clear staleness cache for this TSV
        manager.clear_staleness_cache(tsv_path)

        # Now sync should happen
        synced = await manager.ensure_synced(tsv_path, sync_callback)
        assert synced is True
        assert sync_count == 2  # Callback called after cache clear

    async def test_clear_staleness_cache_all_tsvs(self, tmp_path: Path):
        """clear_staleness_cache(None) should clear cache for all TSVs."""
        # Create two TSV files
        tsv_path1 = str(tmp_path / "test1.fxtr.tsv")
        tsv_path2 = str(tmp_path / "test2.fxtr.tsv")
        Path(tsv_path1).write_text("key1\tid1\n")
        Path(tsv_path2).write_text("key2\tid2\n")

        # Create manager with 1s staleness window
        manager = DirtyMarkManager(str(tmp_path), sync_staleness_threshold=1.0)

        # Sync both TSVs
        sync_count1 = 0
        sync_count2 = 0

        async def sync_callback1():
            nonlocal sync_count1
            sync_count1 += 1

        async def sync_callback2():
            nonlocal sync_count2
            sync_count2 += 1

        await manager.ensure_synced(tsv_path1, sync_callback1)
        await manager.ensure_synced(tsv_path2, sync_callback2)
        assert sync_count1 == 1
        assert sync_count2 == 1

        # Modify both TSVs externally
        Path(tsv_path1).write_text("key1\tid1_modified\n")
        Path(tsv_path2).write_text("key2\tid2_modified\n")

        # Verify staleness prevents both syncs
        await manager.ensure_synced(tsv_path1, sync_callback1)
        await manager.ensure_synced(tsv_path2, sync_callback2)
        assert sync_count1 == 1  # Still 1
        assert sync_count2 == 1  # Still 1

        # Clear ALL staleness caches
        manager.clear_staleness_cache(None)

        # Now both should sync
        await manager.ensure_synced(tsv_path1, sync_callback1)
        await manager.ensure_synced(tsv_path2, sync_callback2)
        assert sync_count1 == 2  # Incremented
        assert sync_count2 == 2  # Incremented
