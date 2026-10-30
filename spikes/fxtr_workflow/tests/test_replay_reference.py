"""Tests for replay reference modes (strict + best-effort)."""

from typing import Any

import pytest

from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr_workflow.cache_slot import ReplayMode
from fxtr_workflow.entrypoint import TopLevelEntrypoint
from fxtr_workflow.errors import CacheSlotError, StepAbortError
from fxtr_workflow.step_context import StepContext

from .conftest import StepKeyEntity

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Test 29: strict mode — reference matches → result reused
# ---------------------------------------------------------------------------


async def test_strict_reference_matches_reuses_result(db_path: str):
    """When reference has matching cached result, strict mode reuses it."""
    exec_count = 0

    async def produce(ctx: StepContext, x: int) -> int:
        nonlocal exec_count
        exec_count += 1
        return x * 10

    # First: populate reference namespace
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with TopLevelEntrypoint() as ep:
            key = StepKeyEntity(label="produce").store().entity_id
            step = ep.bind_external_step(produce, key)
            slot = ep.open_cache_slot(ref_ns, "root")
            result = await slot.run(step, 5)
            assert result == 50
            assert exec_count == 1

    # Second: open new namespace with reference to the old one
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with SQLiteFxtrNamespace(db_path, "target") as target_ns:
            async with TopLevelEntrypoint() as ep:
                key = StepKeyEntity(label="produce").store().entity_id
                step = ep.bind_external_step(produce, key)

                ref_slot = ep.open_cache_slot(ref_ns, "root")
                target_slot = ep.open_cache_slot(
                    target_ns,
                    "root",
                    replay_reference=ref_slot,
                    replay_mode=ReplayMode.STRICT,
                )
                result = await target_slot.run(step, 5)
                assert result == 50
                assert exec_count == 1  # NOT re-executed


# ---------------------------------------------------------------------------
# Test 30: best-effort mode — reference matches → result reused
# ---------------------------------------------------------------------------


async def test_best_effort_reference_matches_reuses_result(db_path: str):
    """Best-effort mode also reuses matching cached results."""
    exec_count = 0

    async def produce(ctx: StepContext, x: int) -> int:
        nonlocal exec_count
        exec_count += 1
        return x * 10

    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with TopLevelEntrypoint() as ep:
            key = StepKeyEntity(label="produce").store().entity_id
            step = ep.bind_external_step(produce, key)
            slot = ep.open_cache_slot(ref_ns, "root")
            await slot.run(step, 5)
            assert exec_count == 1

    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with SQLiteFxtrNamespace(db_path, "target") as target_ns:
            async with TopLevelEntrypoint() as ep:
                key = StepKeyEntity(label="produce").store().entity_id
                step = ep.bind_external_step(produce, key)

                ref_slot = ep.open_cache_slot(ref_ns, "root")
                target_slot = ep.open_cache_slot(
                    target_ns,
                    "root",
                    replay_reference=ref_slot,
                    replay_mode=ReplayMode.BEST_EFFORT,
                )
                result = await target_slot.run(step, 5)
                assert result == 50
                assert exec_count == 1


# ---------------------------------------------------------------------------
# Test 31: strict mode — cache key mismatch → error
# ---------------------------------------------------------------------------


async def test_strict_cache_key_mismatch_errors(db_path: str):
    """Strict mode errors when reference has different cache key."""

    async def produce(ctx: StepContext, x: int) -> int:
        return x * 10

    # Populate reference with produce(5)
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with TopLevelEntrypoint() as ep:
            key = StepKeyEntity(label="produce").store().entity_id
            step = ep.bind_external_step(produce, key)
            slot = ep.open_cache_slot(ref_ns, "root")
            await slot.run(step, 5)

    # Try to replay with produce(9) — different args → different cache key
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with SQLiteFxtrNamespace(db_path, "target") as target_ns:
            async with TopLevelEntrypoint() as ep:
                key = StepKeyEntity(label="produce").store().entity_id
                step = ep.bind_external_step(produce, key)

                ref_slot = ep.open_cache_slot(ref_ns, "root")
                target_slot = ep.open_cache_slot(
                    target_ns,
                    "root",
                    replay_reference=ref_slot,
                    replay_mode=ReplayMode.STRICT,
                )
                pending = target_slot.run(step, 9)
                with pytest.raises(CacheSlotError, match="mismatch"):
                    await pending


# ---------------------------------------------------------------------------
# Test 32: best-effort mode — cache key mismatch → executes fresh
# ---------------------------------------------------------------------------


async def test_best_effort_cache_key_mismatch_executes_fresh(db_path: str):
    """Best-effort mode executes fresh when reference has different cache key."""
    exec_count = 0

    async def produce(ctx: StepContext, x: int) -> int:
        nonlocal exec_count
        exec_count += 1
        return x * 10

    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with TopLevelEntrypoint() as ep:
            key = StepKeyEntity(label="produce").store().entity_id
            step = ep.bind_external_step(produce, key)
            slot = ep.open_cache_slot(ref_ns, "root")
            await slot.run(step, 5)
            assert exec_count == 1

    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with SQLiteFxtrNamespace(db_path, "target") as target_ns:
            async with TopLevelEntrypoint() as ep:
                key = StepKeyEntity(label="produce").store().entity_id
                step = ep.bind_external_step(produce, key)

                ref_slot = ep.open_cache_slot(ref_ns, "root")
                target_slot = ep.open_cache_slot(
                    target_ns,
                    "root",
                    replay_reference=ref_slot,
                    replay_mode=ReplayMode.BEST_EFFORT,
                )
                result = await target_slot.run(step, 9)
                assert result == 90
                assert exec_count == 2  # Re-executed with new args


