"""Benchmark case definitions: transfer scenarios and the entity × transfer matrix."""

from __future__ import annotations

import contextlib
import tempfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr2.data.store_indexing.indexers import DerefMarker, PathKey, TypedPathValueIndexer
from fxtr2.data.store_indexing.indexing_types import IndexerColumnType

from .entity_generators import EntityScenario, many_flat_1k, rich_flat_1k, single_tree_1k

GenerateFn = Callable[[], tuple[list[tuple[str, FxtrID]], int]]


# ---------------------------------------------------------------------------
# BenchmarkCase: entity scenario × transfer scenario
# ---------------------------------------------------------------------------


@dataclass
class BenchmarkCase:
    name: str
    entity: EntityScenario
    transfer: TransferScenario

    @property
    def description(self) -> str:
        return f"{self.entity.description} | {self.transfer.description}"


# ---------------------------------------------------------------------------
# Transfer scenario base
# ---------------------------------------------------------------------------


class TransferScenario:
    name: str
    description: str
    _entity_count: int
    _pin_count: int

    @property
    def entity_count(self) -> int:
        return self._entity_count

    @property
    def pin_count(self) -> int:
        return self._pin_count

    def global_context(self, generate: GenerateFn) -> contextlib.AbstractAsyncContextManager[None]:
        raise NotImplementedError

    def iteration_context(self) -> contextlib.AbstractAsyncContextManager[None]:
        raise NotImplementedError

    async def run(self) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Transfer: Memory scope -> SQLite namespace (pin)
# ---------------------------------------------------------------------------


class MemoryToSqlite(TransferScenario):
    name = "memory_to_sqlite"
    description = "Pin from memory scope into SQLite namespace"

    def __init__(self) -> None:
        self._source_scope: FxtrScope | None = None
        self._entity_keys: list[tuple[str, FxtrID]] = []
        self._entity_count = 0
        self._pin_count = 0
        self._namespace: SQLiteFxtrNamespace | None = None

    @contextlib.asynccontextmanager
    async def global_context(self, generate: GenerateFn) -> AsyncIterator[None]:
        async with FxtrScope() as scope:
            self._source_scope = scope
            self._entity_keys, self._entity_count = generate()
            self._pin_count = len(self._entity_keys)
            yield

    @contextlib.asynccontextmanager
    async def iteration_context(self) -> AsyncIterator[None]:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = str(Path(tmp_dir) / "bench.sqlite")
            async with SQLiteFxtrNamespace(db_path, "bench_ns", isolate_scope=True) as ns:
                self._namespace = ns
                yield
                self._namespace = None

    async def run(self) -> None:
        assert self._namespace is not None
        assert self._source_scope is not None
        for key, entity_id in self._entity_keys:
            await self._namespace.pin(key, entity_id, scope=self._source_scope)


# ---------------------------------------------------------------------------
# Transfer: SQLite namespace -> Memory scope (retrieve + copy)
# ---------------------------------------------------------------------------


class SqliteToMemory(TransferScenario):
    name = "sqlite_to_memory"
    description = "Retrieve+copy from SQLite into isolated memory scope"

    def __init__(self) -> None:
        self._db_dir: tempfile.TemporaryDirectory | None = None  # type: ignore[type-arg]
        self._db_path: str = ""
        self._pin_keys: list[str] = []
        self._entity_count = 0
        self._pin_count = 0
        self._namespace: SQLiteFxtrNamespace | None = None
        self._target_scope: FxtrScope | None = None

    @contextlib.asynccontextmanager
    async def global_context(self, generate: GenerateFn) -> AsyncIterator[None]:
        # Create entities in memory, pin into sqlite, then close the scope.
        with tempfile.TemporaryDirectory() as tmp_dir:
            self._db_path = str(Path(tmp_dir) / "bench.sqlite")

            async with FxtrScope() as scope:
                entity_keys, self._entity_count = generate()
                self._pin_keys = [k for k, _ in entity_keys]
                self._pin_count = len(entity_keys)

                async with SQLiteFxtrNamespace(self._db_path, "bench_ns", isolate_scope=True) as ns:
                    for key, entity_id in entity_keys:
                        await ns.pin(key, entity_id, scope=scope)
            # Source scope and namespace closed — DB file persists in tmp_dir

            yield
            # tmp_dir cleaned up after yield returns

    @contextlib.asynccontextmanager
    async def iteration_context(self) -> AsyncIterator[None]:
        # Cold open: no in-memory caches from previous iterations
        async with SQLiteFxtrNamespace(self._db_path, "bench_ns", isolate_scope=True) as ns:
            self._namespace = ns
            async with FxtrScope(isolated=True) as target:
                self._target_scope = target
                yield
                self._target_scope = None
            self._namespace = None

    async def run(self) -> None:
        assert self._namespace is not None
        assert self._target_scope is not None
        for key in self._pin_keys:
            await self._namespace.retrieve(key, into_scope=self._target_scope, copy_into_scope=True)


