"""The twins of the classes in ``test_dataclass_quoted_annotations.py``, defined here under
``from __future__ import annotations`` (which makes every whole field annotation a string) rather
than with quoted names nested inside the annotations. Both modules are exercised by the same
tests."""

from __future__ import annotations

import dataclasses

from frozendict import frozendict

from fxtr.entity_defns.dataclass_entity import DataclassEntity


@dataclasses.dataclass(frozen=True)
class Leaf:
    label: str


@dataclasses.dataclass(frozen=True)
class PlainNode:
    label: str
    children: tuple[PlainNode, ...]


@dataclasses.dataclass(frozen=True, kw_only=True)
class Group(DataclassEntity, fxtr_type="test.fxtr.futureAnnotations.group"):
    name: str
    groups: tuple[Group, ...]
    leaves: tuple[Leaf, ...]


@dataclasses.dataclass(frozen=True, kw_only=True)
class PlainHolder(DataclassEntity, fxtr_type="test.fxtr.futureAnnotations.plainHolder"):
    nodes: tuple[PlainNode, ...]
    by_name: frozendict[str, Leaf]