# ---------------------------------------------------------------------------
# Test 33: strict mode — replay check fails → error
# ---------------------------------------------------------------------------


async def test_strict_replay_check_fails_errors(db_path: str):
    """Strict mode errors when replay check fails (foreign slot data changed)."""
    handles: dict[str, Any] = {}

    async def produce(ctx: StepContext, x: int) -> int:
        return x * 10

    async def consume(ctx: StepContext, data_slot: Any) -> int:
        val = await data_slot.subslot("val").run(handles["produce"], 5)
        return val + 1

    # Populate reference: fill slot B, run A(B)
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with TopLevelEntrypoint() as ep:
            key_p = StepKeyEntity(label="produce").store().entity_id
            key_c = StepKeyEntity(label="consume").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_p)
            handle_c = ep.bind_external_step(consume, key_c)

            slot_b = ep.open_cache_slot(ref_ns, "B")
            await slot_b.subslot("val").run(handles["produce"], 5)

            slot_a = ep.open_cache_slot(ref_ns, "A")
            await slot_a.run(handle_c, slot_b)

    # Replay with different slot B data
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with SQLiteFxtrNamespace(db_path, "target") as target_ns:
            async with TopLevelEntrypoint() as ep:
                key_p = StepKeyEntity(label="produce").store().entity_id
                key_c = StepKeyEntity(label="consume").store().entity_id
                handles["produce"] = ep.bind_external_step(produce, key_p)
                handle_c = ep.bind_external_step(consume, key_c)

                # Target has different data in B
                slot_b_target = ep.open_cache_slot(target_ns, "B")
                await slot_b_target.subslot("val").run(handles["produce"], 99)

                ref_slot_a = ep.open_cache_slot(ref_ns, "A")
                target_slot_a = ep.open_cache_slot(
                    target_ns,
                    "A",
                    replay_reference=ref_slot_a,
                    replay_mode=ReplayMode.STRICT,
                )
                pending = target_slot_a.run(handle_c, slot_b_target)
                with pytest.raises(CacheSlotError):
                    await pending


# ---------------------------------------------------------------------------
# Test 34: best-effort — replay check fails → re-executes
# ---------------------------------------------------------------------------


async def test_best_effort_replay_check_fails_reexecutes(db_path: str):
    """Best-effort re-executes when replay check fails.

    The consume step dispatches produce(5) into data_slot.subslot("val").
    In the reference, B/s/val has produce(5) → 50. In the target, B/s/val
    already has produce(5) → 50 (same data, same key). The replay check
    fails because B's overall state entity is different (different namespace).
    But when re-executing, the inner dispatch succeeds because the cache key matches.

    To trigger an actual replay check failure, we use a step that receives
    a slot arg but doesn't dispatch into it — it just reads the slot's state.
    Actually the simplest approach: reference has A cached with slot B as arg.
    Target provides a completely empty slot C (different state from B).
    """
    exec_count = 0
    handles: dict[str, Any] = {}

    async def produce(ctx: StepContext, x: int) -> int:
        return x * 10

    async def consume(ctx: StepContext, data_slot: Any) -> int:
        nonlocal exec_count
        exec_count += 1
        val = await data_slot.subslot("val").run(handles["produce"], 5)
        return val + 1

    # Populate reference: fill B, run A(B)
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with TopLevelEntrypoint() as ep:
            key_p = StepKeyEntity(label="produce").store().entity_id
            key_c = StepKeyEntity(label="consume").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_p)
            handle_c = ep.bind_external_step(consume, key_c)

            slot_b = ep.open_cache_slot(ref_ns, "B")
            await slot_b.subslot("val").run(handles["produce"], 5)

            slot_a = ep.open_cache_slot(ref_ns, "A")
            await slot_a.run(handle_c, slot_b)
            assert exec_count == 1

    # Replay: target provides empty slot C instead of filled B.
    # Replay check fails (C is empty, B was Finished). Best-effort re-executes.
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with SQLiteFxtrNamespace(db_path, "target") as target_ns:
            async with TopLevelEntrypoint() as ep:
                key_p = StepKeyEntity(label="produce").store().entity_id
                key_c = StepKeyEntity(label="consume").store().entity_id
                handles["produce"] = ep.bind_external_step(produce, key_p)
                handle_c = ep.bind_external_step(consume, key_c)

                # Empty slot C — different state from filled B
                slot_c = ep.open_cache_slot(target_ns, "C")

                ref_slot_a = ep.open_cache_slot(ref_ns, "A")
                target_slot_a = ep.open_cache_slot(
                    target_ns,
                    "A",
                    replay_reference=ref_slot_a,
                    replay_mode=ReplayMode.BEST_EFFORT,
                )
                result = await target_slot_a.run(handle_c, slot_c)
                assert result == 51  # produce(5) → 50, + 1
                assert exec_count == 2


# ---------------------------------------------------------------------------
# Test 35: subslot propagation
# ---------------------------------------------------------------------------


