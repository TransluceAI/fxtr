"""Tests for fxtr2/data/arena_util.py."""

import pytest

from fxtr2.data.arena_util import (
    async_visit_and_project_ids_in_canonical_cbor_order,
    visit_and_project_ids,
    visit_and_project_ids_in_canonical_cbor_order,
    visit_ids,
)
from fxtr2.data.entity_core import (
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawStrongFxtrID,
    RawWeakFxtrID,
)

# =============================================================================
# visit_ids Tests
# =============================================================================


class TestVisitIds:
    """Tests for visit_ids function."""

    def test_visit_ids_weak_ref(self):
        """Calls callback for RawWeakFxtrID."""
        fid = FxtrID.builtin("test")
        raw = RawWeakFxtrID(inner_id=fid)
        visited = []
        visit_ids(raw, visited.append)
        assert len(visited) == 1
        assert visited[0] == raw

    def test_visit_ids_strong_ref(self):
        """Calls callback for RawStrongFxtrID."""
        fid = FxtrID.builtin("test")
        raw = RawStrongFxtrID(inner_id=fid)
        visited = []
        visit_ids(raw, visited.append)
        assert len(visited) == 1
        assert visited[0] == raw

    def test_visit_ids_marker(self):
        """Calls callback for RawFxtrEntityMarkerID."""
        fid = FxtrID.from_tag_and_payload(".", b"")
        raw = RawFxtrEntityMarkerID(inner_id=fid)
        visited = []
        visit_ids(raw, visited.append)
        assert len(visited) == 1
        assert visited[0] == raw

    def test_visit_ids_nested_list(self):
        """Visits IDs in nested lists."""
        fid1 = FxtrID.builtin("a")
        fid2 = FxtrID.builtin("b")
        data: FxtrCbor = [
            RawWeakFxtrID(inner_id=fid1),
            [RawWeakFxtrID(inner_id=fid2)],
        ]
        visited = []
        visit_ids(data, visited.append)
        assert len(visited) == 2

    def test_visit_ids_nested_dict(self):
        """Visits IDs in nested dicts."""
        fid1 = FxtrID.builtin("a")
        fid2 = FxtrID.builtin("b")
        data: FxtrCbor = {
            "ref1": RawWeakFxtrID(inner_id=fid1),
            "nested": {"ref2": RawStrongFxtrID(inner_id=fid2)},
        }
        visited = []
        visit_ids(data, visited.append)
        assert len(visited) == 2

    def test_visit_ids_primitives_ignored(self):
        """Primitives don't trigger callback."""
        data: FxtrCbor = {
            "none": None,
            "bool": True,
            "int": 42,
            "float": 3.14,
            "str": "hello",
            "bytes": b"world",
        }
        visited = []
        visit_ids(data, visited.append)
        assert len(visited) == 0

    def test_visit_ids_mixed_content(self):
        """Visits IDs among primitives."""
        fid = FxtrID.builtin("test")
        data: FxtrCbor = {
            "name": "test",
            "ref": RawWeakFxtrID(inner_id=fid),
            "count": 42,
        }
        visited = []
        visit_ids(data, visited.append)
        assert len(visited) == 1

    def test_visit_ids_empty_structures(self):
        """Empty structures work."""
        visit_ids({}, lambda x: None)  # Should not raise
        visit_ids([], lambda x: None)  # Should not raise


# =============================================================================
# visit_and_project_ids Tests
# =============================================================================


