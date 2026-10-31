"""ArrayHandles: lazy arrays inside a workflow.

A *handle* stands for an array that a workflow has arranged to have computed, before it has been
computed. Its :class:`~fxtr.entity_defns.array.ArrayType` is known as soon as the handle exists —
every :class:`~fxtr.experiment.workflows.WorkflowContext` method derives the type of the handle it
returns from its inputs' types synchronously — so a workflow can be type-checked as it describes
its DAG, and only later does the work actually run. Awaiting :meth:`ArrayHandle.observe` waits
for the array itself, which is how a workflow makes its DAG depend on computed values.

:meth:`ArrayHandle.query` starts an :class:`ArrayHandleQuery`, a query the workflow builds over its
handles (as does :func:`fxtr.sql` over handles). Building one records nothing;
:meth:`ArrayHandleQuery.collect` records the whole chain as one query node of the DAG, which the
host runs over its inputs' arrays, and returns the handle to it. So a workflow can reduce a large
array without holding its rows, and the nodes it records are exactly the queries it collected.

ArrayHandles are produced by a workflow context, never constructed directly; the runner's
implementation is :class:`fxtr.runner.contexts.RunnerArrayHandle`.
"""

from __future__ import annotations

import abc
from collections.abc import Mapping
from typing import Annotated, Any

from frozendict import frozendict

