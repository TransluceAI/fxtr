"""Entity blob format for segment-file storage.

This module provides encoding and decoding functions for the blob format used to store
entity data in append-only segment files. The blob format is identical to the LMDB blob
format (from lmdb_blob.py) — the same 24-byte header with fxtr_id, cbor, and metadata
sections. The only change is that blobs are wrapped in a record envelope when stored in
segment files, and precomp entries gain an optional 5th element for same-segment offsets.

Record Envelope:
    [8-byte LE record_length][blob_bytes ...]

Blob Format (same as lmdb_blob.py):
    Offset  Size  Field
    0       8     offset_fxtr_id  (always 24)
    8       8     offset_cbor
    16      8     offset_metadata
    24      ...   fxtr_id_bytes
    ...     ...   cbor_bytes (empty for derived nodes)
    ...     ...   metadata_cbor

Extended Precomp Entry:
    [fxtr_id_bytes, unwraps_to_or_null, ctx_node_or_null, backstep_or_null, seg_offset_or_null]
    The 5th element is optional (backwards-compatible with 4-element entries from LMDB).
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from typing import Any

import cbor2

# Fixed header size: 3 x u64 (offset_fxtr_id, offset_cbor, offset_metadata)
HEADER_SIZE = 24

# Pack/unpack format for the header: 3 little-endian unsigned 64-bit integers
_HEADER_STRUCT = struct.Struct("<QQQ")

# Record envelope header: 1 x u64 (record_length)
RECORD_ENVELOPE_HEADER = struct.Struct("<Q")


def encode_blob(
    fxtr_id_bytes: bytes,
    cbor_bytes: bytes | None,
    metadata: dict[str, Any],
) -> bytes:
    """Encode an entity blob.

    Args:
        fxtr_id_bytes: Raw bytes of the entity's FxtrID.
        cbor_bytes: Canonical CBOR bytes of the entity, or None for derived nodes.
        metadata: Metadata dict with "deps" and "precomp" keys.

    Returns:
        Encoded blob bytes.
    """
    metadata_cbor = cbor2.dumps(metadata)

    offset_fxtr_id = HEADER_SIZE
    offset_cbor = offset_fxtr_id + len(fxtr_id_bytes)
    offset_metadata = offset_cbor + (len(cbor_bytes) if cbor_bytes else 0)

    header = _HEADER_STRUCT.pack(offset_fxtr_id, offset_cbor, offset_metadata)

    if cbor_bytes:
        return header + fxtr_id_bytes + cbor_bytes + metadata_cbor
    else:
        return header + fxtr_id_bytes + metadata_cbor


def encode_record_envelope(blob: bytes) -> bytes:
    """Wrap a blob in a record envelope for segment file storage.

    Args:
        blob: The blob bytes to wrap.

    Returns:
        Record envelope bytes: [8-byte LE length][blob].
    """
    return RECORD_ENVELOPE_HEADER.pack(len(blob)) + blob


def decode_blob_header(data: bytes | memoryview) -> tuple[int, int, int]:
    """Decode the header of a blob.

    Args:
        data: Blob bytes or memoryview.

    Returns:
        Tuple of (offset_fxtr_id, offset_cbor, offset_metadata).
    """
    return _HEADER_STRUCT.unpack_from(data, 0)


def decode_fxtr_id_bytes(data: bytes | memoryview) -> bytes:
    """Extract the FxtrID bytes from a blob.

    Args:
        data: Blob bytes or memoryview.

    Returns:
        FxtrID bytes.
    """
    offset_fxtr_id, offset_cbor, _offset_metadata = decode_blob_header(data)
    if isinstance(data, memoryview):
        return bytes(data[offset_fxtr_id:offset_cbor])
    return data[offset_fxtr_id:offset_cbor]


def decode_cbor_bytes(data: bytes | memoryview) -> bytes | None:
    """Extract the CBOR bytes from a blob.

    Args:
        data: Blob bytes or memoryview.

    Returns:
        CBOR bytes, or None if the section is empty (derived node).
    """
    _offset_fxtr_id, offset_cbor, offset_metadata = decode_blob_header(data)
    if offset_cbor == offset_metadata:
        return None
    if isinstance(data, memoryview):
        return bytes(data[offset_cbor:offset_metadata])
    return data[offset_cbor:offset_metadata]


def decode_metadata(data: bytes | memoryview) -> tuple[list[tuple[int, str]], dict[int, list]]:
    """Extract and decode the metadata from a blob.

    Args:
        data: Blob bytes or memoryview.

    Returns:
        Tuple of (deps_list, precomp_map) where:
        - deps_list: List of (internal_id, kind_str) tuples
        - precomp_map: Dict mapping internal_id to 5-element precomp entry list
          [fxtr_id_bytes, unwraps_to, ctx_node, backstep, seg_offset_or_none]
    """
    _offset_fxtr_id, _offset_cbor, offset_metadata = decode_blob_header(data)
    metadata_cbor = data[offset_metadata:]
    metadata = cbor2.loads(metadata_cbor)

    deps_list = [tuple(dep) for dep in metadata["deps"]]
    precomp_map = metadata["precomp"]

    return deps_list, precomp_map


def build_closed_precomp_map_for_segments(
    own_internal_id: int,
    own_precomp_entry: list,
    deps_with_internal_ids: list[tuple[int, str]],
    precomp_cache: dict[int, dict[int, list]],
    read_blob: Callable[[int], tuple[bytes, bool]],
) -> dict[int, list]:
    """Build a closed precomp map using an in-memory cache, falling back to blob reads.

    Replaces build_closed_precomp_map_cached from lmdb_blob.py. Instead of an LMDB
    transaction, takes a generic read_blob callback that reads a blob by internal_id.

    Handles cross-segment offset clearing: when read_blob reports a dep is in a different
    segment, all seg_offsets in its precomp map are cleared to None before caching (they'd
    refer to the wrong segment). This relies on an inductive invariant: every persisted
    blob's non-None seg_offsets refer to the entity's own segment, so clearing at the
    source dep level is sufficient.

    Args:
        own_internal_id: Internal ID of the entity being saved.
        own_precomp_entry: Precomp entry for the entity (4 or 5 elements).
        deps_with_internal_ids: List of (dep_internal_id, kind_str) for the entity's deps.
        precomp_cache: Mutable cache mapping internal_id -> closed precomp map. Updated in place.
        read_blob: Callback that returns (blob_bytes, same_segment) for a given internal_id.
            same_segment is True if the dep is in the same segment as the entity being written.

    Returns:
        Closed precomp map: dict mapping internal_id to precomp entry.
    """
    result: dict[int, list] = {own_internal_id: own_precomp_entry}

    for dep_internal_id, _kind in deps_with_internal_ids:
        if dep_internal_id in result:
            continue

        # Check cache first (same-batch deps are always cached with correct offsets)
        if dep_internal_id in precomp_cache:
            dep_precomp_map = precomp_cache[dep_internal_id]
        else:
            # Read blob via callback; clear stale offsets for cross-segment deps
            blob, same_segment = read_blob(dep_internal_id)
            _dep_deps_list, dep_precomp_map = decode_metadata(blob)
            if not same_segment:
                for entry in dep_precomp_map.values():
                    if len(entry) >= 5:
                        entry[4] = None
            precomp_cache[dep_internal_id] = dep_precomp_map

        for iid, entry in dep_precomp_map.items():
            if iid not in result:
                result[iid] = entry

    return result
