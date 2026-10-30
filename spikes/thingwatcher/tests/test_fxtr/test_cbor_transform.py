"""Tests for thingwatcher.fxtr.cbor_transform module."""

from __future__ import annotations

import pytest

# Skip all tests if thingwatcher is not installed
pytest.importorskip("thingwatcher")

from fxtr2.data.entity_core import FxtrID, RawFxtrEntityMarkerID, RawStrongFxtrID, RawWeakFxtrID
from thingwatcher.fxtr.cbor_transform import (
    fxtr_entity_to_thing_state,
    fxtr_key_to_thing_key,
    thing_key_to_fxtr_id,
)


class TestKeyConversion:
    """Tests for key conversion functions."""

    def test_fxtr_key_to_thing_key_builtin(self):
        """Test converting a builtin FxtrID to thing key."""
        fxtr_id = FxtrID.builtin("mytype")
        result = fxtr_key_to_thing_key(fxtr_id)
        assert result == ["fxtr_entity", "!mytype"]

    def test_fxtr_key_to_thing_key_ground(self):
        """Test converting the ground node to thing key."""
        fxtr_id = FxtrID.from_id_string(".")
        result = fxtr_key_to_thing_key(fxtr_id)
        assert result == ["fxtr_entity", "."]

    def test_thing_key_to_fxtr_id_builtin(self):
        """Test parsing a valid builtin thing key back to FxtrID."""
        key = ["fxtr_entity", "!mytype"]
        result = thing_key_to_fxtr_id(key)
        assert result is not None
        assert result == FxtrID.builtin("mytype")

    def test_thing_key_to_fxtr_id_invalid_format(self):
        """Test that invalid key formats return None."""
        assert thing_key_to_fxtr_id("not a list") is None
        assert thing_key_to_fxtr_id([]) is None
        assert thing_key_to_fxtr_id(["fxtr_entity"]) is None
        assert thing_key_to_fxtr_id(["fxtr_entity", "x", "y"]) is None

    def test_thing_key_to_fxtr_id_wrong_prefix(self):
        """Test that keys with wrong prefix return None."""
        assert thing_key_to_fxtr_id(["other_prefix", "!mytype"]) is None

    def test_thing_key_to_fxtr_id_invalid_id_string(self):
        """Test that invalid id strings return None."""
        assert thing_key_to_fxtr_id(["fxtr_entity", "invalid@@@"]) is None

    def test_thing_key_to_fxtr_id_non_string_value(self):
        """Test that non-string id values return None."""
        assert thing_key_to_fxtr_id(["fxtr_entity", 123]) is None

    def test_roundtrip_builtin(self):
        """Test that key conversion roundtrips for builtin IDs."""
        original = FxtrID.builtin("test_type")
        key = fxtr_key_to_thing_key(original)
        recovered = thing_key_to_fxtr_id(key)
        assert recovered is not None
        assert recovered == original

    def test_roundtrip_ground(self):
        """Test that key conversion roundtrips for ground node."""
        original = FxtrID.from_id_string(".")
        key = fxtr_key_to_thing_key(original)
        recovered = thing_key_to_fxtr_id(key)
        assert recovered is not None
        assert recovered == original


def _make_d_node(n: int) -> FxtrID:
    """Create a d-node ID with dummy payload for testing."""
    # Use SHA-256 sized payload (32 bytes)
    payload = bytes([n % 256] * 32)
    return FxtrID.from_tag_and_payload("d", payload)


def _make_c_node(n: int) -> FxtrID:
    """Create a c-node ID with dummy payload for testing."""
    payload = bytes([n % 256] * 32)
    return FxtrID.from_tag_and_payload("c", payload)


