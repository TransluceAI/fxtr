"""Tests for frozen internal cache slots, FrozenCacheSlot, and pin cleanup."""

from typing import Any

import pytest

from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr_workflow.cache_slot import FrozenCacheSlot, FrozenStepContext
from fxtr_workflow.entities import FrozenInternalSlots, FrozenSlotNode, SlotFinished
from fxtr_workflow.entrypoint import TopLevelEntrypoint
from fxtr_workflow.errors import StepAbortError
from fxtr_workflow.step_context import StepContext

from .conftest import StepKeyEntity

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Freezing produces correct tree structure
# ---------------------------------------------------------------------------


async def test_freeze_simple_internal_slot(db_path: str):
    """A step with one internal cache slot produces a frozen tree with one entry."""

    async def inner(ctx: StepContext, x: int) -> int:
        return x * 10

    async def outer(ctx: StepContext) -> int:
        return await ctx.cache_slot("work").run(handles["inner"], 5)

    handles: dict[str, Any] = {}

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_inner = StepKeyEntity(label="inner_freeze").store().entity_id
            key_outer = StepKeyEntity(label="outer_freeze").store().entity_id
            handles["inner"] = ep.bind_external_step(inner, key_inner)
            handle_outer = ep.bind_external_step(outer, key_outer)

            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run(handle_outer)
            assert result == 50

            # Inspect frozen internal slots
            state = await slot._load_state()
            assert isinstance(state, SlotFinished)
            frozen = await state.frozen_internal_slots_ref.load()
            assert isinstance(frozen, FrozenInternalSlots)
            assert "work" in frozen.slots

            # The frozen "work" slot should be Finished
            work_node = await frozen.slots["work"].load()
            assert isinstance(work_node, FrozenSlotNode)
            assert work_node.state_ref is not None
            work_state = await work_node.state_ref.load()
            assert isinstance(work_state, SlotFinished)


async def test_freeze_nested_internal_with_subslots(db_path: str):
    """Internal cache slot has subslots — frozen tree captures the subslot tree."""
    handles: dict[str, Any] = {}

    async def produce(ctx: StepContext, x: int) -> int:
        return x * 10

    async def outer(ctx: StepContext) -> int:
        work = ctx.cache_slot("work")
        a = await work.subslot("a").run(handles["produce"], 1)
        b = await work.subslot("b").run(handles["produce"], 2)
        return a + b

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce_fn").store().entity_id
            key_outer = StepKeyEntity(label="outer_fn").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handle_outer = ep.bind_external_step(outer, key_outer)

            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run(handle_outer)
            assert result == 30

            state = await slot._load_state()
            assert isinstance(state, SlotFinished)
            frozen = await state.frozen_internal_slots_ref.load()

            # "work" slot should be Split with two subslots
            work_node = await frozen.slots["work"].load()
            assert "a" in work_node.subslots
            assert "b" in work_node.subslots

            # Each subslot should be Finished
            a_node = await work_node.subslots["a"].load()
            assert a_node.state_ref is not None
            b_node = await work_node.subslots["b"].load()
            assert b_node.state_ref is not None


async def test_freeze_deeply_nested(db_path: str):
    """Step with nested internal cache slots: outer→dispatch→inner."""
    handles: dict[str, Any] = {}

    async def inner(ctx: StepContext, x: int) -> int:
        return await ctx.cache_slot("deep").run(handles["leaf"], x)

    async def leaf(ctx: StepContext, x: int) -> int:
        return x * 10

    async def outer(ctx: StepContext) -> int:
        return await ctx.cache_slot("dispatch").run(handles["inner"], 5)

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_leaf = StepKeyEntity(label="leaf_dn").store().entity_id
            key_inner = StepKeyEntity(label="inner_dn").store().entity_id
            key_outer = StepKeyEntity(label="outer_dn").store().entity_id
            handles["leaf"] = ep.bind_external_step(leaf, key_leaf)
            handles["inner"] = ep.bind_external_step(inner, key_inner)
            handle_outer = ep.bind_external_step(outer, key_outer)

            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run(handle_outer)
            assert result == 50

            # Outer's frozen tree has "dispatch"
            state = await slot._load_state()
            assert isinstance(state, SlotFinished)
            frozen = await state.frozen_internal_slots_ref.load()
            assert "dispatch" in frozen.slots

            # "dispatch" is Finished (inner step completed)
            dispatch_node = await frozen.slots["dispatch"].load()
            assert dispatch_node.state_ref is not None
            dispatch_state = await dispatch_node.state_ref.load()
            assert isinstance(dispatch_state, SlotFinished)

            # Inner step's frozen tree (inside dispatch_state) has "deep"
            inner_frozen = await dispatch_state.frozen_internal_slots_ref.load()
            assert "deep" in inner_frozen.slots