class TestVisitAndProjectIds:
    """Tests for visit_and_project_ids function."""

    def test_project_ids_transforms_weak(self):
        """Weak refs are transformed."""
        fid1 = FxtrID.builtin("old")
        fid2 = FxtrID.builtin("new")
        raw = RawWeakFxtrID(inner_id=fid1)

        def transform(typed_id):
            return fid2

        result = visit_and_project_ids(raw, transform)
        assert isinstance(result, RawWeakFxtrID)
        assert result.inner_id == fid2

    def test_project_ids_transforms_strong(self):
        """Strong refs are transformed."""
        fid1 = FxtrID.builtin("old")
        fid2 = FxtrID.builtin("new")
        raw = RawStrongFxtrID(inner_id=fid1)

        def transform(typed_id):
            return fid2

        result = visit_and_project_ids(raw, transform)
        assert isinstance(result, RawStrongFxtrID)
        assert result.inner_id == fid2

    def test_project_ids_transforms_marker(self):
        """Markers are transformed."""
        fid1 = FxtrID.from_tag_and_payload(".", b"")
        fid2 = FxtrID.builtin("new_ctx")
        raw = RawFxtrEntityMarkerID(inner_id=fid1)

        def transform(typed_id):
            return fid2

        result = visit_and_project_ids(raw, transform)
        assert isinstance(result, RawFxtrEntityMarkerID)
        assert result.inner_id == fid2

    def test_project_ids_preserves_type(self):
        """Transformed ID keeps wrapper type."""
        fid = FxtrID.builtin("test")

        weak = RawWeakFxtrID(inner_id=fid)
        strong = RawStrongFxtrID(inner_id=fid)
        marker = RawFxtrEntityMarkerID(inner_id=fid)

        result_weak = visit_and_project_ids(weak, lambda x: x.inner_id)
        result_strong = visit_and_project_ids(strong, lambda x: x.inner_id)
        result_marker = visit_and_project_ids(marker, lambda x: x.inner_id)

        assert isinstance(result_weak, RawWeakFxtrID)
        assert isinstance(result_strong, RawStrongFxtrID)
        assert isinstance(result_marker, RawFxtrEntityMarkerID)

    def test_project_ids_nested_structures(self):
        """Works in nested lists/dicts."""
        fid1 = FxtrID.builtin("old")
        fid2 = FxtrID.builtin("new")

        data: FxtrCbor = {
            "list": [RawWeakFxtrID(inner_id=fid1)],
            "nested": {"ref": RawStrongFxtrID(inner_id=fid1)},
        }

        result = visit_and_project_ids(data, lambda x: fid2)
        assert result["list"][0].inner_id == fid2  # type: ignore
        assert result["nested"]["ref"].inner_id == fid2  # type: ignore

    def test_project_ids_primitives_unchanged(self):
        """Primitives pass through."""
        assert visit_and_project_ids(None, lambda x: x) is None
        assert visit_and_project_ids(42, lambda x: x) == 42
        assert visit_and_project_ids("test", lambda x: x) == "test"
        assert visit_and_project_ids(b"bytes", lambda x: x) == b"bytes"
        assert visit_and_project_ids(True, lambda x: x) is True
        assert visit_and_project_ids(3.14, lambda x: x) == 3.14

    def test_project_ids_identity_roundtrip(self):
        """Identity function returns equivalent structure."""
        fid = FxtrID.builtin("test")
        ground = FxtrID.from_tag_and_payload(".", b"")
        data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
            "ref": RawWeakFxtrID(inner_id=fid),
            "name": "test",
        }

        result = visit_and_project_ids(data, lambda x: x.inner_id)
        assert result["$fxtr.ctx"].inner_id == ground  # type: ignore
        assert result["ref"].inner_id == fid  # type: ignore
        assert result["name"] == "test"


# =============================================================================
# visit_and_project_ids_in_canonical_cbor_order Tests
# =============================================================================


class TestCanonicalOrder:
    """Tests for visit_and_project_ids_in_canonical_cbor_order."""

    def test_canonical_order_transforms(self):
        """Transforms work correctly in canonical order."""
        fid1 = FxtrID.builtin("old")
        fid2 = FxtrID.builtin("new")

        ground = FxtrID.from_tag_and_payload(".", b"")
        data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
            "ref": RawWeakFxtrID(inner_id=fid1),
        }

        result = visit_and_project_ids_in_canonical_cbor_order(data, lambda x: fid2)
        assert result["ref"].inner_id == fid2  # type: ignore

    def test_canonical_order_preserves_structure(self):
        """Structure preserved after transform."""
        fid = FxtrID.builtin("test")
        ground = FxtrID.from_tag_and_payload(".", b"")

        data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
            "name": "test",
            "count": 42,
            "ref": RawWeakFxtrID(inner_id=fid),
        }

        result = visit_and_project_ids_in_canonical_cbor_order(data, lambda x: x.inner_id)
        assert result["name"] == "test"
        assert result["count"] == 42
        assert "$fxtr.ctx" in result
        assert "ref" in result

    def test_canonical_order_tracks_index(self):
        """Can track index of each ID."""
        fid = FxtrID.builtin("test")
        ground = FxtrID.from_tag_and_payload(".", b"")

        data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
            "refs": [
                RawWeakFxtrID(inner_id=fid),
                RawWeakFxtrID(inner_id=fid),
                RawWeakFxtrID(inner_id=fid),
            ],
        }

        indices = []

        def track_and_return(typed_id):
            indices.append(len(indices))
            return typed_id.inner_id

        visit_and_project_ids_in_canonical_cbor_order(data, track_and_return)
        # Should have visited 4 IDs: ctx + 3 refs
        assert len(indices) == 4


