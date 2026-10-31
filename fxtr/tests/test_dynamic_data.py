"""Tests for DynamicData: tagged values held undecoded and read back as a requested type."""

from __future__ import annotations

import dataclasses

import pytest
from frozendict import frozendict

from fxtr.core.entities import BoundID
from fxtr.core.entity_data import (
    TYPE_KEY,
    EntityID,
    InvalidEntityDataError,
    freeze_fxtr_data,
)
from fxtr.entity_defns.converter import DEFAULT_CONVERTER
from fxtr.entity_defns.dataclass_bases import ConversionError, StrictTypeError, TaggedDataclass
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.entity_defns.dynamic_data import DynamicData
from fxtr.storage.memory_storage import MemoryStorage


@dataclasses.dataclass(frozen=True, kw_only=True)
class Circle(TaggedDataclass, fxtr_type="test.fxtr.dyn.circle"):
    radius: float


@dataclasses.dataclass(frozen=True, kw_only=True)
class Square(TaggedDataclass, fxtr_type="test.fxtr.dyn.square"):
    side: float


@dataclasses.dataclass(frozen=True, kw_only=True)
class Leaf(DataclassEntity, fxtr_type="test.fxtr.dyn.leaf"):
    label: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class Nested(TaggedDataclass, fxtr_type="test.fxtr.dyn.nested"):
    """A tagged value carrying another tagged value and a reference."""

    shape: Circle
    child: BoundID[Leaf]


@dataclasses.dataclass(frozen=True, kw_only=True)
class Record(DataclassEntity, fxtr_type="test.fxtr.dyn.record"):
    name: str
    metadata: DynamicData


@dataclasses.dataclass(frozen=True, kw_only=True)
class ConcreteRecord(DataclassEntity, fxtr_type=Record._fxtr_require_type_id()):
    """Same type id and fields as Record, with the metadata slot statically typed."""

    name: str
    metadata: Circle


@dataclasses.dataclass(frozen=True, kw_only=True)
class UnionRecord(DataclassEntity, fxtr_type=Record._fxtr_require_type_id()):
    """Same type id and fields as Record, with the metadata slot as a tagged union."""

    name: str
    metadata: Circle | Square


@dataclasses.dataclass(frozen=True, kw_only=True)
class OptionalRecord(DataclassEntity, fxtr_type="test.fxtr.dyn.optionalRecord"):
    metadata: DynamicData | None


# ---- construction and views ----


def test_from_tagged_holds_the_wire_form() -> None:
    dyn = DynamicData.from_tagged(Circle(radius=2.0))
    assert dyn.type == "test.fxtr.dyn.circle"
    assert dyn.data == frozendict({TYPE_KEY: "test.fxtr.dyn.circle", "radius": 2.0})
    assert dyn.data == DEFAULT_CONVERTER.unstructure_entity(Circle(radius=2.0))


def test_from_data_accepts_any_storable_shape() -> None:
    wire = DEFAULT_CONVERTER.unstructure_entity(Circle(radius=2.0))
    assert DynamicData.from_data(wire) == DynamicData.from_tagged(Circle(radius=2.0))
    frozen = freeze_fxtr_data(wire)
    assert isinstance(frozen, frozendict)
    assert DynamicData.from_data(frozen) == DynamicData.from_tagged(Circle(radius=2.0))


def test_from_data_requires_tagged_mapping() -> None:
    with pytest.raises(InvalidEntityDataError):
        DynamicData.from_data({"radius": 2.0})
    with pytest.raises(InvalidEntityDataError):
        DynamicData.from_data([1, 2])  # type: ignore[arg-type]


def test_from_tagged_rejects_untagged_class() -> None:
    @dataclasses.dataclass(frozen=True)
    class Abstract(TaggedDataclass):
        x: int

    with pytest.raises(ConversionError):
        DynamicData.from_tagged(Abstract(x=1))


def test_constructor_validation() -> None:
    tag = Circle._fxtr_require_type_id()
    with pytest.raises(TypeError, match="frozendict"):
        DynamicData(data={TYPE_KEY: tag, "radius": 1.0})  # type: ignore[arg-type]
    for untagged in ({"radius": 1.0}, {TYPE_KEY: "", "radius": 1.0}, {TYPE_KEY: 3}):
        with pytest.raises(InvalidEntityDataError):
            DynamicData(data=frozendict(untagged))


# ---- reading back ----


def test_as_type_decodes_matching_type() -> None:
    dyn = DynamicData.from_tagged(Circle(radius=2.0))
    assert dyn.is_type(Circle)
    assert dyn.as_type(Circle) == Circle(radius=2.0)


def test_as_type_rejects_other_type() -> None:
    dyn = DynamicData.from_tagged(Circle(radius=2.0))
    assert not dyn.is_type(Square)
    with pytest.raises(StrictTypeError, match="does not match"):
        dyn.as_type(Square)


def test_try_as_type_returns_none_on_tag_mismatch_only() -> None:
    dyn = DynamicData.from_tagged(Circle(radius=2.0))
    assert dyn.try_as_type(Square) is None
    assert dyn.try_as_type(Circle) == Circle(radius=2.0)
    if (circle := dyn.try_as_type(Circle)) is not None:
        assert circle.radius == 2.0

    # Matching tag with content that does not decode is corruption, not a different type.
    corrupt = DynamicData(data=frozendict({TYPE_KEY: "test.fxtr.dyn.circle", "radius": "no"}))
    with pytest.raises(ConversionError):
        corrupt.try_as_type(Circle)


