"""Tests for segment file I/O operations."""

import os

import pytest

from fxtr2.data.sqlite_store.entity_blob import (
    RECORD_ENVELOPE_HEADER,
    encode_blob,
    encode_record_envelope,
)
from fxtr2.data.sqlite_store.segment_io import (
    SegmentManager,
    append_record,
    flush_segment,
    truncate_to_cursor,
)


def _make_blob(fxtr_id: bytes = b"test_id", cbor: bytes | None = b"test_cbor") -> bytes:
    """Helper to create a simple blob."""
    return encode_blob(fxtr_id, cbor, {"deps": [], "precomp": {}})


class TestAppendRecord:
    """Test append_record writes correct envelope format."""

    def test_append_record_format(self, tmp_path):
        """Test that append_record writes [8-byte LE length][blob]."""
        seg_path = tmp_path / "seg_00000000.bin"
        fd = os.open(str(seg_path), os.O_CREAT | os.O_RDWR)
        try:
            blob = _make_blob()
            offset, length = append_record(fd, blob)

            assert offset == 0
            assert length == len(blob)

            # Read back raw bytes and verify format
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 8 + len(blob))
            (recorded_length,) = RECORD_ENVELOPE_HEADER.unpack_from(raw, 0)
            assert recorded_length == len(blob)
            assert raw[8:] == blob
        finally:
            os.close(fd)

    def test_append_multiple_records(self, tmp_path):
        """Test appending multiple records sequentially."""
        seg_path = tmp_path / "seg_00000000.bin"
        fd = os.open(str(seg_path), os.O_CREAT | os.O_RDWR)
        try:
            blob1 = _make_blob(b"id1")
            blob2 = _make_blob(b"id2")

            offset1, length1 = append_record(fd, blob1)
            offset2, length2 = append_record(fd, blob2)

            assert offset1 == 0
            assert offset2 == 8 + len(blob1)  # After first record's envelope
            assert length1 == len(blob1)
            assert length2 == len(blob2)

            # Total file size should be sum of both envelopes
            total = os.lseek(fd, 0, os.SEEK_END)
            assert total == (8 + len(blob1)) + (8 + len(blob2))
        finally:
            os.close(fd)

    def test_append_record_at_nonzero_offset(self, tmp_path):
        """Test appending when file position is not at start."""
        seg_path = tmp_path / "seg_00000000.bin"
        fd = os.open(str(seg_path), os.O_CREAT | os.O_RDWR)
        try:
            # Write some initial data
            os.write(fd, b"\x00" * 100)

            blob = _make_blob()
            offset, length = append_record(fd, blob)

            assert offset == 100
            assert length == len(blob)
        finally:
            os.close(fd)


