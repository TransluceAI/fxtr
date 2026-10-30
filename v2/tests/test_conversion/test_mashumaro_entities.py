"""Tests for mashumaro-based DataclassEntity serialization/deserialization.

Extracted from test_entity_serdes.py and test_entity_ref_scope.py.
Tests the core DataclassEntity functionality including:
- Basic serialization and deserialization
- Type discrimination
- Complex field types (refs, optionals, lists)
- Type parameter preservation
- Inline entities (Child[T], LinkedEntity[T])
- Collection types (set, frozenset, dict, frozendict)
- Canonical ordering and content addressing
"""

from __future__ import annotations

import dataclasses
import typing
from collections.abc import AsyncIterator
from typing import Any

import pytest
from frozendict import frozendict
from mashumaro.exceptions import ExtraKeysError

from fxtr2.conversion.converters.mashumaro_entities import (
    Child,
    ChildRef,
    DataclassEntity,
    DataclassEntityLoadHandler,
    DeferredLoadPlaceholder,
    LinkedEntity,
)
from fxtr2.conversion.converters.unknown_entity import LoadAsUnknownByDefault, UnknownEntity
from fxtr2.data.entity_core import (
    MISSING_FXTR_CTX_SENTINEL,
    DuplicateEntityMarkerError,
    FrozenFxtrCbor,
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawFxtrEntityMissingMarkerSentinel,
    RawStrongFxtrID,
    RawWeakFxtrID,
    freeze_fxtr_cbor,
)
from fxtr2.data.entity_ref_scope import Entity, EntityRef, FxtrScope

# =============================================================================
# Helper Functions
# =============================================================================

_dc_handler = DataclassEntityLoadHandler()


def _to_cbor(entity: DataclassEntity) -> FxtrCbor:
    """Serialize a DataclassEntity to its storage-bound CBOR form.

    Routes through ``_fxtr_convert_to_cbor`` (not the raw mashumaro encoder) so
    the result includes the framework-managed ``$fxtr.type`` / ``$fxtr.ctx``
    keys, which mashumaro itself no longer emits (they are flagged
    ``serialize:"omit"`` and injected on the storage path). Requires an active
    scope — the test scope fixtures activate themselves for the duration of the
    test, and the ``$fxtr.ctx`` marker is taken from that scope unless the entity
    was constructed with an explicit ``fxtr_ctx``.
    """
    return entity._fxtr_convert_to_cbor()


async def _from_cbor(cbor: FxtrCbor, target_type: type) -> DataclassEntity:
    """Deserialize CBOR to a DataclassEntity via the conversion handler."""
    result = await _dc_handler.from_lowlevel_entity_data(cbor, target_type)
    return result.value


# =============================================================================
# Basic Test Entity Definitions
# =============================================================================


@dataclasses.dataclass(frozen=True)
class SimpleEntity(DataclassEntity, fxtr_type=FxtrID.builtin("test_simple")):
    """A simple test entity."""

    name: str
    value: int


@dataclasses.dataclass(frozen=True)
class EntityWithRef(DataclassEntity, fxtr_type=FxtrID.builtin("test_with_ref")):
    """An entity with a reference to another entity."""

    name: str
    child: ChildRef[SimpleEntity]


@dataclasses.dataclass(frozen=True)
class EntityWithOptionalRef(DataclassEntity, fxtr_type=FxtrID.builtin("test_opt_ref")):
    """An entity with an optional reference."""

    name: str
    child: ChildRef[SimpleEntity] | None = None


@dataclasses.dataclass(frozen=True)
class EntityWithListOfRefs(DataclassEntity, fxtr_type=FxtrID.builtin("test_list_refs")):
    """An entity with a list of references."""

    name: str
    children: list[ChildRef[SimpleEntity]]


@dataclasses.dataclass(frozen=True)
class EntityWithBytes(DataclassEntity, fxtr_type=FxtrID.builtin("test_bytes")):
    """An entity with binary data."""

    name: str
    data: bytes


@dataclasses.dataclass(frozen=True)
class EntityWithDict(DataclassEntity, fxtr_type=FxtrID.builtin("test_dict")):
    """An entity with a dict field."""

    name: str
    metadata: dict[str, Any]


def track_test_entity_types(scope: FxtrScope) -> None:
    """Track all builtin types used by test entities."""
    scope.track_builtin(FxtrID.builtin("test_simple"))
    scope.track_builtin(FxtrID.builtin("test_with_ref"))
    scope.track_builtin(FxtrID.builtin("test_opt_ref"))
    scope.track_builtin(FxtrID.builtin("test_list_refs"))
    scope.track_builtin(FxtrID.builtin("test_bytes"))
    scope.track_builtin(FxtrID.builtin("test_dict"))
    scope.track_builtin(FxtrID.builtin("test_primitive"))
    scope.track_builtin(FxtrID.builtin("test_list"))
    scope.track_builtin(FxtrID.builtin("test_caching_entity_c"))
    scope.track_builtin(FxtrID.builtin("test_caching_entity_b"))
    scope.track_builtin(FxtrID.builtin("test_caching_entity_a"))


@pytest.fixture
async def serdes_scope() -> AsyncIterator[FxtrScope]:
    """Scope with test entity types pre-tracked and set as active."""
    async with FxtrScope(name="serdes_test") as scope:
        track_test_entity_types(scope)
        yield scope


# =============================================================================
# Type Parameter Test Entity Definitions
# =============================================================================


@dataclasses.dataclass(frozen=True)
class TypeA(DataclassEntity, fxtr_type=FxtrID.builtin("test_type_a")):
    """First type in a union."""

    a_field: str


@dataclasses.dataclass(frozen=True)
class TypeB(DataclassEntity, fxtr_type=FxtrID.builtin("test_type_b")):
    """Second type in a union."""

    b_field: int


@dataclasses.dataclass(frozen=True)
class TypeC(DataclassEntity, fxtr_type=FxtrID.builtin("test_type_c")):
    """Third type in a union."""

    c_field: float


@dataclasses.dataclass(frozen=True)
class WrapperWithRef[T: Entity](DataclassEntity, fxtr_type=FxtrID.builtin("test_wrapper_ref")):
    """A wrapper containing an EntityRef whose type is parameterized."""

    name: str
    ref: ChildRef[T]


@dataclasses.dataclass(frozen=True)
class WrapperWithRefAndFallback[T: LoadAsUnknownByDefault[Entity]](
    DataclassEntity, fxtr_type=FxtrID.builtin("test_wrapper_ref")
):
    """A wrapper containing an EntityRef whose type is parameterized and annotated with a fallback."""

    name: str
    ref: ChildRef[T]


@dataclasses.dataclass(frozen=True)
class WrapperWithMagicInferredRef[T: Entity](
    DataclassEntity, fxtr_type=FxtrID.builtin("test_wrapper_ref")
):
    """A wrapper containing an EntityRef that infers its roundtrip type while storing."""

    name: str
    ref: ChildRef[T]

    def _fxtr_store_impl(self) -> EntityRef[WrapperWithMagicInferredRef[T]]:
        stored = super()._fxtr_store_impl()
        # Infer our own value of T from our ref's target type annotation and propagate it
        inferred_T = self.ref.target_type
        return stored.replace_target_type(WrapperWithMagicInferredRef[inferred_T])


@dataclasses.dataclass(frozen=True)
class EntityWithUnionRef(DataclassEntity, fxtr_type=FxtrID.builtin("test_union_ref")):
    """An entity containing an EntityRef to a union of types."""

    name: str
    child: ChildRef[TypeA | TypeB]


def track_type_param_test_types(scope: FxtrScope) -> None:
    """Track builtin types used by type parameter preservation tests."""
    scope.track_builtin(FxtrID.builtin("test_type_a"))
    scope.track_builtin(FxtrID.builtin("test_type_b"))
    scope.track_builtin(FxtrID.builtin("test_type_c"))
    scope.track_builtin(FxtrID.builtin("test_wrapper_ref"))
    scope.track_builtin(FxtrID.builtin("test_union_ref"))
    scope.track_builtin(FxtrID.builtin("test_custom_entity"))


@pytest.fixture
async def type_param_scope() -> AsyncIterator[FxtrScope]:
    """Scope with type parameter test entity types pre-tracked."""
    async with FxtrScope(name="type_param_test") as scope:
        track_test_entity_types(scope)
        track_type_param_test_types(scope)
        yield scope


# =============================================================================
# Inline Entity Test Entity Definitions
# =============================================================================


@dataclasses.dataclass(frozen=True)
class LeafEntity(DataclassEntity, fxtr_type=FxtrID.builtin("test_leaf_entity")):
    """A simple leaf entity for inline tests."""

    value: int


@dataclasses.dataclass(frozen=True)
class ParentWithChild(DataclassEntity, fxtr_type=FxtrID.builtin("test_parent_with_child")):
    """A parent entity with a Child[T] field."""

    name: str
    child: Child[LeafEntity]


@dataclasses.dataclass(frozen=True)
class ParentWithLinkedEntity(DataclassEntity, fxtr_type=FxtrID.builtin("test_parent_with_linked")):
    """A parent entity with a LinkedEntity[T] field."""

    name: str
    linked: LinkedEntity[LeafEntity]


@dataclasses.dataclass(frozen=True)
class ParentWithMixedRefs(DataclassEntity, fxtr_type=FxtrID.builtin("test_parent_with_mixed")):
    """A parent entity with both inline and lazy refs."""

    name: str
    child: Child[LeafEntity]  # Auto-loaded
    lazy: ChildRef[LeafEntity]  # Must call .load()


@dataclasses.dataclass(frozen=True)
class DeepNestingParent(DataclassEntity, fxtr_type=FxtrID.builtin("test_deep_nesting")):
    """A parent entity that contains another parent with a child."""

    name: str
    nested: Child[ParentWithChild]


