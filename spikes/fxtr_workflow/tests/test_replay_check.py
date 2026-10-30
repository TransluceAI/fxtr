"""Tests for the cache-slot-arg replay check.

These tests exercise the TracingCacheSlot, ForeignRunTracker, and replay_check()
machinery by passing cache slots as arguments to steps.
"""

from typing import Any

import pytest

from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr_workflow.entrypoint import TopLevelEntrypoint
from fxtr_workflow.errors import ReplayCheckError
from fxtr_workflow.step_context import StepContext

from .conftest import StepKeyEntity

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Basic replay check: same slot args → cache hit
# ---------------------------------------------------------------------------


async def test_replay_check_same_slot_args_cached(db_path: str):
    """Step A takes slot B as arg, dispatches into B's subslot.
    Re-open B pointed at the same data → A's result is reused."""
    produce_count = 0
    consume_count = 0
    handles: dict[str, Any] = {}

    async def produce(ctx: StepContext, x: int) -> int:
        nonlocal produce_count
        produce_count += 1
        return x * 10

    async def consume(ctx: StepContext, data_slot: Any) -> int:
        nonlocal consume_count
        consume_count += 1
        val = await data_slot.subslot("val").run(handles["produce"], 5)
        return val + 1

    # First run: fill B, run A(B)
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce").store().entity_id
            key_consume = StepKeyEntity(label="consume").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handle_consume = ep.bind_external_step(consume, key_consume)

            slot_b = ep.open_cache_slot(ns, "B")
            await slot_b.subslot("val").run(handles["produce"], 5)

            slot_a = ep.open_cache_slot(ns, "A")
            result = await slot_a.run(handle_consume, slot_b)
            assert result == 51
            assert consume_count == 1
            assert produce_count == 1

    # Second run: re-open same data → should reuse A's cached result
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce").store().entity_id
            key_consume = StepKeyEntity(label="consume").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handle_consume = ep.bind_external_step(consume, key_consume)

            slot_b = ep.open_cache_slot(ns, "B")
            slot_a = ep.open_cache_slot(ns, "A")
            result = await slot_a.run(handle_consume, slot_b)
            assert result == 51
            assert consume_count == 1  # NOT called again
            assert produce_count == 1  # NOT called again


# ---------------------------------------------------------------------------
# Replay check invalidation: different slot data → abort
# ---------------------------------------------------------------------------


async def test_replay_check_fails_different_slot_data(db_path: str):
    """Step A takes a data slot as arg. Swapping to slot C (same shape,
    different data) should fail the replay check even though the cache
    key matches (cache slots are placeholders in the key)."""
    handles: dict[str, Any] = {}

    async def produce(ctx: StepContext, x: int) -> int:
        return x * 10

    async def consume(ctx: StepContext, data_slot: Any) -> int:
        val = await data_slot.subslot("val").run(handles["produce"], 5)
        return val + 1

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce").store().entity_id
            key_consume = StepKeyEntity(label="consume").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handle_consume = ep.bind_external_step(consume, key_consume)

            # Fill slot B with produce(5) → 50
            slot_b = ep.open_cache_slot(ns, "B")
            await slot_b.subslot("val").run(handles["produce"], 5)

            # Run A(B) → caches result 51
            slot_a = ep.open_cache_slot(ns, "A")
            result = await slot_a.run(handle_consume, slot_b)
            assert result == 51

            # Now create slot C with different data: produce(9) → 90
            slot_c = ep.open_cache_slot(ns, "C")
            await slot_c.subslot("val").run(handles["produce"], 9)

            # Try A(C) — same cache key (slot is placeholder), but
            # replay check should detect that C has different contents from B
            pending = slot_a.run(handle_consume, slot_c)
            with pytest.raises(ReplayCheckError):
                await pending


