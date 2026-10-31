"""Tests for the remote protocol's known-id tables: the host's giving and checking (with pending
worker stores) and the worker's local knowledge."""

from __future__ import annotations

import anyio
import pytest

from fxtr.core.entity_data import EntityID
from fxtr.entity_defns.array import Array, ArrayType
from fxtr.runner.experimental.remote.known_ids import HostKnownIds, WorkerKnownIds
from fxtr.runner.experimental.remote.wire import ProtocolError, WireError, WireValue, encode_value

pytestmark = pytest.mark.anyio

REFS = ArrayType.of(("[k: int]", EntityID))


def cid(n: int) -> bytes:
    return b"\x01\x71\x12\x20" + n.to_bytes(32, "big")


# ---- the host's table ----


async def test_check_accepts_given_ids_and_refuses_others() -> None:
    known = HostKnownIds()
    assert known.give(EntityID(cid(1))) == EntityID(cid(1))
    await known.check(EntityID(cid(1)))
    with pytest.raises(ProtocolError, match="neither given to nor stored by"):
        await known.check(EntityID(cid(2)))


async def test_each_attempt_has_its_own_table() -> None:
    first, second = HostKnownIds(), HostKnownIds()
    first.give(EntityID(cid(1)))
    with pytest.raises(ProtocolError):
        await second.check(EntityID(cid(1)))
    # An id from another attempt is accepted wherever this attempt knows its cid too.
    second.give(EntityID(cid(1)))
    await second.check(EntityID(cid(1)))


async def test_pending_stores_are_awaited_and_verified() -> None:
    known = HostKnownIds()
    known.expect(cid(5))
    assert known.pending == 1
    checked: list[EntityID] = []

    async def checker() -> None:
        await known.check(EntityID(cid(5)))
        checked.append(EntityID(cid(5)))

    async with anyio.create_task_group() as tg:
        tg.start_soon(checker)
        await anyio.wait_all_tasks_blocked()
        assert checked == []
        known.complete(cid(5), EntityID(cid(5)))
    assert checked == [EntityID(cid(5))]
    assert known.pending == 0
    # Storing a cid already known needs no pending entry.
    known.expect(cid(5))
    assert known.pending == 0
    # The worker's claimed cid must be what the host stored.
    known.expect(cid(6))
    with pytest.raises(ProtocolError, match="claimed cid"):
        known.complete(cid(6), EntityID(cid(7)))
    with pytest.raises(ProtocolError, match="claimed cid"):
        await known.check(EntityID(cid(6)))
    # A refused store fails every check, present and future.
    known.expect(cid(8))
    known.fail(cid(8), ValueError("no such reference"))
    with pytest.raises(ValueError, match="no such reference"):
        await known.check(EntityID(cid(8)))


async def test_a_second_store_of_a_pending_cid_joins_the_first() -> None:
    known = HostKnownIds()
    known.expect(cid(5))
    known.expect(cid(5))
    assert known.pending == 1
    checked: list[int] = []

    async def checker(n: int) -> None:
        await known.check(EntityID(cid(5)))
        checked.append(n)

    async with anyio.create_task_group() as tg:
        tg.start_soon(checker, 1)
        tg.start_soon(checker, 2)
        await anyio.wait_all_tasks_blocked()
        assert checked == []
        known.complete(cid(5), EntityID(cid(5)))
    assert sorted(checked) == [1, 2]
    # The second store's completion is a no-op.
    known.complete(cid(5), EntityID(cid(5)))
    assert known.pending == 0


async def test_values_and_arrays_are_checked_reference_by_reference() -> None:
    known = HostKnownIds()
    host_value = {"$type": "t", "refs": [EntityID(cid(1)), EntityID(cid(2))]}
    wire = known.give_value(host_value)
    assert await known.check_value(wire) == host_value
    # A stored id inside a value is accepted once its store completes.
    known.expect(cid(3))
    known.complete(cid(3), EntityID(cid(3)))
    from_worker = encode_value({"$type": "t", "refs": [EntityID(cid(1)), EntityID(cid(3))]})
    assert await known.check_value(from_worker) == {
        "$type": "t",
        "refs": [EntityID(cid(1)), EntityID(cid(3))],
    }
    with pytest.raises(ProtocolError):
        await known.check_value(encode_value({"$type": "t", "ref": EntityID(cid(4))}))
    with pytest.raises(WireError, match="Malformed"):
        await known.check_value(WireValue(b"\xff"))
    array = Array.from_items([((1,), EntityID(cid(9)))], REFS)
    wire_array = known.give_array(array)
    assert await known.check_array(wire_array) == array
    with pytest.raises(WireError, match="not an array"):
        await known.check_array(wire)


# ---- the worker's table ----


def test_worker_knows_what_it_received_and_stored() -> None:
    known = WorkerKnownIds()
    known.note(EntityID(cid(1)))
    known.note_value(encode_value({"$type": "t", "refs": [EntityID(cid(8)), EntityID(cid(9))]}))
    assert known.stored(cid(2)) == EntityID(cid(2))
    for n in (1, 2, 8, 9):
        assert known.is_known(EntityID(cid(n)))
    assert not known.is_known(EntityID(cid(7)))
