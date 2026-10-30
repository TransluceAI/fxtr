"""Tests for tsv namespace dirty mark management."""

import os
from pathlib import Path

import anyio
import pytest

from fxtr2.data.tsv_namespace.dirty import (
    DIRTYMARK_FLAG_FILENAME,
    DIRTYMARK_PREFIX,
    SENTINEL_FILENAME,
    ClaimResult,
    DirtyMarkManager,
    FlushResult,
    dirty_dir_for_tsv,
    sync_lock_path_for_tsv,
)

pytestmark = pytest.mark.anyio


class TestDirtyDirHelpers:
    """Tests for helper functions."""

    def test_dirty_dir_for_tsv(self, tmp_path: Path):
        """dirty_dir_for_tsv should return correct path."""
        tsv_path = str(tmp_path / "data" / "models.fxtr.tsv")
        dirty_dir = dirty_dir_for_tsv(tsv_path)

        assert dirty_dir == f"{tsv_path}.dirty"

    def test_sync_lock_path_for_tsv(self, tmp_path: Path):
        """sync_lock_path_for_tsv should return correct path."""
        tsv_path = str(tmp_path / "data" / "models.fxtr.tsv")
        sync_lock = sync_lock_path_for_tsv(tsv_path)

        assert sync_lock == f"{tsv_path}.sync_lock"


class TestDirtyMarkManagerBasic:
    """Basic tests for DirtyMarkManager."""

    def test_manager_has_unique_uuid(self, tmp_path: Path):
        """Each manager should have a unique UUID."""
        manager1 = DirtyMarkManager(str(tmp_path))
        manager2 = DirtyMarkManager(str(tmp_path))

        assert manager1.manager_uuid != manager2.manager_uuid

    def test_may_have_dirty_mark_initially_false(self, tmp_path: Path):
        """may_have_dirty_mark should return False before claiming."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path = str(tmp_path / "test.fxtr.tsv")

        assert manager.may_have_dirty_mark(tsv_path) is False

    def test_is_dirty_initially_false(self, tmp_path: Path):
        """is_dirty should return False when no sentinel exists."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path = str(tmp_path / "test.fxtr.tsv")

        assert manager.is_dirty(tsv_path) is False