async def test_subslot_propagation(db_path: str):
    """Reference subslots are propagated to child slots."""
    exec_count = 0

    async def produce(ctx: StepContext, x: int) -> int:
        nonlocal exec_count
        exec_count += 1
        return x * 10

    # Populate reference with subslots
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with TopLevelEntrypoint() as ep:
            key = StepKeyEntity(label="produce").store().entity_id
            step = ep.bind_external_step(produce, key)
            slot = ep.open_cache_slot(ref_ns, "root")
            await slot.subslot("a").run(step, 1)
            await slot.subslot("b").run(step, 2)
            assert exec_count == 2

    # Replay — subslots should be reused from reference
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with SQLiteFxtrNamespace(db_path, "target") as target_ns:
            async with TopLevelEntrypoint() as ep:
                key = StepKeyEntity(label="produce").store().entity_id
                step = ep.bind_external_step(produce, key)

                ref_slot = ep.open_cache_slot(ref_ns, "root")
                target_slot = ep.open_cache_slot(
                    target_ns,
                    "root",
                    replay_reference=ref_slot,
                    replay_mode=ReplayMode.STRICT,
                )
                r_a = await target_slot.subslot("a").run(step, 1)
                r_b = await target_slot.subslot("b").run(step, 2)
                assert r_a == 10
                assert r_b == 20
                assert exec_count == 2  # NOT re-executed


# ---------------------------------------------------------------------------
# Test 38: no replay reference → current behavior unchanged
# ---------------------------------------------------------------------------


async def test_no_replay_reference_unchanged(db_path: str):
    """Without replay reference, behavior is identical to before."""
    exec_count = 0

    async def produce(ctx: StepContext, x: int) -> int:
        nonlocal exec_count
        exec_count += 1
        return x * 10

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key = StepKeyEntity(label="produce").store().entity_id
            step = ep.bind_external_step(produce, key)
            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run(step, 5)
            assert result == 50
            assert exec_count == 1

    # Re-run — should use local cache
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key = StepKeyEntity(label="produce").store().entity_id
            step = ep.bind_external_step(produce, key)
            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run(step, 5)
            assert result == 50
            assert exec_count == 1


# ---------------------------------------------------------------------------
# Test 40: fallible run with replay reference
# ---------------------------------------------------------------------------


async def test_fallible_run_with_replay_reference(db_path: str):
    """Cached fallible error is replayed from reference."""
    from fxtr_workflow.errors import WrappedFallibleRunError

    exec_count = 0

    async def failing_step(ctx: StepContext) -> int:
        nonlocal exec_count
        exec_count += 1
        raise ValueError("intentional")

    # Populate reference with fallible error
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with TopLevelEntrypoint() as ep:
            key = StepKeyEntity(label="failing").store().entity_id
            step = ep.bind_external_step(failing_step, key)
            slot = ep.open_cache_slot(ref_ns, "root")
            pending = slot.run_fallible(step)
            with pytest.raises(WrappedFallibleRunError):
                await pending
            assert exec_count == 1

    # Replay — should re-raise the cached error without re-executing
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with SQLiteFxtrNamespace(db_path, "target") as target_ns:
            async with TopLevelEntrypoint() as ep:
                key = StepKeyEntity(label="failing").store().entity_id
                step = ep.bind_external_step(failing_step, key)

                ref_slot = ep.open_cache_slot(ref_ns, "root")
                target_slot = ep.open_cache_slot(
                    target_ns,
                    "root",
                    replay_reference=ref_slot,
                    replay_mode=ReplayMode.STRICT,
                )
                pending = target_slot.run_fallible(step)
                with pytest.raises(WrappedFallibleRunError, match="intentional"):
                    await pending
                assert exec_count == 1  # NOT re-executed


# ---------------------------------------------------------------------------
# Test 41: nested replay
# ---------------------------------------------------------------------------


async def test_nested_replay(db_path: str):
    """Step A realized from reference, inner steps also realized."""
    exec_counts: dict[str, int] = {"inner": 0, "outer": 0}
    handles: dict[str, Any] = {}

    async def inner(ctx: StepContext, x: int) -> int:
        exec_counts["inner"] += 1
        return x * 10

    async def outer(ctx: StepContext) -> int:
        exec_counts["outer"] += 1
        return await ctx.cache_slot("work").run(handles["inner"], 5)

    # Populate reference
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with TopLevelEntrypoint() as ep:
            key_i = StepKeyEntity(label="inner").store().entity_id
            key_o = StepKeyEntity(label="outer").store().entity_id
            handles["inner"] = ep.bind_external_step(inner, key_i)
            handle_o = ep.bind_external_step(outer, key_o)
            slot = ep.open_cache_slot(ref_ns, "root")
            result = await slot.run(handle_o)
            assert result == 50
            assert exec_counts == {"inner": 1, "outer": 1}

    # Replay in new namespace
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with SQLiteFxtrNamespace(db_path, "target") as target_ns:
            async with TopLevelEntrypoint() as ep:
                key_i = StepKeyEntity(label="inner").store().entity_id
                key_o = StepKeyEntity(label="outer").store().entity_id
                handles["inner"] = ep.bind_external_step(inner, key_i)
                handle_o = ep.bind_external_step(outer, key_o)

                ref_slot = ep.open_cache_slot(ref_ns, "root")
                target_slot = ep.open_cache_slot(
                    target_ns,
                    "root",
                    replay_reference=ref_slot,
                    replay_mode=ReplayMode.STRICT,
                )
                result = await target_slot.run(handle_o)
                assert result == 50
                assert exec_counts == {"inner": 1, "outer": 1}  # Neither re-executed


