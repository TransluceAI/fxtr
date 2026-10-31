"""Tests for schemas: the constructors, the annotation grammar, the wire form, assignability,
coercion, and inference."""

from __future__ import annotations

import dataclasses
import typing
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, NotRequired, TypedDict, cast

import pytest
import typing_extensions
from frozendict import frozendict

from fxtr.core.entities import BoundID
from fxtr.core.entity_data import EntityID
from fxtr.core.loader_registry import DEFAULT_ENTITY_LOADER_REGISTRY
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.entity_defns.schema import (
    IdSchema,
    LeafSchema,
    ListSchema,
    NullableSchema,
    Schema,
    SchemaError,
    StructSchema,
    ValueSchemaError,
    as_schema,
    coerce_value,
    id_of,
    infer_schema,
    is_assignable,
    list_of,
    needs_presentation,
    optional,
    schema_from_annotation,
    schema_from_wire,
    schema_to_wire,
    strip_presentation,
    struct_of,
    value_to_wire,
)
from fxtr.entity_defns.unknown_entity import UNKNOWN_ENTITY_LOADER, UnknownEntity
from fxtr.storage.memory_storage import MemoryStorage

SAMPLE_TYPE = "test.fxtr.schema.sample"
OTHER_TYPE = "test.fxtr.schema.other"

_TypingOptional: Any = typing.Optional  # noqa: UP045 - the old spelling, tested deliberately


@pytest.mark.parametrize(
    "schema",
    [
        LeafSchema("int"),
        IdSchema(),
        IdSchema(SAMPLE_TYPE),
        optional(int),
        list_of(str),
        optional(list_of(optional(int))),
        struct_of(a=int, b=list_of(struct_of(c=bytes))),
        struct_of(),
    ],
)
def test_schema_round_trips_through_the_wire(schema: Schema) -> None:
    assert schema_from_wire(schema_to_wire(schema)) == schema


def test_str_is_for_display() -> None:
    assert str(as_schema(int)) == "int"
    assert str(optional(list_of(optional(int)))) == "list[int?]?"
    assert str(struct_of(b=str, a=int)) == "struct{a: int, b: str}"
    assert str(IdSchema()) == "id"
    assert str(IdSchema(SAMPLE_TYPE)) == "id[test.fxtr.schema.sample]"


def test_schema_construction_validates() -> None:
    with pytest.raises(SchemaError):
        NullableSchema(NullableSchema(LeafSchema("int")))
    with pytest.raises(SchemaError):
        LeafSchema("float64")  # type: ignore[arg-type]
    with pytest.raises(SchemaError):
        LeafSchema("id")  # type: ignore[arg-type]
    with pytest.raises(SchemaError):
        IdSchema("")
    with pytest.raises(SchemaError):
        IdSchema(cast("Any", 42))
    with pytest.raises(SchemaError):
        StructSchema(frozendict({"": LeafSchema("int")}))
    with pytest.raises(SchemaError):
        StructSchema(frozendict({"a": "int"}))  # type: ignore[dict-item]


def test_a_schema_is_not_a_string() -> None:
    # There is no text form of schemas, so a string spec is an error wherever one is accepted.
    for spec in ["int", "struct{a: int}", ""]:
        with pytest.raises(SchemaError, match="not written as a string"):
            as_schema(spec)
    with pytest.raises(SchemaError, match="not written as a string"):
        list_of("int")
    with pytest.raises(SchemaError, match="not written as a string"):
        struct_of(a="int")


def test_struct_of_sorts_its_names() -> None:
    struct = struct_of(b=list_of(str), a=float)
    assert struct == StructSchema(
        frozendict({"a": LeafSchema("float"), "b": ListSchema(LeafSchema("str"))})
    )
    assert struct.names == ("a", "b")
    assert str(struct) == "struct{a: float, b: list[str]}"
    assert struct_of() == StructSchema(frozendict())