class TestHoldDirty:
    """Tests for hold_dirty context manager."""

    async def test_hold_dirty_first_calls_sync_callback(self, tmp_path: Path):
        """First hold_dirty should call sync_if_first callback and return SYNCED."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.touch()
        tsv_path = str(tsv_path_p)

        sync_called = False

        async def sync_callback():
            nonlocal sync_called
            sync_called = True

        async with manager.hold_dirty(tsv_path, sync_if_first=sync_callback) as result:
            assert result == ClaimResult.SYNCED
            assert sync_called is True
            assert manager.may_have_dirty_mark(tsv_path) is True

    async def test_hold_dirty_creates_sentinel(self, tmp_path: Path):
        """hold_dirty should create sentinel file on first claim."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.touch()
        tsv_path = str(tsv_path_p)

        async def sync_callback():
            pass

        async with manager.hold_dirty(tsv_path, sync_if_first=sync_callback):
            dirty_dir = dirty_dir_for_tsv(tsv_path)
            sentinel_path = f"{dirty_dir}/{SENTINEL_FILENAME}"
            assert os.path.exists(sentinel_path)

    async def test_hold_dirty_creates_mark_file(self, tmp_path: Path):
        """hold_dirty should create dirtymark-{uuid} file."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.touch()
        tsv_path = str(tsv_path_p)

        async def sync_callback():
            pass

        async with manager.hold_dirty(tsv_path, sync_if_first=sync_callback):
            dirty_dir = dirty_dir_for_tsv(tsv_path)
            mark_path = f"{dirty_dir}/{DIRTYMARK_PREFIX}{manager.manager_uuid}"
            assert os.path.exists(mark_path)

    async def test_hold_dirty_creates_flag_file(self, tmp_path: Path):
        """hold_dirty should create dirtymark_flag file."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.touch()
        tsv_path = str(tsv_path_p)

        async def sync_callback():
            pass

        async with manager.hold_dirty(tsv_path, sync_if_first=sync_callback):
            dirty_dir = dirty_dir_for_tsv(tsv_path)
            flag_path = f"{dirty_dir}/{DIRTYMARK_FLAG_FILENAME}"
            assert os.path.exists(flag_path)

    async def test_hold_dirty_second_manager_returns_acquired(self, tmp_path: Path):
        """Second manager claiming should return ACQUIRED (no sync)."""
        manager1 = DirtyMarkManager(str(tmp_path))
        manager2 = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.touch()
        tsv_path = str(tsv_path_p)

        sync_count = 0

        async def sync_callback():
            nonlocal sync_count
            sync_count += 1

        # First manager claims
        async with manager1.hold_dirty(tsv_path, sync_if_first=sync_callback) as result1:
            assert result1 == ClaimResult.SYNCED
            assert sync_count == 1

            # Second manager claims while first is still holding
            async with manager2.hold_dirty(tsv_path, sync_if_first=sync_callback) as result2:
                assert result2 == ClaimResult.ACQUIRED
                assert sync_count == 1  # Still 1, not called again

    async def test_hold_dirty_same_manager_returns_already_held(self, tmp_path: Path):
        """Same manager calling hold_dirty again should return ALREADY_HELD."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.touch()
        tsv_path = str(tsv_path_p)

        async def sync_callback():
            pass

        async with manager.hold_dirty(tsv_path, sync_if_first=sync_callback) as result1:
            assert result1 == ClaimResult.SYNCED

        # After exiting, call again
        async with manager.hold_dirty(tsv_path, sync_if_first=sync_callback) as result2:
            assert result2 == ClaimResult.ALREADY_HELD

    async def test_hold_dirty_reclams_if_mark_removed(self, tmp_path: Path):
        """hold_dirty should re-claim if our mark was externally removed."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.touch()
        tsv_path = str(tsv_path_p)

        sync_count = 0

        async def sync_callback():
            nonlocal sync_count
            sync_count += 1

        # First claim
        async with manager.hold_dirty(tsv_path, sync_if_first=sync_callback) as result1:
            assert result1 == ClaimResult.SYNCED
            assert sync_count == 1

        # Remove the mark file (simulating force-flush)
        dirty_dir = dirty_dir_for_tsv(tsv_path)
        mark_path = f"{dirty_dir}/{DIRTYMARK_PREFIX}{manager.manager_uuid}"
        os.unlink(mark_path)

        # Next call should detect missing mark and re-claim
        # Since sentinel still exists, should return ACQUIRED (not SYNCED)
        async with manager.hold_dirty(tsv_path, sync_if_first=sync_callback) as result2:
            assert result2 == ClaimResult.ACQUIRED
            assert sync_count == 1  # Sync not called again since sentinel exists

    async def test_hold_dirty_sync_callback_exception_no_sentinel_created(self, tmp_path: Path):
        """If sync_if_first raises, sentinel and mark files should not be created."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.touch()
        tsv_path = str(tsv_path_p)

        class SyncError(Exception):
            pass

        async def failing_sync():
            raise SyncError("sync failed")

        # Attempting to hold_dirty should propagate the exception
        with pytest.raises(SyncError, match="sync failed"):
            async with manager.hold_dirty(tsv_path, sync_if_first=failing_sync):
                pass  # Should never reach here

        # No dirty state should have been created
        dirty_dir = dirty_dir_for_tsv(tsv_path)
        sentinel_path = f"{dirty_dir}/{SENTINEL_FILENAME}"
        mark_path = f"{dirty_dir}/{DIRTYMARK_PREFIX}{manager.manager_uuid}"
        flag_path = f"{dirty_dir}/{DIRTYMARK_FLAG_FILENAME}"

        assert not os.path.exists(sentinel_path), "Sentinel should not be created if sync fails"
        assert not os.path.exists(mark_path), "Mark file should not be created if sync fails"
        assert not os.path.exists(flag_path), "Flag file should not be created if sync fails"
        assert not manager.may_have_dirty_mark(tsv_path), "In-memory state should not be set"

        # A subsequent attempt with a working callback should succeed
        async def working_sync():
            pass

        async with manager.hold_dirty(tsv_path, sync_if_first=working_sync) as result:
            assert result == ClaimResult.SYNCED
            assert os.path.exists(sentinel_path)


class TestMayHaveDirtyMark:
    """Tests for may_have_dirty_mark."""

    async def test_may_have_dirty_mark_true_after_hold(self, tmp_path: Path):
        """may_have_dirty_mark should return True after holding."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.touch()
        tsv_path = str(tsv_path_p)

        async def sync_callback():
            pass

        async with manager.hold_dirty(tsv_path, sync_if_first=sync_callback):
            assert manager.may_have_dirty_mark(tsv_path) is True

        # Still true after exiting context
        assert manager.may_have_dirty_mark(tsv_path) is True

    async def test_may_have_dirty_mark_can_be_stale(self, tmp_path: Path):
        """may_have_dirty_mark can return True even if mark was removed."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.touch()
        tsv_path = str(tsv_path_p)

        async def sync_callback():
            pass

        async with manager.hold_dirty(tsv_path, sync_if_first=sync_callback):
            pass

        # Remove mark externally
        dirty_dir = dirty_dir_for_tsv(tsv_path)
        mark_path = f"{dirty_dir}/{DIRTYMARK_PREFIX}{manager.manager_uuid}"
        os.unlink(mark_path)

        # In-memory state is stale
        assert manager.may_have_dirty_mark(tsv_path) is True


class TestIsDirty:
    """Tests for is_dirty."""

    async def test_is_dirty_true_when_sentinel_exists(self, tmp_path: Path):
        """is_dirty should return True when sentinel exists."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.touch()
        tsv_path = str(tsv_path_p)

        async def sync_callback():
            pass

        async with manager.hold_dirty(tsv_path, sync_if_first=sync_callback):
            # Any manager can check is_dirty
            other_manager = DirtyMarkManager(str(tmp_path))
            assert other_manager.is_dirty(tsv_path) is True

    async def test_is_dirty_false_when_no_sentinel(self, tmp_path: Path):
        """is_dirty should return False when no sentinel exists."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.touch()
        tsv_path = str(tsv_path_p)

        assert manager.is_dirty(tsv_path) is False


class TestFlush:
    """Tests for flush."""

    async def test_flush_returns_no_mark_without_holding(self, tmp_path: Path):
        """flush should return NO_MARK when we haven't held."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.touch()
        tsv_path = str(tsv_path_p)

        dump_called = False

        async def dump_fn(temp_path: str):
            nonlocal dump_called
            dump_called = True

        result = await manager.flush(tsv_path, dump_fn)

        assert result == FlushResult.NO_MARK
        assert dump_called is False

    async def test_flush_complete_when_alone(self, tmp_path: Path):
        """flush should return COMPLETE when we're the only writer."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.touch()
        tsv_path = str(tsv_path_p)

        async def sync_callback():
            pass

        dump_called = False

        async def dump_fn(temp_path: str):
            nonlocal dump_called
            dump_called = True
            with open(temp_path, "w") as f:
                f.write("flushed\tid\n")

        async with manager.hold_dirty(tsv_path, sync_if_first=sync_callback):
            pass

        result = await manager.flush(tsv_path, dump_fn)

        assert result == FlushResult.COMPLETE
        assert dump_called is True
        # Dirty dir should be removed
        assert not os.path.exists(dirty_dir_for_tsv(tsv_path))
        # In-memory state should be cleared
        assert manager.may_have_dirty_mark(tsv_path) is False
        # TSV should have new content
        assert tsv_path_p.read_text() == "flushed\tid\n"

    async def test_flush_deferred_when_others_present(self, tmp_path: Path):
        """flush should return DEFERRED when other writers exist."""
        manager1 = DirtyMarkManager(str(tmp_path))
        manager2 = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.touch()
        tsv_path = str(tsv_path_p)

        async def sync_callback():
            pass

        dump_called = False

        async def dump_fn(temp_path: str):
            nonlocal dump_called
            dump_called = True

        # Both managers claim
        async with manager1.hold_dirty(tsv_path, sync_if_first=sync_callback):
            async with manager2.hold_dirty(tsv_path, sync_if_first=sync_callback):
                pass

        # Manager1 flushes - should defer since manager2 still has mark
        result = await manager1.flush(tsv_path, dump_fn)

        assert result == FlushResult.DEFERRED
        assert dump_called is False
        # Manager1's mark should be removed
        assert manager1.may_have_dirty_mark(tsv_path) is False
        # But dirty dir still exists (manager2 still has a mark)
        assert os.path.exists(dirty_dir_for_tsv(tsv_path))

    async def test_flush_force_ignores_other_marks(self, tmp_path: Path):
        """flush with force=True should flush even with other marks."""
        manager1 = DirtyMarkManager(str(tmp_path))
        manager2 = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.touch()
        tsv_path = str(tsv_path_p)

        async def sync_callback():
            pass

        dump_called = False

        async def dump_fn(temp_path: str):
            nonlocal dump_called
            dump_called = True
            with open(temp_path, "w") as f:
                f.write("force-flushed\tid\n")

        # Both managers claim
        async with manager1.hold_dirty(tsv_path, sync_if_first=sync_callback):
            async with manager2.hold_dirty(tsv_path, sync_if_first=sync_callback):
                pass

        # Manager1 force flushes
        result = await manager1.flush(tsv_path, dump_fn, force=True)

        assert result == FlushResult.COMPLETE
        assert dump_called is True


class TestForceFlush:
    """Tests for flush(force=True)."""

    async def test_force_flush_removes_other_marks_and_flushes(self, tmp_path: Path):
        """flush(force=True) should remove other marks and complete flush."""
        manager1 = DirtyMarkManager(str(tmp_path))
        manager2 = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.touch()
        tsv_path = str(tsv_path_p)

        async def sync_callback():
            pass

        dump_called = []

        async def dump_fn(temp_path: str):
            dump_called.append(True)
            with open(temp_path, "w") as f:
                f.write("force-flushed\tid\n")

        # Both managers claim
        async with manager1.hold_dirty(tsv_path, sync_if_first=sync_callback):
            async with manager2.hold_dirty(tsv_path, sync_if_first=sync_callback):
                pass

        # Verify both marks exist
        dirty_dir = dirty_dir_for_tsv(tsv_path)
        assert os.path.exists(f"{dirty_dir}/{DIRTYMARK_PREFIX}{manager1.manager_uuid}")
        assert os.path.exists(f"{dirty_dir}/{DIRTYMARK_PREFIX}{manager2.manager_uuid}")

        # Manager1 force flushes
        result = await manager1.flush(tsv_path, dump_fn, force=True)

        assert result == FlushResult.COMPLETE
        assert len(dump_called) == 1
        # Dirty dir should be cleaned up
        assert not os.path.exists(dirty_dir)

    async def test_force_flush_returns_no_mark_without_mark(self, tmp_path: Path):
        """flush(force=True) should return NO_MARK without a dirty mark."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.touch()
        tsv_path = str(tsv_path_p)

        async def dump_fn(temp_path: str):
            pass

        result = await manager.flush(tsv_path, dump_fn, force=True)

        assert result == FlushResult.NO_MARK

    async def test_force_flush_returns_no_mark_when_our_mark_removed(self, tmp_path: Path):
        """flush(force=True) should return NO_MARK if our mark was removed."""
        manager1 = DirtyMarkManager(str(tmp_path))
        manager2 = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.touch()
        tsv_path = str(tsv_path_p)

        async def sync_callback():
            pass

        dump_called = []

        async def dump_fn(temp_path: str):
            dump_called.append(True)

        # Both managers claim
        async with manager1.hold_dirty(tsv_path, sync_if_first=sync_callback):
            async with manager2.hold_dirty(tsv_path, sync_if_first=sync_callback):
                pass

        # Remove manager1's mark (simulating another force-flush winning the race)
        dirty_dir = dirty_dir_for_tsv(tsv_path)
        os.unlink(f"{dirty_dir}/{DIRTYMARK_PREFIX}{manager1.manager_uuid}")

        # Manager1 tries to force flush - should return NO_MARK
        result = await manager1.flush(tsv_path, dump_fn, force=True)

        assert result == FlushResult.NO_MARK
        assert len(dump_called) == 0  # dump_fn should not be called