async def test_replay_check_fails_empty_slot(db_path: str):
    """Passing an empty slot (no data) where a filled slot was expected
    should fail the replay check."""
    handles: dict[str, Any] = {}

    async def produce(ctx: StepContext, x: int) -> int:
        return x * 10

    async def consume(ctx: StepContext, data_slot: Any) -> int:
        val = await data_slot.subslot("val").run(handles["produce"], 5)
        return val + 1

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce").store().entity_id
            key_consume = StepKeyEntity(label="consume").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handle_consume = ep.bind_external_step(consume, key_consume)

            # Fill slot B, run A(B)
            slot_b = ep.open_cache_slot(ns, "B")
            await slot_b.subslot("val").run(handles["produce"], 5)

            slot_a = ep.open_cache_slot(ns, "A")
            result = await slot_a.run(handle_consume, slot_b)
            assert result == 51

            # Slot C is empty — no data at all
            slot_c = ep.open_cache_slot(ns, "C")

            # A(C) should fail replay check: C/s/val doesn't exist
            pending = slot_a.run(handle_consume, slot_c)
            with pytest.raises(ReplayCheckError):
                await pending


# ---------------------------------------------------------------------------
# Trivial case: slot arg received but never accessed
# ---------------------------------------------------------------------------


async def test_replay_check_no_slot_deps_trivially_passes(db_path: str):
    """A step that receives a cache slot but never accesses it passes trivially."""

    async def ignores_slot(ctx: StepContext, data_slot: Any) -> str:
        return "ignored"

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="ignores").store().entity_id
            step = ep.bind_external_step(ignores_slot, key_id)

            slot_b = ep.open_cache_slot(ns, "B")
            slot_a = ep.open_cache_slot(ns, "A")
            result = await slot_a.run(step, slot_b)
            assert result == "ignored"

    # Re-run with same (empty) slot — cached
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="ignores").store().entity_id
            step = ep.bind_external_step(ignores_slot, key_id)

            slot_b = ep.open_cache_slot(ns, "B")
            slot_a = ep.open_cache_slot(ns, "A")
            result = await slot_a.run(step, slot_b)
            assert result == "ignored"


# ---------------------------------------------------------------------------
# Case (b): foreign slot passed as ARG to an internal run
# ---------------------------------------------------------------------------


async def test_case_b_foreign_arg_to_internal_run(db_path: str):
    """Step passes a foreign slot as an arg to ctx.cache_slot().run().
    Swapping to a different foreign slot should fail the replay check."""
    handles: dict[str, Any] = {}

    async def reader(ctx: StepContext, data_slot: Any) -> int:
        """Reads from data_slot by dispatching into it."""
        return await data_slot.subslot("val").run(handles["produce"], 5)

    async def orchestrator(ctx: StepContext, foreign: Any) -> int:
        """Passes foreign slot as an arg to an internal run (case b)."""
        internal = ctx.cache_slot("inner")
        val = await internal.run(handles["reader"], foreign)
        return val + 1000

    async def produce(ctx: StepContext, x: int) -> int:
        return x * 10

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce").store().entity_id
            key_reader = StepKeyEntity(label="reader").store().entity_id
            key_orch = StepKeyEntity(label="orchestrator").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handles["reader"] = ep.bind_external_step(reader, key_reader)
            handle_orch = ep.bind_external_step(orchestrator, key_orch)

            # Fill slot B with produce(5) → 50
            slot_b = ep.open_cache_slot(ns, "B")
            await slot_b.subslot("val").run(handles["produce"], 5)

            # Run orchestrator(B) → reader reads 50, orchestrator returns 1050
            slot_a = ep.open_cache_slot(ns, "A")
            result = await slot_a.run(handle_orch, slot_b)
            assert result == 1050

            # Now create slot C with different data: produce(9) → 90
            slot_c = ep.open_cache_slot(ns, "C")
            await slot_c.subslot("val").run(handles["produce"], 9)

            # orchestrator(C) should fail: foreign slot passed as arg to internal
            # run has different contents, but cache key matches (placeholder)
            pending = slot_a.run(handle_orch, slot_c)
            with pytest.raises(ReplayCheckError):
                await pending