@dataclasses.dataclass(frozen=True)
class BareNestedInner(DataclassEntity, fxtr_type=FxtrID.builtin("test_bare_nested_inner")):
    """Inner entity for testing bare nesting (without Child[T])."""

    foo: str


@dataclasses.dataclass(frozen=True)
class BareNestedOuter(DataclassEntity, fxtr_type=FxtrID.builtin("test_bare_nested_outer")):
    """Outer entity that directly nests another entity without Child[T] wrapper."""

    bar: BareNestedInner
    baz: BareNestedInner


def track_inline_entity_types(scope: FxtrScope) -> None:
    """Track builtin types used by inline entity tests."""
    scope.track_builtin(FxtrID.builtin("test_leaf_entity"))
    scope.track_builtin(FxtrID.builtin("test_parent_with_child"))
    scope.track_builtin(FxtrID.builtin("test_parent_with_linked"))
    scope.track_builtin(FxtrID.builtin("test_parent_with_mixed"))
    scope.track_builtin(FxtrID.builtin("test_deep_nesting"))


@pytest.fixture
async def inline_entity_scope() -> AsyncIterator[FxtrScope]:
    """Scope with inline entity test types pre-tracked."""
    async with FxtrScope(name="inline_entity_test") as scope:
        track_test_entity_types(scope)
        track_type_param_test_types(scope)
        track_inline_entity_types(scope)
        yield scope


# =============================================================================
# Collection Type Test Entity Definitions
# =============================================================================


@dataclasses.dataclass(frozen=True)
class EntityWithSet(DataclassEntity, fxtr_type=FxtrID.builtin("test_set")):
    """An entity with a set field."""

    name: str
    items: set[str]


@dataclasses.dataclass(frozen=True)
class EntityWithFrozenset(DataclassEntity, fxtr_type=FxtrID.builtin("test_frozenset")):
    """An entity with a frozenset field."""

    name: str
    items: frozenset[str]


@dataclasses.dataclass(frozen=True)
class EntityWithFrozendict(DataclassEntity, fxtr_type=FxtrID.builtin("test_frozendict")):
    """An entity with a frozendict field."""

    name: str
    mapping: frozendict[str, int]


@dataclasses.dataclass(frozen=True)
class EntityWithRefSet(DataclassEntity, fxtr_type=FxtrID.builtin("test_ref_set")):
    """An entity with a set of EntityRefs."""

    name: str
    refs: set[EntityRef[SimpleEntity]]


@dataclasses.dataclass(frozen=True)
class EntityWithRefFrozenset(DataclassEntity, fxtr_type=FxtrID.builtin("test_ref_frozenset")):
    """An entity with a frozenset of EntityRefs."""

    name: str
    refs: frozenset[EntityRef[SimpleEntity]]


@dataclasses.dataclass(frozen=True)
class EntityWithFxtrIDSet(DataclassEntity, fxtr_type=FxtrID.builtin("test_fxtrid_set")):
    """An entity with a set of FxtrIDs."""

    name: str
    ids: set[FxtrID]


@dataclasses.dataclass(frozen=True)
class EntityWithFxtrIDFrozenset(DataclassEntity, fxtr_type=FxtrID.builtin("test_fxtrid_frozenset")):
    """An entity with a frozenset of FxtrIDs."""

    name: str
    ids: frozenset[FxtrID]


@dataclasses.dataclass(frozen=True)
class EntityWithRefKeyDict(DataclassEntity, fxtr_type=FxtrID.builtin("test_ref_key_dict")):
    """An entity with EntityRef keys in a dict."""

    name: str
    mapping: dict[EntityRef[SimpleEntity], str]


@dataclasses.dataclass(frozen=True)
class EntityWithRefKeyFrozendict(
    DataclassEntity, fxtr_type=FxtrID.builtin("test_ref_key_frozendict")
):
    """An entity with EntityRef keys in a frozendict."""

    name: str
    mapping: frozendict[EntityRef[SimpleEntity], str]


@dataclasses.dataclass(frozen=True)
class EntityWithFxtrIDKeyDict(DataclassEntity, fxtr_type=FxtrID.builtin("test_fxtrid_key_dict")):
    """An entity with FxtrID keys in a dict."""

    name: str
    mapping: dict[FxtrID, str]


@dataclasses.dataclass(frozen=True)
class EntityWithNestedFrozen(DataclassEntity, fxtr_type=FxtrID.builtin("test_nested_frozen")):
    """An entity with nested frozen collections."""

    name: str
    mapping: frozendict[str, frozenset[int]]


@dataclasses.dataclass(frozen=True)
class EntityWithFrozenCbor(DataclassEntity, fxtr_type=FxtrID.builtin("test_frozen_cbor")):
    """An entity whose body is an opaque FrozenFxtrCbor tree.

    Exercises the global FrozenFxtrCbor serialization strategy on
    DataclassEntity.Config — used by report entities to hold
    MDX-compiled trees with embedded entity refs.
    """

    name: str
    body: FrozenFxtrCbor


def track_collection_test_types(scope: FxtrScope) -> None:
    """Track builtin types used by collection tests."""
    scope.track_builtin(FxtrID.builtin("test_set"))
    scope.track_builtin(FxtrID.builtin("test_frozenset"))
    scope.track_builtin(FxtrID.builtin("test_frozendict"))
    scope.track_builtin(FxtrID.builtin("test_ref_set"))
    scope.track_builtin(FxtrID.builtin("test_ref_frozenset"))
    scope.track_builtin(FxtrID.builtin("test_fxtrid_set"))
    scope.track_builtin(FxtrID.builtin("test_fxtrid_frozenset"))
    scope.track_builtin(FxtrID.builtin("test_ref_key_dict"))
    scope.track_builtin(FxtrID.builtin("test_ref_key_frozendict"))
    scope.track_builtin(FxtrID.builtin("test_fxtrid_key_dict"))
    scope.track_builtin(FxtrID.builtin("test_nested_frozen"))
    scope.track_builtin(FxtrID.builtin("test_frozen_cbor"))


@pytest.fixture
async def collection_scope() -> AsyncIterator[FxtrScope]:
    """Scope with collection test entity types pre-tracked."""
    async with FxtrScope(name="collection_test") as scope:
        track_test_entity_types(scope)
        track_collection_test_types(scope)
        yield scope


# =============================================================================
# Tests: DataclassEntity Basics
# =============================================================================


class TestDataclassEntityBasics:
    """Tests for DataclassEntity basic functionality."""

    @pytest.mark.anyio
    async def test_dataclass_entity_creation(self, serdes_scope: FxtrScope):
        """Can create DataclassEntity subclass."""
        entity = SimpleEntity(name="test", value=42)
        assert entity.name == "test"
        assert entity.value == 42

    @pytest.mark.anyio
    async def test_dataclass_entity_frozen(self, serdes_scope: FxtrScope):
        """Entity is frozen (immutable)."""
        entity = SimpleEntity(name="test", value=42)
        with pytest.raises(dataclasses.FrozenInstanceError):
            entity.name = "modified"  # type: ignore

    @pytest.mark.anyio
    async def test_dataclass_entity_type_registration(self, serdes_scope: FxtrScope):
        """Type is registered for deserialization."""
        entity = SimpleEntity(name="test", value=42)
        assert entity._fxtr_known_type_id == FxtrID.builtin("test_simple")

    @pytest.mark.anyio
    async def test_dataclass_entity_fields(self, serdes_scope: FxtrScope):
        """Fields are preserved in serialization."""
        entity = SimpleEntity(name="test_name", value=123)
        cbor = typing.cast(Any, _to_cbor(entity))
        assert cbor["name"] == "test_name"
        assert cbor["value"] == 123

    @pytest.mark.anyio
    async def test_dataclass_entity_has_fxtr_type(self, serdes_scope: FxtrScope):
        """Entity has fxtr_type attribute."""
        entity = SimpleEntity(name="test", value=42)
        assert entity.fxtr_type == FxtrID.builtin("test_simple")


# =============================================================================
# Tests: Entity Serialization
# =============================================================================


