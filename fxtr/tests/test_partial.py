"""Tests for Partial: values of tagged dataclasses still being produced."""

from __future__ import annotations

import copy
import dataclasses
import pickle
from typing import Literal

import pytest
from frozendict import frozendict

from fxtr.core.entities import BoundID
from fxtr.core.entity_data import TYPE_KEY
from fxtr.entity_defns.converter import DEFAULT_CONVERTER
from fxtr.entity_defns.dataclass_bases import ConversionError, StrictTypeError, TaggedDataclass
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.entity_defns.dynamic_data import DynamicData
from fxtr.entity_defns.partial import PARTIAL_TYPE_ID, IncompletePartialError, Partial
from fxtr.storage.memory_storage import MemoryStorage


@dataclasses.dataclass(frozen=True, kw_only=True)
class Usage(TaggedDataclass, fxtr_type="test.fxtr.partial.Usage"):
    tokens: int


@dataclasses.dataclass(frozen=True, kw_only=True)
class Text(TaggedDataclass, fxtr_type="test.fxtr.partial.Text"):
    text: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class ToolCall(TaggedDataclass, fxtr_type="test.fxtr.partial.ToolCall"):
    name: str
    arguments: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class Message(TaggedDataclass, fxtr_type="test.fxtr.partial.Message"):
    blocks: tuple[Text | ToolCall, ...]
    usage: Usage
    stop: str
    note: str | None = None
    role: Literal["assistant"] = "assistant"


@dataclasses.dataclass(frozen=True, kw_only=True)
class Leaf(DataclassEntity, fxtr_type="test.fxtr.partial.Leaf"):
    label: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class Conversation(DataclassEntity, fxtr_type="test.fxtr.partial.Conversation"):
    messages: tuple[Message, ...]
    by_name: frozendict[str, Message]
    tags: frozenset[str]
    source: BoundID[Leaf]
    extra: DynamicData | None
    outcome: str

    def __post_init__(self) -> None:
        if self.outcome == "invalid":
            raise ValueError("outcome may not be 'invalid'")


@dataclasses.dataclass(frozen=True, kw_only=True)
class Box[T: TaggedDataclass](TaggedDataclass, fxtr_type="test.fxtr.partial.Box"):
    item: T
    count: int


@dataclasses.dataclass(frozen=True, kw_only=True)
class Group(TaggedDataclass, fxtr_type="test.fxtr.partial.Group"):
    name: str
    children: tuple[Group, ...]


@dataclasses.dataclass(frozen=True, kw_only=True)
class Preview(DataclassEntity, fxtr_type="test.fxtr.partial.Preview"):
    """An entity holding a partial value, as a stream event might."""

    sequence: int
    value: Partial[Message]


@dataclasses.dataclass(frozen=True)
class Plain:
    x: int


def unstructure(value: object) -> object:
    return DEFAULT_CONVERTER._cattrs.unstructure(value)


def round_trip[T](value: T, annotation: object) -> T:
    return DEFAULT_CONVERTER.structure(unstructure(value), annotation)


def leaf_ref() -> BoundID[Leaf]:
    return Leaf(label="leaf").content_id().bind_type(Leaf)


FINISHED = Message(blocks=(Text(text="hi"),), usage=Usage(tokens=3), stop="end")


# ---- construction and reading ----


def test_unset_fields_take_defaults_or_none() -> None:
    message = Partial(Message, blocks=(Text(text="Hel"),))
    assert message.partial_fields == {
        "blocks": (Text(text="Hel"),),
        "usage": None,
        "stop": None,
        "note": None,
        "role": "assistant",
    }
    assert list(message.partial_fields) == ["blocks", "usage", "stop", "note", "role"]
    assert message.blocks == (Text(text="Hel"),)
    assert message.usage is None
    assert message.partial_type is Message


def test_construction_rejects_unknown_fields_and_untagged_classes() -> None:
    with pytest.raises(TypeError, match="no field"):
        Partial(Message, colour="red")
    with pytest.raises(TypeError, match="tagged dataclasses"):
        Partial(Plain, x=1)  # type: ignore[type-var]
    with pytest.raises(AttributeError, match="no field 'colour'"):
        _ = Partial(Message).colour


