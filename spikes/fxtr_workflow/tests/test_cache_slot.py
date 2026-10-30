"""Tests for CacheSlot state machine invariants and subslot behavior."""

from pathlib import Path

import pytest

from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr_workflow.entities import SlotFinished, SlotSplit
from fxtr_workflow.entrypoint import TopLevelEntrypoint
from fxtr_workflow.errors import CacheSlotError, StepAbortError
from fxtr_workflow.step_context import StepContext

from .conftest import StepKeyEntity

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Subslot key normalization (#1)
# ---------------------------------------------------------------------------


async def test_int_and_string_subslot_keys_are_equivalent(tmp_path: Path):
    """subslot(42) and subslot("42") return the same object with the same pin_key."""

    async def identity(ctx: StepContext, x: int) -> int:
        return x

    db_path = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="id").store().entity_id
            step = ep.bind_external_step(identity, key_id)

            root = ep.open_cache_slot(ns, "root")

            sub_int = root.subslot(42)
            sub_str = root.subslot("42")
            assert sub_int is sub_str

            result = await sub_int.run(step, 7)
            assert result == 7

    # Resume via string key should find the finished run
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            root = ep.open_cache_slot(ns, "root")
            sub = root.subslot("42")
            state = await sub._load_state()
            assert isinstance(state, SlotFinished)


async def test_int_and_string_ctx_cache_slot_keys_are_equivalent(tmp_path: Path):
    """ctx.cache_slot(42) and ctx.cache_slot("42") return the same underlying slot."""
    handles: dict = {}

    async def outer(ctx: StepContext) -> int:
        slot_int = ctx.cache_slot(42)
        result = await slot_int.run(handles["inner"], 10)
        # Access via string key — should be the same internal slot
        slot_str = ctx.cache_slot("42")
        result2 = await slot_str.run(handles["inner"], 10)
        assert result == result2
        return result

    async def inner(ctx: StepContext, x: int) -> int:
        return x + 1

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_outer = StepKeyEntity(label="outer").store().entity_id
            key_inner = StepKeyEntity(label="inner").store().entity_id
            handles["inner"] = ep.bind_external_step(inner, key_inner)
            outer_step = ep.bind_external_step(outer, key_outer)

            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run(outer_step)
            assert result == 11


# ---------------------------------------------------------------------------
# Split state integrity (#2, #3, #4)
# ---------------------------------------------------------------------------


async def test_cannot_subslot_a_partial_slot(db_path: str):
    """Accessing subslots on a Partial slot raises CacheSlotError."""

    async def failing_step(ctx: StepContext) -> int:
        raise ValueError("fail to leave slot Partial")

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="failing").store().entity_id
            step = ep.bind_external_step(failing_step, key_id)

            slot = ep.open_cache_slot(ns, "root")
            pending = slot.run(step)
            with pytest.raises(StepAbortError):
                await pending

    # Re-open and try to use subslots on the Partial slot
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            slot = ep.open_cache_slot(ns, "root")
            child = slot.subslot("x")
            # The error surfaces when the subslot tries to write (ensure_split on parent)
            key_id = StepKeyEntity(label="id").store().entity_id

            async def identity(ctx: StepContext, x: int) -> int:
                return x

            step = ep.bind_external_step(identity, key_id)
            pending = child.run(step, 1)
            with pytest.raises(CacheSlotError, match="Cannot use slot.*subslots"):
                await pending


async def test_cannot_subslot_a_finished_slot(db_path: str):
    """Accessing subslots on a Finished slot raises CacheSlotError."""

    async def identity(ctx: StepContext, x: int) -> int:
        return x

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="id").store().entity_id
            step = ep.bind_external_step(identity, key_id)

            slot = ep.open_cache_slot(ns, "root")
            await slot.run(step, 1)

    # Re-open and try subslots on the Finished slot
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            slot = ep.open_cache_slot(ns, "root")
            child = slot.subslot("x")
            key_id = StepKeyEntity(label="id").store().entity_id

            async def identity2(ctx: StepContext, x: int) -> int:
                return x

            step = ep.bind_external_step(identity2, key_id)
            pending = child.run(step, 1)
            with pytest.raises(CacheSlotError, match="Cannot use slot.*subslots"):
                await pending


async def test_cannot_run_in_a_split_slot(db_path: str):
    """Running directly in a Split slot raises CacheSlotError."""

    async def identity(ctx: StepContext, x: int) -> int:
        return x

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="id").store().entity_id
            step = ep.bind_external_step(identity, key_id)

            root = ep.open_cache_slot(ns, "root")
            # Write into a subslot so the root becomes Split
            await root.subslot("a").run(step, 1)

    # Re-open and try to run directly in the Split root
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="id").store().entity_id
            step = ep.bind_external_step(identity, key_id)

            root = ep.open_cache_slot(ns, "root")
            pending = root.run(step, 99)
            with pytest.raises(CacheSlotError, match="already used for subslots"):
                await pending


# ---------------------------------------------------------------------------
# Deep subslot path (#11)
# ---------------------------------------------------------------------------


async def test_deep_subslot_path(db_path: str):
    """Four levels deep: a/b/c/d. Verify the run and Split chain persist on resume."""

    async def identity(ctx: StepContext, x: int) -> int:
        return x

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="id").store().entity_id
            step = ep.bind_external_step(identity, key_id)

            root = ep.open_cache_slot(ns, "root")
            leaf = root.subslot("a").subslot("b").subslot("c").subslot("d")
            result = await leaf.run(step, 42)
            assert result == 42

    # Verify the entire Split chain and the Finished leaf
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            root = ep.open_cache_slot(ns, "root")
            assert isinstance(await root._load_state(), SlotSplit)

            a = root.subslot("a")
            assert isinstance(await a._load_state(), SlotSplit)

            b = a.subslot("b")
            assert isinstance(await b._load_state(), SlotSplit)

            c = b.subslot("c")
            assert isinstance(await c._load_state(), SlotSplit)

            d = c.subslot("d")
            d_state = await d._load_state()
            assert isinstance(d_state, SlotFinished)
            result_output = await d_state.result_ref.load()
            assert result_output.value == 42
