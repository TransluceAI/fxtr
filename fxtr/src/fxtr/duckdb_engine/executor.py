"""DuckDB execution over the Arrow form of fxtr arrays.

:class:`DuckDBArrowExecutor` runs a compiled query over its input arrays' Arrow tables, which
each array converts once and keeps, and returns the result as Arrow, validated against the
declared type and bounded by the executor's limits. Local queries run on it, and so does
:func:`~fxtr.duckdb_engine.local_query.query_arrays`, which computes a workflow's query nodes. A
plan may also carry a user's read-only SQL (from :func:`fxtr.sql`), which is checked to read only
the tables it names.
"""

from __future__ import annotations

import dataclasses
import math
import re
import secrets
import threading
from bisect import bisect_left
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import asynccontextmanager, contextmanager
from functools import partial
from typing import Any, cast

import anyio
import duckdb
import pyarrow as pa
from anyio.from_thread import check_cancelled
from anyio.lowlevel import checkpoint
from anyio.to_thread import run_sync as run_sync_in_worker_thread
from duckdb.sqltypes import DuckDBPyType
from frozendict import frozendict

from fxtr.core.entity_data import EntityID
from fxtr.duckdb_engine.compiler import (
    SQLResultCheck,
    compile_array_query,
    quote_identifier,
)
from fxtr.entity_defns.array import Array, ArrayType, Key
from fxtr.entity_defns.arrow import (
    DEFAULT_ARROW_BATCH_SIZE,
    ArrowArrayError,
    array_arrow_schema,
    array_arrow_table,
    array_holds_arrow_table,
    arrow_columns,
    cancellable,
    normalize_arrow_batch,
    require_arrow_column_names,
    validate_arrow_batches,
)
from fxtr.entity_defns.query import DuckDBResourceLimitError, QueryError
from fxtr.entity_defns.query_plan import (
    QueryPlan,
    QuerySelect,
    row_preserving_source,
)
from fxtr.entity_defns.schema import (
    IdSchema,
    LeafSchema,
    ListSchema,
    NullableSchema,
    Schema,
    StructSchema,
)
from fxtr.entity_defns.sql import PRIVATE_NAME_PREFIX


async def arrow_table_of(array: Array[Any]) -> pa.Table:
    """``array``'s Arrow table, converted in a worker thread the first time a query reads it.

    The table is kept on the array, so every later query over it converts nothing.
    """
    if array_holds_arrow_table(array):
        return array_arrow_table(array)
    return await run_sync_in_worker_thread(partial(array_arrow_table, array, check_cancelled))


@dataclasses.dataclass
class DuckDBExecutionMetrics:
    """Counts and costs of the queries and connections of one executor."""

    queries: int = 0
    arrow_bytes_written: int = 0
    connections_opened: int = 0
    connections_reused: int = 0


class DuckDBQueryCancelledError(RuntimeError):
    """A query was interrupted because its caller was cancelled, not because of a limit."""


_INTERRUPT_INTERVAL_SECONDS = 0.005
"""How often a cancelled run is interrupted again until its native thread stops."""

# The names fxtr generates, and no others: a user's own field may be called __fxtr_anything.
_GENERATED_NAME = re.compile(
    re.escape(PRIVATE_NAME_PREFIX)
    + r"(?:input_[0-9a-f]{16}_\d+|value(?:_\d+)?|sql_output|result)\b"
)


def _query_error(exc: duckdb.Error, *, keep_sql_excerpt: bool) -> QueryError:
    """A :class:`QueryError` carrying DuckDB's own explanation of a failed query.

    DuckDB follows its explanation with an excerpt of the statement. For a query-builder plan that
    is SQL fxtr generated, which means nothing to the query's author, so only user SQL keeps it.
    """
    message = str(exc).strip()
    if not keep_sql_excerpt:
        message = message.split("\n\nLINE ", 1)[0]
    message = _GENERATED_NAME.sub("<internal>", message)
    return QueryError(f"DuckDB could not run the query: {message}")