# ---------------------------------------------------------------------------
# Test 42: _ensure_split with replay reference
# ---------------------------------------------------------------------------


async def test_ensure_split_with_replay_reference(db_path: str):
    """Reference is Split → realize. Reference is Finished → strict error."""
    exec_count = 0

    async def produce(ctx: StepContext, x: int) -> int:
        nonlocal exec_count
        exec_count += 1
        return x * 10

    # Populate reference with subslots (Split state)
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with TopLevelEntrypoint() as ep:
            key = StepKeyEntity(label="produce").store().entity_id
            step = ep.bind_external_step(produce, key)
            slot = ep.open_cache_slot(ref_ns, "root")
            await slot.subslot("a").run(step, 1)

    # Replay with subslots — should realize Split from reference
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with SQLiteFxtrNamespace(db_path, "target") as target_ns:
            async with TopLevelEntrypoint() as ep:
                key = StepKeyEntity(label="produce").store().entity_id
                step = ep.bind_external_step(produce, key)

                ref_slot = ep.open_cache_slot(ref_ns, "root")
                target_slot = ep.open_cache_slot(
                    target_ns,
                    "root",
                    replay_reference=ref_slot,
                    replay_mode=ReplayMode.STRICT,
                )
                result = await target_slot.subslot("a").run(step, 1)
                assert result == 10
                assert exec_count == 1  # Reused from reference


# ---------------------------------------------------------------------------
# Test: best-effort drops reference on structural mismatch in _ensure_split
# ---------------------------------------------------------------------------


async def test_ensure_split_best_effort_drops_on_mismatch(db_path: str):
    """Best-effort drops reference when _ensure_split finds Finished instead of Split."""
    exec_count = 0

    async def produce(ctx: StepContext, x: int) -> int:
        nonlocal exec_count
        exec_count += 1
        return x * 10

    # Populate reference with a simple run (Finished, not Split)
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with TopLevelEntrypoint() as ep:
            key = StepKeyEntity(label="produce").store().entity_id
            step = ep.bind_external_step(produce, key)
            slot = ep.open_cache_slot(ref_ns, "root")
            await slot.run(step, 5)
            assert exec_count == 1

    # Try to use subslots in target — reference is Finished, not Split
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with SQLiteFxtrNamespace(db_path, "target") as target_ns:
            async with TopLevelEntrypoint() as ep:
                key = StepKeyEntity(label="produce").store().entity_id
                step = ep.bind_external_step(produce, key)

                ref_slot = ep.open_cache_slot(ref_ns, "root")
                target_slot = ep.open_cache_slot(
                    target_ns,
                    "root",
                    replay_reference=ref_slot,
                    replay_mode=ReplayMode.BEST_EFFORT,
                )
                # Best effort should drop reference and create Split fresh
                result = await target_slot.subslot("a").run(step, 3)
                assert result == 30
                assert exec_count == 2  # Executed fresh


# ---------------------------------------------------------------------------
# Test 36: realization during replay check
# ---------------------------------------------------------------------------


async def test_realization_during_replay_check(db_path: str):
    """Foreign slot empty in target + reference has matching state → realized.

    Reference has A(B) cached. B has data. Target has empty B. During replay
    check, B's state should be realized from the reference if B has a replay ref.
    """
    handles: dict[str, Any] = {}
    exec_count = 0

    async def produce(ctx: StepContext, x: int) -> int:
        return x * 10

    async def consume(ctx: StepContext, data_slot: Any) -> int:
        nonlocal exec_count
        exec_count += 1
        val = await data_slot.subslot("val").run(handles["produce"], 5)
        return val + 1

    # Populate reference: fill B, run A(B)
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with TopLevelEntrypoint() as ep:
            key_p = StepKeyEntity(label="produce").store().entity_id
            key_c = StepKeyEntity(label="consume").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_p)
            handle_c = ep.bind_external_step(consume, key_c)

            slot_b = ep.open_cache_slot(ref_ns, "B")
            await slot_b.subslot("val").run(handles["produce"], 5)

            slot_a = ep.open_cache_slot(ref_ns, "A")
            await slot_a.run(handle_c, slot_b)
            assert exec_count == 1

    # Replay: target B is empty but has replay ref to reference B.
    # A also has replay ref. Replay check on A should realize B's state
    # from the reference and succeed.
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with SQLiteFxtrNamespace(db_path, "target") as target_ns:
            async with TopLevelEntrypoint() as ep:
                key_p = StepKeyEntity(label="produce").store().entity_id
                key_c = StepKeyEntity(label="consume").store().entity_id
                handles["produce"] = ep.bind_external_step(produce, key_p)
                handle_c = ep.bind_external_step(consume, key_c)

                ref_slot_b = ep.open_cache_slot(ref_ns, "B")
                target_slot_b = ep.open_cache_slot(
                    target_ns,
                    "B",
                    replay_reference=ref_slot_b,
                    replay_mode=ReplayMode.STRICT,
                )

                ref_slot_a = ep.open_cache_slot(ref_ns, "A")
                target_slot_a = ep.open_cache_slot(
                    target_ns,
                    "A",
                    replay_reference=ref_slot_a,
                    replay_mode=ReplayMode.STRICT,
                )
                result = await target_slot_a.run(handle_c, target_slot_b)
                assert result == 51
                assert exec_count == 1  # NOT re-executed — realized from reference