def test_as_type_is_strict_about_content() -> None:
    extra = DynamicData(
        data=frozendict({TYPE_KEY: "test.fxtr.dyn.circle", "radius": 1.0, "bogus": 1})
    )
    with pytest.raises(ConversionError):
        extra.as_type(Circle)


def test_is_type_requires_declared_tag() -> None:
    class Abstract(TaggedDataclass):
        pass

    with pytest.raises(TypeError, match="does not declare"):
        DynamicData.from_tagged(Circle(radius=1.0)).is_type(Abstract)


# ---- transparent wire form ----


def test_wire_form_matches_concrete_and_union_annotations() -> None:
    circle = Circle(radius=2.0)
    dyn_record = Record(name="r", metadata=DynamicData.from_tagged(circle))
    concrete = ConcreteRecord(name="r", metadata=circle)
    union = UnionRecord(name="r", metadata=circle)
    dyn_data = DEFAULT_CONVERTER.unstructure_entity(dyn_record)
    assert dyn_data == DEFAULT_CONVERTER.unstructure_entity(concrete)
    assert dyn_data == DEFAULT_CONVERTER.unstructure_entity(union)
    # Every nested value is in plain mutable form, like any other unstructured value.
    assert type(dyn_data["metadata"]) is dict
    assert dyn_record.content_id() == concrete.content_id()


@pytest.mark.anyio
async def test_same_bytes_load_under_any_annotation() -> None:
    store = MemoryStorage()
    circle = Circle(radius=2.0)
    ref = await store.store(ConcreteRecord(name="r", metadata=circle))
    as_dynamic = DEFAULT_CONVERTER.structure(await store.load_raw_data(ref.entity_id), Record)
    assert isinstance(as_dynamic.metadata, DynamicData)
    assert as_dynamic.metadata == DynamicData.from_tagged(circle)
    assert as_dynamic.metadata.as_type(Circle) == circle


# ---- storage paths ----


@pytest.mark.anyio
async def test_store_load_roundtrip() -> None:
    store = MemoryStorage()
    record = Record(name="r", metadata=DynamicData.from_tagged(Circle(radius=2.0)))
    ref = await store.store(record)
    loaded = await store.load(ref)
    assert loaded == record
    assert loaded.metadata.as_type(Circle) == Circle(radius=2.0)
    # Re-storing is byte-identical.
    assert (await store.store(loaded)).entity_id == ref.entity_id


@pytest.mark.anyio
async def test_refs_in_content() -> None:
    """Content holding a reference stores and loads back equal."""
    store = MemoryStorage()
    leaf_ref = await store.store(Leaf(label="l"))
    record = Record(
        name="r",
        metadata=DynamicData.from_tagged(Nested(shape=Circle(radius=1.0), child=leaf_ref)),
    )
    loaded = await store.load(await store.store(record))
    assert loaded == record
    loaded_child = loaded.metadata.data["child"]
    assert isinstance(loaded_child, EntityID)
    assert loaded_child == leaf_ref.entity_id
    assert hash(loaded.metadata) == hash(record.metadata)
    decoded = loaded.metadata.as_type(Nested)
    assert await store.load(decoded.child) == Leaf(label="l")


@pytest.mark.anyio
async def test_nested_tagged_values_and_refs_survive() -> None:
    store = MemoryStorage()
    leaf_ref = await store.store(Leaf(label="l"))
    nested = Nested(shape=Circle(radius=1.0), child=leaf_ref)
    record = Record(name="r", metadata=DynamicData.from_tagged(nested))
    inner = record.metadata.data["shape"]
    assert inner == frozendict({"radius": 1.0, TYPE_KEY: "test.fxtr.dyn.circle"})
    assert record.metadata.data["child"] == leaf_ref.entity_id
    loaded = await store.load(await store.store(record))
    decoded = loaded.metadata.as_type(Nested)
    assert decoded.shape == Circle(radius=1.0)
    assert decoded.child.entity_id == leaf_ref.entity_id
    assert await store.load(decoded.child) == Leaf(label="l")


@pytest.mark.anyio
async def test_optional_field() -> None:
    store = MemoryStorage()
    loaded_none = await store.load(await store.store(OptionalRecord(metadata=None)))
    assert loaded_none.metadata is None
    loaded_some = await store.load(
        await store.store(OptionalRecord(metadata=DynamicData.from_tagged(Circle(radius=1.0))))
    )
    assert loaded_some.metadata is not None
    assert loaded_some.metadata.as_type(Circle) == Circle(radius=1.0)


def test_structure_rejects_non_tagged_content() -> None:
    tag = Record._fxtr_require_type_id()
    for bad in ({"radius": 1.0}, 3, [1]):
        with pytest.raises(ConversionError):
            DEFAULT_CONVERTER.structure({TYPE_KEY: tag, "name": "r", "metadata": bad}, Record)


# ---- equality ----


def test_equality_is_field_equality() -> None:
    fresh = DynamicData.from_tagged(Circle(radius=2.0))
    assert fresh == DynamicData.from_tagged(Circle(radius=2.0))
    assert hash(fresh) == hash(DynamicData.from_tagged(Circle(radius=2.0)))
    assert fresh != DynamicData.from_tagged(Circle(radius=3.0))
    assert fresh != DynamicData.from_tagged(Square(side=2.0))
