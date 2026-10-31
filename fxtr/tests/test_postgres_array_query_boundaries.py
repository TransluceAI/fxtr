"""Canonical references must remain usable when an Arrow-backed host is reopened."""

from __future__ import annotations

import pytest
from frozendict import frozendict

from fxtr.duckdb_engine.local_query import query_arrays
from fxtr.entity_defns.array import Array
from fxtr.entity_defns.query import JoinKey, dim, value
from fxtr.entity_defns.query_plan import (
    QueryJoin,
    QuerySelect,
    QuerySource,
)
from fxtr.entity_defns.schema import IdSchema, struct_of
from fxtr.runner.postgres.database import Database
from fxtr.runner.postgres.entities import PostgresEntityStorage


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["select", "join"])
async def test_entity_id_results_are_identical_each_time_the_host_reopens_storage(
    database: Database,
    operation: str,
) -> None:
    seed = PostgresEntityStorage(database)
    ticket = await seed.store_raw_data({"$type": "tests.SupportTicket", "ticket": "TKT-1842"})
    source = await seed.store(Array.from_items([((1842,), ticket)], ("[ticket: int]", IdSchema())))
    await seed.flush()
    if operation == "join":
        keys = (JoinKey(dim("ticket"), dim("ticket")),)
        query = QueryJoin(QuerySource(0), QuerySource(1), keys, "reported", "verified")
        expected = Array.from_items(
            [((1842,), {"reported": ticket, "verified": ticket})],
            ("[ticket: int]", struct_of(reported=IdSchema(), verified=IdSchema())),
        )
    else:
        query = QuerySelect(QuerySource(0), frozendict({"evidence": value()}))
        expected = Array.from_items(
            [((1842,), {"evidence": ticket})], ("[ticket: int]", struct_of(evidence=IdSchema()))
        )
    result_ids = []
    for _ in range(3):
        attempt_storage = PostgresEntityStorage(database)
        attempt_storage.assume_known([source.entity_id])
        assert not attempt_storage.is_known(ticket)
        # As a workflow's query node runs: its input loaded, the query run locally, the result
        # stored.
        loaded_input = await attempt_storage.load(source)
        assert attempt_storage.is_known(ticket)  # loading the input confirmed what it references
        inputs = [loaded_input, loaded_input] if operation == "join" else [loaded_input]
        result = query_arrays(query, inputs)
        reference = await attempt_storage.store(result)
        await attempt_storage.flush()
        result_ids.append(reference.entity_id.cid)
        assert await PostgresEntityStorage(database).load(reference) == expected
    assert len(set(result_ids)) == 1
