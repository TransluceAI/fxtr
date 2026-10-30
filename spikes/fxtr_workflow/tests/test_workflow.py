"""Integration tests for the workflow system."""

from pathlib import Path

import pytest

from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr_workflow.entities import SlotFinished, SlotPartial, SlotSplit
from fxtr_workflow.entrypoint import TopLevelEntrypoint
from fxtr_workflow.errors import CacheSlotError, StepAbortError
from fxtr_workflow.step_context import StepContext

from .conftest import StepKeyEntity

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Basic workflow tests
# ---------------------------------------------------------------------------


async def test_simple_step_run(tmp_path: Path):
    """A single step runs and produces a result."""

    async def add_one(ctx: StepContext, x: int) -> int:
        return x + 1

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="add_one").store().entity_id
            step = ep.bind_external_step(add_one, key_id)
            slot = ep.open_cache_slot(ns, "root")
            pending = slot.run(step, 41)
            result = await pending
            assert result == 42


async def test_two_step_workflow(tmp_path: Path):
    """Step A produces a value, step B consumes it via Pending forwarding."""

    async def double(ctx: StepContext, x: int) -> int:
        return x * 2

    async def add_ten(ctx: StepContext, x: int) -> int:
        return x + 10

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_double = StepKeyEntity(label="double").store().entity_id
            key_add = StepKeyEntity(label="add_ten").store().entity_id
            step_double = ep.bind_external_step(double, key_double)
            step_add = ep.bind_external_step(add_ten, key_add)

            slot_a = ep.open_cache_slot(ns, "a")
            slot_b = ep.open_cache_slot(ns, "b")

            pending_a = slot_a.run(step_double, 5)
            # Forward pending_a as arg to step B — runtime resolves it
            pending_b = slot_b.run(step_add, pending_a)

            assert await pending_b == 20  # (5 * 2) + 10


async def test_cache_reuse_on_resume(tmp_path: Path):
    """Finished slots are reused on second execution."""
    call_count = 0

    async def counting_step(ctx: StepContext, x: int) -> int:
        nonlocal call_count
        call_count += 1
        return x + 1

    db_path = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

    # First run
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="counting").store().entity_id
            step = ep.bind_external_step(counting_step, key_id)
            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run(step, 10)
            assert result == 11
            assert call_count == 1

    # Second run — same slot, same args, should reuse cache
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="counting").store().entity_id
            step = ep.bind_external_step(counting_step, key_id)
            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run(step, 10)
            assert result == 11
            assert call_count == 1  # NOT called again


async def test_subslot_tree(tmp_path: Path):
    """Parent splits into subslots, each runs independently."""

    async def identity(ctx: StepContext, x: int) -> int:
        return x

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="identity").store().entity_id
            step = ep.bind_external_step(identity, key_id)

            root = ep.open_cache_slot(ns, "root")
            r1 = root.subslot("a").run(step, 1)
            r2 = root.subslot("b").run(step, 2)
            r3 = root.subslot("c").run(step, 3)

            assert await r1 == 1
            assert await r2 == 2
            assert await r3 == 3


async def test_write_once_violation_different_key(tmp_path: Path):
    """Running a different step in an already-used slot errors."""

    async def step_a(ctx: StepContext, x: int) -> int:
        return x

    async def step_b(ctx: StepContext, x: int) -> int:
        return x

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_a = StepKeyEntity(label="a").store().entity_id
            key_b = StepKeyEntity(label="b").store().entity_id
            handle_a = ep.bind_external_step(step_a, key_a)
            handle_b = ep.bind_external_step(step_b, key_b)

            slot = ep.open_cache_slot(ns, "root")
            await slot.run(handle_a, 1)

            # Now try to run a different step in the same slot
            pending = slot.run(handle_b, 1)
            with pytest.raises(CacheSlotError, match="Write-once"):
                await pending


# ---------------------------------------------------------------------------
# Pending forwarding (#5, #6)
# ---------------------------------------------------------------------------


