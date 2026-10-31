"""DAG-CBOR / DRISL codec for fxtr data values.

This module is the concrete content-addressing layer: it serializes a data value to the
DAG-CBOR / DRISL wire form (https://dasl.ing/drisl.html) and computes the DASL CID of those bytes.
It is the one place that knows the wire format, so a storage engine that wants to intercept or vary
serialization has a single, well-defined seam to work against.

The heavy lifting is done by the ``cbrrr`` library, a strict DAG-CBOR implementation: floats are
always 64-bit, map keys are sorted canonically and must be strings, integers use shortest form,
only CID tag 42 is permitted, and indefinite-length items are rejected. A CID link is carried on
the wire as tag 42 over the byte string ``0x00`` + the 36-byte binary CID.

:func:`encode` accepts the full :data:`StorableFxtrDataValue` union — mutable and frozen shapes
encode to the same canonical bytes. cbrrr represents links with its own :class:`cbrrr.CID` type,
so this module converts references to ``cbrrr.CID`` before encoding and back afterwards.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence

import cbrrr

from fxtr.core.entity_data import EntityID, FxtrDataValue, StorableFxtrDataValue


def canonical_map_items[T](
    value: Mapping[str, T],
) -> list[tuple[str, T]]:
    """Return a map's items sorted in canonical DAG-CBOR / DRISL key order.

    Canonical key order is length-first, then bytewise on the UTF-8 encoding of the key — the
    order map entries appear in on the wire. Iterating in this order makes a walk of any storable
    value visit references in the same order they occur in the encoded bytes.

    Args:
        value: The map whose items to order.

    Returns:
        The ``(key, value)`` pairs in canonical order.

    Raises:
        TypeError: If any key is not a string.
    """

    def sort_key(item: tuple[str, T]) -> tuple[int, bytes]:
        key = item[0]
        if not isinstance(key, str):
            raise TypeError(f"Map keys must be strings; got {key!r}")
        encoded = key.encode()
        return (len(encoded), encoded)

    return sorted(value.items(), key=sort_key)


def iter_entity_ids(value: StorableFxtrDataValue) -> Iterator[EntityID]:
    """Yield every reference directly contained in ``value``, in canonical wire order.

    Walks sequences and the *values* of maps (keys are always strings; map entries are visited in
    canonical key order via :func:`canonical_map_items`), but does *not* follow links into other
    entities. It only finds the references contained in this one value, e.g. as the single-step
    expansion when walking a whole subgraph.

    Because the traversal is canonical, the sequence of yielded references depends only on the
    logical value (not on map insertion order), and matches the order of the CID links in
    :func:`encode`'s output.

    Args:
        value: The data value to walk (any storable shape).

    Yields:
        Each ``EntityID`` contained in ``value``, in canonical wire order (duplicates included).
    """
    match value:
        case EntityID():
            yield value
        case str() | bytes():
            return
        case Sequence():
            for item in value:
                yield from iter_entity_ids(item)
        case Mapping():
            for _key, item in canonical_map_items(value):
                yield from iter_entity_ids(item)
        case _:
            return


def encode(value: StorableFxtrDataValue) -> bytes:
    """Serialize a data value to canonical DAG-CBOR / DRISL bytes.

    Args:
        value: The value to serialize; any storable shape (mutable or frozen).

    Returns:
        The canonical DRISL encoding of ``value``.
    """
    return cbrrr.encode_dag_cbor(_to_cbrrr(value))


def decode(data: bytes) -> FxtrDataValue:
    """Deserialize canonical DAG-CBOR / DRISL bytes back into a data value.

    CID links become ``EntityID``\\ s. Decoded maps preserve the wire's canonical key order as
    dict insertion order, so walking the result (e.g. with :func:`iter_entity_ids`) visits
    references in wire order.

    Args:
        data: The DRISL-encoded bytes.

    Returns:
        The decoded :data:`FxtrDataValue`.

    Raises:
        cbrrr.CbrrrDecodeError: If ``data`` is not valid, canonical DAG-CBOR.
    """
    return _from_cbrrr(cbrrr.decode_dag_cbor(data))


def compute_cid(encoded: bytes) -> bytes:
    """Compute the DASL CID (as raw bytes) for already-encoded DRISL content.

    Args:
        encoded: The DRISL-encoded content bytes, as produced by :func:`encode`.

    Returns:
        The 36-byte binary CID (CIDv1, dag-cbor codec, SHA-256): ``01 71 12 20`` + digest.
    """
    return bytes(cbrrr.CID.cidv1_dag_cbor_sha256_32_from(encoded))


def _to_cbrrr(value: StorableFxtrDataValue) -> cbrrr.DagCborTypes:
    """Convert a data value into cbrrr's representation, replacing references with ``cbrrr.CID``.

    Container shapes (list/tuple, dict/frozendict) normalize to plain lists and dicts.
    """
    match value:
        case EntityID():
            return cbrrr.CID(value.cid)
        case None | bool() | int() | float() | str() | bytes():
            return value
        case Sequence():
            return [_to_cbrrr(item) for item in value]
        case Mapping():
            return {key: _to_cbrrr(item) for key, item in canonical_map_items(value)}
        case _:
            raise TypeError(f"Unsupported data value type: {type(value)}")


def _from_cbrrr(value: cbrrr.DagCborTypes) -> FxtrDataValue:
    """Convert cbrrr's decoded representation back into a data value.

    ``cbrrr.CID`` links become ``EntityID``\\ s.
    """
    match value:
        case cbrrr.CID():
            return EntityID(cid=value.cid_bytes)
        case list():
            return [_from_cbrrr(item) for item in value]
        case dict():
            return {key: _from_cbrrr(item) for key, item in value.items()}
        case _:
            return value