# =============================================================================
# async_visit_and_project_ids_in_canonical_cbor_order Tests
# =============================================================================


class TestAsyncCanonicalOrder:
    """Tests for async_visit_and_project_ids_in_canonical_cbor_order."""

    @pytest.mark.anyio
    async def test_async_canonical_order(self):
        """Async version works."""
        fid1 = FxtrID.builtin("old")
        fid2 = FxtrID.builtin("new")
        ground = FxtrID.from_tag_and_payload(".", b"")

        data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
            "ref": RawWeakFxtrID(inner_id=fid1),
        }

        async def transform(typed_id):
            return fid2

        result = await async_visit_and_project_ids_in_canonical_cbor_order(data, transform)
        assert result["ref"].inner_id == fid2  # type: ignore

    @pytest.mark.anyio
    async def test_async_callback_awaited(self):
        """Async callback is awaited."""
        fid = FxtrID.builtin("test")
        ground = FxtrID.from_tag_and_payload(".", b"")
        call_count = 0

        data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
            "ref": RawWeakFxtrID(inner_id=fid),
        }

        async def counting_transform(typed_id):
            nonlocal call_count
            call_count += 1
            return typed_id.inner_id

        await async_visit_and_project_ids_in_canonical_cbor_order(data, counting_transform)
        assert call_count == 2  # ctx + ref

    @pytest.mark.anyio
    async def test_async_order_preserved(self):
        """Order preserved with async callback."""
        ground = FxtrID.from_tag_and_payload(".", b"")
        fid_a = FxtrID.builtin("a")
        fid_b = FxtrID.builtin("b")
        fid_c = FxtrID.builtin("c")

        data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
            "refs": [
                RawWeakFxtrID(inner_id=fid_a),
                RawWeakFxtrID(inner_id=fid_b),
                RawWeakFxtrID(inner_id=fid_c),
            ],
        }

        order = []

        async def track_order(typed_id):
            order.append(typed_id.inner_id.local_id_string())
            return typed_id.inner_id

        await async_visit_and_project_ids_in_canonical_cbor_order(data, track_order)
        # Should be in CBOR canonical order
        assert len(order) == 4


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Edge case tests for arena_util functions."""

    def test_empty_dict(self):
        """Empty dict works."""
        result = visit_and_project_ids({}, lambda x: x.inner_id)
        assert result == {}

    def test_empty_list(self):
        """Empty list works."""
        result = visit_and_project_ids([], lambda x: x.inner_id)
        assert result == []

    def test_deeply_nested(self):
        """Deeply nested structures work."""
        fid = FxtrID.builtin("deep")

        # Build deeply nested structure
        data: FxtrCbor = {"level0": {"level1": {"level2": {"level3": RawWeakFxtrID(inner_id=fid)}}}}

        visited = []
        visit_ids(data, visited.append)
        assert len(visited) == 1
        assert visited[0].inner_id == fid

    def test_multiple_refs_same_id(self):
        """Multiple refs to same ID all transformed."""
        fid = FxtrID.builtin("shared")
        new_fid = FxtrID.builtin("new")

        data: FxtrCbor = [
            RawWeakFxtrID(inner_id=fid),
            RawWeakFxtrID(inner_id=fid),
            RawStrongFxtrID(inner_id=fid),
        ]

        result = visit_and_project_ids(data, lambda x: new_fid)
        assert all(r.inner_id == new_fid for r in result)  # type: ignore

    def test_list_with_primitives_and_refs(self):
        """List mixing primitives and refs works."""
        fid = FxtrID.builtin("test")

        data: FxtrCbor = [1, "two", RawWeakFxtrID(inner_id=fid), None, True]

        visited = []
        visit_ids(data, visited.append)
        assert len(visited) == 1

        result = visit_and_project_ids(data, lambda x: x.inner_id)
        assert result[0] == 1
        assert result[1] == "two"
        assert isinstance(result[2], RawWeakFxtrID)
        assert result[3] is None
        assert result[4] is True
