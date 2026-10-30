"""Tests for file locking utilities."""

from pathlib import Path

import anyio
import anyio.to_process
import pytest

from fxtr2.data.file_util.lock import (
    LockFileNotFoundError,
    LockNotAcquiredError,
    async_file_lock_acquire,
    sync_file_lock_acquire_nonblocking,
)

pytestmark = pytest.mark.anyio


# Helper functions for multi-process tests (must be module-level for pickling)
def _holder_process(lock_path: str, marker_path: str, hold_time: float) -> None:
    """Process that acquires lock, writes marker, holds, then releases."""
    import asyncio
    from pathlib import Path

    from fxtr2.data.file_util.lock import async_file_lock_acquire

    async def main():
        async with async_file_lock_acquire(Path(lock_path)):
            Path(marker_path).write_text("holding")
            await asyncio.sleep(hold_time)
            # Write "released" BEFORE releasing lock to avoid race condition
            Path(marker_path).write_text("released")

    asyncio.run(main())


def _waiter_process(lock_path: str, marker_path: str) -> tuple[float, str]:
    """Process that waits for marker, then acquires lock and returns timing."""
    import asyncio
    import time
    from pathlib import Path

    from fxtr2.data.file_util.lock import async_file_lock_acquire

    async def main() -> tuple[float, str]:
        # Wait for holder to be holding
        marker = Path(marker_path)
        while not marker.exists() or marker.read_text() != "holding":
            await asyncio.sleep(0.01)

        start = time.time()
        async with async_file_lock_acquire(Path(lock_path)):
            elapsed = time.time() - start
            marker_state = marker.read_text()
        return elapsed, marker_state

    return asyncio.run(main())


def _worker_process(lock_path: str, log_path: str, hold_time: float) -> None:
    """Process that acquires lock, logs start/end with PID."""
    import asyncio
    import os
    from pathlib import Path

    from fxtr2.data.file_util.lock import async_file_lock_acquire

    async def main():
        pid = os.getpid()
        async with async_file_lock_acquire(Path(lock_path)):
            with open(log_path, "a") as f:
                f.write(f"{pid}_start\n")
            await asyncio.sleep(hold_time)
            with open(log_path, "a") as f:
                f.write(f"{pid}_end\n")

    asyncio.run(main())


class TestAsyncLockBasic:
    """Basic tests for async file locking."""

    async def test_acquire_creates_lock_file(self, tmp_path: Path):
        """Acquiring a lock should create the lock file."""
        lock_path = tmp_path / "test.lock"
        assert not lock_path.exists()

        async with async_file_lock_acquire(lock_path):
            assert lock_path.exists()

        # File should still exist after release (we don't delete lock files)
        assert lock_path.exists()

    async def test_acquire_creates_parent_directories(self, tmp_path: Path):
        """Acquiring a lock should create parent directories if needed."""
        lock_path = tmp_path / "nested" / "deeply" / "test.lock"
        assert not lock_path.parent.exists()

        async with async_file_lock_acquire(lock_path):
            assert lock_path.exists()

    async def test_reacquire_after_release(self, tmp_path: Path):
        """Should be able to reacquire a lock after releasing it."""
        lock_path = tmp_path / "test.lock"

        async with async_file_lock_acquire(lock_path):
            pass

        # Should be able to acquire again
        async with async_file_lock_acquire(lock_path):
            pass


class TestAsyncLockInodeVerification:
    """Tests for inode verification in async file locking."""

    async def test_reacquire_after_directory_recreated(self, tmp_path: Path):
        """Should handle case where directory is deleted and recreated."""
        nested_path = tmp_path / "nested"
        lock_path = nested_path / "test.lock"

        # First acquire creates the directory
        async with async_file_lock_acquire(lock_path):
            pass

        assert nested_path.exists()

        # Delete the directory
        lock_path.unlink()
        nested_path.rmdir()
        assert not nested_path.exists()

        # Second acquire should recreate everything
        async with async_file_lock_acquire(lock_path):
            assert lock_path.exists()


