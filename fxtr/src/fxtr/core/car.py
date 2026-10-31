"""CAR v1 files: content-addressed archives of entity blocks.

A CAR (content-addressed archive) is the standard container for IPLD blocks: a header naming its
root cids, then the blocks, each a cid with the bytes it names. fxtr's entity ids are binary
CIDv1 (DAG-CBOR, sha-256) and its encodings DRISL, a subset of DAG-CBOR, so a set of stored
entities is a CAR's blocks as they are, and other IPLD tools can read one.

The layout (https://ipld.io/specs/transport/car/carv1/): an unsigned LEB128 varint giving the
header's length, the header (DAG-CBOR ``{"version": 1, "roots": [cid, ...]}``), then for each
block a varint giving the length of the cid and data together, the cid's bytes, and the data.
:func:`write_car` gzips the whole file; :func:`read_car` accepts a gzipped or a plain one, told
apart by gzip's magic bytes, and checks every block's bytes against its cid.
"""

from __future__ import annotations

import gzip
from collections.abc import Iterable, Iterator
from pathlib import Path

import cbrrr

from fxtr.core.codec import compute_cid

_GZIP_MAGIC = b"\x1f\x8b"
_CID_LENGTH = 36
"""The length of a binary CIDv1 DAG-CBOR sha-256 cid, the only kind fxtr writes."""


class CarError(ValueError):
    """A file is not a CAR fxtr can read, or one of its blocks does not match its cid."""


def write_car(path: Path, roots: Iterable[bytes], blocks: Iterable[tuple[bytes, bytes]]) -> None:
    """Write a gzipped CAR v1 file. The file's bytes depend only on the roots and the blocks: the
    gzip header has no time and no file name.

    Args:
        path: Where to write it.
        roots: The root cids, as binary cids.
        blocks: ``(cid, data)`` pairs, written in the given order; each cid must name its data
            (not checked here; :func:`read_car` checks).
    """
    header = cbrrr.encode_dag_cbor({"version": 1, "roots": [cbrrr.CID(cid) for cid in roots]})
    # A fixed time and no file name in the gzip header: equal blocks make equal files.
    with (
        path.open("wb") as file,
        gzip.GzipFile(filename="", fileobj=file, mode="wb", mtime=0) as out,
    ):
        out.write(_varint(len(header)))
        out.write(header)
        for cid, data in blocks:
            out.write(_varint(len(cid) + len(data)))
            out.write(cid)
            out.write(data)


def read_car(path: Path) -> tuple[list[bytes], dict[bytes, bytes]]:
    """Read a CAR v1 file, gzipped or not, checking every block against its cid.

    Returns:
        The root cids, and the blocks by cid.

    Raises:
        CarError: If the file is not a CAR v1 of fxtr's cids, or a block's data does not hash to
            its cid.
    """
    raw = path.read_bytes()
    content = gzip.decompress(raw) if raw.startswith(_GZIP_MAGIC) else raw
    reader = _Reader(content)
    header = cbrrr.decode_dag_cbor(reader.take(reader.varint()))
    if not isinstance(header, dict) or header.get("version") != 1:
        raise CarError(f"{path} is not a CAR v1 file")
    roots = header.get("roots")
    if not isinstance(roots, list):
        raise CarError(f"{path} has no list of root cids")
    root_cids: list[bytes] = []
    for root in roots:
        if not isinstance(root, cbrrr.CID):
            raise CarError(f"{path} has a root that is not a cid")
        root_cids.append(bytes(root))
    blocks: dict[bytes, bytes] = {}
    for cid, data in _blocks(reader):
        if compute_cid(data) != cid:
            raise CarError(f"a block of {path} does not match its cid")
        blocks[cid] = data
    return root_cids, blocks


def _blocks(reader: _Reader) -> Iterator[tuple[bytes, bytes]]:
    while not reader.done:
        section = reader.take(reader.varint())
        if (
            len(section) < _CID_LENGTH
            or not cbrrr.CID(section[:_CID_LENGTH]).is_cidv1_dag_cbor_sha256_32()
        ):
            raise CarError("a block's cid is not a CIDv1 DAG-CBOR sha-256 cid")
        yield section[:_CID_LENGTH], section[_CID_LENGTH:]


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


class _Reader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._at = 0

    @property
    def done(self) -> bool:
        return self._at >= len(self._data)

    def varint(self) -> int:
        value, shift = 0, 0
        while True:
            if self.done:
                raise CarError("the file ends inside a length")
            byte = self._data[self._at]
            self._at += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value
            shift += 7

    def take(self, length: int) -> bytes:
        if self._at + length > len(self._data):
            raise CarError("the file ends inside a section")
        taken = self._data[self._at : self._at + length]
        self._at += length
        return taken


__all__ = ["CarError", "read_car", "write_car"]