class TestCborTransform:
    """Tests for CBOR to ThingState transformation."""

    def test_simple_dict_entity(self):
        """Test transforming a simple dict entity."""
        entity_id = _make_d_node(1)
        ctx = FxtrID.from_id_string(".")
        cbor_data = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(ctx),
            "name": "test",
            "value": 42,
        }

        result = fxtr_entity_to_thing_state(entity_id, cbor_data)

        assert result.key == ["fxtr_entity", entity_id.id_string()]
        assert result.thing_type == "fxtr.transluce.org/fxtr_entity_fallback"
        assert result.thing_data["name"] == "test"
        assert result.thing_data["value"] == 42
        # $fxtr.ctx should be transformed to a marker object
        assert "$!fxtr.link.marker" in result.thing_data["$fxtr.ctx"]
        # Should have one link for the ctx marker
        assert len(result.links) == 1
        assert result.links[0].link_type == "marker"
        assert result.links[0].link_key == ["$fxtr.ctx"]

    def test_entity_with_fxtr_type(self):
        """Test that entities with $fxtr.type get a typed thing_type."""
        entity_id = _make_d_node(1)
        ctx = FxtrID.from_id_string(".")
        type_id = FxtrID.builtin("mytype")
        cbor_data = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(ctx),
            "$fxtr.type": RawWeakFxtrID(type_id),
            "data": "hello",
        }

        result = fxtr_entity_to_thing_state(entity_id, cbor_data)

        assert result.thing_type == "fxtr.transluce.org/fxtr_entity/d/!mytype"

    def test_non_d_node_gets_fallback_type(self):
        """Test that non-d-node entities always get fallback type."""
        # Use a c-node ID
        entity_id = _make_c_node(1)
        ctx = FxtrID.from_id_string(".")
        type_id = FxtrID.builtin("mytype")
        cbor_data = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(ctx),
            "$fxtr.type": RawWeakFxtrID(type_id),  # Has type, but not a d-node
        }

        result = fxtr_entity_to_thing_state(entity_id, cbor_data)

        # Should still be fallback because it's not a d-node
        assert result.thing_type == "fxtr.transluce.org/fxtr_entity_fallback"

    def test_weak_link_extraction(self):
        """Test that weak links are extracted correctly."""
        entity_id = _make_d_node(1)
        ref_id = _make_d_node(2)
        ctx = FxtrID.from_id_string(".")
        cbor_data = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(ctx),
            "reference": RawWeakFxtrID(ref_id),
        }

        result = fxtr_entity_to_thing_state(entity_id, cbor_data)

        # Find the weak link
        weak_links = [l for l in result.links if l.link_type == "weak"]
        assert len(weak_links) == 1
        assert weak_links[0].link_key == ["reference"]
        assert weak_links[0].to_thing_key == ["fxtr_entity", ref_id.id_string()]

        # Check marker in data
        assert result.thing_data["reference"] == {
            "$!fxtr.link.weak": ["fxtr_entity", ref_id.id_string()]
        }

    def test_strong_link_extraction(self):
        """Test that strong links are extracted correctly."""
        entity_id = _make_d_node(1)
        child_id = _make_d_node(2)
        ctx = FxtrID.from_id_string(".")
        cbor_data = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(ctx),
            "child": RawStrongFxtrID(child_id),
        }

        result = fxtr_entity_to_thing_state(entity_id, cbor_data)

        strong_links = [l for l in result.links if l.link_type == "strong"]
        assert len(strong_links) == 1
        assert strong_links[0].link_key == ["child"]
        assert strong_links[0].to_thing_key == ["fxtr_entity", child_id.id_string()]

    def test_nested_links(self):
        """Test that links in nested structures have correct paths."""
        entity_id = _make_d_node(1)
        ref1 = _make_d_node(2)
        ref2 = _make_d_node(3)
        ctx = FxtrID.from_id_string(".")
        cbor_data = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(ctx),
            "nested": {
                "deep": {
                    "link": RawWeakFxtrID(ref1),
                }
            },
            "array": [RawWeakFxtrID(ref2)],
        }

        result = fxtr_entity_to_thing_state(entity_id, cbor_data)

        # Find links by their paths
        link_paths = {tuple(l.link_key): l for l in result.links}

        assert ("nested", "deep", "link") in link_paths
        assert ("array", 0) in link_paths

    def test_list_with_multiple_links(self):
        """Test that list indices are correctly captured in link keys."""
        entity_id = _make_d_node(1)
        ref1 = _make_d_node(2)
        ref2 = _make_d_node(3)
        ctx = FxtrID.from_id_string(".")
        cbor_data = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(ctx),
            "items": [RawWeakFxtrID(ref1), RawWeakFxtrID(ref2)],
        }

        result = fxtr_entity_to_thing_state(entity_id, cbor_data)

        # Find links in the items array (excluding $fxtr.ctx marker)
        item_links = [l for l in result.links if l.link_key[0] == "items"]
        assert len(item_links) == 2

        paths = {tuple(l.link_key) for l in item_links}
        assert ("items", 0) in paths
        assert ("items", 1) in paths

    def test_bytes_conversion(self):
        """Test that bytes are converted to base64."""
        entity_id = _make_d_node(1)
        ctx = FxtrID.from_id_string(".")
        cbor_data = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(ctx),
            "data": b"hello world",
        }

        result = fxtr_entity_to_thing_state(entity_id, cbor_data)

        assert result.thing_data["data"] == {"$!bytes": "aGVsbG8gd29ybGQ="}

    def test_primitive_values_preserved(self):
        """Test that primitive values are preserved unchanged."""
        entity_id = _make_d_node(1)
        ctx = FxtrID.from_id_string(".")
        cbor_data = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(ctx),
            "string": "hello",
            "int": 42,
            "float": 3.14,
            "bool": True,
            "null": None,
        }

        result = fxtr_entity_to_thing_state(entity_id, cbor_data)

        assert result.thing_data["string"] == "hello"
        assert result.thing_data["int"] == 42
        assert result.thing_data["float"] == 3.14
        assert result.thing_data["bool"] is True
        assert result.thing_data["null"] is None
