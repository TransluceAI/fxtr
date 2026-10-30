"""File locking utilities for fxtr2.

Provides functions for coordinating access to files across async tasks,
threads, and processes using fcntl.flock().

Key features:
- Python-level threading lock per path: Coordinates access between async
  tasks and sync code within the same process
- FS-level flock: Actual file locking via fcntl.flock() for cross-process
  coordination
- Both locks acquired/released together: The threading lock is acquired
  right before flock and released right after, ensuring both async and
  sync code paths respect each other
- Inode verification: After acquiring, verify the lock file's inode matches
  the path. If the file was moved/replaced during wait, retry
- Async (blocking) and sync (non-blocking) variants
"""

from __future__ import annotations

import fcntl
import os
import threading
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import AsyncIterator, Iterator

import anyio
import anyio.to_thread


class LockFileNotFoundError(Exception):
    """Raised when trying to acquire a lock on a non-existent file.

    This is raised when async_file_lock_acquire() is called with
    create_if_not_exists=False and the lock file doesn't exist,
    or if the file is deleted while waiting for the lock.
    """

    def __init__(self, path: Path):
        self.path = path
        super().__init__(f"Lock file not found: {path}")


class LockNotAcquiredError(Exception):
    """Raised when a non-blocking lock acquisition fails.

    This indicates another process holds the lock.
    """

    def __init__(self, path: Path):
        self.path = path
        super().__init__(f"Lock not acquired (held by another process): {path}")


# Module-level dict of Python threading locks per path
# These coordinate access between async tasks and sync code within
# the same process. The threading lock is always acquired/released
# alongside the flock to ensure both paths respect each other.
_thread_locks_mutex = threading.Lock()
_thread_locks: dict[Path, threading.Lock] = {}


def _get_thread_lock(path: Path) -> threading.Lock:
    """Get or create a Python-level threading lock for a path."""
    # Normalize the path to avoid issues with different representations
    resolved = path.resolve()
    with _thread_locks_mutex:
        if resolved not in _thread_locks:
            _thread_locks[resolved] = threading.Lock()
        return _thread_locks[resolved]


