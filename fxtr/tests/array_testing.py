"""Arrays covering every value shape, for tests that must hold across all of them.

Each array differs from the others in some way that the canonical encoding, the Arrow
conversion, or a representation of an array has to get right: integer extremes, a negative zero,
non-ASCII text and keys, bytes, entity ids with and without a type, nullable list elements,
nested and optional structs, and empty structs in every position they can take.
"""

from __future__ import annotations

from fxtr.core import codec
from fxtr.core.entity_data import EntityID
from fxtr.entity_defns.array import ARRAY_TYPE, Array
from fxtr.entity_defns.schema import IdSchema, list_of, optional, struct_of


def cid_for(seed: int) -> bytes:
    """A valid content ID that differs for each ``seed``."""
    return codec.compute_cid(codec.encode({"$type": "fxtr.builtin.test", "seed": seed}))


def identity_arrays() -> list[Array[object]]:
    """One array per value shape; see the module docstring."""
    entity_id = EntityID(cid=cid_for(1))
    return [
        Array.from_items([], ("[i: int]", str)),
        Array.from_items([((-2,), 3), ((4,), -(2**63))], ("[i: int]", int)),
        Array.from_items([(("a",), -0.0), (("é",), 1.5)], ("[name: str]", float)),
        Array.from_items([((1,), True), ((2,), False)], ("[i: int]", bool)),
        Array.scalar("snowman ☃", str),
        Array.scalar(b"\x00\xff", bytes),
        Array.from_items([((1,), entity_id)], ("[i: int]", EntityID)),
        Array.from_items(
            [((1,), entity_id)],
            ("i: int", IdSchema(ARRAY_TYPE)),
        ),
        Array.from_items(
            [((1,), (1, None, 3)), ((2,), ())],
            ("[i: int]", list_of(optional(int))),
        ),
        Array.from_items(
            [
                ((1,), {"meta": {"raw": b"a"}, "name": "one", "scores": (1.0, 2.0)}),
                ((2,), {"meta": None, "name": "two", "scores": ()}),
            ],
            (
                "[i: int]",
                struct_of(
                    meta=optional(struct_of(raw=bytes)),
                    name=str,
                    scores=list_of(float),
                ),
            ),
        ),
        Array.from_items(
            [((1,), None), ((2,), {"label": "two"})],
            ("[i: int]", optional(struct_of(label=str))),
        ),
        Array.scalar({}, struct_of()),
        Array.from_items(
            [((1,), None), ((2,), {})],
            ("[ticket: int]", optional(struct_of())),
        ),
        Array.scalar(
            {"metadata": {}, "events": ({}, {})},
            struct_of(metadata=struct_of(), events=list_of(struct_of())),
        ),
    ]