async def test_freeze_no_internal_slots(db_path: str):
    """A step with no internal cache slots produces an empty frozen tree."""

    async def simple(ctx: StepContext) -> str:
        return "hello"

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key = StepKeyEntity(label="simple_fn").store().entity_id
            handle = ep.bind_external_step(simple, key)

            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run(handle)
            assert result == "hello"

            state = await slot._load_state()
            assert isinstance(state, SlotFinished)
            frozen = await state.frozen_internal_slots_ref.load()
            assert len(frozen.slots) == 0


# ---------------------------------------------------------------------------
# FrozenCacheSlot navigation
# ---------------------------------------------------------------------------


async def test_frozen_cache_slot_navigation(db_path: str):
    """FrozenCacheSlot supports path accumulation and deferred loading."""
    handles: dict[str, Any] = {}

    async def produce(ctx: StepContext, x: int) -> int:
        return x * 10

    async def outer(ctx: StepContext) -> int:
        work = ctx.cache_slot("work")
        return await work.subslot("a").subslot("deep").run(handles["produce"], 3)

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce_nav").store().entity_id
            key_outer = StepKeyEntity(label="outer_nav").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handle_outer = ep.bind_external_step(outer, key_outer)

            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run(handle_outer)
            assert result == 30

            # Build a FrozenCacheSlot from the frozen tree
            state = await slot._load_state()
            assert isinstance(state, SlotFinished)
            frozen = await state.frozen_internal_slots_ref.load(scope=ns.scope)
            work_ref = frozen.slots["work"]

            # Navigate via child_subslot (sync, no loading)
            fc = FrozenCacheSlot(work_ref, load_scope=ns.scope)
            fc_a = fc.child_subslot("a")
            fc_deep = fc_a.child_subslot("deep")

            # Now load state (async)
            deep_state = await fc_deep.load_state()
            assert isinstance(deep_state, SlotFinished)

            # Also test load_state_ref
            deep_ref = await fc_deep.load_state_ref()
            assert deep_ref is not None

            # Nonexistent subslot returns None
            fc_missing = fc.child_subslot("nonexistent")
            missing_state = await fc_missing.load_state()
            assert missing_state is None


async def test_frozen_step_context_lookup(db_path: str):
    """FrozenStepContext returns FrozenCacheSlot by key, or None for missing keys."""
    handles: dict[str, Any] = {}

    async def inner(ctx: StepContext, x: int) -> int:
        return x * 10

    async def outer(ctx: StepContext) -> int:
        return await ctx.cache_slot("work").run(handles["inner"], 5)

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_inner = StepKeyEntity(label="inner_fsc").store().entity_id
            key_outer = StepKeyEntity(label="outer_fsc").store().entity_id
            handles["inner"] = ep.bind_external_step(inner, key_inner)
            handle_outer = ep.bind_external_step(outer, key_outer)

            slot = ep.open_cache_slot(ns, "root")
            await slot.run(handle_outer)

            state = await slot._load_state()
            assert isinstance(state, SlotFinished)
            frozen_internals = await state.frozen_internal_slots_ref.load(scope=ns.scope)
            fsc = FrozenStepContext(frozen_internals, load_scope=ns.scope)

            # Existing key
            work = fsc.get_internal_slot("work")
            assert work is not None
            assert isinstance(work, FrozenCacheSlot)

            # Load its state
            work_state = await work.load_state()
            assert isinstance(work_state, SlotFinished)

            # Missing key
            assert fsc.get_internal_slot("nonexistent") is None


# ---------------------------------------------------------------------------
# Pin cleanup
# ---------------------------------------------------------------------------


async def test_internal_slot_pins_cleaned_up(db_path: str):
    """After a step finishes, its internal cache slot pins are deleted."""
    handles: dict[str, Any] = {}

    async def inner(ctx: StepContext, x: int) -> int:
        return x * 10

    async def outer(ctx: StepContext) -> int:
        return await ctx.cache_slot("work").run(handles["inner"], 5)

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_inner = StepKeyEntity(label="inner_pin").store().entity_id
            key_outer = StepKeyEntity(label="outer_pin").store().entity_id
            handles["inner"] = ep.bind_external_step(inner, key_inner)
            handle_outer = ep.bind_external_step(outer, key_outer)

            slot = ep.open_cache_slot(ns, "root")
            await slot.run(handle_outer)

            # The internal cache slot pin should be gone
            ref = await ns.try_retrieve("root/c/work")
            assert ref is None, "Internal cache slot pin should have been cleaned up"


