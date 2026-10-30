"""Tests for fxtr2/data/entity_core.py."""

import hashlib

import pytest

from fxtr2.data.entity_core import (
    DuplicateEntityMarkerError,
    FxtrCbor,
    FxtrID,
    InvalidEntityError,
    RawFxtrEntityMarkerID,
    RawStrongFxtrID,
    RawWeakFxtrID,
    _b32dec,
    _b32enc,
    _decode_varint32,
    _encode_varint32,
    fxtr_cbor_dumps,
    fxtr_cbor_dumps_with_marker_check,
    fxtr_cbor_lift_to_provenance,
    fxtr_cbor_loads,
)

# =============================================================================
# FxtrID Construction and Basic Operations
# =============================================================================


class TestFxtrIDBasics:
    """Tests for FxtrID construction and basic operations."""

    def test_fxtr_id_immutability(self):
        """Verify FxtrID is immutable."""
        fid = FxtrID.from_tag_and_payload(".", b"")
        with pytest.raises(AttributeError, match="immutable"):
            fid._raw_data_id = b"x"

    def test_fxtr_id_equality_same(self):
        """Two FxtrIDs with same raw_data_id are equal."""
        fid1 = FxtrID.from_tag_and_payload(".", b"")
        fid2 = FxtrID.from_tag_and_payload(".", b"")
        assert fid1 == fid2

    def test_fxtr_id_equality_different(self):
        """Two FxtrIDs with different raw_data_id are not equal."""
        fid1 = FxtrID.from_tag_and_payload(".", b"")
        fid2 = FxtrID.builtin("test")
        assert fid1 != fid2

    def test_fxtr_id_hash(self):
        """Equal FxtrIDs have same hash, can be used as dict keys."""
        fid1 = FxtrID.from_tag_and_payload(".", b"")
        fid2 = FxtrID.from_tag_and_payload(".", b"")
        assert hash(fid1) == hash(fid2)

        # Can use as dict key
        d = {fid1: "value"}
        assert d[fid2] == "value"

    def test_fxtr_id_equality_ignores_provenance_parent(self):
        """Two FxtrIDs with same data but different provenance parents are equal."""
        parent1 = FxtrID.builtin("parent1")
        parent2 = FxtrID.builtin("parent2")
        fid1 = FxtrID.from_tag_and_payload(".", b"", provenance_parent=parent1)
        fid2 = FxtrID.from_tag_and_payload(".", b"", provenance_parent=parent2)
        assert fid1 == fid2
        assert hash(fid1) == hash(fid2)

    def test_fxtr_id_from_tag_and_payload(self):
        """Create FxtrID from tag and payload."""
        fid = FxtrID.from_tag_and_payload("!", b"test")
        assert fid.get_tag() == "!"
        tag, payload = fid.split_tag_and_payload()
        assert tag == "!"
        assert payload == b"test"

    def test_fxtr_id_split_tag_and_payload_roundtrip(self):
        """Round-trip tag/payload extraction."""
        original = FxtrID.builtin("example")
        tag, payload = original.split_tag_and_payload()
        reconstructed = FxtrID.from_tag_and_payload(tag, payload)
        assert original == reconstructed

    def test_fxtr_id_get_tag(self):
        """get_tag returns correct single-character tag."""
        assert FxtrID.from_tag_and_payload(".", b"").get_tag() == "."
        assert FxtrID.from_tag_and_payload("!", b"x").get_tag() == "!"
        assert FxtrID.from_tag_and_payload("d", b"x").get_tag() == "d"


# =============================================================================
# FxtrID String Serialization
# =============================================================================