async def test_case_ab_combined_foreign_target_and_arg(db_path: str):
    """A run is dispatched TO a foreign slot AND receives another foreign slot as arg.
    Both should be tracked; changing either should fail the replay check."""
    handles: dict[str, Any] = {}

    async def writer(ctx: StepContext, other_slot: Any) -> int:
        """Dispatched into a foreign slot, also receives another foreign slot as arg."""
        other_val = await other_slot.subslot("val").run(handles["produce"], 7)
        return other_val + 1

    async def orchestrator(ctx: StepContext, slot_x: Any, slot_y: Any) -> int:
        """Dispatches writer TO slot_x, passing slot_y as an arg."""
        return await slot_x.subslot("result").run(handles["writer"], slot_y)

    async def produce(ctx: StepContext, x: int) -> int:
        return x * 10

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce").store().entity_id
            key_writer = StepKeyEntity(label="writer").store().entity_id
            key_orch = StepKeyEntity(label="orchestrator_ab").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handles["writer"] = ep.bind_external_step(writer, key_writer)
            handle_orch = ep.bind_external_step(orchestrator, key_orch)

            # Fill slot Y with produce(7) → 70
            slot_y = ep.open_cache_slot(ns, "Y")
            await slot_y.subslot("val").run(handles["produce"], 7)

            # Slots for dispatch target (X) and orchestrator (A)
            slot_x = ep.open_cache_slot(ns, "X")
            slot_a = ep.open_cache_slot(ns, "A")

            # Run orchestrator(X, Y) → writer dispatched TO X, receives Y as arg
            result = await slot_a.run(handle_orch, slot_x, slot_y)
            assert result == 71  # (7 * 10) + 1

            # Create Y2 with different data
            slot_y2 = ep.open_cache_slot(ns, "Y2")
            await slot_y2.subslot("val").run(handles["produce"], 3)

            # orchestrator(X, Y2) should fail: Y2 has different data than Y
            pending = slot_a.run(handle_orch, slot_x, slot_y2)
            with pytest.raises(ReplayCheckError):
                await pending


async def test_case_b_multiple_foreign_args(db_path: str):
    """Step receives two foreign slots, passes both to an internal run."""
    handles: dict[str, Any] = {}

    async def combiner(ctx: StepContext, slot_a: Any, slot_b: Any) -> int:
        """Reads from two foreign slots."""
        a = await slot_a.subslot("val").run(handles["produce"], 5)
        b = await slot_b.subslot("val").run(handles["produce"], 7)
        return a + b

    async def orchestrator(ctx: StepContext, foreign_a: Any, foreign_b: Any) -> int:
        """Passes both foreign slots to an internal run."""
        internal = ctx.cache_slot("combine")
        return await internal.run(handles["combiner"], foreign_a, foreign_b)

    async def produce(ctx: StepContext, x: int) -> int:
        return x * 10

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce").store().entity_id
            key_combiner = StepKeyEntity(label="combiner").store().entity_id
            key_orch = StepKeyEntity(label="orchestrator_multi").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handles["combiner"] = ep.bind_external_step(combiner, key_combiner)
            handle_orch = ep.bind_external_step(orchestrator, key_orch)

            # Fill slots B1 and B2
            slot_b1 = ep.open_cache_slot(ns, "B1")
            await slot_b1.subslot("val").run(handles["produce"], 5)
            slot_b2 = ep.open_cache_slot(ns, "B2")
            await slot_b2.subslot("val").run(handles["produce"], 7)

            slot_a = ep.open_cache_slot(ns, "A")
            result = await slot_a.run(handle_orch, slot_b1, slot_b2)
            assert result == 120  # 50 + 70

            # Create B2_alt with different data
            slot_b2_alt = ep.open_cache_slot(ns, "B2_alt")
            await slot_b2_alt.subslot("val").run(handles["produce"], 99)

            # Swap only second arg → should fail
            pending = slot_a.run(handle_orch, slot_b1, slot_b2_alt)
            with pytest.raises(ReplayCheckError):
                await pending


