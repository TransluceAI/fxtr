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

A CAR can come from someone else (a job published to a hub), so :func:`read_car` takes
:class:`CarLimits`. It reads and decompresses the file as it goes, and refuses it with
:class:`CarLimitError` as soon as a limit is exceeded, so a small file that decompresses to a huge
one never sits in memory whole.
"""

from __future__ import annotations

import dataclasses
import gzip
import zlib
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Protocol

import cbrrr

from fxtr.core.codec import compute_cid

_GZIP_MAGIC = b"\x1f\x8b"
_CID_LENGTH = 36
"""The length of a binary CIDv1 DAG-CBOR sha-256 cid, the only kind fxtr writes."""
_MAX_VARINT_BYTES = 9
"""A section length longer than this many varint bytes is over 2**63, which no file holds."""


class CarError(ValueError):
    """A file is not a CAR fxtr can read, or one of its blocks does not match its cid."""


class CarLimitError(CarError):
    """A file exceeds one of the :class:`CarLimits` it was read with."""


@dataclasses.dataclass(frozen=True, kw_only=True)
class CarLimits:
    """How much of a CAR file :func:`read_car` accepts. ``None`` means no limit.

    Attributes:
        max_compressed_bytes: The size of the file as stored.
        max_bytes: The size of the CAR once decompressed: the header and every block section.
    """

    max_compressed_bytes: int | None = None
    max_bytes: int | None = None


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


def read_car(
    path: Path, *, limits: CarLimits | None = None
) -> tuple[list[bytes], dict[bytes, bytes]]:
    """Read a CAR v1 file, gzipped or not, checking every block against its cid.

    Args:
        path: The file.
        limits: What the file may hold; no limits when not given.

    Returns:
        The root cids, and the blocks by cid.

    Raises:
        CarLimitError: If the file exceeds one of ``limits``.
        CarError: If the file is not a CAR v1 of fxtr's cids, or a block's data does not hash to
            its cid.
    """
    limits = limits or CarLimits()
    size = path.stat().st_size
    if limits.max_compressed_bytes is not None and size > limits.max_compressed_bytes:
        raise CarLimitError(
            f"{path} is {size} bytes, over the limit of {limits.max_compressed_bytes}"
        )
    with path.open("rb") as file:
        gzipped = file.read(len(_GZIP_MAGIC)) == _GZIP_MAGIC
        file.seek(0)
        stream: _Stream = gzip.GzipFile(fileobj=file, mode="rb") if gzipped else file
        try:
            return _read(_Reader(stream, limits.max_bytes), path)
        except (OSError, EOFError, zlib.error) as exc:  # a corrupt or truncated gzip stream
            raise CarError(f"{path} is not a readable gzip file: {exc}") from exc


def _read(reader: _Reader, path: Path) -> tuple[list[bytes], dict[bytes, bytes]]:
    header_length = reader.varint()
    if header_length is None:
        raise CarError(f"{path} is empty")
    header_bytes = reader.take(header_length)
    try:
        header = cbrrr.decode_dag_cbor(header_bytes)
    except ValueError as exc:  # cbrrr's decode error: the bytes are not DAG-CBOR
        raise CarError(f"{path}'s header is not DAG-CBOR: {exc}") from exc
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
    while (length := reader.varint()) is not None:
        if length < _CID_LENGTH:
            raise CarError("a block's cid is not a CIDv1 DAG-CBOR sha-256 cid")
        section = reader.take(length)
        if not cbrrr.CID(section[:_CID_LENGTH]).is_cidv1_dag_cbor_sha256_32():
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


class _Stream(Protocol):
    def read(self, size: int, /) -> bytes: ...


class _Reader:
    """Reads a decompressed CAR from a stream, counting what it has read against ``max_bytes``
    before reading it."""

    def __init__(self, stream: _Stream, max_bytes: int | None) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self._read = 0

    def varint(self) -> int | None:
        """The next varint, or ``None`` at the end of the file."""
        value, shift = 0, 0
        for index in range(_MAX_VARINT_BYTES):
            byte = self._stream.read(1)
            if not byte:
                if index == 0:
                    return None
                raise CarError("the file ends inside a length")
            self._count(1)
            value |= (byte[0] & 0x7F) << shift
            if not byte[0] & 0x80:
                return value
            shift += 7
        raise CarError("a length is longer than any file")

    def take(self, length: int) -> bytes:
        self._count(length)
        taken = self._stream.read(length)
        if len(taken) < length:
            raise CarError("the file ends inside a section")
        return taken

    def _count(self, length: int) -> None:
        self._read += length
        if self._max_bytes is not None and self._read > self._max_bytes:
            raise CarLimitError(f"the file decompresses to over {self._max_bytes} bytes")


__all__ = ["CarError", "CarLimitError", "CarLimits", "read_car", "write_car"]