class TestEntitySerialization:
    """Tests for entity serialization to FxtrCbor."""

    @pytest.mark.anyio
    async def test_to_fxtr_cbor_basic(self, serdes_scope: FxtrScope):
        """Simple entity serializes to FxtrCbor."""
        entity = SimpleEntity(name="test", value=42)
        cbor = typing.cast(Any, _to_cbor(entity))
        assert isinstance(cbor, dict)

    @pytest.mark.anyio
    async def test_to_fxtr_cbor_has_ctx(self, serdes_scope: FxtrScope):
        """Result has $fxtr.ctx field when explicitly set on the entity.

        Note: as of stage 9, ``fxtr_ctx`` defaults to ``None`` and is
        captured at store-time (inside ``_fxtr_store_impl``) rather than
        at construction time. The direct mashumaro encoder used by
        ``_to_cbor`` does not run that injection, so we set the marker
        explicitly here.
        """
        marker = RawFxtrEntityMarkerID(inner_id=FxtrID.from_tag_and_payload(".", b""))
        entity = SimpleEntity(name="test", value=42, fxtr_ctx=marker)
        cbor = typing.cast(Any, _to_cbor(entity))
        assert "$fxtr.ctx" in cbor
        assert isinstance(cbor["$fxtr.ctx"], RawFxtrEntityMarkerID)

    @pytest.mark.anyio
    async def test_to_fxtr_cbor_has_type(self, serdes_scope: FxtrScope):
        """Result has $fxtr.type field."""
        entity = SimpleEntity(name="test", value=42)
        cbor = typing.cast(Any, _to_cbor(entity))
        assert "$fxtr.type" in cbor
        assert isinstance(cbor["$fxtr.type"], (RawWeakFxtrID, RawStrongFxtrID))
        assert cbor["$fxtr.type"].inner_id == FxtrID.builtin("test_simple")

    @pytest.mark.anyio
    async def test_to_fxtr_cbor_primitive_fields(self, serdes_scope: FxtrScope):
        """int, str, bool, float fields serialize correctly."""

        @dataclasses.dataclass(frozen=True)
        class PrimitiveEntity(DataclassEntity, fxtr_type=FxtrID.builtin("test_primitive")):
            int_field: int
            str_field: str
            bool_field: bool
            float_field: float

        entity = PrimitiveEntity(int_field=42, str_field="hello", bool_field=True, float_field=3.14)
        cbor = typing.cast(Any, _to_cbor(entity))
        assert cbor["int_field"] == 42
        assert cbor["str_field"] == "hello"
        assert cbor["bool_field"] is True
        assert cbor["float_field"] == 3.14

    @pytest.mark.anyio
    async def test_to_fxtr_cbor_list_fields(self, serdes_scope: FxtrScope):
        """List fields serialize correctly."""

        @dataclasses.dataclass(frozen=True)
        class ListEntity(DataclassEntity, fxtr_type=FxtrID.builtin("test_list")):
            items: list[int]

        entity = ListEntity(items=[1, 2, 3])
        cbor = typing.cast(Any, _to_cbor(entity))
        assert cbor["items"] == [1, 2, 3]

    @pytest.mark.anyio
    async def test_to_fxtr_cbor_dict_fields(self, serdes_scope: FxtrScope):
        """Dict fields serialize correctly."""
        entity = EntityWithDict(name="test", metadata={"key": "value", "number": 42})
        cbor = typing.cast(Any, _to_cbor(entity))
        assert cbor["metadata"] == {"key": "value", "number": 42}

    @pytest.mark.anyio
    async def test_to_fxtr_cbor_bytes_field(self, serdes_scope: FxtrScope):
        """Bytes fields pass through unchanged (CBOR has a native byte-string
        type and the workstation's wire_escape tags raw bytes as
        ``["bytes", "<base64>"]``; the default mashumaro behavior of
        base64-encoding to ``str`` would lose the bytes identity through
        both layers)."""
        entity = EntityWithBytes(name="test", data=b"\x00\x01\x02\xff")
        cbor = typing.cast(Any, _to_cbor(entity))
        assert cbor["data"] == b"\x00\x01\x02\xff"


# =============================================================================
# Tests: Entity Deserialization
# =============================================================================


class TestEntityDeserialization:
    """Tests for entity deserialization from FxtrCbor."""

    @pytest.mark.anyio
    async def test_from_fxtr_cbor_basic(self, serdes_scope: FxtrScope):
        """Deserialize simple entity."""
        cbor: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=FxtrID.from_tag_and_payload(".", b"")),
            "$fxtr.type": RawWeakFxtrID(inner_id=FxtrID.builtin("test_simple")),
            "name": "test",
            "value": 42,
        }
        entity = await _from_cbor(cbor, SimpleEntity)
        assert entity.name == "test"
        assert entity.value == 42

    @pytest.mark.anyio
    async def test_from_fxtr_cbor_roundtrip(self, serdes_scope: FxtrScope):
        """to_fxtr_cbor -> from_fxtr_cbor roundtrips.

        ``fxtr_ctx`` is supplied explicitly because the direct mashumaro
        encoder doesn't run the store-time injection (see
        ``test_to_fxtr_cbor_has_ctx``).
        """
        marker = RawFxtrEntityMarkerID(inner_id=FxtrID.from_tag_and_payload(".", b""))
        original = SimpleEntity(name="roundtrip", value=100, fxtr_ctx=marker)
        cbor = _to_cbor(original)
        restored = await _from_cbor(cbor, SimpleEntity)
        assert restored.name == original.name
        assert restored.value == original.value


# =============================================================================
# Tests: fxtr_ctx capture-at-store-time (Stage 9 / Stage 1)
# =============================================================================


class TestFxtrCtxCaptureAtStore:
    """``fxtr_ctx`` defaults to a sentinel at construction; replaced at store time."""

    @pytest.mark.anyio
    async def test_default_fxtr_ctx_is_sentinel(self, serdes_scope: FxtrScope):
        """Newly-constructed entities have ``fxtr_ctx = MISSING_FXTR_CTX_SENTINEL``."""
        entity = SimpleEntity(name="freshly_built", value=1)
        assert isinstance(entity.fxtr_ctx, RawFxtrEntityMissingMarkerSentinel)

    @pytest.mark.anyio
    async def test_store_captures_marker_from_destination_scope(self, serdes_scope: FxtrScope):
        """Storing into a scope captures that scope's marker — including its
        provenance parent — into the stored cbor."""
        entity = SimpleEntity(name="captured", value=2)
        ref = entity.store(serdes_scope)
        # Load it back and check the cbor's $fxtr.ctx field is the
        # destination scope's marker.
        cbor = await serdes_scope.load_cbor(ref.entity_id)
        assert isinstance(cbor, dict)
        marker = cbor["$fxtr.ctx"]
        assert isinstance(marker, RawFxtrEntityMarkerID)
        assert marker.inner_id == serdes_scope.get_context_marker_id()

    @pytest.mark.anyio
    async def test_store_captures_provenance_marker(self, serdes_scope: FxtrScope):
        """Storing into a scope with a provenance c-node yields a layered
        FxtrID and propagates the provenance parent into the cbor's marker."""
        # Build a c-node in serdes_scope.
        ground = FxtrID.from_tag_and_payload(".", b"")
        ctx_type = serdes_scope.track_builtin(FxtrID.builtin("ctx"))
        ctx_data: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
            "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
            "value": RawWeakFxtrID(inner_id=ground),
            "ctx_key": "marker_capture_test",
        }
        c_node = serdes_scope._main_arena.create_c_node(
            serdes_scope.clone_authorization(), ctx_data
        )

        async with FxtrScope(
            provenance_context=c_node, provenance_context_from_scope=serdes_scope
        ) as scope:
            entity = SimpleEntity(name="captured", value=2)
            ref = entity.store(scope)

            # The returned FxtrID is layered.
            assert ref.entity_id._provenance_parent is not None
            assert ref.entity_id._provenance_parent.get_tag() == "n"

            # The loaded cbor's marker has the c-node as its provenance parent.
            cbor = await scope.load_cbor(ref.entity_id)
            assert isinstance(cbor, dict)
            marker = cbor["$fxtr.ctx"]
            assert isinstance(marker, RawFxtrEntityMarkerID)
            assert marker.inner_id._provenance_parent == c_node

    @pytest.mark.anyio
    async def test_load_then_restore_preserves_original_marker(self, serdes_scope: FxtrScope):
        """Loading an entity preserves its stored ``fxtr_ctx`` — even when
        re-stored into a scope with a *different* provenance context."""
        # Build two distinct c-nodes in serdes_scope.
        ground = FxtrID.from_tag_and_payload(".", b"")
        ctx_type = serdes_scope.track_builtin(FxtrID.builtin("ctx"))

        def _make_c(key: str) -> FxtrID:
            ctx_data: FxtrCbor = {
                "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=ground),
                "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
                "value": RawWeakFxtrID(inner_id=ground),
                "ctx_key": key,
            }
            return serdes_scope._main_arena.create_c_node(
                serdes_scope.clone_authorization(), ctx_data
            )

        c_a = _make_c("trace_a")
        c_b = _make_c("trace_b")

        async with FxtrScope(
            provenance_context=c_a, provenance_context_from_scope=serdes_scope
        ) as scope_a:
            entity = SimpleEntity(name="rt", value=3)
            ref = entity.store(scope_a)
            loaded = await ref.load(scope_a)
            assert isinstance(loaded.fxtr_ctx, RawFxtrEntityMarkerID)
            # The loaded entity's marker carries c_a as its provenance parent.
            assert loaded.fxtr_ctx.inner_id._provenance_parent == c_a

            # Re-store the loaded entity in a scope whose own marker is c_b.
            # The round-tripped entity should retain c_a (not c_b).
            async with FxtrScope(
                provenance_context=c_b, provenance_context_from_scope=serdes_scope
            ) as scope_b:
                ref2 = loaded.store(scope_b)
                cbor2 = await scope_b.load_cbor(ref2.entity_id)
                assert isinstance(cbor2, dict)
                marker2 = cbor2["$fxtr.ctx"]
                assert isinstance(marker2, RawFxtrEntityMarkerID)
                assert marker2 == loaded.fxtr_ctx
                # And specifically the prov parent is c_a, not c_b.
                assert marker2.inner_id._provenance_parent == c_a
                assert marker2.inner_id._provenance_parent != c_b

    @pytest.mark.anyio
    async def test_convert_to_cbor_matches_store_cbor(self, serdes_scope: FxtrScope):
        """``_fxtr_convert_to_cbor`` returns the same cbor that
        ``_fxtr_store_impl`` hands to ``store_cbor``.

        Verifies the refactor: ``_fxtr_store_impl`` is
        ``_fxtr_convert_to_cbor`` followed by storage, so the cbor
        produced by the convert helper should round-trip back when
        we load the stored entity by id.
        """
        with serdes_scope.as_active_scope():
            entity = SimpleEntity(name="convert_helper", value=42)
            cbor = entity._fxtr_convert_to_cbor()
        # Same entity stored normally — load the cbor by its id.
        ref = entity.store(serdes_scope)
        stored_cbor = await serdes_scope.load_cbor(ref.entity_id)
        # Both should match: convert produces the cbor the store path
        # uses; round-tripping it via load_cbor gives back the same
        # shape.
        assert cbor == stored_cbor

    @pytest.mark.anyio
    async def test_convert_to_cbor_then_create_c_node(self, serdes_scope: FxtrScope):
        """Sandbox-provenance usage path: build cbor from a mashumaro
        entity, hand it to ``scope.create_c_node`` directly to mint a
        c-node without going through ``.store()`` (no double-store).
        """
        with serdes_scope.as_active_scope():
            # Use an entity shape that has a "value" field (required
            # for create_c_node).
            entity = SimpleEntity(name="c_node_payload", value=7)
            cbor = entity._fxtr_convert_to_cbor()
        assert isinstance(cbor, dict)
        # SimpleEntity.value is an int, not a FxtrID — patch the
        # cbor's "value" field for c-node creation. The intent of
        # this test is just to verify the convert -> create_c_node
        # plumbing works; sandbox code uses a purpose-built entity
        # with a properly-typed value field.
        ground = FxtrID.from_tag_and_payload(".", b"")
        cbor["value"] = RawWeakFxtrID(inner_id=ground)
        c_node = serdes_scope.create_c_node(cbor)
        assert c_node.get_tag() == "c"
        assert c_node._provenance_parent is None

    @pytest.mark.anyio
    async def test_load_rejects_sentinel_marker(self, serdes_scope: FxtrScope):
        """``from_lowlevel_entity_data`` rejects cbor whose ``$fxtr.ctx`` is the
        missing-marker sentinel.

        Stored cbor never contains a sentinel (the encoder rejects it at
        storage time), but defensive validation here catches anyone trying
        to feed a not-yet-stored entity dict directly to the load handler.
        """
        cbor = typing.cast(
            Any,
            {
                "$fxtr.type": RawWeakFxtrID(inner_id=FxtrID.builtin("test_simple")),
                "name": "broken",
                "value": 0,
                "$fxtr.ctx": MISSING_FXTR_CTX_SENTINEL,
            },
        )
        with pytest.raises(ValueError, match="not a"):
            await _from_cbor(cbor, SimpleEntity)