async def test_case_b_nested_forwarding(db_path: str):
    """Step A passes foreign slot to step B (internal), step B dispatches into it."""
    handles: dict[str, Any] = {}

    async def inner_step(ctx: StepContext, data_slot: Any) -> int:
        """Dispatches into the foreign slot it received."""
        return await data_slot.subslot("val").run(handles["produce"], 5)

    async def outer_step(ctx: StepContext, foreign: Any) -> int:
        """Passes foreign to an internal step, which dispatches into it."""
        internal = ctx.cache_slot("inner")
        val = await internal.run(handles["inner"], foreign)
        return val + 1

    async def produce(ctx: StepContext, x: int) -> int:
        return x * 10

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce").store().entity_id
            key_inner = StepKeyEntity(label="inner_nest").store().entity_id
            key_outer = StepKeyEntity(label="outer_nest").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handles["inner"] = ep.bind_external_step(inner_step, key_inner)
            handle_outer = ep.bind_external_step(outer_step, key_outer)

            # Fill slot B
            slot_b = ep.open_cache_slot(ns, "B")
            await slot_b.subslot("val").run(handles["produce"], 5)

            # Run outer(B) → inner reads from B → 50, outer returns 51
            slot_a = ep.open_cache_slot(ns, "A")
            result = await slot_a.run(handle_outer, slot_b)
            assert result == 51

            # Create slot C with different data
            slot_c = ep.open_cache_slot(ns, "C")
            await slot_c.subslot("val").run(handles["produce"], 9)

            # outer(C) should fail replay check
            pending = slot_a.run(handle_outer, slot_c)
            with pytest.raises(ReplayCheckError):
                await pending


async def test_inner_step_receives_foreign_and_internal_args(db_path: str):
    """Inner step receives both an outer-foreign and an outer-internal cache-slot arg.

    The inner step's arg indices cover both, so the recursive replay check must
    have complete inner_args (not just foreign-origin entries).
    """
    handles: dict[str, Any] = {}

    async def produce(ctx: StepContext, x: int) -> int:
        return x * 10

    async def inner_step(ctx: StepContext, internal_slot: Any, foreign_slot: Any) -> int:
        """Dispatches into both the internal and foreign slots it received."""
        a = await internal_slot.subslot("a").run(handles["produce"], 1)
        b = await foreign_slot.subslot("val").run(handles["produce"], 2)
        return a + b

    async def outer_step(ctx: StepContext, foreign: Any) -> int:
        """Passes both an internal cache slot and the foreign arg to the inner step."""
        work = ctx.cache_slot("work")
        return await ctx.cache_slot("dispatch").run(handles["inner"], work, foreign)

    # First run: populate everything
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce_fi").store().entity_id
            key_inner = StepKeyEntity(label="inner_fi").store().entity_id
            key_outer = StepKeyEntity(label="outer_fi").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handles["inner"] = ep.bind_external_step(inner_step, key_inner)
            handle_outer = ep.bind_external_step(outer_step, key_outer)

            slot_b = ep.open_cache_slot(ns, "B")
            await slot_b.subslot("val").run(handles["produce"], 2)

            slot_a = ep.open_cache_slot(ns, "A")
            result = await slot_a.run(handle_outer, slot_b)
            assert result == 30  # 1*10 + 2*10

    # Second run: same data → cache hit (exercises recursive replay check with mixed args)
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce_fi").store().entity_id
            key_inner = StepKeyEntity(label="inner_fi").store().entity_id
            key_outer = StepKeyEntity(label="outer_fi").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handles["inner"] = ep.bind_external_step(inner_step, key_inner)
            handle_outer = ep.bind_external_step(outer_step, key_outer)

            slot_b = ep.open_cache_slot(ns, "B")
            slot_a = ep.open_cache_slot(ns, "A")
            result = await slot_a.run(handle_outer, slot_b)
            assert result == 30

    # Third run: different foreign data → replay check fails
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce_fi").store().entity_id
            key_inner = StepKeyEntity(label="inner_fi").store().entity_id
            key_outer = StepKeyEntity(label="outer_fi").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handles["inner"] = ep.bind_external_step(inner_step, key_inner)
            handle_outer = ep.bind_external_step(outer_step, key_outer)

            slot_c = ep.open_cache_slot(ns, "C")
            await slot_c.subslot("val").run(handles["produce"], 9)

            slot_a = ep.open_cache_slot(ns, "A")
            pending = slot_a.run(handle_outer, slot_c)
            with pytest.raises(ReplayCheckError):
                await pending