class TestFxtrIDStringSerialization:
    """Tests for FxtrID string serialization and parsing."""

    def test_here_node_id_string_roundtrip(self):
        """'.' node roundtrips through string serialization."""
        fid = FxtrID.from_tag_and_payload(".", b"")
        s = fid.local_id_string()
        assert s == "."
        parsed = FxtrID.from_id_string(s)
        assert fid == parsed

    def test_builtin_id_string_roundtrip(self):
        """'!' node roundtrips through string serialization."""
        fid = FxtrID.builtin("test_type")
        s = fid.local_id_string()
        assert s == "!test_type"
        parsed = FxtrID.from_id_string(s)
        assert fid == parsed

    def test_d_node_id_string_roundtrip(self):
        """'d' node roundtrips through string serialization."""
        hash_bytes = hashlib.sha256(b"test data").digest()
        fid = FxtrID.from_tag_and_payload("d", hash_bytes)
        s = fid.local_id_string()
        assert s.startswith("d")
        parsed = FxtrID.from_id_string(s)
        assert fid == parsed

    def test_n_node_id_string_roundtrip(self):
        """'n' node roundtrips through string serialization."""
        hash_bytes = hashlib.sha256(b"test data").digest()
        fid = FxtrID.from_tag_and_payload("n", hash_bytes)
        s = fid.local_id_string()
        assert s.startswith("n")
        parsed = FxtrID.from_id_string(s)
        assert fid == parsed

    def test_c_node_id_string_roundtrip(self):
        """'c' node roundtrips through string serialization."""
        hash_bytes = hashlib.sha256(b"test data").digest()
        fid = FxtrID.from_tag_and_payload("c", hash_bytes)
        s = fid.local_id_string()
        assert s.startswith("c")
        parsed = FxtrID.from_id_string(s)
        assert fid == parsed

    def test_u_node_id_string_roundtrip(self):
        """'u' node roundtrips through string serialization."""
        inner = FxtrID.from_tag_and_payload(".", b"")
        fid = FxtrID.from_tag_and_payload("u", inner._raw_data_id)
        s = fid.local_id_string()
        assert s == "u."
        parsed = FxtrID.from_id_string(s)
        assert fid == parsed

    def test_p_node_empty_path_roundtrip(self):
        """'p' node with empty path roundtrips."""
        import cbor2

        hash_bytes = hashlib.sha256(b"test").digest()
        payload = cbor2.dumps([hash_bytes], canonical=True)
        fid = FxtrID.from_tag_and_payload("p", payload)
        s = fid.local_id_string()
        assert "[" in s and "]" in s
        parsed = FxtrID.from_id_string(s)
        assert fid == parsed

    def test_p_node_with_path_roundtrip(self):
        """'p' node with path indices roundtrips."""
        import cbor2

        hash_bytes = hashlib.sha256(b"test").digest()
        payload = cbor2.dumps([hash_bytes, 0, 1, 2], canonical=True)
        fid = FxtrID.from_tag_and_payload("p", payload)
        s = fid.local_id_string()
        parsed = FxtrID.from_id_string(s)
        assert fid == parsed

    def test_id_string_excludes_provenance_by_default(self):
        """id_string() excludes provenance by default."""
        parent = FxtrID.builtin("parent")
        fid = FxtrID.from_tag_and_payload(".", b"", provenance_parent=parent)
        assert fid.id_string() == "."

    def test_id_string_with_provenance_opt_in(self):
        """id_string(include_provenance=True) includes @provenance_part."""
        parent = FxtrID.builtin("parent")
        fid = FxtrID.from_tag_and_payload(".", b"", provenance_parent=parent)
        s = fid.id_string(include_provenance=True)
        assert s == ".@!parent"

    def test_from_id_string_with_provenance(self):
        """from_id_string() parses X@Y format."""
        fid = FxtrID.from_id_string(".@!parent")
        assert fid.get_tag() == "."
        assert fid._provenance_parent is not None
        assert fid._provenance_parent.get_tag() == "!"

    def test_from_id_string_allow_provenance_false_rejects_at_form(self):
        """allow_provenance=False rejects the @-form (for bare-id wire slots)."""
        with pytest.raises(ValueError, match="allow_provenance"):
            FxtrID.from_id_string(".@!parent", allow_provenance=False)
        # A bare id still parses fine under allow_provenance=False.
        bare = FxtrID.from_id_string(".", allow_provenance=False)
        assert bare.get_tag() == "." and bare._provenance_parent is None

    def test_from_id_string_empty_raises(self):
        """Empty string raises ValueError."""
        with pytest.raises(ValueError, match="Empty"):
            FxtrID.from_id_string("")

    def test_from_id_string_unknown_tag_raises(self):
        """Unknown tag raises ValueError."""
        with pytest.raises(ValueError, match="Unknown tag"):
            FxtrID.from_id_string("Xinvalid")

    def test_here_node_summary(self):
        """'.' node summary is '.'."""
        fid = FxtrID.from_tag_and_payload(".", b"")
        assert fid.local_id_summary() == "."

    def test_d_node_summary_truncated(self):
        """'d' node summary is truncated."""
        hash_bytes = hashlib.sha256(b"test data").digest()
        fid = FxtrID.from_tag_and_payload("d", hash_bytes)
        summary = fid.local_id_summary()
        assert summary.startswith("d")
        assert len(summary) == 8  # "d" + 7 chars