# =============================================================================
# Tests: Type Discrimination
# =============================================================================


class TestTypeDiscrimination:
    """Tests for type discrimination during deserialization."""

    @pytest.mark.anyio
    async def test_multiple_entity_types(self, serdes_scope: FxtrScope):
        """Multiple DataclassEntity subclasses can coexist."""
        simple = SimpleEntity(name="simple", value=1)
        with_bytes = EntityWithBytes(name="bytes", data=b"data")

        ref1 = simple.store()
        ref2 = with_bytes.store()

        loaded1 = await ref1.load()
        loaded2 = await ref2.load()

        assert isinstance(loaded1, SimpleEntity)
        assert isinstance(loaded2, EntityWithBytes)

    @pytest.mark.anyio
    async def test_type_discrimination_on_load(self, serdes_scope: FxtrScope):
        """Correct type instantiated on load."""
        entity = SimpleEntity(name="discrimination", value=42)
        ref = entity.store()
        loaded = await ref.load()
        assert type(loaded) is SimpleEntity

    @pytest.mark.anyio
    async def test_type_id_uniqueness(self, serdes_scope: FxtrScope):
        """Each type has unique ID."""
        assert SimpleEntity._fxtr_known_type_id != EntityWithBytes._fxtr_known_type_id
        assert SimpleEntity._fxtr_known_type_id != EntityWithDict._fxtr_known_type_id


# =============================================================================
# Tests: Complex Field Types
# =============================================================================


class TestComplexFieldTypes:
    """Tests for entities with complex field types."""

    @pytest.mark.anyio
    async def test_entity_with_entity_ref_field(self, serdes_scope: FxtrScope):
        """Field of type EntityRef[T]."""
        child = SimpleEntity(name="child", value=1)
        child_ref = child.store()

        parent = EntityWithRef(name="parent", child=child_ref)
        parent_ref = parent.store()

        loaded = await parent_ref.load()
        assert loaded.name == "parent"

        # Load child
        loaded_child = await loaded.child.load()
        assert loaded_child.name == "child"

    @pytest.mark.anyio
    async def test_entity_with_list_of_refs(self, serdes_scope: FxtrScope):
        """Field of type list[EntityRef[T]]."""
        child1 = SimpleEntity(name="child1", value=1)
        child2 = SimpleEntity(name="child2", value=2)
        ref1 = child1.store()
        ref2 = child2.store()

        parent = EntityWithListOfRefs(name="parent", children=[ref1, ref2])
        parent_ref = parent.store()

        loaded = await parent_ref.load()
        assert len(loaded.children) == 2

        # Load children
        c1 = await loaded.children[0].load()
        c2 = await loaded.children[1].load()
        assert c1.name == "child1"
        assert c2.name == "child2"

    @pytest.mark.anyio
    async def test_entity_with_optional_ref_none(self, serdes_scope: FxtrScope):
        """Field of type Optional[EntityRef[T]] with None value."""
        entity = EntityWithOptionalRef(name="no_child", child=None)
        ref = entity.store()

        loaded = await ref.load()
        assert loaded.child is None

    @pytest.mark.anyio
    async def test_entity_with_optional_ref_some(self, serdes_scope: FxtrScope):
        """Field of type Optional[EntityRef[T]] with Some value."""
        child = SimpleEntity(name="child", value=42)
        child_ref = child.store()

        entity = EntityWithOptionalRef(name="has_child", child=child_ref)
        ref = entity.store()

        loaded = await ref.load()
        assert loaded.child is not None
        child_loaded = await loaded.child.load()
        assert child_loaded.name == "child"

    @pytest.mark.anyio
    async def test_entity_with_bytes_field(self, serdes_scope: FxtrScope):
        """Field of type bytes."""
        entity = EntityWithBytes(name="binary", data=b"\x00\x01\x02\xff")
        ref = entity.store()

        loaded = await ref.load()
        assert loaded.data == b"\x00\x01\x02\xff"


# =============================================================================
# Tests: Type Parameter Preservation
# =============================================================================


