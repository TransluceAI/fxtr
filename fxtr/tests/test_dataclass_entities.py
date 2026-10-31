"""Tests for dataclass entities, tagged dataclasses, and their loader integration."""

from __future__ import annotations

import dataclasses
from typing import Any, ClassVar, Literal, cast

import cattrs
import pytest
from cattrs.dispatch import StructureHook, UnstructureHook
from cattrs.gen import make_dict_structure_fn, make_dict_unstructure_fn, override
from frozendict import frozendict

from fxtr.core.entities import (
    AmbiguousVariantError,
    BoundID,
    Entity,
    NoMatchingVariantError,
    StaticTypeLoader,
)
from fxtr.core.entity_data import (
    FxtrDataValue,
    StorableFxtrDataValue,
    freeze_fxtr_data,
)
from fxtr.core.json import FrozenJSONObject, FrozenJSONValue, JSONObject, JSONValue
from fxtr.core.loader_registry import (
    DEFAULT_ENTITY_LOADER_REGISTRY,
)
from fxtr.entity_defns import dataclass_entity
from fxtr.entity_defns.array import Array
from fxtr.entity_defns.converter import DEFAULT_CONVERTER
from fxtr.entity_defns.dataclass_bases import (
    ConversionError,
    StrictTypeError,
    TaggedDataclass,
    make_dataclass_structure_hook,
    make_dataclass_unstructure_hook,
)
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.entity_defns.unknown_entity import UNKNOWN_ENTITY_LOADER, UnknownEntity
from fxtr.storage.memory_storage import MemoryStorage

# ---- inline tagged values ----


@dataclasses.dataclass(frozen=True, kw_only=True)
class Circle(TaggedDataclass, fxtr_type="test.fxtr.circle"):
    radius: float


@dataclasses.dataclass(frozen=True, kw_only=True)
class Square(TaggedDataclass, fxtr_type="test.fxtr.square"):
    side: float


# ---- entities ----


@dataclasses.dataclass(frozen=True, kw_only=True)
class Leaf(DataclassEntity, fxtr_type="test.fxtr.leaf"):
    label: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class OtherLeaf(DataclassEntity, fxtr_type="test.fxtr.otherLeaf"):
    number: int


@dataclasses.dataclass(frozen=True, kw_only=True)
class Node(DataclassEntity, fxtr_type="test.fxtr.node"):
    name: str
    payload: bytes
    shape: Circle | Square
    child: BoundID[Leaf]


@dataclasses.dataclass(frozen=True, kw_only=True)
class UnionHolder(DataclassEntity, fxtr_type="test.fxtr.unionHolder"):
    ref: BoundID[Leaf | OtherLeaf]


@dataclasses.dataclass(frozen=True, kw_only=True)
class Wrapper[T: TaggedDataclass](DataclassEntity, fxtr_type="test.fxtr.wrapper"):
    inner: T


def _make_node(store: MemoryStorage) -> tuple[Node, BoundID[Leaf]]:
    leaf_ref = store.store_sync(Leaf(label="hi"))
    node = Node(name="n", payload=b"\x00\x01", shape=Circle(radius=2.0), child=leaf_ref)
    return node, leaf_ref


# ---- round trips ----


@pytest.mark.anyio
async def test_store_load_roundtrip() -> None:
    store = MemoryStorage()
    node, leaf_ref = _make_node(store)
    ref = await store.store(node)
    loaded = await store.load(ref)
    assert loaded == node
    assert await store.load(loaded.child) == Leaf(label="hi")


@pytest.mark.anyio
async def test_cid_stable_across_roundtrip() -> None:
    store = MemoryStorage()
    node, _ = _make_node(store)
    ref = await store.store(node)
    loaded = await store.load(ref)
    assert (await store.store(loaded)).entity_id == ref.entity_id


def test_wire_form_shape() -> None:
    store = MemoryStorage()
    node, leaf_ref = _make_node(store)
    data = DEFAULT_CONVERTER.unstructure_entity(node)
    assert data["$type"] == Node._fxtr_require_type_id() == "test.fxtr.node"
    shape_data = data["shape"]
    assert isinstance(shape_data, dict)
    assert shape_data["$type"] == Circle._fxtr_require_type_id()
    assert data["child"] == leaf_ref.entity_id
    assert data["payload"] == b"\x00\x01"


# ---- tagged dataclass subparts (serialized inline, discriminated by type id) ----


@dataclasses.dataclass(frozen=True, kw_only=True)
class ShapePile(DataclassEntity, fxtr_type="test.fxtr.shapePile"):
    """Entity exercising tagged subparts at various depths."""

    top: Circle | Square
    stack: tuple[Circle | Square, ...]
    by_name: frozendict[str, Circle | Square]
    maybe: Circle | Square | None


@pytest.mark.anyio
async def test_tagged_subparts_dispatch_at_any_depth() -> None:
    store = MemoryStorage()
    pile = ShapePile(
        top=Circle(radius=1.0),
        stack=(Square(side=2.0), Circle(radius=3.0)),
        by_name=frozendict({"a": Square(side=4.0)}),
        maybe=None,
    )
    loaded = await store.load(await store.store(pile))
    assert loaded == pile
    assert isinstance(loaded.stack[0], Square) and isinstance(loaded.stack[1], Circle)
    assert isinstance(loaded.by_name["a"], Square)


def test_tagged_subpart_wire_form_uses_type_id_string() -> None:
    data = DEFAULT_CONVERTER.unstructure_entity(Circle(radius=1.5))
    assert data == {"$type": "test.fxtr.circle", "radius": 1.5}


def test_tagged_subpart_structures_standalone() -> None:
    # Subparts convert without any storage involved — tagging is independent of storage.
    data = {"$type": Square._fxtr_require_type_id(), "side": 2.0}
    assert DEFAULT_CONVERTER.structure(data, Circle | Square) == Square(side=2.0)


@pytest.mark.anyio
async def test_tagged_subpart_is_not_storable() -> None:
    store = MemoryStorage()
    circle = Circle(radius=1.0)
    assert not isinstance(circle, Entity)
    with pytest.raises(AttributeError):
        await store.store(circle)  # type: ignore[arg-type]


