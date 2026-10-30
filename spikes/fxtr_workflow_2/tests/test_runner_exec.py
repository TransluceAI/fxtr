"""Tests for the workflow runner execution core (stage 15 part 2, chunk 2c).

A task function is registered as an external service on a
``BasicStatelessServiceProvider``; the runner spawns + invokes it. Covers
execute + cache hit, cache-key conflict, task failure (+ retry), concurrent
joins onto one job, and op-log events.
"""

from __future__ import annotations

import contextlib
import dataclasses
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import anyio
import pytest

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity, EntityRef
from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef, get_active_scope
from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method
from fxtr_workflow_2 import BasicStatelessServiceProvider, WorkflowRunner, wrap_singleton
from fxtr_workflow_2.entities import RunCompleted, RunStarted
from fxtr_workflow_2.task_types import TaskFnProto, TaskStepContextProto

pytestmark = pytest.mark.anyio

_TEST_NS = uuid.UUID("d31b7e90-4c25-4a6f-8b13-9e0a2c5f7d48")
_RESULT_TYPE_ID = system_entities.define_type(_TEST_NS, "result")
_ADD_FN_TYPE_ID = system_entities.define_type(_TEST_NS, "add_one_fn")
_BOOM_FN_TYPE_ID = system_entities.define_type(_TEST_NS, "boom_fn")


@dataclasses.dataclass(frozen=True)
class _Result(DataclassEntity, fxtr_type=_RESULT_TYPE_ID):
    value: int


@dataclasses.dataclass(frozen=True)
class _Key(DataclassEntity, fxtr_type=system_entities.define_type(_TEST_NS, "key")):
    name: str


class _AddOneFn(TypedServiceImpl[TaskFnProto]):
    """Task fn: returns ``_Result(args[0] + 1)``; counts invocations. If
    ``gate`` is set, blocks on it (to force concurrent joins onto one job)."""

    def __init__(self, gate: anyio.Event | None = None) -> None:
        self.calls = 0
        self._gate = gate

    def _target_type_annotation(self) -> Any:
        return TaskFnProto

    def _service_type_id(self) -> Any:
        return _ADD_FN_TYPE_ID

    @service_method
    async def run(
        self, step_ctx: ServiceRef[TaskStepContextProto], *args: Any, **kwargs: Any
    ) -> EntityRef[Any]:
        self.calls += 1
        scope = get_active_scope()
        ctx = await step_ctx.resolve(scope)
        await ctx.log(f"adding one to {args[0]}")
        if self._gate is not None:
            await self._gate.wait()
        return _Result(value=args[0] + 1).store(scope)


class _BoomFn(TypedServiceImpl[TaskFnProto]):
    """Task fn that always raises."""

    def __init__(self) -> None:
        self.calls = 0

    def _target_type_annotation(self) -> Any:
        return TaskFnProto

    def _service_type_id(self) -> Any:
        return _BOOM_FN_TYPE_ID

    @service_method
    async def run(
        self, step_ctx: ServiceRef[TaskStepContextProto], *args: Any, **kwargs: Any
    ) -> EntityRef[Any]:
        self.calls += 1
        raise RuntimeError("boom")


@contextlib.asynccontextmanager
async def _runner_with_fn(
    tmp_path: Any, fn_impl: TypedServiceImpl[TaskFnProto], *, key_name: str = "fn"
) -> AsyncGenerator[tuple[WorkflowRunner, EntityRef[UnknownEntity], SQLiteFxtrNamespace]]:
    db = str(tmp_path / "wf.sqlite")
    async with (
        SQLiteFxtrNamespace(db, "rmap") as rmap,
        SQLiteFxtrNamespace(db, "oplog") as oplog,
    ):
        host = rmap.scope  # outlives the runner; included by the runner scope
        provider = await (await host.mount(BasicStatelessServiceProvider())).resolve(host)
        fn_ref = await host.mount(fn_impl)
        connector = await wrap_singleton(host, fn_ref)
        key = _Key(name=key_name).store(host)
        handle = await provider.register_external(key, connector)
        spawner = await provider.spawner()
        async with WorkflowRunner(
            spawner=spawner, realization_map=rmap, op_log=oplog, owner="o"
        ) as runner:
            yield runner, handle, oplog


