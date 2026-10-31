"""Output pagination, global query scope, exact values, and bounded query collectors."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Literal

import anyio
import pytest

from fxtr.core import codec
from fxtr.core.entity_data import EntityID
from fxtr.entity_defns.array import ARRAY_TYPE, Array

pytest.importorskip("sqlalchemy")

from postgres_testing import a_client  # noqa: E402

from fxtr.runner.postgres.database import Database  # noqa: E402
from fxtr.runner.postgres.debug_web.contracts import Sort  # noqa: E402
from fxtr.runner.postgres.debug_web.output import PageQuery, decode_array  # noqa: E402
from fxtr.runner.postgres.debug_web.table_values import preview, search_text, summary  # noqa: E402


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["success", "cancel", "error"])
@pytest.mark.parametrize("retain", [True, False])
async def test_array_loads_coalesce_per_cid_and_release_waiters(
    database: Database, monkeypatch, outcome: str, retain: bool
):
    from fxtr.runner.postgres.debug_web import output

    async with a_client(database) as client:
        cids = [
            (await client.entities.store(Array.scalar(i, int))).entity_id.cid_str()
            for i in range(3)
        ]
        async with database.transaction() as tx:
            await client.entities.flush(tx)
        store = output.OutputStore(client)
        await store.load([cids[0]])
        if not retain:
            monkeypatch.setattr(output, "MAX_BYTES", 0)
        decoding, release = anyio.Event(), anyio.Event()
        decodes = 0

        async def gated_decode(function, blob):
            nonlocal decodes
            data, size = function(blob)
            if data["rows"][0][-1] == 1:
                decodes += 1
                if decodes == 1:
                    decoding.set()
                    await release.wait()
                    if outcome == "error":
                        raise ValueError("decode failed")
            return data, size

        monkeypatch.setattr(output, "run_sync", gated_decode)
        results = {}

        async def read(name, *, task_status=anyio.TASK_STATUS_IGNORED):
            with anyio.CancelScope() as scope:
                task_status.started(scope)
                try:
                    results[name] = (await store.load([cids[1]]))[cids[1]]
                except ValueError as error:
                    results[name] = str(error)

        try:
            with anyio.fail_after(5):
                async with anyio.create_task_group() as tasks:
                    owner = await tasks.start(read, "owner")
                    await decoding.wait()
                    await tasks.start(read, "waiter")
                    # Neither a cached array nor an unrelated cold array waits for this decode.
                    assert (await store.load([cids[0]]))[cids[0]]["rows"] == [[0]]
                    assert (await store.load([cids[2]]))[cids[2]]["rows"] == [[2]]
                    assert "waiter" not in results and decodes == 1
                    if outcome == "cancel":
                        owner.cancel()
                    else:
                        release.set()
            assert not store.inflight
            if outcome == "error":
                assert results == {"owner": "decode failed", "waiter": "decode failed"}
                assert (await store.load([cids[1]]))[cids[1]]["rows"] == [[1]]
            else:
                assert results["waiter"]["rows"] == [[1]]
                if outcome == "success":
                    assert results["owner"] is results["waiter"]
            assert decodes == (1 if outcome == "success" else 2)
            assert (cids[1] in store.arrays) == retain
            assert store.bytes == sum(size for _, size in store.arrays.values())
        finally:
            store.close()


@pytest.mark.parametrize(
    "fixture",
    json.loads(
        (Path(__file__).parents[2] / "frontend/test/fixtures/table-values.json").read_text()
    ),
    ids=lambda fixture: fixture["name"],
)
def test_browser_table_text_contract_and_exact_display_filter(fixture):
    value = codec.decode(base64.b64decode(fixture["cbor"]))
    assert summary(value) == fixture["summary"]
    assert preview(value) == fixture["preview"]
    assert search_text(value) == fixture["search"]
    query = PageQuery(
        0,
        25,
        {"value:": fixture["preview"] if isinstance(value, (list, dict)) else fixture["summary"]},
        None,
    )
    query.add([], {"dims": [], "schema": "object", "rows": [[value]]})
    assert query.result(True)["total"] == 1


def test_large_array_pages_filter_and_sort_globally():
    array = Array.from_items([((i,), i) for i in range(100_000)], ("[sample: int]", int))
    data, size = decode_array(codec.encode(array._fxtr_serialize()))
    assert size > 1_000_000
    query = PageQuery(0, 25, {}, None)
    query.add([], data)
    page = query.result(True)
    assert len(page["rows"]) == 25 and page["total"] == 100_000 and page["hasMore"]
    assert page["rows"][0]["key"] == [0]
    query = PageQuery(25, 25, {}, None)
    query.add([], data)
    assert query.result(True)["rows"][0]["key"] == [25]
    query = PageQuery(0, 25, {"value:": "99999"}, None)
    query.add([], data)
    page = query.result(True)
    assert page["total"] == 1 and page["rows"][0]["key"] == [99_999] and not page["hasMore"]
    query = PageQuery(25, 25, {}, {"key": "value:", "direction": "desc"})
    query.add([], data)
    page = query.result(True)
    assert len(query.heap) == 50
    assert [row["key"][0] for row in page["rows"]] == list(range(99_974, 99_949, -1))
    assert codec.decode(base64.b64decode(page["rows"][0]["value"])) == 99_974


def test_repeated_arrays_preserve_spawn_keys_and_numeric_precision():
    array = Array.from_items([(("x",), 2**60 + 1), (("y",), 2**60)], ("[inner: str]", int))
    data, _ = decode_array(codec.encode(array._fxtr_serialize()))
    query = PageQuery(
        0,
        25,
        {"key:outer": "b"},
        {"key": "value:", "direction": "asc"},
        {"dims": [["inner", "str"], ["outer", "str"]], "schema": "int"},
        ["outer"],
    )
    query.add(["a"], data)
    query.add(["b"], data)
    page = query.result(True)
    assert page["total"] == 2
    assert [row["key"] for row in page["rows"]] == [["y", "b"], ["x", "b"]]
    assert codec.decode(base64.b64decode(page["rows"][1]["value"])) == 2**60 + 1


def test_empty_arrays_and_unknown_totals_are_not_page_lengths():
    data, _ = decode_array(codec.encode(Array.from_items([], ("[x: int]", int))._fxtr_serialize()))
    query = PageQuery(
        0, 25, {}, None, {"dims": [["sample", "int"], ["x", "int"]], "schema": "int"}, ["sample"]
    )
    for i in range(40):
        query.add([i], data)
    assert not query.enough
    assert query.result(False)["total"] is None
    page = query.result(True)
    assert page["rows"] == [] and page["total"] == 0 and not page["hasMore"]


def test_large_nested_values_remain_cbor_and_are_searchable():
    reference = EntityID(cid=Array.scalar(1, int).content_id().cid)
    value = {"text": "a" * 100_000 + "needle", "nested": [None, True, reference, b"bytes"]}
    data, _ = decode_array(
        codec.encode({"$type": ARRAY_TYPE, "dims": [], "schema": "object", "rows": [[value]]})
    )
    query = PageQuery(0, 25, {"value:": "needle"}, None)
    query.add([], data)
    page = query.result(True)
    assert page["total"] == 1
    assert codec.decode(base64.b64decode(page["rows"][0]["value"])) == value


def test_sort_ties_remain_stable_across_arrays_and_pages():
    data, _ = decode_array(codec.encode(Array.scalar(1, int)._fxtr_serialize()))
    query = PageQuery(
        25,
        25,
        {},
        {"key": "value:", "direction": "desc"},
        {"dims": [["sample", "int"]], "schema": "int"},
        ["sample"],
    )
    for i in range(100):
        query.add([i], data)
    assert [row["key"][0] for row in query.result(True)["rows"]] == list(range(25, 50))


@pytest.mark.parametrize("total", [0, 1, 25, 26, 50, 63])
@pytest.mark.parametrize("direction", [None, "asc", "desc"])
@pytest.mark.parametrize("filtered", [False, True])
def test_last_pages_and_overshoots_align_with_filters_and_stable_sort_ties(
    total: int, direction: Literal["asc", "desc"] | None, filtered: bool
):
    filters = {"key:sample": "1"} if filtered else {}
    sort: Sort | None = {"key": "value:", "direction": direction} if direction else None
    expected = [i for i in range(total) if not filtered or "1" in str(i)]
    if direction:
        expected.sort(key=lambda i: i % 3, reverse=direction == "desc")
    start = max(0, (len(expected) - 1) // 25 * 25)
    for offset, last in [(0, True), (10_000, False)]:
        query = PageQuery(
            offset,
            25,
            filters,
            sort,
            {"dims": [["sample", "int"]], "schema": "int"},
            ["sample"],
            last=last,
        )
        for i in range(total):
            data, _ = decode_array(codec.encode(Array.scalar(i % 3, int)._fxtr_serialize()))
            query.add([i], data)
            assert not query.enough
            assert len(query.tail) <= 25
            if last:
                assert len(query.heap) <= 25
        page = query.result(True)
        assert page["offset"] == start
        assert page["total"] == len(expected) and not page["hasMore"]
        assert [row["key"][0] for row in page["rows"]] == expected[start:]


def test_last_page_of_large_array_retains_only_a_bounded_tail_or_sort_heap():
    data, _ = decode_array(
        codec.encode(
            Array.from_items(
                [((i,), i) for i in range(100_003)], ("[sample: int]", int)
            )._fxtr_serialize()
        )
    )
    for sort in [None, {"key": "value:", "direction": "asc"}]:
        query = PageQuery(0, 25, {}, sort, last=True)
        query.add([], data)
        page = query.result(True)
        assert page["offset"] == 100_000 and page["total"] == 100_003
        assert [row["key"][0] for row in page["rows"]] == [100_000, 100_001, 100_002]
        assert len(query.rows) <= 25 and len(query.tail) <= 25 and len(query.heap) <= 25