class TestTypeParameterPreservation:
    """Tests for preserving type parameters through store/load roundtrips."""

    @pytest.mark.anyio
    async def test_entity_ref_to_concrete_type_loads_correctly(self, type_param_scope: FxtrScope):
        """EntityRef[ConcreteType] loads and deserializes as the concrete type."""
        child = TypeA(a_field="child_value")
        child_ref = child.store()

        # Verify the ref knows how to load as TypeA
        loaded = await child_ref.load()
        assert isinstance(loaded, TypeA)
        assert loaded.a_field == "child_value"

    @pytest.mark.anyio
    async def test_entity_ref_union_loads_correct_type_a(self, type_param_scope: FxtrScope):
        """EntityRef[TypeA | TypeB] loads TypeA correctly based on discriminator."""
        # Store a TypeA
        child_a = TypeA(a_field="i_am_a")
        child_ref = child_a.store()

        # Create entity with union-typed ref
        parent = EntityWithUnionRef(name="parent_a", child=child_ref)
        parent_ref = parent.store()

        # Load parent and then child
        loaded_parent = await parent_ref.load()
        loaded_child = await loaded_parent.child.load()

        # Should load as TypeA specifically
        assert isinstance(loaded_child, TypeA), f"Expected TypeA, got {type(loaded_child)}"
        assert loaded_child.a_field == "i_am_a"

    @pytest.mark.anyio
    async def test_entity_ref_union_loads_correct_type_b(self, type_param_scope: FxtrScope):
        """EntityRef[TypeA | TypeB] loads TypeB correctly based on discriminator."""
        # Store a TypeB
        child_b = TypeB(b_field=42)
        child_ref = child_b.store()

        # Create entity with union-typed ref
        parent = EntityWithUnionRef(name="parent_b", child=child_ref)
        parent_ref = parent.store()

        # Load parent and then child
        loaded_parent = await parent_ref.load()
        loaded_child = await loaded_parent.child.load()

        # Should load as TypeB specifically
        assert isinstance(loaded_child, TypeB), f"Expected TypeB, got {type(loaded_child)}"
        assert loaded_child.b_field == 42

    @pytest.mark.anyio
    async def test_entity_ref_type_var_can_store_and_load_with_rewritten_type(
        self, type_param_scope: FxtrScope
    ):
        """
        WrapperWithRef[T] has field ref: EntityRef[T].
        When T=TypeA the ref should load as TypeA.
        """
        inner = TypeA(a_field="nested")
        inner_ref = inner.store()

        wrapper = WrapperWithRef(name="wrapper", ref=inner_ref)
        wrapper_ref = wrapper.store().replace_target_type(WrapperWithRef[TypeA])

        # Load wrapper
        loaded_wrapper = await wrapper_ref.load()
        assert loaded_wrapper.name == "wrapper"

        # Load the inner ref - should be TypeA
        loaded_inner = await loaded_wrapper.ref.load()
        assert isinstance(loaded_inner, TypeA), f"Expected TypeA, got {type(loaded_inner)}"
        assert loaded_inner.a_field == "nested"

    @pytest.mark.anyio
    async def test_entity_ref_type_var_can_store_and_load_without_specific_type(
        self, type_param_scope: FxtrScope
    ):
        """
        WrapperWithRef[T] has field ref: EntityRef[T].
        When T is not known at store time, it should load back as UnknownEntity.
        """
        inner = TypeA(a_field="nested")
        inner_ref = inner.store()

        # wrapper is known to be WrapperWithRef[TypeA] at typechecking time, but not at runtime
        wrapper: WrapperWithRef[TypeA] = WrapperWithRef(name="wrapper", ref=inner_ref)
        # so when storing it, it will store without knowing how to load TypeA
        wrapper_ref = wrapper.store()

        # Load wrapper
        loaded_wrapper = await wrapper_ref.load()
        assert loaded_wrapper.name == "wrapper"

        # Load the inner ref - should be UnknownEntity
        loaded_inner = await loaded_wrapper.ref.load()
        assert isinstance(
            loaded_inner, UnknownEntity
        ), f"Expected UnknownEntity, got {type(loaded_inner)}"
        assert loaded_inner.raw_cbor["a_field"] == "nested"

        # Storing again should give the same result
        roundtrip_inner = loaded_inner.store()
        assert roundtrip_inner == loaded_wrapper.ref
        roundtrip_ref = loaded_wrapper.store()
        assert roundtrip_ref == wrapper_ref

    @pytest.mark.anyio
    async def test_entity_ref_type_var_allows_introspection_at_store_time(
        self, type_param_scope: FxtrScope
    ):
        """
        WrapperWithMagicInferredRef[T] has field ref: EntityRef[T].
        WrapperWithMagicInferredRef demonstrates that it is possible to introspect the type
        parameter of the outer entity when storing, and load it back.
        """
        inner = TypeA(a_field="nested")
        inner_ref = inner.store()

        # At typechecking time we don't necessarily know the target type T. However, inner_ref
        # knows it is TypeA at runtime, and WrapperWithMagicInferredRef can introspect this.
        wrapper = WrapperWithMagicInferredRef(name="wrapper", ref=inner_ref)
        wrapper_ref = wrapper.store()

        assert wrapper_ref.target_type == WrapperWithMagicInferredRef[TypeA]

        # Load wrapper
        loaded_wrapper = await wrapper_ref.load()
        assert loaded_wrapper.name == "wrapper"

        # Load the inner ref - should be TypeA
        loaded_inner = await loaded_wrapper.ref.load()
        assert isinstance(loaded_inner, TypeA), f"Expected TypeA, got {type(loaded_inner)}"
        assert loaded_inner.a_field == "nested"

    @pytest.mark.anyio
    async def test_entity_ref_type_var_errors_when_loaded_without_type_params(
        self, type_param_scope: FxtrScope
    ):
        """
        WrapperWithRef[T] has field ref: EntityRef[T]. Loading as WrapperWithRef with no args will
        try to load as EntityRef[Entity], which is not allowed. Should show a useful error.
        """
        inner = TypeA(a_field="nested")
        inner_ref = inner.store()
        wrapper = WrapperWithRef(name="wrapper", ref=inner_ref)
        wrapper_ref = wrapper.store()

        # Load wrapper with no type param
        bad_ref = EntityRef(wrapper_ref.entity_id, WrapperWithRef)

        with pytest.raises(Exception) as exc:
            await bad_ref.load()

        # mashumaro by default wraps the internal error with an error about an invalid field.
        # Walk the __cause__/__context__ chain to find the helpful TypeError.
        expected_msg = (
            "If you are seeing this error while loading a generic entity class, it is possible you"
            " forgot to specify a concrete type for a generic type parameter"
        )
        err = exc.value
        while err is not None:
            if isinstance(err, TypeError) and expected_msg in str(err):
                break
            err = err.__cause__ or err.__context__
        else:
            raise AssertionError(
                f"Expected a TypeError with message containing {expected_msg!r} "
                "in the exception chain, but didn't find one. "
                f"Original exception: {exc.value!r}"
            )

    @pytest.mark.anyio
    async def test_entity_ref_type_var_allows_fallback_when_loaded_without_type_params(
        self, type_param_scope: FxtrScope
    ):
        """
        WrapperWithRefAndFallback[T] has field ref: EntityRef[T] but declares the type bound as
        LoadAsUnknownByDefault[Entity]. When loaded without type params, it should load back as
        UnknownEntity via the LoadAsUnknownByDefault fallback.
        """
        inner = TypeA(a_field="nested")
        inner_ref = inner.store()
        wrapper = WrapperWithRefAndFallback(name="wrapper", ref=inner_ref)
        wrapper_ref = wrapper.store()

        # Load wrapper with no type param
        ref_without_type_params = EntityRef(wrapper_ref.entity_id, WrapperWithRefAndFallback)

        # Load wrapper
        loaded_wrapper = await ref_without_type_params.load()
        assert loaded_wrapper.name == "wrapper"

        # Load the inner ref - should be UnknownEntity
        loaded_inner = await loaded_wrapper.ref.load()
        assert isinstance(
            loaded_inner, UnknownEntity
        ), f"Expected UnknownEntity, got {type(loaded_inner)}"
        assert loaded_inner.raw_cbor["a_field"] == "nested"

        # Storing again should give the same ID
        roundtrip_ref = loaded_wrapper.store()
        assert roundtrip_ref.entity_id == wrapper_ref.entity_id


# =============================================================================
# Tests: Inline Entity - Child[T]
# =============================================================================


class TestInlineEntityChild:
    """Tests for Child[T] type alias."""

    @pytest.mark.anyio
    async def test_child_basic_store_load(self, inline_entity_scope: FxtrScope):
        """Basic store and load of entity with Child[T] field."""
        leaf = LeafEntity(value=42)
        parent = ParentWithChild(name="parent", child=leaf)

        # Store parent (child should be stored automatically)
        ref = parent.store()

        # Load parent (child should be loaded automatically)
        loaded = await ref.load()

        assert loaded.name == "parent"
        assert isinstance(loaded.child, LeafEntity)
        assert loaded.child.value == 42

    @pytest.mark.anyio
    async def test_child_creates_strong_link(self, inline_entity_scope: FxtrScope):
        """Child[T] should create a strong link (RawStrongFxtrID) in CBOR."""
        leaf = LeafEntity(value=1)
        parent = ParentWithChild(name="test", child=leaf)

        ref = parent.store()
        cbor = await inline_entity_scope.load_cbor(ref.entity_id)

        # Check that child is stored as a strong link
        assert isinstance(cbor, dict)
        assert "child" in cbor
        child_value = cbor["child"]
        # After serialization through mashumaro, it should be RawStrongFxtrID
        assert isinstance(child_value, RawStrongFxtrID)


# =============================================================================
# Tests: Inline Entity - LinkedEntity[T]
# =============================================================================


class TestInlineEntityLinked:
    """Tests for LinkedEntity[T] type alias."""

    @pytest.mark.anyio
    async def test_linked_entity_basic_store_load(self, inline_entity_scope: FxtrScope):
        """Basic store and load of entity with LinkedEntity[T] field."""
        leaf = LeafEntity(value=99)
        parent = ParentWithLinkedEntity(name="linked_parent", linked=leaf)

        ref = parent.store()
        loaded = await ref.load()

        assert loaded.name == "linked_parent"
        assert isinstance(loaded.linked, LeafEntity)
        assert loaded.linked.value == 99

    @pytest.mark.anyio
    async def test_linked_entity_creates_weak_link(self, inline_entity_scope: FxtrScope):
        """LinkedEntity[T] should create a weak link (RawWeakFxtrID) in CBOR."""
        leaf = LeafEntity(value=1)
        parent = ParentWithLinkedEntity(name="test", linked=leaf)

        ref = parent.store()
        cbor = await inline_entity_scope.load_cbor(ref.entity_id)

        # Check that linked is stored as a weak link
        assert isinstance(cbor, dict)
        assert "linked" in cbor
        linked_value = cbor["linked"]
        assert isinstance(linked_value, RawWeakFxtrID)


# =============================================================================
# Tests: Inline Entity - Mixed References
# =============================================================================


class TestInlineEntityMixed:
    """Tests for entities with both inline and lazy refs."""

    @pytest.mark.anyio
    async def test_mixed_inline_and_lazy(self, inline_entity_scope: FxtrScope):
        """Entity with both Child[T] (inline) and ChildRef[T] (lazy) fields."""
        child_leaf = LeafEntity(value=1)
        lazy_leaf = LeafEntity(value=2)

        parent = ParentWithMixedRefs(
            name="mixed",
            child=child_leaf,
            lazy=lazy_leaf.store(),
        )

        ref = parent.store()
        loaded = await ref.load()

        # Child should be auto-loaded
        assert loaded.name == "mixed"
        assert isinstance(loaded.child, LeafEntity)
        assert loaded.child.value == 1

        # Lazy should be an EntityRef that needs explicit loading
        assert isinstance(loaded.lazy, EntityRef)
        lazy_loaded = await loaded.lazy.load()
        assert isinstance(lazy_loaded, LeafEntity)
        assert lazy_loaded.value == 2


# =============================================================================
# Tests: Inline Entity - Nested
# =============================================================================


class TestInlineEntityNested:
    """Tests for nested inline entities."""

    @pytest.mark.anyio
    async def test_nested_inline_entities(self, inline_entity_scope: FxtrScope):
        """Entity A contains Child[B] where B contains Child[C]."""
        leaf = LeafEntity(value=42)
        inner = ParentWithChild(name="inner", child=leaf)
        outer = DeepNestingParent(name="outer", nested=inner)

        ref = outer.store()
        loaded = await ref.load()

        # All levels should be loaded
        assert loaded.name == "outer"
        assert isinstance(loaded.nested, ParentWithChild)
        assert loaded.nested.name == "inner"
        assert isinstance(loaded.nested.child, LeafEntity)
        assert loaded.nested.child.value == 42


# =============================================================================
# Tests: Inline Entity - DeferredLoadPlaceholder
# =============================================================================


