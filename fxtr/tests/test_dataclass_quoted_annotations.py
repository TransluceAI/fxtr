"""Tests for quoted names nested inside dataclass field annotations (``tuple["Group", ...]``).

This module deliberately omits ``from __future__ import annotations``, so its field annotations
are generic types with strings inside rather than whole strings. ``future_annotations_testing``
defines the same classes with the future import, and every test runs against both.
"""

import dataclasses
import sys
from types import ModuleType
from typing import Any

import future_annotations_testing
import pytest
from frozendict import frozendict

from fxtr.entity_defns.converter import DEFAULT_CONVERTER
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.storage.memory_storage import MemoryStorage


@dataclasses.dataclass(frozen=True)
class Leaf:
    label: str


@dataclasses.dataclass(frozen=True)
class PlainNode:
    label: str
    children: tuple["PlainNode", ...]


@dataclasses.dataclass(frozen=True, kw_only=True)
class Group(DataclassEntity, fxtr_type="test.fxtr.quotedAnnotations.group"):
    name: str
    groups: tuple["Group", ...]
    leaves: tuple["Leaf", ...]


@dataclasses.dataclass(frozen=True, kw_only=True)
class PlainHolder(DataclassEntity, fxtr_type="test.fxtr.quotedAnnotations.plainHolder"):
    nodes: tuple["PlainNode", ...]
    by_name: frozendict[str, "Leaf"]


# The twin modules' classes are distinct types of the same shape; tests reach them by attribute.
ANNOTATION_STYLES = pytest.mark.parametrize(
    "defs",
    [sys.modules[__name__], future_annotations_testing],
    ids=["nested_quotes", "future_import"],
)


def _group_tree(defs: ModuleType) -> Any:
    """Three levels of groups, each with children, so every level's field hooks run on data."""
    inner = defs.Group(name="c", groups=(), leaves=(defs.Leaf("c1"),))
    middle = defs.Group(name="b", groups=(inner,), leaves=(defs.Leaf("b1"),))
    return defs.Group(name="a", groups=(middle,), leaves=(defs.Leaf("a1"), defs.Leaf("a2")))


@ANNOTATION_STYLES
def test_nested_group_wire_form(defs: ModuleType) -> None:
    tag = defs.Group._fxtr_require_type_id()
    assert DEFAULT_CONVERTER.unstructure_entity(_group_tree(defs)) == {
        "$type": tag,
        "name": "a",
        "leaves": [{"label": "a1"}, {"label": "a2"}],
        "groups": [
            {
                "$type": tag,
                "name": "b",
                "leaves": [{"label": "b1"}],
                "groups": [{"$type": tag, "name": "c", "leaves": [{"label": "c1"}], "groups": []}],
            }
        ],
    }


@pytest.mark.anyio
@ANNOTATION_STYLES
async def test_nested_groups_round_trip(defs: ModuleType) -> None:
    store = MemoryStorage()
    tree = _group_tree(defs)
    loaded = await store.load(await store.store(tree))
    assert loaded == tree
    assert type(loaded.groups[0].groups[0]) is defs.Group
    assert type(loaded.groups[0].groups[0].leaves[0]) is defs.Leaf


@pytest.mark.anyio
@ANNOTATION_STYLES
async def test_plain_dataclass_quoted_names_round_trip(defs: ModuleType) -> None:
    # Plain dataclasses take cattrs' built-in dataclass path rather than fxtr's protocol-class
    # hooks; a self-reference and a quoted name inside frozendict must resolve there too.
    store = MemoryStorage()
    holder = defs.PlainHolder(
        nodes=(defs.PlainNode("root", (defs.PlainNode("kid", (defs.PlainNode("x", ()),)),)),),
        by_name=frozendict({"k": defs.Leaf("v")}),
    )
    data = DEFAULT_CONVERTER.unstructure_entity(holder)
    leaf_node = {"label": "x", "children": []}
    assert data["nodes"] == [
        {"label": "root", "children": [{"label": "kid", "children": [leaf_node]}]}
    ]
    assert data["by_name"] == {"k": {"label": "v"}}
    loaded = await store.load(await store.store(holder))
    assert loaded == holder
    assert type(loaded.nodes[0].children[0].children[0]) is defs.PlainNode
    assert type(loaded.by_name["k"]) is defs.Leaf