# ---------------------------------------------------------------------------
# Transfer: Memory scope -> SQLite namespace with 20 indexers (pin)
# ---------------------------------------------------------------------------


def _make_indexers() -> list[TypedPathValueIndexer]:
    """Build 20 TypedPathValueIndexer instances targeting RichBenchmarkTreeNode fields."""
    indexers: list[TypedPathValueIndexer] = []
    for i in range(10):
        indexers.append(
            TypedPathValueIndexer(
                initial_lookup=DerefMarker(),
                strict=False,
                path=(PathKey(key=f"f_int_{i}"),),
                var_types=(),
                value_type=IndexerColumnType.INT,
            )
        )
    for i in range(10):
        indexers.append(
            TypedPathValueIndexer(
                initial_lookup=DerefMarker(),
                strict=False,
                path=(PathKey(key=f"f_float_{i}"),),
                var_types=(),
                value_type=IndexerColumnType.FLOAT,
            )
        )
    return indexers


class MemoryToSqliteIndexed(TransferScenario):
    name = "memory_to_sqlite_indexed"
    description = "Pin from memory into SQLite namespace with 20 indexers"

    def __init__(self) -> None:
        self._source_scope: FxtrScope | None = None
        self._entity_keys: list[tuple[str, FxtrID]] = []
        self._entity_count = 0
        self._pin_count = 0
        self._indexers: list[TypedPathValueIndexer] = []
        self._namespace: SQLiteFxtrNamespace | None = None

    @contextlib.asynccontextmanager
    async def global_context(self, generate: GenerateFn) -> AsyncIterator[None]:
        async with FxtrScope() as scope:
            self._source_scope = scope
            self._entity_keys, self._entity_count = generate()
            self._pin_count = len(self._entity_keys)
            self._indexers = _make_indexers()
            yield

    @contextlib.asynccontextmanager
    async def iteration_context(self) -> AsyncIterator[None]:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = str(Path(tmp_dir) / "bench.sqlite")
            async with SQLiteFxtrNamespace(db_path, "bench_ns", isolate_scope=True) as ns:
                # Register indexers on the empty namespace (not timed)
                for indexer in self._indexers:
                    await ns.register_indexer(indexer)
                self._namespace = ns
                yield
                self._namespace = None

    async def run(self) -> None:
        assert self._namespace is not None
        assert self._source_scope is not None
        for key, entity_id in self._entity_keys:
            await self._namespace.pin(key, entity_id, scope=self._source_scope)


# ---------------------------------------------------------------------------
# Curated matrix: entity scenario × transfer scenario
# ---------------------------------------------------------------------------

ALL_CASES = [
    BenchmarkCase("tree_pin", single_tree_1k, MemoryToSqlite()),
    BenchmarkCase("tree_retrieve", single_tree_1k, SqliteToMemory()),
    BenchmarkCase("flat_pin", many_flat_1k, MemoryToSqlite()),
    BenchmarkCase("flat_retrieve", many_flat_1k, SqliteToMemory()),
    BenchmarkCase("flat_indexed", rich_flat_1k, MemoryToSqliteIndexed()),
]