class TestAsyncLockPythonLevelLocking:
    """Tests for Python-level async locking (same event loop coordination)."""

    async def test_same_path_serializes_within_process(self, tmp_path: Path):
        """Multiple acquires of the same path should serialize within a process."""
        lock_path = tmp_path / "test.lock"
        order: list[str] = []
        first_acquired = anyio.Event()

        async def first():
            async with async_file_lock_acquire(lock_path):
                order.append("first_start")
                first_acquired.set()
                await anyio.sleep(0.05)
                order.append("first_end")

        async def second():
            await first_acquired.wait()
            # Small delay to ensure we're waiting on the lock
            await anyio.sleep(0.01)
            async with async_file_lock_acquire(lock_path):
                order.append("second")

        async with anyio.create_task_group() as tg:
            tg.start_soon(first)
            tg.start_soon(second)

        # Second should have waited for first to complete
        assert order == ["first_start", "first_end", "second"]

    async def test_different_paths_independent(self, tmp_path: Path):
        """Different paths should have independent locks."""
        path1 = tmp_path / "test1.lock"
        path2 = tmp_path / "test2.lock"

        both_acquired = anyio.Event()
        path1_acquired = anyio.Event()
        path2_acquired = anyio.Event()

        async def holder1():
            async with async_file_lock_acquire(path1):
                path1_acquired.set()
                await path2_acquired.wait()
                both_acquired.set()

        async def holder2():
            async with async_file_lock_acquire(path2):
                path2_acquired.set()
                await path1_acquired.wait()
                # Both should be acquired now

        async with anyio.create_task_group() as tg:
            tg.start_soon(holder1)
            tg.start_soon(holder2)

        assert both_acquired.is_set()


class TestAsyncLockErrorHandling:
    """Tests for error handling in async file locking."""

    async def test_lock_released_on_exception(self, tmp_path: Path):
        """Lock should be released even if exception is raised inside."""
        lock_path = tmp_path / "test.lock"

        with pytest.raises(ValueError, match="test error"):
            async with async_file_lock_acquire(lock_path):
                raise ValueError("test error")

        # Lock should be released - another acquire should succeed immediately
        acquired = False
        with anyio.move_on_after(0.5) as cancel_scope:
            async with async_file_lock_acquire(lock_path):
                acquired = True

        assert not cancel_scope.cancelled_caught
        assert acquired


class TestAsyncLockMultiProcess:
    """Tests for cross-process locking using actual separate processes."""

    async def test_lock_blocks_other_process(self, tmp_path: Path):
        """A lock held by one process should block another process."""
        lock_path = tmp_path / "test.lock"
        marker_file = tmp_path / "marker.txt"

        elapsed = None
        marker_state = None
        async with anyio.create_task_group() as tg:
            # Start holder in separate process
            tg.start_soon(
                anyio.to_process.run_sync,
                _holder_process,
                str(lock_path),
                str(marker_file),
                0.05,  # hold for 0.5s
            )

            # Give holder time to acquire
            await anyio.sleep(0.01)

            # Start waiter in separate process
            elapsed, marker_state = await anyio.to_process.run_sync(
                _waiter_process,
                str(lock_path),
                str(marker_file),
            )

        # Waiter should have waited and seen "released" state
        assert marker_state == "released", "Lock acquired before holder released!"
        assert (
            elapsed is not None and elapsed >= 0.03
        ), f"Waiter should have waited ~0.04s, but waited {elapsed:.2f}s"

    async def test_multiple_processes_serialize(self, tmp_path: Path):
        """Multiple processes trying to acquire should serialize."""
        lock_path = tmp_path / "test.lock"
        log_file = tmp_path / "log.txt"

        # Create empty log file
        log_file.write_text("")

        # Start 3 workers concurrently in separate processes
        async with anyio.create_task_group() as tg:
            for _ in range(3):
                tg.start_soon(
                    anyio.to_process.run_sync,
                    _worker_process,
                    str(lock_path),
                    str(log_file),
                    0.01,  # hold for 0.1s
                )
                await anyio.sleep(0.002)  # Slight stagger

        # Check log - each process should complete before next starts
        log_content = log_file.read_text().strip().split("\n")
        assert len(log_content) == 6, f"Expected 6 log entries, got: {log_content}"

        # Verify no interleaving - each _start should be followed by same pid's _end
        i = 0
        while i < len(log_content):
            start_entry = log_content[i]
            end_entry = log_content[i + 1]
            assert start_entry.endswith("_start"), f"Expected _start, got: {start_entry}"
            assert end_entry.endswith("_end"), f"Expected _end, got: {end_entry}"
            start_pid = start_entry.replace("_start", "")
            end_pid = end_entry.replace("_end", "")
            assert start_pid == end_pid, f"Interleaving detected: {log_content}"
            i += 2


