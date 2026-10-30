"""LMDB blob format for entity storage.

This module provides encoding and decoding functions for the LMDB blob format used to store
entity data in the hybrid SQLite-LMDB store.

Blob Format:
-----------
Each LMDB value is a flat bytearray with a fixed-size header:

Offset  Size  Field
0       8     offset_fxtr_id  (always 24)
8       8     offset_cbor
16      8     offset_metadata
24      ...   fxtr_id_bytes   (raw bytes of the entity's FxtrID)
...     ...   cbor_bytes      (canonical CBOR of entity; empty for derived nodes)
...     ...   metadata_cbor   (CBOR-encoded metadata object)

The section boundaries are computed from consecutive offsets:
- fxtr_id runs from offset_fxtr_id to offset_cbor
- cbor runs from offset_cbor to offset_metadata
- metadata runs from offset_metadata to end of blob

Metadata Object:
---------------
The metadata section is a CBOR-encoded dict with two entries:
{
    "deps": [                    # entity's dependency list
        [internal_id, kind_str], # kind_str: "strong_ref", "weak_ref", "ctx_node", etc.
        ..
    ],
    "precomp": {                 # closed precomp map (int keys)
        internal_id: [
            fxtr_id_bytes,       # bytes: the FxtrID for this internal ID
            unwraps_to_or_null,  # int|null: internal ID of unwrap target
            ctx_node_or_null,    # int|null: internal ID of context data node
            backstep_or_null,    # int|null: internal ID of step-back-p-ctx
        ],
        ...
    }
}

The precomp map is **closed**: it contains an entry for the entity itself, every entity in its
deps list, and every entity that appears as a value in any precomp tuple.
"""

from __future__ import annotations

import struct
from typing import Any

import cbor2
import lmdb

# Fixed header size: 3 x u64 (offset_fxtr_id, offset_cbor, offset_metadata)
HEADER_SIZE = 24

# Pack/unpack format for the header: 3 little-endian unsigned 64-bit integers
_HEADER_STRUCT = struct.Struct("<QQQ")


def encode_blob(
    fxtr_id_bytes: bytes,
    cbor_bytes: bytes | None,
    metadata: dict[str, Any],
) -> bytes:
    """Encode an LMDB blob with the standard format.

    Args:
        fxtr_id_bytes: Raw bytes of the entity's FxtrID.
        cbor_bytes: Canonical CBOR bytes of the entity, or None for derived nodes.
        metadata: Metadata dict with "deps" and "precomp" keys.

    Returns:
        Encoded blob bytes.
    """
    # Serialize metadata with cbor2
    metadata_cbor = cbor2.dumps(metadata)

    # Compute offsets
    offset_fxtr_id = HEADER_SIZE
    offset_cbor = offset_fxtr_id + len(fxtr_id_bytes)
    offset_metadata = offset_cbor + (len(cbor_bytes) if cbor_bytes else 0)

    # Build header
    header = _HEADER_STRUCT.pack(offset_fxtr_id, offset_cbor, offset_metadata)

    # Concatenate sections
    if cbor_bytes:
        return header + fxtr_id_bytes + cbor_bytes + metadata_cbor
    else:
        return header + fxtr_id_bytes + metadata_cbor


def decode_blob_header(data: bytes | memoryview) -> tuple[int, int, int]:
    """Decode the header of an LMDB blob.

    Args:
        data: Blob bytes or memoryview.

    Returns:
        Tuple of (offset_fxtr_id, offset_cbor, offset_metadata).
    """
    return _HEADER_STRUCT.unpack_from(data, 0)


