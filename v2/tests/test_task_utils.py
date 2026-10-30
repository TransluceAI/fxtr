"""Tests for scope-context hygiene: ``no_active_scope`` /
``NoActiveScopeError`` (`fxtr2/data/entity_ref_scope.py`) and the
task-start helpers (`fxtr2/common/task_utils.py`)."""

from __future__ import annotations

import anyio
import anyio.abc
import pytest

from fxtr2.common.task_utils import start_soon_with_scope, start_with_scope
from fxtr2.data.entity_ref_scope import (
    GLOBAL_SCOPE,
    FxtrScope,
    NoActiveScopeError,
    get_active_scope,
    no_active_scope,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_no_active_scope_blocks_implicit_use() -> None:
    before = get_active_scope()
    with no_active_scope():
        with pytest.raises(NoActiveScopeError) as excinfo:
            get_active_scope()
        assert "no_active_scope()" in str(excinfo.value)
        assert "scope_discipline" in str(excinfo.value)
        with pytest.raises(NoActiveScopeError):
            FxtrScope.get_active()
        # Non-isolated construction captures the implicit parent through
        # get_active_scope() — the hazard becomes an error at the line.
        with pytest.raises(NoActiveScopeError):
            FxtrScope(name="implicit-under-poison")
        # Fully explicit construction is unaffected.
        FxtrScope(isolated=True, include=[GLOBAL_SCOPE], name="explicit-under-poison")
    assert get_active_scope() is before


async def test_poison_and_activation_nest() -> None:
    parent = FxtrScope(isolated=True, include=[GLOBAL_SCOPE], name="nest-parent")
    async with parent:
        with no_active_scope():
            with parent.as_active_scope():
                assert get_active_scope() is parent
            with pytest.raises(NoActiveScopeError):
                get_active_scope()
        assert get_active_scope() is GLOBAL_SCOPE


async def test_start_soon_with_scope_installs_scope() -> None:
    parent = FxtrScope(isolated=True, include=[GLOBAL_SCOPE], name="helper-parent")
    seen: dict[str, object] = {}
    done = anyio.Event()

    async def probe() -> None:
        seen["scope"] = get_active_scope()
        done.set()

    async with parent:
        async with anyio.create_task_group() as tg:
            start_soon_with_scope(tg, probe, scope=parent)
            await done.wait()
    assert seen["scope"] is parent


async def test_start_soon_with_scope_none_poisons() -> None:
    seen: dict[str, object] = {}
    done = anyio.Event()

    async def probe() -> None:
        try:
            get_active_scope()
            seen["error"] = None
        except NoActiveScopeError as exc:
            seen["error"] = exc
        done.set()

    async with anyio.create_task_group() as tg:
        start_soon_with_scope(tg, probe, scope=None)
        await done.wait()
    assert isinstance(seen["error"], NoActiveScopeError)


async def test_start_with_scope_passes_task_status_and_args() -> None:
    parent = FxtrScope(isolated=True, include=[GLOBAL_SCOPE], name="start-parent")
    release = anyio.Event()

    async def serve(tag: str, *, task_status: anyio.abc.TaskStatus[str]) -> None:
        task_status.started(f"{tag}:{get_active_scope()._name}")
        await release.wait()

    async with parent:
        async with anyio.create_task_group() as tg:
            started = await start_with_scope(tg, serve, "svc", scope=parent)
            release.set()
    assert started == "svc:start-parent"