def test_inline_union_unknown_tag_rejected() -> None:
    bogus = "test.fxtr.bogus"
    with pytest.raises(ConversionError, match="matches no member") as excinfo:
        DEFAULT_CONVERTER.structure({"$type": bogus, "radius": 1.0}, Circle | Square)
    assert isinstance(excinfo.value.__cause__, NoMatchingVariantError)


def test_inline_union_duplicate_type_ids_rejected() -> None:
    @dataclasses.dataclass(frozen=True, kw_only=True)
    class CircleClone(TaggedDataclass, fxtr_type=Circle._fxtr_require_type_id()):
        radius: float

    with pytest.raises(ConversionError, match="same type tag"):
        DEFAULT_CONVERTER.structure(
            {"$type": Circle._fxtr_require_type_id(), "radius": 1.0}, Circle | CircleClone
        )


# ---- union refs ----


@pytest.mark.anyio
async def test_union_ref_dispatch() -> None:
    store = MemoryStorage()
    leaf_ref = await store.store(Leaf(label="l"))
    other_ref = await store.store(OtherLeaf(number=7))
    for target_ref, expected in ((leaf_ref, Leaf(label="l")), (other_ref, OtherLeaf(number=7))):
        holder = UnionHolder(ref=BoundID(entity_id=target_ref.entity_id, loader=target_ref.loader))
        loaded = await store.load(await store.store(holder))
        assert await store.load(loaded.ref) == expected


@pytest.mark.anyio
async def test_union_ref_no_match_errors() -> None:
    store = MemoryStorage()
    node, _ = _make_node(store)
    node_ref = await store.store(node)
    loader = DEFAULT_ENTITY_LOADER_REGISTRY.loader_for(Leaf | OtherLeaf)
    with pytest.raises(NoMatchingVariantError):
        await store.load(node_ref.entity_id, with_loader=loader)


@dataclasses.dataclass(frozen=True, kw_only=True)
class LeafImposter(DataclassEntity, fxtr_type=Leaf._fxtr_require_type_id()):
    """Distinct Python class claiming the same entity type id as Leaf."""

    label: str


@pytest.mark.anyio
async def test_union_ref_ambiguity_errors() -> None:
    store = MemoryStorage()
    leaf_ref = await store.store(Leaf(label="l"))
    loader = DEFAULT_ENTITY_LOADER_REGISTRY.loader_for(Leaf | LeafImposter)
    with pytest.raises(AmbiguousVariantError):
        await store.load(leaf_ref.entity_id, with_loader=loader)


# ---- registry integration ----


type LeafAlias = Leaf

type LeafLike = Leaf | LeafImposter
"""Ambiguous as a plain union (same type id); a factory registered for the alias resolves it.

No OverrideFxtrHandlers sentinel is needed because the DataclassEntity base factory is
registered with allow_implicit_overrides=True.
"""


@dataclasses.dataclass(frozen=True)
class _PreferLeafFactory:
    def build_loader(self, annotation: Any) -> Any:
        return DEFAULT_ENTITY_LOADER_REGISTRY.loader_for(Leaf)


DEFAULT_ENTITY_LOADER_REGISTRY.register(LeafLike, _PreferLeafFactory())


@dataclasses.dataclass(frozen=True, kw_only=True)
class SpecialLeaf(DataclassEntity, fxtr_type="test.fxtr.specialLeaf"):
    label: str


class _UpperLeafLoader(StaticTypeLoader[SpecialLeaf]):
    """Custom loader demonstrating a subclass-specific registry override."""

    _static_type_id = SpecialLeaf._fxtr_require_type_id()

    def convert(self, data: StorableFxtrDataValue) -> SpecialLeaf:
        loaded = DEFAULT_CONVERTER.structure(data, SpecialLeaf)
        return dataclasses.replace(loaded, label=loaded.label.upper())


@dataclasses.dataclass(frozen=True)
class _UpperLeafFactory:
    def build_loader(self, annotation: Any) -> Any:
        return _UpperLeafLoader()


DEFAULT_ENTITY_LOADER_REGISTRY.register(SpecialLeaf, _UpperLeafFactory())


@pytest.mark.anyio
async def test_alias_resolves_through_registry() -> None:
    store = MemoryStorage()
    leaf_ref = await store.store(Leaf(label="a"))
    loader = DEFAULT_ENTITY_LOADER_REGISTRY.loader_for(LeafAlias)
    assert await store.load(leaf_ref.entity_id, with_loader=loader) == Leaf(label="a")


@pytest.mark.anyio
async def test_alias_with_custom_factory_overrides_union() -> None:
    store = MemoryStorage()
    leaf_ref = await store.store(Leaf(label="a"))
    loader = DEFAULT_ENTITY_LOADER_REGISTRY.loader_for(LeafLike)
    assert await store.load(leaf_ref.entity_id, with_loader=loader) == Leaf(label="a")


@pytest.mark.anyio
async def test_subclass_specific_factory_overrides_base() -> None:
    store = MemoryStorage()
    ref = await store.store(SpecialLeaf(label="quiet"))
    loaded = await store.load(ref)
    assert loaded == SpecialLeaf(label="QUIET")


@pytest.mark.anyio
async def test_unknown_entity_wildcard_loader() -> None:
    store = MemoryStorage()
    leaf_ref = await store.store(Leaf(label="x"))
    loader = DEFAULT_ENTITY_LOADER_REGISTRY.loader_for(UnknownEntity)
    loaded = await store.load(leaf_ref.entity_id, with_loader=loader)
    assert isinstance(loaded, UnknownEntity)
    assert (await store.store(loaded)).entity_id == leaf_ref.entity_id


@pytest.mark.anyio
async def test_bind_type_builds_loadable_bound_id() -> None:
    store = MemoryStorage()
    stored_id = (await store.store(OtherLeaf(number=3))).entity_id
    ref = stored_id.bind_type(Leaf | OtherLeaf)
    assert await store.load(ref) == OtherLeaf(number=3)
    # Equal annotations share cached loaders, so equal-content bound ids compare equal.
    assert ref == stored_id.bind_type(Leaf | OtherLeaf)
    # bind_loader takes the loader directly.
    assert ref == stored_id.bind_loader(DEFAULT_ENTITY_LOADER_REGISTRY.loader_for(Leaf | OtherLeaf))


