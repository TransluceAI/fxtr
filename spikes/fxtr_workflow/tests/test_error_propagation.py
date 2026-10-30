"""Tests for error propagation, abort handling, and the poisoning model."""

from pathlib import Path
from typing import Any

import pytest

from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr_workflow.entities import SlotPartial
from fxtr_workflow.entrypoint import TopLevelEntrypoint
from fxtr_workflow.errors import StepAbortError
from fxtr_workflow.step_context import StepContext

from .conftest import StepKeyEntity

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Basic abort propagation
# ---------------------------------------------------------------------------


async def test_step_abort_propagates(tmp_path: Path):
    """An exception in a step becomes StepAbortError."""

    async def failing_step(ctx: StepContext) -> int:
        raise ValueError("something went wrong")

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="failing").store().entity_id
            step = ep.bind_external_step(failing_step, key_id)
            slot = ep.open_cache_slot(ns, "root")
            pending = slot.run(step)
            with pytest.raises(StepAbortError):
                await pending


async def test_abort_leaves_slot_partial(db_path: str):
    """After an abort, the slot is Partial, not Finished."""

    async def failing_step(ctx: StepContext) -> int:
        raise ValueError("boom")

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="failing").store().entity_id
            step = ep.bind_external_step(failing_step, key_id)
            slot = ep.open_cache_slot(ns, "root")
            pending = slot.run(step)
            with pytest.raises(StepAbortError):
                await pending

    # Check state persisted as Partial
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            slot = ep.open_cache_slot(ns, "root")
            state = await slot._load_state()
            assert isinstance(state, SlotPartial)


# ---------------------------------------------------------------------------
# Poisoning: sub-step abort prevents caching of outer step
# ---------------------------------------------------------------------------


async def test_poisoning_catches_swallowed_abort(tmp_path: Path):
    """If a sub-step aborts, the outer step's result is discarded even if it returns normally."""

    async def inner_failing(ctx: StepContext) -> int:
        raise ValueError("inner boom")

    # We need to pass the inner step handle into the outer step.
    # Store it in a mutable container so the outer step closure can access it.
    handles: dict[str, Any] = {}

    async def outer_catches(ctx: StepContext) -> str:
        # Dispatch inner step in a sub-slot — it will fail
        inner_slot = ctx.cache_slot("inner")
        pending = inner_slot.run(
            handles["inner"],
        )
        # Explicitly catch the abort (which is illegal)
        try:
            await pending
        except BaseException:
            pass
        return "should be discarded"

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_inner = StepKeyEntity(label="inner_fail").store().entity_id
            key_outer = StepKeyEntity(label="outer_catch").store().entity_id
            handles["inner"] = ep.bind_external_step(inner_failing, key_inner)
            outer_step = ep.bind_external_step(outer_catches, key_outer)
            slot = ep.open_cache_slot(ns, "root")
            pending = slot.run(outer_step)
            with pytest.raises(StepAbortError):
                await pending


# ---------------------------------------------------------------------------
# Nested abort propagation (#17)
# ---------------------------------------------------------------------------


async def test_nested_abort_propagation_three_levels(tmp_path: Path):
    """A → B → C, C raises. Abort propagates through B up to A. Both A and B stay Partial."""
    handles: dict[str, Any] = {}

    async def step_c(ctx: StepContext) -> int:
        raise ValueError("deep failure")

    async def step_b(ctx: StepContext) -> int:
        slot_c = ctx.cache_slot("c")
        return await slot_c.run(handles["step_c"])

    async def step_a(ctx: StepContext) -> int:
        slot_b = ctx.cache_slot("b")
        return await slot_b.run(handles["step_b"])

    db_path = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_a = StepKeyEntity(label="step_a").store().entity_id
            key_b = StepKeyEntity(label="step_b").store().entity_id
            key_c = StepKeyEntity(label="step_c").store().entity_id
            handles["step_b"] = ep.bind_external_step(step_b, key_b)
            handles["step_c"] = ep.bind_external_step(step_c, key_c)
            handle_a = ep.bind_external_step(step_a, key_a)

            slot = ep.open_cache_slot(ns, "root")
            pending = slot.run(handle_a)
            with pytest.raises(StepAbortError):
                await pending

    # Verify root slot is Partial (A didn't finish)
    from fxtr_workflow.entities import SlotPartial

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            slot = ep.open_cache_slot(ns, "root")
            state = await slot._load_state()
            assert isinstance(state, SlotPartial)


# ---------------------------------------------------------------------------
# Multiple sub-steps abort simultaneously (#18)
# ---------------------------------------------------------------------------


async def test_multiple_sub_steps_abort_simultaneously(tmp_path: Path):
    """Two sub-steps both fail. Parent gets StepAbortError, result not cached."""
    handles: dict[str, Any] = {}

    async def failing_a(ctx: StepContext) -> int:
        raise ValueError("fail a")

    async def failing_b(ctx: StepContext) -> int:
        raise ValueError("fail b")

    async def parent(ctx: StepContext) -> str:
        p1 = ctx.cache_slot("a").run(handles["fa"])
        p2 = ctx.cache_slot("b").run(handles["fb"])
        try:
            await p1
        except BaseException:
            pass
        try:
            await p2
        except BaseException:
            pass
        return "should be discarded"

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_fa = StepKeyEntity(label="fail_a").store().entity_id
            key_fb = StepKeyEntity(label="fail_b").store().entity_id
            key_parent = StepKeyEntity(label="parent_multi_abort").store().entity_id
            handles["fa"] = ep.bind_external_step(failing_a, key_fa)
            handles["fb"] = ep.bind_external_step(failing_b, key_fb)
            parent_step = ep.bind_external_step(parent, key_parent)

            slot = ep.open_cache_slot(ns, "root")
            pending = slot.run(parent_step)
            with pytest.raises(StepAbortError):
                await pending


# ---------------------------------------------------------------------------
# Un-awaited sub-step (#19)
# ---------------------------------------------------------------------------


async def test_unawaited_sub_step_still_waited_and_poisons(tmp_path: Path):
    """Sub-step dispatched but never awaited — containment waits for it and abort poisons parent."""
    handles: dict[str, Any] = {}

    async def failing_inner(ctx: StepContext) -> int:
        raise ValueError("inner fail")

    async def parent_ignores(ctx: StepContext) -> str:
        # Dispatch but never await the result
        ctx.cache_slot("inner").run(handles["inner"])
        return "looks ok"

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_inner = StepKeyEntity(label="inner_unawaited").store().entity_id
            key_parent = StepKeyEntity(label="parent_ignores").store().entity_id
            handles["inner"] = ep.bind_external_step(failing_inner, key_inner)
            parent_step = ep.bind_external_step(parent_ignores, key_parent)

            slot = ep.open_cache_slot(ns, "root")
            pending = slot.run(parent_step)
            with pytest.raises(StepAbortError):
                await pending
