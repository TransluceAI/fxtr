"""Tests for entity blob encoding/decoding, record envelopes, and precomp map building."""

import pytest

from fxtr2.data.sqlite_store.entity_blob import (
    HEADER_SIZE,
    RECORD_ENVELOPE_HEADER,
    build_closed_precomp_map_for_segments,
    decode_blob_header,
    decode_cbor_bytes,
    decode_fxtr_id_bytes,
    decode_metadata,
    encode_blob,
    encode_record_envelope,
)


class TestBlobEncodeDecode:
    """Test basic blob encoding and decoding."""

    def test_encode_decode_roundtrip_with_cbor(self):
        """Test encoding and decoding a blob with CBOR data."""
        fxtr_id_bytes = b"test_fxtr_id_12345"
        cbor_bytes = b"cbor_data_here"
        metadata = {
            "deps": [[1, "strong_ref"], [2, "weak_ref"]],
            "precomp": {
                1: [b"id1", None, None, None],
                2: [b"id2", 3, None, None],
            },
        }

        blob = encode_blob(fxtr_id_bytes, cbor_bytes, metadata)

        assert len(blob) > HEADER_SIZE
        offset_fxtr_id, offset_cbor, offset_metadata = decode_blob_header(blob)
        assert offset_fxtr_id == HEADER_SIZE
        assert offset_cbor == HEADER_SIZE + len(fxtr_id_bytes)
        assert offset_metadata == offset_cbor + len(cbor_bytes)

        decoded_fxtr_id = decode_fxtr_id_bytes(blob)
        assert decoded_fxtr_id == fxtr_id_bytes

        decoded_cbor = decode_cbor_bytes(blob)
        assert decoded_cbor == cbor_bytes

        deps_list, precomp_map = decode_metadata(blob)
        assert deps_list == [(1, "strong_ref"), (2, "weak_ref")]
        assert precomp_map == metadata["precomp"]

    def test_encode_decode_roundtrip_without_cbor(self):
        """Test encoding and decoding a derived node (no CBOR data)."""
        fxtr_id_bytes = b"derived_node_id"
        cbor_bytes = None
        metadata = {
            "deps": [[10, "ctx_node"]],
            "precomp": {10: [b"id10", None, None, None]},
        }

        blob = encode_blob(fxtr_id_bytes, cbor_bytes, metadata)

        offset_fxtr_id, offset_cbor, offset_metadata = decode_blob_header(blob)
        assert offset_fxtr_id == HEADER_SIZE
        assert offset_cbor == HEADER_SIZE + len(fxtr_id_bytes)
        assert offset_metadata == offset_cbor  # Empty CBOR section

        decoded_fxtr_id = decode_fxtr_id_bytes(blob)
        assert decoded_fxtr_id == fxtr_id_bytes

        decoded_cbor = decode_cbor_bytes(blob)
        assert decoded_cbor is None

        deps_list, precomp_map = decode_metadata(blob)
        assert deps_list == [(10, "ctx_node")]
        assert precomp_map == metadata["precomp"]

    def test_empty_deps_and_precomp(self):
        """Test blob with empty deps and precomp."""
        fxtr_id_bytes = b"simple_id"
        cbor_bytes = b"data"
        metadata = {"deps": [], "precomp": {}}

        blob = encode_blob(fxtr_id_bytes, cbor_bytes, metadata)
        deps_list, precomp_map = decode_metadata(blob)

        assert deps_list == []
        assert precomp_map == {}

    def test_memoryview_decode(self):
        """Test that decoding works with memoryview."""
        fxtr_id_bytes = b"test_id"
        cbor_bytes = b"test_cbor"
        metadata = {"deps": [[1, "strong_ref"]], "precomp": {1: [b"id1", None, None, None]}}

        blob = encode_blob(fxtr_id_bytes, cbor_bytes, metadata)
        mv = memoryview(blob)

        assert decode_fxtr_id_bytes(mv) == fxtr_id_bytes
        assert decode_cbor_bytes(mv) == cbor_bytes
        deps_list, precomp_map = decode_metadata(mv)
        assert deps_list == [(1, "strong_ref")]
        assert precomp_map[1] == [b"id1", None, None, None]