async def test_pending_forwarded_as_arg_to_sub_step(tmp_path: Path):
    """Pending from one step forwarded as arg to another — runtime resolves before execution."""

    async def double(ctx: StepContext, x: int) -> int:
        return x * 2

    async def add_ten(ctx: StepContext, x: int) -> int:
        return x + 10

    handles: dict = {}

    async def orchestrator(ctx: StepContext) -> int:
        slot_a = ctx.cache_slot("a")
        pending_a = slot_a.run(handles["double"], 5)
        # Forward the Pending directly as arg — runtime must resolve it
        slot_b = ctx.cache_slot("b")
        return await slot_b.run(handles["add_ten"], pending_a)

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_double = StepKeyEntity(label="double").store().entity_id
            key_add = StepKeyEntity(label="add_ten").store().entity_id
            key_orch = StepKeyEntity(label="orch_fwd").store().entity_id
            handles["double"] = ep.bind_external_step(double, key_double)
            handles["add_ten"] = ep.bind_external_step(add_ten, key_add)
            orch = ep.bind_external_step(orchestrator, key_orch)

            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run(orch)
            assert result == 20  # (5 * 2) + 10


async def test_pending_from_sub_step_forwarded_to_sibling(tmp_path: Path):
    """Pending from one sibling sub-step forwarded as arg to another sibling."""

    async def produce(ctx: StepContext, x: int) -> int:
        return x * 3

    async def consume(ctx: StepContext, x: int) -> int:
        return x + 100

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce").store().entity_id
            key_consume = StepKeyEntity(label="consume").store().entity_id
            step_produce = ep.bind_external_step(produce, key_produce)
            step_consume = ep.bind_external_step(consume, key_consume)

            root = ep.open_cache_slot(ns, "root")
            pending_a = root.subslot("a").run(step_produce, 7)
            # Forward pending_a as arg to sibling subslot
            pending_b = root.subslot("b").run(step_consume, pending_a)

            assert await pending_b == 121  # (7 * 3) + 100


# ---------------------------------------------------------------------------
# Write-once edge cases (#7)
# ---------------------------------------------------------------------------


async def test_write_once_same_step_different_args(tmp_path: Path):
    """Same step with different args in an already-finished slot → write-once violation."""

    async def identity(ctx: StepContext, x: int) -> int:
        return x

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="id").store().entity_id
            step = ep.bind_external_step(identity, key_id)

            slot = ep.open_cache_slot(ns, "root")
            await slot.run(step, 1)

            # Same step, different arg → different cache key → write-once violation
            pending = slot.run(step, 2)
            with pytest.raises(CacheSlotError, match="Write-once"):
                await pending


# ---------------------------------------------------------------------------
# Resume after partial completion
# ---------------------------------------------------------------------------


async def test_resume_partial_reuses_finished_substeps(tmp_path: Path):
    """After a partial run, finished sub-steps are reused on retry."""
    step_a_count = 0
    step_b_should_fail = True

    async def step_a(ctx: StepContext, x: int) -> int:
        nonlocal step_a_count
        step_a_count += 1
        return x * 2

    async def step_b(ctx: StepContext, x: int) -> int:
        if step_b_should_fail:
            raise ValueError("not ready yet")
        return x + 100

    db_path = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

    # First run: step_a succeeds in its subslot, step_b fails in its subslot
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_a = StepKeyEntity(label="a").store().entity_id
            key_b = StepKeyEntity(label="b").store().entity_id
            handle_a = ep.bind_external_step(step_a, key_a)
            handle_b = ep.bind_external_step(step_b, key_b)

            root = ep.open_cache_slot(ns, "root")
            r_a = root.subslot("a").run(handle_a, 5)
            assert await r_a == 10
            assert step_a_count == 1

            r_b = root.subslot("b").run(handle_b, 10)
            with pytest.raises(StepAbortError):
                await r_b

    # Second run: step_b no longer fails
    step_b_should_fail = False
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_a = StepKeyEntity(label="a").store().entity_id
            key_b = StepKeyEntity(label="b").store().entity_id
            handle_a = ep.bind_external_step(step_a, key_a)
            handle_b = ep.bind_external_step(step_b, key_b)

            root = ep.open_cache_slot(ns, "root")
            r_a = root.subslot("a").run(handle_a, 5)
            assert await r_a == 10
            assert step_a_count == 1  # NOT called again — cached!

            r_b = root.subslot("b").run(handle_b, 10)
            assert await r_b == 110  # now succeeds