# ---------------------------------------------------------------------------
# Test 37: partial realization — some foreign slots match, some don't
# ---------------------------------------------------------------------------


async def test_partial_realization_replay_check(db_path: str):
    """Replay check with two foreign slots: one matches (realized), one doesn't.

    Step A takes two foreign slots (X and Y), dispatches into both.
    Reference has A cached with X_ref and Y_ref. Target provides X with a
    replay ref (matches X_ref), and Y with different data (no match).
    Replay check should fail overall. In best-effort, A re-executes.
    """
    handles: dict[str, Any] = {}
    exec_count = 0

    async def produce(ctx: StepContext, x: int) -> int:
        return x * 10

    async def consume_two(ctx: StepContext, slot_x: Any, slot_y: Any) -> int:
        nonlocal exec_count
        exec_count += 1
        a = await slot_x.subslot("v").run(handles["produce"], 1)
        b = await slot_y.subslot("v").run(handles["produce"], 2)
        return a + b

    # Populate reference: fill X and Y, run A(X, Y)
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with TopLevelEntrypoint() as ep:
            key_p = StepKeyEntity(label="produce_pr").store().entity_id
            key_c = StepKeyEntity(label="consume_two_pr").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_p)
            handle_c = ep.bind_external_step(consume_two, key_c)

            slot_x = ep.open_cache_slot(ref_ns, "X")
            await slot_x.subslot("v").run(handles["produce"], 1)
            slot_y = ep.open_cache_slot(ref_ns, "Y")
            await slot_y.subslot("v").run(handles["produce"], 2)

            slot_a = ep.open_cache_slot(ref_ns, "A")
            result = await slot_a.run(handle_c, slot_x, slot_y)
            assert result == 30  # 10 + 20
            assert exec_count == 1

    # Replay best-effort: X has replay ref (matches), Y has different data
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with SQLiteFxtrNamespace(db_path, "target") as target_ns:
            async with TopLevelEntrypoint() as ep:
                key_p = StepKeyEntity(label="produce_pr").store().entity_id
                key_c = StepKeyEntity(label="consume_two_pr").store().entity_id
                handles["produce"] = ep.bind_external_step(produce, key_p)
                handle_c = ep.bind_external_step(consume_two, key_c)

                # X with replay ref — will match reference
                ref_x = ep.open_cache_slot(ref_ns, "X")
                target_x = ep.open_cache_slot(
                    target_ns,
                    "X",
                    replay_reference=ref_x,
                    replay_mode=ReplayMode.BEST_EFFORT,
                )

                # Y is empty — will NOT match reference (which has Y/s/v filled)
                target_y = ep.open_cache_slot(target_ns, "Y")

                ref_a = ep.open_cache_slot(ref_ns, "A")
                target_a = ep.open_cache_slot(
                    target_ns,
                    "A",
                    replay_reference=ref_a,
                    replay_mode=ReplayMode.BEST_EFFORT,
                )
                result = await target_a.run(handle_c, target_x, target_y)
                # Re-executed because replay check failed (Y mismatch)
                assert exec_count == 2
                # Result uses target_x (realized from ref: produce(1)→10)
                # and target_y (local: produce(2)→20, since consume dispatches produce(2))
                assert result == 30  # 10 + 20


# ---------------------------------------------------------------------------
# Test 39: Partial reference with matching cache key
# ---------------------------------------------------------------------------


async def test_partial_reference_matching_key_resumes(db_path: str):
    """Reference has Partial state with matching cache key → realize + resume.

    The reference's first run succeeds at an inner step ("work") before failing
    at a later step ("fail_step"). The inner step's result is cached in the
    reference's /c/ pins. When the target resumes, the inner step should be
    reused from the reference's live pins via LiveRunReference.
    """
    handles: dict[str, Any] = {}
    fail_flag = True
    exec_counts: dict[str, int] = {"inner": 0, "outer": 0}

    async def inner(ctx: StepContext, x: int) -> int:
        exec_counts["inner"] += 1
        return x * 10

    async def failing_inner(ctx: StepContext) -> int:
        raise ValueError("intentional failure")

    async def outer(ctx: StepContext) -> int:
        exec_counts["outer"] += 1
        # This inner step succeeds even when outer ultimately fails
        val = await ctx.cache_slot("work").run(handles["inner"], 5)
        if fail_flag:
            # This causes the outer step to abort, leaving Partial state
            # but "work" is already cached in /c/ pins
            await ctx.cache_slot("boom").run(handles["failing_inner"])
        return val

    # Populate reference: outer runs inner(5)→50, then fails at failing_inner
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with TopLevelEntrypoint() as ep:
            key_i = StepKeyEntity(label="inner_pr").store().entity_id
            key_f = StepKeyEntity(label="failing_pr").store().entity_id
            key_o = StepKeyEntity(label="outer_pr").store().entity_id
            handles["inner"] = ep.bind_external_step(inner, key_i)
            handles["failing_inner"] = ep.bind_external_step(failing_inner, key_f)
            handle_o = ep.bind_external_step(outer, key_o)
            slot = ep.open_cache_slot(ref_ns, "root")
            pending = slot.run(handle_o)
            with pytest.raises(StepAbortError):
                await pending
            assert exec_counts == {"inner": 1, "outer": 1}

    # Replay: target resumes from Partial. The inner "work" step should be
    # reused from the reference's live /c/ pins (via LiveRunReference).
    fail_flag = False
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with SQLiteFxtrNamespace(db_path, "target") as target_ns:
            async with TopLevelEntrypoint() as ep:
                key_i = StepKeyEntity(label="inner_pr").store().entity_id
                key_f = StepKeyEntity(label="failing_pr").store().entity_id
                key_o = StepKeyEntity(label="outer_pr").store().entity_id
                handles["inner"] = ep.bind_external_step(inner, key_i)
                handles["failing_inner"] = ep.bind_external_step(failing_inner, key_f)
                handle_o = ep.bind_external_step(outer, key_o)

                ref_slot = ep.open_cache_slot(ref_ns, "root")
                target_slot = ep.open_cache_slot(
                    target_ns,
                    "root",
                    replay_reference=ref_slot,
                    replay_mode=ReplayMode.STRICT,
                )
                result = await target_slot.run(handle_o)
                assert result == 50
                assert exec_counts["outer"] == 2  # Re-executed (resumed from Partial)
                assert exec_counts["inner"] == 1  # NOT re-executed — reused from reference


