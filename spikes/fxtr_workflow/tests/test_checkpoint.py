"""Tests for save_checkpoint / load_checkpoint."""

from pathlib import Path

import pytest

from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr_workflow.entrypoint import TopLevelEntrypoint
from fxtr_workflow.errors import FallibleRunError, StepAbortError
from fxtr_workflow.step_context import StepContext

from .conftest import StepKeyEntity

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# 11a. Save and load within same execution
# ---------------------------------------------------------------------------


async def test_save_and_load_checkpoint(tmp_path: Path):
    async def step_with_ckpt(ctx: StepContext) -> str:
        await ctx.save_checkpoint({"progress": 50})
        ckpt = await ctx.load_checkpoint()
        assert ckpt == {"progress": 50}
        return "done"

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="ckpt").store().entity_id
            step = ep.bind_external_step(step_with_ckpt, key_id)
            slot = ep.open_cache_slot(ns, "root")
            assert await slot.run(step) == "done"


# ---------------------------------------------------------------------------
# 11b. Save, abort, resume — checkpoint survives across executions
# ---------------------------------------------------------------------------


async def test_checkpoint_survives_abort_and_resume(tmp_path: Path):
    run_count = 0

    async def step_with_resume(ctx: StepContext) -> str:
        nonlocal run_count
        run_count += 1
        ckpt = await ctx.load_checkpoint()
        if ckpt is not None:
            return f"resumed from {ckpt}"
        await ctx.save_checkpoint("halfway")
        raise RuntimeError("crash mid-way")

    db_path = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

    # First run: saves checkpoint, then crashes
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="resume").store().entity_id
            step = ep.bind_external_step(step_with_resume, key_id)
            slot = ep.open_cache_slot(ns, "root")
            with pytest.raises(StepAbortError):
                await slot.run(step)

    assert run_count == 1

    # Second run: checkpoint is still there, step resumes
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="resume").store().entity_id
            step = ep.bind_external_step(step_with_resume, key_id)
            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run(step)
            assert result == "resumed from halfway"

    assert run_count == 2


# ---------------------------------------------------------------------------
# 11c. Overwrite: second save replaces first
# ---------------------------------------------------------------------------


async def test_checkpoint_overwrite(tmp_path: Path):
    async def step_overwrites(ctx: StepContext) -> str:
        await ctx.save_checkpoint("first")
        await ctx.save_checkpoint("second")
        ckpt = await ctx.load_checkpoint()
        assert ckpt == "second"
        return "done"

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="overwrite").store().entity_id
            step = ep.bind_external_step(step_overwrites, key_id)
            slot = ep.open_cache_slot(ns, "root")
            assert await slot.run(step) == "done"


# ---------------------------------------------------------------------------
# 11d. Load with no checkpoint → None
# ---------------------------------------------------------------------------


async def test_load_checkpoint_returns_none_when_empty(tmp_path: Path):
    async def step_no_ckpt(ctx: StepContext) -> str:
        ckpt = await ctx.load_checkpoint()
        assert ckpt is None
        return "none"

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="nocheck").store().entity_id
            step = ep.bind_external_step(step_no_ckpt, key_id)
            slot = ep.open_cache_slot(ns, "root")
            assert await slot.run(step) == "none"


# ---------------------------------------------------------------------------
# 11e. Checkpoint cleaned up on successful finish
# ---------------------------------------------------------------------------


async def test_checkpoint_cleaned_up_on_finish(tmp_path: Path):
    async def step_saves_ckpt(ctx: StepContext) -> str:
        await ctx.save_checkpoint("data")
        return "done"

    db_path = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

    # Run and complete
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="clean").store().entity_id
            step = ep.bind_external_step(step_saves_ckpt, key_id)
            slot = ep.open_cache_slot(ns, "root")
            await slot.run(step)

    # Verify the checkpoint pin was deleted
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        ref = await ns.try_retrieve("root/ckpt")
        assert ref is None


# ---------------------------------------------------------------------------
# 11f. Checkpoint cleaned up on fallible error finish
# ---------------------------------------------------------------------------


async def test_checkpoint_cleaned_up_on_fallible_error(tmp_path: Path):
    async def step_ckpt_then_fail(ctx: StepContext) -> str:
        await ctx.save_checkpoint("before error")
        raise ValueError("fail after checkpoint")

    db_path = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="clean_fallible").store().entity_id
            step = ep.bind_external_step(step_ckpt_then_fail, key_id)
            slot = ep.open_cache_slot(ns, "root")
            with pytest.raises(FallibleRunError):
                await slot.run_fallible(step)

    # Verify the checkpoint pin was deleted
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        ref = await ns.try_retrieve("root/ckpt")
        assert ref is None


# ---------------------------------------------------------------------------
# 11g. Checkpoint data validation (rejects non-serializable types)
# ---------------------------------------------------------------------------


async def test_checkpoint_rejects_non_serializable(tmp_path: Path):
    async def step_bad_ckpt(ctx: StepContext) -> str:
        await ctx.save_checkpoint(object())  # Not serializable
        return "never"

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="bad_ckpt").store().entity_id
            step = ep.bind_external_step(step_bad_ckpt, key_id)
            slot = ep.open_cache_slot(ns, "root")
            # The step will abort because save_checkpoint should validate
            with pytest.raises(StepAbortError):
                await slot.run(step)