class TestInlineEntityPlaceholder:
    """Tests for DeferredLoadPlaceholder handling."""

    @pytest.mark.anyio
    async def test_placeholder_type_exists(self, inline_entity_scope: FxtrScope):
        """DeferredLoadPlaceholder should be importable and have expected structure."""
        placeholder = DeferredLoadPlaceholder(index=5)
        assert placeholder.index == 5

    @pytest.mark.anyio
    async def test_post_init_with_placeholder(self, inline_entity_scope: FxtrScope):
        """Entity with __post_init__ that handles DeferredLoadPlaceholder."""

        @dataclasses.dataclass(frozen=True)
        class ValidatedParent(DataclassEntity, fxtr_type=FxtrID.builtin("test_validated_parent")):
            """A parent that validates child in __post_init__."""

            name: str
            child: Child[LeafEntity]

            def __post_init__(self):
                super().__post_init__()  # Must call parent's __post_init__
                # Skip validation during collecting phase (placeholder values)
                if isinstance(self.child, DeferredLoadPlaceholder):
                    return
                # Actual validation when child is loaded
                if self.child.value < 0:
                    raise ValueError("Child value must be non-negative")

        inline_entity_scope.track_builtin(FxtrID.builtin("test_validated_parent"))

        # Create and store valid parent
        leaf = LeafEntity(value=42)
        parent = ValidatedParent(name="validated", child=leaf)
        ref = parent.store()

        # Load should work (and post_init validation passes)
        loaded = await ref.load()
        assert loaded.name == "validated"
        assert loaded.child.value == 42


# =============================================================================
# Tests: Inline Entity - Error Handling
# =============================================================================


class TestInlineEntityErrorHandling:
    """Tests for error handling during inline entity loading."""

    @pytest.mark.anyio
    async def test_successful_inline_load(self, inline_entity_scope: FxtrScope):
        """Verify that inline entity loading works correctly (baseline for error tests)."""
        # This test verifies the happy path works
        leaf = LeafEntity(value=42)
        parent = ParentWithChild(name="success", child=leaf)
        ref = parent.store()

        # Load should succeed
        loaded = await ref.load()
        assert loaded.name == "success"
        assert loaded.child.value == 42

    @pytest.mark.anyio
    async def test_nested_entity_without_child_wrapper_raises_with_hint(
        self, inline_entity_scope: FxtrScope
    ):
        """Directly nesting a DataclassEntity inside another without Child[T] raises DuplicateEntityMarkerError with a helpful note."""
        inline_entity_scope.track_builtin(FxtrID.builtin("test_bare_nested_inner"))
        inline_entity_scope.track_builtin(FxtrID.builtin("test_bare_nested_outer"))

        outer = BareNestedOuter(
            bar=BareNestedInner(foo="hello"),
            baz=BareNestedInner(foo="world"),
        )

        with pytest.raises(DuplicateEntityMarkerError) as exc_info:
            outer.store()

        # The error should have a note suggesting to use Child[T]
        notes = exc_info.value.__notes__
        assert any("Please wrap inline child entities with Child[T]" in note for note in notes)

    @pytest.mark.anyio
    async def test_multiple_children_parallel_load(self, inline_entity_scope: FxtrScope):
        """Multiple inline children are loaded in parallel."""

        @dataclasses.dataclass(frozen=True)
        class MultiChildParent(
            DataclassEntity, fxtr_type=FxtrID.builtin("test_multi_child_parent")
        ):
            """A parent with multiple inline children."""

            name: str
            first: Child[LeafEntity]
            second: Child[LeafEntity]
            third: Child[LeafEntity]

        inline_entity_scope.track_builtin(FxtrID.builtin("test_multi_child_parent"))

        # Create parent with multiple children
        parent = MultiChildParent(
            name="multi",
            first=LeafEntity(value=1),
            second=LeafEntity(value=2),
            third=LeafEntity(value=3),
        )
        ref = parent.store()

        # Load and verify all children are loaded
        loaded = await ref.load()
        assert loaded.name == "multi"
        assert loaded.first.value == 1
        assert loaded.second.value == 2
        assert loaded.third.value == 3


# =============================================================================
# Tests: Inline Entity - Generics
# =============================================================================


class TestInlineEntityGeneric:
    """Tests for generic entities with inline children."""

    @pytest.mark.anyio
    async def test_generic_entity_with_child(self, inline_entity_scope: FxtrScope):
        """Generic Wrapper[T] where the wrapped value is a Child[T]."""

        @dataclasses.dataclass(frozen=True)
        class GenericWrapper[T](
            DataclassEntity, fxtr_type=FxtrID.builtin("test_generic_wrapper_child")
        ):
            """A generic wrapper with an inline child."""

            name: str
            child: Child[T]

        inline_entity_scope.track_builtin(FxtrID.builtin("test_generic_wrapper_child"))

        # Create wrapper with TypeA child
        inner = TypeA(a_field="generic_child")
        wrapper = GenericWrapper[TypeA](name="wrapper", child=inner)
        ref = wrapper.store().replace_target_type(GenericWrapper[TypeA])

        # Load and verify
        loaded = await ref.load()
        assert loaded.name == "wrapper"
        assert isinstance(loaded.child, TypeA)
        assert loaded.child.a_field == "generic_child"

    @pytest.mark.anyio
    async def test_generic_entity_with_multiple_children(self, inline_entity_scope: FxtrScope):
        """Generic Wrapper[T] with multiple inline children of type T."""

        @dataclasses.dataclass(frozen=True)
        class MultiChildWrapper[T](
            DataclassEntity, fxtr_type=FxtrID.builtin("test_multi_child_wrapper")
        ):
            """A wrapper with multiple inline children."""

            name: str
            first: Child[T]
            second: Child[T]

        inline_entity_scope.track_builtin(FxtrID.builtin("test_multi_child_wrapper"))

        # Create wrapper with two TypeB children
        child1 = TypeB(b_field=1)
        child2 = TypeB(b_field=2)
        wrapper = MultiChildWrapper[TypeB](name="multi", first=child1, second=child2)
        ref = wrapper.store().replace_target_type(MultiChildWrapper[TypeB])

        # Load and verify both children are loaded
        loaded = await ref.load()
        assert loaded.name == "multi"
        assert isinstance(loaded.first, TypeB)
        assert loaded.first.b_field == 1
        assert isinstance(loaded.second, TypeB)
        assert loaded.second.b_field == 2

    @pytest.mark.anyio
    async def test_generic_entity_with_list_of_children(self, inline_entity_scope: FxtrScope):
        """Generic Wrapper with list[Child[T]] field."""

        @dataclasses.dataclass(frozen=True)
        class ListChildWrapper[T](
            DataclassEntity, fxtr_type=FxtrID.builtin("test_list_child_wrapper")
        ):
            """A wrapper with a list of inline children."""

            name: str
            children: list[Child[T]]

        inline_entity_scope.track_builtin(FxtrID.builtin("test_list_child_wrapper"))

        # Create wrapper with list of TypeA children
        children = [TypeA(a_field=f"child_{i}") for i in range(3)]
        wrapper = ListChildWrapper[TypeA](name="list_wrapper", children=children)
        ref = wrapper.store().replace_target_type(ListChildWrapper[TypeA])

        # Load and verify all children are loaded
        loaded = await ref.load()
        assert loaded.name == "list_wrapper"
        assert len(loaded.children) == 3
        for i, child in enumerate(loaded.children):
            assert isinstance(child, TypeA)
            assert child.a_field == f"child_{i}"


# =============================================================================
# Tests: Basic Collection Types
# =============================================================================


class TestBasicCollectionTypes:
    """Tests for basic collection type serialization (set, frozenset, dict, frozendict)."""

    @pytest.mark.anyio
    async def test_set_field_roundtrip(self, collection_scope: FxtrScope):
        """set[str] field stores and loads correctly."""
        entity = EntityWithSet(name="test", items={"a", "b", "c"})
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.name == "test"
        assert loaded.items == {"a", "b", "c"}
        assert isinstance(loaded.items, set)

    @pytest.mark.anyio
    async def test_frozenset_field_roundtrip(self, collection_scope: FxtrScope):
        """frozenset[str] field stores and loads correctly."""
        entity = EntityWithFrozenset(name="test", items=frozenset({"x", "y", "z"}))
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.name == "test"
        assert loaded.items == frozenset({"x", "y", "z"})
        assert isinstance(loaded.items, frozenset)

    @pytest.mark.anyio
    async def test_frozendict_field_roundtrip(self, collection_scope: FxtrScope):
        """frozendict[str, int] field stores and loads correctly."""
        entity = EntityWithFrozendict(name="test", mapping=frozendict({"a": 1, "b": 2}))
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.name == "test"
        assert loaded.mapping == frozendict({"a": 1, "b": 2})
        assert isinstance(loaded.mapping, frozendict)

    @pytest.mark.anyio
    async def test_dict_field_roundtrip(self, collection_scope: FxtrScope):
        """dict[str, Any] field stores and loads correctly."""
        entity = EntityWithDict(name="test", metadata={"key": "value", "num": 42})
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.name == "test"
        assert loaded.metadata == {"key": "value", "num": 42}
        assert isinstance(loaded.metadata, dict)

    @pytest.mark.anyio
    async def test_empty_set_roundtrip(self, collection_scope: FxtrScope):
        """Empty set preserves type."""
        entity = EntityWithSet(name="empty", items=set())
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.items == set()
        assert isinstance(loaded.items, set)

    @pytest.mark.anyio
    async def test_empty_frozenset_roundtrip(self, collection_scope: FxtrScope):
        """Empty frozenset preserves type."""
        entity = EntityWithFrozenset(name="empty", items=frozenset())
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.items == frozenset()
        assert isinstance(loaded.items, frozenset)

    @pytest.mark.anyio
    async def test_empty_frozendict_roundtrip(self, collection_scope: FxtrScope):
        """Empty frozendict preserves type."""
        entity = EntityWithFrozendict(name="empty", mapping=frozendict())
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.mapping == frozendict()
        assert isinstance(loaded.mapping, frozendict)

    @pytest.mark.anyio
    async def test_nested_frozen_collections_roundtrip(self, collection_scope: FxtrScope):
        """Nested frozen collections (frozendict[str, frozenset[int]]) roundtrip."""
        entity = EntityWithNestedFrozen(
            name="nested",
            mapping=frozendict({"nums": frozenset({1, 2, 3}), "more": frozenset({4, 5})}),
        )
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.name == "nested"
        assert loaded.mapping == frozendict(
            {"nums": frozenset({1, 2, 3}), "more": frozenset({4, 5})}
        )
        assert isinstance(loaded.mapping, frozendict)
        assert isinstance(loaded.mapping["nums"], frozenset)