# ---------------------------------------------------------------------------
# Test 43: internal cache slot reference resolution
# ---------------------------------------------------------------------------


async def test_internal_cache_slot_reference_from_frozen_tree(db_path: str):
    """When reference is Finished, internal cache slots use frozen tree for replay refs."""
    handles: dict[str, Any] = {}
    exec_counts: dict[str, int] = {"inner": 0, "outer": 0}

    async def inner(ctx: StepContext, x: int) -> int:
        exec_counts["inner"] += 1
        return x * 10

    async def outer(ctx: StepContext) -> int:
        exec_counts["outer"] += 1
        a = await ctx.cache_slot("a").run(handles["inner"], 1)
        b = await ctx.cache_slot("b").run(handles["inner"], 2)
        return a + b

    # Populate reference
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with TopLevelEntrypoint() as ep:
            key_i = StepKeyEntity(label="inner_ics").store().entity_id
            key_o = StepKeyEntity(label="outer_ics").store().entity_id
            handles["inner"] = ep.bind_external_step(inner, key_i)
            handle_o = ep.bind_external_step(outer, key_o)
            slot = ep.open_cache_slot(ref_ns, "root")
            result = await slot.run(handle_o)
            assert result == 30
            assert exec_counts == {"inner": 2, "outer": 1}

    # Replay — outer and both inner steps should be reused from frozen tree
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with SQLiteFxtrNamespace(db_path, "target") as target_ns:
            async with TopLevelEntrypoint() as ep:
                key_i = StepKeyEntity(label="inner_ics").store().entity_id
                key_o = StepKeyEntity(label="outer_ics").store().entity_id
                handles["inner"] = ep.bind_external_step(inner, key_i)
                handle_o = ep.bind_external_step(outer, key_o)

                ref_slot = ep.open_cache_slot(ref_ns, "root")
                target_slot = ep.open_cache_slot(
                    target_ns,
                    "root",
                    replay_reference=ref_slot,
                    replay_mode=ReplayMode.STRICT,
                )
                result = await target_slot.run(handle_o)
                assert result == 30
                assert exec_counts == {"inner": 2, "outer": 1}  # Nothing re-executed


# ---------------------------------------------------------------------------
# Test 44: best-effort reject of Partial reference with cache key mismatch
# ---------------------------------------------------------------------------


async def test_best_effort_partial_reference_key_mismatch(db_path: str):
    """Best-effort: Partial reference with different cache key → re-executes fresh."""
    handles: dict[str, Any] = {}
    exec_count = 0

    async def step_a(ctx: StepContext, x: int) -> int:
        nonlocal exec_count
        exec_count += 1
        return x * 10

    # Populate reference: run step_a(5) which fails → Partial
    fail_flag = True

    async def failing_step(ctx: StepContext, x: int) -> int:
        if fail_flag:
            raise ValueError("fail")
        return x * 10

    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with TopLevelEntrypoint() as ep:
            key = StepKeyEntity(label="step_bemr").store().entity_id
            step = ep.bind_external_step(failing_step, key)
            slot = ep.open_cache_slot(ref_ns, "root")
            pending = slot.run(step, 5)
            with pytest.raises(StepAbortError):
                await pending

    # Replay with different step (different cache key) in best-effort mode
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with SQLiteFxtrNamespace(db_path, "target") as target_ns:
            async with TopLevelEntrypoint() as ep:
                key = StepKeyEntity(label="step_a_bemr").store().entity_id
                step = ep.bind_external_step(step_a, key)

                ref_slot = ep.open_cache_slot(ref_ns, "root")
                target_slot = ep.open_cache_slot(
                    target_ns,
                    "root",
                    replay_reference=ref_slot,
                    replay_mode=ReplayMode.BEST_EFFORT,
                )
                result = await target_slot.run(step, 7)
                assert result == 70
                assert exec_count == 1  # Executed fresh