async def test_dict_args_with_multiple_cache_slots(db_path: str):
    """Cache slots in a dict arg are indexed by sorted-path order, not insertion order."""
    handles: dict[str, Any] = {}

    async def produce(ctx: StepContext, x: int) -> int:
        return x * 10

    async def inner_step(ctx: StepContext, slots: Any) -> int:
        """Dispatches into both slots from a dict arg."""
        a = await slots["z_slot"].subslot("v").run(handles["produce"], 1)
        b = await slots["a_slot"].subslot("v").run(handles["produce"], 2)
        return a + b

    async def outer_step(ctx: StepContext, foreign: Any) -> int:
        """Passes a dict with the foreign slot under two different keys."""
        # Insertion order differs from sorted key order
        d = {"z_slot": ctx.cache_slot("z"), "a_slot": foreign}
        return await ctx.cache_slot("dispatch").run(handles["inner"], d)

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce_dict").store().entity_id
            key_inner = StepKeyEntity(label="inner_dict").store().entity_id
            key_outer = StepKeyEntity(label="outer_dict").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handles["inner"] = ep.bind_external_step(inner_step, key_inner)
            handle_outer = ep.bind_external_step(outer_step, key_outer)

            slot_b = ep.open_cache_slot(ns, "B")
            await slot_b.subslot("v").run(handles["produce"], 2)

            slot_a = ep.open_cache_slot(ns, "A")
            result = await slot_a.run(handle_outer, slot_b)
            assert result == 30  # 1*10 + 2*10

    # Replay with same data — should hit cache
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce_dict").store().entity_id
            key_inner = StepKeyEntity(label="inner_dict").store().entity_id
            key_outer = StepKeyEntity(label="outer_dict").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handles["inner"] = ep.bind_external_step(inner_step, key_inner)
            handle_outer = ep.bind_external_step(outer_step, key_outer)

            slot_b = ep.open_cache_slot(ns, "B")
            slot_a = ep.open_cache_slot(ns, "A")
            result = await slot_a.run(handle_outer, slot_b)
            assert result == 30

    # Replay with different foreign data — should fail
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce_dict").store().entity_id
            key_inner = StepKeyEntity(label="inner_dict").store().entity_id
            key_outer = StepKeyEntity(label="outer_dict").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handles["inner"] = ep.bind_external_step(inner_step, key_inner)
            handle_outer = ep.bind_external_step(outer_step, key_outer)

            slot_c = ep.open_cache_slot(ns, "C")
            await slot_c.subslot("v").run(handles["produce"], 9)

            slot_a = ep.open_cache_slot(ns, "A")
            pending = slot_a.run(handle_outer, slot_c)
            with pytest.raises(ReplayCheckError):
                await pending