async def test_resume_partial_slot_same_key_reexecutes(db_path: str):
    """A Partial slot with the same cache key re-executes (not errors)."""
    call_count = 0
    should_fail = True

    async def flaky_step(ctx: StepContext, x: int) -> int:
        nonlocal call_count
        call_count += 1
        if should_fail:
            raise ValueError("not yet")
        return x + 1

    # First run: fails, slot becomes Partial
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="flaky").store().entity_id
            step = ep.bind_external_step(flaky_step, key_id)
            slot = ep.open_cache_slot(ns, "root")
            pending = slot.run(step, 10)
            with pytest.raises(StepAbortError):
                await pending
            assert call_count == 1

    # Second run: same args, should re-execute (not error)
    should_fail = False
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="flaky").store().entity_id
            step = ep.bind_external_step(flaky_step, key_id)
            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run(step, 10)
            assert result == 11
            assert call_count == 2


# ---------------------------------------------------------------------------
# State persistence tests
# ---------------------------------------------------------------------------


async def test_split_state_persisted_on_subslot_write(tmp_path: Path):
    """When a subslot writes data, the parent's Split state is persisted."""

    async def identity(ctx: StepContext, x: int) -> int:
        return x

    db_path = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="id").store().entity_id
            step = ep.bind_external_step(identity, key_id)

            root = ep.open_cache_slot(ns, "root")
            # Just creating a subslot doesn't persist anything yet
            sub = root.subslot("a")

            # Running in the subslot triggers persistence of both
            # the subslot's Partial/Finished state AND the parent's Split state
            result = await sub.run(step, 42)
            assert result == 42

    # Verify persisted state after reopening
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            root = ep.open_cache_slot(ns, "root")
            state = await root._load_state()
            assert isinstance(state, SlotSplit)

            sub = root.subslot("a")
            sub_state = await sub._load_state()
            assert isinstance(sub_state, SlotFinished)
            result_output = await sub_state.result_ref.load()
            assert result_output.value == 42


async def test_nested_subslot_split_persistence(tmp_path: Path):
    """Nested subslots persist Split state up the entire chain."""

    async def identity(ctx: StepContext, x: int) -> int:
        return x

    db_path = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="id").store().entity_id
            step = ep.bind_external_step(identity, key_id)

            root = ep.open_cache_slot(ns, "root")
            result = await root.subslot("a").subslot("b").run(step, 99)
            assert result == 99

    # Both root and root/s/a should be Split
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            root = ep.open_cache_slot(ns, "root")
            assert isinstance(await root._load_state(), SlotSplit)

            sub_a = root.subslot("a")
            assert isinstance(await sub_a._load_state(), SlotSplit)

            sub_b = sub_a.subslot("b")
            sub_b_state = await sub_b._load_state()
            assert isinstance(sub_b_state, SlotFinished)
            result_output = await sub_b_state.result_ref.load()
            assert result_output.value == 99


async def test_all_state_variants_load_correctly(tmp_path: Path):
    """Verify that SlotSplit, SlotPartial, and SlotFinished all load correctly after restart."""

    async def failing_step(ctx: StepContext) -> int:
        raise ValueError("fail")

    async def ok_step(ctx: StepContext, x: int) -> int:
        return x

    db_path = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_fail = StepKeyEntity(label="fail").store().entity_id
            key_ok = StepKeyEntity(label="ok").store().entity_id
            handle_fail = ep.bind_external_step(failing_step, key_fail)
            handle_ok = ep.bind_external_step(ok_step, key_ok)

            root = ep.open_cache_slot(ns, "root")
            # "split" slot has subslots
            # "partial" slot has a failed run
            # "finished" slot has a completed run
            await root.subslot("finished").run(handle_ok, 7)

            p = root.subslot("partial").run(handle_fail)
            with pytest.raises(StepAbortError):
                await p

    # Reload and verify all three types
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            root = ep.open_cache_slot(ns, "root")

            root_state = await root._load_state()
            assert isinstance(root_state, SlotSplit)

            finished_state = await root.subslot("finished")._load_state()
            assert isinstance(finished_state, SlotFinished)
            result_output = await finished_state.result_ref.load()
            assert result_output.value == 7

            partial_state = await root.subslot("partial")._load_state()
            assert isinstance(partial_state, SlotPartial)