@pytest.mark.anyio
async def test_load_with_as_type_and_with_loader() -> None:
    store = MemoryStorage()
    bound = await store.store(OtherLeaf(number=3))
    bare = bound.entity_id
    # A bare id needs a loader from somewhere.
    assert await store.load(bare, as_type=OtherLeaf) == OtherLeaf(number=3)
    assert await store.load(bare, with_loader=bound.loader) == OtherLeaf(number=3)
    with pytest.raises(TypeError, match="needs as_type"):
        await store.load(bare)  # type: ignore[call-overload]
    # Either keyword overrides a bound id's own loader.
    unknown = await store.load(bound, as_type=UnknownEntity)
    assert isinstance(unknown, UnknownEntity)
    assert isinstance(await store.load(bound, with_loader=UNKNOWN_ENTITY_LOADER), UnknownEntity)
    with pytest.raises(TypeError, match="at most one"):
        await store.load(bare, as_type=OtherLeaf, with_loader=bound.loader)  # type: ignore[call-overload]


@pytest.mark.anyio
async def test_bind_type_to_a_concrete_annotation() -> None:
    store = MemoryStorage()
    wrapper = Wrapper(inner=Circle(radius=2.0))
    # Wrapper's type parameter has no default, so its store-produced loader cannot decode it;
    # bind the id to the concrete annotation instead.
    entity_id = (await store.store(wrapper)).entity_id
    assert await store.load(entity_id.bind_type(Wrapper[Circle])) == wrapper


# ---- strictness ----


def test_extra_keys_rejected() -> None:
    data = {"$type": Leaf._fxtr_require_type_id(), "label": "x", "sneaky": 1}
    with pytest.raises(ConversionError) as excinfo:
        DEFAULT_CONVERTER.structure(data, Leaf)
    # cattrs' field-validation group arrives as the cause; the extra key is named inside.
    assert "sneaky" in repr(getattr(excinfo.value.__cause__, "exceptions", ()))


def test_wrong_type_tag_rejected() -> None:
    data = {"$type": OtherLeaf._fxtr_require_type_id(), "label": "x"}
    with pytest.raises(StrictTypeError, match="does not match"):
        DEFAULT_CONVERTER.structure(data, Leaf)


def test_missing_type_tag_rejected() -> None:
    with pytest.raises(Exception):
        DEFAULT_CONVERTER.structure({"label": "x"}, Leaf)


def test_builtin_string_tag_rejected_for_dataclass_target() -> None:
    with pytest.raises(StrictTypeError, match="does not match"):
        DEFAULT_CONVERTER.structure({"$type": "fxtr.builtin.testing.unrelated", "label": "x"}, Leaf)


def test_primitive_coercion_rejected() -> None:
    with pytest.raises(Exception):
        DEFAULT_CONVERTER.structure({"$type": Leaf._fxtr_require_type_id(), "label": 3}, Leaf)
    with pytest.raises(Exception):
        DEFAULT_CONVERTER.structure(
            {"$type": OtherLeaf._fxtr_require_type_id(), "number": True}, OtherLeaf
        )


# ---- generics ----


@pytest.mark.anyio
async def test_generic_entity_default_ref_cannot_resolve_abstract_bound() -> None:
    # Storing a generic entity infers ``Wrapper[TaggedDataclass]`` (the bound) as the default
    # loading annotation; an abstract tagged class cannot be decoded, so loading through the
    # default ref fails. Loading with an explicit annotation works.
    store = MemoryStorage()
    wrapper = Wrapper(inner=Circle(radius=5.0))
    ref = await store.store(wrapper)
    with pytest.raises(ConversionError, match="does not declare fxtr_type"):
        await store.load(ref)
    loader = DEFAULT_ENTITY_LOADER_REGISTRY.loader_for(Wrapper[Circle])
    assert await store.load(ref.entity_id, with_loader=loader) == wrapper


@pytest.mark.anyio
async def test_generic_entity_explicit_annotation() -> None:
    store = MemoryStorage()
    wrapper = Wrapper(inner=Square(side=2.0))
    entity_id = (await store.store(wrapper)).entity_id
    loader = DEFAULT_ENTITY_LOADER_REGISTRY.loader_for(Wrapper[Square])
    loaded = await store.load(entity_id, with_loader=loader)
    assert loaded == wrapper


def test_bare_generic_structure_rejected_at_hook_build() -> None:
    # Unstructuring dispatches on the bare runtime class and works (serialization never depends
    # on type parameters); structuring as a bare generic is rejected at hook-build time with an
    # informative error, like a bare BoundID field.
    data = DEFAULT_CONVERTER.unstructure_entity(Wrapper(inner=Circle(radius=1.0)))
    with pytest.raises(StrictTypeError, match="without type parameters"):
        DEFAULT_CONVERTER.structure(data, Wrapper)


@dataclasses.dataclass(frozen=True, kw_only=True)
class BareRefHolder(DataclassEntity, fxtr_type="test.fxtr.bareRefHolder"):
    ref: BoundID  # type: ignore[type-arg]


@dataclasses.dataclass(frozen=True, kw_only=True)
class AnyRefHolder(DataclassEntity, fxtr_type="test.fxtr.anyRefHolder"):
    ref: BoundID[Any]


@pytest.mark.anyio
async def test_bare_entity_ref_field_rejected_informatively() -> None:
    store = MemoryStorage()
    leaf_ref = await store.store(Leaf(label="x"))
    data = {"$type": BareRefHolder._fxtr_require_type_id(), "ref": leaf_ref.entity_id}
    with pytest.raises(StrictTypeError, match="bare BoundID"):
        DEFAULT_CONVERTER.structure(data, BareRefHolder)


@pytest.mark.anyio
async def test_entity_ref_any_field_rejected_informatively() -> None:
    store = MemoryStorage()
    leaf_ref = await store.store(Leaf(label="x"))
    data = {"$type": AnyRefHolder._fxtr_require_type_id(), "ref": leaf_ref.entity_id}
    with pytest.raises(StrictTypeError, match=r"BoundID\[Any\]"):
        DEFAULT_CONVERTER.structure(data, AnyRefHolder)


# ---- collections ----


@dataclasses.dataclass(frozen=True, kw_only=True)
class Collections(DataclassEntity, fxtr_type="test.fxtr.collections"):
    items: tuple[int, ...]
    tags: frozenset[str]
    meta: frozendict[str, int]