@pytest.mark.parametrize(
    "data",
    [
        {"list": "int", "extra": 1},
        {"set": "int"},
        "nope",
        3,
        {"id": ""},  # a type id is a non-empty string
        {"id": 7},
    ],
)
def test_schema_from_wire_rejects_invalid(data: object) -> None:
    with pytest.raises(SchemaError):
        schema_from_wire(data)


@pytest.mark.parametrize(
    ("provided", "expected", "ok"),
    [
        (int, int, True),
        (int, int | None, True),
        (int | None, int, False),
        (int, str, False),
        (list_of(str), list_of(optional(str)), True),
        (list_of(optional(str)), list_of(str), False),
        (struct_of(a=int), struct_of(a=optional(int)), True),
        (struct_of(a=int), struct_of(a=int, b=int), False),
        (optional(struct_of(a=int)), optional(struct_of(a=optional(int))), True),
        (list_of(int), int, False),
    ],
)
def test_is_assignable(provided: object, expected: object, ok: bool) -> None:
    assert is_assignable(as_schema(provided), as_schema(expected)) is ok


def test_coerce_value_is_strict_and_freezes() -> None:
    frozen = coerce_value(
        [{"b": 1, "a": [None]}], list_of(struct_of(a=list_of(optional(int)), b=int))
    )
    assert frozen == (frozendict({"a": (None,), "b": 1}),)
    assert value_to_wire(frozen) == [{"a": [None], "b": 1}]
    assert coerce_value(None, optional(int)) is None
    eid = EntityID(cid=b"\x01" * 36)
    assert coerce_value(eid, IdSchema()) is eid
    assert coerce_value(eid, IdSchema(SAMPLE_TYPE)) is eid  # the type is not checked
    for value, spec in [
        (True, int),
        (1, bool),
        (1, float),
        ("s", bytes),
        ("s", list_of(str)),
        ({"a": 1, "b": 2}, struct_of(a=int)),
        (None, int),
        ([None], list_of(int)),
        (b"x", IdSchema()),
    ]:
        with pytest.raises(ValueSchemaError):
            coerce_value(value, as_schema(spec))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, LeafSchema("int")),
        (True, LeafSchema("bool")),
        ([1, None], list_of(optional(int))),
        ([[1, None], []], list_of(list_of(optional(int)))),
        ([[[]], [[1]]], list_of(list_of(list_of(int)))),
        (
            [{"a": 1, "b": None}, {"a": 2, "b": "x"}],
            list_of(struct_of(a=int, b=optional(str))),
        ),
        ([None, {"a": 1}], list_of(optional(struct_of(a=int)))),
        ({"a": [1], "b": "x"}, struct_of(a=list_of(int), b=str)),
    ],
)
def test_infer_schema(value: object, expected: Schema) -> None:
    assert infer_schema(value) == expected


# ---- typed refs ----


def test_typed_id_wire_form_carries_the_type_id() -> None:
    schema = list_of(optional(id_of(SAMPLE_TYPE)))
    assert schema == ListSchema(NullableSchema(IdSchema(SAMPLE_TYPE)))
    assert isinstance(schema.element, NullableSchema)
    assert schema.element.inner.type_id == SAMPLE_TYPE  # type: ignore[union-attr]
    assert schema_to_wire(schema) == {"list": {"nullable": {"id": SAMPLE_TYPE}}}
    assert schema_from_wire(schema_to_wire(schema)) == schema
    assert schema_to_wire(IdSchema()) == "id"


def test_id_schema_equality() -> None:
    declared = IdSchema(SAMPLE_TYPE)
    assert declared == IdSchema("test.fxtr.schema.sample")
    assert len({declared, IdSchema("test.fxtr.schema.sample")}) == 1
    assert declared != IdSchema() and declared != IdSchema(OTHER_TYPE)
    assert IdSchema() != LeafSchema("int")
    assert struct_of(a=declared) == struct_of(a=IdSchema(SAMPLE_TYPE))