# =============================================================================
# Tests: EntityRef in Collections
# =============================================================================


class TestEntityRefInCollections:
    """Tests for EntityRef values in sets and frozensets."""

    @pytest.mark.anyio
    async def test_entityref_in_set_roundtrip(self, collection_scope: FxtrScope):
        """set[EntityRef[T]] roundtrips correctly."""
        child1 = SimpleEntity(name="child1", value=1)
        child2 = SimpleEntity(name="child2", value=2)
        ref1 = child1.store()
        ref2 = child2.store()

        entity = EntityWithRefSet(name="test", refs={ref1, ref2})
        parent_ref = entity.store()
        loaded = await parent_ref.load()

        assert loaded.name == "test"
        assert len(loaded.refs) == 2
        assert isinstance(loaded.refs, set)

        # Verify we can load the referenced entities
        loaded_children = [await r.load() for r in loaded.refs]
        names = {c.name for c in loaded_children}
        assert names == {"child1", "child2"}

    @pytest.mark.anyio
    async def test_entityref_in_frozenset_roundtrip(self, collection_scope: FxtrScope):
        """frozenset[EntityRef[T]] roundtrips correctly."""
        child1 = SimpleEntity(name="child1", value=1)
        child2 = SimpleEntity(name="child2", value=2)
        ref1 = child1.store()
        ref2 = child2.store()

        entity = EntityWithRefFrozenset(name="test", refs=frozenset({ref1, ref2}))
        parent_ref = entity.store()
        loaded = await parent_ref.load()

        assert loaded.name == "test"
        assert len(loaded.refs) == 2
        assert isinstance(loaded.refs, frozenset)

        # Verify we can load the referenced entities
        loaded_children = [await r.load() for r in loaded.refs]
        names = {c.name for c in loaded_children}
        assert names == {"child1", "child2"}

    @pytest.mark.anyio
    async def test_fxtrid_in_set_roundtrip(self, collection_scope: FxtrScope):
        """set[FxtrID] roundtrips correctly."""
        id1 = FxtrID.builtin("test_id_1")
        id2 = FxtrID.builtin("test_id_2")
        collection_scope.track_builtin(id1)
        collection_scope.track_builtin(id2)

        entity = EntityWithFxtrIDSet(name="test", ids={id1, id2})
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.name == "test"
        assert loaded.ids == {id1, id2}
        assert isinstance(loaded.ids, set)

    @pytest.mark.anyio
    async def test_fxtrid_in_frozenset_roundtrip(self, collection_scope: FxtrScope):
        """frozenset[FxtrID] roundtrips correctly."""
        id1 = FxtrID.builtin("test_id_1")
        id2 = FxtrID.builtin("test_id_2")
        collection_scope.track_builtin(id1)
        collection_scope.track_builtin(id2)

        entity = EntityWithFxtrIDFrozenset(name="test", ids=frozenset({id1, id2}))
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.name == "test"
        assert loaded.ids == frozenset({id1, id2})
        assert isinstance(loaded.ids, frozenset)


# =============================================================================
# Tests: EntityRef as Dict Keys
# =============================================================================


class TestEntityRefAsDictKeys:
    """Tests for EntityRef and FxtrID as dict/frozendict keys."""

    @pytest.mark.anyio
    async def test_entityref_as_dict_key_roundtrip(self, collection_scope: FxtrScope):
        """dict[EntityRef[T], str] roundtrips correctly."""
        child1 = SimpleEntity(name="child1", value=1)
        child2 = SimpleEntity(name="child2", value=2)
        ref1 = child1.store()
        ref2 = child2.store()

        entity = EntityWithRefKeyDict(name="test", mapping={ref1: "first", ref2: "second"})
        parent_ref = entity.store()
        loaded = await parent_ref.load()

        assert loaded.name == "test"
        assert len(loaded.mapping) == 2
        assert isinstance(loaded.mapping, dict)

        # Verify we can look up by the refs
        assert loaded.mapping[ref1] == "first"
        assert loaded.mapping[ref2] == "second"

    @pytest.mark.anyio
    async def test_entityref_as_frozendict_key_roundtrip(self, collection_scope: FxtrScope):
        """frozendict[EntityRef[T], str] roundtrips correctly."""
        child1 = SimpleEntity(name="child1", value=1)
        child2 = SimpleEntity(name="child2", value=2)
        ref1 = child1.store()
        ref2 = child2.store()

        entity = EntityWithRefKeyFrozendict(
            name="test", mapping=frozendict({ref1: "first", ref2: "second"})
        )
        parent_ref = entity.store()
        loaded = await parent_ref.load()

        assert loaded.name == "test"
        assert len(loaded.mapping) == 2
        assert isinstance(loaded.mapping, frozendict)

        # Verify we can look up by the refs
        assert loaded.mapping[ref1] == "first"
        assert loaded.mapping[ref2] == "second"

    @pytest.mark.anyio
    async def test_fxtrid_as_dict_key_roundtrip(self, collection_scope: FxtrScope):
        """dict[FxtrID, str] roundtrips correctly."""
        id1 = FxtrID.builtin("key_id_1")
        id2 = FxtrID.builtin("key_id_2")
        collection_scope.track_builtin(id1)
        collection_scope.track_builtin(id2)

        entity = EntityWithFxtrIDKeyDict(name="test", mapping={id1: "first", id2: "second"})
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.name == "test"
        assert len(loaded.mapping) == 2
        assert isinstance(loaded.mapping, dict)
        assert loaded.mapping[id1] == "first"
        assert loaded.mapping[id2] == "second"


# =============================================================================
# Tests: Canonical Ordering
# =============================================================================


class TestCanonicalOrdering:
    """Tests for canonical byte determinism regardless of insertion order."""

    @pytest.mark.anyio
    async def test_set_canonical_ordering(self, collection_scope: FxtrScope):
        """Different insertion orders for set produce identical CBOR."""
        # Create entities with sets in different insertion orders
        e1 = EntityWithSet(name="test", items={"z", "a", "m"})
        e2 = EntityWithSet(name="test", items={"a", "m", "z"})
        e3 = EntityWithSet(name="test", items={"m", "z", "a"})

        # All should produce identical CBOR (after serialization)
        cbor1 = _to_cbor(e1)
        cbor2 = _to_cbor(e2)
        cbor3 = _to_cbor(e3)

        assert cbor1 == cbor2 == cbor3

    @pytest.mark.anyio
    async def test_frozenset_canonical_ordering(self, collection_scope: FxtrScope):
        """Different insertion orders for frozenset produce identical CBOR."""
        e1 = EntityWithFrozenset(name="test", items=frozenset({"z", "a", "m"}))
        e2 = EntityWithFrozenset(name="test", items=frozenset({"a", "m", "z"}))
        e3 = EntityWithFrozenset(name="test", items=frozenset({"m", "z", "a"}))

        cbor1 = _to_cbor(e1)
        cbor2 = _to_cbor(e2)
        cbor3 = _to_cbor(e3)

        assert cbor1 == cbor2 == cbor3

    @pytest.mark.anyio
    async def test_entityref_set_canonical_ordering(self, collection_scope: FxtrScope):
        """EntityRef sets are canonically ordered by FxtrID bytes."""
        child1 = SimpleEntity(name="child1", value=1)
        child2 = SimpleEntity(name="child2", value=2)
        child3 = SimpleEntity(name="child3", value=3)
        ref1 = child1.store()
        ref2 = child2.store()
        ref3 = child3.store()

        # Create entities with refs in different orders
        e1 = EntityWithRefSet(name="test", refs={ref1, ref2, ref3})
        e2 = EntityWithRefSet(name="test", refs={ref3, ref1, ref2})
        e3 = EntityWithRefSet(name="test", refs={ref2, ref3, ref1})

        cbor1 = _to_cbor(e1)
        cbor2 = _to_cbor(e2)
        cbor3 = _to_cbor(e3)

        assert cbor1 == cbor2 == cbor3

    @pytest.mark.anyio
    async def test_fxtrid_set_canonical_ordering(self, collection_scope: FxtrScope):
        """FxtrID sets are canonically ordered."""
        id1 = FxtrID.builtin("id_z")
        id2 = FxtrID.builtin("id_a")
        id3 = FxtrID.builtin("id_m")
        collection_scope.track_builtin(id1)
        collection_scope.track_builtin(id2)
        collection_scope.track_builtin(id3)

        e1 = EntityWithFxtrIDSet(name="test", ids={id1, id2, id3})
        e2 = EntityWithFxtrIDSet(name="test", ids={id2, id3, id1})
        e3 = EntityWithFxtrIDSet(name="test", ids={id3, id1, id2})

        cbor1 = _to_cbor(e1)
        cbor2 = _to_cbor(e2)
        cbor3 = _to_cbor(e3)

        assert cbor1 == cbor2 == cbor3

    @pytest.mark.anyio
    async def test_set_produces_same_entity_id(self, collection_scope: FxtrScope):
        """Sets with same elements but different insertion order produce same entity ID."""
        e1 = EntityWithSet(name="test", items={"z", "a", "m"})
        e2 = EntityWithSet(name="test", items={"a", "m", "z"})

        ref1 = e1.store()
        ref2 = e2.store()

        # Same content should produce same ID (content-addressed)
        assert ref1.entity_id == ref2.entity_id