class TestRecordEnvelope:
    """Test record envelope encoding."""

    def test_encode_record_envelope_format(self):
        """Test that encode_record_envelope produces correct format."""
        blob = b"hello world"
        envelope = encode_record_envelope(blob)

        # Should be 8-byte header + blob
        assert len(envelope) == 8 + len(blob)

        # First 8 bytes should be little-endian length
        (length,) = RECORD_ENVELOPE_HEADER.unpack_from(envelope, 0)
        assert length == len(blob)

        # Remaining bytes should be the blob
        assert envelope[8:] == blob

    def test_encode_record_envelope_empty_blob(self):
        """Test envelope with an empty blob."""
        blob = b""
        envelope = encode_record_envelope(blob)
        assert len(envelope) == 8
        (length,) = RECORD_ENVELOPE_HEADER.unpack_from(envelope, 0)
        assert length == 0

    def test_encode_record_envelope_large_blob(self):
        """Test envelope with a large blob."""
        blob = b"x" * 100_000
        envelope = encode_record_envelope(blob)
        assert len(envelope) == 8 + 100_000
        (length,) = RECORD_ENVELOPE_HEADER.unpack_from(envelope, 0)
        assert length == 100_000
        assert envelope[8:] == blob

    def test_precomp_with_5th_element(self):
        """Test that blobs with 5-element precomp entries roundtrip correctly."""
        fxtr_id_bytes = b"test_id"
        metadata = {
            "deps": [[1, "strong_ref"]],
            "precomp": {
                1: [b"id1", None, None, None, 128],  # 5th element: seg_offset
                2: [b"id2", 1, None, None, None],  # 5th element: null (cross-segment)
            },
        }

        blob = encode_blob(fxtr_id_bytes, b"cbor", metadata)
        deps_list, precomp_map = decode_metadata(blob)
        assert precomp_map[1] == [b"id1", None, None, None, 128]
        assert precomp_map[2] == [b"id2", 1, None, None, None]


def _make_blob_store(
    blobs: dict[int, bytes],
    cross_segment_ids: set[int] | None = None,
):
    """Create a read_blob callback backed by a dict.

    Returns (blob, same_segment) tuples. By default all deps are same-segment.
    Pass cross_segment_ids to mark specific deps as cross-segment.
    """

    def read_blob(internal_id: int) -> tuple[bytes, bool]:
        if internal_id not in blobs:
            raise ValueError(f"Dependency with internal_id {internal_id} not found")
        same_segment = cross_segment_ids is None or internal_id not in cross_segment_ids
        return blobs[internal_id], same_segment

    return read_blob


