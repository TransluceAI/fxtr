"""Tests for step requests: the fingerprint entity and the cache key."""

from __future__ import annotations

import pytest
from frozendict import frozendict

from fxtr.entity_defns.array import Array, ArrayType
from fxtr.runner import nodes
from fxtr.runner.steps import StepRequest, cache_key, fingerprint_of, request_for
from fxtr.storage.memory_storage import MemoryStorage

pytestmark = pytest.mark.anyio


async def run_step(**overrides: object) -> nodes.RunStep:
    storage = MemoryStorage()
    source = nodes.SourceArray(name="x", array=await storage.store(Array.scalar(1, int)))
    fields: dict[str, object] = dict(
        name="score",
        step_name="tests.score",
        inputs=frozendict({"x": source.content_id()}),
        map_over=("prompt",),
        join="exact",
        replicas=frozendict(),
        cache_key_prefix="root/score",
        type=ArrayType.of(("[prompt: int]", float)),
    )
    fields.update(overrides)
    return nodes.RunStep(**fields)  # type: ignore[arg-type]


async def test_fingerprint_is_the_cid_of_the_call_and_ignores_who_asked() -> None:
    storage = MemoryStorage()
    x = await storage.store(Array.scalar(1, int))
    y = await storage.store(Array.scalar(2, int))
    same_type_other_name = await run_step(name="other", cache_key_prefix="elsewhere/score")
    a = request_for(await run_step(), {"x": x})
    b = request_for(same_type_other_name, {"x": x})
    assert fingerprint_of(a) == fingerprint_of(b)  # the node's name and key are not the call
    assert fingerprint_of(a) != fingerprint_of(request_for(await run_step(), {"x": y}))
    assert fingerprint_of(a) != fingerprint_of(
        request_for(await run_step(step_name="tests.other"), {"x": x})
    )
    # The declared result type is not part of the call.
    retyped = await run_step(type=ArrayType.of(("[prompt: int]", int)))
    assert fingerprint_of(a) == fingerprint_of(request_for(retyped, {"x": x}))


async def test_request_round_trips_through_storage_under_its_fingerprint() -> None:
    storage = MemoryStorage()
    x = await storage.store(Array.scalar(1, int))
    request = request_for(await run_step(), {"x": x})
    ref = await storage.store(request)
    assert ref.entity_id == fingerprint_of(request)
    loaded = await storage.load(ref)
    assert isinstance(loaded, StepRequest)
    # BoundIDs compare by loader too; the call is the same when its ids are.
    assert fingerprint_of(loaded) == fingerprint_of(request)
    assert {k: v.entity_id for k, v in loaded.inputs.items()} == {"x": x.entity_id}


async def test_cache_key_appends_the_mapped_key_to_the_prefix() -> None:
    node = await run_step()
    assert cache_key(node, {}) == "root/score"
    assert cache_key(node, {"prompt": 3}) == "root/score[prompt:3]"
    assert cache_key(node, {"prompt": 3, "rep": "a"}) == 'root/score[prompt:3,rep:"a"]'