# ---------------------------------------------------------------------------
# Deeply nested args (#12)
# ---------------------------------------------------------------------------


async def test_deeply_nested_args_with_cache_slot(tmp_path: Path):
    """Cache slot embedded in [{"key": [1, 2, {"inner": slot}]}] is properly traced."""
    from typing import Any

    handles: dict = {}

    async def produce(ctx: StepContext, x: int) -> int:
        return x * 10

    async def reader(ctx: StepContext, nested: Any) -> int:
        """Extract the slot from deep nesting and dispatch into it."""
        slot = nested[0]["key"][2]["inner"]
        return await slot.subslot("v").run(handles["produce"], 3)

    async def orchestrator(ctx: StepContext, foreign: Any) -> int:
        """Wrap foreign slot in deeply nested structure and pass to inner step."""
        nested = [{"key": [1, 2, {"inner": foreign}]}]
        return await ctx.cache_slot("run").run(handles["reader"], nested)

    db_path = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce").store().entity_id
            key_reader = StepKeyEntity(label="reader_nested").store().entity_id
            key_orch = StepKeyEntity(label="orch_nested").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handles["reader"] = ep.bind_external_step(reader, key_reader)
            orch = ep.bind_external_step(orchestrator, key_orch)

            slot_b = ep.open_cache_slot(ns, "B")
            await slot_b.subslot("v").run(handles["produce"], 3)

            slot_a = ep.open_cache_slot(ns, "A")
            result = await slot_a.run(orch, slot_b)
            assert result == 30  # 3 * 10

    # Replay with same data → cache hit
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_produce = StepKeyEntity(label="produce").store().entity_id
            key_reader = StepKeyEntity(label="reader_nested").store().entity_id
            key_orch = StepKeyEntity(label="orch_nested").store().entity_id
            handles["produce"] = ep.bind_external_step(produce, key_produce)
            handles["reader"] = ep.bind_external_step(reader, key_reader)
            orch = ep.bind_external_step(orchestrator, key_orch)

            slot_b = ep.open_cache_slot(ns, "B")
            slot_a = ep.open_cache_slot(ns, "A")
            result = await slot_a.run(orch, slot_b)
            assert result == 30


# ---------------------------------------------------------------------------
# Multiple root cache slots (#21)
# ---------------------------------------------------------------------------


async def test_multiple_root_cache_slots(tmp_path: Path):
    """Two independent root cache slots in one entrypoint don't interfere."""

    async def add_one(ctx: StepContext, x: int) -> int:
        return x + 1

    async def times_two(ctx: StepContext, x: int) -> int:
        return x * 2

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_add = StepKeyEntity(label="add_one").store().entity_id
            key_mul = StepKeyEntity(label="times_two").store().entity_id
            step_add = ep.bind_external_step(add_one, key_add)
            step_mul = ep.bind_external_step(times_two, key_mul)

            slot_x = ep.open_cache_slot(ns, "X")
            slot_y = ep.open_cache_slot(ns, "Y")

            rx = await slot_x.run(step_add, 10)
            ry = await slot_y.run(step_mul, 10)

            assert rx == 11
            assert ry == 20


# ---------------------------------------------------------------------------
# Concurrent dispatches (#22)
# ---------------------------------------------------------------------------


async def test_concurrent_dispatches_to_sibling_subslots(tmp_path: Path):
    """Concurrent dispatches to sibling subslots both complete without contention."""
    import anyio

    async def slow_step(ctx: StepContext, x: int) -> int:
        await anyio.sleep(0.01)
        return x * 10

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="slow").store().entity_id
            step = ep.bind_external_step(slow_step, key_id)

            root = ep.open_cache_slot(ns, "root")
            # Dispatch both concurrently (don't await between dispatches)
            p1 = root.subslot("a").run(step, 1)
            p2 = root.subslot("b").run(step, 2)

            assert await p1 == 10
            assert await p2 == 20


# ---------------------------------------------------------------------------
# Same-slot concurrency: same cache key
# ---------------------------------------------------------------------------