class TestSegmentManager:
    """Test SegmentManager read operations and FD caching."""

    def test_read_record(self, tmp_path):
        """Test reading a record by segment_id, offset, length."""
        seg_dir = str(tmp_path / "shards")
        os.makedirs(seg_dir)
        seg_path = os.path.join(seg_dir, "seg_00000000.bin")

        blob = _make_blob()
        # Write an envelope manually
        with open(seg_path, "wb") as f:
            f.write(encode_record_envelope(blob))

        mgr = SegmentManager(seg_dir)
        try:
            result = mgr.read_record(0, 0, len(blob))
            assert result == blob
        finally:
            mgr.close()

    def test_read_record_at(self, tmp_path):
        """Test reading a record using only offset (reads envelope header first)."""
        seg_dir = str(tmp_path / "shards")
        os.makedirs(seg_dir)
        seg_path = os.path.join(seg_dir, "seg_00000000.bin")

        blob = _make_blob()
        with open(seg_path, "wb") as f:
            f.write(encode_record_envelope(blob))

        mgr = SegmentManager(seg_dir)
        try:
            result = mgr.read_record_at(0, 0)
            assert result == blob
        finally:
            mgr.close()

    def test_read_multiple_records(self, tmp_path):
        """Test reading multiple records from the same segment."""
        seg_dir = str(tmp_path / "shards")
        os.makedirs(seg_dir)
        seg_path = os.path.join(seg_dir, "seg_00000000.bin")

        blob1 = _make_blob(b"id1")
        blob2 = _make_blob(b"id2", b"different_cbor")

        with open(seg_path, "wb") as f:
            env1 = encode_record_envelope(blob1)
            f.write(env1)
            offset2 = len(env1)
            f.write(encode_record_envelope(blob2))

        mgr = SegmentManager(seg_dir)
        try:
            result1 = mgr.read_record(0, 0, len(blob1))
            assert result1 == blob1

            result2 = mgr.read_record(0, offset2, len(blob2))
            assert result2 == blob2

            # Also test read_record_at
            result1_at = mgr.read_record_at(0, 0)
            assert result1_at == blob1

            result2_at = mgr.read_record_at(0, offset2)
            assert result2_at == blob2
        finally:
            mgr.close()

    def test_read_from_multiple_segments(self, tmp_path):
        """Test reading from different segment files."""
        seg_dir = str(tmp_path / "shards")
        os.makedirs(seg_dir)

        blob0 = _make_blob(b"seg0_id")
        blob1 = _make_blob(b"seg1_id")

        with open(os.path.join(seg_dir, "seg_00000000.bin"), "wb") as f:
            f.write(encode_record_envelope(blob0))
        with open(os.path.join(seg_dir, "seg_00000001.bin"), "wb") as f:
            f.write(encode_record_envelope(blob1))

        mgr = SegmentManager(seg_dir)
        try:
            assert mgr.read_record(0, 0, len(blob0)) == blob0
            assert mgr.read_record(1, 0, len(blob1)) == blob1
        finally:
            mgr.close()

    def test_fd_cache_eviction(self, tmp_path):
        """Test that LRU eviction closes the oldest fd when cache is full."""
        seg_dir = str(tmp_path / "shards")
        os.makedirs(seg_dir)

        # Create 4 segment files
        blobs = []
        for i in range(4):
            blob = _make_blob(f"id_{i}".encode())
            blobs.append(blob)
            with open(os.path.join(seg_dir, f"seg_{i:08d}.bin"), "wb") as f:
                f.write(encode_record_envelope(blob))

        # Cache size of 2
        mgr = SegmentManager(seg_dir, max_cache_size=2)
        try:
            # Read segments 0, 1 — both should be cached
            mgr.read_record(0, 0, len(blobs[0]))
            mgr.read_record(1, 0, len(blobs[1]))
            assert len(mgr._fd_cache) == 2
            assert 0 in mgr._fd_cache
            assert 1 in mgr._fd_cache

            # Read segment 2 — should evict segment 0 (oldest)
            mgr.read_record(2, 0, len(blobs[2]))
            assert len(mgr._fd_cache) == 2
            assert 0 not in mgr._fd_cache
            assert 1 in mgr._fd_cache
            assert 2 in mgr._fd_cache

            # Read segment 1 again (touch it) then read segment 3 — should evict segment 2
            mgr.read_record(1, 0, len(blobs[1]))
            mgr.read_record(3, 0, len(blobs[3]))
            assert len(mgr._fd_cache) == 2
            assert 1 in mgr._fd_cache
            assert 3 in mgr._fd_cache
            assert 2 not in mgr._fd_cache
        finally:
            mgr.close()

    def test_close_closes_all_fds(self, tmp_path):
        """Test that close() closes all cached file descriptors."""
        seg_dir = str(tmp_path / "shards")
        os.makedirs(seg_dir)

        blob = _make_blob()
        for i in range(3):
            with open(os.path.join(seg_dir, f"seg_{i:08d}.bin"), "wb") as f:
                f.write(encode_record_envelope(blob))

        mgr = SegmentManager(seg_dir)
        mgr.read_record(0, 0, len(blob))
        mgr.read_record(1, 0, len(blob))
        mgr.read_record(2, 0, len(blob))

        # Grab the fds before close
        fds = list(mgr._fd_cache.values())
        assert len(fds) == 3

        mgr.close()

        # Cache should be empty
        assert len(mgr._fd_cache) == 0

        # The fds should be closed (attempting pread should raise)
        for fd in fds:
            with pytest.raises(OSError):
                os.pread(fd, 1, 0)

    def test_segment_path(self, tmp_path):
        """Test segment_path formatting."""
        mgr = SegmentManager(str(tmp_path))
        assert mgr.segment_path(0).endswith("seg_00000000.bin")
        assert mgr.segment_path(1).endswith("seg_00000001.bin")
        assert mgr.segment_path(42).endswith("seg_00000042.bin")
        mgr.close()