@asynccontextmanager
async def async_file_lock_acquire(
    lock_path: Path,
    create_if_not_exists: bool = True,
    delete_on_release: bool = False,
) -> AsyncIterator[int]:
    """Acquire exclusive file lock with inode verification.

    This acquires both a Python-level threading lock (to coordinate within
    the same process, including between async and sync code) and an OS-level
    file lock (to coordinate across processes). Both locks are acquired
    together in a worker thread.

    After acquiring the flock, verifies the lock file's inode matches
    the path. If the file was moved/replaced while waiting, releases
    and retries (if create_if_not_exists=True) or raises
    LockFileNotFoundError (if create_if_not_exists=False).

    Args:
        lock_path: Path to lock file
        create_if_not_exists: If True (default), create the file and parent
            directories if they don't exist. If False, raise
            LockFileNotFoundError if the file doesn't exist.
        delete_on_release: If True, delete the lock file while still holding
            the lock, right before releasing. This ensures no other process
            can acquire the lock between release and delete. Useful for
            ephemeral lock files that should be cleaned up after use.

    Yields:
        The file descriptor (fd) of the locked file.

    Raises:
        LockFileNotFoundError: If create_if_not_exists=False and the file
            doesn't exist or is deleted while waiting for the lock.

    Usage:
        # Default: create file if it doesn't exist
        async with async_file_lock_acquire(lock_path) as fd:
            # Holding lock
            ...

        # Require file to exist (for dirty mark files)
        try:
            async with async_file_lock_acquire(lock_path, create_if_not_exists=False) as fd:
                # Holding lock on existing file
                ...
        except LockFileNotFoundError:
            # File doesn't exist or was deleted

        # Delete the lock file on release (for ephemeral locks)
        async with async_file_lock_acquire(lock_path, delete_on_release=True) as fd:
            # Do work...
            # File will be deleted while lock is held, before releasing
    """
    thread_lock = _get_thread_lock(lock_path)

    fd: int | None = None
    thread_lock_held = False
    try:
        while True:
            if create_if_not_exists:
                # Ensure parent directory exists
                lock_path.parent.mkdir(parents=True, exist_ok=True)

                # Open or create the lock file
                try:
                    fd = os.open(
                        lock_path,
                        os.O_RDWR | os.O_CREAT,
                        0o644,
                    )
                except FileNotFoundError:
                    # Parent directory was deleted between mkdir and open, retry
                    continue
            else:
                # Don't create - file must exist
                try:
                    fd = os.open(
                        lock_path,
                        os.O_RDWR,
                        0o644,
                    )
                except FileNotFoundError:
                    raise LockFileNotFoundError(lock_path)

            # Acquire thread lock and flock together in worker thread
            # This ensures coordination with both async and sync code paths
            fd_to_lock = fd
            assert fd_to_lock is not None  # For type checker

            def acquire_locks() -> None:
                thread_lock.acquire()
                try:
                    fcntl.flock(fd_to_lock, fcntl.LOCK_EX)
                except BaseException:
                    thread_lock.release()
                    raise

            try:
                await anyio.to_thread.run_sync(acquire_locks)
                thread_lock_held = True
            except OSError:
                os.close(fd)
                fd = None
                raise

            # Verify inode matches (check file wasn't moved while we waited)
            try:
                path_stat = os.stat(lock_path)
                fd_stat = os.fstat(fd)
            except FileNotFoundError:
                # File was deleted while we held the lock
                fcntl.flock(fd, fcntl.LOCK_UN)
                thread_lock.release()
                thread_lock_held = False
                os.close(fd)
                fd = None
                if create_if_not_exists:
                    # Retry - will recreate the file
                    continue
                else:
                    # Don't retry - file must exist
                    raise LockFileNotFoundError(lock_path)

            if (path_stat.st_ino, path_stat.st_dev) != (
                fd_stat.st_ino,
                fd_stat.st_dev,
            ):
                # Inode mismatch - file was moved and recreated
                fcntl.flock(fd, fcntl.LOCK_UN)
                thread_lock.release()
                thread_lock_held = False
                os.close(fd)
                fd = None
                if create_if_not_exists:
                    # Retry - will get the new file
                    continue
                else:
                    # File was replaced - treat as not found
                    raise LockFileNotFoundError(lock_path)

            # Success - we hold both locks
            break

        yield fd

    finally:
        if fd is not None:
            # Delete file while still holding the lock (before releasing)
            if delete_on_release:
                try:
                    os.unlink(lock_path)
                except OSError:
                    pass  # Ignore errors (file may already be deleted)
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass  # Ignore errors during unlock
        if thread_lock_held:
            thread_lock.release()
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass  # Ignore errors during close


@contextmanager
def sync_file_lock_acquire_nonblocking(lock_path: Path) -> Iterator[int]:
    """Attempt non-blocking exclusive lock acquisition.

    Opens the file and attempts to acquire an exclusive lock without blocking.
    If the lock is held by another process or thread (including async tasks),
    raises LockNotAcquiredError immediately.

    This function coordinates with async_file_lock_acquire via a shared
    threading lock, ensuring that sync and async code paths respect each other.

    Args:
        lock_path: Path to lock file (must exist)

    Yields:
        The file descriptor (fd) of the locked file.

    Raises:
        LockNotAcquiredError: If lock is held by another process or thread.
        FileNotFoundError: If the file doesn't exist.

    Usage:
        try:
            with sync_file_lock_acquire_nonblocking(path) as fd:
                # Lock held, fd is open
                ...
        except LockNotAcquiredError:
            # Another process or thread holds the lock
            ...
    """
    thread_lock = _get_thread_lock(lock_path)

    # Try to acquire thread lock non-blocking first
    if not thread_lock.acquire(blocking=False):
        raise LockNotAcquiredError(lock_path)

    try:
        # Open file (must exist)
        fd = os.open(lock_path, os.O_RDWR, 0o644)
    except BaseException:
        thread_lock.release()
        raise

    try:
        # Attempt non-blocking exclusive flock
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Lock is held by another process
            os.close(fd)
            thread_lock.release()
            raise LockNotAcquiredError(lock_path)

        yield fd

    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass  # Ignore errors during unlock
        thread_lock.release()
        try:
            os.close(fd)
        except OSError:
            pass  # Ignore errors during close