class TestBuildClosedPrecompMapForSegments:
    """Test building closed precomp maps with the segment-file variant."""

    def test_no_deps(self):
        """Test building a precomp map for an entity with no dependencies."""
        own_internal_id = 100
        own_precomp_entry = [b"entity100", None, None, None, None]
        precomp_cache: dict[int, dict[int, list]] = {}

        result = build_closed_precomp_map_for_segments(
            own_internal_id,
            own_precomp_entry,
            [],
            precomp_cache,
            _make_blob_store({}),
        )

        assert result == {100: [b"entity100", None, None, None, None]}

    def test_with_deps(self):
        """Test building a precomp map for an entity with dependencies."""
        # Create dep blobs
        dep1_metadata = {
            "deps": [],
            "precomp": {1: [b"dep1_id", None, None, None, 0]},
        }
        dep1_blob = encode_blob(b"dep1_id", b"dep1_cbor", dep1_metadata)

        dep2_metadata = {
            "deps": [],
            "precomp": {
                2: [b"dep2_id", 3, None, None, 100],
                3: [b"dep3_id", None, None, None, 200],
            },
        }
        dep2_blob = encode_blob(b"dep2_id", b"dep2_cbor", dep2_metadata)

        blobs = {1: dep1_blob, 2: dep2_blob}
        precomp_cache: dict[int, dict[int, list]] = {}

        own_precomp_entry = [b"entity100", None, None, None, None]
        deps_with_internal_ids = [(1, "strong_ref"), (2, "weak_ref")]

        result = build_closed_precomp_map_for_segments(
            100,
            own_precomp_entry,
            deps_with_internal_ids,
            precomp_cache,
            _make_blob_store(blobs),
        )

        assert len(result) == 4
        assert result[100] == [b"entity100", None, None, None, None]
        assert result[1] == [b"dep1_id", None, None, None, 0]
        assert result[2] == [b"dep2_id", 3, None, None, 100]
        assert result[3] == [b"dep3_id", None, None, None, 200]

        # Cache should be populated
        assert 1 in precomp_cache
        assert 2 in precomp_cache

    def test_deduplicates_shared_deps(self):
        """Test that overlapping dependencies are deduplicated."""
        shared_metadata = {
            "deps": [],
            "precomp": {50: [b"shared_id", None, None, None, None]},
        }
        shared_blob = encode_blob(b"shared_id", b"shared_cbor", shared_metadata)

        dep1_metadata = {
            "deps": [[50, "weak_ref"]],
            "precomp": {
                1: [b"dep1_id", None, None, None, None],
                50: [b"shared_id", None, None, None, None],
            },
        }
        dep1_blob = encode_blob(b"dep1_id", b"dep1_cbor", dep1_metadata)

        dep2_metadata = {
            "deps": [[50, "weak_ref"]],
            "precomp": {
                2: [b"dep2_id", None, None, None, None],
                50: [b"shared_id", None, None, None, None],
            },
        }
        dep2_blob = encode_blob(b"dep2_id", b"dep2_cbor", dep2_metadata)

        blobs = {50: shared_blob, 1: dep1_blob, 2: dep2_blob}
        precomp_cache: dict[int, dict[int, list]] = {}

        result = build_closed_precomp_map_for_segments(
            100,
            [b"entity100", None, None, None, None],
            [(1, "strong_ref"), (2, "strong_ref")],
            precomp_cache,
            _make_blob_store(blobs),
        )

        assert len(result) == 4  # 100, 1, 2, 50
        assert result[50] == [b"shared_id", None, None, None, None]

    def test_missing_dep_raises(self):
        """Test that missing deps raise ValueError."""
        with pytest.raises(ValueError, match="not found"):
            build_closed_precomp_map_for_segments(
                100,
                [b"entity100", None, None, None, None],
                [(999, "strong_ref")],
                {},
                _make_blob_store({}),
            )

    def test_uses_cache(self):
        """Test that the precomp cache is checked before calling read_blob."""
        # Pre-populate cache with dep1's precomp map
        dep1_precomp = {1: [b"dep1_id", None, None, None, 0]}
        precomp_cache: dict[int, dict[int, list]] = {1: dep1_precomp}

        call_count = 0
        original_store = _make_blob_store({})

        def counting_read_blob(internal_id: int) -> tuple[bytes, bool]:
            nonlocal call_count
            call_count += 1
            return original_store(internal_id)

        result = build_closed_precomp_map_for_segments(
            100,
            [b"entity100", None, None, None, None],
            [(1, "strong_ref")],
            precomp_cache,
            counting_read_blob,
        )

        assert len(result) == 2
        assert result[1] == [b"dep1_id", None, None, None, 0]
        assert call_count == 0, "Should not have called read_blob since dep was cached"

    def test_cross_segment_offsets_cleared(self):
        """Inherited offsets from a dep in a different segment are cleared to None."""
        dep_metadata = {
            "deps": [],
            "precomp": {
                1: [b"dep1_id", None, None, None, 42],
                2: [b"dep2_id", None, None, None, 99],
            },
        }
        dep_blob = encode_blob(b"dep1_id", b"dep1_cbor", dep_metadata)

        result = build_closed_precomp_map_for_segments(
            100,
            [b"entity100", None, None, None, None],
            [(1, "strong_ref")],
            {},
            _make_blob_store({1: dep_blob}, cross_segment_ids={1}),
        )

        assert len(result) == 3
        # Inherited entries should have offsets cleared (cross-segment)
        assert result[1][4] is None
        assert result[2][4] is None
        # Original dep blob should be unmodified
        assert dep_metadata["precomp"][1][4] == 42
        assert dep_metadata["precomp"][2][4] == 99

    def test_same_segment_offsets_preserved(self):
        """Inherited offsets from a dep in the same segment are preserved."""
        dep_metadata = {
            "deps": [],
            "precomp": {
                1: [b"dep1_id", None, None, None, 42],
                2: [b"dep2_id", None, None, None, 99],
            },
        }
        dep_blob = encode_blob(b"dep1_id", b"dep1_cbor", dep_metadata)

        result = build_closed_precomp_map_for_segments(
            100,
            [b"entity100", None, None, None, None],
            [(1, "strong_ref")],
            {},
            _make_blob_store({1: dep_blob}),  # same segment by default
        )

        assert len(result) == 3
        # Same-segment offsets should be preserved
        assert result[1][4] == 42
        assert result[2][4] == 99