# =============================================================================
# Node Type Specific Tests
# =============================================================================


class TestHereNode:
    """Tests for '.' (here/ground) nodes."""

    def test_here_node_creation(self):
        """Create '.' node with empty payload."""
        fid = FxtrID.from_tag_and_payload(".", b"")
        assert fid.get_tag() == "."
        _, payload = fid.split_tag_and_payload()
        assert payload == b""

    def test_here_node_payload_must_be_empty(self):
        """Non-empty payload raises error in from_id_string."""
        with pytest.raises(ValueError, match="Expected empty"):
            FxtrID.from_id_string(".extra")


class TestBuiltinNode:
    """Tests for '!' (primitive/builtin) nodes."""

    def test_builtin_creation(self):
        """FxtrID.builtin('name') creates correct node."""
        fid = FxtrID.builtin("my_type")
        assert fid.get_tag() == "!"
        _, payload = fid.split_tag_and_payload()
        assert payload == b"my_type"

    def test_builtin_id_string(self):
        """Returns '!name' format."""
        fid = FxtrID.builtin("test")
        assert fid.local_id_string() == "!test"

    def test_builtin_various_names(self):
        """Various builtin names roundtrip correctly."""
        for name in ["ctx", "pathroot", "entity", "my_custom_type"]:
            fid = FxtrID.builtin(name)
            parsed = FxtrID.from_id_string(fid.local_id_string())
            assert fid == parsed


class TestUNode:
    """Tests for 'u' (untracked) nodes."""

    def test_u_node_wrapping_here(self):
        """u node wrapping '.' node."""
        inner = FxtrID.from_tag_and_payload(".", b"")
        fid = FxtrID.from_tag_and_payload("u", inner._raw_data_id)
        assert fid.get_tag() == "u"
        assert fid.local_id_string() == "u."

    def test_u_node_wrapping_d_node(self):
        """u node wrapping a d node."""
        hash_bytes = hashlib.sha256(b"test").digest()
        inner = FxtrID.from_tag_and_payload("d", hash_bytes)
        fid = FxtrID.from_tag_and_payload("u", inner._raw_data_id)
        assert fid.get_tag() == "u"
        s = fid.local_id_string()
        assert s.startswith("ud")


# =============================================================================
# Varint32 Encoding
# =============================================================================


class TestVarint32:
    """Tests for varint32 encoding used by p-nodes."""

    def test_encode_varint32_zero(self):
        """0 encodes to '0'."""
        assert _encode_varint32(0) == "0"

    def test_encode_varint32_small(self):
        """Values 1-15 encode to single character."""
        for i in range(16):
            encoded = _encode_varint32(i)
            assert len(encoded) == 1

    def test_encode_varint32_continuation(self):
        """Values >= 16 use continuation."""
        encoded = _encode_varint32(16)
        assert len(encoded) == 2

    def test_encode_varint32_large(self):
        """Large values encode correctly."""
        encoded = _encode_varint32(1000000)
        assert len(encoded) > 1

    def test_decode_varint32_roundtrip(self):
        """encode -> decode roundtrips."""
        for value in [0, 1, 15, 16, 100, 1000, 65535, 1000000]:
            encoded = _encode_varint32(value)
            decoded, remaining = _decode_varint32(encoded)
            assert decoded == value
            assert remaining == ""

    def test_encode_varint32_negative_raises(self):
        """Negative values raise ValueError."""
        with pytest.raises(ValueError, match="non-negative"):
            _encode_varint32(-1)


# =============================================================================
# Base32 Encoding Helpers
# =============================================================================


class TestBase32Helpers:
    """Tests for base32 encoding helpers."""

    def test_b32enc_dec_roundtrip(self):
        """_b32enc -> _b32dec roundtrips."""
        for data in [b"", b"x", b"hello", hashlib.sha256(b"test").digest()]:
            encoded = _b32enc(data)
            decoded = _b32dec(encoded)
            assert decoded == data

    def test_b32dec_case_insensitive(self):
        """Decoding is case-insensitive."""
        data = b"test"
        encoded = _b32enc(data)
        assert _b32dec(encoded.upper()) == data
        assert _b32dec(encoded.lower()) == data

    def test_b32enc_no_padding(self):
        """Encoded strings have no '=' padding."""
        for data in [b"x", b"xx", b"xxx", b"xxxx"]:
            encoded = _b32enc(data)
            assert "=" not in encoded