class TestGetDirtyTsvs:
    """Tests for get_dirty_tsvs."""

    async def test_get_dirty_tsvs_returns_held_paths(self, tmp_path: Path):
        """get_dirty_tsvs should return all TSV paths we've held."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv1_p = tmp_path / "one.fxtr.tsv"
        tsv2_p = tmp_path / "two.fxtr.tsv"
        tsv1_p.touch()
        tsv2_p.touch()
        tsv1 = str(tsv1_p)
        tsv2 = str(tsv2_p)

        async def sync_callback():
            pass

        async with manager.hold_dirty(tsv1, sync_if_first=sync_callback):
            pass
        async with manager.hold_dirty(tsv2, sync_if_first=sync_callback):
            pass

        dirty_tsvs = manager.get_dirty_tsvs()

        assert len(dirty_tsvs) == 2
        assert tsv1 in dirty_tsvs
        assert tsv2 in dirty_tsvs


class TestFlushRestart:
    """Tests for the RESTART behavior during flush."""

    async def test_flush_restarts_when_flag_appears(self, tmp_path: Path):
        """flush should restart if dirtymark_flag appears during dump."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.touch()
        tsv_path = str(tsv_path_p)

        async def sync_callback():
            pass

        dump_count = 0

        async def dump_fn(temp_path: str):
            nonlocal dump_count
            dump_count += 1
            with open(temp_path, "w") as f:
                f.write(f"dump{dump_count}\tid\n")
            if dump_count == 1:
                # First call: simulate another process claiming
                dirty_dir = dirty_dir_for_tsv(tsv_path)
                flag_path = f"{dirty_dir}/{DIRTYMARK_FLAG_FILENAME}"
                with open(flag_path, "a"):
                    os.utime(flag_path, None)

        async with manager.hold_dirty(tsv_path, sync_if_first=sync_callback):
            pass

        result = await manager.flush(tsv_path, dump_fn)

        assert result == FlushResult.COMPLETE
        # dump_fn should be called twice due to restart
        assert dump_count == 2


