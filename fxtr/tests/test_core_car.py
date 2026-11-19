"""Tests for CAR files: the round trip, gzipped or not, and every way a file is refused."""

from __future__ import annotations

import gzip
import time
import tracemalloc
from pathlib import Path

import cbrrr
import pytest

from fxtr.core import codec
from fxtr.core.car import CarError, CarLimitError, CarLimits, read_car, write_car


def block(value: object) -> tuple[bytes, bytes]:
    data = codec.encode(value)  # type: ignore[arg-type]
    return codec.compute_cid(data), data


def test_blocks_round_trip_gzipped_or_not(tmp_path: Path) -> None:
    root, other = block({"$type": "test.Root", "n": 1}), block({"$type": "test.Leaf", "s": "x"})
    path = tmp_path / "a.car"
    write_car(path, [root[0]], [root, other])
    assert path.read_bytes()[:2] == b"\x1f\x8b"  # written gzipped
    assert read_car(path) == ([root[0]], {root[0]: root[1], other[0]: other[1]})
    plain = tmp_path / "plain.car"
    plain.write_bytes(gzip.decompress(path.read_bytes()))
    assert read_car(plain) == read_car(path)


def test_equal_blocks_make_equal_files(tmp_path: Path) -> None:
    root = block({"$type": "test.Root", "n": 1})
    write_car(tmp_path / "a.car", [root[0]], [root])
    time.sleep(1.1)  # a gzip header with a time would now differ
    write_car(tmp_path / "other-name.car", [root[0]], [root])
    assert (tmp_path / "a.car").read_bytes() == (tmp_path / "other-name.car").read_bytes()


def test_a_block_that_does_not_match_its_cid_is_refused(tmp_path: Path) -> None:
    cid, _ = block({"$type": "test.Root", "n": 1})
    _, wrong = block({"$type": "test.Root", "n": 2})
    path = tmp_path / "bad.car"
    write_car(path, [cid], [(cid, wrong)])
    with pytest.raises(CarError, match="does not match its cid"):
        read_car(path)


def test_a_truncated_file_is_refused(tmp_path: Path) -> None:
    root = block({"$type": "test.Root", "n": 1})
    path = tmp_path / "a.car"
    write_car(path, [root[0]], [root])
    path.write_bytes(gzip.decompress(path.read_bytes())[:-3])
    with pytest.raises(CarError, match="ends inside"):
        read_car(path)


def test_a_file_that_is_not_a_car_v1_is_refused(tmp_path: Path) -> None:
    header = cbrrr.encode_dag_cbor({"version": 2, "roots": []})
    path = tmp_path / "v2.car"
    path.write_bytes(bytes([len(header)]) + header)
    with pytest.raises(CarError, match="not a CAR v1"):
        read_car(path)


def test_a_header_that_is_not_dag_cbor_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "garbage.car"
    path.write_bytes(b"\x05\xff\xff\xff\xff\xff")  # a length of 5, then bytes no decoder reads
    with pytest.raises(CarError, match="header is not DAG-CBOR"):
        read_car(path)


def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "empty.car"
    path.write_bytes(b"")
    with pytest.raises(CarError, match="empty"):
        read_car(path)


def test_a_corrupt_or_truncated_gzip_stream_is_refused(tmp_path: Path) -> None:
    root = block({"$type": "test.Root", "n": 1})
    path = tmp_path / "a.car"
    write_car(path, [root[0]], [root])
    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) // 2])
    with pytest.raises(CarError):
        read_car(path)
    path.write_bytes(raw[:10] + bytes(len(raw) - 10))
    with pytest.raises(CarError):
        read_car(path)


def zeros_car(tmp_path: Path, size: int) -> Path:
    """A gzipped CAR with one block of ``size`` bytes of zeros, which compresses to almost
    nothing: the shape of a file that is small on disk and huge once read."""
    data = codec.encode({"$type": "test.Zeros", "z": bytes(size)})
    cid = codec.compute_cid(data)
    path = tmp_path / "zeros.car"
    write_car(path, [cid], [(cid, data)])
    return path


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        (CarLimits(max_compressed_bytes=100), "over the limit of 100"),
        (CarLimits(max_bytes=1_000_000), "decompresses to over 1000000 bytes"),
    ],
)
def test_each_limit_refuses_a_file_that_exceeds_it(
    tmp_path: Path, limits: CarLimits, message: str
) -> None:
    path = zeros_car(tmp_path, 10_000_000)
    assert path.stat().st_size < 100_000
    with pytest.raises(CarLimitError, match=message):
        read_car(path, limits=limits)


def test_a_file_within_its_limits_is_read(tmp_path: Path) -> None:
    path = zeros_car(tmp_path, 1000)
    roots, blocks = read_car(path, limits=CarLimits(max_compressed_bytes=10_000, max_bytes=10_000))
    assert list(blocks) == roots


def test_the_size_limit_stops_reading_before_the_whole_block_is_held(tmp_path: Path) -> None:
    """The limit is checked before a section is read, so a 50 MB block is refused without being
    decompressed into memory."""
    path = zeros_car(tmp_path, 50_000_000)
    tracemalloc.start()
    try:
        with pytest.raises(CarLimitError):
            read_car(path, limits=CarLimits(max_bytes=1_000_000))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 10_000_000