@pytest.mark.anyio
async def test_collections_roundtrip_and_wire_form() -> None:
    store = MemoryStorage()
    entity = Collections(
        items=(3, 1, 2),
        tags=frozenset({"b", "a", "c"}),
        meta=frozendict({"k": 1}),
    )
    data = DEFAULT_CONVERTER.unstructure_entity(entity)
    assert data["items"] == [3, 1, 2] and type(data["items"]) is list
    assert data["tags"] == ["a", "b", "c"]  # canonical sorted order
    assert data["meta"] == {"k": 1} and type(data["meta"]) is dict
    loaded = await store.load(await store.store(entity))
    assert loaded == entity
    assert type(loaded.meta) is frozendict


@dataclasses.dataclass(frozen=True, kw_only=True)
class RefCollections(DataclassEntity, fxtr_type="test.fxtr.refCollections"):
    refs: tuple[BoundID[Leaf], ...]
    ref_set: frozenset[BoundID[Leaf]]
    maybe_ref: BoundID[Leaf] | None


@pytest.mark.anyio
async def test_ref_collections_roundtrip() -> None:
    store = MemoryStorage()
    ref_a = await store.store(Leaf(label="a"))
    ref_b = await store.store(Leaf(label="b"))
    entity = RefCollections(refs=(ref_a, ref_b), ref_set=frozenset({ref_a, ref_b}), maybe_ref=None)
    loaded = await store.load(await store.store(entity))
    assert loaded == entity
    assert [await store.load(r) for r in loaded.refs] == [Leaf(label="a"), Leaf(label="b")]
    with_ref = RefCollections(refs=(), ref_set=frozenset(), maybe_ref=ref_a)
    loaded_with = await store.load(await store.store(with_ref))
    assert loaded_with.maybe_ref is not None
    assert await store.load(loaded_with.maybe_ref) == Leaf(label="a")


@pytest.mark.anyio
async def test_ref_set_serializes_in_canonical_order() -> None:
    store = MemoryStorage()
    ref_a = await store.store(Leaf(label="a"))
    ref_b = await store.store(Leaf(label="b"))
    entity = RefCollections(refs=(), ref_set=frozenset({ref_a, ref_b}), maybe_ref=None)
    data = DEFAULT_CONVERTER.unstructure_entity(entity)
    expected = sorted([ref_a.entity_id, ref_b.entity_id], key=lambda i: i.cid)
    assert data["ref_set"] == expected


# ---- explicit loaders and local ids ----


@pytest.mark.anyio
async def test_load_with_an_explicit_loader() -> None:
    store = MemoryStorage()
    wrapper = Wrapper(inner=Square(side=1.0))
    entity_id = (await store.store(wrapper)).entity_id
    loader = DEFAULT_ENTITY_LOADER_REGISTRY.loader_for(Wrapper[Square])
    assert await store.load(entity_id, with_loader=loader) == wrapper


def test_content_id_matches_stored_id() -> None:
    wrapper = Wrapper(inner=Square(side=2.0))
    assert MemoryStorage().store_sync(wrapper).entity_id == wrapper.content_id()


# ---- the serialization-hook protocol ----


class Angle:
    """Custom leaf value type implementing the serialization-hook protocol."""

    def __init__(self, degrees: float) -> None:
        self.degrees = degrees

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Angle) and other.degrees == self.degrees

    @classmethod
    def __fxtr_make_structure_hook__(cls, conv: cattrs.Converter, annotation: Any) -> StructureHook:
        def structure(value: Any, _target: Any) -> Angle:
            if type(value) is not float:
                raise StrictTypeError(f"Expected float, got {type(value).__name__}")
            return cls(value)

        return structure

    @classmethod
    def __fxtr_make_unstructure_hook__(
        cls, conv: cattrs.Converter, annotation: Any
    ) -> UnstructureHook:
        def unstructure(value: Angle) -> float:
            return value.degrees

        return unstructure


@dataclasses.dataclass(frozen=True, kw_only=True)
class HasAngle(DataclassEntity, fxtr_type="test.fxtr.hasAngle"):
    angle: Angle


@pytest.mark.anyio
async def test_protocol_value_type_roundtrip() -> None:
    store = MemoryStorage()
    entity = HasAngle(angle=Angle(90.0))
    data = DEFAULT_CONVERTER.unstructure_entity(entity)
    assert data["angle"] == 90.0
    loaded = await store.load(await store.store(entity))
    assert loaded == entity


def test_protocol_value_type_strictness_is_its_own() -> None:
    data = {"$type": HasAngle._fxtr_require_type_id(), "angle": "not a float"}
    with pytest.raises(ConversionError) as excinfo:
        DEFAULT_CONVERTER.structure(data, HasAngle)
    # The class's own check, raised inside a field hook, arrives inside the cause group.
    assert "Expected float" in repr(getattr(excinfo.value.__cause__, "exceptions", ()))


@dataclasses.dataclass(frozen=True, kw_only=True)
class RenamedField(DataclassEntity, fxtr_type="test.fxtr.renamedField"):
    """Tagged class customizing its content hooks: one field renamed on the wire."""

    inner_name: str

    @classmethod
    def __fxtr_make_structure_hook__(cls, conv: cattrs.Converter, annotation: Any) -> StructureHook:
        return make_dict_structure_fn(annotation, conv, inner_name=override(rename="innerName"))

    @classmethod
    def __fxtr_make_unstructure_hook__(
        cls, conv: cattrs.Converter, annotation: Any
    ) -> UnstructureHook:
        return make_dict_unstructure_fn(annotation, conv, inner_name=override(rename="innerName"))


@pytest.mark.anyio
async def test_tagged_class_content_hooks_override() -> None:
    # The class controls its field format via the protocol; the framework still applies the
    # $type layer around the returned hooks (spliced on serialize, validated + stripped on
    # deserialize), so tag mechanics cannot be customized away.
    store = MemoryStorage()
    entity = RenamedField(inner_name="x")
    data = DEFAULT_CONVERTER.unstructure_entity(entity)
    assert data == {"$type": RenamedField._fxtr_require_type_id(), "innerName": "x"}
    assert await store.load(await store.store(entity)) == entity
    with pytest.raises(StrictTypeError, match="does not match"):
        DEFAULT_CONVERTER.structure(
            {"$type": Leaf._fxtr_require_type_id(), "innerName": "x"}, RenamedField
        )


