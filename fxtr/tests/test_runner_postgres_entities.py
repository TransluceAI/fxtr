"""The Postgres entity store: buffering, flushing, what is known, and persistence."""

from __future__ import annotations

from typing import Any

import pytest

from fxtr.core import codec
from fxtr.core.entity_data import (
    EntityID,
    EntityNotFoundError,
    FxtrDataValue,
    InvalidEntityDataError,
    UnknownReferenceError,
)
from fxtr.entity_defns.array import Array
from fxtr.entity_defns.schema import IdSchema, optional, struct_of

pytest.importorskip("sqlalchemy")

from sqlalchemy import select  # noqa: E402

from fxtr.runner.postgres.database import Database  # noqa: E402
from fxtr.runner.postgres.entities import PostgresEntityStorage  # noqa: E402
from fxtr.runner.postgres.schema import entities  # noqa: E402

pytestmark = pytest.mark.anyio

NODE_TYPE = "test.fxtr.node"


def node(**fields: FxtrDataValue) -> dict[str, FxtrDataValue]:
    return {"$type": "fxtr.builtin.testing.node", **fields}


def id_of(value: FxtrDataValue) -> EntityID:
    return EntityID(cid=codec.compute_cid(codec.encode(value)))


async def test_store_load_before_flush(database: Database) -> None:
    store = PostgresEntityStorage(database)
    value = node(a=1, b=[2, 3], c=b"\x04")
    entity_id = await store.store_raw_data(value)
    assert store.is_known(entity_id)
    assert store.pending_count == 1
    assert await store.load_raw_data(entity_id) == value
    # Nothing has reached the database yet.
    other = PostgresEntityStorage(database)
    assert not other.is_known(entity_id)
    with pytest.raises(EntityNotFoundError):
        await other.load_raw_data(entity_id)


async def test_flush_persists_for_other_instances(database: Database) -> None:
    store = PostgresEntityStorage(database)
    value = node(x="persisted")
    entity_id = await store.store_raw_data(value)
    await store.flush()
    assert store.pending_count == 0
    assert store.is_known(entity_id)
    other = PostgresEntityStorage(database)
    assert await other.load_raw_data(entity_id) == value
    assert other.is_known(entity_id)
    await store.flush()  # nothing pending: a no-op


async def test_flush_is_idempotent_across_instances(database: Database) -> None:
    """Two instances storing the same content both flush without conflict."""
    value = node(same=True)
    first, second = PostgresEntityStorage(database), PostgresEntityStorage(database)
    assert await first.store_raw_data(value) == await second.store_raw_data(value)
    await first.flush()
    await second.flush()
    async with database.transaction() as conn:
        count = len((await conn.execute(select(entities.c.cid))).all())
    assert count == 1


async def test_unseen_entities_are_unknown_until_confirmed(database: Database) -> None:
    writer = PostgresEntityStorage(database)
    entity_id = await writer.store_raw_data(node(n=1))
    await writer.flush()
    reader = PostgresEntityStorage(database)
    assert not reader.is_known(entity_id)
    with pytest.raises(UnknownReferenceError):
        await reader.store_raw_data(node(ref=entity_id))
    absent = id_of(node(n="absent"))
    assert await reader.confirm_known([entity_id, absent]) == {entity_id}
    assert reader.is_known(entity_id)
    assert not reader.is_known(absent)
    await reader.store_raw_data(node(ref=entity_id))


async def test_loading_confirms_direct_references(database: Database) -> None:
    writer = PostgresEntityStorage(database)
    leaf_id = await writer.store_raw_data(node(leaf=True))
    parent_id = await writer.store_raw_data(node(child=leaf_id))
    await writer.flush()
    reader = PostgresEntityStorage(database)
    await reader.load_raw_data(parent_id)
    assert reader.is_known(leaf_id)
    await reader.store_raw_data(node(ref=leaf_id))


async def test_type_tag_column_holds_the_type_id(database: Database) -> None:
    store = PostgresEntityStorage(database)
    entity_id = await store.store_raw_data({"$type": NODE_TYPE, "v": 1})
    await store.flush()
    async with database.transaction() as conn:
        rows = (await conn.execute(select(entities.c.cid, entities.c.type_tag))).all()
    assert {bytes(row.cid): row.type_tag for row in rows} == {entity_id.cid: NODE_TYPE}
    other = PostgresEntityStorage(database)
    assert await other.load_raw_data(entity_id) == {"$type": NODE_TYPE, "v": 1}


