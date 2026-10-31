"""Tests for CAR files: the round trip, gzipped or not, and every way a file is refused."""

from __future__ import annotations

import gzip
import time
from pathlib import Path

import cbrrr
import pytest

from fxtr.core import codec
from fxtr.core.car import CarError, read_car, write_car


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