@pytest.mark.parametrize(
    ("provided", "expected", "ok"),
    [
        (IdSchema(SAMPLE_TYPE), IdSchema(), True),
        (IdSchema(), IdSchema(SAMPLE_TYPE), False),
        (IdSchema(SAMPLE_TYPE), IdSchema(SAMPLE_TYPE), True),
        (IdSchema(SAMPLE_TYPE), IdSchema(OTHER_TYPE), False),
        (IdSchema(SAMPLE_TYPE), NullableSchema(IdSchema()), True),
        (ListSchema(IdSchema(SAMPLE_TYPE)), ListSchema(IdSchema()), True),
        (ListSchema(IdSchema()), ListSchema(IdSchema(SAMPLE_TYPE)), False),
        (IdSchema(), LeafSchema("bytes"), False),
    ],
)
def test_is_assignable_refs(provided: object, expected: object, ok: bool) -> None:
    assert is_assignable(provided, expected) is ok  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    [None, [], [[]], [[], [None]], [1, 2.0], [1, True], [{"a": 1}, {"b": 1}], [1, [1]], {1}],
)
def test_infer_schema_rejects_undetermined_or_conflicting(value: object) -> None:
    with pytest.raises(ValueSchemaError):
        infer_schema(value)


@pytest.mark.parametrize(
    "value",
    [EntityID(cid=b"\x01" * 36), {"id": 1, "who": EntityID(cid=b"\x01" * 36)}],
)
def test_infer_schema_rejects_entity_ids(value: object) -> None:
    # An id does not determine the type of the entity it points to, so refs must be declared.
    with pytest.raises(ValueSchemaError, match="explicit schema"):
        infer_schema(value)


# ---- building schemas from Python ----


@dataclasses.dataclass(frozen=True, kw_only=True)
class Sample(DataclassEntity, fxtr_type=SAMPLE_TYPE):
    name: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class Other(DataclassEntity, fxtr_type=OTHER_TYPE):
    n: int


@dataclasses.dataclass(frozen=True, kw_only=True)
class Untagged(DataclassEntity):
    """An entity class with no declared type: the registry cannot build a loader for it."""


class Review(TypedDict):
    proposal: BoundID[Sample] | None
    message: str


class Nested(TypedDict):
    reviews: list[Review]
    tags: Sequence[str]


class ClosedReview(typing_extensions.TypedDict, closed=True):
    message: str


class PartialReview(TypedDict, total=False):
    message: str


class NotRequiredReview(TypedDict):
    message: NotRequired[str]


class OpenReview(typing_extensions.TypedDict, closed=False):
    message: str


class ExtraReview(typing_extensions.TypedDict, extra_items=int):
    message: str


def test_id_of() -> None:
    sample_loader = DEFAULT_ENTITY_LOADER_REGISTRY.loader_for(Sample)
    typed = id_of(Sample)
    assert typed.type_id == SAMPLE_TYPE and typed.loader is sample_loader
    assert id_of(SAMPLE_TYPE) == typed and id_of(SAMPLE_TYPE).loader is None
    assert id_of() == IdSchema() and id_of().loader is None
    # A class without a static type id gives an untyped id that still carries the loader.
    unknown = id_of(UnknownEntity)
    assert unknown == IdSchema() and unknown.loader is UNKNOWN_ENTITY_LOADER
    # Classes the registry cannot build a loader for, and non-entity classes, are errors.
    with pytest.raises(SchemaError, match="No loader"):
        id_of(Untagged)
    with pytest.raises(SchemaError, match="entity class"):
        id_of(cast("Any", int))
    with pytest.raises(SchemaError):
        IdSchema(SAMPLE_TYPE, cast("Any", "not a loader"))