class TestConcurrentHoldDirty:
    """Tests for concurrent hold_dirty operations."""

    async def test_concurrent_hold_dirty_only_one_syncs(self, tmp_path: Path):
        """When multiple managers hold_dirty concurrently, only one should sync."""
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.touch()
        tsv_path = str(tsv_path_p)

        sync_count = 0
        sync_lock = anyio.Lock()

        async def sync_callback():
            nonlocal sync_count
            async with sync_lock:
                sync_count += 1
            # Small delay to increase chance of race
            await anyio.sleep(0.01)

        managers = [DirtyMarkManager(str(tmp_path)) for _ in range(5)]
        results = []

        async def hold_with_manager(manager: DirtyMarkManager):
            async with manager.hold_dirty(tsv_path, sync_if_first=sync_callback) as result:
                results.append(result)
                await anyio.sleep(0.01)  # Hold briefly

        async with anyio.create_task_group() as tg:
            for manager in managers:
                tg.start_soon(hold_with_manager, manager)

        # Exactly one should have SYNCED
        synced_count = sum(1 for r in results if r == ClaimResult.SYNCED)
        acquired_count = sum(1 for r in results if r == ClaimResult.ACQUIRED)

        assert synced_count == 1
        assert acquired_count == 4
        assert sync_count == 1