def test_replace_and_immutability() -> None:
    message = Partial(Message)
    changed = message.replace(stop="end")
    assert changed.stop == "end"
    assert message.stop is None
    with pytest.raises(TypeError, match="no field"):
        message.replace(colour="red")
    with pytest.raises(dataclasses.FrozenInstanceError):
        message.stop = "end"  # type: ignore[misc]


def test_equality_hash_pickle_and_copy() -> None:
    message = Partial(Message, blocks=(Text(text="a"),))
    same = Partial(Message, blocks=(Text(text="a"),))
    assert message == same
    assert hash(message) == hash(same)
    assert message != Partial(Message, blocks=(Text(text="b"),))
    assert message != FINISHED
    assert pickle.loads(pickle.dumps(message)) == message
    assert copy.deepcopy(message) == message


def test_from_value_holds_every_field() -> None:
    partial = Partial.from_value(FINISHED)
    assert partial.partial_fields == {
        f.name: getattr(FINISHED, f.name) for f in dataclasses.fields(Message)
    }
    assert partial.complete() == FINISHED


# ---- wire form ----


def test_wire_form_wraps_the_class_tagged_mapping() -> None:
    message = Partial(Message, blocks=(Partial(ToolCall, name="search"),))
    assert unstructure(message) == {
        TYPE_KEY: PARTIAL_TYPE_ID,
        "of": {
            TYPE_KEY: "test.fxtr.partial.Message",
            "blocks": [
                {
                    TYPE_KEY: PARTIAL_TYPE_ID,
                    "of": {
                        TYPE_KEY: "test.fxtr.partial.ToolCall",
                        "name": "search",
                        "arguments": None,
                    },
                }
            ],
            "usage": None,
            "stop": None,
            "note": None,
            "role": "assistant",
        },
    }


def test_round_trip_with_partials_at_every_admitted_position() -> None:
    conversation = Partial(
        Conversation,
        messages=(FINISHED, Partial(Message, blocks=(Text(text="x"), Partial(ToolCall)))),
        by_name=frozendict({"a": Partial(Message, stop="end"), "b": FINISHED}),
        tags=frozenset({"y", "x"}),
        source=leaf_ref(),
        extra=DynamicData.from_tagged(Usage(tokens=1)),
    )
    back = round_trip(conversation, Partial[Conversation])
    assert back == conversation
    assert isinstance(back.messages[0], Message)
    assert isinstance(back.messages[1], Partial)
    assert isinstance(back.by_name, frozendict)
    assert back.source.entity_id == leaf_ref().entity_id


def test_any_field_may_be_none_but_elements_may_not() -> None:
    assert round_trip(Partial(Conversation), Partial[Conversation]) == Partial(Conversation)
    data = unstructure(Partial(Message, blocks=(Text(text="a"),)))
    data["of"]["blocks"].append(None)  # type: ignore[index]
    with pytest.raises(ConversionError):
        DEFAULT_CONVERTER.structure(data, Partial[Message])


def test_a_partial_never_decodes_as_its_finished_class() -> None:
    with pytest.raises(StrictTypeError, match="does not match"):
        DEFAULT_CONVERTER.structure(unstructure(Partial.from_value(FINISHED)), Message)


def test_finished_values_inside_a_partial_hold_no_partials() -> None:
    # A finished Message nested in a partial decodes as Message, so its own fields are strict.
    message = Message(blocks=(Partial(Text),), usage=Usage(tokens=1), stop="end")  # type: ignore[arg-type]
    conversation = Partial(Conversation, messages=(message,))
    with pytest.raises(ConversionError):
        round_trip(conversation, Partial[Conversation])


def test_a_partial_must_be_of_a_type_the_slot_admits() -> None:
    with pytest.raises(StrictTypeError, match="does not decode as"):
        round_trip(Partial(Conversation, messages=(Partial(Usage),)), Partial[Conversation])
    with pytest.raises(StrictTypeError, match="does not decode as"):
        round_trip(Partial(Usage), Partial[Message])