from fxtr.entity_defns.array import (
    Array,
    ArrayType,
    ArrayTypeError,
    ArrayTypeSpec,
    Dims,
    KeyValue,
    PartialArrayTypeSpec,
    as_array_type,
    as_partial_array_type,
    is_assignable_array,
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
from fxtr.entity_defns.query_plan import PlannedQuery
from fxtr.entity_defns.schema import Schema


class ArrayHandle[V](abc.ABC):
    """A handle to a lazily-computed array in a workflow.

    The type is known as soon as the handle exists, before the array is computed; it is what the
    workflow context checks signatures against while the workflow describes its DAG. ``V`` is the
    Python type of the array's presented values, as for
    :class:`~fxtr.entity_defns.array.Array`.

    :meth:`query` builds a query over the handle, which :meth:`ArrayHandleQuery.collect` records
    as a node::

        selected = rows.query().filter(value().field("score") >= 0.8).collect()
        summary = await selected.query().aggregate({"rows": count()}).collect().item()

    SQL over handles is :func:`fxtr.sql`.
    """

    @property
    @abc.abstractmethod
    def type(self) -> ArrayType:
        """The type of the array this handle stands for."""

    @property
    def dims(self) -> Dims:
        return self.type.dims

    @property
    def schema(self) -> Schema:
        return self.type.schema

    def expect_type(self, type: PartialArrayTypeSpec) -> ArrayHandle[V]:
        """Assert this handle's type and return the handle, so the assertion can be written inline
        on the call that produced it.

        Behaves like :meth:`~fxtr.entity_defns.array.Array.expect_type`, and is checked at the
        same time the handle's type is derived: while the workflow describes its DAG, before
        anything runs. When the spec gives a schema, the returned handle carries its presentation
        (loaders, TypedDicts, list forms), so observing it yields values in that form.

        Raises:
            ArrayTypeError: If the handle's type is not the expected one.
        """
        expected = as_partial_array_type(type)
        expected.check(self.type, "ArrayHandle")
        if expected.schema is None:
            return self
        return self.retype(ArrayType(expected.dims, expected.schema))

    def retype(self, type: ArrayTypeSpec) -> ArrayHandle[Any]:
        """A handle to the same array declared as ``type``, which this handle's type must be
        assignable to (see :func:`~fxtr.entity_defns.array.is_assignable_array`); observing it
        yields the array retyped accordingly (:meth:`~fxtr.entity_defns.array.Array.retype`), so
        the dims come in ``type``'s order and values present as ``type``'s schema says. A type
        that differs from this handle's by more than presentation records a query node at once;
        one that differs only in presentation names the same node.

        Raises:
            ArrayTypeError: If this handle's type is not assignable to ``type``.
        """
        target = as_array_type(type)
        if not is_assignable_array(self.type, target):
            raise ArrayTypeError(f"Cannot retype a handle of type {self.type} as {target}")
        return self._retyped(target)

    @abc.abstractmethod
    def _retyped(self, type: ArrayType) -> ArrayHandle[Any]:
        """Build the handle :meth:`retype` returns; ``type`` has already been checked."""

    @abc.abstractmethod
    async def observe(self) -> Array[V]:
        """Wait for and return the array."""

    async def item(self) -> V:
        """Wait for a scalar (no dims) array and return its single value."""
        return (await self.observe()).item()

    def query(self) -> ArrayHandleQuery[V]:
        """Start a query over this handle's array; nothing is recorded until it is collected.

        The shortcuts below (:meth:`filter`, :meth:`select`, ...) start from here, as
        :class:`~fxtr.entity_defns.array.Array`'s do, and return the query, not a handle.
        """
        return ArrayHandleQuery(PlannedQuery.over(self, self.type))

    def filter(self, predicate: Condition) -> ArrayHandleQuery[V]:
        """Start a query keeping the rows where ``predicate`` is true; see
        :meth:`ArrayHandleQuery.filter`.

        To take the rows at given dim values and drop those dims, use :meth:`sel`.
        """
        return self.query().filter(predicate)

    def field(self, name: str) -> ArrayHandleQuery[Any]:
        """Start a query replacing each value with its struct field ``name``; see
        :meth:`ArrayHandleQuery.field`."""
        return self.query().field(name)

    def sel(self, key: Mapping[str, KeyValue]) -> ArrayHandleQuery[V]:
        """Start a query keeping the rows at the given dim values and dropping those dims; see
        :meth:`ArrayHandleQuery.sel`."""
        return self.query().sel(key)

    def rename(self, names: Mapping[str, str]) -> ArrayHandleQuery[V]:
        """Start a query renaming dims, old name to new name; see
        :meth:`ArrayHandleQuery.rename`."""
        return self.query().rename(names)

    def select(
        self, fields: Mapping[str, ValueExpression] | ValueExpression
    ) -> ArrayHandleQuery[Any]:
        """Start a query replacing each row value with one expression, or with a struct of named
        ones; see :meth:`ArrayHandleQuery.select`.

        This computes values; it is unrelated to :meth:`sel`, which slices along dims. To take
        one struct field, use :meth:`field`.
        """
        return self.query().select(fields)

    def limit(
        self,
        count: int,
        *,
        order_by: OrderBy = (),
        ties: TiePolicy = "first",
    ) -> ArrayHandleQuery[V]:
        """Start a query keeping the first ``count`` rows under ``order_by``; see
        :meth:`ArrayHandleQuery.limit`."""
        return self.query().limit(count, order_by=order_by, ties=ties)

    def aggregate(
        self,
        aggregations: Mapping[str, Aggregation],
        *,
        group_by: Mapping[str, ValueExpression] = frozendict(),
    ) -> ArrayHandleQuery[Any]:
        """Start a query aggregating all rows, or each ``group_by`` group; see
        :meth:`ArrayHandleQuery.aggregate`."""
        return self.query().aggregate(aggregations, group_by=group_by)

    def join(
        self,
        other: ArrayHandle[Any] | ArrayHandleQuery[Any],
        *,
        on: JoinOn,
        left_name: str = "left",
        right_name: str = "right",
    ) -> ArrayHandleQuery[Any]:
        """Start a query inner-joining with another handle or query on equal columns; see
        :meth:`ArrayHandleQuery.join`."""
        return self.query().join(other, on=on, left_name=left_name, right_name=right_name)

    @abc.abstractmethod
    def _stored(self, query: PlannedQuery[ArrayHandle[Any]]) -> ArrayHandle[Any]:
        """Record ``query``, whose sources include this handle, as a node, and return its handle.

        Raises:
            ForeignArrayHandleError: If a source is not a handle from this handle's attempt.
        """


class ArrayHandleQuery[V]:
    """A query a workflow builds over its handles, recorded as a node only when collected.

    It mirrors :class:`~fxtr.duckdb_engine.local_query.ArrayQuery`, the query over concrete
    arrays: each method type-checks one more operation at once and returns the extended query.
    :meth:`collect` records the whole chain as one query node and returns a handle to it; the
    host runs the query over its inputs' arrays once they have values. A query
    collected twice is recorded twice, as one node: equal plans over equal inputs are the same
    node. To run a shared part once and build on its result, collect it and query the handle
    that returns.
    """

    def __init__(self, query: PlannedQuery[ArrayHandle[Any]]) -> None:
        self._query = query

    @property
    def type(self) -> ArrayType:
        """The result's array type, known without running the query."""
        return self._query.type

    @property
    def dims(self) -> Dims:
        return self.type.dims

    @property
    def schema(self) -> Schema:
        return self.type.schema

    def expect_type(self, type: PartialArrayTypeSpec) -> ArrayHandleQuery[V]:
        """Assert this query's type and return the query, as :meth:`ArrayHandle.expect_type`
        does for a handle.

        Raises:
            ArrayTypeError: If the query's type is not the expected one.
        """
        expected = as_partial_array_type(type)
        expected.check(self.type, "ArrayHandleQuery")
        if expected.schema is None:
            return self
        return self.retype(ArrayType(expected.dims, expected.schema))

    def retype(self, type: ArrayTypeSpec) -> ArrayHandleQuery[Any]:
        """This query declared as ``type``, as :meth:`ArrayHandle.retype` does for a handle.

        Raises:
            ArrayTypeError: If the query's type is not assignable to ``type``.
        """
        return ArrayHandleQuery(self._query.retype(as_array_type(type)))

    def filter(self, predicate: Condition) -> ArrayHandleQuery[V]:
        """Keep only the rows where ``predicate`` is true.

        A predicate that is null for a row (a comparison with a null operand) drops the row; see
        :mod:`fxtr.entity_defns.query`.

        Raises:
            ArrayTypeError: If the predicate does not fit this query's type.
            QueryError: If ``predicate`` is not a query predicate.
        """
        return ArrayHandleQuery(self._query.filter(predicate))

    def select(
        self, fields: Mapping[str, ValueExpression] | ValueExpression
    ) -> ArrayHandleQuery[Any]:
        """Replace the complete value with one expression, or with a struct of named ones.

        Raises:
            ArrayTypeError: If an expression does not fit this query's type.
            QueryError: If an expression is an untyped null.
        """
        return ArrayHandleQuery(self._query.select(fields))

    def field(self, name: str) -> ArrayHandleQuery[Any]:
        """Replace each value with its struct field ``name``, keeping the dims (see
        :func:`~fxtr.entity_defns.array.field_type`).

        Raises:
            ArrayTypeError: If the schema is not a struct or has no such field.
        """
        return ArrayHandleQuery(self._query.field(name))

    def sel(self, key: Mapping[str, KeyValue]) -> ArrayHandleQuery[V]:
        """Keep the rows at the given dim values and drop those dims (see
        :func:`~fxtr.entity_defns.array.sel_type`). Fixing every dim leaves one value, so the
        result must hold exactly one row.

        Raises:
            ArrayTypeError: If a named dim does not exist or a value is not of its dim's kind.
        """
        return ArrayHandleQuery(self._query.sel(key))

    def rename(self, names: Mapping[str, str]) -> ArrayHandleQuery[V]:
        """Rename dims, old name to new name; rows and values are unchanged (see
        :func:`~fxtr.entity_defns.array.rename_type`).

        Raises:
            ArrayTypeError: If an old name does not exist, or the renamed dims are not unique.
        """
        return ArrayHandleQuery(self._query.rename(names))

    def limit(
        self,
        count: int,
        *,
        order_by: OrderBy = (),
        ties: TiePolicy = "first",
    ) -> ArrayHandleQuery[V]:
        """Keep the first ``count`` rows under ``order_by``.

        Rows that ``order_by`` does not separate are taken in dimension-key order, so the result
        is deterministic. ``ties="all"`` also keeps every row tied with the last one kept. The
        result is an array like any other, keyed and ordered by its dimensions: the ordering
        chooses the rows and is not recorded in them.

        Raises:
            ArrayTypeError: If a sort expression is not a scalar of this query's type.
            QueryError: If ``count`` is negative, or ``ties="all"`` is given without ``order_by``.
        """
        return ArrayHandleQuery(self._query.limit(count, order_by=order_by, ties=ties))

    def aggregate(
        self,
        aggregations: Mapping[str, Aggregation],
        *,
        group_by: Mapping[str, ValueExpression] = frozendict(),
    ) -> ArrayHandleQuery[Any]:
        """Compute named aggregations, over all rows or over each group.

        The ``group_by`` names become the result's dimensions, in the order given.

        Raises:
            ArrayTypeError: If a group is not a non-null int or str expression, or an aggregation
                does not suit its expression's type.
        """
        return ArrayHandleQuery(self._query.aggregate(aggregations, group_by=group_by))

    def join(
        self,
        other: ArrayHandle[Any] | ArrayHandleQuery[Any],
        *,
        on: JoinOn,
        left_name: str = "left",
        right_name: str = "right",
    ) -> ArrayHandleQuery[Any]:
        """Inner equality join with a handle or another query of the same workflow attempt.

        The result's value is a struct holding this query's value under ``left_name`` and
        ``other``'s under ``right_name``; see :func:`~fxtr.entity_defns.query.join_dimensions` for
        its dimensions.

        Raises:
            ArrayTypeError: If a key column is missing, not scalar, or the pair cannot be equal.
            QueryError: If ``on`` is empty, the two names are not distinct, or ``other`` is not
                a handle or a handle query.
        """
        right = handle_plan(other, "A handle query's join partners")
        return ArrayHandleQuery(
            self._query.join(right, on, left_name=left_name, right_name=right_name)
        )

    def collect(self) -> ArrayHandle[V]:
        """Record this query as one node of the workflow's DAG and return the handle to it.

        Nothing runs here: the host runs the query once its inputs have values, as it computes
        any other node. Observe the returned handle to wait for the rows. The result is not
        stored for its own sake: a resumed attempt and a later job run the query again, on one
        DuckDB thread so that it repeats, but SQL that asks for varying values (``random()``,
        ``now()``) answers differently there. The documentation's array query reference
        (``docs/fxtr/reference/array-queries.mdx``, "Keep query nodes stable") says what that
        breaks.

        Raises:
            ForeignArrayHandleError: If the query reads handles from another workflow attempt.
        """
        return self._query.sources[0]._stored(self._query)

    def __repr__(self) -> str:
        return f"ArrayHandleQuery(type={self.type}, plan={self._query.plan!r})"


def handle_plan(table: object, what: str) -> PlannedQuery[ArrayHandle[Any]]:
    """``table``, a handle or a query over handles, as the plan a query built from it reads.

    Raises:
        QueryError: If ``table`` is neither, such as a concrete array; ``what`` names what it was
            given as.
    """
    if isinstance(table, ArrayHandleQuery):
        return table._query
    if isinstance(table, ArrayHandle):
        return PlannedQuery.over(table, table.type)
    raise QueryError(
        f"{what} must be handles or handle queries of this workflow attempt, got "
        f"{described(table)}; a concrete array joins only with concrete arrays and local queries"
    )


type ScalarHandle[X] = Annotated[ArrayHandle[X], "[]"]
"""A handle to a scalar (no-dims) array: ``ScalarHandle[X]`` stands for
``Annotated[ArrayHandle[X], "[]"]``, the dims-less array annotation spelled once (the annotation
reader expands the alias; see :func:`~fxtr.entity_defns.schema.expand_type_alias`). Use it in a
workflow function's signature, e.g. ``model: ScalarHandle[BoundID[ModelConfig]]``."""