def decode_fxtr_id_bytes(data: bytes | memoryview) -> bytes:
    """Extract the FxtrID bytes from an LMDB blob.

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
    """Extract the CBOR bytes from an LMDB blob.

    Args:
        data: Blob bytes or memoryview.

    Returns:
        CBOR bytes, or None if the section is empty (derived node).
    """
    _offset_fxtr_id, offset_cbor, offset_metadata = decode_blob_header(data)
    if offset_cbor == offset_metadata:
        # Empty CBOR section
        return None
    if isinstance(data, memoryview):
        return bytes(data[offset_cbor:offset_metadata])
    return data[offset_cbor:offset_metadata]


def decode_metadata(data: bytes | memoryview) -> tuple[list[tuple[int, str]], dict[int, list]]:
    """Extract and decode the metadata from an LMDB blob.

    Args:
        data: Blob bytes or memoryview.

    Returns:
        Tuple of (deps_list, precomp_map) where:
        - deps_list: List of (internal_id, kind_str) tuples
        - precomp_map: Dict mapping internal_id to [fxtr_id_bytes, unwraps_to, ctx_node, backstep]
    """
    _offset_fxtr_id, _offset_cbor, offset_metadata = decode_blob_header(data)
    metadata_cbor = data[offset_metadata:]
    metadata = cbor2.loads(metadata_cbor)

    # Convert deps list from [[int, str], ...] to [(int, str), ...]
    deps_list = [tuple(dep) for dep in metadata["deps"]]

    # precomp map is already in the right format (int keys, list values)
    precomp_map = metadata["precomp"]

    return deps_list, precomp_map


def build_closed_precomp_map(
    own_internal_id: int,
    own_fxtr_id_bytes: bytes,
    own_precomp_entry: list,
    deps_with_internal_ids: list[tuple[int, str]],
    lmdb_env: lmdb.Environment,
    lmdb_data_db: Any,
) -> dict[int, list]:
    """Build a closed precomp map for an entity.

    The precomp map is "closed" meaning it contains:
    - An entry for the entity itself
    - Entries for all its direct dependencies
    - Entries for all entities referenced in any precomp tuple

    Args:
        own_internal_id: Internal ID of the entity being saved.
        own_fxtr_id_bytes: FxtrID bytes of the entity being saved.
        own_precomp_entry: Precomp entry for the entity: [fxtr_id_bytes, unwraps_to, ctx_node, backstep].
        deps_with_internal_ids: List of (dep_internal_id, kind_str) for the entity's deps.
        lmdb_env: LMDB environment.
        lmdb_data_db: LMDB sub-database handle.

    Returns:
        Closed precomp map: dict mapping internal_id to precomp entry.
    """
    # Start with the entity's own entry
    result: dict[int, list] = {own_internal_id: own_precomp_entry}

    # Read each dep's LMDB blob and merge its precomp map
    with lmdb_env.begin(db=lmdb_data_db) as txn:
        for dep_internal_id, _kind in deps_with_internal_ids:
            # Skip if already in the result (can happen if multiple deps share dependencies)
            if dep_internal_id in result:
                continue

            # Read dep's blob
            key = dep_internal_id.to_bytes(8, "little")
            blob = txn.get(key)
            if blob is None:
                raise ValueError(f"Dependency with internal_id {dep_internal_id} not found in LMDB")

            # Decode its metadata and merge the precomp map
            _dep_deps_list, dep_precomp_map = decode_metadata(blob)  # type: ignore
            for iid, entry in dep_precomp_map.items():
                if iid not in result:
                    result[iid] = entry

    return result


def build_closed_precomp_map_cached(
    own_internal_id: int,
    own_precomp_entry: list,
    deps_with_internal_ids: list[tuple[int, str]],
    precomp_cache: dict[int, dict[int, list]],
    lmdb_txn: Any,
) -> dict[int, list]:
    """Build a closed precomp map using an in-memory cache, falling back to LMDB reads.

    Like build_closed_precomp_map but:
    - Uses an existing LMDB transaction (so it can read its own uncommitted writes)
    - Checks precomp_cache before reading from LMDB
    - Populates precomp_cache with any newly-read precomp maps

    Args:
        own_internal_id: Internal ID of the entity being saved.
        own_precomp_entry: Precomp entry for the entity: [fxtr_id_bytes, unwraps_to, ctx_node, backstep].
        deps_with_internal_ids: List of (dep_internal_id, kind_str) for the entity's deps.
        precomp_cache: Mutable cache mapping internal_id -> closed precomp map. Updated in place.
        lmdb_txn: An active LMDB transaction for reading dep blobs.

    Returns:
        Closed precomp map: dict mapping internal_id to precomp entry.
    """
    result: dict[int, list] = {own_internal_id: own_precomp_entry}

    for dep_internal_id, _kind in deps_with_internal_ids:
        if dep_internal_id in result:
            continue

        # Check cache first
        if dep_internal_id in precomp_cache:
            dep_precomp_map = precomp_cache[dep_internal_id]
        else:
            # Read from LMDB (within the provided transaction)
            key = dep_internal_id.to_bytes(8, "little")
            blob = lmdb_txn.get(key)
            if blob is None:
                raise ValueError(f"Dependency with internal_id {dep_internal_id} not found in LMDB")
            _dep_deps_list, dep_precomp_map = decode_metadata(blob)
            precomp_cache[dep_internal_id] = dep_precomp_map

        for iid, entry in dep_precomp_map.items():
            if iid not in result:
                result[iid] = entry

    return result
