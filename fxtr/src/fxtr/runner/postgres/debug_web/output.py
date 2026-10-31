"""Current output pages and full-dataset queries, with a bounded immutable-array cache.

Result references are read in batches for each request. Plain pages stop as soon as enough
rows are found; filters and sorts scan the selected output. Only page values cross the wire.
Arrays remain indivisible CBOR blobs, so cold access still decodes each needed array in full.
A dim's value list is merged from a bounded cache of per-array summaries, so searching and paging
it, and reading it again as a run gains outputs, decode only arrays not summarized yet (an array
source still loads its one array to open it).
"""

from __future__ import annotations

import base64
import heapq
import json
import sys
import uuid
from collections import OrderedDict, deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass, field
from functools import cmp_to_key
from typing import Any, cast

import anyio
from anyio.to_thread import run_sync
from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import JSONB

from fxtr.core import codec
from fxtr.core.entity_data import EntityID, cid_from_str, cid_to_str
from fxtr.entity_defns.array import ARRAY_TYPE
from fxtr.runner.postgres.client import LocalProjectClient
from fxtr.runner.postgres.ledger import JobID
from fxtr.runner.postgres.schema import entities

from .browse import KEY_LIST_LIMIT, page_offset, page_size, run_context, selected, spawn_dict
from .contracts import (
    DimValuePage,
    InvocationPage,
    InvocationRow,
    OutputPage,
    OutputSource,
    Sort,
)
from .table_values import compare_cells, search_text

MAX_ARRAYS = 128
MAX_BYTES = 256 * 1024 * 1024
BATCH_SIZE = 32
# Result references read per query when their arrays are mostly summarized already.
REF_BATCH_SIZE = 1024
# Distinct values held across every cached dim summary.
MAX_SUMMARY_VALUES = 1_000_000


def decode_array(blob: bytes) -> tuple[dict[str, Any], int]:
    raw = codec.decode(blob)
    if not isinstance(raw, Mapping) or raw.get("$type") != ARRAY_TYPE:
        raise ValueError("output is not an Array")
    # Count the decoded object graph, including keys and container overhead, not just CBOR size.
    pending: list[Any] = [raw]
    seen, size = set(), 0
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        size += sys.getsizeof(value)
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)
        elif isinstance(value, EntityID):
            pending.append(value.cid)
    return cast(dict[str, Any], raw), size


@dataclass
class RankedRow:
    rank: Any
    ordinal: int
    key: list[Any]
    value: Any
    reverse: bool = False

    def __lt__(self, other: RankedRow) -> bool:
        # The worst retained row is the heap root, so later rows can cheaply displace it.
        if self.reverse:
            return (self.rank, self.ordinal) < (other.rank, other.ordinal)
        return (self.rank, self.ordinal) > (other.rank, other.ordinal)


def same_key(a: Any, b: Any) -> bool:
    """Key equality that keeps `1` and `"1"` (and `True` and `1`) apart."""
    return type(a) is type(b) and a == b