# =============================================================================
# Tests: FrozenFxtrCbor field round-trip
# =============================================================================


class TestFrozenFxtrCborField:
    """Round-trip a DataclassEntity carrying a FrozenFxtrCbor body field.

    Locks in the global FrozenFxtrCborSerializationStrategy registration
    on DataclassEntity.Config — relied on by fxtr_report_builder's
    RootReport / SubReport entity types to carry MDX-compiled trees
    with embedded entity refs.
    """

    @pytest.mark.anyio
    async def test_primitives_only_roundtrip(self, collection_scope: FxtrScope):
        """Tree of strings, ints, lists, and dicts round-trips."""
        body = freeze_fxtr_cbor(
            [
                "__fragment",
                {},
                [["h1", {}, ["Hello"]], ["p", {"className": "intro"}, ["World", 42]]],
            ]
        )
        entity = EntityWithFrozenCbor(name="t", body=body)
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.name == "t"
        assert loaded.body == body

    @pytest.mark.anyio
    async def test_embedded_raw_weak_fxtr_id_survives(self, collection_scope: FxtrScope):
        """A RawWeakFxtrID nested deep inside the tree round-trips."""
        # Store some other entity first so its id is well-formed.
        inner = SimpleEntity(name="inner", value=1)
        inner_ref = inner.store()
        weak = RawWeakFxtrID(inner_id=inner_ref.entity_id)

        body = freeze_fxtr_cbor(
            ["__fragment", {}, [["RenderFigure", {"entity": weak, "figureKey": "figure-0"}, []]]]
        )
        entity = EntityWithFrozenCbor(name="with-ref", body=body)
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.name == "with-ref"
        assert loaded.body == body
        # Specifically verify the ref leaf survived as the same wrapper type.
        fragment_children = loaded.body[2]  # type: ignore[index]
        figure_node = fragment_children[0]  # type: ignore[index]
        figure_props = figure_node[1]  # type: ignore[index]
        assert isinstance(figure_props["entity"], RawWeakFxtrID)
        assert figure_props["entity"].inner_id == inner_ref.entity_id

    @pytest.mark.anyio
    async def test_bytes_in_tree_roundtrip(self, collection_scope: FxtrScope):
        """bytes leaves inside the FrozenFxtrCbor tree round-trip."""
        body = freeze_fxtr_cbor({"binary": b"\x00\x01\x02"})
        entity = EntityWithFrozenCbor(name="bytes", body=body)
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.body == body


# =============================================================================
# Tests: Strict serialization defaults (forbid_extra_keys + omit_default)
# =============================================================================


@dataclasses.dataclass(frozen=True)
class EntityWithDefaultField(DataclassEntity, fxtr_type=FxtrID.builtin("test_strict_default")):
    """Entity with defaulted fields, for omit_default tests."""

    name: str
    count: int = 0
    note: str = "unset"


@dataclasses.dataclass(frozen=True)
class EntityOptingIntoOmitDefault(
    DataclassEntity, fxtr_type=FxtrID.builtin("test_strict_omit_optin")
):
    """Same as ``EntityWithDefaultField`` but opts in to ``omit_default``."""

    name: str
    count: int = 0
    note: str = "unset"

    class Config(DataclassEntity.Config):
        omit_default = True


@dataclasses.dataclass(frozen=True)
class StrictInner(DataclassEntity, fxtr_type=FxtrID.builtin("test_strict_inner")):
    """Inner entity for strict bare-nesting tests."""

    foo: str


@dataclasses.dataclass(frozen=True)
class StrictBareOuter(DataclassEntity, fxtr_type=FxtrID.builtin("test_strict_bare_outer")):
    """Bare-nests StrictInner without a Child[T] wrapper (illegal)."""

    inner: StrictInner


@dataclasses.dataclass(frozen=True)
class StrictChildOuter(DataclassEntity, fxtr_type=FxtrID.builtin("test_strict_child_outer")):
    """Wraps StrictInner with Child[T] (the correct form)."""

    inner: Child[StrictInner]


@pytest.fixture
async def strict_scope() -> AsyncIterator[FxtrScope]:
    """Scope with strict-serialization test types pre-tracked and active."""
    async with FxtrScope(name="strict_test") as scope:
        for type_name in (
            "test_strict_default",
            "test_strict_omit_optin",
            "test_strict_inner",
            "test_strict_bare_outer",
            "test_strict_child_outer",
        ):
            scope.track_builtin(FxtrID.builtin(type_name))
        yield scope


class TestStrictSerializationDefaults:
    """Serialization defaults for every entity.

    ``forbid_extra_keys`` is on by default (a freshly-defined DataclassEntity inherits
    it from ``DataclassEntity.Config``). ``omit_default`` is *off* by default —
    default-valued fields are serialized like any other — but an entity can opt in via
    ``class Config(DataclassEntity.Config): omit_default = True``, and the
    framework-managed fields survive the opt-in.
    """

    @pytest.mark.anyio
    async def test_forbid_extra_keys_rejects_unknown_key(self, strict_scope: FxtrScope):
        """Decoding cbor with a key not on the dataclass raises ExtraKeysError."""
        cbor: FxtrCbor = {
            "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=FxtrID.from_tag_and_payload(".", b"")),
            "$fxtr.type": RawWeakFxtrID(inner_id=FxtrID.builtin("test_strict_default")),
            "name": "x",
            "count": 1,
            "note": "n",
            "bogus_extra": 123,
        }
        with pytest.raises(ExtraKeysError):
            await _from_cbor(cbor, EntityWithDefaultField)

    @pytest.mark.anyio
    async def test_forbid_extra_keys_clean_roundtrip_ok(self, strict_scope: FxtrScope):
        """A store/load roundtrip with only known keys is unaffected."""
        ref = EntityWithDefaultField(name="ok", count=3, note="hi").store()
        loaded = await ref.load()
        assert (loaded.name, loaded.count, loaded.note) == ("ok", 3, "hi")

    @pytest.mark.anyio
    async def test_default_valued_fields_kept_by_default(self, strict_scope: FxtrScope):
        """``omit_default`` is off by default: default-valued fields are still serialized."""
        ref = EntityWithDefaultField(name="only_name").store()
        cbor = await strict_scope.load_cbor(ref.entity_id)
        assert isinstance(cbor, dict)
        assert cbor["name"] == "only_name"
        assert cbor["count"] == 0
        assert cbor["note"] == "unset"

    @pytest.mark.anyio
    async def test_omit_default_opt_in_drops_default_valued_fields(self, strict_scope: FxtrScope):
        """An entity opting into ``omit_default`` drops its default-valued fields."""
        ref = EntityOptingIntoOmitDefault(name="only_name").store()
        cbor = await strict_scope.load_cbor(ref.entity_id)
        assert isinstance(cbor, dict)
        assert cbor["name"] == "only_name"
        assert "count" not in cbor
        assert "note" not in cbor

    @pytest.mark.anyio
    async def test_omit_default_opt_in_keeps_non_default_fields(self, strict_scope: FxtrScope):
        """Even with ``omit_default`` on, fields set to a non-default value are present."""
        ref = EntityOptingIntoOmitDefault(name="n", count=5, note="x").store()
        cbor = await strict_scope.load_cbor(ref.entity_id)
        assert isinstance(cbor, dict)
        assert cbor["count"] == 5
        assert cbor["note"] == "x"

    @pytest.mark.anyio
    async def test_framework_fields_survive_omit_default_opt_in(self, strict_scope: FxtrScope):
        """``$fxtr.type`` / ``$fxtr.ctx`` are kept even for an all-defaults entity that
        opts into ``omit_default``, and it still round-trips."""
        ref = EntityOptingIntoOmitDefault(name="only_name").store()
        cbor = await strict_scope.load_cbor(ref.entity_id)
        assert isinstance(cbor, dict)
        assert isinstance(cbor["$fxtr.type"], (RawWeakFxtrID, RawStrongFxtrID))
        assert cbor["$fxtr.type"].inner_id == FxtrID.builtin("test_strict_omit_optin")
        assert isinstance(cbor["$fxtr.ctx"], RawFxtrEntityMarkerID)
        loaded = await ref.load()
        assert type(loaded) is EntityOptingIntoOmitDefault
        assert loaded.count == 0
        assert loaded.note == "unset"

    @pytest.mark.anyio
    async def test_bare_nesting_rejected_with_hint(self, strict_scope: FxtrScope):
        """Bare-nesting an entity (no Child[T]) is rejected with the Child[T] hint."""
        outer = StrictBareOuter(inner=StrictInner(foo="x"))
        with pytest.raises(DuplicateEntityMarkerError) as exc_info:
            outer.store()
        assert any(
            "Please wrap inline child entities with Child[T]" in note
            for note in exc_info.value.__notes__
        )

    @pytest.mark.anyio
    async def test_child_wrapping_still_works(self, strict_scope: FxtrScope):
        """The Child[T]-wrapped form stores and loads fine (guard doesn't fire)."""
        ref = StrictChildOuter(inner=StrictInner(foo="y")).store()
        loaded = await ref.load()
        assert isinstance(loaded.inner, StrictInner)
        assert loaded.inner.foo == "y"


# =============================================================================
# Tests: ValidateEntityTypesAnnotation (from test_entity_ref_scope.py)
# =============================================================================
