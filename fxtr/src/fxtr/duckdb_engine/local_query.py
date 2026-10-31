"""Array queries over concrete arrays: lazy local queries (:class:`ArrayQuery`), and
:func:`query_arrays`, which computes a workflow's query nodes with the same plans and DuckDB
engine."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import cache
from typing import Any, cast

import anyio
import pyarrow as pa
from frozendict import frozendict

from fxtr.duckdb_engine.executor import (
    DuckDBArrowExecutor,
    DuckDBExecutionLimits,
    arrow_table_of,
)
from fxtr.entity_defns.array import (
    Array,
    ArrayType,
    ArrayTypeSpec,
    Dims,
    KeyValue,
    PartialArrayTypeSpec,
    as_array_type,
    as_partial_array_type,
)
from fxtr.entity_defns.arrow import (
    array_arrow_table,
    array_from_validated_table,
    array_holds_rows,
)
from fxtr.entity_defns.query import (
    Aggregation,
    Condition,
    JoinOn,
    OrderBy,
    QueryError,
    TiePolicy,
    ValueExpression,
    described,
)
from fxtr.entity_defns.query_plan import (
    PlannedQuery,
    QueryPlan,
    row_preserving_source,
)
from fxtr.entity_defns.schema import Schema
from fxtr.entity_defns.sql import SQLParameters


@cache
def default_executor() -> DuckDBArrowExecutor:
    """The executor of every local query that sets no limits of its own.

    An executor keeps one idle DuckDB connection, about 3 MB, for its next query. One for the
    process keeps that cost fixed: one per query object, or per array, would hold a connection
    for every one a caller kept alive, about 900 MB for 300 queried arrays. Concurrent queries
    still get a connection each; only the idle one is shared.
    """
    return DuckDBArrowExecutor()


def _executor_of(queries: Sequence[ArrayQuery[Any]]) -> DuckDBArrowExecutor:
    """The executor a query combining ``queries`` runs on.

    Limits a root was given apply to whatever is built from it, whichever side of a join or
    whichever SQL table it is: dropping them would undo what the root asked for.

    Raises:
        QueryError: If the queries come from roots given different limits.
    """
    limited = [query._executor for query in queries if not query._executor.is_unlimited]
    if len({executor.limit_settings for executor in limited}) > 1:
        raise QueryError(
            "These queries come from roots with different limits; start them from one "
            "array.query(limits=...) root, or for SQL pass limits= to fxtr.sql"
        )
    return limited[0] if limited else queries[0]._executor


@cache
def _dag_executor() -> DuckDBArrowExecutor:
    """The executor :func:`query_arrays` runs on, which runs each query on one DuckDB thread.

    A query node runs again on every attempt of its workflow, and a step it feeds or a decision
    the workflow function makes on it must see the same answer each time; across threads DuckDB
    splits a reduction's work differently from run to run, so a floating-point sum can differ in
    its last digits.
    """
    return DuckDBArrowExecutor(limits=DuckDBExecutionLimits(threads=1))


def query_arrays(query: QueryPlan, inputs: Sequence[Array[Any]]) -> Array[Any]:
    """Execute ``query`` over concrete arrays on the calling thread, returning the result array.

    ``inputs[i]`` is what the plan's ``QuerySource(i)`` reads. Synchronous, because this is how a
    workflow DAG derives a node's value: a query node sits beside ``combine`` and ``stack``,
    which are plain array operations, and every value in the graph is a concrete array. Nothing
    is stored and no content ID is computed.

    Raises:
        QueryError: If the plan cannot be compiled, DuckDB cannot run it, or its result is not
            a valid array (a repeated key, a null dimension, ...).
        ArrayTypeError: If the plan is ill-typed for the inputs.
    """
    table, output_type = _dag_executor().execute_query_on_this_thread(
        query,
        [array.type for array in inputs],
        lambda: [array_arrow_table(array) for array in inputs],
    )
    # The result holds only its Arrow table: a chain of query nodes passes Arrow along, and a
    # node's rows are decoded only if something reads them.
    return array_from_validated_table(table, output_type)


def query_array[V](
    array: Array[V],
    *,
    limits: DuckDBExecutionLimits | None = None,
    query_limiter: anyio.CapacityLimiter | None = None,
) -> ArrayQuery[V]:
    """The lazy query that reads ``array`` unchanged; see :meth:`Array.query`."""
    executor = (
        default_executor()
        if limits is None and query_limiter is None
        else DuckDBArrowExecutor(limits=limits, query_limiter=query_limiter)
    )
    return ArrayQuery(PlannedQuery.over(cast("Array[Any]", array), array.type), executor)


def sql_query(
    text: str,
    *,
    tables: Mapping[str, ArrayQuery[Any] | Array[Any]],
    output_type: ArrayType,
    parameters: SQLParameters,
    limits: DuckDBExecutionLimits | None,
    query_limiter: anyio.CapacityLimiter | None,
) -> ArrayQuery[Any]:
    """The local query :func:`fxtr.sql` returns for concrete arrays and local queries.

    With ``limits`` or ``query_limiter`` the query gets an executor of its own. Without them it
    runs on the executor of its tables: the one a table was given limits on, else the shared one
    of its first table by name.

    Raises:
        QueryError: If a table is neither an array nor a local query, tables were given limits on
            different roots and none is given here, or :meth:`PlannedQuery.sql` rejects the query.
    """
    queries = {
        name: ArrayQuery._as_query(table, "Local SQL tables") for name, table in tables.items()
    }
    planned = PlannedQuery.sql(
        text,
        tables={name: query._query for name, query in queries.items()},
        output_type=output_type,
        parameters=parameters,
    )
    first, *others = (queries[name] for name in sorted(queries))
    if limits is not None or query_limiter is not None:
        executor = DuckDBArrowExecutor(limits=limits, query_limiter=query_limiter)
    else:
        executor = _executor_of([first, *others])
    return ArrayQuery(planned, executor)


class ArrayQuery[V]:
    """A lazy query over concrete arrays, executed in this process by DuckDB.

    Query methods only build checked plans, and ``collect()`` runs one, returning a concrete
    Array. A query holds a plan and no result: every ``collect()``
    executes the plan again. To use a result more than once, keep the array ``collect()``
    returned; it carries its Arrow table, so querying it converts nothing. Arrow conversion is
    cached on each immutable input. No content ID is computed and no CBOR or Arrow file is
    written.

    Queries derived from one root share an executor and its single idle native connection. Every
    query that sets no ``limits`` or ``query_limiter`` shares the process's one
    :func:`default_executor`; a root given either has an executor of its own.
    """

    def __init__(self, query: PlannedQuery[Array[Any]], executor: DuckDBArrowExecutor) -> None:
        self._query = query
        self._executor = executor

    @property
    def type(self) -> ArrayType:
        """The result's array type, known without executing the query."""
        return self._query.type

    @property
    def dims(self) -> Dims:
        """The result's dimensions."""
        return self.type.dims

    @property
    def schema(self) -> Schema:
        """The result's value schema."""
        return self.type.schema

    def _derived(
        self, query: PlannedQuery[Array[Any]], others: Sequence[ArrayQuery[Any]] = ()
    ) -> ArrayQuery[Any]:
        return ArrayQuery(query, _executor_of([self, *others]))

    @staticmethod
    def _as_query(other: object, what: str) -> ArrayQuery[Any]:
        if isinstance(other, Array):
            return cast("Array[Any]", other).query()
        if not isinstance(other, ArrayQuery):
            raise QueryError(
                f"{what} must be concrete arrays or local queries, got {described(other)}"
            )
        return cast("ArrayQuery[Any]", other)

    def expect_type(self, type: PartialArrayTypeSpec) -> ArrayQuery[V]:
        """Assert this query's type and return the query, as :meth:`Array.expect_type` does for
        an array; a schema in ``type`` gives the collected values its presentation.

        Raises:
            ArrayTypeError: If the query's type is not the expected one.
        """
        expected = as_partial_array_type(type)
        expected.check(self.type, "ArrayQuery")
        if expected.schema is None:
            return self
        return self.retype(ArrayType(expected.dims, expected.schema))

    def retype(self, type: ArrayTypeSpec) -> ArrayQuery[Any]:
        """This query declared as ``type``, as :meth:`Array.retype` does for an array: the dims
        come in ``type``'s order and the values present as its schema says.

        Raises:
            ArrayTypeError: If this query's type is not assignable to ``type``.
        """
        return self._derived(self._query.retype(as_array_type(type)))

    def filter(self, predicate: Condition) -> ArrayQuery[V]:
        """Build a lazy row filter, retaining only rows where ``predicate`` is true.

        A predicate that is null for a row (a comparison with a null operand) drops the row; see
        :mod:`fxtr.entity_defns.query`.

        Raises:
            ArrayTypeError: If the predicate does not fit this query's type.
            QueryError: If ``predicate`` is not a query predicate.
        """
        return self._derived(self._query.filter(predicate))

    def select(
        self,
        fields: Mapping[str, ValueExpression] | ValueExpression,
    ) -> ArrayQuery[Any]:
        """Replace the complete value or select named expressions into a struct value.

        Raises:
            ArrayTypeError: If an expression does not fit this query's type.
            QueryError: If an expression is an untyped null.
        """
        return self._derived(self._query.select(fields))

    def field(self, name: str) -> ArrayQuery[Any]:
        """Replace each value with its struct field ``name``, keeping the dims (see
        :func:`~fxtr.entity_defns.array.field_type`).

        Raises:
            ArrayTypeError: If the value is not a struct with that field.
        """
        return self._derived(self._query.field(name))

    def sel(self, key: Mapping[str, KeyValue]) -> ArrayQuery[V]:
        """Keep the rows at the given dim values and drop those dims (see
        :func:`~fxtr.entity_defns.array.sel_type`). Fixing every dim leaves one value, so the
        result must hold exactly one row.

        Raises:
            ArrayTypeError: If a named dim does not exist, or a value is not of its
                dim's kind.
        """
        return self._derived(self._query.sel(key))

    def rename(self, names: Mapping[str, str]) -> ArrayQuery[V]:
        """Rename dims, old name to new name; rows and values are unchanged (see
        :func:`~fxtr.entity_defns.array.rename_type`).

        Raises:
            ArrayTypeError: If an old name does not exist, or the renamed dims are not unique.
            QueryError: If two of the renamed dims differ only by case.
        """
        return self._derived(self._query.rename(names))

    def limit(
        self,
        count: int,
        *,
        order_by: OrderBy = (),
        ties: TiePolicy = "first",
    ) -> ArrayQuery[V]:
        """Keep the first ``count`` rows under ``order_by``.

        Rows that ``order_by`` does not separate are taken in dimension-key order, so the result
        is deterministic. ``ties="all"`` also keeps every row tied with the last one kept. The
        result is an array like any other, keyed and ordered by its dimensions: the ordering
        chooses the rows and is not recorded in them.

        Raises:
            ArrayTypeError: If a sort expression is not a scalar of this query's type.
            QueryError: If ``count`` is negative, or ``ties="all"`` is given without ``order_by``.
        """
        return self._derived(self._query.limit(count, order_by=order_by, ties=ties))

    def aggregate(
        self,
        aggregations: Mapping[str, Aggregation],
        *,
        group_by: Mapping[str, ValueExpression] = frozendict(),
    ) -> ArrayQuery[Any]:
        """Compute named aggregations, over all rows or over each group.

        The ``group_by`` names become the result's dimensions, in the order given.

        Raises:
            ArrayTypeError: If a group is not a non-null int or str expression, or an aggregation
                does not suit its expression's type.
        """
        return self._derived(self._query.aggregate(aggregations, group_by=group_by))

    def join(
        self,
        other: ArrayQuery[Any] | Array[Any],
        *,
        on: JoinOn,
        left_name: str = "left",
        right_name: str = "right",
    ) -> ArrayQuery[Any]:
        """Inner equality join with a concrete array or another local query.

        The result's value is a struct holding this query's value under ``left_name`` and
        ``other``'s under ``right_name``; see :func:`~fxtr.entity_defns.query.join_dimensions` for
        its dimensions.

        Raises:
            ArrayTypeError: If a key column is missing, not scalar, or the pair cannot be equal.
            QueryError: If ``on`` is empty, the two names are not distinct, or ``other`` is
                neither an array nor a local query.
        """
        right = self._as_query(other, "A local array query's join partners")
        planned = self._query.join(right._query, on, left_name=left_name, right_name=right_name)
        return self._derived(planned, [right])

    async def _run(self) -> Array[Any]:
        """Execute the plan once and return its result.

        A plan that reads one array unchanged is that array, unless a result cap is set: the cap
        applies to it like any other result, and DuckDB is what enforces it.
        """
        limits = self._executor.limits
        uncapped = limits.max_result_rows is None and limits.max_result_bytes is None
        if self._query.is_source and uncapped:
            return self._query.sources[0]
        return await self._execute()

    async def _execute(self) -> Array[Any]:
        sources = self._query.sources

        async def prepare() -> Sequence[pa.Table]:
            return [await arrow_table_of(source) for source in sources]

        row_source = self._row_source()
        table, _ = await self._executor.execute_query(
            self._query.plan,
            [source.type for source in sources],
            prepare,
            reuse_rows_from=row_source,
        )
        return array_from_validated_table(table, self.type, row_source=row_source)

    def _row_source(self) -> Array[Any] | None:
        """The input whose rows the result's are a subset of, when the plan only drops rows and
        that input holds Python rows to share (see :func:`array_from_validated_table`)."""
        if len(self._query.sources) != 1 or row_preserving_source(self._query.plan) != 0:
            return None
        source = self._query.sources[0]
        return source if source.type == self.type and array_holds_rows(source) else None

    async def collect(self) -> Array[V]:
        """Execute locally and return the result as a concrete Array.

        Each call executes the query again and returns a new array. The array holds DuckDB's
        Arrow result and no Python rows: a query over it reads that table, and its rows are
        decoded only when something reads them. A query that reads one array unchanged executes
        nothing and returns that array, unless its limits set a result cap.

        Raises:
            QueryError: If DuckDB cannot run the query (a cast failed on some row, ...), or its
                result is not a valid array (a repeated key, a null dimension, ...).
            DuckDBResourceLimitError: If a configured limit was exceeded.
        """
        array = await self._run()
        if array.dims != self.dims or array.schema is not self.schema:
            array = array.retype(self.type)
        return cast("Array[V]", array)

    def __repr__(self) -> str:
        return f"ArrayQuery(type={self.type}, plan={self._query.plan!r})"