def split_selection(
    selection: dict[str, Any], mapped_dims: list[str], output_dims: list[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A selection as the invocation keys it picks (filtered in SQL) and the keys of dims produced
    inside one invocation (filtered row by row)."""
    invocation, rows = {}, {}
    for name, value in selection.items():
        if type(value) not in (str, int):
            raise ValueError("invalid dimension selection")
        if name in mapped_dims:
            invocation[name] = value
        elif name in output_dims:
            rows[name] = value
        else:
            raise ValueError("invalid dimension selection")
    return invocation, rows


def value_cells(schema: Any, value: Any) -> dict[str, Any]:
    """A row's value as table cells: a struct's fields (`value:<field>`, all null for a null
    struct), or the one anonymous cell (`value:`)."""
    if isinstance(schema, Mapping) and "nullable" in schema:
        schema = schema["nullable"]
    fields = schema.get("struct") if isinstance(schema, Mapping) else None
    if fields is None:
        return {"value:": value}
    return {
        f"value:{name}": value.get(name) if isinstance(value, Mapping) else None for name in fields
    }


def keyed_rows(prefix: dict[str, Any], data: dict[str, Any], keys: dict[str, Any]):
    """Each row of an invocation's array with its keys by dim name, the invocation's key first,
    keeping only the rows whose keys match `keys`."""
    dims = [name for name, _ in data["dims"]]
    if any(name not in prefix and name not in dims for name in keys):
        raise ValueError("invalid dimension selection")
    for row in data["rows"]:
        by_name = prefix | dict(zip(dims, row[:-1], strict=True))
        if all(same_key(by_name[name], value) for name, value in keys.items()):
            yield by_name, row


@dataclass(frozen=True)
class DimSummary:
    """One array's distinct values of a dim, by JSON token, and the tokens of the values some row
    has alongside the keys picked for the other dims."""

    values: dict[str, Any]
    compatible: frozenset[str]


def summarize(
    prefix: dict[str, Any], data: dict[str, Any], dim: str, others: dict[str, Any]
) -> DimSummary:
    values: dict[str, Any] = {}
    compatible: set[str] = set()
    for by_name, _ in keyed_rows(prefix, data, {}):
        value = by_name[dim]
        token = json.dumps(value)
        values.setdefault(token, value)
        if token not in compatible and all(
            same_key(by_name[name], key) for name, key in others.items()
        ):
            compatible.add(token)
    return DimSummary(values, frozenset(compatible))


class PageQuery:
    """One request's collector. Plain pages retain displayed values and a bounded tail."""

    def __init__(
        self,
        offset: int,
        first: int,
        filters: dict[str, str],
        sort: Sort | None,
        output_type: dict[str, Any] | None = None,
        mapped_dims: list[str] | None = None,
        last: bool = False,
        keys: dict[str, Any] | None = None,
    ):
        self.offset, self.first = offset, first
        self.last = last
        self.filters = {key: text.strip().lower() for key, text in filters.items() if text.strip()}
        self.sort = sort
        self.type = output_type
        self.mapped_dims = mapped_dims or []
        # Exact keys of dims produced inside an invocation; rows with other keys are left out.
        self.keys = keys or {}
        self.count = 0
        self.rows: list[tuple[list[Any], Any]] = []
        self.tail: deque[tuple[list[Any], Any]] = deque(maxlen=first)
        self.heap: list[RankedRow] = []
        direction = -1 if sort and sort["direction"] == "desc" else 1
        self.rank = cmp_to_key(lambda a, b: direction * compare_cells(a, b))

    @property
    def enough(self) -> bool:
        return (
            not self.last
            and not self.filters
            and not self.sort
            and self.count > self.offset + self.first
        )

    def add(self, spawn_key: list[Any], data: dict[str, Any]):
        if self.type is None:
            self.type = {"dims": data["dims"], "schema": data["schema"]}
        dims = [name for name, _ in data["dims"]]
        output_dims = [name for name, _ in self.type["dims"]]
        prefix = dict(zip(self.mapped_dims, spawn_key, strict=True))
        if not self.filters and not self.sort and not self.keys:
            # Only the requested slice and a one-page tail need per-row work.
            rows = data["rows"]
            start = max(0, self.offset - self.count)
            end = max(0, self.offset + self.first - self.count)
            self.count += len(rows)
            for row in rows[-self.first :]:
                by_name = prefix | dict(zip(dims, row[:-1], strict=True))
                self.tail.append(([by_name[name] for name in output_dims], row[-1]))
            for row in rows[start:end]:
                by_name = prefix | dict(zip(dims, row[:-1], strict=True))
                self.rows.append(([by_name[name] for name in output_dims], row[-1]))
            return
        for by_name, row in keyed_rows(prefix, data, self.keys):
            key = [by_name[name] for name in output_dims]
            value = row[-1]
            cells: dict[str, Any] = {}
            if self.filters or self.sort:
                cells = {f"key:{name}": by_name[name] for name in output_dims}
                cells.update(value_cells(data["schema"], value))
                if not all(
                    needle in search_text(cells.get(name)) for name, needle in self.filters.items()
                ):
                    continue
            ordinal = self.count
            self.count += 1
            if self.sort:
                candidate = RankedRow(
                    self.rank(cells.get(self.sort["key"])),
                    ordinal,
                    key,
                    value,
                    reverse=self.last,
                )
                if len(self.heap) < (self.first if self.last else self.offset + self.first):
                    heapq.heappush(self.heap, candidate)
                else:
                    heapq.heappushpop(self.heap, candidate)
                continue
            self.tail.append((key, value))
            if self.offset <= ordinal < self.offset + self.first:
                self.rows.append((key, value))

    def result(self, complete: bool) -> OutputPage:
        rows = self.rows
        offset = self.offset
        at_end = complete and (self.last or offset >= self.count)
        if at_end:
            offset = max(0, (self.count - 1) // self.first * self.first)
            rows = list(self.tail)[-(self.count - offset) :] if self.count else []
        if self.sort:
            ordered = sorted(self.heap, key=lambda row: (row.rank, row.ordinal))
            chosen = (
                ordered[-(self.count - offset) :] if self.last and self.count else ordered[offset:]
            )
            rows = [(row.key, row.value) for row in chosen]
        return {
            "type": self.type,
            "rows": [
                {"key": key, "value": base64.b64encode(codec.encode(value)).decode()}
                for key, value in rows
            ],
            "total": self.count if complete else None,
            "offset": offset,
            "hasMore": self.count > offset + self.first or not complete,
        }


@dataclass
class Scan:
    """Where a source's arrays come from: one stored array (`cid`), or the results of a run's
    invocations (`targets`, keyed by `mapped_dims`). `type` is the type of the source's rows."""

    cid: str | None
    targets: Any
    mapped_dims: list[str]
    type: dict[str, Any]

    @property
    def output_dims(self) -> list[str]:
        return [name for name, _ in self.type["dims"]]


@dataclass
class ArrayLoad:
    """One in-flight CID. An unset result after completion means its owner was cancelled."""

    done: anyio.Event = field(default_factory=anyio.Event)
    data: dict[str, Any] | None = None
    error: Exception | None = None


class OutputStore:
    """Caches only immutable decoded arrays. Eviction is invisible to page clients."""

    def __init__(self, client: LocalProjectClient):
        self.client = client
        self.arrays: OrderedDict[str, tuple[dict[str, Any], int]] = OrderedDict()
        self.bytes = 0
        self.limiter = anyio.CapacityLimiter(2)
        self.inflight: dict[str, ArrayLoad] = {}
        # Keyed by (array cid, invocation key, mapped dims, dim, the other dims' picks), all JSON.
        self.summaries: OrderedDict[tuple[str, ...], DimSummary] = OrderedDict()
        self.summary_values = 0

    def close(self):
        self.arrays.clear()
        self.bytes = 0
        self.summaries.clear()
        self.summary_values = 0

    def _remember_summary(self, key: tuple[str, ...], summary: DimSummary) -> None:
        size = len(summary.values) + 1
        if size > MAX_SUMMARY_VALUES:
            return
        while self.summaries and self.summary_values + size > MAX_SUMMARY_VALUES:
            _, removed = self.summaries.popitem(last=False)
            self.summary_values -= len(removed.values) + 1
        self.summaries[key] = summary
        self.summary_values += size

    def _remember(self, cid: str, data: dict[str, Any], size: int) -> None:
        if size > MAX_BYTES:
            return
        while self.arrays and (len(self.arrays) >= MAX_ARRAYS or self.bytes + size > MAX_BYTES):
            _, (_, removed_size) = self.arrays.popitem(last=False)
            self.bytes -= removed_size
        self.arrays[cid] = (data, size)
        self.bytes += size

    async def load(self, cids: list[str]) -> dict[str, dict[str, Any]]:
        # Cache and in-flight bookkeeping never await: all mutations belong to the event loop.
        # Only readers of the same CID wait for one another, including oversized uncached arrays.
        loaded: dict[str, dict[str, Any]] = {}
        wanted = set(cids)
        while wanted - loaded.keys():
            owned: dict[str, ArrayLoad] = {}
            pending: dict[str, ArrayLoad] = {}
            for cid in wanted - loaded.keys():
                if cid in self.arrays:
                    loaded[cid] = self.arrays[cid][0]
                    self.arrays.move_to_end(cid)
                else:
                    if cid not in self.inflight:
                        owned[cid] = self.inflight[cid] = ArrayLoad()
                    pending[cid] = self.inflight[cid]
            if owned:
                try:
                    async with self.client.database.transaction() as tx:
                        records = (
                            await tx.execute(
                                select(entities.c.cid, entities.c.data).where(
                                    entities.c.cid.in_([cid_from_str(cid) for cid in owned])
                                )
                            )
                        ).all()
                    if len(records) != len(owned):
                        raise ValueError("an output array is missing")
                    for record in records:
                        cid = cid_to_str(record.cid)
                        data, size = await run_sync(decode_array, bytes(record.data))
                        self._remember(cid, data, size)
                        owned[cid].data = data
                        owned[cid].done.set()
                except Exception as error:
                    for slot in owned.values():
                        if slot.data is None:
                            slot.error = error
                    raise
                finally:
                    # Cancellation releases waiters too; a surviving reader retries missing CIDs.
                    for cid, slot in owned.items():
                        self.inflight.pop(cid)
                        slot.done.set()
            for cid, slot in pending.items():
                await slot.done.wait()
                if slot.error is not None:
                    raise slot.error
                if slot.data is not None:
                    loaded[cid] = slot.data
        return loaded

    async def _open(self, source: OutputSource) -> tuple[Scan, dict[str, Any]]:
        """Where a source's arrays come from, and the selection it names."""
        cid = source.get("cid")
        job_id, pathname, node_id = (
            source.get("jobId"),
            source.get("pathname"),
            source.get("nodeId"),
        )
        selection = source.get("selection")
        if selection is None:
            selection = {}
        if not isinstance(selection, dict):
            raise ValueError("invalid dimension selection")
        if cid is not None:
            if not cid or any(value is not None for value in (job_id, pathname, node_id)):
                raise ValueError("an array source requires a nonempty cid and no run")
            data = (await self.load([cid]))[cid]
            return Scan(cid, None, [], {"dims": data["dims"], "schema": data["schema"]}), selection
        if not all(isinstance(value, str) and value for value in (job_id, pathname, node_id)):
            raise ValueError("a run source requires jobId, pathname, and nodeId")
        assert job_id is not None and pathname is not None and node_id is not None
        async with self.client.database.transaction() as tx:
            targets, mapped_dims, output_type = await run_context(
                tx, JobID(uuid.UUID(job_id)), pathname, node_id
            )
        return Scan(None, targets, mapped_dims, output_type), selection

    async def _refs(
        self,
        scan: Scan,
        invocation_keys: dict[str, Any],
        disconnected: Callable[[], Awaitable[bool]] | None,
        batch_size: int = BATCH_SIZE,
    ) -> AsyncGenerator[tuple[list[tuple[list[Any], str]], bool]]:
        """The scan's arrays in batches, as (invocation key, array cid) in key order (a stored
        array's alone, with no key), each batch with whether it is the last."""
        if scan.cid is not None:
            yield [([], scan.cid)], True
            return
        targets = scan.targets
        refs = selected(
            select(targets.c.key, targets.c.result_cid),
            targets.c.key,
            scan.mapped_dims,
            invocation_keys,
        )
        refs = refs.where(targets.c.result_cid.is_not(None)).order_by(targets.c.key)
        after = None
        while True:
            if disconnected and await disconnected():
                raise ValueError("output query cancelled")
            batch = (
                refs if after is None else refs.where(targets.c.key > literal(after, type_=JSONB))
            )
            async with self.client.database.transaction() as tx:
                records = (await tx.execute(batch.limit(batch_size + 1))).all()
            complete = len(records) <= batch_size
            records = records[:batch_size]
            yield [(row.key, cid_to_str(row.result_cid)) for row in records], complete
            if complete:
                return
            after = records[-1].key

    async def _each(
        self,
        scan: Scan,
        invocation_keys: dict[str, Any],
        visit: Callable[[list[Any], dict[str, Any]], bool],
        disconnected: Callable[[], Awaitable[bool]] | None,
    ) -> bool:
        """Hand `visit` each array of the scan (a run's in key order, with its invocation's key)
        until it returns true. Returns whether every array was handed over."""
        async with aclosing(self._refs(scan, invocation_keys, disconnected)) as batches:
            async for refs, complete in batches:
                arrays = await self.load([cid for _, cid in refs])
                for index, (spawn_key, cid) in enumerate(refs):
                    if await run_sync(visit, spawn_key, arrays[cid]):
                        return complete and index == len(refs) - 1
        return True

    async def page(
        self,
        source: OutputSource,
        offset: int = 0,
        first: int = 25,
        filters: dict[str, str] | None = None,
        sort: Sort | None = None,
        disconnected: Callable[[], Awaitable[bool]] | None = None,
        last: bool = False,
    ):
        """A page of the source's rows. Its selection may name invocation keys (picked out in SQL)
        and keys of dims produced inside an invocation (rows filtered as the arrays are read)."""
        page_size(first)
        if type(offset) is not int or offset < 0:
            raise ValueError("invalid output offset")
        if sort and (
            not isinstance(sort.get("key"), str) or sort.get("direction") not in ("asc", "desc")
        ):
            raise ValueError("invalid sort")
        async with self.limiter:
            if disconnected and await disconnected():
                raise ValueError("output query cancelled")
            scan, selection = await self._open(source)
            invocation_keys, row_keys = split_selection(
                selection, scan.mapped_dims, scan.output_dims
            )
            query = PageQuery(
                offset, first, filters or {}, sort, scan.type, scan.mapped_dims, last, row_keys
            )

            def visit(spawn_key: list[Any], data: dict[str, Any]) -> bool:
                query.add(spawn_key, data)
                return query.enough

            complete = await self._each(scan, invocation_keys, visit, disconnected)
            return await run_sync(query.result, complete)

    async def invocation_page(
        self,
        job_id: JobID,
        pathname: str,
        node_id: str,
        selection: dict[str, Any],
        offset: int = 0,
        first: int = 25,
        filters: dict[str, str] | None = None,
        sort: Sort | None = None,
        state: str | None = None,
        last: bool = False,
        disconnected: Callable[[], Awaitable[bool]] | None = None,
    ) -> InvocationPage:
        """A page of a run's invocations the selection picks out (only those in `state`, given
        one), each with what it produced: its result's row count, and when every invocation
        produces one row (the output has no dims but the invocation dims), that row's value.

        Filters and the sort are over an invocation's cells: `key:<dim>` (its key), `state:` (its
        display state), `rows:` (its row count) and the value cells of its one row (`value:` or
        `value:<field>`), as `outputPage` matches and orders cells. An invocation with no result
        has no row count or value, so a filter on those leaves it out, and a sort on them puts it
        last. With no filter or sort, the page is read in key order in SQL; otherwise every
        invocation in the selection is, and every result too when they name its row count or
        values. Ties keep key order."""
        page_size(first)
        if type(offset) is not int or offset < 0:
            raise ValueError("invalid invocation offset")
        if sort and (
            not isinstance(sort.get("key"), str) or sort.get("direction") not in ("asc", "desc")
        ):
            raise ValueError("invalid sort")
        needles = {
            key: text.strip().lower() for key, text in (filters or {}).items() if text.strip()
        }
        async with self.limiter:
            async with self.client.database.transaction(snapshot=True) as tx:
                targets, dims, output_type = await run_context(tx, job_id, pathname, node_id)
                query = selected(select(targets), targets.c.key, dims, selection)
                if state:
                    query = query.where(targets.c.state == state)
                query = query.order_by(targets.c.key)
                if not needles and not sort:
                    total = (
                        await tx.execute(select(func.count()).select_from(query.subquery()))
                    ).scalar_one()
                    offset = page_offset(offset, first, total, last)
                    rows = (await tx.execute(query.offset(offset).limit(first))).all()
                else:
                    rows = (await tx.execute(query)).all()
                    # Until the filters below narrow them.
                    total = len(rows)
            one_row = len(output_type["dims"]) == len(dims)
            if needles or sort:
                columns = set(needles) | ({sort["key"]} if sort else set())
                reads_results = any(key == "rows:" or key.startswith("value:") for key in columns)
                cells = [
                    {
                        **{f"key:{name}": key for name, key in zip(dims, row.key, strict=True)},
                        "state:": row.state,
                    }
                    for row in rows
                ]
                if reads_results:
                    with_result = [index for index, row in enumerate(rows) if row.result_cid]
                    for start in range(0, len(with_result), BATCH_SIZE):
                        if disconnected and await disconnected():
                            raise ValueError("invocation query cancelled")
                        chunk = with_result[start : start + BATCH_SIZE]
                        arrays = await self.load(
                            [cid_to_str(rows[index].result_cid) for index in chunk]
                        )
                        for index in chunk:
                            data = arrays[cid_to_str(rows[index].result_cid)]
                            cells[index]["rows:"] = len(data["rows"])
                            if one_row and data["rows"]:
                                cells[index].update(
                                    value_cells(data["schema"], data["rows"][0][-1])
                                )
                kept = [
                    index
                    for index, row_cells in enumerate(cells)
                    if all(
                        key in row_cells and needle in search_text(row_cells[key])
                        for key, needle in needles.items()
                    )
                ]
                if sort:
                    # Invocations without the cell (no result yet) go last either way.
                    key = sort["key"]
                    rank = cmp_to_key(compare_cells)
                    present = [index for index in kept if key in cells[index]]
                    present.sort(
                        key=lambda index: rank(cells[index][key]),
                        reverse=sort["direction"] == "desc",
                    )
                    kept = present + [index for index in kept if key not in cells[index]]
                total = len(kept)
                offset = page_offset(offset, first, total, last)
                rows = [rows[index] for index in kept[offset : offset + first]]
            cids = [cid_to_str(row.result_cid) for row in rows if row.result_cid]
            arrays = await self.load(cids)
        items: list[InvocationRow] = []
        for row in rows:
            data = arrays[cid_to_str(row.result_cid)] if row.result_cid else None
            first_row = data["rows"][0] if data and one_row and data["rows"] else None
            items.append(
                {
                    "spawn": spawn_dict(row),
                    "rowCount": None if data is None else len(data["rows"]),
                    "value": None
                    if first_row is None
                    else base64.b64encode(codec.encode(first_row[-1])).decode(),
                }
            )
        return {"items": items, "total": total, "offset": offset}

    async def dim_values(
        self,
        source: OutputSource,
        dim: str,
        search: str = "",
        offset: int = 0,
        first: int = 25,
        disconnected: Callable[[], Awaitable[bool]] | None = None,
    ) -> DimValuePage:
        """A page of the distinct keys `dim` takes in the source's rows, read from every array (so
        for dims produced inside an invocation, which no table records). A value is compatible
        when some row with it matches the source's selection of the other dims."""
        page_size(first, KEY_LIST_LIMIT)
        if type(offset) is not int or offset < 0:
            raise ValueError("invalid key offset")
        async with self.limiter:
            scan, selection = await self._open(source)
            if dim not in scan.output_dims:
                raise ValueError("unknown dimension")
            others = {name: value for name, value in selection.items() if name != dim}
            split_selection(others, scan.mapped_dims, scan.output_dims)
            shape = (json.dumps(scan.mapped_dims), dim, json.dumps(sorted(others.items())))
            values: dict[str, Any] = {}
            compatible: set[str] = set()
            async with aclosing(self._refs(scan, {}, disconnected, REF_BATCH_SIZE)) as batches:
                async for refs, _ in batches:
                    summaries: dict[tuple[str, ...], DimSummary] = {}
                    missing: list[tuple[tuple[str, ...], list[Any], str]] = []
                    for spawn_key, cid in refs:
                        key = (cid, json.dumps(spawn_key), *shape)
                        cached = self.summaries.get(key)
                        if cached is None:
                            missing.append((key, spawn_key, cid))
                        else:
                            self.summaries.move_to_end(key)
                            summaries[key] = cached
                    for start in range(0, len(missing), BATCH_SIZE):
                        if disconnected and await disconnected():
                            raise ValueError("output query cancelled")
                        chunk = missing[start : start + BATCH_SIZE]
                        arrays = await self.load([cid for _, _, cid in chunk])
                        for key, spawn_key, cid in chunk:
                            prefix = dict(zip(scan.mapped_dims, spawn_key, strict=True))
                            summary = await run_sync(summarize, prefix, arrays[cid], dim, others)
                            self._remember_summary(key, summary)
                            summaries[key] = summary
                    for summary in summaries.values():
                        for token, value in summary.values.items():
                            values.setdefault(token, value)
                        compatible |= summary.compatible
        needle = search.strip().lower()
        if needle:
            values = {
                token: value for token, value in values.items() if needle in str(value).lower()
            }
        # Strings before numbers, as key pages' jsonb order; strings by code point, not collation.
        ordered = sorted(values.items(), key=lambda item: (not isinstance(item[1], str), item[1]))
        total = len(ordered)
        offset = min(offset // first * first, max(0, (total - 1) // first * first))
        return {
            "items": [
                {"value": value, "compatible": token in compatible}
                for token, value in ordered[offset : offset + first]
            ],
            "total": total,
            "offset": offset,
        }