# =============================================================================
# CBOR Wrapper Classes
# =============================================================================


class TestCborWrapperClasses:
    """Tests for Raw*FxtrID classes."""

    def test_raw_weak_fxtr_id(self):
        """RawWeakFxtrID wraps FxtrID correctly."""
        fid = FxtrID.builtin("test")
        raw = RawWeakFxtrID(inner_id=fid)
        assert raw.inner_id == fid
        assert raw._CBOR_TAG == 33001

    def test_raw_strong_fxtr_id(self):
        """RawStrongFxtrID wraps FxtrID correctly."""
        fid = FxtrID.builtin("test")
        raw = RawStrongFxtrID(inner_id=fid)
        assert raw.inner_id == fid
        assert raw._CBOR_TAG == 33002

    def test_raw_entity_marker_id(self):
        """RawFxtrEntityMarkerID wraps FxtrID correctly."""
        fid = FxtrID.from_tag_and_payload(".", b"")
        raw = RawFxtrEntityMarkerID(inner_id=fid)
        assert raw.inner_id == fid
        assert raw._CBOR_TAG == 33000

    def test_raw_id_classes_frozen(self):
        """All Raw*ID classes are frozen dataclasses."""
        fid = FxtrID.builtin("test")
        weak = RawWeakFxtrID(inner_id=fid)
        with pytest.raises(Exception):  # FrozenInstanceError
            weak.inner_id = fid


# =============================================================================
# CBOR Encoding/Decoding
# =============================================================================


class TestCborEncoding:
    """Tests for CBOR encoding/decoding."""

    def test_fxtr_cbor_dumps_loads_roundtrip(self):
        """Simple entity roundtrips."""
        ground = FxtrID.from_tag_and_payload(".", b"")
        type_id = FxtrID.builtin("test")
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
            "$fxtr.type": RawWeakFxtrID(inner_id=type_id),
            "name": "test",
            "value": 42,
        }
        encoded = fxtr_cbor_dumps(entity)
        decoded = fxtr_cbor_loads(encoded)
        assert decoded["name"] == "test"
        assert decoded["value"] == 42
        assert isinstance(decoded["$fxtr.ctx"], RawFxtrEntityMarkerID)
        assert isinstance(decoded["$fxtr.type"], RawWeakFxtrID)

    def test_fxtr_cbor_primitive_types(self):
        """All primitive types work."""
        ground = FxtrID.from_tag_and_payload(".", b"")
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
            "none_val": None,
            "bool_true": True,
            "bool_false": False,
            "int_val": 42,
            "float_val": 3.14,
            "str_val": "hello",
            "bytes_val": b"world",
            "list_val": [1, 2, 3],
            "dict_val": {"a": 1, "b": 2},
        }
        encoded = fxtr_cbor_dumps(entity)
        decoded = fxtr_cbor_loads(encoded)
        assert decoded["none_val"] is None
        assert decoded["bool_true"] is True
        assert decoded["bool_false"] is False
        assert decoded["int_val"] == 42
        assert decoded["float_val"] == 3.14
        assert decoded["str_val"] == "hello"
        assert decoded["bytes_val"] == b"world"
        assert decoded["list_val"] == [1, 2, 3]
        assert decoded["dict_val"] == {"a": 1, "b": 2}