async def test_duplicate_slot_args(db_path: str):
    """Same cache slot passed at multiple arg positions gets distinct arg indices."""
    handles: dict[str, Any] = {}

    async def produce(ctx: StepContext, x: int) -> int:
        return x * 10

    async def inner_step(ctx: StepContext, slot_a: Any, slot_b: Any) -> int:
        """Dispatches into both slots — which are actually the same object."""
        a = await slot_a.subslot("x").run(handles["produce"], 1)
        b = await slot_b.subslot("y").run(handles["produce"], 2)
        return a + b

    async def outer_step(ctx: StepContext, foreign: Any) -> int:
        """Passes the same foreign slot as both args to inner_step."""
        return await ctx.cache_slot("run").run(handles["inner"], foreign, foreign)

    # First run
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce_dup").store().entity_id
            key_inner = StepKeyEntity(label="inner_dup").store().entity_id
            key_outer = StepKeyEntity(label="outer_dup").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handles["inner"] = ep.bind_external_step(inner_step, key_inner)
            handle_outer = ep.bind_external_step(outer_step, key_outer)

            slot_b = ep.open_cache_slot(ns, "B")
            slot_a = ep.open_cache_slot(ns, "A")
            result = await slot_a.run(handle_outer, slot_b)
            assert result == 30  # 1*10 + 2*10

    # Replay with same data — should hit cache
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce_dup").store().entity_id
            key_inner = StepKeyEntity(label="inner_dup").store().entity_id
            key_outer = StepKeyEntity(label="outer_dup").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handles["inner"] = ep.bind_external_step(inner_step, key_inner)
            handle_outer = ep.bind_external_step(outer_step, key_outer)

            slot_b = ep.open_cache_slot(ns, "B")
            slot_a = ep.open_cache_slot(ns, "A")
            result = await slot_a.run(handle_outer, slot_b)
            assert result == 30


# ---------------------------------------------------------------------------
# Multiple dispatches into different subslots of same foreign arg (#13)
# ---------------------------------------------------------------------------


async def test_multiple_dispatches_to_foreign_subslots(db_path: str):
    """Step dispatches into foreign.subslot("x") and foreign.subslot("y").
    Changing data in just one subslot should fail the replay check."""
    handles: dict[str, Any] = {}

    async def produce(ctx: StepContext, x: int) -> int:
        return x * 10

    async def multi_dispatch(ctx: StepContext, foreign: Any) -> int:
        a = await foreign.subslot("x").run(handles["produce"], 1)
        b = await foreign.subslot("y").run(handles["produce"], 2)
        return a + b

    # First run: fill foreign subslots and cache the step
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce_ms").store().entity_id
            key_multi = StepKeyEntity(label="multi_dispatch").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handle_multi = ep.bind_external_step(multi_dispatch, key_multi)

            slot_f = ep.open_cache_slot(ns, "F")
            await slot_f.subslot("x").run(handles["produce"], 1)
            await slot_f.subslot("y").run(handles["produce"], 2)

            slot_a = ep.open_cache_slot(ns, "A")
            result = await slot_a.run(handle_multi, slot_f)
            assert result == 30  # 10 + 20

    # Replay with same data — should pass
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce_ms").store().entity_id
            key_multi = StepKeyEntity(label="multi_dispatch").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handle_multi = ep.bind_external_step(multi_dispatch, key_multi)

            slot_f = ep.open_cache_slot(ns, "F")
            slot_a = ep.open_cache_slot(ns, "A")
            result = await slot_a.run(handle_multi, slot_f)
            assert result == 30

    # Now create a slot with different data in subslot "y" only
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce_ms").store().entity_id
            key_multi = StepKeyEntity(label="multi_dispatch").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handle_multi = ep.bind_external_step(multi_dispatch, key_multi)

            # New foreign: same "x" data, different "y" data
            slot_g = ep.open_cache_slot(ns, "G")
            await slot_g.subslot("x").run(handles["produce"], 1)
            await slot_g.subslot("y").run(handles["produce"], 99)

            slot_a = ep.open_cache_slot(ns, "A")
            pending = slot_a.run(handle_multi, slot_g)
            with pytest.raises(ReplayCheckError):
                await pending


# ---------------------------------------------------------------------------
# Foreign subslot passed as arg (#14)
# ---------------------------------------------------------------------------