@dataclasses.dataclass(frozen=True, kw_only=True)
class TagMeddler(DataclassEntity, fxtr_type="test.fxtr.tagMeddler"):
    x: int

    @classmethod
    def __fxtr_make_structure_hook__(cls, conv: cattrs.Converter, annotation: Any) -> StructureHook:
        return make_dataclass_structure_hook(conv, annotation)

    @classmethod
    def __fxtr_make_unstructure_hook__(
        cls, conv: cattrs.Converter, annotation: Any
    ) -> UnstructureHook:
        unstructure = make_dataclass_unstructure_hook(conv, annotation)

        def bad_unstructure(value: Any) -> Any:
            return {**unstructure(value), "$type": "sneaky"}

        return bad_unstructure


def test_content_hooks_must_not_emit_tag() -> None:
    with pytest.raises(StrictTypeError, match="framework-managed"):
        DEFAULT_CONVERTER.unstructure_entity(TagMeddler(x=1))


class HalfHooked:
    """Untagged class defining only one direction of the hook pair."""

    @classmethod
    def __fxtr_make_unstructure_hook__(
        cls, conv: cattrs.Converter, annotation: Any
    ) -> UnstructureHook:
        return lambda value: 1.0


@dataclasses.dataclass(frozen=True, kw_only=True)
class HasHalfHooked(DataclassEntity, fxtr_type="test.fxtr.hasHalfHooked"):
    value: HalfHooked


def test_half_defined_hook_pair_rejected() -> None:
    # Defining either method routes the class through the protocol, and the factory then
    # requires both — a class must not be serializable without being deserializable.
    with pytest.raises(ConversionError, match="must define both"):
        DEFAULT_CONVERTER.unstructure_entity(HasHalfHooked(value=HalfHooked()))


def test_tagged_class_body_must_customize_both_directions() -> None:
    # For tagged classes the defaults are inherited, so a half-override would silently pair a
    # custom half with a default half; it is rejected at class definition instead.
    with pytest.raises(TypeError, match="customized together"):

        @dataclasses.dataclass(frozen=True, kw_only=True)
        class HalfCustom(DataclassEntity, fxtr_type="test.fxtr.halfCustom"):
            @classmethod
            def __fxtr_make_structure_hook__(
                cls, conv: cattrs.Converter, annotation: Any
            ) -> StructureHook:
                return make_dataclass_structure_hook(conv, annotation)


# ---- strict field presence ----


@dataclasses.dataclass(frozen=True, kw_only=True)
class HasDefault(DataclassEntity, fxtr_type="test.fxtr.hasDefault"):
    name: str
    count: int = 0


def test_defaulted_fields_always_serialized() -> None:
    data = DEFAULT_CONVERTER.unstructure_entity(HasDefault(name="n"))
    assert data == {"$type": HasDefault._fxtr_require_type_id(), "name": "n", "count": 0}


def test_missing_defaulted_field_rejected() -> None:
    # A canonical writer always emits every field, so data missing one was written under a
    # different schema; silently filling the default would alias two distinct wire forms to
    # one Python value.
    data = {"$type": HasDefault._fxtr_require_type_id(), "name": "n"}
    with pytest.raises(StrictTypeError, match="required"):
        DEFAULT_CONVERTER.structure(data, HasDefault)


# ---- type entities and NSIDs ----


def test_subclass_does_not_inherit_parent_type_id() -> None:
    @dataclasses.dataclass(frozen=True, kw_only=True)
    class LeafChild(Leaf):
        pass

    assert LeafChild._fxtr_known_type_id is None
    assert LeafChild.fxtr_type_id() is None
    with pytest.raises(ConversionError, match="does not declare fxtr_type"):
        DEFAULT_CONVERTER.unstructure_entity(LeafChild(label="x"))


@pytest.mark.anyio
async def test_fxtr_type_id_and_static_type_id() -> None:
    # A class knows its type statically when it declared fxtr_type; inline tagged values too.
    assert Leaf.fxtr_type_id() == "test.fxtr.leaf"
    assert Circle.fxtr_type_id() == "test.fxtr.circle"
    # Not known statically: wildcard, no declared tag.
    assert UnknownEntity.fxtr_type_id() is None
    assert Entity.fxtr_type_id() is None
    # A loader for one type reports it; composite and wildcard loaders do not.
    assert DEFAULT_ENTITY_LOADER_REGISTRY.loader_for(Leaf).static_type_id == Leaf.fxtr_type_id()
    assert DEFAULT_ENTITY_LOADER_REGISTRY.loader_for(Leaf | OtherLeaf).static_type_id is None
    assert UNKNOWN_ENTITY_LOADER.static_type_id is None
    ref = await MemoryStorage().store(Leaf(label="x"))
    assert ref.loader.static_type_id == Leaf.fxtr_type_id()


def test_fxtr_type_must_be_a_non_empty_string() -> None:
    for bad in ("", 42, b"bytes"):
        with pytest.raises(TypeError, match="non-empty string"):

            @dataclasses.dataclass(frozen=True, kw_only=True)
            class Bad(TaggedDataclass, fxtr_type=cast("Any", bad)):
                pass


# ---- serialization dispatches on the runtime value, not the annotation ----
#
# cattrs bakes each field's *declared* type hook and does not runtime-dispatch on its own, so a
# value whose runtime type differs from the slot's declared type (a subclass in a base-typed or
# bound-typed type-parameter slot) must not be silently re-serialized under the declared type.
# The converter re-dispatches on the value's own type; these lock that in.


@dataclasses.dataclass(frozen=True, kw_only=True)
class ShapeBase(TaggedDataclass, fxtr_type="test.fxtr.shapeBase"):
    name: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class ShapeSub(ShapeBase, fxtr_type="test.fxtr.shapeSub"):
    extra: int


@dataclasses.dataclass(frozen=True, kw_only=True)
class HoldsBaseShape(DataclassEntity, fxtr_type="test.fxtr.holdsBaseShape"):
    inner: ShapeBase


def test_subclass_in_base_slot_serializes_as_subclass() -> None:
    # A subclass in a base-typed slot keeps its own $type and its own fields (nothing dropped).
    data = DEFAULT_CONVERTER.unstructure_entity(HoldsBaseShape(inner=ShapeSub(name="x", extra=7)))
    inner = data["inner"]
    assert isinstance(inner, dict)
    assert inner["$type"] == ShapeSub._fxtr_require_type_id()
    assert inner["extra"] == 7