class TestEnsureSynced:
    """Tests for ensure_synced mtime/checksum tracking."""

    async def test_ensure_synced_skips_when_dirty(self, tmp_path: Path):
        """ensure_synced should skip sync when TSV is dirty."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.write_text("key1\tid1\n")
        tsv_path = str(tsv_path_p)

        sync_count = 0

        async def sync_fn():
            nonlocal sync_count
            sync_count += 1

        # Make it dirty first
        async def hold_sync():
            pass

        async with manager.hold_dirty(tsv_path, sync_if_first=hold_sync):
            # While dirty, ensure_synced should skip
            result = await manager.ensure_synced(tsv_path, sync_fn)
            assert result is False
            assert sync_count == 0

    async def test_ensure_synced_skips_when_mtime_unchanged(self, tmp_path: Path):
        """ensure_synced should skip when mtime is unchanged."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.write_text("key1\tid1\n")
        tsv_path = str(tsv_path_p)

        sync_count = 0

        async def sync_fn():
            nonlocal sync_count
            sync_count += 1

        # First sync should run
        result1 = await manager.ensure_synced(tsv_path, sync_fn)
        assert result1 is True
        assert sync_count == 1

        # Second sync should be skipped (mtime unchanged)
        result2 = await manager.ensure_synced(tsv_path, sync_fn)
        assert result2 is False
        assert sync_count == 1

    async def test_ensure_synced_syncs_when_mtime_changed(self, tmp_path: Path):
        """ensure_synced should sync when mtime changes."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.write_text("key1\tid1\n")
        tsv_path = str(tsv_path_p)

        sync_count = 0

        async def sync_fn():
            nonlocal sync_count
            sync_count += 1

        # First sync
        result1 = await manager.ensure_synced(tsv_path, sync_fn)
        assert result1 is True
        assert sync_count == 1

        # Modify file (changes mtime)
        await anyio.sleep(0.01)  # Ensure mtime changes
        tsv_path_p.write_text("key1\tid1\nkey2\tid2\n")

        # Second sync should run (mtime changed)
        result2 = await manager.ensure_synced(tsv_path, sync_fn)
        assert result2 is True
        assert sync_count == 2

    async def test_ensure_synced_syncs_new_file(self, tmp_path: Path):
        """ensure_synced should sync files not in cache."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.write_text("key1\tid1\n")
        tsv_path = str(tsv_path_p)

        sync_count = 0

        async def sync_fn():
            nonlocal sync_count
            sync_count += 1

        # First call should sync (no cache entry)
        result = await manager.ensure_synced(tsv_path, sync_fn)
        assert result is True
        assert sync_count == 1

    async def test_ensure_synced_removes_sync_lock_when_tsv_missing(self, tmp_path: Path):
        """ensure_synced should remove .sync_lock when TSV doesn't exist after sync."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path = str(tsv_path_p)
        # Don't create the TSV file

        sync_count = 0

        async def sync_fn():
            nonlocal sync_count
            sync_count += 1

        # Call ensure_synced on non-existent file
        result = await manager.ensure_synced(tsv_path, sync_fn)
        assert result is True
        assert sync_count == 1

        # sync_lock should be removed (file doesn't exist)
        sync_lock = sync_lock_path_for_tsv(tsv_path)
        assert not os.path.exists(sync_lock)

    async def test_ensure_synced_checksum_verification(self, tmp_path: Path):
        """ensure_synced should do checksum check when mtime equals reference_mtime."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.write_text("key1\tid1\n")
        tsv_path = str(tsv_path_p)

        sync_count = 0

        async def sync_fn():
            nonlocal sync_count
            sync_count += 1

        # First sync
        result1 = await manager.ensure_synced(tsv_path, sync_fn)
        assert result1 is True
        assert sync_count == 1

        # Get the reference_mtime from cached state (sync_lock file is deleted after use)
        cached_state = manager._tsv_state[tsv_path]
        reference_mtime = cached_state.reference_mtime

        # Modify TSV content but set mtime to match reference_mtime
        tsv_path_p.write_text("key1\tid1\nkey2\tid2\n")
        os.utime(tsv_path, (reference_mtime, reference_mtime))

        # Should sync because checksum differs even though mtime matches
        result2 = await manager.ensure_synced(tsv_path, sync_fn)
        assert result2 is True
        assert sync_count == 2