class TestCborMarkerCheck:
    """Tests for fxtr_cbor_dumps_with_marker_check."""

    def test_marker_check_valid_entity(self):
        """Valid entity passes check."""
        ground = FxtrID.from_tag_and_payload(".", b"")
        entity: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
            "data": "test",
        }
        # Should not raise
        encoded = fxtr_cbor_dumps_with_marker_check(entity)
        assert encoded is not None

    def test_marker_check_non_dict_raises(self):
        """Non-dict raises InvalidEntityError."""
        with pytest.raises(InvalidEntityError, match="dictionary"):
            fxtr_cbor_dumps_with_marker_check([1, 2, 3])  # type: ignore

    def test_marker_check_missing_ctx_raises(self):
        """Missing $fxtr.ctx raises InvalidEntityError."""
        with pytest.raises(InvalidEntityError, match="\\$fxtr.ctx"):
            fxtr_cbor_dumps_with_marker_check({"data": "test"})

    def test_marker_check_wrong_ctx_type_raises(self):
        """Wrong type for $fxtr.ctx raises InvalidEntityError."""
        ground = FxtrID.from_tag_and_payload(".", b"")
        with pytest.raises(InvalidEntityError, match="RawFxtrEntityMarkerID"):
            fxtr_cbor_dumps_with_marker_check(
                {
                    "$fxtr.ctx": RawWeakFxtrID(inner_id=ground),  # Wrong type
                    "data": "test",
                }
            )

    def test_marker_check_multiple_markers_raises(self):
        """Multiple RawFxtrEntityMarkerID raises DuplicateEntityMarkerError."""
        ground = FxtrID.from_tag_and_payload(".", b"")
        with pytest.raises(DuplicateEntityMarkerError, match="more than one"):
            fxtr_cbor_dumps_with_marker_check(
                {
                    "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
                    "nested": {"$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground)},
                }
            )


# =============================================================================
# Lift to Provenance
# =============================================================================


class TestLiftToProvenance:
    """Tests for fxtr_cbor_lift_to_provenance."""

    def test_lift_to_provenance_weak_id(self):
        """Lifts RawWeakFxtrID."""
        parent = FxtrID.builtin("parent")
        # Create inner with provenance_parent using the constructor
        inner_data = FxtrID.builtin("inner")
        inner = FxtrID(raw_data_id=inner_data._raw_data_id, provenance_parent=parent)
        raw = RawWeakFxtrID(inner_id=inner)
        lifted = fxtr_cbor_lift_to_provenance(raw)
        assert isinstance(lifted, RawWeakFxtrID)
        assert lifted.inner_id == parent

    def test_lift_to_provenance_nested(self):
        """Lifts IDs in nested structures."""
        parent = FxtrID.builtin("parent")
        inner_data = FxtrID.builtin("inner")
        inner = FxtrID(raw_data_id=inner_data._raw_data_id, provenance_parent=parent)
        data: FxtrCbor = {
            "list": [RawWeakFxtrID(inner_id=inner)],
            "nested": {"ref": RawStrongFxtrID(inner_id=inner)},
        }
        lifted = fxtr_cbor_lift_to_provenance(data)
        assert isinstance(lifted, dict)
        assert lifted["list"][0].inner_id == parent  # type: ignore
        assert lifted["nested"]["ref"].inner_id == parent  # type: ignore

    def test_lift_to_provenance_missing_parent_raises(self):
        """Raises if provenance_parent missing."""
        inner = FxtrID.builtin("inner")  # No parent
        raw = RawWeakFxtrID(inner_id=inner)
        with pytest.raises(ValueError, match="provenance"):
            fxtr_cbor_lift_to_provenance(raw)

    def test_lift_to_provenance_primitives_unchanged(self):
        """Primitives pass through unchanged."""
        assert fxtr_cbor_lift_to_provenance(None) is None
        assert fxtr_cbor_lift_to_provenance(42) == 42
        assert fxtr_cbor_lift_to_provenance("test") == "test"
        assert fxtr_cbor_lift_to_provenance(b"bytes") == b"bytes"


# =============================================================================
# FxtrID repr
# =============================================================================


class TestFxtrIDStrAndRepr:
    """Tests for FxtrID __str__ and __repr__."""

    def test_str_returns_full_id_string(self):
        """str() returns the full id_string, usable in f-strings for pins etc."""
        hash_bytes = hashlib.sha256(b"test data").digest()
        fid = FxtrID.from_tag_and_payload("d", hash_bytes)
        assert str(fid) == fid.id_string()
        assert "<" not in f"transcripts/{fid}"

    def test_str_excludes_provenance(self):
        """str() does not include provenance info."""
        parent = FxtrID.builtin("parent")
        fid = FxtrID.from_tag_and_payload(".", b"", provenance_parent=parent)
        assert str(fid) == "."
        assert "@" not in str(fid)

    def test_repr_is_short_and_includes_provenance(self):
        """repr uses the short summary form and includes provenance."""
        parent = FxtrID.builtin("parent")
        hash_bytes = hashlib.sha256(b"test data").digest()
        fid = FxtrID.from_tag_and_payload("d", hash_bytes, provenance_parent=parent)
        r = repr(fid)
        assert r == f"<FxtrID {fid.summary(include_provenance=True)}>"
        assert "@" in r
        # repr is shorter than str due to truncation
        assert len(fid.summary()) < len(str(fid))