def test_decoding_is_strict_about_shape() -> None:
    good = unstructure(Partial(Usage, tokens=1))
    assert isinstance(good, dict)
    for bad in (
        {**good, "extra": 1},
        {TYPE_KEY: PARTIAL_TYPE_ID},
        {**good, "of": {k: v for k, v in good["of"].items() if k != "tokens"}},
        {**good, "of": {**good["of"], "colour": None}},
        {**good, "of": {**good["of"], "tokens": "1"}},
        {**good, "of": {"tokens": 1}},
    ):
        with pytest.raises(ConversionError):
            DEFAULT_CONVERTER.structure(bad, Partial[Usage])
    with pytest.raises(ConversionError, match="bare Partial"):
        DEFAULT_CONVERTER.structure(good, Partial)


def test_union_of_partials_dispatches_on_the_inner_tag() -> None:
    assert round_trip(Partial(Text, text="a"), Partial[Text | ToolCall]) == Partial(Text, text="a")
    assert round_trip(Partial(ToolCall), Partial[Text | ToolCall]) == Partial(ToolCall)


def test_generic_and_recursive_classes() -> None:
    box = Partial(Box[Usage], item=Partial(Usage))
    back = round_trip(box, Partial[Box[Usage]])
    assert back == box
    with pytest.raises(ConversionError, match="type arguments"):
        DEFAULT_CONVERTER.structure(unstructure(box), Partial[Box])

    group = Partial(Group, name="root", children=(Group(name="a", children=()), Partial(Group)))
    assert round_trip(group, Partial[Group]) == group


# ---- completion ----


def test_complete_finishes_nested_partials() -> None:
    conversation = Partial(
        Conversation,
        messages=(
            Partial(Message, blocks=(Partial(Text, text="hi"),), usage=Usage(tokens=3), stop="end"),
        ),
        by_name=frozendict({"a": Partial.from_value(FINISHED)}),
        tags=frozenset(),
        source=leaf_ref(),
        outcome="done",
    )
    done = conversation.complete()
    assert done == Conversation(
        messages=(FINISHED,),
        by_name=frozendict({"a": FINISHED}),
        tags=frozenset(),
        source=leaf_ref(),
        extra=None,
        outcome="done",
    )
    # Completion decodes as a load would, so the reference gets its annotation's loader.
    assert done.source == leaf_ref()


def test_complete_names_every_missing_field() -> None:
    conversation = Partial(
        Conversation,
        messages=(FINISHED, Partial(Message, blocks=(Partial(ToolCall, name="f"),))),
    )
    with pytest.raises(IncompletePartialError) as info:
        conversation.complete()
    assert info.value.missing == (
        "messages[1].blocks[0].arguments",
        "messages[1].usage",
        "messages[1].stop",
        "by_name",
        "tags",
        "source",
        "outcome",
    )


def test_complete_allows_none_where_the_class_does() -> None:
    message = Partial(Message, blocks=(), usage=Usage(tokens=0), stop="end")
    assert message.note is None
    assert message.complete() == Message(blocks=(), usage=Usage(tokens=0), stop="end")


def test_complete_reports_a_finished_value_that_cannot_be_built_or_decoded() -> None:
    base = {
        "messages": (),
        "by_name": frozendict(),
        "tags": frozenset(),
        "source": leaf_ref(),
    }
    with pytest.raises(ConversionError, match="could not be built"):
        Partial(Conversation, **base, outcome="invalid").complete()
    with pytest.raises(ConversionError):
        Partial(Conversation, **base, outcome=3).complete()


# ---- storage ----


@pytest.mark.anyio
async def test_an_entity_holding_a_partial_stores_and_loads() -> None:
    store = MemoryStorage()
    preview = Preview(sequence=1, value=Partial(Message, blocks=(Partial(Text, text="He"),)))
    ref = await store.store(preview)
    loaded = await store.load(ref)
    assert loaded == preview
    assert (await store.store(loaded)).entity_id == ref.entity_id
    finished = Preview(sequence=1, value=Partial.from_value(FINISHED))
    assert finished.content_id() != preview.content_id()
