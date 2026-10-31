"""Arrays nested many levels deep, for tests of Arrow, DuckDB and storage at depth.

Each row is a chain of structs (or of lists of structs) ``depth`` levels deep around a leaf struct
of every scalar kind. One chain per row is populated, so lists do not multiply the row's size,
and some rows break the chain with a null or an empty list at one level. The leaf can also hold
entity IDs of a small stored entity, :class:`SupportCase`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any, cast

from fxtr.core.entities import BoundID
from fxtr.core.entity_data import EntityID
from fxtr.entity_defns.array import Array
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.entity_defns.query import Column, value
from fxtr.entity_defns.schema import Schema, ValueInput, list_of, optional, struct_of


@dataclasses.dataclass(frozen=True, kw_only=True)
class SupportCase(DataclassEntity, fxtr_type="test.fxtr.deepArrays.supportCase"):
    ticket_id: str
    customer_tier: str
    question: str


SUPPORT_CASES = {
    "refund": SupportCase(
        ticket_id="TKT-1842",
        customer_tier="business",
        question="Can a duplicate annual renewal be refunded without closing the account?",
    ),
    "delivery": SupportCase(
        ticket_id="TKT-2091",
        customer_tier="enterprise",
        question="How should we reroute a delayed replacement laptop to the field office?",
    ),
}


def nested_schema(depth: int, *, lists: bool = False, entities: bool = False) -> Schema:
    """Each wrapper adds a nullable child; list wrappers also allow null elements."""
    inner = struct_of(
        quality=int, note=optional(str), tags=list_of(str), raw=bytes, empty=struct_of()
    )
    if entities:
        inner = struct_of(
            details=inner, primary=BoundID[SupportCase], evidence=list_of(BoundID[SupportCase])
        )
    for _ in range(depth):
        child = optional(list_of(optional(inner))) if lists else optional(inner)
        inner = struct_of(level=int, child=child)
    return struct_of(group=int, tokens=int, payload=inner)


def nested_row(
    depth: int,
    index: int,
    *,
    lists: bool = False,
    reference: EntityID | BoundID[SupportCase] | None = None,
    missing_level: int | None = None,
    empty_list: bool = False,
) -> ValueInput:
    """One populated chain avoids exponential list fanout while retaining null siblings."""
    payload: ValueInput = {
        "quality": index * 7 + 9,
        "note": None if index % 2 else "review ☃",
        "tags": ["production", "深い"] if index % 2 else [],
        "raw": b"\x00\xff",
        "empty": {},
    }
    if reference is not None:
        payload = {"details": payload, "primary": reference, "evidence": [reference]}
    for level in reversed(range(depth)):
        child: ValueInput = [payload, None] if lists else payload
        if missing_level == level:
            child = [] if lists and empty_list else None
        payload = {"level": level, "child": child}
    return {"group": index % 2, "tokens": 80 + index, "payload": payload}


def nested_array(
    depth: int,
    *,
    lists: bool = False,
    reference: EntityID | BoundID[SupportCase] | None = None,
    rows: int = 8,
) -> Array[Any]:
    """``rows`` rows of :func:`nested_row`; every fourth row breaks its chain at one level."""
    schema = nested_schema(depth, lists=lists, entities=reference is not None)
    return Array.from_items(
        (
            (
                (index,),
                nested_row(
                    depth,
                    index,
                    lists=lists,
                    reference=reference,
                    missing_level=(index % depth if depth and index % 4 == 3 else None),
                    empty_list=index % 8 == 7,
                ),
            )
            for index in range(rows)
        ),
        ("[row: int]", schema),
    )


def leaf_column(depth: int, field: str = "quality") -> Column:
    """The column naming ``field`` of the leaf struct under ``depth`` levels."""
    column = value().field("payload")
    for _ in range(depth):
        column = column.field("child")
    return column.field(field)


def evidence_ids(value: object) -> list[EntityID | BoundID[SupportCase]]:
    """Every entity ID in ``value``, a row's value or a tuple of them, in order."""
    if isinstance(value, (EntityID, BoundID)):
        return [cast("EntityID | BoundID[SupportCase]", value)]
    if isinstance(value, Mapping):
        return [reference for item in value.values() for reference in evidence_ids(item)]
    if isinstance(value, (list, tuple)):
        return [reference for item in value for reference in evidence_ids(item)]
    return []