def test_presentation_is_not_part_of_equality_or_the_wire() -> None:
    typed, plain = id_of(Sample), IdSchema(SAMPLE_TYPE)
    assert typed == plain and hash(typed) == hash(plain)
    assert schema_to_wire(typed) == schema_to_wire(plain)
    as_list, as_tuple = (
        ListSchema(LeafSchema("int"), present_as=list),
        ListSchema(LeafSchema("int")),
    )
    assert as_list == as_tuple and hash(as_list) == hash(as_tuple)
    assert schema_to_wire(as_list) == schema_to_wire(as_tuple)
    review = schema_from_annotation(Review)
    anonymous = struct_of(proposal=optional(id_of(Sample)), message=str)
    assert review == anonymous and hash(review) == hash(anonymous)
    assert schema_to_wire(review) == schema_to_wire(anonymous)
    assert isinstance(review, StructSchema) and review.python_type is Review
    assert anonymous.python_type is None
    with pytest.raises(SchemaError, match="presentation"):
        ListSchema(LeafSchema("int"), present_as=cast("Any", set))
    with pytest.raises(SchemaError, match="TypedDict"):
        StructSchema(frozendict({"a": LeafSchema("int")}), python_type=dict)
    with pytest.raises(SchemaError, match="keys"):
        StructSchema(frozendict({"a": LeafSchema("int")}), python_type=Review)


def test_strip_presentation_removes_presentation() -> None:
    presented = struct_of(
        proposal=optional(id_of(Sample)),
        tags=ListSchema(LeafSchema("int"), present_as=list),
        review=schema_from_annotation(Review),
    )
    assert needs_presentation(presented)
    stripped = strip_presentation(presented)
    # Presentation is off the wire, so stripping it leaves an equal schema with the same wire form.
    assert stripped == presented
    assert schema_to_wire(stripped) == schema_to_wire(presented)
    assert not needs_presentation(stripped)
    # Idempotent, and a schema with nothing to strip is returned unchanged.
    assert strip_presentation(stripped) is stripped
    plain = struct_of(a=int, b=list_of(str))
    assert strip_presentation(plain) is plain


def test_constructors_accept_schemas_and_annotations() -> None:
    schema = struct_of(score=float, tags=list_of(str), who=optional(BoundID[Sample]))
    assert schema == struct_of(
        score=LeafSchema("float"),
        tags=ListSchema(LeafSchema("str")),
        who=NullableSchema(IdSchema(SAMPLE_TYPE)),
    )
    who = schema.fields["who"]
    assert isinstance(who, NullableSchema) and isinstance(who.inner, IdSchema)
    assert who.inner.loader is DEFAULT_ENTITY_LOADER_REGISTRY.loader_for(Sample)
    assert list_of(LeafSchema("int")) == list_of(int)
    assert as_schema(int) == LeafSchema("int")
    leaf = LeafSchema("int")
    assert as_schema(leaf) is leaf
    with pytest.raises(SchemaError):
        optional(optional(int))


@pytest.mark.parametrize(
    ("annotation", "expected"),
    [
        (int, LeafSchema("int")),
        (bool, LeafSchema("bool")),
        (bytes, LeafSchema("bytes")),
        (Sequence[int], list_of(int)),
        (tuple[str, ...], list_of(str)),
        (list[float], list_of(float)),
        (int | None, optional(int)),
        (_TypingOptional[str], optional(str)),  # the typing.Union spelling
        (list[int | None] | None, optional(list_of(optional(int)))),
        (Annotated[int, "some other tool's metadata"], LeafSchema("int")),
        (EntityID, IdSchema()),
        (BoundID[Sample] | None, optional(id_of(SAMPLE_TYPE))),
        (Sequence[BoundID[Other]], list_of(id_of(OTHER_TYPE))),
        (Review, struct_of(message=str, proposal=optional(id_of(SAMPLE_TYPE)))),
        (ClosedReview, struct_of(message=str)),
        (
            Nested,
            struct_of(
                reviews=list_of(struct_of(message=str, proposal=optional(id_of(SAMPLE_TYPE)))),
                tags=list_of(str),
            ),
        ),
    ],
)
def test_schema_from_annotation(annotation: object, expected: Schema) -> None:
    assert schema_from_annotation(annotation) == expected


