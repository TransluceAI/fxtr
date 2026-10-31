"""Project arrays: the registry, upserting and loading rows, deletion, versions, and the
entities an array references."""

from __future__ import annotations

from typing import Any

import pytest
from frozendict import frozendict
from postgres_testing import run_blocked

from fxtr.core import codec
from fxtr.core.entity_data import EntityID, UnknownReferenceError
from fxtr.entity_defns.array import Array
from fxtr.entity_defns.schema import IdSchema, list_of, optional, struct_of

pytest.importorskip("sqlalchemy")

from fxtr.runner.postgres.database import Database  # noqa: E402
from fxtr.runner.postgres.entities import PostgresEntityStorage  # noqa: E402
from fxtr.runner.postgres.project_arrays import (  # noqa: E402
    ProjectArrayEmptyError,
    ProjectArrayExistsError,
    ProjectArrayNotFoundError,
    ProjectArrays,
    ProjectArrayTypeError,
)

pytestmark = pytest.mark.anyio

NODE_TYPE = "test.fxtr.node"
PROMPTS = ("[prompt: int]", struct_of(text=str, score=optional(float)))


def prompts(*rows: tuple[int, str, float | None]) -> Array[Any]:
    return Array.from_records([{"prompt": p, "text": t, "score": s} for p, t, s in rows], PROMPTS)


@pytest.fixture
def entities(database: Database) -> PostgresEntityStorage:
    return PostgresEntityStorage(database)


@pytest.fixture
def arrays(database: Database, entities: PostgresEntityStorage) -> ProjectArrays:
    return ProjectArrays(database, entities)


async def test_create_describe_list_drop(arrays: ProjectArrays) -> None:
    info = await arrays.create_array("prompts", PROMPTS)
    assert (info.name, str(info.type), info.version, info.row_count) == (
        "prompts",
        "[prompt: int]: struct{score: float?, text: str}",
        0,
        0,
    )
    assert info.created_at == info.updated_at
    # Creating again with the same type is a no-op; with another type an error.
    assert await arrays.create_array("prompts", PROMPTS) == info
    with pytest.raises(ProjectArrayExistsError):
        await arrays.create_array("prompts", ("[prompt: int]", str))
    await arrays.create_array("other", str)
    assert [i.name for i in await arrays.list_arrays()] == ["other", "prompts"]
    assert await arrays.describe("other") == (await arrays.list_arrays())[0]
    await arrays.drop_array("other")
    assert [i.name for i in await arrays.list_arrays()] == ["prompts"]
    with pytest.raises(ProjectArrayNotFoundError):
        await arrays.describe("other")
    with pytest.raises(ProjectArrayNotFoundError):
        await arrays.drop_array("other")


async def test_upsert_merges_and_load_returns_the_declared_type(arrays: ProjectArrays) -> None:
    await arrays.create_array("prompts", PROMPTS)
    assert await arrays.load("prompts") == prompts()
    info = await arrays.upsert("prompts", prompts((0, "a", 1.0), (1, "b", None)))
    assert (info.version, info.row_count) == (1, 2)
    info = await arrays.upsert("prompts", prompts((1, "b2", 0.5), (2, "c", None)))
    assert (info.version, info.row_count) == (2, 3)
    assert await arrays.load("prompts") == prompts((0, "a", 1.0), (1, "b2", 0.5), (2, "c", None))
    # An array with a narrower (assignable) schema loads back as the declared type.
    narrow = Array.from_records(
        [{"prompt": 3, "text": "d", "score": 2.0}],
        ("[prompt: int]", struct_of(text=str, score=float)),
    )
    await arrays.upsert("prompts", narrow)
    loaded = await arrays.load("prompts")
    assert loaded.type == prompts().type
    assert loaded[(3,)] == frozendict({"text": "d", "score": 2.0})


async def test_upsert_matches_dims_by_name(arrays: ProjectArrays) -> None:
    await arrays.create_array("grid", ("[a: int, b: str]", int))
    transposed = Array.from_items([(("x", 1), 10), (("y", 2), 20)], ("[b: str, a: int]", int))
    await arrays.upsert("grid", transposed)
    assert await arrays.load("grid") == Array.from_items(
        [((1, "x"), 10), ((2, "y"), 20)], ("[a: int, b: str]", int)
    )


