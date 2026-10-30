"""Tests for fxtr2/legacy_compat/data.py - v1 entity compatibility layer."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fxtr.object_model import core as v1_core

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_serdes import EntityRef, store_entity
from fxtr2.data.fxtr_scope import ACTIVE_SCOPE, FxtrScope
from fxtr2.legacy_compat import (
    LEGACY_V1_TYPE_ID,
    LegacyCompat,
    walk_and_collect_cids,
    walk_and_collect_fxtr_ids,
    walk_and_replace_cids,
)

# =============================================================================
# V1 Test Entity Classes
# Note: v1's DataEntityBase uses a metaclass that handles dataclass transformation,
# so we don't use @dataclasses.dataclass decorator on these.
# =============================================================================


class SimpleV1Entity(v1_core.DataEntityBase, type_id="SimpleV1^1", namespace_id="test.legacy"):
    """A simple v1 test entity with no children."""

    name: str
    value: int


class V1EntityWithChild(
    v1_core.DataEntityBase, type_id="V1WithChild^1", namespace_id="test.legacy"
):
    """A v1 entity that references another v1 entity."""

    name: str
    child: SimpleV1Entity


class V1EntityWithOptionalChild(
    v1_core.DataEntityBase, type_id="V1WithOptChild^1", namespace_id="test.legacy"
):
    """A v1 entity with an optional reference."""

    name: str
    child: SimpleV1Entity | None


class V1EntityWithList(v1_core.DataEntityBase, type_id="V1WithList^1", namespace_id="test.legacy"):
    """A v1 entity with a list of children."""

    name: str
    children: tuple[SimpleV1Entity, ...]


class NestedV1Entity(v1_core.DataEntityBase, type_id="NestedV1^1", namespace_id="test.legacy"):
    """A v1 entity with nested child references."""

    name: str
    inner: V1EntityWithChild


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
async def compat_scope() -> AsyncIterator[FxtrScope]:
    """Scope with legacy compat type pre-tracked and set as active."""
    scope = FxtrScope(isolated=True)
    scope._start_as_root_scope()
    scope.track_builtin(LEGACY_V1_TYPE_ID)
    token = ACTIVE_SCOPE.set(scope)
    try:
        yield scope
    finally:
        ACTIVE_SCOPE.reset(token)


# =============================================================================
# Helper Tests
# =============================================================================


class TestCborTraversalHelpers:
    """Tests for CBOR traversal helper functions."""

    def test_walk_and_collect_cids_empty(self) -> None:
        """Empty structures have no CIDs."""
        assert walk_and_collect_cids({}) == []
        assert walk_and_collect_cids([]) == []
        assert walk_and_collect_cids("test") == []

    def test_walk_and_collect_cids_nested(self) -> None:
        """CIDs in nested structures are collected."""
        # Create a simple v1 entity to get a real CID
        entity = SimpleV1Entity(name="test", value=42)
        cid = v1_core.entity_cid(entity)

        data = {
            "key1": cid,
            "key2": {"nested": cid},
            "key3": [cid, "not_a_cid", cid],
        }
        cids = walk_and_collect_cids(data)
        assert len(cids) == 4  # cid appears 4 times
        assert all(c == cid for c in cids)

    def test_walk_and_replace_cids(self) -> None:
        """CIDs are replaced with FxtrIDs."""
        entity = SimpleV1Entity(name="test", value=42)
        cid = v1_core.entity_cid(entity)
        fxtr_id = FxtrID.from_tag_and_payload("d", b"test_payload")

        data = {"ref": cid, "nested": {"inner_ref": cid}}
        mapping = {cid: fxtr_id}
        result = walk_and_replace_cids(data, mapping)

        assert result["ref"].inner_id == fxtr_id
        assert result["nested"]["inner_ref"].inner_id == fxtr_id

    def test_walk_and_collect_fxtr_ids(self) -> None:
        """FxtrIDs in structures are collected."""
        from fxtr2.data.entity_core import RawStrongFxtrID, RawWeakFxtrID

        fxtr_id1 = FxtrID.from_tag_and_payload("d", b"payload1")
        fxtr_id2 = FxtrID.from_tag_and_payload("d", b"payload2")

        data = {
            "strong": RawStrongFxtrID(inner_id=fxtr_id1),
            "weak": RawWeakFxtrID(inner_id=fxtr_id2),
            "nested": [RawStrongFxtrID(inner_id=fxtr_id1)],
        }
        ids = walk_and_collect_fxtr_ids(data)
        assert len(ids) == 3
        assert ids[0] == fxtr_id1
        assert ids[1] == fxtr_id2
        assert ids[2] == fxtr_id1


# =============================================================================
# LegacyCompat Tests
# =============================================================================


class TestLegacyCompatBasic:
    """Basic tests for LegacyCompat wrapper."""

    def test_wrap_v1_entity(self) -> None:
        """Can wrap a v1 entity."""
        v1_entity = SimpleV1Entity(name="test", value=42)
        compat = LegacyCompat(v1_entity)

        assert compat.value is v1_entity
        assert compat.legacy_cid == v1_core.entity_cid(v1_entity)

    def test_wrap_non_entity_fails(self) -> None:
        """Wrapping a non-entity raises TypeError."""
        with pytest.raises(TypeError, match="LegacyCompat requires a v1 EntityBase"):
            LegacyCompat("not an entity")  # type: ignore

    def test_equality(self) -> None:
        """LegacyCompat equality is based on CID."""
        v1_entity = SimpleV1Entity(name="test", value=42)
        compat1 = LegacyCompat(v1_entity)
        compat2 = LegacyCompat(v1_entity)

        assert compat1 == compat2
        assert hash(compat1) == hash(compat2)

    def test_repr(self) -> None:
        """LegacyCompat has a useful repr."""
        v1_entity = SimpleV1Entity(name="test", value=42)
        compat = LegacyCompat(v1_entity)
        assert "LegacyCompat" in repr(compat)


class TestLegacyCompatRoundtrip:
    """Tests for storing and loading LegacyCompat entities."""

    @pytest.mark.anyio
    async def test_simple_entity_roundtrip(self, compat_scope: FxtrScope) -> None:
        """A simple v1 entity can be stored and loaded."""
        v1_entity = SimpleV1Entity(name="hello", value=123)
        compat = LegacyCompat(v1_entity)

        # Store
        ref = store_entity(compat)
        assert isinstance(ref, EntityRef)

        # Load
        loaded = await ref.load(compat_scope)
        assert isinstance(loaded, LegacyCompat)
        assert isinstance(loaded.value, SimpleV1Entity)
        assert loaded.value.name == "hello"
        assert loaded.value.value == 123
        assert loaded.legacy_cid == v1_core.entity_cid(v1_entity)

    @pytest.mark.anyio
    async def test_entity_with_child_roundtrip(self, compat_scope: FxtrScope) -> None:
        """A v1 entity with a child reference can be stored and loaded."""
        child = SimpleV1Entity(name="child", value=1)
        parent = V1EntityWithChild(name="parent", child=child)
        compat = LegacyCompat(parent)

        # Store
        ref = store_entity(compat)

        # Load
        loaded = await ref.load(compat_scope)
        assert isinstance(loaded, LegacyCompat)
        assert isinstance(loaded.value, V1EntityWithChild)
        assert loaded.value.name == "parent"
        assert loaded.value.child.name == "child"
        assert loaded.value.child.value == 1

    @pytest.mark.anyio
    async def test_entity_with_optional_child_none(self, compat_scope: FxtrScope) -> None:
        """A v1 entity with None optional child works."""
        entity = V1EntityWithOptionalChild(name="test", child=None)
        compat = LegacyCompat(entity)

        ref = store_entity(compat)
        loaded = await ref.load(compat_scope)

        assert isinstance(loaded.value, V1EntityWithOptionalChild)
        assert loaded.value.child is None

    @pytest.mark.anyio
    async def test_entity_with_optional_child_present(self, compat_scope: FxtrScope) -> None:
        """A v1 entity with present optional child works."""
        child = SimpleV1Entity(name="child", value=99)
        entity = V1EntityWithOptionalChild(name="test", child=child)
        compat = LegacyCompat(entity)

        ref = store_entity(compat)
        loaded = await ref.load(compat_scope)

        assert isinstance(loaded.value, V1EntityWithOptionalChild)
        assert loaded.value.child is not None
        assert loaded.value.child.name == "child"

    @pytest.mark.anyio
    async def test_entity_with_list_of_children(self, compat_scope: FxtrScope) -> None:
        """A v1 entity with a list of children works."""
        children = (
            SimpleV1Entity(name="child1", value=1),
            SimpleV1Entity(name="child2", value=2),
            SimpleV1Entity(name="child3", value=3),
        )
        entity = V1EntityWithList(name="parent", children=children)
        compat = LegacyCompat(entity)

        ref = store_entity(compat)
        loaded = await ref.load(compat_scope)

        assert isinstance(loaded.value, V1EntityWithList)
        assert len(loaded.value.children) == 3
        assert loaded.value.children[0].name == "child1"
        assert loaded.value.children[1].name == "child2"
        assert loaded.value.children[2].name == "child3"

    @pytest.mark.anyio
    async def test_nested_entities_roundtrip(self, compat_scope: FxtrScope) -> None:
        """Deeply nested v1 entities work."""
        leaf = SimpleV1Entity(name="leaf", value=42)
        inner = V1EntityWithChild(name="inner", child=leaf)
        outer = NestedV1Entity(name="outer", inner=inner)
        compat = LegacyCompat(outer)

        ref = store_entity(compat)
        loaded = await ref.load(compat_scope)

        assert isinstance(loaded.value, NestedV1Entity)
        assert loaded.value.name == "outer"
        assert loaded.value.inner.name == "inner"
        assert loaded.value.inner.child.name == "leaf"
        assert loaded.value.inner.child.value == 42

    @pytest.mark.anyio
    async def test_shared_children(self, compat_scope: FxtrScope) -> None:
        """Shared children (same CID) are handled correctly."""
        shared = SimpleV1Entity(name="shared", value=999)
        # Create two parents that reference the same child
        parent1 = V1EntityWithChild(name="parent1", child=shared)
        parent2 = V1EntityWithChild(name="parent2", child=shared)

        # Store both
        ref1 = store_entity(LegacyCompat(parent1))
        ref2 = store_entity(LegacyCompat(parent2))

        # Load both
        loaded1 = await ref1.load(compat_scope)
        loaded2 = await ref2.load(compat_scope)

        # Both should have children with same CID
        assert v1_core.entity_cid(loaded1.value.child) == v1_core.entity_cid(loaded2.value.child)


class TestLegacyCompatCidVerification:
    """Tests for CID integrity verification."""

    @pytest.mark.anyio
    async def test_cid_preserved(self, compat_scope: FxtrScope) -> None:
        """The original v1 CID is preserved through roundtrip."""
        v1_entity = SimpleV1Entity(name="test", value=42)
        original_cid = v1_core.entity_cid(v1_entity)

        compat = LegacyCompat(v1_entity)
        ref = store_entity(compat)
        loaded = await ref.load(compat_scope)

        assert loaded.legacy_cid == original_cid
        assert v1_core.entity_cid(loaded.value) == original_cid