@dataclasses.dataclass(frozen=True, kw_only=True)
class BoundedWrap[T: ShapeBase](DataclassEntity, fxtr_type="test.fxtr.boundedWrap"):
    inner: T


def test_subclass_in_bound_type_param_slot_serializes_as_subclass() -> None:
    # The default loading annotation is BoundedWrap[ShapeBase], but the runtime value is a
    # ShapeSub: it must serialize as ShapeSub.
    data = DEFAULT_CONVERTER.unstructure_entity(BoundedWrap(inner=ShapeSub(name="x", extra=7)))
    inner = data["inner"]
    assert isinstance(inner, dict)
    assert inner["$type"] == ShapeSub._fxtr_require_type_id()
    assert inner["extra"] == 7


@pytest.mark.anyio
async def test_bound_type_param_roundtrips_with_explicit_annotation() -> None:
    store = MemoryStorage()
    wrapper = BoundedWrap(inner=ShapeSub(name="x", extra=7))
    ref = await store.store(wrapper)
    loader = DEFAULT_ENTITY_LOADER_REGISTRY.loader_for(BoundedWrap[ShapeSub])
    loaded = await store.load(ref.entity_id, with_loader=loader)
    assert loaded == wrapper
    assert (await store.store(loaded)).entity_id == ref.entity_id


def test_non_tagged_value_in_tagged_slot_rejected_at_serialize() -> None:
    # A tagged slot must yield a mapping; a stray non-tagged value is caught at serialize time.
    @dataclasses.dataclass(frozen=True, kw_only=True)
    class HoldsShape(DataclassEntity, fxtr_type="test.fxtr.holdsShape2"):
        inner: Circle

    bogus = HoldsShape(inner=5)  # type: ignore[arg-type]
    with pytest.raises(StrictTypeError, match="not a serializable tagged value"):
        DEFAULT_CONVERTER.unstructure_entity(bogus)


class _BaseLeaf:
    """Untagged custom leaf value type; its subclass overrides the wire form."""

    def __init__(self, v: int) -> None:
        self.v = v

    def __eq__(self, other: object) -> bool:
        return type(self) is type(other) and self.v == other.v  # type: ignore[attr-defined]

    @classmethod
    def __fxtr_make_structure_hook__(cls, conv: cattrs.Converter, annotation: Any) -> StructureHook:
        return lambda value, _t: cls(value["base"])

    @classmethod
    def __fxtr_make_unstructure_hook__(
        cls, conv: cattrs.Converter, annotation: Any
    ) -> UnstructureHook:
        return lambda value: {"base": value.v}


class _SubLeaf(_BaseLeaf):
    @classmethod
    def __fxtr_make_structure_hook__(cls, conv: cattrs.Converter, annotation: Any) -> StructureHook:
        return lambda value, _t: cls(value["sub"])

    @classmethod
    def __fxtr_make_unstructure_hook__(
        cls, conv: cattrs.Converter, annotation: Any
    ) -> UnstructureHook:
        return lambda value: {"sub": value.v}


@dataclasses.dataclass(frozen=True, kw_only=True)
class HoldsLeaf(DataclassEntity, fxtr_type="test.fxtr.holdsLeaf"):
    leaf: _BaseLeaf


def test_leaf_subclass_serializes_by_runtime_type() -> None:
    # The untagged-leaf path dispatches on runtime type too: the subclass's own hook runs.
    data = DEFAULT_CONVERTER.unstructure_entity(HoldsLeaf(leaf=_SubLeaf(5)))
    assert data["leaf"] == {"sub": 5}


# ---- mutable raw-data alias fields ----


@dataclasses.dataclass(frozen=True, kw_only=True)
class RawHolder(DataclassEntity, fxtr_type="test.fxtr.rawHolder"):
    payload: dict[str, FxtrDataValue]


@pytest.mark.anyio
async def test_mutable_raw_data_field_roundtrips() -> None:
    # The mutable FxtrDataValue alias structures (and re-serializes) arbitrary wire data, keeping
    # bare EntityID references and producing plain mutable containers.
    store = MemoryStorage()
    leaf_ref = await store.store(Leaf(label="x"))
    holder = RawHolder(payload={"n": 1, "s": "hi", "lst": [1, 2], "id": leaf_ref.entity_id})
    loaded = await store.load(await store.store(holder))
    assert loaded == holder
    assert type(loaded.payload) is dict and type(loaded.payload["lst"]) is list


# ---- unions of primitives ----


@dataclasses.dataclass(frozen=True, kw_only=True)
class KeyHolder(DataclassEntity, fxtr_type="test.fxtr.keyHolder"):
    key: int | str
    maybe: int | str | None


def test_primitive_union_field_structures_by_exact_type() -> None:
    for holder in (KeyHolder(key=1, maybe=None), KeyHolder(key="k", maybe="m")):
        data = DEFAULT_CONVERTER.unstructure_entity(holder)
        assert DEFAULT_CONVERTER.structure(data, KeyHolder) == holder
    data = DEFAULT_CONVERTER.unstructure_entity(KeyHolder(key=1, maybe=None))
    with pytest.raises(ConversionError):
        DEFAULT_CONVERTER.structure({**data, "key": 1.5}, KeyHolder)
    with pytest.raises(ConversionError):
        DEFAULT_CONVERTER.structure({**data, "key": True}, KeyHolder)  # bool is not int
    with pytest.raises(ConversionError):
        DEFAULT_CONVERTER.structure({**data, "key": None}, KeyHolder)  # None only where allowed


# ---- strict JSON fields ----


@dataclasses.dataclass(frozen=True)
class JSONHolder(DataclassEntity, fxtr_type="test.fxtr.jsonHolder"):
    metadata: JSONObject
    value: JSONValue


@pytest.mark.anyio
async def test_json_fields_preserve_scalar_types_and_container_shapes() -> None:
    store = MemoryStorage()
    payload: JSONObject = {
        "scores": [1, 0.5, True, None],
        "nested": [{"text": "hello", "empty": []}, {}],
        "minimum": -(2**64),
        "maximum": 2**64 - 1,
    }
    entity = JSONHolder(metadata=payload, value=[False, {}, []])
    saved = await store.store(entity)
    restored = await store.load(saved)
    assert restored == entity
    assert isinstance(restored.metadata["scores"], list)
    assert [type(x) for x in restored.metadata["scores"]] == [int, float, bool, type(None)]
    assert (await store.store(restored)).entity_id == saved.entity_id
    assert DEFAULT_CONVERTER.structure({}, JSONObject) == {}