def test_schema_from_annotation_presentation() -> None:
    assert schema_from_annotation(list[int]).present_as is list  # type: ignore[union-attr]
    assert schema_from_annotation(Sequence[int]).present_as is tuple  # type: ignore[union-attr]
    assert schema_from_annotation(tuple[int, ...]).present_as is tuple  # type: ignore[union-attr]
    nested = schema_from_annotation(Nested)
    assert isinstance(nested, StructSchema) and nested.python_type is Nested
    reviews = nested.fields["reviews"]
    assert isinstance(reviews, ListSchema) and reviews.present_as is list
    assert isinstance(reviews.element, StructSchema) and reviews.element.python_type is Review
    bound = schema_from_annotation(BoundID[Sample])
    assert isinstance(bound, IdSchema)
    assert bound.loader is DEFAULT_ENTITY_LOADER_REGISTRY.loader_for(Sample)
    assert schema_from_annotation(BoundID[UnknownEntity]) == IdSchema()
    assert schema_from_annotation(BoundID[UnknownEntity]).loader is UNKNOWN_ENTITY_LOADER  # type: ignore[union-attr]


type SampleRef = BoundID[Sample]
type MaybeRef[T] = BoundID[T] | None


def test_schema_from_annotation_expands_type_aliases() -> None:
    assert schema_from_annotation(SampleRef) == id_of(Sample)
    assert schema_from_annotation(MaybeRef[Sample]) == optional(id_of(Sample))
    # The loader still comes through the expansion.
    assert schema_from_annotation(SampleRef).loader is DEFAULT_ENTITY_LOADER_REGISTRY.loader_for(  # type: ignore[union-attr]
        Sample
    )


@pytest.mark.parametrize(
    ("annotation", "match"),
    [
        (list, "not a schema"),
        (dict, "no fixed fields"),
        (dict[str, int], "no fixed fields"),
        (Mapping[str, int], "no fixed fields"),
        (Any, "not a schema"),
        (object, "not a schema"),
        (type(None), "None alone"),
        (int | str, "union"),
        (int | str | None, "union"),
        (Literal["a"], "not a schema"),
        (tuple[int, str], "tuple\\[X, \\.\\.\\.\\]"),
        (Sample, "BoundID\\[Sample\\]"),
        (BoundID, "entity class"),
        (BoundID[Sample | Other], "entity class"),
        (BoundID[Untagged], "No loader"),
        (BoundID[int], "entity class"),  # type: ignore[type-var]
        (PartialReview, "total"),
        (NotRequiredReview, "total"),
        (OpenReview, "closed=False"),
        (ExtraReview, "extra_items"),
        ("Sample", "forward reference"),
        (list[int | None | str], "union"),
    ],
)
def test_schema_from_annotation_rejects(annotation: object, match: str) -> None:
    with pytest.raises(SchemaError, match=match):
        schema_from_annotation(annotation)


@pytest.mark.anyio
async def test_coerce_value_accepts_bound_ids() -> None:
    ref = await MemoryStorage().store(Sample(name="s"))
    assert coerce_value(ref, IdSchema()) is ref.entity_id
    assert coerce_value([ref], list_of(id_of(Sample))) == (ref.entity_id,)
    assert coerce_value({"who": ref, "n": 1}, struct_of(who=id_of(Sample), n=int)) == frozendict(
        {"n": 1, "who": ref.entity_id}
    )


@pytest.mark.anyio
async def test_infer_schema_from_bound_ids() -> None:
    storage = MemoryStorage()
    ref = await storage.store(Sample(name="s"))
    other = await storage.store(Other(n=1))
    inferred = infer_schema(ref)
    assert inferred == id_of(Sample) and inferred.loader is ref.loader  # type: ignore[union-attr]
    assert infer_schema([ref, None]) == list_of(optional(id_of(Sample)))
    assert infer_schema({"a": [ref]}) == struct_of(a=list_of(id_of(Sample)))
    with pytest.raises(ValueSchemaError, match="conflicting"):
        infer_schema([ref, other])
    with pytest.raises(ValueSchemaError, match="does not name a single entity type"):
        infer_schema(ref.entity_id.bind_type(Sample | Other))
