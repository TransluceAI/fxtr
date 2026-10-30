"""Tests for CBOR entity stream encoding/decoding."""

from fxtr2.data.abstract_arena import DepKind, FxtrEntityFlatData
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.remote.transfer_format import (
    decode_entity_stream,
    decode_push_entity_stream,
    encode_entity_stream,
    encode_push_entity_stream,
)


def _make_id(tag: str, n: int) -> FxtrID:
    """Create a FxtrID with the given tag and a deterministic 20-byte payload."""
    payload = n.to_bytes(20, "big")
    return FxtrID.from_tag_and_payload(tag, payload)


def test_roundtrip_empty() -> None:
    encoded = encode_entity_stream([])
    decoded = decode_entity_stream(encoded)
    assert decoded == []


def test_roundtrip_single_entity_no_deps() -> None:
    entity = FxtrEntityFlatData(
        entity_id=_make_id("d", 1),
        entity_bytes=b"\xa1\x01\x02",
        deps=[],
    )
    encoded = encode_entity_stream([entity])
    decoded = decode_entity_stream(encoded)

    assert len(decoded) == 1
    assert decoded[0].entity_id == entity.entity_id
    assert decoded[0].entity_bytes == entity.entity_bytes
    assert decoded[0].deps == []


def test_roundtrip_entity_with_deps() -> None:
    dep_id = _make_id("d", 2)
    parent_id = _make_id("n", 3)

    dep_entity = FxtrEntityFlatData(
        entity_id=dep_id,
        entity_bytes=b"\xa0",
        deps=[],
    )
    parent_entity = FxtrEntityFlatData(
        entity_id=parent_id,
        entity_bytes=b"\xa1\x03\x04",
        deps=[(dep_id, DepKind.STRONG_REF)],
    )

    encoded = encode_entity_stream([dep_entity, parent_entity])
    decoded = decode_entity_stream(encoded)

    assert len(decoded) == 2
    assert decoded[0].entity_id == dep_id
    assert decoded[1].entity_id == parent_id
    assert decoded[1].deps == [(dep_id, DepKind.STRONG_REF)]


def test_roundtrip_derived_node_none_bytes() -> None:
    """Derived nodes (c/p/u/./!) have None for entity_bytes."""
    ctx_dep = _make_id("d", 4)
    entity = FxtrEntityFlatData(
        entity_id=_make_id("d", 5),
        entity_bytes=None,
        deps=[(ctx_dep, DepKind.CTX_NODE)],
    )
    encoded = encode_entity_stream([entity])
    decoded = decode_entity_stream(encoded)

    assert len(decoded) == 1
    assert decoded[0].entity_bytes is None
    assert decoded[0].deps[0][1] == DepKind.CTX_NODE


def test_roundtrip_all_dep_kinds() -> None:
    dep_ids = [_make_id("d", 10 + i) for i in range(5)]
    all_kinds = [
        DepKind.STRONG_REF,
        DepKind.WEAK_REF,
        DepKind.CTX_NODE,
        DepKind.BACKSTEP,
        DepKind.UNWRAPS_TO,
    ]

    entity = FxtrEntityFlatData(
        entity_id=_make_id("d", 99),
        entity_bytes=b"\xa0",
        deps=list(zip(dep_ids, all_kinds)),
    )
    encoded = encode_entity_stream([entity])
    decoded = decode_entity_stream(encoded)

    assert len(decoded) == 1
    for (orig_dep, orig_kind), (dec_dep, dec_kind) in zip(entity.deps, decoded[0].deps):
        assert orig_dep == dec_dep
        assert orig_kind == dec_kind


def test_roundtrip_multiple_entities() -> None:
    entities = [
        FxtrEntityFlatData(
            entity_id=_make_id("d", 100 + i),
            entity_bytes=bytes([i]) * 10,
            deps=[],
        )
        for i in range(10)
    ]
    encoded = encode_entity_stream(entities)
    decoded = decode_entity_stream(encoded)

    assert len(decoded) == 10
    for orig, dec in zip(entities, decoded):
        assert orig.entity_id == dec.entity_id
        assert orig.entity_bytes == dec.entity_bytes


def test_push_roundtrip_single_entity_omits_deps() -> None:
    dep_id = _make_id("d", 2)
    entity = FxtrEntityFlatData(
        entity_id=_make_id("d", 1),
        entity_bytes=b"\xa1\x01\x02",
        deps=[(dep_id, DepKind.STRONG_REF)],
    )

    encoded = encode_push_entity_stream([entity])
    decoded = decode_push_entity_stream(encoded)

    assert len(decoded) == 1
    assert decoded[0].entity_id == entity.entity_id
    assert decoded[0].entity_bytes == entity.entity_bytes
    assert decoded[0].deps == []


def test_push_roundtrip_empty() -> None:
    encoded = encode_push_entity_stream([])
    decoded = decode_push_entity_stream(encoded)
    assert decoded == []