async def test_store_validation(database: Database) -> None:
    store = PostgresEntityStorage(database)
    with pytest.raises(InvalidEntityDataError):
        await store.store_raw_data({"no": "tag"})
    # An id this store has never seen, built without storing anything.
    unknown = EntityID(cid=codec.compute_cid(codec.encode(node(v=1))))
    assert not store.is_known(unknown)
    with pytest.raises(UnknownReferenceError):
        await store.store_raw_data(node(ref=unknown))
    assert store.pending_count == 0  # a rejected store buffers nothing


async def test_typed_array_round_trip(database: Database) -> None:
    store = PostgresEntityStorage(database)
    array = Array.from_records(
        [{"prompt": 0, "text": "a"}, {"prompt": 1, "text": "b"}],
        ("[prompt: int]", struct_of(text=str)),
    )
    bound = await store.store(array)
    await store.flush()
    other = PostgresEntityStorage(database)
    assert await other.load(bound) == array
    assert await other.load(bound.entity_id, as_type=Array[Any]) == array


@pytest.mark.parametrize("entry", ["async", "sync"])
async def test_an_array_holding_only_arrow_is_stored_as_the_same_entity(
    database: Database, entry: str
) -> None:
    store = PostgresEntityStorage(database)
    referenced = await store.store_raw_data(node(v=1))
    array = Array.from_records(
        [{"prompt": 0, "text": "a", "ref": referenced}, {"prompt": 1, "text": "b", "ref": None}],
        ("[prompt: int]", struct_of(text=str, ref=optional(IdSchema()))),
    )
    built = Array._from_arrow(array._arrow_table(), array.type)

    bound = await store.store(built) if entry == "async" else store.store_sync(built)
    await store.flush()

    assert bound.entity_id == array.content_id()
    assert await PostgresEntityStorage(database).load(bound) == array


async def test_load_raw_bytes(database: Database) -> None:
    store = PostgresEntityStorage(database)
    value = node(a=1, b=[2, 3], c=b"\x04")
    entity_id = await store.store_raw_data(value)
    expected = codec.encode(value)
    # Served from the pending buffer before any flush.
    assert await store.load_raw_bytes(entity_id) == (expected, "fxtr.builtin.testing.node")
    await store.flush()
    # Served from the database for a fresh instance.
    other = PostgresEntityStorage(database)
    assert await other.load_raw_bytes(entity_id) == (expected, "fxtr.builtin.testing.node")
    assert other.is_known(entity_id)
    # Missing entities are None, not an error.
    assert await other.load_raw_bytes(id_of(node(n="absent"))) is None


async def test_load_raw_bytes_many_keeps_the_order_asked(database: Database) -> None:
    store = PostgresEntityStorage(database)
    flushed = node(where="database")
    flushed_id = await store.store_raw_data(flushed)
    await store.flush()
    pending = node(where="pending")
    pending_id = await store.store_raw_data(pending)
    absent_id = id_of(node(where="nowhere"))
    tag = "fxtr.builtin.testing.node"

    results = await store.load_raw_bytes_many([pending_id, absent_id, flushed_id, flushed_id])

    assert results == [
        (codec.encode(pending), tag),
        None,
        (codec.encode(flushed), tag),
        (codec.encode(flushed), tag),
    ]
    assert await store.load_raw_bytes_many([]) == []


async def test_flush_in_a_transaction_that_rolls_back_keeps_entities_pending(
    database: Database,
) -> None:
    store = PostgresEntityStorage(database)
    entity_id = await store.store_raw_data(node(kept="pending"))
    with pytest.raises(RuntimeError):
        async with database.transaction() as tx:
            await store.flush(tx)
            raise RuntimeError("abort")
    assert store.pending_count == 1
    assert store.is_known(entity_id)
    assert not PostgresEntityStorage(database).is_known(entity_id)
    # Confirming in a transaction defers the bookkeeping too.
    reader = PostgresEntityStorage(database)
    writer = PostgresEntityStorage(database)
    known_id = await writer.store_raw_data(node(kept="known"))
    await writer.flush()
    with pytest.raises(RuntimeError):
        async with database.transaction() as tx:
            assert await reader.confirm_known([known_id], tx) == {known_id}
            raise RuntimeError("abort")
    assert not reader.is_known(known_id)
    async with database.transaction() as tx:
        assert await reader.confirm_known([known_id], tx) == {known_id}
        assert not reader.is_known(known_id)
    assert reader.is_known(known_id)
    # A later flush lands it.
    async with database.transaction() as tx:
        await store.flush(tx)
        assert store.pending_count == 1  # bookkeeping waits for the commit
    assert store.pending_count == 0
    assert await PostgresEntityStorage(database).load_raw_data(entity_id) == node(kept="pending")
