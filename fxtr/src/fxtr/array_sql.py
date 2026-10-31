"""SQL over named fxtr arrays: :func:`sql`, exported as ``fxtr.sql``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast, overload

from fxtr.entity_defns.array import Array, ArrayTypeSpec, as_array_type
from fxtr.entity_defns.query import QueryError
from fxtr.entity_defns.query_plan import PlannedQuery
from fxtr.entity_defns.sql import SQLParameters
from fxtr.experiment.handles import ArrayHandle, ArrayHandleQuery, handle_plan

if TYPE_CHECKING:
    import anyio

    from fxtr.duckdb_engine.executor import DuckDBExecutionLimits
    from fxtr.duckdb_engine.local_query import ArrayQuery

type LocalSQLTable = Array[Any] | ArrayQuery[Any]
type HandleSQLTable = ArrayHandle[Any] | ArrayHandleQuery[Any]


@overload
def sql(
    text: str,
    *,
    tables: Mapping[str, LocalSQLTable],
    output_type: ArrayTypeSpec | None = None,
    parameters: SQLParameters = (),
    limits: DuckDBExecutionLimits | None = None,
    query_limiter: anyio.CapacityLimiter | None = None,
) -> ArrayQuery[Any]: ...


@overload
def sql(
    text: str,
    *,
    tables: Mapping[str, HandleSQLTable],
    output_type: ArrayTypeSpec | None = None,
    parameters: SQLParameters = (),
) -> ArrayHandleQuery[Any]: ...


def sql(
    text: str,
    *,
    tables: Mapping[str, LocalSQLTable] | Mapping[str, HandleSQLTable],
    output_type: ArrayTypeSpec | None = None,
    parameters: SQLParameters = (),
    limits: DuckDBExecutionLimits | None = None,
    query_limiter: anyio.CapacityLimiter | None = None,
) -> ArrayQuery[Any] | ArrayHandleQuery[Any]:
    """Build a lazy DuckDB query: one read-only ``SELECT`` over arrays the SQL reads by name.

    Each table has a ``dims`` struct column and a ``value`` column::

        fxtr.sql(
            "SELECT runs.dims, {score: runs.value.score, owner: models.value.owner} AS value "
            "FROM runs JOIN models ON runs.dims.model = models.dims.model "
            "WHERE runs.value.score >= $minimum",
            tables={"runs": runs, "models": models},
            output_type=("[model: str, run: int]", struct_of(score=float, owner=str)),
            parameters={"minimum": 0.8},
        )

    The ``SELECT`` returns ``dims`` and ``value`` matching ``output_type``; a result with no dims
    needs only ``value``. With exactly one table, ``output_type`` defaults to that table's type,
    which suits SQL that only chooses rows. SQL ``ORDER BY`` affects which rows a ``LIMIT`` or a
    window keeps, not the result's order: an array is ordered by its dimension keys. Parameters
    (``?``, ``$1``, ``$name``) are bound, never interpolated into ``text``.

    Where the query runs follows its tables, which must all be of one kind:

    * concrete arrays and local queries run in this process; ``limits`` and ``query_limiter``
      bound the execution (default: those of the root a table came from, if one was given any).
    * a workflow's handles and handle queries, all from one attempt, give an
      :class:`~fxtr.experiment.handles.ArrayHandleQuery`: collecting it records one node of the
      workflow's graph, which the host runs over its inputs' arrays on one DuckDB thread, with
      no deadline or result-size cap.

    Nothing is uploaded or downloaded to make the kinds agree: ``observe()`` a handle to query
    its rows locally.

    DuckDB accepts SQL whose answer can vary between runs. Across threads, a float sum,
    ``any_value()``, ``string_agg()`` or ``list()`` without ``ORDER BY``, and ``LIMIT`` without an
    ``ORDER BY`` that separates every row can; a workflow's queries run on one DuckDB thread, where
    they repeat. ``random()``, ``gen_random_uuid()``, and clock functions such as ``now()`` vary on
    any thread, and a workflow's query runs again on a resumed attempt and in a later job.

    Raises:
        QueryError: If ``tables`` is empty or mixes local arrays with handles, ``output_type`` is
            omitted with more than one table, a local-execution argument is given for handles, a
            table name is reserved or repeated, ``text`` is not one read-only ``SELECT`` that
            reads only ``tables``, or its placeholders do not match ``parameters``.
        ForeignArrayHandleError: When the query is collected, if its handles are not all from
            one attempt.
    """
    from fxtr.duckdb_engine.local_query import ArrayQuery, sql_query

    if not isinstance(tables, Mapping) or not tables:
        raise QueryError("fxtr.sql needs tables: a mapping from each table name to its array")
    given = tuple(tables.values())
    run_locally = all(isinstance(table, (Array, ArrayQuery)) for table in given)
    if not run_locally and not all(
        isinstance(table, (ArrayHandle, ArrayHandleQuery)) for table in given
    ):
        raise QueryError(
            "fxtr.sql tables must be all concrete arrays and local queries, or all handles and "
            "handle queries from one workflow attempt; observe() a handle to query it locally"
        )
    if output_type is not None:
        result_type = as_array_type(output_type)
    elif len(given) == 1:
        result_type = given[0].type
    else:
        raise QueryError(
            "fxtr.sql needs output_type when it reads more than one table; "
            "only a single table's type can be assumed for the result"
        )
    if run_locally:
        return sql_query(
            text,
            tables=cast("Mapping[str, LocalSQLTable]", tables),
            output_type=result_type,
            parameters=parameters,
            limits=limits,
            query_limiter=query_limiter,
        )
    if limits is not None or query_limiter is not None:
        raise QueryError(
            "limits and query_limiter configure a local query; a query over handles runs "
            "as a workflow node, which takes neither"
        )
    return ArrayHandleQuery(
        PlannedQuery.sql(
            text,
            tables={name: handle_plan(table, "fxtr.sql tables") for name, table in tables.items()},
            output_type=result_type,
            parameters=parameters,
        )
    )


__all__ = ["sql"]
