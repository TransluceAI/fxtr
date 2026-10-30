"""Utility functions for encoding and decoding DAG-CBOR with fallback to cbor2."""

from __future__ import annotations

import cbor2
import cbrrr


def _default_encoder(encoder: cbor2.CBOREncoder, value: object) -> None:
    """Custom encoder for cbrrr.CID objects.

    Encodes CIDs as tag 42 with a byte string starting with 0x00 followed by the CID bytes,
    consistent with the cbrrr implementation.
    """
    if isinstance(value, cbrrr.CID):
        cid_bytes = bytes(value)
        # CID format: tag 42 with byte string starting with 0x00 followed by CID bytes
        encoder.encode(cbor2.CBORTag(42, b"\x00" + cid_bytes))
    else:
        raise TypeError(f"Cannot encode type {type(value)}")


def _tag_hook(
    decoder: cbor2.CBORDecoder, tag: cbor2.CBORTag, shareable_index: int | None = None
) -> object:
    """Custom decoder for tag 42 (CID).

    Decodes tag 42 as a CID object, expecting a byte string starting with 0x00.
    """
    if tag.tag == 42:
        if not isinstance(tag.value, bytes):
            raise ValueError(f"CID tag 42 must contain bytes, got {type(tag.value)}")
        if len(tag.value) == 0 or tag.value[0] != 0:
            raise ValueError("CID byte string must start with 0x00")
        # Extract CID bytes (skip the leading 0x00)
        cid_bytes = tag.value[1:]
        return cbrrr.CID(cid_bytes)
    return tag


def encode_cbor(cbor: cbrrr.DagCborTypes) -> bytes:
    """Encode a DAG-CBOR value to bytes.

    Tries cbrrr.encode_dag_cbor first, falls back to cbor2.dumps with canonical=True
    if a ValueError is raised. Handles cbrrr.CID objects via custom encoder.

    Args:
        cbor: The DAG-CBOR value to encode

    Returns:
        The encoded bytes
    """
    try:
        return cbrrr.encode_dag_cbor(cbor)
    except ValueError:
        return cbor2.dumps(cbor, canonical=True, default=_default_encoder)


def decode_cbor(data: bytes) -> cbrrr.DagCborTypes:
    """Decode bytes to a DAG-CBOR value.

    Tries cbrrr.decode_dag_cbor first, falls back to cbor2.loads with canonical=True
    if a ValueError is raised. Handles tag 42 (CID) via custom tag hook.

    Args:
        data: The bytes to decode

    Returns:
        The decoded DAG-CBOR value
    """
    try:
        return cbrrr.decode_dag_cbor(data)
    except ValueError:
        return cbor2.loads(data, tag_hook=_tag_hook)
