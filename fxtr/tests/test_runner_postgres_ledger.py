"""The ledger's facts and validity rules on their own: expansion into children, step spawns, and
the result."""

from __future__ import annotations

import pytest
from frozendict import frozendict
from postgres_testing import (
    a_job,
    expand_children,
    start_root,
)

from fxtr.entity_defns.array import Array, ArrayType
from fxtr.runner import nodes

pytest.importorskip("sqlalchemy")

from fxtr.runner.postgres import execution, ledger  # noqa: E402
from fxtr.runner.postgres.database import Database  # noqa: E402
from fxtr.runner.postgres.entities import PostgresEntityStorage  # noqa: E402
from fxtr.runner.postgres.ledger import Emission, LedgerError  # noqa: E402

pytestmark = pytest.mark.anyio


async def test_list_jobs(database: Database) -> None:
    async with database.transaction() as tx:
        assert await ledger.list_jobs(tx) == []
    first = await a_job(database)
    second = await a_job(database)
    async with database.transaction() as tx:
        jobs = await ledger.list_jobs(tx)
    assert [j.id for j in jobs] == [second, first]  # newest first
    assert all(j.workflow_name == "wf" for j in jobs)


async def test_expanding_a_run_node_creates_children_once(database: Database) -> None:
    job_id = await a_job(database)
    claimed = await start_root(database, job_id)
    root = claimed.invocation
    run, created = await expand_children(database, claimed)
    async with database.transaction() as tx:
        spawned = await ledger.load_spawns(tx, root.id, run.content_id())
        parent = await ledger.find_parent(tx, created[0].id)
        child_nodes = await ledger.load_nodes(tx, created[0].id)
        # Expanding again is a no-op: the node already has spawns.
        active = await execution.guard(tx, claimed.attempt_id)
        assert await ledger.expand_run_node(tx, active, run.content_id(), []) == []
        assert len(await ledger.load_spawns(tx, root.id)) == 2
        assert await ledger.find_parent(tx, root.id) is None
    assert [s.key for s in spawned] == [(0,), (1,)]
    assert [s.child_invocation_id for s in spawned] == [c.id for c in created]
    assert [c.pathname for c in created] == ["root/child[k:0]", "root/child[k:1]"]
    assert all(c.workflow_name == "child_wf" and not c.finished for c in created)
    assert parent is not None and parent.invocation_id == root.id
    assert parent.node_identity == run.content_id() and parent.key == (0,)
    (input_node,) = child_nodes
    assert isinstance(input_node.node, nodes.Input) and input_node.node.name == "x"
    assert input_node.value is not None


async def test_step_spawns_record_the_keys_requested(database: Database) -> None:
    job_id = await a_job(database)
    claimed = await start_root(database, job_id)
    entities = PostgresEntityStorage(database)
    source = nodes.SourceArray(name="x", array=await entities.store(Array.scalar(1, int)))
    run = nodes.RunStep(
        name="s",
        step_name="s",
        inputs=frozendict({"x": source.content_id()}),
        map_over=(),
        join="exact",
        replicas=frozendict({"rep": (0, 1)}),
        cache_key_prefix="root/s",
        type=ArrayType.of(("[rep: int]", int)),
    )
    async with database.transaction() as tx:
        active = await execution.guard(tx, claimed.attempt_id)
        await entities.flush(tx)
        await ledger.record_emissions(
            tx, active, [Emission(source, ArrayType.of(int)), Emission(run, run.type)]
        )
        await ledger.record_step_spawns(
            tx, active, run.content_id(), {(0,): "root/s[rep:0]", (1,): "root/s[rep:1]"}
        )
        await ledger.record_step_spawns(
            tx, active, run.content_id(), {(0,): "elsewhere"}
        )  # a no-op
        spawned = await ledger.load_spawns(tx, claimed.invocation.id, run.content_id())
    assert [(s.key, s.step_cache_key, s.child_invocation_id) for s in spawned] == [
        ((0,), "root/s[rep:0]", None),
        ((1,), "root/s[rep:1]", None),
    ]


async def test_a_result_needs_a_return_and_is_set_once(database: Database) -> None:
    job_id = await a_job(database)
    claimed = await start_root(database, job_id)
    entities = PostgresEntityStorage(database)
    source = nodes.SourceArray(name="x", array=await entities.store(Array.scalar(1, int)))
    async with database.transaction() as tx:
        active = await execution.guard(tx, claimed.attempt_id)
        await entities.flush(tx)
        await ledger.record_emissions(tx, active, [Emission(source, ArrayType.of(int))])
        with pytest.raises(LedgerError):
            await ledger.record_result(tx, active, source.array)  # nothing returned yet
    async with database.transaction() as tx:
        active = await execution.guard(tx, claimed.attempt_id)
        await ledger.record_return(tx, active, source.content_id())
        await ledger.record_result(tx, active, source.array)
        with pytest.raises(LedgerError):
            await ledger.record_result(tx, active, source.array)  # set once
        invocation = await ledger.get_invocation(tx, claimed.invocation.id)
    assert invocation.finished and invocation.returned_node == source.content_id()
    assert invocation.result is not None and invocation.result.entity_id == source.array.entity_id