class TestDiscoverTsvsNeedingSync:
    """Tests for discover_tsvs_needing_sync."""

    async def test_discover_finds_new_tsvs(self, tmp_path: Path):
        """discover_tsvs_needing_sync should find new TSV files."""
        manager = DirtyMarkManager(str(tmp_path))

        # Create some TSV files
        (tmp_path / "one.fxtr.tsv").write_text("key1\tid1\n")
        (tmp_path / "two.fxtr.tsv").write_text("key2\tid2\n")

        # Discover should find both
        tsvs = manager.discover_tsvs_needing_sync()

        assert len(tsvs) == 2
        resolved_tsvs = set(tsvs)
        assert str((tmp_path / "one.fxtr.tsv").resolve()) in resolved_tsvs
        assert str((tmp_path / "two.fxtr.tsv").resolve()) in resolved_tsvs

    async def test_discover_finds_modified_tsvs(self, tmp_path: Path):
        """discover_tsvs_needing_sync should find modified TSV files."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.write_text("key1\tid1\n")
        tsv_path = str(tsv_path_p)

        # Sync it first
        async def sync_fn():
            pass

        await manager.ensure_synced(tsv_path, sync_fn)

        # Now discover should NOT find it (already synced)
        tsvs1 = manager.discover_tsvs_needing_sync()
        assert tsv_path not in tsvs1

        # Modify the file
        await anyio.sleep(0.01)  # Ensure mtime changes
        tsv_path_p.write_text("key1\tid1\nkey2\tid2\n")

        # Now discover should find it (mtime changed)
        tsvs2 = manager.discover_tsvs_needing_sync()
        assert tsv_path in tsvs2

    async def test_discover_finds_deleted_tsvs(self, tmp_path: Path):
        """discover_tsvs_needing_sync should find deleted TSV files."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.write_text("key1\tid1\n")
        tsv_path = str(tsv_path_p)

        # Sync it first
        async def sync_fn():
            pass

        await manager.ensure_synced(tsv_path, sync_fn)

        # Delete the file
        tsv_path_p.unlink()

        # Now discover should find it (file deleted but in cache)
        tsvs = manager.discover_tsvs_needing_sync()
        assert tsv_path in tsvs

    async def test_discover_skips_dirty_tsvs(self, tmp_path: Path):
        """discover_tsvs_needing_sync should skip dirty TSVs."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.write_text("key1\tid1\n")
        tsv_path = str(tsv_path_p)

        async def sync_callback():
            pass

        # Make it dirty
        async with manager.hold_dirty(tsv_path, sync_if_first=sync_callback):
            # While dirty, discover should skip it
            tsvs = manager.discover_tsvs_needing_sync()
            # discover resolves paths, so check with resolved path
            resolved = str(tsv_path_p.resolve())
            assert resolved not in tsvs

    async def test_discover_finds_tsvs_in_subdirectories(self, tmp_path: Path):
        """discover_tsvs_needing_sync should find TSVs in subdirectories."""
        manager = DirtyMarkManager(str(tmp_path))

        # Create TSVs in subdirectories
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "models.fxtr.tsv").write_text("key1\tid1\n")
        (tmp_path / "data" / "nested").mkdir()
        (tmp_path / "data" / "nested" / "deep.fxtr.tsv").write_text("key2\tid2\n")

        tsvs = manager.discover_tsvs_needing_sync()

        assert len(tsvs) == 2
        resolved_tsvs = set(tsvs)
        assert str((tmp_path / "data" / "models.fxtr.tsv").resolve()) in resolved_tsvs
        assert str((tmp_path / "data" / "nested" / "deep.fxtr.tsv").resolve()) in resolved_tsvs


class TestFlushMtimeCache:
    """Tests for flush mtime/checksum cache updates."""

    async def test_flush_updates_cache_after_dump(self, tmp_path: Path):
        """flush should update mtime/checksum cache after dumping."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.write_text("key1\tid1\n")
        tsv_path = str(tsv_path_p)

        async def sync_callback():
            pass

        # Hold dirty then release
        async with manager.hold_dirty(tsv_path, sync_if_first=sync_callback):
            pass

        # dump_fn will write to temp path (flush handles rename)
        async def dump_fn(temp_path: str):
            with open(temp_path, "w") as f:
                f.write("key1\tid1\nkey2\tid2\n")

        result = await manager.flush(tsv_path, dump_fn)
        assert result == FlushResult.COMPLETE

        # Cache should be updated - subsequent ensure_synced should skip
        sync_count = 0

        async def sync_fn():
            nonlocal sync_count
            sync_count += 1

        result2 = await manager.ensure_synced(tsv_path, sync_fn)
        assert result2 is False  # Skipped because cache is up to date
        assert sync_count == 0

    async def test_flush_removes_sync_lock_when_tsv_deleted(self, tmp_path: Path):
        """flush should remove .sync_lock when TSV is deleted (empty dump)."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.write_text("key1\tid1\n")
        tsv_path = str(tsv_path_p)

        async def sync_callback():
            pass

        # Hold dirty then release
        async with manager.hold_dirty(tsv_path, sync_if_first=sync_callback):
            pass

        # dump_fn writes nothing (simulating empty content - flush will delete TSV)
        async def dump_fn(temp_path: str):
            # Don't write anything - this signals empty content
            pass

        result = await manager.flush(tsv_path, dump_fn)
        assert result == FlushResult.COMPLETE

        # sync_lock should be removed
        sync_lock = sync_lock_path_for_tsv(tsv_path)
        assert not os.path.exists(sync_lock)

        # Cache should also be cleared
        assert tsv_path not in manager._tsv_state


class TestFlushBackupSystem:
    """Tests for the backup system during flush (concurrent edit detection)."""

    async def test_flush_no_backup_when_checksum_matches(self, tmp_path: Path):
        """No backup created when TSV checksum matches cached value."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path_p.write_text("key1\tid1\n")
        tsv_path = str(tsv_path_p)

        # First, populate the cache by ensuring synced
        async def sync_fn():
            pass

        await manager.ensure_synced(tsv_path, sync_fn)

        # Now hold dirty (cache should have the checksum)
        async def sync_callback():
            pass

        async with manager.hold_dirty(tsv_path, sync_if_first=sync_callback):
            pass

        # Flush - checksum should match, no backup created
        async def dump_fn(temp_path: str):
            with open(temp_path, "w") as f:
                f.write("key1\tid1\nkey2\tid2\n")

        result = await manager.flush(tsv_path, dump_fn)
        assert result == FlushResult.COMPLETE

        # No backup files should exist
        backup_files = list(tmp_path.glob("*.backup-*.tsv"))
        assert len(backup_files) == 0

    async def test_flush_creates_backup_when_checksum_differs(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """Backup created and warning logged when TSV was modified externally."""
        import logging

        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        original_content = "key1\tid1\n"
        tsv_path_p.write_text(original_content)
        tsv_path = str(tsv_path_p)

        # First, populate the cache by ensuring synced
        async def sync_fn():
            pass

        await manager.ensure_synced(tsv_path, sync_fn)

        # Now hold dirty (cache should have the original checksum)
        async def sync_callback():
            pass

        async with manager.hold_dirty(tsv_path, sync_if_first=sync_callback):
            pass

        # Externally modify the TSV (simulating concurrent edit)
        modified_content = "key1\tid1\nmanual_edit\tid2\n"
        tsv_path_p.write_text(modified_content)

        # Flush - checksum should NOT match, backup should be created
        async def dump_fn(temp_path: str):
            with open(temp_path, "w") as f:
                f.write("key1\tid1\nkey3\tid3\n")

        with caplog.at_level(logging.WARNING, logger="fxtr2.data.tsv_namespace.dirty"):
            result = await manager.flush(tsv_path, dump_fn)

        assert result == FlushResult.COMPLETE

        # Backup file should exist
        backup_files = list(tmp_path.glob("*.backup-*.tsv"))
        assert len(backup_files) == 1

        # Backup should contain the modified content
        backup_content = backup_files[0].read_text()
        assert backup_content == modified_content

        # Warning should be logged
        assert any("CONCURRENT EDIT DETECTED" in record.message for record in caplog.records)

    async def test_flush_creates_backup_when_no_cached_state(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """Backup created when TSV exists but no cached state (unexpected)."""
        import logging

        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path = str(tsv_path_p)

        # Don't write to TSV yet - no initial sync

        async def sync_callback():
            pass

        # Hold dirty (creates sentinel but no TSV exists yet, so no cache)
        async with manager.hold_dirty(tsv_path, sync_if_first=sync_callback):
            pass

        # Now create TSV externally (simulating unexpected file appearance)
        external_content = "external\tid\n"
        tsv_path_p.write_text(external_content)

        # Flush - no cached state, but file exists -> backup created
        async def dump_fn(temp_path: str):
            with open(temp_path, "w") as f:
                f.write("new\tid\n")

        with caplog.at_level(logging.WARNING, logger="fxtr2.data.tsv_namespace.dirty"):
            result = await manager.flush(tsv_path, dump_fn)

        assert result == FlushResult.COMPLETE

        # Backup file should exist
        backup_files = list(tmp_path.glob("*.backup-*.tsv"))
        assert len(backup_files) == 1

        # Backup should contain the external content
        backup_content = backup_files[0].read_text()
        assert backup_content == external_content

        # Warning should be logged
        assert any("no cached state" in record.message for record in caplog.records)

    async def test_flush_no_backup_when_tsv_didnt_exist(self, tmp_path: Path):
        """No backup when TSV didn't exist before flush."""
        manager = DirtyMarkManager(str(tmp_path))
        tsv_path_p = tmp_path / "test.fxtr.tsv"
        tsv_path = str(tsv_path_p)

        # Don't create TSV

        async def sync_callback():
            pass

        # Hold dirty (no TSV exists)
        async with manager.hold_dirty(tsv_path, sync_if_first=sync_callback):
            pass

        # Flush
        async def dump_fn(temp_path: str):
            with open(temp_path, "w") as f:
                f.write("new\tid\n")

        result = await manager.flush(tsv_path, dump_fn)
        assert result == FlushResult.COMPLETE

        # No backup files should exist
        backup_files = list(tmp_path.glob("*.backup-*.tsv"))
        assert len(backup_files) == 0

        # TSV should have new content
        assert tsv_path_p.read_text() == "new\tid\n"