# ---------------------------------------------------------------------------
# Edge case: best-effort fallthrough caches in target, subsequent run uses local
# ---------------------------------------------------------------------------


async def test_best_effort_fallthrough_then_local_cache(db_path: str):
    """After best-effort reject + fresh execution, result is cached locally.
    A subsequent run (no reference) uses the local cache."""
    exec_count = 0

    async def produce(ctx: StepContext, x: int) -> int:
        nonlocal exec_count
        exec_count += 1
        return x * 10

    # Reference has produce(5)
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with TopLevelEntrypoint() as ep:
            key = StepKeyEntity(label="produce").store().entity_id
            step = ep.bind_external_step(produce, key)
            slot = ep.open_cache_slot(ref_ns, "root")
            await slot.run(step, 5)
            assert exec_count == 1

    # Best-effort with different args → reject + fresh execution
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with SQLiteFxtrNamespace(db_path, "target") as target_ns:
            async with TopLevelEntrypoint() as ep:
                key = StepKeyEntity(label="produce").store().entity_id
                step = ep.bind_external_step(produce, key)
                ref_slot = ep.open_cache_slot(ref_ns, "root")
                target_slot = ep.open_cache_slot(
                    target_ns,
                    "root",
                    replay_reference=ref_slot,
                    replay_mode=ReplayMode.BEST_EFFORT,
                )
                result = await target_slot.run(step, 9)
                assert result == 90
                assert exec_count == 2

    # Third run: target namespace only, no reference — should use local cache
    async with SQLiteFxtrNamespace(db_path, "target") as target_ns:
        async with TopLevelEntrypoint() as ep:
            key = StepKeyEntity(label="produce").store().entity_id
            step = ep.bind_external_step(produce, key)
            slot = ep.open_cache_slot(target_ns, "root")
            result = await slot.run(step, 9)
            assert result == 90
            assert exec_count == 2  # Still cached


# ---------------------------------------------------------------------------
# Edge case: local state takes precedence over reference
# ---------------------------------------------------------------------------


async def test_local_state_takes_precedence(db_path: str):
    """If target already has local state, reference is ignored."""
    exec_count = 0

    async def produce(ctx: StepContext, x: int) -> int:
        nonlocal exec_count
        exec_count += 1
        return x * 10

    # Populate reference
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with TopLevelEntrypoint() as ep:
            key = StepKeyEntity(label="produce").store().entity_id
            step = ep.bind_external_step(produce, key)
            slot = ep.open_cache_slot(ref_ns, "root")
            await slot.run(step, 5)
            assert exec_count == 1

    # Populate target with DIFFERENT data
    async with SQLiteFxtrNamespace(db_path, "target") as target_ns:
        async with TopLevelEntrypoint() as ep:
            key = StepKeyEntity(label="produce").store().entity_id
            step = ep.bind_external_step(produce, key)
            slot = ep.open_cache_slot(target_ns, "root")
            await slot.run(step, 9)
            assert exec_count == 2

    # Now open with reference — local state (produce(9)→90) should win
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with SQLiteFxtrNamespace(db_path, "target") as target_ns:
            async with TopLevelEntrypoint() as ep:
                key = StepKeyEntity(label="produce").store().entity_id
                step = ep.bind_external_step(produce, key)
                ref_slot = ep.open_cache_slot(ref_ns, "root")
                target_slot = ep.open_cache_slot(
                    target_ns,
                    "root",
                    replay_reference=ref_slot,
                    replay_mode=ReplayMode.STRICT,
                )
                result = await target_slot.run(step, 9)
                assert result == 90
                assert exec_count == 2  # Local cache, not re-executed


# ---------------------------------------------------------------------------
# Edge case: double replay chain (ns1 → ns2 → ns3)
# ---------------------------------------------------------------------------


async def test_double_replay_chain(db_path: str):
    """Run in ns1, replay into ns2, then replay ns2 into ns3."""
    exec_count = 0

    async def produce(ctx: StepContext, x: int) -> int:
        nonlocal exec_count
        exec_count += 1
        return x * 10

    # ns1: original execution
    async with SQLiteFxtrNamespace(db_path, "ns1") as ns1:
        async with TopLevelEntrypoint() as ep:
            key = StepKeyEntity(label="produce").store().entity_id
            step = ep.bind_external_step(produce, key)
            slot = ep.open_cache_slot(ns1, "root")
            await slot.run(step, 5)
            assert exec_count == 1

    # ns2: replay from ns1
    async with SQLiteFxtrNamespace(db_path, "ns1") as ns1:
        async with SQLiteFxtrNamespace(db_path, "ns2") as ns2:
            async with TopLevelEntrypoint() as ep:
                key = StepKeyEntity(label="produce").store().entity_id
                step = ep.bind_external_step(produce, key)
                ref_slot = ep.open_cache_slot(ns1, "root")
                target_slot = ep.open_cache_slot(
                    ns2,
                    "root",
                    replay_reference=ref_slot,
                    replay_mode=ReplayMode.STRICT,
                )
                result = await target_slot.run(step, 5)
                assert result == 50
                assert exec_count == 1

    # ns3: replay from ns2
    async with SQLiteFxtrNamespace(db_path, "ns2") as ns2:
        async with SQLiteFxtrNamespace(db_path, "ns3") as ns3:
            async with TopLevelEntrypoint() as ep:
                key = StepKeyEntity(label="produce").store().entity_id
                step = ep.bind_external_step(produce, key)
                ref_slot = ep.open_cache_slot(ns2, "root")
                target_slot = ep.open_cache_slot(
                    ns3,
                    "root",
                    replay_reference=ref_slot,
                    replay_mode=ReplayMode.STRICT,
                )
                result = await target_slot.run(step, 5)
                assert result == 50
                assert exec_count == 1  # Never re-executed