@pytest.mark.parametrize("bad", [b"bytes", {1: "non-string key"}])
def test_json_rejects_non_json_values_on_save_and_load(bad: Any) -> None:
    store = MemoryStorage()
    with pytest.raises(ConversionError):
        store.store_sync(JSONHolder(metadata={"nested": [bad]}, value=None))
    with pytest.raises(ConversionError):
        DEFAULT_CONVERTER.structure({"nested": [bad]}, JSONObject)


@pytest.mark.parametrize("frozen_shape", [(1, 2), frozendict({"a": 1})])
def test_json_saves_only_mutable_shapes_but_loads_frozen_ones(frozen_shape: Any) -> None:
    # Saving a tuple into a list-shaped slot would load back unequal, so it is rejected; loading
    # accepts the frozen form of the same data (EntityLoader.convert may be given frozen data).
    with pytest.raises(ConversionError):
        MemoryStorage().store_sync(JSONHolder(metadata={"nested": [frozen_shape]}, value=None))
    restored = DEFAULT_CONVERTER.structure({"nested": [frozen_shape]}, JSONObject)
    assert type(restored["nested"][0]) is (list if isinstance(frozen_shape, tuple) else dict)


def test_json_loader_convert_accepts_frozen_data() -> None:
    entity = JSONHolder(metadata={"scores": [1, {"x": None}]}, value=[True])
    frozen = freeze_fxtr_data(DEFAULT_CONVERTER.unstructure_entity(entity))
    assert DEFAULT_ENTITY_LOADER_REGISTRY.loader_for(JSONHolder).convert(frozen) == entity


def test_json_rejects_entity_references_and_non_object_roots() -> None:
    store = MemoryStorage()
    ref = store.store_sync(Leaf(label="reference"))
    for bad in (ref, ref.entity_id):
        with pytest.raises(ConversionError):
            store.store_sync(JSONHolder(metadata={"ref": bad}, value=None))  # type: ignore[arg-type]
        with pytest.raises(ConversionError):
            DEFAULT_CONVERTER.structure(bad, JSONValue)
    for bad in ([], None, "text"):
        with pytest.raises(ConversionError):
            store.store_sync(JSONHolder(metadata=bad, value=None))  # type: ignore[arg-type]
        with pytest.raises(ConversionError):
            DEFAULT_CONVERTER.structure(bad, JSONObject)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), 2**64, -(2**64) - 1])
def test_json_fields_retain_storage_numeric_limits(bad: float | int) -> None:
    with pytest.raises(ValueError):
        MemoryStorage().store_sync(JSONHolder(metadata={"measurement": bad}, value=None))


@dataclasses.dataclass(frozen=True)
class FrozenJSONHolder(DataclassEntity, fxtr_type="test.fxtr.frozenJSONHolder"):
    metadata: FrozenJSONObject
    value: FrozenJSONValue


@pytest.mark.anyio
async def test_frozen_json_fields_round_trip_as_hashable_values() -> None:
    store = MemoryStorage()
    entity = FrozenJSONHolder(
        metadata=frozendict({"scores": (1, 0.5, True, None), "nested": (frozendict(), ())}),
        value=(False, frozendict({"a": "b"})),
    )
    restored = await store.load(await store.store(entity))
    assert restored == entity
    assert hash(restored) == hash(entity)
    assert type(restored.metadata) is frozendict
    nested = cast(tuple[Any, ...], restored.metadata["nested"])
    assert type(nested) is tuple
    assert type(nested[0]) is frozendict
    scores = cast(tuple[Any, ...], restored.metadata["scores"])
    assert [type(x) for x in scores] == [int, float, bool, type(None)]


def test_frozen_and_mutable_json_fields_share_wire_data() -> None:
    frozen = FrozenJSONHolder(metadata=frozendict({"a": (1, frozendict())}), value=("x",))
    mutable = JSONHolder(metadata={"a": [1, {}]}, value=["x"])
    frozen_data = DEFAULT_CONVERTER.unstructure_entity(frozen)
    mutable_data = DEFAULT_CONVERTER.unstructure_entity(mutable)
    assert frozen_data.pop("$type") != mutable_data.pop("$type")
    assert frozen_data == mutable_data
    assert type(frozen_data["metadata"]) is dict
    assert type(cast(dict[str, Any], frozen_data["metadata"])["a"]) is list


@pytest.mark.parametrize(
    ("metadata", "value"),
    [
        ({"a": 1}, None),  # mutable root
        (frozendict({"a": [1]}), None),  # mutable nested list
        (frozendict(), {"a": 1}),  # mutable value
        (frozendict({"a": b"bytes"}), None),  # not a JSON value
    ],
)
def test_frozen_json_rejects_mutable_shapes_and_non_json_on_save(metadata: Any, value: Any) -> None:
    with pytest.raises(ConversionError):
        MemoryStorage().store_sync(FrozenJSONHolder(metadata=metadata, value=value))


def test_frozen_json_structures_either_shape_to_frozen() -> None:
    for data in ({"a": [1, {"b": None}]}, frozendict({"a": (1, frozendict({"b": None}))})):
        restored = DEFAULT_CONVERTER.structure(data, FrozenJSONObject)
        assert restored == frozendict({"a": (1, frozendict({"b": None}))})
        assert type(restored["a"]) is tuple
    with pytest.raises(ConversionError):
        DEFAULT_CONVERTER.structure([1], FrozenJSONObject)


@dataclasses.dataclass(frozen=True)
class WrappedJSONHolder(DataclassEntity, fxtr_type="test.fxtr.wrappedJSONHolder"):
    by_name: frozendict[str, JSONValue]
    tags: frozenset[JSONValue]


@pytest.mark.parametrize(
    ("by_name", "tags"),
    [
        (frozendict({"x": b"bytes"}), frozenset()),
        (frozendict({"x": (1, 2)}), frozenset()),  # would load back as a list
        (frozendict(), frozenset({b"a", b"b"})),
    ],
)
def test_json_values_inside_frozendict_and_frozenset_are_checked_on_save(
    by_name: Any, tags: Any
) -> None:
    with pytest.raises(ConversionError):
        MemoryStorage().store_sync(WrappedJSONHolder(by_name=by_name, tags=tags))