async def test_foreign_subslot_passed_as_arg(db_path: str):
    """Top level pre-fills slot_b.subslot("data"). Step receives slot_b and passes
    slot_b.subslot("data") to an inner step. Change in subslot detected."""
    handles: dict[str, Any] = {}

    async def produce(ctx: StepContext, x: int) -> int:
        return x * 10

    async def inner_reader(ctx: StepContext, data_slot: Any) -> int:
        return await data_slot.subslot("v").run(handles["produce"], 5)

    async def outer(ctx: StepContext, foreign: Any) -> int:
        sub = foreign.subslot("data")
        return await ctx.cache_slot("run").run(handles["inner_reader"], sub)

    # First run
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce_sub").store().entity_id
            key_inner = StepKeyEntity(label="inner_reader_sub").store().entity_id
            key_outer = StepKeyEntity(label="outer_sub").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handles["inner_reader"] = ep.bind_external_step(inner_reader, key_inner)
            handle_outer = ep.bind_external_step(outer, key_outer)

            slot_b = ep.open_cache_slot(ns, "B")
            await slot_b.subslot("data").subslot("v").run(handles["produce"], 5)

            slot_a = ep.open_cache_slot(ns, "A")
            result = await slot_a.run(handle_outer, slot_b)
            assert result == 50

    # Same data → cache hit
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce_sub").store().entity_id
            key_inner = StepKeyEntity(label="inner_reader_sub").store().entity_id
            key_outer = StepKeyEntity(label="outer_sub").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handles["inner_reader"] = ep.bind_external_step(inner_reader, key_inner)
            handle_outer = ep.bind_external_step(outer, key_outer)

            slot_b = ep.open_cache_slot(ns, "B")
            slot_a = ep.open_cache_slot(ns, "A")
            result = await slot_a.run(handle_outer, slot_b)
            assert result == 50

    # Different data in the subslot → should fail
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce_sub").store().entity_id
            key_inner = StepKeyEntity(label="inner_reader_sub").store().entity_id
            key_outer = StepKeyEntity(label="outer_sub").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handles["inner_reader"] = ep.bind_external_step(inner_reader, key_inner)
            handle_outer = ep.bind_external_step(outer, key_outer)

            slot_c = ep.open_cache_slot(ns, "C")
            await slot_c.subslot("data").subslot("v").run(handles["produce"], 99)

            slot_a = ep.open_cache_slot(ns, "A")
            pending = slot_a.run(handle_outer, slot_c)
            with pytest.raises(ReplayCheckError):
                await pending


# ---------------------------------------------------------------------------
# Purely internal dispatch — no tracking overhead (#15)
# ---------------------------------------------------------------------------


async def test_purely_internal_dispatch_no_tracking(db_path: str):
    """Step creates ctx.cache_slot("work") and dispatches with no foreign args.
    slot_deps should have zero tracked runs."""
    handles: dict[str, Any] = {}

    async def inner(ctx: StepContext, x: int) -> int:
        return x * 10

    async def outer(ctx: StepContext) -> int:
        work = ctx.cache_slot("work")
        return await work.run(handles["inner"], 5)

    from fxtr_workflow.entities import SlotFinished

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_inner = StepKeyEntity(label="inner_pure").store().entity_id
            key_outer = StepKeyEntity(label="outer_pure").store().entity_id
            handles["inner"] = ep.bind_external_step(inner, key_inner)
            handle_outer = ep.bind_external_step(outer, key_outer)

            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run(handle_outer)
            assert result == 50

            # Inspect slot_deps — should be empty
            state = await slot._load_state()
            assert isinstance(state, SlotFinished)
            deps = await state.slot_deps_ref.load()
            assert len(deps.runs) == 0


# ---------------------------------------------------------------------------
# Replay with extra subslots added (#16)
# ---------------------------------------------------------------------------