async def test_upsert_rejects_wrong_types_and_unknown_arrays(arrays: ProjectArrays) -> None:
    await arrays.create_array("prompts", PROMPTS)
    with pytest.raises(ProjectArrayTypeError):
        await arrays.upsert("prompts", Array.from_items([((0,), "a")], ("[prompt: int]", str)))
    with pytest.raises(ProjectArrayTypeError):
        await arrays.upsert(
            "prompts", await prompts((0, "a", None)).rename({"prompt": "p"}).collect()
        )
    with pytest.raises(ProjectArrayNotFoundError):
        await arrays.upsert("missing", prompts())
    assert (await arrays.describe("prompts")).version == 0


async def test_replace_delete_rows_and_clear(arrays: ProjectArrays) -> None:
    await arrays.create_array("prompts", PROMPTS)
    await arrays.upsert("prompts", prompts((0, "a", None), (1, "b", None), (2, "c", None)))
    info = await arrays.replace("prompts", prompts((1, "b", 1.0), (5, "f", None)))
    assert (info.version, info.row_count) == (2, 2)
    assert await arrays.load("prompts") == prompts((1, "b", 1.0), (5, "f", None))
    assert await arrays.delete_rows("prompts", [(1,), {"prompt": 5}, (9,)]) == 2
    assert await arrays.load("prompts") == prompts()
    assert await arrays.delete_rows("prompts", []) == 0
    await arrays.upsert("prompts", prompts((0, "a", None)))
    assert await arrays.clear("prompts") == 1
    info = await arrays.describe("prompts")
    assert (info.version, info.row_count) == (6, 0)
    assert info.updated_at > info.created_at


async def test_scalar_and_ragged_arrays(arrays: ProjectArrays) -> None:
    await arrays.create_array("n", int)
    with pytest.raises(ProjectArrayEmptyError):
        await arrays.load("n")
    with pytest.raises(ProjectArrayEmptyError):
        await arrays.snapshot("n")
    await arrays.upsert("n", Array.scalar(3, int))
    assert await arrays.load("n") == Array.scalar(3, int)
    await arrays.replace("n", Array.scalar(4, int))
    assert (await arrays.load("n")).item() == 4
    await arrays.create_array("samples", ("[prompt: int, sample: int]", list_of(bytes)))
    ragged = Array.from_items(
        [((0, 0), [b"\x00"]), ((0, 1), []), ((7, 0), [b"a", b"b"])],
        ("[prompt: int, sample: int]", list_of(bytes)),
    )
    await arrays.upsert("samples", ragged)
    assert await arrays.load("samples") == ragged


async def test_referenced_entities_are_persisted_with_the_rows(
    database: Database, entities: PostgresEntityStorage, arrays: ProjectArrays
) -> None:
    bio_type = ("[person: str]", IdSchema(NODE_TYPE))
    await arrays.create_array("bios", bio_type)
    bio = await entities.store_raw_data({"$type": NODE_TYPE, "name": "Ada"})
    assert entities.pending_count == 1
    await arrays.upsert("bios", Array.from_items([(("ada",), bio)], bio_type))
    assert entities.pending_count == 0
    # Another process sees both the row and the entity it references.
    fresh_entities = PostgresEntityStorage(database)
    loaded = await ProjectArrays(database, fresh_entities).load("bios")
    assert loaded == Array.from_items([(("ada",), bio)], bio_type)
    assert await fresh_entities.load_raw_data(loaded[("ada",)]) == {
        "$type": NODE_TYPE,
        "name": "Ada",
    }


async def test_unknown_references_are_rejected_before_writing(
    database: Database, arrays: ProjectArrays
) -> None:
    await arrays.create_array("refs", ("[k: int]", IdSchema()))
    absent = EntityID(cid=codec.compute_cid(codec.encode({"$type": "fxtr.builtin.testing.x"})))
    with pytest.raises(UnknownReferenceError):
        await arrays.upsert("refs", Array.from_items([((0,), absent)], ("[k: int]", IdSchema())))
    # The failed write rolled back entirely: no version bump, no rows.
    info = await arrays.describe("refs")
    assert (info.version, info.row_count) == (0, 0)
    # An entity written by another process is fine: the check consults the database.
    other = PostgresEntityStorage(database)
    present = await other.store_raw_data({"$type": "fxtr.builtin.testing.x"})
    await other.flush()
    await arrays.upsert("refs", Array.from_items([((0,), present)], ("[k: int]", IdSchema())))
    assert (await arrays.load("refs"))[(0,)] == present


