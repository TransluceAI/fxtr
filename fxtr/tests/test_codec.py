"""Tests for the DAG-CBOR / DRISL content-addressing codec."""

from __future__ import annotations

import base64

import pytest
from frozendict import frozendict

from fxtr.core import codec
from fxtr.core.entity_data import (
    EntityID,
    FxtrDataValue,
    freeze_fxtr_data,
    thaw_fxtr_data,
)

# A CID computed over some content, reused as a link in test values.
SAMPLE_CID = codec.compute_cid(codec.encode({"leaf": 1}))
SAMPLE_ID = EntityID(cid=SAMPLE_CID)


def test_cid_binary_format() -> None:
    # CIDv1, dag-cbor codec (0x71), sha-256 (0x12), length 32 (0x20), then 32 digest bytes.
    assert len(SAMPLE_CID) == 36
    assert SAMPLE_CID[:4] == bytes([0x01, 0x71, 0x12, 0x20])


def test_encoding_is_deterministic_across_key_order() -> None:
    a: FxtrDataValue = {"z": 1, "a": 2, "aa": 3, "": 4}
    b: FxtrDataValue = {"": 4, "aa": 3, "a": 2, "z": 1}
    assert codec.encode(a) == codec.encode(b)
    assert codec.compute_cid(codec.encode(a)) == codec.compute_cid(codec.encode(b))


def test_distinct_values_get_distinct_cids() -> None:
    assert codec.compute_cid(codec.encode({"x": 1})) != codec.compute_cid(codec.encode({"x": 2}))


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        False,
        0,
        -1,
        1234567890,
        2.0,
        "hello ☃",
        b"\x00\x01\x02",
        [],
        {},
        [1, "two", b"\x03", None, True],
        {"nested": {"list": [1, 2, {"deep": SAMPLE_ID}]}, "ref": SAMPLE_ID},
    ],
)
def test_encode_decode_roundtrip(value: FxtrDataValue) -> None:
    assert codec.decode(codec.encode(value)) == value


def test_link_wire_form_is_tag42_with_null_prefix() -> None:
    # tag 42 (d8 2a), byte string of length 37 (58 25), then 0x00 + the 36-byte CID.
    encoded = codec.encode(SAMPLE_ID)
    assert encoded == bytes([0xD8, 0x2A, 0x58, 0x25, 0x00]) + SAMPLE_CID


# Integer test vectors from the DRISL / cborc-42 spec (diagnostic value -> CBOR hex).
@pytest.mark.parametrize(
    "value,expected_hex",
    [
        (0, "00"),
        (-1, "20"),
        (23, "17"),
        (-24, "37"),
        (24, "1818"),
        (255, "18ff"),
        (256, "190100"),
        (65535, "19ffff"),
        (65536, "1a00010000"),
        (4294967295, "1affffffff"),
        (18446744073709551615, "1bffffffffffffffff"),
        (-18446744073709551616, "3bffffffffffffffff"),
    ],
)
def test_integer_spec_vectors(value: int, expected_hex: str) -> None:
    assert codec.encode(value).hex() == expected_hex


# Float vectors: DRISL requires the 64-bit (fb...) form always.
@pytest.mark.parametrize(
    "value,expected_hex",
    [
        (2.0, "fb4000000000000000"),
        (65504.0, "fb40effc0000000000"),
        (5e-324, "fb0000000000000001"),
        (1.7976931348623157e308, "fb7fefffffffffffff"),
        (10.559998512268066, "fb40251eb820000000"),
    ],
)
def test_float_spec_vectors(value: float, expected_hex: str) -> None:
    assert codec.encode(value).hex() == expected_hex


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_special_floats_rejected(value: float) -> None:
    with pytest.raises(ValueError):
        codec.encode(value)


def test_non_string_map_keys_rejected() -> None:
    with pytest.raises(TypeError):
        codec.encode({1: 2})  # pyright: ignore[reportArgumentType]


def test_decoded_links_are_entity_ids() -> None:
    (decoded,) = codec.iter_entity_ids(codec.decode(codec.encode([SAMPLE_ID])))
    assert decoded == SAMPLE_ID
    assert isinstance(decoded, EntityID)


def test_iter_entity_ids_walks_mutable_and_frozen() -> None:
    other = EntityID(cid=codec.compute_cid(codec.encode("other")))
    mutable = {"a": [SAMPLE_ID, {"b": other}], "c": "not an id"}
    assert set(codec.iter_entity_ids(mutable)) == {SAMPLE_ID, other}
    frozen = frozendict({"a": (SAMPLE_ID, frozendict({"b": other}))})
    assert set(codec.iter_entity_ids(frozen)) == {SAMPLE_ID, other}


def test_cid_string_format() -> None:
    assert SAMPLE_ID.cid_str().startswith("bafyrei")  # base32 of 01 71 12 20 ...


def test_cid_string_matches_dasl_example() -> None:
    # A raw-codec (0x55) CID string from the DASL spec; validates our cid_str output.
    example = "bafkreifn5yxi7nkftsn46b6x26grda57ict7md2xuvfbsgkiahe2e7vnq4"
    body = example[1:].upper()
    body += "=" * ((-len(body)) % 8)
    raw = base64.b32decode(body)
    assert raw[:4] == bytes([0x01, 0x55, 0x12, 0x20])
    assert EntityID(cid=raw).cid_str() == example


def test_freeze_thaw_roundtrip() -> None:
    value = {"a": [1, 2, {"b": b"\x00"}], "id": SAMPLE_ID}
    frozen = freeze_fxtr_data(value)
    assert isinstance(frozen, frozendict)
    assert hash(frozen) == hash(freeze_fxtr_data(value))
    assert thaw_fxtr_data(frozen) == value


def test_canonical_map_items_matches_wire_order() -> None:
    # Decoded dicts preserve wire order, so this checks our sort against cbrrr's canonical sort.
    value: FxtrDataValue = {"z": 1, "aa": 2, "b": 3, "": 4, "ab": 5, "a": 6, "é": 7}
    decoded = codec.decode(codec.encode(value))
    assert isinstance(decoded, dict)
    assert [k for k, _ in codec.canonical_map_items(value)] == list(decoded.keys())


def test_iter_entity_ids_yields_in_canonical_wire_order() -> None:
    other = EntityID(cid=codec.compute_cid(codec.encode("other")))
    # Canonical key order is length-first then bytewise: "b" before "z" before "aa".
    value: FxtrDataValue = {"z": [SAMPLE_ID], "aa": other, "b": other}
    assert list(codec.iter_entity_ids(value)) == [other, SAMPLE_ID, other]
    shuffled: FxtrDataValue = {"b": other, "aa": other, "z": [SAMPLE_ID]}
    assert list(codec.iter_entity_ids(shuffled)) == [other, SAMPLE_ID, other]


def test_encode_accepts_frozen_shapes() -> None:
    mutable = {"a": [1, 2], "id": SAMPLE_ID}
    assert codec.encode(freeze_fxtr_data(mutable)) == codec.encode(mutable)


def test_cid_string_round_trip() -> None:
    from fxtr.core.entity_data import cid_from_str, cid_to_str

    cid = codec.compute_cid(codec.encode({"$type": "fxtr.builtin.testing.node"}))
    assert cid_from_str(cid_to_str(cid)) == cid
    with pytest.raises(ValueError):
        cid_from_str("nope")
    with pytest.raises(ValueError):
        cid_from_str("b!!!")
