"""Segment file I/O for append-only entity storage.

This module provides low-level I/O operations for reading and writing entity blobs
in append-only segment files. Segment files live in a shards directory and are named
seg_{N:08d}.bin with monotonically increasing N.

Record format in segment files:
    [8-byte LE record_length][blob_bytes ...]

Key design decisions:
- Reads use os.pread() which is thread-safe (no shared seek pointer), so multiple
  reader threads can share the same cached file descriptor without locking.
- Writes use os.write() at the current file position (caller must seek to write_cursor).
- Platform-aware durable flush: fdatasync on Linux, F_FULLFSYNC on macOS.
"""

from __future__ import annotations

import os
import sys

from fxtr2.data.sqlite_store.entity_blob import RECORD_ENVELOPE_HEADER

# 8 bytes for the record envelope header
_ENVELOPE_HEADER_SIZE = RECORD_ENVELOPE_HEADER.size


class SegmentManager:
    """Manages file descriptors for reading segment files.

    Holds an LRU cache of open file descriptors for segment files. Reads use
    os.pread() which is thread-safe. Sealed segments are immutable so their
    fds are safe to cache indefinitely.
    """

    def __init__(self, segments_dir: str, max_cache_size: int = 16):
        self.segments_dir = segments_dir
        self._max_cache_size = max_cache_size
        # Ordered dict behavior via regular dict (Python 3.7+ insertion order).
        # Most recently used entries are moved to the end.
        self._fd_cache: dict[int, int] = {}

    def segment_path(self, segment_id: int) -> str:
        """Return the file path for a segment.

        Args:
            segment_id: The segment number.

        Returns:
            Path string like "{segments_dir}/seg_00000000.bin".
        """
        return os.path.join(self.segments_dir, f"seg_{segment_id:08d}.bin")

    def _get_fd(self, segment_id: int) -> int:
        """Get a cached file descriptor for a segment, opening if needed.

        Uses LRU eviction: when the cache is full, the least recently used
        fd is closed and removed.

        Args:
            segment_id: The segment number.

        Returns:
            An open file descriptor for the segment file.
        """
        if segment_id in self._fd_cache:
            # Move to end (most recently used)
            fd = self._fd_cache.pop(segment_id)
            self._fd_cache[segment_id] = fd
            return fd

        # Evict oldest if at capacity
        if len(self._fd_cache) >= self._max_cache_size:
            oldest_key = next(iter(self._fd_cache))
            oldest_fd = self._fd_cache.pop(oldest_key)
            os.close(oldest_fd)

        # Open and cache
        path = self.segment_path(segment_id)
        fd = os.open(path, os.O_RDONLY)
        self._fd_cache[segment_id] = fd
        return fd

    def read_record(self, segment_id: int, offset: int, length: int) -> bytes:
        """Read a record from a segment file.

        Reads `length` bytes starting at `offset + 8` (skipping the envelope header).

        Args:
            segment_id: The segment number.
            offset: Byte offset of the record envelope start.
            length: Byte length of the blob (excluding the 8-byte envelope header).

        Returns:
            The blob bytes.
        """
        fd = self._get_fd(segment_id)
        return os.pread(fd, length, offset + _ENVELOPE_HEADER_SIZE)

    def read_record_at(self, segment_id: int, offset: int) -> bytes:
        """Read a record from a segment file using only the offset.

        Reads the envelope header first (8 bytes) to determine the blob length,
        then reads the blob. Used for the same-segment precomp fast path where
        the caller only has an offset but not a length.

        Args:
            segment_id: The segment number.
            offset: Byte offset of the record envelope start.

        Returns:
            The blob bytes.
        """
        fd = self._get_fd(segment_id)
        # Read envelope header to get length
        header_bytes = os.pread(fd, _ENVELOPE_HEADER_SIZE, offset)
        (length,) = RECORD_ENVELOPE_HEADER.unpack(header_bytes)
        # Read the blob
        return os.pread(fd, length, offset + _ENVELOPE_HEADER_SIZE)

    def close(self) -> None:
        """Close all cached file descriptors."""
        for fd in self._fd_cache.values():
            os.close(fd)
        self._fd_cache.clear()


def append_record(fd: int, blob: bytes) -> tuple[int, int]:
    """Append a record to a segment file at the current file position.

    Writes [8-byte LE length][blob] to the file.

    Args:
        fd: An open file descriptor positioned at the write cursor.
        blob: The blob bytes to write.

    Returns:
        Tuple of (offset_of_envelope_start, len(blob)).
        The offset is determined from the current file position before writing.
    """
    offset = os.lseek(fd, 0, os.SEEK_CUR)
    envelope = RECORD_ENVELOPE_HEADER.pack(len(blob)) + blob
    written = 0
    mv = memoryview(envelope)
    while written < len(envelope):
        n = os.write(fd, mv[written:])
        if n == 0:
            raise OSError("os.write returned 0")
        written += n
    return offset, len(blob)


def flush_segment(fd: int) -> None:
    """Durably flush a segment file to disk.

    Uses platform-aware flush:
    - Linux: os.fdatasync(fd) — flushes data to disk (not just OS cache)
    - macOS: fcntl F_FULLFSYNC — ensures data reaches the physical drive
      (plain fsync on macOS only flushes to the drive's write cache)
    """
    if sys.platform == "darwin":
        import fcntl

        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    else:
        os.fdatasync(fd)


def truncate_to_cursor(path: str, write_cursor: int) -> None:
    """Truncate a segment file to the committed write cursor position.

    Used for crash recovery: if the segment file is longer than the committed
    write_cursor (from segment_metadata), trailing uncommitted data is removed.

    Args:
        path: Path to the segment file.
        write_cursor: The committed byte offset to truncate to.
    """
    current_size = os.path.getsize(path)
    if current_size > write_cursor:
        with open(path, "r+b") as f:
            f.truncate(write_cursor)