async def test_loading_makes_the_arrays_ids_known(
    database: Database, entities: PostgresEntityStorage, arrays: ProjectArrays
) -> None:
    bio_type = ("[person: str]", IdSchema(NODE_TYPE))
    await arrays.create_array("bios", bio_type)
    bio = await entities.store_raw_data({"$type": NODE_TYPE, "name": "Ada"})
    await arrays.upsert("bios", Array.from_items([(("ada",), bio)], bio_type))
    fresh = PostgresEntityStorage(database)
    loaded = await ProjectArrays(database, fresh).load("bios")
    # The value ids are known without any load or check.
    assert fresh.is_known(loaded[("ada",)])
    await fresh.store_raw_data({"$type": "fxtr.builtin.testing.node", "ref": loaded[("ada",)]})
    await fresh.store(loaded)


async def test_snapshots(database: Database, arrays: ProjectArrays) -> None:
    await arrays.create_array("prompts", PROMPTS)
    await arrays.upsert("prompts", prompts((0, "a", None), (1, "b", 0.5)))
    first = await arrays.snapshot("prompts")
    assert (first.array_name, first.version) == ("prompts", 1)
    # The snapshot is an ordinary entity, loadable by anyone.
    other = PostgresEntityStorage(database)
    assert await other.load(first.array) == prompts((0, "a", None), (1, "b", 0.5))
    # Unchanged version: the same snapshot again, no new record.
    assert await arrays.snapshot("prompts") == first
    assert await arrays.list_snapshots("prompts") == [first]
    # A mutation makes the next snapshot new; reverting the content gives the same entity under
    # a new version.
    await arrays.upsert("prompts", prompts((2, "c", None)))
    second = await arrays.snapshot("prompts")
    assert second.version == 2 and second.array != first.array
    await arrays.delete_rows("prompts", [(2,)])
    third = await arrays.snapshot("prompts")
    assert third.version == 3 and third.array == first.array
    assert [s.version for s in await arrays.list_snapshots("prompts")] == [1, 2, 3]
    # Dropping the array drops the records but not the entities.
    await arrays.drop_array("prompts")
    with pytest.raises(ProjectArrayNotFoundError):
        await arrays.list_snapshots("prompts")
    assert await other.load(second.array) == prompts((0, "a", None), (1, "b", 0.5), (2, "c", None))


async def test_racing_snapshots_share_the_current_version(
    database: Database, arrays: ProjectArrays
) -> None:
    await arrays.create_array("prompts", PROMPTS)
    await arrays.upsert("prompts", prompts((0, "a", None), (1, "b", 0.5)))

    first_transaction = database.transaction()
    first_tx = await first_transaction.__aenter__()
    first = await arrays.snapshot("prompts", first_tx)

    async def take_second() -> Any:
        async with database.transaction() as second_tx:
            return await arrays.snapshot("prompts", second_tx)

    async def commit_first() -> None:
        await first_transaction.__aexit__(None, None, None)

    second = await run_blocked(take_second, commit_first)
    assert second == first
    assert await arrays.list_snapshots("prompts") == [first]


async def test_snapshot_of_a_array_of_references(
    database: Database, entities: PostgresEntityStorage, arrays: ProjectArrays
) -> None:
    bio_type = ("[person: str]", IdSchema(NODE_TYPE))
    await arrays.create_array("bios", bio_type)
    bio = await entities.store_raw_data({"$type": NODE_TYPE, "name": "Ada"})
    await arrays.upsert("bios", Array.from_items([(("ada",), bio)], bio_type))
    snap = await ProjectArrays(database, PostgresEntityStorage(database)).snapshot("bios")
    loaded = await PostgresEntityStorage(database).load(snap.array)
    assert loaded == Array.from_items([(("ada",), bio)], bio_type)
    assert await arrays.load("bios") == loaded