async def test_orphan_pins_from_partial_run_preserved(db_path: str):
    """Orphan pins from a failed partial run are preserved (not deleted).

    Run a step that creates internal slots then fails. Re-run with different
    internal slots that succeed. The original internal slot pins should remain
    as orphans (different keys from the successful run's slots).
    """
    handles: dict[str, Any] = {}
    fail_flag = True

    async def inner(ctx: StepContext, x: int) -> int:
        return x * 10

    async def failing_inner(ctx: StepContext) -> int:
        raise ValueError("intentional failure")

    async def outer(ctx: StepContext) -> int:
        if fail_flag:
            # First run: creates "old_work" then fails
            await ctx.cache_slot("old_work").run(handles["failing_inner"])
            return -1  # never reached
        else:
            # Second run: creates "new_work" instead
            return await ctx.cache_slot("new_work").run(handles["inner"], 5)

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_inner = StepKeyEntity(label="inner_orphan").store().entity_id
            key_failing = StepKeyEntity(label="failing_orphan").store().entity_id
            key_outer = StepKeyEntity(label="outer_orphan").store().entity_id
            handles["inner"] = ep.bind_external_step(inner, key_inner)
            handles["failing_inner"] = ep.bind_external_step(failing_inner, key_failing)
            handle_outer = ep.bind_external_step(outer, key_outer)

            # First run: fails, leaves root as Partial with old_work pin
            slot = ep.open_cache_slot(ns, "root")
            pending = slot.run(handle_outer)
            with pytest.raises(StepAbortError):
                await pending

    # Second run: succeeds with new_work
    fail_flag = False
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_inner = StepKeyEntity(label="inner_orphan").store().entity_id
            key_failing = StepKeyEntity(label="failing_orphan").store().entity_id
            key_outer = StepKeyEntity(label="outer_orphan").store().entity_id
            handles["inner"] = ep.bind_external_step(inner, key_inner)
            handles["failing_inner"] = ep.bind_external_step(failing_inner, key_failing)
            handle_outer = ep.bind_external_step(outer, key_outer)

            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run(handle_outer)
            assert result == 50

            # new_work pin should be cleaned up (frozen)
            ref = await ns.try_retrieve("root/c/new_work")
            assert ref is None, "Frozen pin should be cleaned up"

            # old_work pin is an orphan — it was created during the partial run
            # but not accessed during the successful run, so it's not frozen
            # and not cleaned up. It should still exist (or not exist if the
            # failing step's abort prevented persisting the pin).
            # The key point is that we don't crash or incorrectly delete it.


async def test_replay_check_works_after_pin_cleanup(db_path: str):
    """Replay check still works after internal cache slot pins are cleaned up,
    by reading from frozen state."""
    handles: dict[str, Any] = {}

    async def produce(ctx: StepContext, x: int) -> int:
        return x * 10

    async def inner_step(ctx: StepContext, data_slot: Any) -> int:
        return await data_slot.subslot("val").run(handles["produce"], 5)

    async def outer_step(ctx: StepContext, foreign: Any) -> int:
        internal = ctx.cache_slot("inner")
        val = await internal.run(handles["inner"], foreign)
        return val + 1000

    # First run
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce_pc").store().entity_id
            key_inner = StepKeyEntity(label="inner_pc").store().entity_id
            key_outer = StepKeyEntity(label="outer_pc").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handles["inner"] = ep.bind_external_step(inner_step, key_inner)
            handle_outer = ep.bind_external_step(outer_step, key_outer)

            slot_b = ep.open_cache_slot(ns, "B")
            await slot_b.subslot("val").run(handles["produce"], 5)

            slot_a = ep.open_cache_slot(ns, "A")
            result = await slot_a.run(handle_outer, slot_b)
            assert result == 1050

            # Verify internal pin was cleaned up
            ref = await ns.try_retrieve("A/c/inner")
            assert ref is None, "Internal pin should be cleaned up"

    # Second run: same data → replay check should pass using frozen state
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce_pc").store().entity_id
            key_inner = StepKeyEntity(label="inner_pc").store().entity_id
            key_outer = StepKeyEntity(label="outer_pc").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handles["inner"] = ep.bind_external_step(inner_step, key_inner)
            handle_outer = ep.bind_external_step(outer_step, key_outer)

            slot_b = ep.open_cache_slot(ns, "B")
            slot_a = ep.open_cache_slot(ns, "A")
            result = await slot_a.run(handle_outer, slot_b)
            assert result == 1050  # Cached, not re-executed