class TestAsyncLockCreateIfNotExists:
    """Tests for create_if_not_exists parameter."""

    async def test_create_if_not_exists_false_raises_on_missing_file(self, tmp_path: Path):
        """Should raise LockFileNotFoundError when file doesn't exist."""
        lock_path = tmp_path / "nonexistent.lock"

        with pytest.raises(LockFileNotFoundError) as exc_info:
            async with async_file_lock_acquire(lock_path, create_if_not_exists=False):
                pass

        assert exc_info.value.path == lock_path

    async def test_create_if_not_exists_false_works_with_existing_file(self, tmp_path: Path):
        """Should succeed when file exists and create_if_not_exists=False."""
        lock_path = tmp_path / "existing.lock"
        lock_path.touch()

        async with async_file_lock_acquire(lock_path, create_if_not_exists=False):
            assert lock_path.exists()

    async def test_create_if_not_exists_true_creates_file(self, tmp_path: Path):
        """Default behavior should create file if it doesn't exist."""
        lock_path = tmp_path / "new.lock"
        assert not lock_path.exists()

        async with async_file_lock_acquire(lock_path, create_if_not_exists=True):
            assert lock_path.exists()

    async def test_create_if_not_exists_false_raises_on_deleted_during_wait(self, tmp_path: Path):
        """Should raise if file is deleted while waiting for lock."""
        lock_path = tmp_path / "test.lock"
        lock_path.touch()

        deleted = anyio.Event()

        async def holder():
            async with async_file_lock_acquire(lock_path):
                # Delete the file while holding the lock
                lock_path.unlink()
                deleted.set()
                await anyio.sleep(0.05)

        async def waiter():
            await deleted.wait()
            # File is deleted, but we still have the fd from holder
            # When we try to acquire with create_if_not_exists=False,
            # and the file doesn't exist, we should get an error
            with pytest.raises(LockFileNotFoundError):
                async with async_file_lock_acquire(lock_path, create_if_not_exists=False):
                    pass

        async with anyio.create_task_group() as tg:
            tg.start_soon(holder)
            tg.start_soon(waiter)

    async def test_create_if_not_exists_default_is_true(self, tmp_path: Path):
        """Default value of create_if_not_exists should be True."""
        lock_path = tmp_path / "test.lock"
        assert not lock_path.exists()

        # Default behavior - should create
        async with async_file_lock_acquire(lock_path):
            assert lock_path.exists()


class TestSyncNonblockingLock:
    """Tests for sync_file_lock_acquire_nonblocking."""

    def test_acquire_on_unlocked_file(self, tmp_path: Path):
        """Should succeed when file exists and is not locked."""
        lock_path = tmp_path / "test.lock"
        lock_path.touch()

        with sync_file_lock_acquire_nonblocking(lock_path) as fd:
            assert fd > 0
            assert lock_path.exists()

    def test_raises_on_missing_file(self, tmp_path: Path):
        """Should raise FileNotFoundError when file doesn't exist."""
        lock_path = tmp_path / "nonexistent.lock"

        with pytest.raises(FileNotFoundError):
            with sync_file_lock_acquire_nonblocking(lock_path):
                pass

    def test_raises_when_lock_held_by_async(self, tmp_path: Path):
        """Should raise LockNotAcquiredError when lock is held by async lock."""
        import asyncio

        lock_path = tmp_path / "test.lock"
        lock_path.touch()

        async def holder_and_checker():
            async with async_file_lock_acquire(lock_path):
                # While holding async lock, try nonblocking acquire
                with pytest.raises(LockNotAcquiredError) as exc_info:
                    with sync_file_lock_acquire_nonblocking(lock_path):
                        pass
                assert exc_info.value.path == lock_path

        asyncio.run(holder_and_checker())

    def test_lock_released_on_exit(self, tmp_path: Path):
        """Lock should be released when context manager exits."""
        lock_path = tmp_path / "test.lock"
        lock_path.touch()

        # Acquire and release
        with sync_file_lock_acquire_nonblocking(lock_path):
            pass

        # Should be able to acquire again (proves it was released)
        with sync_file_lock_acquire_nonblocking(lock_path):
            pass

    def test_lock_released_on_exception(self, tmp_path: Path):
        """Lock should be released even if exception is raised inside."""
        lock_path = tmp_path / "test.lock"
        lock_path.touch()

        with pytest.raises(ValueError, match="test error"):
            with sync_file_lock_acquire_nonblocking(lock_path):
                raise ValueError("test error")

        # Should be able to acquire again
        with sync_file_lock_acquire_nonblocking(lock_path):
            pass

    def test_two_nonblocking_acquires_conflict(self, tmp_path: Path):
        """Second nonblocking acquire should fail when first holds lock."""
        lock_path = tmp_path / "test.lock"
        lock_path.touch()

        with sync_file_lock_acquire_nonblocking(lock_path):
            # While holding, try another nonblocking acquire
            with pytest.raises(LockNotAcquiredError):
                with sync_file_lock_acquire_nonblocking(lock_path):
                    pass
