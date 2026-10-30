"""Tests for run_fallible: caching errors as valid outcomes."""

from pathlib import Path

import pytest

from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr_workflow.cache_slot import CacheSlot
from fxtr_workflow.entrypoint import TopLevelEntrypoint
from fxtr_workflow.errors import (
    CacheSlotError,
    CustomFallibleRunError,
    FallibleRunError,
    StepAbortError,
    WrappedFallibleRunError,
)
from fxtr_workflow.step_context import StepContext

from .conftest import StepKeyEntity

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# 10a. Basic fallible run: step raises, error cached, replayed on second call
# ---------------------------------------------------------------------------


async def test_fallible_error_cached_and_replayed(tmp_path: Path):
    call_count = 0

    async def failing_step(ctx: StepContext, x: int) -> int:
        nonlocal call_count
        call_count += 1
        raise ValueError(f"bad value: {x}")

    db_path = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

    # First run: step fails, error is cached
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="failing").store().entity_id
            step = ep.bind_external_step(failing_step, key_id)
            slot = ep.open_cache_slot(ns, "root")
            pending = slot.run_fallible(step, 42)
            with pytest.raises(WrappedFallibleRunError) as exc_info:
                await pending
            assert "bad value: 42" in str(exc_info.value)
            assert exc_info.value.original_type_name == "ValueError"

    assert call_count == 1

    # Second run: error replayed from cache without re-executing
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="failing").store().entity_id
            step = ep.bind_external_step(failing_step, key_id)
            slot = ep.open_cache_slot(ns, "root")
            pending = slot.run_fallible(step, 42)
            with pytest.raises(WrappedFallibleRunError) as exc_info:
                await pending
            assert "bad value: 42" in str(exc_info.value)

    assert call_count == 1  # Not called again


# ---------------------------------------------------------------------------
# 10b. Fallible run success: step succeeds, result cached normally
# ---------------------------------------------------------------------------


async def test_fallible_success_cached(tmp_path: Path):
    call_count = 0

    async def good_step(ctx: StepContext, x: int) -> int:
        nonlocal call_count
        call_count += 1
        return x + 1

    db_path = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="good").store().entity_id
            step = ep.bind_external_step(good_step, key_id)
            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run_fallible(step, 41)
            assert result == 42

    # Second run: cached
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="good").store().entity_id
            step = ep.bind_external_step(good_step, key_id)
            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run_fallible(step, 41)
            assert result == 42

    assert call_count == 1


# ---------------------------------------------------------------------------
# 10c. Cache-slot args rejected by run_fallible
# ---------------------------------------------------------------------------


async def test_fallible_rejects_cache_slot_args(tmp_path: Path):
    async def some_step(ctx: StepContext, slot: CacheSlot) -> int:
        return 1

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="some").store().entity_id
            step = ep.bind_external_step(some_step, key_id)
            slot = ep.open_cache_slot(ns, "root")
            other_slot = ep.open_cache_slot(ns, "other")
            with pytest.raises(TypeError, match="run_fallible does not allow cache slot"):
                slot.run_fallible(step, other_slot)


# ---------------------------------------------------------------------------
# 10d. `run` on a slot with cached error → CacheSlotError
# ---------------------------------------------------------------------------


async def test_run_on_slot_with_cached_error(tmp_path: Path):
    async def failing_step(ctx: StepContext) -> int:
        raise ValueError("oops")

    async def good_step(ctx: StepContext) -> int:
        return 1

    db_path = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

    # First: run_fallible caches the error
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="step").store().entity_id
            step = ep.bind_external_step(failing_step, key_id)
            slot = ep.open_cache_slot(ns, "root")
            with pytest.raises(FallibleRunError):
                await slot.run_fallible(step)

    # Second: try run (infallible) on same slot → CacheSlotError
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="step").store().entity_id
            step = ep.bind_external_step(good_step, key_id)
            slot = ep.open_cache_slot(ns, "root")
            pending = slot.run(step)
            with pytest.raises(CacheSlotError, match="cached error from a prior run_fallible"):
                await pending


# ---------------------------------------------------------------------------
# 10e. run_fallible on a slot with cached success → returns cached result
# ---------------------------------------------------------------------------