async def test_replay_with_extra_subslots_added(db_path: str):
    """Step dispatches into foreign.subslot("x"). Between runs, add data into
    foreign.subslot("y"). Replay should still pass — "x" hasn't changed."""
    handles: dict[str, Any] = {}

    async def produce(ctx: StepContext, x: int) -> int:
        return x * 10

    async def reader(ctx: StepContext, foreign: Any) -> int:
        return await foreign.subslot("x").run(handles["produce"], 3)

    # First run
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce_extra").store().entity_id
            key_reader = StepKeyEntity(label="reader_extra").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handle_reader = ep.bind_external_step(reader, key_reader)

            slot_f = ep.open_cache_slot(ns, "F")
            await slot_f.subslot("x").run(handles["produce"], 3)

            slot_a = ep.open_cache_slot(ns, "A")
            result = await slot_a.run(handle_reader, slot_f)
            assert result == 30

    # Add extra subslot to foreign between runs
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce_extra").store().entity_id
            key_reader = StepKeyEntity(label="reader_extra").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handle_reader = ep.bind_external_step(reader, key_reader)

            slot_f = ep.open_cache_slot(ns, "F")
            # Add new data that the step doesn't care about
            await slot_f.subslot("y").run(handles["produce"], 99)

            slot_a = ep.open_cache_slot(ns, "A")
            # Replay should pass — "x" is unchanged
            result = await slot_a.run(handle_reader, slot_f)
            assert result == 30


# ---------------------------------------------------------------------------
# Foreign slot forwarded through multiple levels without use (#25)
# ---------------------------------------------------------------------------


async def test_foreign_slot_forwarded_without_use(db_path: str):
    """Foreign slot passed through 3 levels of nesting but never dispatched into.
    Replay should pass even if the forwarded slot's data changes."""
    handles: dict[str, Any] = {}

    async def leaf(ctx: StepContext, unused_slot: Any) -> str:
        # Receives the slot but doesn't dispatch into it
        return "leaf_result"

    async def mid(ctx: StepContext, unused_slot: Any) -> str:
        return await ctx.cache_slot("leaf").run(handles["leaf"], unused_slot)

    async def top(ctx: StepContext, foreign: Any) -> str:
        return await ctx.cache_slot("mid").run(handles["mid"], foreign)

    # First run with empty foreign
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_leaf = StepKeyEntity(label="leaf_unused").store().entity_id
            key_mid = StepKeyEntity(label="mid_unused").store().entity_id
            key_top = StepKeyEntity(label="top_unused").store().entity_id
            handles["leaf"] = ep.bind_external_step(leaf, key_leaf)
            handles["mid"] = ep.bind_external_step(mid, key_mid)
            handle_top = ep.bind_external_step(top, key_top)

            slot_f = ep.open_cache_slot(ns, "F")
            slot_a = ep.open_cache_slot(ns, "A")
            result = await slot_a.run(handle_top, slot_f)
            assert result == "leaf_result"

    # Replay with a DIFFERENT foreign slot that has data — should still pass
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_leaf = StepKeyEntity(label="leaf_unused").store().entity_id
            key_mid = StepKeyEntity(label="mid_unused").store().entity_id
            key_top = StepKeyEntity(label="top_unused").store().entity_id
            handles["leaf"] = ep.bind_external_step(leaf, key_leaf)
            handles["mid"] = ep.bind_external_step(mid, key_mid)
            handle_top = ep.bind_external_step(top, key_top)

            async def produce(ctx: StepContext, x: int) -> int:
                return x * 10

            key_produce = StepKeyEntity(label="produce_unused").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)

            # Fill foreign slot with data this time
            slot_g = ep.open_cache_slot(ns, "G")
            await slot_g.subslot("data").run(handles["produce"], 42)

            slot_a = ep.open_cache_slot(ns, "A")
            # Should still pass — the slot was never observed
            result = await slot_a.run(handle_top, slot_g)
            assert result == "leaf_result"
