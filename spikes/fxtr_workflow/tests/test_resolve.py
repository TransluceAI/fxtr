"""Tests for arg resolution and cache key computation."""

import pytest

from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr_workflow.pending import Pending
from fxtr_workflow.resolve import collect_cache_slot_args, compute_cache_key, resolve_args
from fxtr_workflow.step_handle import ExternalStepHandle

from .conftest import StepKeyEntity, make_dummy_slot

pytestmark = pytest.mark.anyio


# -- resolve_args tests --


async def test_resolve_primitives():
    args, kwargs = await resolve_args((1, "hello", True), {"x": 3.14})
    assert args == (1, "hello", True)
    assert kwargs == {"x": 3.14}


async def test_resolve_nested_pending():
    p: Pending[int] = Pending()
    p.set_result(42)

    args, kwargs = await resolve_args(([p, "a"], {"nested": p}), {})
    assert args == ([42, "a"], {"nested": 42})


async def test_resolve_pending_chain():
    """A Pending that resolves to another Pending should resolve both."""
    inner: Pending[int] = Pending()
    inner.set_result(99)
    outer: Pending[Pending[int]] = Pending()
    outer.set_result(inner)

    args, _ = await resolve_args((outer,), {})
    assert args == (99,)


# -- compute_cache_key tests --


async def test_cache_key_deterministic():
    async with FxtrScope() as scope:
        key_id = StepKeyEntity(label="step_a").store().entity_id
        k1 = compute_cache_key(key_id, (1, "hello"), {"x": True})
        k2 = compute_cache_key(key_id, (1, "hello"), {"x": True})
        assert k1.entity_id == k2.entity_id


async def test_cache_key_differs_with_different_args():
    async with FxtrScope() as scope:
        key_id = StepKeyEntity(label="step_a").store().entity_id
        k1 = compute_cache_key(key_id, (1,), {})
        k2 = compute_cache_key(key_id, (2,), {})
        assert k1.entity_id != k2.entity_id


async def test_cache_key_differs_with_different_step():
    async with FxtrScope() as scope:
        key_a = StepKeyEntity(label="step_a").store().entity_id
        key_b = StepKeyEntity(label="step_b").store().entity_id
        k1 = compute_cache_key(key_a, (1,), {})
        k2 = compute_cache_key(key_b, (1,), {})
        assert k1.entity_id != k2.entity_id


# -- collect_cache_slot_args tests --


def test_collect_no_slots():
    entries = collect_cache_slot_args((1, "hello"), {"x": True})
    assert entries == []


def test_collect_top_level_slots():
    s1 = make_dummy_slot("s1")
    s2 = make_dummy_slot("s2")
    entries = collect_cache_slot_args((s1, "between", s2), {})
    assert len(entries) == 2
    assert entries[0][0] == "args[0]"
    assert entries[0][1] is s1
    assert entries[1][0] == "args[2]"
    assert entries[1][1] is s2


def test_collect_nested_slots():
    s1 = make_dummy_slot("s1")
    entries = collect_cache_slot_args(([s1],), {"k": {"inner": s1}})
    assert len(entries) == 2
    paths = [e[0] for e in entries]
    assert "args[0][0]" in paths
    assert "kwargs[k][inner]" in paths


# -- cache key edge cases (#8, #9, #10) --


async def test_cache_key_step_handle_as_arg():
    """Different step handles as args produce different cache keys."""
    async with FxtrScope() as scope:
        key_id = StepKeyEntity(label="caller").store().entity_id

        async def dummy_a(ctx, *args, **kwargs):
            pass

        async def dummy_b(ctx, *args, **kwargs):
            pass

        key_a = StepKeyEntity(label="handle_a").store().entity_id
        key_b = StepKeyEntity(label="handle_b").store().entity_id
        handle_a = ExternalStepHandle(dummy_a, key_a)
        handle_b = ExternalStepHandle(dummy_b, key_b)

        k1 = compute_cache_key(key_id, (handle_a,), {})
        k2 = compute_cache_key(key_id, (handle_b,), {})
        assert k1.entity_id != k2.entity_id

        # Same handle → same key
        k3 = compute_cache_key(key_id, (handle_a,), {})
        assert k1.entity_id == k3.entity_id


async def test_cache_key_empty_args():
    """No positional args and no kwargs produces a valid, deterministic cache key."""
    async with FxtrScope() as scope:
        key_id = StepKeyEntity(label="no_args").store().entity_id
        k1 = compute_cache_key(key_id, (), {})
        k2 = compute_cache_key(key_id, (), {})
        assert k1.entity_id == k2.entity_id


async def test_cache_key_bytes_and_float_args():
    """Bytes and float values round-trip through cache key computation."""
    async with FxtrScope() as scope:
        key_id = StepKeyEntity(label="types").store().entity_id

        k1 = compute_cache_key(key_id, (b"hello", 3.14), {})
        k2 = compute_cache_key(key_id, (b"hello", 3.14), {})
        assert k1.entity_id == k2.entity_id

        # Different bytes → different key
        k3 = compute_cache_key(key_id, (b"world", 3.14), {})
        assert k1.entity_id != k3.entity_id

        # Different float → different key
        k4 = compute_cache_key(key_id, (b"hello", 2.71), {})
        assert k1.entity_id != k4.entity_id