async def test_concurrent_same_slot_same_key_blocks_and_reuses(tmp_path: Path):
    """Two concurrent runs in the same slot with the same cache key:
    the second blocks until the first finishes, then reuses the cached result."""
    import anyio

    call_count = 0
    first_started = anyio.Event()
    allow_first_to_finish = anyio.Event()

    async def controlled_step(ctx: StepContext, x: int) -> int:
        nonlocal call_count
        call_count += 1
        first_started.set()
        await allow_first_to_finish.wait()
        return x * 10

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="ctrl").store().entity_id
            step = ep.bind_external_step(controlled_step, key_id)

            slot = ep.open_cache_slot(ns, "root")

            # Dispatch first run
            p1 = slot.run(step, 5)
            # Wait until the first run is actually executing
            await first_started.wait()
            # Dispatch second run with same args (same cache key) while first is mid-flight
            p2 = slot.run(step, 5)

            # Let the first run complete
            allow_first_to_finish.set()

            assert await p1 == 50
            assert await p2 == 50
            # The step should have been called only once
            assert call_count == 1


async def test_sequential_same_slot_same_key_reuses_cache(tmp_path: Path):
    """Sequential runs in the same slot with the same cache key: second reuses cache."""
    call_count = 0

    async def tracked_step(ctx: StepContext, x: int) -> int:
        nonlocal call_count
        call_count += 1
        return x * 10

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="tracked").store().entity_id
            step = ep.bind_external_step(tracked_step, key_id)

            slot = ep.open_cache_slot(ns, "root")

            # First run
            assert await slot.run(step, 5) == 50
            assert call_count == 1

            # Second run — same cache key, should hit cache
            assert await slot.run(step, 5) == 50
            assert call_count == 1


async def test_concurrent_same_slot_same_key_first_aborts(tmp_path: Path):
    """Two concurrent runs in the same slot, same cache key, but the first aborts.
    The second should raise StepAbortError (not deadlock or silently succeed)."""
    import anyio

    first_started = anyio.Event()
    allow_first_to_finish = anyio.Event()

    async def failing_step(ctx: StepContext, x: int) -> int:
        first_started.set()
        await allow_first_to_finish.wait()
        raise ValueError("boom")

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="fail").store().entity_id
            step = ep.bind_external_step(failing_step, key_id)

            slot = ep.open_cache_slot(ns, "root")

            p1 = slot.run(step, 5)
            await first_started.wait()
            p2 = slot.run(step, 5)

            allow_first_to_finish.set()

            # First run aborts
            with pytest.raises(StepAbortError):
                await p1
            # Second run also gets an abort (not a deadlock)
            with pytest.raises(StepAbortError):
                await p2


# ---------------------------------------------------------------------------
# Same-slot concurrency: different cache key
# ---------------------------------------------------------------------------


async def test_concurrent_same_slot_different_key_errors(tmp_path: Path):
    """Two concurrent runs in the same slot with different cache keys:
    the second raises CacheSlotError."""
    import anyio

    first_started = anyio.Event()
    allow_first_to_finish = anyio.Event()

    async def controlled_step(ctx: StepContext, x: int) -> int:
        first_started.set()
        await allow_first_to_finish.wait()
        return x * 10

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="ctrl").store().entity_id
            step = ep.bind_external_step(controlled_step, key_id)

            slot = ep.open_cache_slot(ns, "root")

            # Dispatch first run
            p1 = slot.run(step, 5)
            # Wait until first run is executing
            await first_started.wait()
            # Dispatch second run with different args (different cache key)
            p2 = slot.run(step, 999)

            # Let the first run complete
            allow_first_to_finish.set()

            assert await p1 == 50
            with pytest.raises(CacheSlotError, match="Write-once"):
                await p2


async def test_sequential_same_slot_different_key_errors(tmp_path: Path):
    """Sequential runs in the same slot with different cache keys:
    second raises CacheSlotError (write-once violation)."""

    async def identity(ctx: StepContext, x: int) -> int:
        return x

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="id").store().entity_id
            step = ep.bind_external_step(identity, key_id)

            slot = ep.open_cache_slot(ns, "root")
            assert await slot.run(step, 1) == 1

            pending = slot.run(step, 2)
            with pytest.raises(CacheSlotError, match="Write-once"):
                await pending