async def test_fallible_on_slot_with_cached_success(tmp_path: Path):
    call_count = 0

    async def good_step(ctx: StepContext) -> str:
        nonlocal call_count
        call_count += 1
        return "ok"

    db_path = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

    # First: normal run caches success
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="step").store().entity_id
            step = ep.bind_external_step(good_step, key_id)
            slot = ep.open_cache_slot(ns, "root")
            assert await slot.run(step) == "ok"

    # Second: run_fallible on same slot returns cached success
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="step").store().entity_id
            step = ep.bind_external_step(good_step, key_id)
            slot = ep.open_cache_slot(ns, "root")
            assert await slot.run_fallible(step) == "ok"

    assert call_count == 1


# ---------------------------------------------------------------------------
# 10f. Inner run() abort bubbles up, cached as error in fallible slot
# ---------------------------------------------------------------------------


async def test_inner_abort_cached_as_fallible_error(tmp_path: Path):
    async def inner_fails(ctx: StepContext) -> int:
        raise RuntimeError("inner boom")

    async def outer(ctx: StepContext) -> int:
        inner_slot = ctx.cache_slot("inner")
        pending = inner_slot.run(
            inner_fails_handle,
        )
        return await pending  # StepAbortError propagates

    db_path = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            inner_key = StepKeyEntity(label="inner").store().entity_id
            inner_fails_handle = ep.bind_external_step(inner_fails, inner_key)

            outer_key = StepKeyEntity(label="outer").store().entity_id
            outer_handle = ep.bind_external_step(outer, outer_key)

            slot = ep.open_cache_slot(ns, "root")
            with pytest.raises(WrappedFallibleRunError) as exc_info:
                await slot.run_fallible(outer_handle)
            # The inner abort becomes a fallible error at the boundary
            assert (
                "StepAbortError" in exc_info.value.original_type_name
                and "inner boom" in exc_info.value.message
            )


# ---------------------------------------------------------------------------
# 10g. Poisoning: inner abort caught by fallible step, still becomes error
# ---------------------------------------------------------------------------


async def test_poisoning_in_fallible_step_cached(tmp_path: Path):
    async def inner_fails(ctx: StepContext) -> int:
        raise RuntimeError("inner fail")

    async def swallows_abort(ctx: StepContext) -> str:
        inner_slot = ctx.cache_slot("inner")
        pending = inner_slot.run(inner_fails_handle)
        try:
            await pending
        except BaseException:
            pass  # Swallow the abort
        return "looks fine"

    db_path = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            inner_key = StepKeyEntity(label="inner").store().entity_id
            inner_fails_handle = ep.bind_external_step(inner_fails, inner_key)

            outer_key = StepKeyEntity(label="outer").store().entity_id
            outer_handle = ep.bind_external_step(swallows_abort, outer_key)

            slot = ep.open_cache_slot(ns, "root")
            # Even though step returned "looks fine", poisoning forces an error
            with pytest.raises(WrappedFallibleRunError):
                await slot.run_fallible(outer_handle)


# ---------------------------------------------------------------------------
# 10h. CustomFallibleRunError: explicit raise, same message on replay
# ---------------------------------------------------------------------------


async def test_custom_fallible_error(tmp_path: Path):
    async def custom_fail(ctx: StepContext) -> int:
        raise CustomFallibleRunError("custom problem")

    db_path = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

    # First run
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="custom").store().entity_id
            step = ep.bind_external_step(custom_fail, key_id)
            slot = ep.open_cache_slot(ns, "root")
            with pytest.raises(CustomFallibleRunError) as exc_info:
                await slot.run_fallible(step)
            assert exc_info.value.message == "custom problem"
            assert exc_info.value.original_type_name is None

    # Replay: same type and message
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="custom").store().entity_id
            step = ep.bind_external_step(custom_fail, key_id)
            slot = ep.open_cache_slot(ns, "root")
            with pytest.raises(CustomFallibleRunError) as exc_info:
                await slot.run_fallible(step)
            assert exc_info.value.message == "custom problem"


# ---------------------------------------------------------------------------
# 10i. WrappedFallibleRunError: arbitrary exception, stringified on replay
# ---------------------------------------------------------------------------


async def test_wrapped_fallible_error(tmp_path: Path):
    async def type_fail(ctx: StepContext) -> int:
        raise TypeError("wrong type")

    db_path = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"

    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="type").store().entity_id
            step = ep.bind_external_step(type_fail, key_id)
            slot = ep.open_cache_slot(ns, "root")
            with pytest.raises(WrappedFallibleRunError) as exc_info:
                await slot.run_fallible(step)
            assert exc_info.value.original_type_name == "TypeError"
            assert exc_info.value.message == "wrong type"

    # Replay
    async with SQLiteFxtrNamespace(db_path, "test") as ns:
        async with TopLevelEntrypoint() as ep:
            key_id = StepKeyEntity(label="type").store().entity_id
            step = ep.bind_external_step(type_fail, key_id)
            slot = ep.open_cache_slot(ns, "root")
            with pytest.raises(WrappedFallibleRunError) as exc_info:
                await slot.run_fallible(step)
            assert exc_info.value.original_type_name == "TypeError"
            assert exc_info.value.message == "wrong type"


