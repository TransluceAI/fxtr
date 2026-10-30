"""Tests for task nesting via ``step_ctx.subslot`` (stage 15 part 3, chunk 3a).

A parent task fn runs a child task into a subslot of its own slot. Covers:
nested execution + `depends_on`, cache reuse of the whole tree, and a child
failure poisoning the parent (even when the parent catches it).
"""

from __future__ import annotations

import contextlib
import dataclasses
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity, EntityRef
from fxtr2.data import system_entities
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef, get_active_scope
from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method
from fxtr_workflow_2 import (
    BasicStatelessServiceProvider,
    WorkflowRunner,
    WorkflowTaskComplete,
    wrap_singleton,
)
from fxtr_workflow_2.task_types import TaskFnProto, TaskStepContextProto

pytestmark = pytest.mark.anyio

_NS = uuid.UUID("f0a1b2c3-d4e5-4f60-8a1b-2c3d4e5f6071")
_RESULT_TYPE_ID = system_entities.define_type(_NS, "result")
_CHILD_FN_TYPE_ID = system_entities.define_type(_NS, "child_fn")
_PARENT_FN_TYPE_ID = system_entities.define_type(_NS, "parent_fn")


@dataclasses.dataclass(frozen=True)
class _Result(DataclassEntity, fxtr_type=_RESULT_TYPE_ID):
    value: int


@dataclasses.dataclass(frozen=True)
class _Key(DataclassEntity, fxtr_type=system_entities.define_type(_NS, "key")):
    name: str


async def _load_result(scope: FxtrScope, ref: EntityRef[Any]) -> int:
    layered = scope.embed_as_provenance_parent(ref.entity_id)
    return (await EntityRef(entity_id=layered, target_type=_Result).load(scope)).value


class _ChildFn(TypedServiceImpl[TaskFnProto]):
    """Returns ``_Result(args[0] + 1)``; counts invocations. Raises if ``boom``."""

    def __init__(self, *, boom: bool = False) -> None:
        self.calls = 0
        self._boom = boom

    def _target_type_annotation(self) -> Any:
        return TaskFnProto

    def _service_type_id(self) -> Any:
        return _CHILD_FN_TYPE_ID

    @service_method
    async def run(
        self, step_ctx: ServiceRef[TaskStepContextProto], *args: Any, **kwargs: Any
    ) -> EntityRef[Any]:
        self.calls += 1
        if self._boom:
            raise RuntimeError("child boom")
        return _Result(value=args[0] + 1).store(get_active_scope())


class _ParentFn(TypedServiceImpl[TaskFnProto]):
    """Runs the child into subslot ``child`` and returns ``child_result + 10``.
    If ``catch``, swallows a child failure and returns 999 anyway."""

    def __init__(self, child_handle: EntityRef[Any], *, catch: bool = False) -> None:
        self.calls = 0
        self._child_handle = child_handle
        self._catch = catch

    def _target_type_annotation(self) -> Any:
        return TaskFnProto

    def _service_type_id(self) -> Any:
        return _PARENT_FN_TYPE_ID

    @service_method
    async def run(
        self, step_ctx: ServiceRef[TaskStepContextProto], *args: Any, **kwargs: Any
    ) -> EntityRef[Any]:
        self.calls += 1
        scope = get_active_scope()
        ctx = await step_ctx.resolve(scope)
        child_slot = await (await ctx.subslot("child")).resolve(scope)
        if self._catch:
            with contextlib.suppress(Exception):
                await child_slot.run(self._child_handle, args[0])
            return _Result(value=999).store(scope)
        child_ref = await child_slot.run(self._child_handle, args[0])
        return _Result(value=await _load_result(scope, child_ref) + 10).store(scope)


@contextlib.asynccontextmanager
async def _setup(
    tmp_path: Any, *, child_boom: bool = False, catch: bool = False
) -> AsyncGenerator[tuple[WorkflowRunner, EntityRef[Any], SQLiteFxtrNamespace, Any, Any]]:
    db = str(tmp_path / "wf.sqlite")
    async with (
        SQLiteFxtrNamespace(db, "rmap") as rmap,
        SQLiteFxtrNamespace(db, "oplog") as oplog,
    ):
        host = rmap.scope
        provider = await (await host.mount(BasicStatelessServiceProvider())).resolve(host)

        child_fn = _ChildFn(boom=child_boom)
        child_handle = await provider.register_external(
            _Key(name="child").store(host), await wrap_singleton(host, await host.mount(child_fn))
        )
        parent_fn = _ParentFn(child_handle, catch=catch)
        parent_handle = await provider.register_external(
            _Key(name="parent").store(host), await wrap_singleton(host, await host.mount(parent_fn))
        )
        spawner = await provider.spawner()
        async with WorkflowRunner(
            spawner=spawner, realization_map=rmap, op_log=oplog, owner="o"
        ) as runner:
            yield runner, parent_handle, rmap, child_fn, parent_fn


async def test_nested_subslot_executes_and_records_depends_on(tmp_path: Any) -> None:
    async with _setup(tmp_path) as (runner, parent_handle, rmap, child_fn, parent_fn):
        async with runner.run_group("g", "d") as group:
            slot = await (await group.get_storage("top")).resolve(group.scope)
            r = await slot.run(parent_handle, 5)
            assert await _load_result(group.scope, r) == 16  # (5+1) + 10
            assert child_fn.calls == 1
            assert parent_fn.calls == 1

        # Both slots pinned; the parent's depends_on points at the child slot.
        parent = await (
            await rmap.retrieve("top", WorkflowTaskComplete, into_scope=rmap.scope)
        ).load(rmap.scope)
        child_id = await rmap.retrieve("top/child", into_scope=rmap.scope)
        assert child_id is not None  # child slot pinned
        assert "top/child" in parent.depends_on
        assert parent.depends_on["top/child"].entity_id == child_id


async def test_nested_cache_reuse(tmp_path: Any) -> None:
    async with _setup(tmp_path) as (runner, parent_handle, rmap, child_fn, parent_fn):
        async with runner.run_group("g", "d") as group:
            slot = await (await group.get_storage("top")).resolve(group.scope)
            await slot.run(parent_handle, 5)
            r2 = await slot.run(parent_handle, 5)  # cache hit on the whole tree
            assert await _load_result(group.scope, r2) == 16
            assert parent_fn.calls == 1
            assert child_fn.calls == 1


async def test_child_failure_poisons_parent(tmp_path: Any) -> None:
    async with _setup(tmp_path, child_boom=True, catch=True) as (
        runner,
        parent_handle,
        rmap,
        _child_fn,
        _parent_fn,
    ):
        async with runner.run_group("g", "d") as group:
            slot = await (await group.get_storage("top")).resolve(group.scope)
            # The parent caught the child error and "returned", but it is poisoned.
            with pytest.raises(Exception, match="child_incomplete"):  # noqa: PT011
                await slot.run(parent_handle, 5)

        # Neither slot is pinned (child failed; parent poisoned).
        assert await rmap.try_retrieve("top", into_scope=rmap.scope) is None
        assert await rmap.try_retrieve("top/child", into_scope=rmap.scope) is None