async def _result_value(scope: FxtrScope, result_ref: EntityRef[Any]) -> int:
    # run returns the stored provenance-layer node; re-attach it to load the data.
    layered = scope.embed_as_provenance_parent(result_ref.entity_id)
    return (await EntityRef(entity_id=layered, target_type=_Result).load(scope)).value


async def test_executes_and_caches(tmp_path: Any) -> None:
    fn = _AddOneFn()
    async with _runner_with_fn(tmp_path, fn) as (runner, handle, oplog):
        async with runner.run_group("g", "d") as group:
            slot = await (await group.get_storage("evals/a")).resolve(group.scope)
            r1 = await slot.run(handle, 5)
            assert await _result_value(group.scope, r1) == 6
            assert fn.calls == 1

            # Same args -> cache hit: identical result, fn not re-run.
            r2 = await slot.run(handle, 5)
            assert r2.entity_id == r1.entity_id
            assert fn.calls == 1

        pins = await oplog.list_pins()
        # opened + (started+completed) for the run + (started+completed) for the cache hit + closed
        assert len(pins) == 6


async def test_cache_key_conflict(tmp_path: Any) -> None:
    fn = _AddOneFn()
    async with _runner_with_fn(tmp_path, fn) as (runner, handle, _oplog):
        async with runner.run_group("g", "d") as group:
            slot = await (await group.get_storage("slot")).resolve(group.scope)
            await slot.run(handle, 5)
            with pytest.raises(Exception, match="different cache key"):
                await slot.run(handle, 6)


async def test_task_failure_then_retry(tmp_path: Any) -> None:
    fn = _BoomFn()
    async with _runner_with_fn(tmp_path, fn) as (runner, handle, oplog):
        async with runner.run_group("g", "d") as group:
            slot = await (await group.get_storage("slot")).resolve(group.scope)
            with pytest.raises(Exception, match="boom"):
                await slot.run(handle, 1)
            assert fn.calls == 1
            # Failure is not pinned -> re-run starts a fresh job.
            with pytest.raises(Exception, match="boom"):
                await slot.run(handle, 1)
            assert fn.calls == 2

        keys = sorted(await oplog.list_pins())
        # opened, started(r1), completed(r1, failed), started(r2), completed(r2, failed), closed
        completed = await (
            await oplog.retrieve(keys[2], RunCompleted, into_scope=oplog.scope)
        ).load(oplog.scope)
        assert completed.outcome == "failed"
        # the op log keeps the full traceback, not just the message
        assert completed.reason is not None
        assert "boom" in completed.reason
        assert "Traceback" in completed.reason


async def test_concurrent_runs_join_one_job(tmp_path: Any) -> None:
    gate = anyio.Event()
    fn = _AddOneFn(gate=gate)
    async with _runner_with_fn(tmp_path, fn) as (runner, handle, _oplog):
        async with runner.run_group("g", "d") as group:
            slot = await (await group.get_storage("slot")).resolve(group.scope)
            results: list[EntityRef[Any]] = []

            async def _do() -> None:
                results.append(await slot.run(handle, 10))

            async with anyio.create_task_group() as tg:
                for _ in range(4):
                    tg.start_soon(_do)
                await anyio.sleep(0.05)  # let all 4 park on the one job
                gate.set()

            assert fn.calls == 1
            assert {r.entity_id for r in results} == {results[0].entity_id}
            assert await _result_value(group.scope, results[0]) == 11


