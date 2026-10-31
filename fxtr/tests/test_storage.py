"""Tests for the storage contract, MemoryStorage, and UnknownEntity."""

from __future__ import annotations

import pytest
from frozendict import frozendict

from fxtr.core import codec
from fxtr.core.entity_data import (
    EntityID,
    EntityNotFoundError,
    FxtrDataValue,
    InvalidEntityDataError,
    UnknownReferenceError,
)
from fxtr.entity_defns.unknown_entity import UnknownEntity
from fxtr.storage.memory_storage import MemoryStorage


@pytest.mark.anyio
async def test_store_then_load_roundtrip() -> None:
    store = MemoryStorage()
    value = {"$type": "fxtr.builtin.testing.node", "a": 1, "b": [2, 3], "c": b"\x04"}
    entity_id = await store.store_raw_data(value)
    assert await store.load_raw_data(entity_id) == value


@pytest.mark.anyio
async def test_stored_id_matches_computed_cid() -> None:
    store = MemoryStorage()
    value: FxtrDataValue = {"$type": "fxtr.builtin.testing.node", "x": [1, 2, 3]}
    entity_id = await store.store_raw_data(value)
    assert entity_id.cid == codec.compute_cid(codec.encode(value))


@pytest.mark.anyio
async def test_storing_same_data_dedups_to_same_id() -> None:
    store = MemoryStorage()
    first = await store.store_raw_data({"$type": "fxtr.builtin.testing.node", "same": True})
    second = await store.store_raw_data({"$type": "fxtr.builtin.testing.node", "same": True})
    assert first == second


@pytest.mark.anyio
async def test_is_known_transitions_on_store() -> None:
    store = MemoryStorage()
    value = {"$type": "fxtr.builtin.testing.node", "name": "absent"}
    absent = EntityID(cid=codec.compute_cid(codec.encode(value)))
    assert not store.is_known(absent)
    present = await store.store_raw_data(value)
    assert store.is_known(present)


@pytest.mark.anyio
async def test_store_rejects_untagged_data() -> None:
    store = MemoryStorage()
    with pytest.raises(InvalidEntityDataError):
        await store.store_raw_data({"no": "tag"})
    with pytest.raises(InvalidEntityDataError):
        await store.store_raw_data([1, 2, 3])
    with pytest.raises(InvalidEntityDataError):
        await store.store_raw_data("bare string")
    with pytest.raises(InvalidEntityDataError):
        await store.store_raw_data({"$type": 7})
    with pytest.raises(InvalidEntityDataError):
        await store.store_raw_data({"$type": ""})


@pytest.mark.anyio
async def test_store_accepts_known_reference() -> None:
    store = MemoryStorage()
    child = await store.store_raw_data({"$type": "fxtr.builtin.testing.node", "leaf": 1})
    parent = await store.store_raw_data({"$type": "fxtr.builtin.testing.node", "child": child})
    assert store.is_known(parent)


@pytest.mark.anyio
async def test_store_rejects_unknown_reference() -> None:
    store = MemoryStorage()
    dangling = EntityID(
        cid=codec.compute_cid(codec.encode({"$type": "fxtr.builtin.testing.node", "nope": 1}))
    )
    with pytest.raises(UnknownReferenceError):
        await store.store_raw_data({"$type": "fxtr.builtin.testing.node", "ref": dangling})


@pytest.mark.anyio
async def test_load_missing_raises() -> None:
    store = MemoryStorage()
    missing = EntityID(
        cid=codec.compute_cid(
            codec.encode({"$type": "fxtr.builtin.testing.node", "name": "missing"})
        )
    )
    with pytest.raises(EntityNotFoundError):
        await store.load_raw_data(missing)


@pytest.mark.anyio
async def test_frozen_data_roundtrip() -> None:
    store = MemoryStorage()
    frozen = frozendict(
        {"$type": "fxtr.builtin.testing.node", "a": (1, 2), "b": frozendict({"c": b"\x00"})}
    )
    entity_id = await store.store_raw_data(frozen)
    assert await store.load_raw_frozen_data(entity_id) == frozen


@pytest.mark.anyio
async def test_all_shapes_of_a_value_store_to_the_same_id() -> None:
    store = MemoryStorage()
    mutable = await store.store_raw_data(
        {"$type": "fxtr.builtin.testing.node", "a": [1, 2], "b": {"c": b"\x00"}}
    )
    frozen = await store.store_raw_data(
        frozendict(
            {"$type": "fxtr.builtin.testing.node", "a": (1, 2), "b": frozendict({"c": b"\x00"})}
        )
    )
    mixed = await store.store_raw_data(
        {"$type": "fxtr.builtin.testing.node", "a": (1, 2), "b": frozendict({"c": b"\x00"})}
    )
    assert mutable == frozen == mixed


@pytest.mark.anyio
async def test_unknown_entity_roundtrips_through_storage() -> None:
    store = MemoryStorage()
    entity = UnknownEntity(
        raw_data=frozendict({"$type": "fxtr.builtin.testing.node", "x": (1, 2, 3), "y": b"\x00"})
    )
    ref = await store.store(entity)
    loaded = await store.load(ref)
    assert loaded == entity


@pytest.mark.anyio
async def test_unknown_entity_store_load_store_is_stable() -> None:
    store = MemoryStorage()
    entity = UnknownEntity(
        raw_data=frozendict({"$type": "fxtr.builtin.testing.node", "hello": "world"})
    )
    ref = await store.store(entity)
    loaded = await store.load(ref)
    assert (await store.store(loaded)).entity_id == ref.entity_id


@pytest.mark.anyio
async def test_bind_type_unknown_entity_loads_arbitrary_id() -> None:
    store = MemoryStorage()
    entity_id = await store.store_raw_data(
        {"$type": "fxtr.builtin.testing.node", "arbitrary": [1, 2, 3]}
    )
    ref = entity_id.bind_type(UnknownEntity)
    loaded = await store.load(ref)
    assert loaded == UnknownEntity(
        raw_data=frozendict({"$type": "fxtr.builtin.testing.node", "arbitrary": (1, 2, 3)})
    )