def _relation_names(count: int) -> tuple[str, ...]:
    """Names to register one execution's source tables under.

    ``validate_sql_reads`` already refuses user SQL that names a private relation. The random
    part changes on every execution, so SQL that got past that check would have to write a name
    it cannot know when it is written. A statement that lists DuckDB's relations would see the
    name, and cannot use it: every function that takes a name at run time is refused.
    """
    execution = secrets.token_hex(8)
    return tuple(f"{PRIVATE_NAME_PREFIX}input_{execution}_{index}" for index in range(count))


@dataclasses.dataclass(frozen=True, kw_only=True)
class DuckDBExecutionLimits:
    """Optional limits on a query; omitted values use DuckDB defaults or impose no fxtr cap.

    DuckDB's memory setting covers its managed buffers, not all Python or Arrow allocations.
    These settings do not isolate a query's total process memory from other requests.
    """

    memory_limit: str | None = None
    threads: int | None = None
    timeout_seconds: float | None = None
    max_result_rows: int | None = None
    max_result_bytes: int | None = None

    def __post_init__(self) -> None:
        if self.memory_limit is not None and not self.memory_limit:
            raise ValueError("memory_limit must not be empty")
        if self.threads is not None and self.threads < 1:
            raise ValueError("threads must be at least 1")
        if self.timeout_seconds is not None and (
            not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")
        if self.max_result_rows is not None and self.max_result_rows < 0:
            raise ValueError("max_result_rows must be non-negative")
        if self.max_result_bytes is not None and self.max_result_bytes < 0:
            raise ValueError("max_result_bytes must be non-negative")


class _DuckDBRun:
    """One query, run on the calling thread and interruptible from the event loop.

    A run that is cancelled between finishing and being claimed does not hand its result back:
    the caller gets :class:`DuckDBQueryCancelledError` instead. The executor's
    :meth:`~DuckDBArrowExecutor.query_scope` deadline cancels the caller, which is how a timeout
    reaches :meth:`cancel`.

    ``check_cancelled`` is how the Arrow work between DuckDB calls notices that the caller has
    been cancelled. The default suits :meth:`~DuckDBArrowExecutor._run_duckdb`, which runs the query
    in an anyio worker thread. :meth:`~DuckDBArrowExecutor.execute_query_on_this_thread` passes
    ``None``: nothing can cancel a synchronous call, and anyio's check refuses to be called outside
    a worker thread.
    """

    def __init__(
        self,
        executor: DuckDBArrowExecutor,
        sql: str,
        prepared: Mapping[str, pa.Table],
        output_type: ArrayType,
        parameters: tuple[Any, ...],
        *,
        sql_checks: tuple[SQLResultCheck, ...] = (),
        check_cancelled: Callable[[], None] | None = check_cancelled,
    ) -> None:
        self.executor = executor
        self.sql = sql
        self.prepared = prepared
        self.output_type = output_type
        self.parameters = parameters
        self.sql_checks = sql_checks
        self.check_cancelled = check_cancelled
        self._lock = threading.Lock()
        self._connection: duckdb.DuckDBPyConnection | None = None
        self._staged: pa.Table | None = None
        self._cancelled = False

    def execute(self) -> pa.Table:
        """Run the query on the calling thread and stage its result."""
        with self.executor._connection(tuple(self.prepared)) as connection:
            try:
                with self._lock:
                    if self._cancelled:
                        raise DuckDBQueryCancelledError("DuckDB query was cancelled")
                    self._connection = connection
                staged = self.executor._execute_to_result(
                    connection,
                    self.sql,
                    self.prepared,
                    self.output_type,
                    self.parameters,
                    sql_checks=self.sql_checks,
                    check_cancelled=self.check_cancelled,
                )
            except duckdb.InterruptException:
                raise
            except duckdb.Error as exc:
                raise _query_error(exc, keep_sql_excerpt=bool(self.sql_checks)) from exc
            finally:
                # Forget the connection before it can be handed to another run, so a late
                # cancel() of this run cannot interrupt that run's query.
                with self._lock:
                    self._connection = None
            # Claim the result before the connection is cleaned up, so a cancel that arrives
            # while cleanup runs still stops the result reaching the caller.
            return self._claim(staged)

    def cancel(self) -> None:
        """Interrupt DuckDB and drop a result not yet returned to the async caller."""
        with self._lock:
            self._cancelled = True
            connection = self._connection
            self._staged = None
            if connection is not None:
                connection.interrupt()

    def take(self, staged: pa.Table) -> None:
        """Hand the result to the async caller, unless a cancel arrived first."""
        with self._lock:
            if self._staged is not staged:
                raise DuckDBQueryCancelledError("DuckDB query was cancelled")
            self._staged = None

    def _claim(self, staged: pa.Table) -> pa.Table:
        with self._lock:
            cancelled = self._cancelled
            if not cancelled:
                self._staged = staged
        if cancelled:
            raise DuckDBQueryCancelledError("DuckDB query was cancelled")
        return staged


class DuckDBArrowExecutor:
    """Runs compiled query plans in DuckDB over in-memory Arrow tables, under its limits."""

    def __init__(
        self,
        *,
        batch_size: int = DEFAULT_ARROW_BATCH_SIZE,
        limits: DuckDBExecutionLimits | None = None,
        query_limiter: anyio.CapacityLimiter | None = None,
    ) -> None:
        if batch_size <= 0:
            raise ValueError(f"batch_size must be positive, got {batch_size}")
        self.batch_size = batch_size
        self.limits = limits or DuckDBExecutionLimits()
        self.query_limiter = query_limiter or anyio.CapacityLimiter(math.inf)
        self.metrics = DuckDBExecutionMetrics()
        self._connection_lock = threading.Lock()
        self._idle_connection: duckdb.DuckDBPyConnection | None = None
        self._given_limiter = query_limiter

    @property
    def limit_settings(self) -> tuple[DuckDBExecutionLimits, int | None]:
        """What this executor was told to limit: its limits, and which limiter it was given.

        Two executors with equal settings bound a query the same way.
        """
        given = self._given_limiter
        return self.limits, None if given is None else id(given)

    @property
    def is_unlimited(self) -> bool:
        """Whether this executor was given neither limits nor a limiter."""
        return self.limit_settings == (DuckDBExecutionLimits(), None)

    def _connect(self) -> duckdb.DuckDBPyConnection:
        config: dict[str, str | bool | int | float | list[str]] = {
            "allow_community_extensions": "false",
            "autoinstall_known_extensions": "false",
            "autoload_known_extensions": "false",
            "enable_external_access": "false",
            "python_enable_replacements": "false",
        }
        if self.limits.memory_limit is not None:
            config["memory_limit"] = self.limits.memory_limit
        if self.limits.threads is not None:
            config["threads"] = str(self.limits.threads)
        connection = duckdb.connect(":memory:", config=config)
        try:
            connection.execute("SET lock_configuration = true")
        except BaseException:
            connection.close()
            raise
        with self._connection_lock:
            self.metrics.connections_opened += 1
        return connection

    @contextmanager
    def _connection(self, inputs: Sequence[str]) -> Iterator[duckdb.DuckDBPyConnection]:
        with self._connection_lock:
            connection, self._idle_connection = self._idle_connection, None
            if connection is not None:
                self.metrics.connections_reused += 1
        if connection is None:
            connection = self._connect()
        try:
            yield connection
            for name in inputs:
                connection.unregister(name)
        except BaseException:
            connection.close()
            raise
        else:
            # Concurrent queries own separate connections. Retain at most one idle connection;
            # registered Arrow inputs have been removed and failed runs are never cached.
            with self._connection_lock:
                if self._idle_connection is None:
                    self._idle_connection = connection
                    return
            connection.close()

    def _stage_result(
        self,
        output_type: ArrayType,
        batches: Iterable[pa.RecordBatch],
    ) -> pa.Table:
        """Retain validated Arrow output without constructing Python rows."""
        validated, _ = validate_arrow_batches(batches, output_type)
        return pa.Table.from_batches(validated, schema=array_arrow_schema(output_type))

    @asynccontextmanager
    async def query_scope(self) -> AsyncIterator[None]:
        """Apply the deadline from waiting for capacity through result handling.

        The deadline bounds how long a caller waits in total, so it covers the wait for query
        capacity too; the error says which of the two ran out of time.
        """
        scope: anyio.CancelScope | None = None
        running = False
        try:
            with anyio.fail_after(self.limits.timeout_seconds) as scope:
                async with self.query_limiter:
                    running = True
                    yield
        except TimeoutError as exc:
            if scope is None or not scope.cancel_called:
                raise
            seconds = f"{self.limits.timeout_seconds:g} seconds"
            raise DuckDBResourceLimitError(
                f"DuckDB query exceeded {seconds}"
                if running
                else f"DuckDB query waited {seconds} for query capacity without starting"
            ) from exc

    async def _run_duckdb(self, run: _DuckDBRun) -> pa.Table:
        # Interrupt from a sibling task, but retain query capacity until the native thread has
        # actually stopped. Abandoning it would let cancelled queries consume uncounted memory.
        native_finished = False

        async def interrupt_on_cancel() -> None:
            try:
                await anyio.sleep_forever()
            finally:
                # DuckDB drops an interrupt that arrives while none of the run's statements is
                # executing, between two of them, so interrupt until the native thread stops.
                with anyio.CancelScope(shield=True):
                    while not native_finished:
                        run.cancel()
                        await anyio.sleep(_INTERRUPT_INTERVAL_SECONDS)

        staged: pa.Table | None = None
        failure: BaseException | None = None
        try:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(interrupt_on_cancel)
                try:
                    staged = await run_sync_in_worker_thread(run.execute)
                except BaseException as exc:
                    failure = exc
                finally:
                    native_finished = True
                    task_group.cancel_scope.cancel()
            # Parent cancellation must take precedence over DuckDB's interruption exception.
            await checkpoint()
            if failure is not None:
                raise failure
            assert staged is not None
            run.take(staged)
            return staged
        except BaseException:
            run.cancel()
            raise

    def _execute_to_result(
        self,
        connection: duckdb.DuckDBPyConnection,
        sql: str,
        prepared: Mapping[str, pa.Table],
        output_type: ArrayType,
        parameters: tuple[Any, ...],
        *,
        sql_checks: tuple[SQLResultCheck, ...] = (),
        check_cancelled: Callable[[], None] | None = check_cancelled,
    ) -> pa.Table:
        # Each step is its own DuckDB statement, and an interrupt reaches only a running one, so
        # a cancel that arrived in between is noticed here before the next statement starts.
        def stop_if_cancelled() -> None:
            if check_cancelled is not None:
                check_cancelled()

        for name, table in prepared.items():
            stop_if_cancelled()
            connection.register(name, table)
        parameters = tuple(_sql_parameter(value) for value in parameters)
        for check in sql_checks:
            stop_if_cancelled()
            _check_sql_result(connection, check, parameters)
        stop_if_cancelled()
        actual_names = _result_columns(connection, sql, parameters).columns
        require_arrow_column_names(actual_names, output_type)
        wrapped = _canonical_result_sql(sql, output_type)
        stop_if_cancelled()
        with connection.sql(wrapped, params=parameters).to_arrow_reader(self.batch_size) as reader:
            try:
                return self._stage_result(output_type, self._bounded_batches(reader, output_type))
            except ArrowArrayError as exc:
                # The check is of Arrow data, and its message says so; the author wrote a
                # query and needs to hear that its rows are what failed.
                raise QueryError(f"The query's rows are not a valid array: {exc}") from exc

    def _bounded_batches(
        self, batches: Iterable[pa.RecordBatch], output_type: ArrayType
    ) -> Iterator[pa.RecordBatch]:
        rows = 0
        bytes_read = 0
        for batch in batches:
            rows += batch.num_rows
            bytes_read += batch.nbytes
            if self.limits.max_result_rows is not None and rows > self.limits.max_result_rows:
                raise DuckDBResourceLimitError(
                    f"DuckDB result exceeded {self.limits.max_result_rows} rows"
                )
            if (
                self.limits.max_result_bytes is not None
                and bytes_read > self.limits.max_result_bytes
            ):
                raise DuckDBResourceLimitError(
                    f"DuckDB result exceeded {self.limits.max_result_bytes} Arrow bytes"
                )
            yield normalize_arrow_batch(batch, output_type)

    def close_idle_connection(self) -> None:
        """Release a cached native connection without interrupting any running query."""
        with self._connection_lock:
            connection = self._idle_connection
            self._idle_connection = None
        if connection is not None:
            connection.close()

    def execute_query_on_this_thread(
        self,
        query: QueryPlan,
        source_types: Sequence[ArrayType],
        prepare: Callable[[], Sequence[pa.Table]],
    ) -> tuple[pa.Table, ArrayType]:
        """Run ``query`` over the tables ``prepare`` returns on the calling thread, returning the
        Arrow output and its type. ``prepare`` is called once the plan has compiled.

        Nothing can cancel the call, so the executor's timeout and query limiter do not apply:
        they work by cancelling or holding up an async caller. Its result caps (enforced as the
        result is read) and connection settings (memory and threads) do.

        Raises:
            QueryError: If the plan cannot be compiled or DuckDB cannot run it.
        """
        names = _relation_names(len(source_types))
        compiled = compile_array_query(query, source_types, names)
        run = _DuckDBRun(
            self,
            compiled.sql,
            dict(zip(names, prepare(), strict=True)),
            compiled.output_type,
            compiled.parameters,
            sql_checks=compiled.sql_checks,
            # The caller's thread may be an event loop's, where anyio's check refuses to run.
            check_cancelled=None,
        )
        return run.execute(), compiled.output_type

    async def execute_query(
        self,
        query: QueryPlan,
        source_types: Sequence[ArrayType],
        prepare: Callable[[], Awaitable[Sequence[pa.Table]]],
        *,
        reuse_rows_from: Array[Any] | None = None,
    ) -> tuple[pa.Table, ArrayType]:
        """Run ``query`` over the tables ``prepare`` returns, within :meth:`query_scope`,
        returning the Arrow output and its type.

        ``reuse_rows_from`` names the one source array when ``query`` only drops rows from it
        (see :func:`~fxtr.entity_defns.query_plan.row_preserving_source`). DuckDB then returns
        just the surviving keys, and the complete rows are taken from the source's Arrow table
        rather than copied through DuckDB.

        Raises:
            QueryError: If the plan cannot be compiled or run, or ``reuse_rows_from`` is given
                for a query that changes rows.
        """
        names = _relation_names(len(source_types))
        if reuse_rows_from is None:
            compiled = compile_array_query(query, source_types, names)
            output_type = compiled.output_type
        else:
            if (
                row_preserving_source(query) != 0
                or len(source_types) != 1
                or source_types[0] != reuse_rows_from.type
            ):
                raise QueryError(
                    "Only filters, limits, and identity value selections over one source can "
                    "reuse its rows"
                )
            # Selecting no fields leaves the dimensions: the keys of the rows to take.
            compiled = compile_array_query(QuerySelect(query, frozendict()), source_types, names)
            output_type = reuse_rows_from.type
        async with self.query_scope():
            tables = await prepare()
            run = _DuckDBRun(
                self,
                compiled.sql,
                dict(zip(names, tables, strict=True)),
                compiled.output_type,
                compiled.parameters,
                sql_checks=compiled.sql_checks,
            )
            result = await self._run_duckdb(run)
            if reuse_rows_from is not None:
                result = await run_sync_in_worker_thread(
                    self._select_source_rows, result, tables[0], reuse_rows_from
                )
            self.metrics.queries += 1
            self.metrics.arrow_bytes_written += result.nbytes
            return result, output_type

    def _select_source_rows(
        self,
        keys: pa.Table,
        table: pa.Table,
        source: Array[Any],
    ) -> pa.Table:
        """The rows of ``table`` (``source`` as Arrow) whose keys DuckDB returned in ``keys``.

        Both are in key order, so the positions to take only increase and ``table``'s batches
        are walked once.
        """

        def batches() -> Iterator[pa.RecordBatch]:
            source_batches = iter(table.to_batches(max_chunksize=self.batch_size))
            source_batch = next(source_batches, None)
            offset = 0
            for batch in keys.to_batches(max_chunksize=self.batch_size):
                columns = [
                    cast("Sequence[int | str]", batch.column(index).to_pylist())
                    for index in range(len(source.dims))
                ]
                row_keys = (
                    zip(*columns, strict=True) if columns else (() for _ in range(batch.num_rows))
                )
                positions = [
                    source.row_position(cast("Key", key))
                    for key in cancellable(row_keys, check_cancelled)
                ]
                start = 0
                while start < len(positions):
                    assert source_batch is not None
                    while positions[start] >= offset + source_batch.num_rows:
                        offset += source_batch.num_rows
                        source_batch = next(source_batches)
                    end = bisect_left(positions, offset + source_batch.num_rows, lo=start)
                    selected = source_batch.take(
                        pa.array(
                            [position - offset for position in positions[start:end]], pa.int64()
                        )
                    )
                    check_cancelled()
                    yield selected
                    start = end

        # Count complete selected values, not just DuckDB's keys, and allocate at most one
        # batch before checking the result budget. Taking from individual input batches also
        # avoids combining every input buffer to select a few rows from a large array.
        return self._stage_result(source.type, self._bounded_batches(batches(), source.type))


def _canonical_result_sql(sql: str, output_type: ArrayType) -> str:
    columns = arrow_columns(output_type)
    projected = ", ".join(quote_identifier(column.physical_name) for column in columns)
    wrapped = f"SELECT {projected} FROM ({sql}) AS __fxtr_result"
    dimensions = columns[: len(output_type.dims)]
    if dimensions:
        order = ", ".join(quote_identifier(column.physical_name) for column in dimensions)
        wrapped += f" ORDER BY {order}"
    return wrapped


def _result_columns(
    connection: duckdb.DuckDBPyConnection, sql: str, parameters: object
) -> duckdb.DuckDBPyRelation:
    """A relation with the columns and types ``sql`` returns, obtained without running it.

    Given parameters, ``connection.sql`` runs the statement to build its relation, so asking the
    statement itself for its columns would run every query a second time. No row of a ``LIMIT 0``
    is ever wanted, and DuckDB answers it from the statement's plan.
    """
    return connection.sql(f"SELECT * FROM ({sql}) AS q LIMIT 0", params=parameters)


def _sql_parameter(value: Any) -> Any:
    """A query parameter as DuckDB binds it: an entity ID by its content bytes, anything else
    unchanged."""
    if isinstance(value, EntityID):
        return value.cid
    if isinstance(value, Mapping):
        return {
            name: _sql_parameter(item) for name, item in cast("Mapping[str, Any]", value).items()
        }
    if isinstance(value, (tuple, list)):
        return [_sql_parameter(item) for item in cast("Sequence[Any]", value)]
    return value


def _check_sql_result(
    connection: duckdb.DuckDBPyConnection, check: SQLResultCheck, parameters: tuple[Any, ...]
) -> None:
    required = connection.extract_statements(check.sql)[0].named_parameters
    bindings = {
        str(index): value
        for index, value in enumerate(parameters, start=1)
        if str(index) in required
    }
    relation = _result_columns(connection, check.sql, bindings)
    names = relation.columns
    expected = {"dims", "value"} if check.output_type.dims else {"value"}
    valid = set(names) == expected or (
        not check.output_type.dims and set(names) == {"dims", "value"}
    )
    if not valid or len(names) != len(set(names)):
        raise QueryError(
            f"SQL result must return {sorted(expected)} columns, got {names}; "
            "use SELECT dims, value for unchanged rows, or construct the declared structs"
        )
    types = relation.types
    if (
        not check.output_type.dims
        and "dims" in names
        and types[names.index("dims")].id != "boolean"
    ):
        # A table with no dims has FALSE for its dims column, so that is what passing one
        # through returns. Anything else means the SELECT builds dims its output_type lacks.
        raise QueryError(
            f"SQL result has a 'dims' column of type {types[names.index('dims')]}, but "
            "output_type has no dims; declare the dims in output_type, or return only 'value'"
        )

    def check_fields(name: str, fields: set[str]) -> None:
        type = types[names.index(name)]
        if type.id != "struct":
            raise QueryError(f"SQL result {name!r} must be a struct with fields {sorted(fields)}")
        actual = {child[0] for child in type.children}
        if actual != fields:
            raise QueryError(
                f"SQL result {name!r} fields do not match output_type; "
                f"expected={sorted(fields)}, actual={sorted(actual)}"
            )

    if check.output_type.dims:
        check_fields("dims", set(check.output_type.dim_names))
        dims_type = _member_types(types[names.index("dims")])
        for dim in check.output_type.dims:
            _require_kind(dims_type[dim.name], LeafSchema(dim.kind), f"dim {dim.name!r}")
    schema = check.output_type.schema
    schema = schema.inner if isinstance(schema, NullableSchema) else schema
    if isinstance(schema, StructSchema) and schema.fields:
        check_fields("value", set(schema.fields))
    _require_kind(types[names.index("value")], check.output_type.schema, "value")


_INTEGER_TYPES = frozenset(
    {
        "tinyint",
        "smallint",
        "integer",
        "bigint",
        "hugeint",
        "utinyint",
        "usmallint",
        "uinteger",
        "ubigint",
        "uhugeint",
    }
)

_KIND_TYPES: dict[str, frozenset[str]] = {
    "int": _INTEGER_TYPES,
    # A number of any kind is a float: DuckDB types a literal such as 2.5 as a DECIMAL.
    "float": _INTEGER_TYPES | {"float", "double", "decimal"},
    "bool": frozenset({"boolean"}),
    "str": frozenset({"varchar"}),
    "bytes": frozenset({"blob"}),
}


def _require_kind(actual: DuckDBPyType, schema: Schema, where: str) -> None:
    """Refuse a SQL result column whose DuckDB type is not of the kind ``schema`` declares.

    The compiled query casts the column to the declared type, and a cast converts across kinds
    (``'5'`` to ``5``, ``7`` to ``true``); this leaves it only a number to widen. DuckDB types a
    bare ``NULL`` or ``[]`` as holding integers, so SQL that means one of another kind casts it.

    Raises:
        QueryError: For the first part of the column of another kind.
    """
    match schema:
        case NullableSchema(inner):
            _require_kind(actual, inner, where)
            return
        case LeafSchema(kind):
            matches = actual.id in _KIND_TYPES[kind]
        case IdSchema():
            matches = actual.id == "blob"
        case ListSchema(element):
            if actual.id == "list":
                _require_kind(_member_types(actual)["child"], element, f"{where}[]")
                return
            matches = False
        case StructSchema(fields) if fields:
            if actual.id == "struct":
                members = _member_types(actual)
                if set(members) != set(fields):
                    raise QueryError(
                        f"SQL result {where} has fields {sorted(members)}, but output_type "
                        f"declares {sorted(fields)}"
                    )
                for name, field in fields.items():
                    _require_kind(members[name], field, f"{where}.{name}")
                return
            matches = False
        case _:
            matches = actual.id == "boolean"
    if not matches:
        raise QueryError(
            f"SQL result {where} is {actual}, but output_type declares {schema}; cast it in the "
            "SQL if that is meant (DuckDB types a bare NULL or [] as integers)"
        )


def _member_types(type: DuckDBPyType) -> dict[str, DuckDBPyType]:
    """A struct's field types by name, or a list's element type as ``child``. (DuckDB lists a
    decimal's width and scale among its children too, as ints.)"""
    return {name: member for name, member in type.children if isinstance(member, DuckDBPyType)}