class TestFlushAndTruncate:
    """Test flush_segment and truncate_to_cursor."""

    def test_flush_segment(self, tmp_path):
        """Test that flush_segment doesn't raise."""
        seg_path = tmp_path / "seg_00000000.bin"
        fd = os.open(str(seg_path), os.O_CREAT | os.O_RDWR)
        try:
            os.write(fd, b"test data")
            flush_segment(fd)  # Should not raise
        finally:
            os.close(fd)

    def test_truncate_to_cursor(self, tmp_path):
        """Test that truncate_to_cursor truncates trailing data."""
        seg_path = tmp_path / "seg_00000000.bin"

        # Write 100 bytes
        with open(seg_path, "wb") as f:
            f.write(b"\x00" * 100)
        assert os.path.getsize(str(seg_path)) == 100

        # Truncate to 50
        truncate_to_cursor(str(seg_path), 50)
        assert os.path.getsize(str(seg_path)) == 50

    def test_truncate_to_cursor_no_op_when_exact(self, tmp_path):
        """Test that truncate is a no-op when file is exactly at cursor."""
        seg_path = tmp_path / "seg_00000000.bin"
        with open(seg_path, "wb") as f:
            f.write(b"\x00" * 50)

        truncate_to_cursor(str(seg_path), 50)
        assert os.path.getsize(str(seg_path)) == 50

    def test_truncate_to_cursor_no_op_when_smaller(self, tmp_path):
        """Test that truncate is a no-op when file is smaller than cursor."""
        seg_path = tmp_path / "seg_00000000.bin"
        with open(seg_path, "wb") as f:
            f.write(b"\x00" * 30)

        truncate_to_cursor(str(seg_path), 50)
        assert os.path.getsize(str(seg_path)) == 30  # Unchanged

    def test_crash_recovery_roundtrip(self, tmp_path):
        """Test the full crash recovery pattern: write, flush, truncate."""
        seg_dir = str(tmp_path / "shards")
        os.makedirs(seg_dir)
        seg_path = os.path.join(seg_dir, "seg_00000000.bin")

        # Simulate a write session
        fd = os.open(seg_path, os.O_CREAT | os.O_RDWR)
        try:
            blob1 = _make_blob(b"committed")
            blob2 = _make_blob(b"uncommitted")

            offset1, length1 = append_record(fd, blob1)
            committed_cursor = os.lseek(fd, 0, os.SEEK_CUR)

            # Write a second record (simulates data written but not committed to SQLite)
            append_record(fd, blob2)
            flush_segment(fd)
        finally:
            os.close(fd)

        # Simulate crash recovery: truncate to committed cursor
        truncate_to_cursor(seg_path, committed_cursor)

        # Verify: only the committed record should be readable
        mgr = SegmentManager(seg_dir)
        try:
            result = mgr.read_record(0, offset1, length1)
            assert result == blob1

            # File should be exactly at the committed cursor
            assert os.path.getsize(seg_path) == committed_cursor
        finally:
            mgr.close()