# ---------------------------------------------------------------------------
# 10j. Nested: fallible step dispatches inner fallible step
# ---------------------------------------------------------------------------


async def test_nested_fallible(tmp_path: Path):
    async def inner_fail(ctx: StepContext) -> int:
        raise ValueError("inner error")

    async def outer(ctx: StepContext) -> str:
        inner_slot = ctx.cache_slot("inner")
        try:
            await inner_slot.run_fallible(inner_fail_handle)
        except FallibleRunError as e:
            return f"caught: {e.message}"
        return "no error"

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            inner_key = StepKeyEntity(label="inner").store().entity_id
            inner_fail_handle = ep.bind_external_step(inner_fail, inner_key)

            outer_key = StepKeyEntity(label="outer").store().entity_id
            outer_handle = ep.bind_external_step(outer, outer_key)

            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run(outer_handle)
            assert result == "caught: inner error"


# ---------------------------------------------------------------------------
# 10k. Caller catches FallibleRunError, continues without poisoning
# ---------------------------------------------------------------------------


async def test_caller_catches_fallible_no_poisoning(tmp_path: Path):
    async def fail_step(ctx: StepContext) -> int:
        raise ValueError("fail")

    async def resilient_caller(ctx: StepContext) -> str:
        slot = ctx.cache_slot("inner")
        try:
            await slot.run_fallible(fail_handle)
        except FallibleRunError:
            pass  # Caught — no poisoning
        return "survived"

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            fail_key = StepKeyEntity(label="fail").store().entity_id
            fail_handle = ep.bind_external_step(fail_step, fail_key)

            caller_key = StepKeyEntity(label="caller").store().entity_id
            caller_handle = ep.bind_external_step(resilient_caller, caller_key)

            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run(caller_handle)
            assert result == "survived"


# ---------------------------------------------------------------------------
# 10l. Caller doesn't catch FallibleRunError, becomes abort at next level
# ---------------------------------------------------------------------------


async def test_uncaught_fallible_becomes_abort(tmp_path: Path):
    async def fail_step(ctx: StepContext) -> int:
        raise ValueError("fail")

    async def non_catching_caller(ctx: StepContext) -> str:
        slot = ctx.cache_slot("inner")
        await slot.run_fallible(fail_handle)  # FallibleRunError propagates
        return "never reached"

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            fail_key = StepKeyEntity(label="fail").store().entity_id
            fail_handle = ep.bind_external_step(fail_step, fail_key)

            caller_key = StepKeyEntity(label="caller").store().entity_id
            caller_handle = ep.bind_external_step(non_catching_caller, caller_key)

            slot = ep.open_cache_slot(ns, "root")
            # The uncaught FallibleRunError becomes a StepAbortError for the
            # infallible run() call
            with pytest.raises(StepAbortError):
                await slot.run(caller_handle)


# ---------------------------------------------------------------------------
# 10m. run_fallible from inside a step (via TracingCacheSlot path)
# ---------------------------------------------------------------------------


async def test_fallible_from_inside_step(tmp_path: Path):
    """run_fallible works on internal cache slots (TracingCacheSlot)."""

    async def inner_fail(ctx: StepContext) -> int:
        raise ValueError("inner error")

    async def outer(ctx: StepContext) -> str:
        slot = ctx.cache_slot("sub")
        try:
            await slot.run_fallible(inner_handle)
        except WrappedFallibleRunError as e:
            return f"caught {e.original_type_name}"
        return "no error"

    async with SQLiteFxtrNamespace(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", "test") as ns:
        async with TopLevelEntrypoint() as ep:
            inner_key = StepKeyEntity(label="inner").store().entity_id
            inner_handle = ep.bind_external_step(inner_fail, inner_key)

            outer_key = StepKeyEntity(label="outer").store().entity_id
            outer_handle = ep.bind_external_step(outer, outer_key)

            slot = ep.open_cache_slot(ns, "root")
            result = await slot.run(outer_handle)
            assert result == "caught ValueError"
