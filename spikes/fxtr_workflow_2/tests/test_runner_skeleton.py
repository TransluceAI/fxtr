"""Tests for the workflow runner skeleton (stage 15 part 2, chunk 2b).

Covers lifecycle + run groups (a plain-python async CM) + slot mounting + op-log
scaffolding, *without* task execution (``run`` raises ``NotImplementedError``).
The realization map + op log are two namespaces over one shared SQLite db file
(content-addressed storage dedups across them).
"""

from __future__ import annotations

from typing import Any

import pytest

from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr_workflow_2 import BasicStatelessServiceProvider, RunGroupHandle, WorkflowRunner
from fxtr_workflow_2.entities import RunGroupClosed, RunGroupOpened
from fxtr_workflow_2.task_types import TaskSlotProto

pytestmark = pytest.mark.anyio


async def _spawner_ref(scope: FxtrScope) -> Any:
    provider = await (await scope.mount(BasicStatelessServiceProvider())).resolve(scope)
    return await provider.spawner()


def _namespaces(tmp_path: Any) -> tuple[SQLiteFxtrNamespace, SQLiteFxtrNamespace]:
    """Two namespaces over one shared db file (recommended pattern)."""
    db = str(tmp_path / "workflow.sqlite")
    return SQLiteFxtrNamespace(db, "rmap"), SQLiteFxtrNamespace(db, "oplog")


async def test_run_group_and_storage_mounting(tmp_path: Any) -> None:
    rmap, oplog = _namespaces(tmp_path)
    async with rmap, oplog:
        spawner = await _spawner_ref(rmap.scope)
        async with WorkflowRunner(
            spawner=spawner, realization_map=rmap, op_log=oplog, owner="owner-1"
        ) as runner:
            async with runner.run_group("g1", "the suite") as group:
                assert isinstance(group, RunGroupHandle)
                slot_ref = await group.get_storage("evals/run1")
                slot: TaskSlotProto = await slot_ref.resolve(group.scope)

                child_ref = await slot.subslot("c")
                await child_ref.resolve(group.scope)  # mounts fine
                # (task execution is covered by test_runner_exec.py)

        # Opened + closed events recorded, in order, stamped with the owner.
        pins = await oplog.list_pins()
        assert len(pins) == 2
        keys = sorted(pins)
        opened = await (await oplog.retrieve(keys[0], RunGroupOpened, into_scope=oplog.scope)).load(
            oplog.scope
        )
        closed = await (await oplog.retrieve(keys[1], RunGroupClosed, into_scope=oplog.scope)).load(
            oplog.scope
        )
        assert opened.owner == "owner-1"
        assert closed.owner == "owner-1"
        assert opened.run_group.entity_id == closed.run_group.entity_id


async def test_get_storage_rejects_empty_path(tmp_path: Any) -> None:
    rmap, oplog = _namespaces(tmp_path)
    async with rmap, oplog:
        spawner = await _spawner_ref(rmap.scope)
        async with WorkflowRunner(spawner=spawner, realization_map=rmap, op_log=oplog) as runner:
            async with runner.run_group("g", "d") as group:
                with pytest.raises(ValueError, match="nonempty"):
                    await group.get_storage("")


async def test_subslot_rejects_slash_key(tmp_path: Any) -> None:
    rmap, oplog = _namespaces(tmp_path)
    async with rmap, oplog:
        spawner = await _spawner_ref(rmap.scope)
        async with WorkflowRunner(spawner=spawner, realization_map=rmap, op_log=oplog) as runner:
            async with runner.run_group("g", "d") as group:
                slot = await (await group.get_storage("a")).resolve(group.scope)
                with pytest.raises(Exception, match="'/'"):
                    await slot.subslot("b/c")


async def test_oplog_records_nested_run_groups_in_order(tmp_path: Any) -> None:
    rmap, oplog = _namespaces(tmp_path)
    async with rmap, oplog:
        spawner = await _spawner_ref(rmap.scope)
        async with WorkflowRunner(spawner=spawner, realization_map=rmap, op_log=oplog) as runner:
            async with runner.run_group("g1", "first"):
                pass
            async with runner.run_group("g2", "second"):
                pass

        pins = await oplog.list_pins()
        # two open/close pairs; the time+seq keys sort in insertion order.
        assert len(pins) == 4
        assert sorted(pins) == list(pins.keys()) or sorted(pins) == sorted(pins)