# ---------------------------------------------------------------------------
# Edge case: nested replay with mixed foreign + internal slots
# ---------------------------------------------------------------------------


async def test_nested_replay_mixed_foreign_internal(db_path: str):
    """Inner step receives both foreign slot and internal slot as args.
    Replay from reference should reuse everything."""
    handles: dict[str, Any] = {}
    exec_counts: dict[str, int] = {"produce": 0, "inner": 0, "outer": 0}

    async def produce(ctx: StepContext, x: int) -> int:
        exec_counts["produce"] += 1
        return x * 10

    async def inner(ctx: StepContext, internal_slot: Any, foreign_slot: Any) -> int:
        exec_counts["inner"] += 1
        a = await internal_slot.subslot("a").run(handles["produce"], 1)
        b = await foreign_slot.subslot("val").run(handles["produce"], 2)
        return a + b

    async def outer(ctx: StepContext, foreign: Any) -> int:
        exec_counts["outer"] += 1
        work = ctx.cache_slot("work")
        return await ctx.cache_slot("dispatch").run(handles["inner"], work, foreign)

    # Reference
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with TopLevelEntrypoint() as ep:
            key_p = StepKeyEntity(label="produce_mi").store().entity_id
            key_i = StepKeyEntity(label="inner_mi").store().entity_id
            key_o = StepKeyEntity(label="outer_mi").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_p)
            handles["inner"] = ep.bind_external_step(inner, key_i)
            handle_o = ep.bind_external_step(outer, key_o)

            slot_b = ep.open_cache_slot(ref_ns, "B")
            await slot_b.subslot("val").run(handles["produce"], 2)

            slot_a = ep.open_cache_slot(ref_ns, "A")
            result = await slot_a.run(handle_o, slot_b)
            assert result == 30  # 1*10 + 2*10
            # produce called twice: once for B/s/val, once for work/s/a
            # (B/s/val already cached when inner dispatches into it)
            assert exec_counts == {"produce": 2, "inner": 1, "outer": 1}

    # Replay: B has replay ref, A has replay ref
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with SQLiteFxtrNamespace(db_path, "target") as target_ns:
            async with TopLevelEntrypoint() as ep:
                key_p = StepKeyEntity(label="produce_mi").store().entity_id
                key_i = StepKeyEntity(label="inner_mi").store().entity_id
                key_o = StepKeyEntity(label="outer_mi").store().entity_id
                handles["produce"] = ep.bind_external_step(produce, key_p)
                handles["inner"] = ep.bind_external_step(inner, key_i)
                handle_o = ep.bind_external_step(outer, key_o)

                ref_b = ep.open_cache_slot(ref_ns, "B")
                target_b = ep.open_cache_slot(
                    target_ns,
                    "B",
                    replay_reference=ref_b,
                    replay_mode=ReplayMode.STRICT,
                )

                ref_a = ep.open_cache_slot(ref_ns, "A")
                target_a = ep.open_cache_slot(
                    target_ns,
                    "A",
                    replay_reference=ref_a,
                    replay_mode=ReplayMode.STRICT,
                )
                result = await target_a.run(handle_o, target_b)
                assert result == 30
                # Nothing re-executed — counts unchanged from reference run
                assert exec_counts == {"produce": 2, "inner": 1, "outer": 1}


# ---------------------------------------------------------------------------
# Edge case: deep subslot realize chain
# ---------------------------------------------------------------------------


async def test_deep_subslot_realize_chain(db_path: str):
    """Realize Split state through a deep subslot path (a/b/c)."""
    exec_count = 0

    async def produce(ctx: StepContext, x: int) -> int:
        nonlocal exec_count
        exec_count += 1
        return x * 10

    # Reference has root/s/a/s/b/s/c with data
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with TopLevelEntrypoint() as ep:
            key = StepKeyEntity(label="produce").store().entity_id
            step = ep.bind_external_step(produce, key)
            slot = ep.open_cache_slot(ref_ns, "root")
            await slot.subslot("a").subslot("b").subslot("c").run(step, 7)
            assert exec_count == 1

    # Replay — should realize all Split states and the leaf Finished
    async with SQLiteFxtrNamespace(db_path, "ref") as ref_ns:
        async with SQLiteFxtrNamespace(db_path, "target") as target_ns:
            async with TopLevelEntrypoint() as ep:
                key = StepKeyEntity(label="produce").store().entity_id
                step = ep.bind_external_step(produce, key)

                ref_slot = ep.open_cache_slot(ref_ns, "root")
                target_slot = ep.open_cache_slot(
                    target_ns,
                    "root",
                    replay_reference=ref_slot,
                    replay_mode=ReplayMode.STRICT,
                )
                result = await target_slot.subslot("a").subslot("b").subslot("c").run(step, 7)
                assert result == 70
                assert exec_count == 1  # Reused from reference