@pytest.mark.anyio
async def test_json_values_inside_frozendict_and_frozenset_round_trip() -> None:
    store = MemoryStorage()
    entity = WrappedJSONHolder(by_name=frozendict({"x": [1, {"y": None}]}), tags=frozenset({"a"}))
    assert await store.load(await store.store(entity)) == entity


@dataclasses.dataclass(frozen=True)
class GenericCircleMap[CircleT: Circle](DataclassEntity, fxtr_type="test.fxtr.genericCircleMap"):
    by_name: frozendict[str, CircleT]


def test_frozendict_of_type_variable_serializes_by_runtime_type() -> None:
    entity = GenericCircleMap(by_name=frozendict({"a": Circle(radius=1.0)}))
    data = DEFAULT_CONVERTER.unstructure_entity(entity)
    assert data["by_name"] == {"a": {"$type": "test.fxtr.circle", "radius": 1.0}}
    MemoryStorage().store_sync(entity)


# ---- the load-back check on serialize ----
#
# Serializing an entity decodes the data back as its class, so a value the strict decoder would
# reject fails when it is stored rather than when it is next read.


@dataclasses.dataclass(frozen=True, kw_only=True)
class FloatHolder(DataclassEntity, fxtr_type="test.fxtr.floatHolder"):
    f: float


@dataclasses.dataclass(frozen=True, kw_only=True)
class LiteralHolder(DataclassEntity, fxtr_type="test.fxtr.literalHolder"):
    mode: Literal["a", "b"]


@dataclasses.dataclass(frozen=True, kw_only=True)
class MixedUnionHolder(DataclassEntity, fxtr_type="test.fxtr.mixedUnionHolder"):
    value: Circle | str


@dataclasses.dataclass(frozen=True, kw_only=True)
class NestedFloatHolder(DataclassEntity, fxtr_type="test.fxtr.nestedFloatHolder"):
    circles: tuple[Circle, ...]


def _undecodable_entities() -> list[Any]:
    return [
        pytest.param(
            FloatHolder(f=cast(Any, 1)), r"Expected float, got int @ \$\.f", id="int-in-float"
        ),
        pytest.param(LiteralHolder(mode=cast(Any, "c")), r"@ \$\.mode", id="outside-literal"),
        pytest.param(
            HoldsBaseShape(inner=ShapeSub(name="x", extra=7)),
            r"'test\.fxtr\.shapeSub' does not match ShapeBase's declared type .* @ \$\.inner",
            id="subclass-in-base-slot",
        ),
        pytest.param(MixedUnionHolder(value="x"), "Unsupported type", id="mixed-union"),
        pytest.param(
            NestedFloatHolder(circles=(Circle(radius=1.0), Circle(radius=cast(Any, 2)))),
            r"@ \$\.circles\[1\]\.radius",
            id="nested",
        ),
    ]


@pytest.mark.parametrize(("entity", "detail"), _undecodable_entities())
def test_undecodable_entity_rejected_on_store(entity: DataclassEntity, detail: str) -> None:
    match = f"{type(entity).__name__} would not load back(?s:.*){detail}"
    with pytest.raises(ConversionError, match=match):
        MemoryStorage().store_sync(entity)
    with pytest.raises(ConversionError, match=match):
        entity.content_id()


def test_load_error_names_each_failing_path() -> None:
    data = {
        "$type": "test.fxtr.nestedFloatHolder",
        "circles": [{"$type": "test.fxtr.circle", "radius": 2}],
    }
    with pytest.raises(
        ConversionError, match=r"Expected float, got int @ \$\.circles\[0\]\.radius"
    ):
        DEFAULT_CONVERTER.structure(data, NestedFloatHolder)


@dataclasses.dataclass(frozen=True, kw_only=True)
class DriftingHooks(DataclassEntity, fxtr_type="test.fxtr.driftingHooks"):
    x: int

    @classmethod
    def __fxtr_make_structure_hook__(cls, conv: cattrs.Converter, annotation: Any) -> StructureHook:
        structure = make_dataclass_structure_hook(conv, annotation)
        return lambda value, target: dataclasses.replace(structure(value, target), x=value["x"] + 1)

    @classmethod
    def __fxtr_make_unstructure_hook__(
        cls, conv: cattrs.Converter, annotation: Any
    ) -> UnstructureHook:
        return make_dataclass_unstructure_hook(conv, annotation)


def test_hooks_that_do_not_reproduce_the_data_rejected_on_store() -> None:
    with pytest.raises(ConversionError, match=r"does not serialize reproducibly.*\['x'\]"):
        MemoryStorage().store_sync(DriftingHooks(x=1))


def test_generic_entity_not_checked() -> None:
    # Loading code chooses a generic entity's type arguments, so a value only some of them accept
    # (here, one the bound's own hook rejects) still stores.
    MemoryStorage().store_sync(BoundedWrap(inner=ShapeBase(name=cast(Any, 1))))


@dataclasses.dataclass(frozen=True, kw_only=True)
class ShapeBaseWrap(BoundedWrap[ShapeBase], fxtr_type="test.fxtr.shapeBaseWrap"):
    pass


def test_non_generic_subclass_of_parameterized_base_checked() -> None:
    MemoryStorage().store_sync(ShapeBaseWrap(inner=ShapeBase(name="x")))
    with pytest.raises(ConversionError, match=r"would not load back(?s:.*)@ \$\.inner"):
        MemoryStorage().store_sync(ShapeBaseWrap(inner=ShapeSub(name="x", extra=7)))


@dataclasses.dataclass(frozen=True, kw_only=True)
class UncheckedFloatHolder(DataclassEntity, fxtr_type="test.fxtr.uncheckedFloatHolder"):
    _fxtr_check_decodes_on_store: ClassVar[bool] = False
    f: float


def test_opted_out_class_not_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    MemoryStorage().store_sync(UncheckedFloatHolder(f=cast(Any, 1)))

    def fail(*_args: object) -> None:
        raise AssertionError("an Array was decoded on store")

    monkeypatch.setattr(dataclass_entity, "_check_loads_back", fail)
    MemoryStorage().store_sync(Array.from_items([((1,), "a")], ("[i: int]", str)))