async def test_cross_group_symmetric_started_completed(tmp_path: Any) -> None:
    """Two run groups race the same slot (B cache-hits A's result). Each group
    records its own started+completed pair; only A's started has launched_job."""
    fn = _AddOneFn()
    async with _runner_with_fn(tmp_path, fn) as (runner, handle, oplog):
        async with runner.run_group("A", "first") as ga:
            await (await (await ga.get_storage("shared")).resolve(ga.scope)).run(handle, 5)
        async with runner.run_group("B", "second") as gb:
            await (await (await gb.get_storage("shared")).resolve(gb.scope)).run(handle, 5)

        assert fn.calls == 1  # B cache-hit A's result
        keys = sorted(await oplog.list_pins())
        # 2 groups x (opened, started, completed, closed)
        assert len(keys) == 8
        started_a = await (await oplog.retrieve(keys[1], RunStarted, into_scope=oplog.scope)).load(
            oplog.scope
        )
        started_b = await (await oplog.retrieve(keys[5], RunStarted, into_scope=oplog.scope)).load(
            oplog.scope
        )
        assert started_a.launched_job is True  # A started the physical job
        assert started_b.launched_job is False  # B observed the cached result
        completed_a = await (
            await oplog.retrieve(keys[2], RunCompleted, into_scope=oplog.scope)
        ).load(oplog.scope)
        completed_b = await (
            await oplog.retrieve(keys[6], RunCompleted, into_scope=oplog.scope)
        ).load(oplog.scope)
        assert completed_a.outcome == "success"
        assert completed_b.outcome == "success"


async def test_conflict_records_conflict_outcome(tmp_path: Any) -> None:
    fn = _AddOneFn()
    async with _runner_with_fn(tmp_path, fn) as (runner, handle, oplog):
        async with runner.run_group("g", "d") as group:
            slot = await (await group.get_storage("slot")).resolve(group.scope)
            await slot.run(handle, 5)
            with pytest.raises(Exception, match="different cache key"):  # noqa: PT011
                await slot.run(handle, 6)

        keys = sorted(await oplog.list_pins())
        # opened, started(r1), completed(r1), started(r2), completed(r2,conflict), closed
        completed_conflict = await (
            await oplog.retrieve(keys[4], RunCompleted, into_scope=oplog.scope)
        ).load(oplog.scope)
        assert completed_conflict.outcome == "conflict"


async def test_full_runner_cancel_records_interrupted(tmp_path: Any) -> None:
    gate = anyio.Event()  # never set -> the job blocks until cancellation
    fn = _AddOneFn(gate=gate)
    db = str(tmp_path / "wf.sqlite")
    async with (
        SQLiteFxtrNamespace(db, "rmap") as rmap,
        SQLiteFxtrNamespace(db, "oplog") as oplog,
    ):
        host = rmap.scope
        provider = await (await host.mount(BasicStatelessServiceProvider())).resolve(host)
        fn_ref = await host.mount(fn)
        connector = await wrap_singleton(host, fn_ref)
        handle = await provider.register_external(_Key(name="fn").store(host), connector)
        spawner = await provider.spawner()

        with anyio.move_on_after(0.3):
            async with WorkflowRunner(
                spawner=spawner, realization_map=rmap, op_log=oplog, owner="o"
            ) as runner:
                async with runner.run_group("g", "d") as group:
                    slot = await (await group.get_storage("slot")).resolve(group.scope)
                    await slot.run(handle, 5)  # parks; cancelled by move_on_after

        # The task fn is mounted on the outer (rmap) scope, so cancelling the
        # runner block doesn't reach its in-flight dispatch — release the gate so
        # that lingering dispatch can finish and rmap can close.
        gate.set()

        # A RunCompleted(interrupted) was recorded for the cancelled run.
        outcomes = []
        for key in sorted(await oplog.list_pins()):
            with contextlib.suppress(Exception):
                ev = await (await oplog.retrieve(key, RunCompleted, into_scope=oplog.scope)).load(
                    oplog.scope
                )
                outcomes.append(ev.outcome)
        assert "interrupted" in outcomes
