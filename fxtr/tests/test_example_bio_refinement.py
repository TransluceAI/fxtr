"""Runs the example project's bio refinement experiment on Postgres."""

from __future__ import annotations

import pytest
from reference_backends import Executor

from basic_project.bio_refinement import Bio, refine_bio, refine_bios, rewrite, simulate
from fxtr.core.entities import BoundID
from fxtr.entity_defns.array import ArrayType

pytestmark = pytest.mark.anyio


async def test_children_loop_until_the_rewriter_stops_proposing(execute: Executor) -> None:
    record = await execute(refine_bios, refine_bio, simulate, rewrite)
    result = record.result
    assert result.type == ArrayType.of(("[bio: str]", BoundID[Bio]))
    # The backend stores raw ids (presentation is re-applied worker-side, not in the store),
    # so each id loads under its type.
    finals = {
        key[0]: await record.storage.load(bio_ref, as_type=Bio) for key, bio_ref in result.items()
    }
    # Both bios were refined up to the instructions' target detail, from different starting points.
    assert {name: bio.detail for name, bio in finals.items()} == {"alice": 2, "bob": 2}
    assert finals["alice"].persona == "a curious student, vivid detail 1, vivid detail 2"
    assert finals["bob"].persona == "a busy engineer, vivid detail 2"
    # One child per bio, at a pathname carrying the mapped key, with its own cache key prefix.
    assert sorted(record.workflows) == [
        "root",
        'root/refine[bio:"alice"]',
        'root/refine[bio:"bob"]',
    ]
    # The number of rounds is data-dependent: alice started two steps short of the target, bob one.
    # The final round's review proposes nothing, so alice ran 3 rounds and bob 2.
    rounds = {
        name: sorted(k for k in record.steps if k.startswith(f'root/refine[bio:"{name}"]/rewrite_'))
        for name in ("alice", "bob")
    }
    assert rounds["alice"] == [f'root/refine[bio:"alice"]/rewrite_{i}' for i in range(3)]
    assert rounds["bob"] == [f'root/refine[bio:"bob"]/rewrite_{i}' for i in range(2)]
