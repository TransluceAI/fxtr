"""Tests for passing TaskStorage slots as task args (stage 15 part 3, chunk 3b).

A task fn receives a slot ``ServiceRef`` as an arg and runs a sub-task into it.
Covers: run into a passed slot + `depends_on`, path-based cache keying, run-group
consistency, cycle detection, and rejection of a foreign (non-slot) ServiceRef.
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
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method
from fxtr_workflow_2 import (
    BasicStatelessServiceProvider,
    WorkflowRunner,
    WorkflowTaskComplete,
    wrap_singleton,
)
from fxtr_workflow_2.task_types import TaskFnProto, TaskSlotProto, TaskStepContextProto

pytestmark = pytest.mark.anyio

_NS = uuid.UUID("a1c2e3f4-5061-4273-8495-a6b7c8d9e0f1")
_RESULT_TYPE_ID = system_entities.define_type(_NS, "result")
_CHILD_FN_TYPE_ID = system_entities.define_type(_NS, "child_fn")
_WRITER_FN_TYPE_ID = system_entities.define_type(_NS, "writer_fn")
_ECHO_TYPE_ID = system_entities.define_type(_NS, "echo")


@dataclasses.dataclass(frozen=True)
class _Result(DataclassEntity, fxtr_type=_RESULT_TYPE_ID):
    value: int


@dataclasses.dataclass(frozen=True)
class _Key(DataclassEntity, fxtr_type=system_entities.define_type(_NS, "key")):
    name: str


class _ChildFn(TypedServiceImpl[TaskFnProto]):
    """Returns ``_Result(args[0] + 1)``; counts invocations."""

    def __init__(self) -> None:
        self.calls = 0

    def _target_type_annotation(self) -> Any:
        return TaskFnProto

    def _service_type_id(self) -> Any:
        return _CHILD_FN_TYPE_ID

    @service_method
    async def run(
        self, step_ctx: ServiceRef[TaskStepContextProto], *args: Any, **kwargs: Any
    ) -> EntityRef[Any]:
        self.calls += 1
        return _Result(value=args[0] + 1).store(get_active_scope())


class _WriterFn(TypedServiceImpl[TaskFnProto]):
    """args: (storage_slot, n). Runs the child into the *passed* slot; counts."""

    def __init__(self, child_handle: EntityRef[Any]) -> None:
        self.calls = 0
        self._child_handle = child_handle

    def _target_type_annotation(self) -> Any:
        return TaskFnProto

    def _service_type_id(self) -> Any:
        return _WRITER_FN_TYPE_ID

    @service_method
    async def run(
        self, step_ctx: ServiceRef[TaskStepContextProto], *args: Any, **kwargs: Any
    ) -> EntityRef[Any]:
        self.calls += 1
        scope = get_active_scope()
        storage_slot = await args[0].replace_service_type(TaskSlotProto).resolve(scope)
        await storage_slot.run(self._child_handle, args[1])
        return _Result(value=0).store(scope)


@service_protocol
class _EchoProto:
    async def echo(self, x: str) -> str: ...


class _EchoImpl(TypedServiceImpl[_EchoProto]):
    def _target_type_annotation(self) -> Any:
        return _EchoProto

    def _service_type_id(self) -> Any:
        return _ECHO_TYPE_ID

    @service_method
    async def echo(self, x: str) -> str:
        return x


@contextlib.asynccontextmanager
async def _setup(
    tmp_path: Any,
) -> AsyncGenerator[tuple[WorkflowRunner, EntityRef[Any], FxtrScope, Any, Any]]:
    db = str(tmp_path / "wf.sqlite")
    async with (
        SQLiteFxtrNamespace(db, "rmap") as rmap,
        SQLiteFxtrNamespace(db, "oplog") as oplog,
    ):
        host = rmap.scope
        provider = await (await host.mount(BasicStatelessServiceProvider())).resolve(host)
        child_fn = _ChildFn()
        child_handle = await provider.register_external(
            _Key(name="child").store(host), await wrap_singleton(host, await host.mount(child_fn))
        )
        writer_fn = _WriterFn(child_handle)
        writer_handle = await provider.register_external(
            _Key(name="writer").store(host), await wrap_singleton(host, await host.mount(writer_fn))
        )
        spawner = await provider.spawner()
        async with WorkflowRunner(
            spawner=spawner, realization_map=rmap, op_log=oplog, owner="o"
        ) as runner:
            yield runner, writer_handle, host, rmap, (child_fn, writer_fn)


async def test_run_into_passed_slot_records_depends_on(tmp_path: Any) -> None:
    async with _setup(tmp_path) as (runner, writer_handle, host, rmap, (child_fn, writer_fn)):
        async with runner.run_group("g", "d") as group:
            data_slot = await group.get_storage("data")  # a slot ref, passed as an arg
            driver = await (await group.get_storage("driver")).resolve(group.scope)
            await driver.run(writer_handle, data_slot, 5)
            assert writer_fn.calls == 1
            assert child_fn.calls == 1

        # The child ran into the passed slot; the driver depends on it.
        data_complete = await (
            await rmap.retrieve("data", WorkflowTaskComplete, into_scope=rmap.scope)
        ).load(rmap.scope)
        layered = rmap.scope.embed_as_provenance_parent(data_complete.result)
        assert (await EntityRef(entity_id=layered, target_type=_Result).load(rmap.scope)).value == 6
        driver_complete = await (
            await rmap.retrieve("driver", WorkflowTaskComplete, into_scope=rmap.scope)
        ).load(rmap.scope)
        assert "data" in driver_complete.depends_on


async def test_storage_arg_cache_keys_by_path(tmp_path: Any) -> None:
    async with _setup(tmp_path) as (runner, writer_handle, host, rmap, (child_fn, writer_fn)):
        async with runner.run_group("g", "d") as group:
            data_slot = await group.get_storage("data")
            driver = await (await group.get_storage("driver")).resolve(group.scope)
            await driver.run(writer_handle, data_slot, 5)
            await driver.run(writer_handle, data_slot, 5)  # cache hit (same path + args)
            assert writer_fn.calls == 1
            assert child_fn.calls == 1


async def test_cross_group_arg_rejected(tmp_path: Any) -> None:
    # Decision 5 (a run's storage args share its run group) is enforced
    # structurally: sibling group scopes are isolated, so a group-A slot isn't
    # reachable to even encode as an arg in a group-B run — the service layer
    # rejects it ("not registered on home scope").
    async with _setup(tmp_path) as (runner, writer_handle, host, rmap, _fns):
        async with runner.run_group("A", "first") as ga, runner.run_group("B", "second") as gb:
            data_slot = await ga.get_storage("data")  # group A
            driver = await (await gb.get_storage("driver")).resolve(gb.scope)  # group B
            with pytest.raises(Exception, match="not registered on home scope"):  # noqa: PT011
                await driver.run(writer_handle, data_slot, 5)


async def test_cycle_detected(tmp_path: Any) -> None:
    async with _setup(tmp_path) as (runner, writer_handle, host, rmap, _fns):
        async with runner.run_group("g", "d") as group:
            driver_ref = await group.get_storage("driver")
            driver = await driver_ref.replace_service_type(TaskSlotProto).resolve(group.scope)
            # The writer runs into the very slot it is running in -> cycle.
            with pytest.raises(Exception, match="request chain"):  # noqa: PT011
                await driver.run(writer_handle, driver_ref, 5)


async def test_foreign_serviceref_rejected(tmp_path: Any) -> None:
    async with _setup(tmp_path) as (runner, writer_handle, host, rmap, _fns):
        echo_ref = await host.mount(_EchoImpl())  # not a runner slot
        async with runner.run_group("g", "d") as group:
            driver = await (await group.get_storage("driver")).resolve(group.scope)
            with pytest.raises(Exception, match="not a task-storage slot"):  # noqa: PT011
                await driver.run(writer_handle, echo_ref, 5)
