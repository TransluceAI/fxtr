"""Immutable array query plans, from the query builder or SQL, for local queries and workflows.

A plan is a tree of the node classes below. Its leaves are :class:`QuerySource` nodes, which name
an input array by position; whoever holds the plan also holds the list those positions index.
:class:`PlannedQuery` keeps the two together, along with the result type derived while the plan
was built, and is what local queries and a workflow's array handles wrap.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from typing import Self

from frozendict import frozendict

from fxtr.entity_defns.array import ArrayType, KeyValue
from fxtr.entity_defns.query import (
    Aggregation,
    Column,
    Condition,
    JoinKey,
    JoinOn,
    OrderBy,
    Predicate,
    QueryError,
    SortField,
    StructurallyEqual,
    Then,
    TiePolicy,
    ValueExpression,
    aggregate_type,
    as_join_keys,
    as_predicate,
    as_sort_fields,
    as_value_expression,
    described,
    filter_type,
    join_type,
    limit_type,
    rename_query_type,
    require_distinct_names_ignoring_case,
    retype_query_type,
    sel_query_type,
    select_type,
    select_value_type,
    within_recursion_limit,
)
from fxtr.entity_defns.schema import FrozenValue
from fxtr.entity_defns.sql import (
    SQLParameters,
    bind_sql,
    normalize_sql_parameters,
    validate_sql_reads,
    validate_sql_table_names,
)


@dataclasses.dataclass(frozen=True, eq=False)
class QuerySource(StructurallyEqual):
    """One source array, by its position in a query's input list."""

    index: int


@dataclasses.dataclass(frozen=True, eq=False)
class QueryFilter(StructurallyEqual):
    """The input's rows for which ``predicate`` is true (not false, and not null)."""

    input: QueryPlan
    predicate: Predicate


@dataclasses.dataclass(frozen=True, eq=False)
class QuerySelect(StructurallyEqual):
    """Replace each row value with a struct of named expressions, retaining the dimensions."""

    input: QueryPlan
    fields: frozendict[str, ValueExpression]


@dataclasses.dataclass(frozen=True, eq=False)
class QuerySelectValue(StructurallyEqual):
    """Replace each row value with one expression while retaining its dimensions."""

    input: QueryPlan
    expression: ValueExpression


@dataclasses.dataclass(frozen=True, eq=False)
class QuerySel(StructurallyEqual):
    """The input's rows at the given dim values, without those dims, as ``Array.sel`` gives."""

    input: QueryPlan
    key: frozendict[str, KeyValue]


@dataclasses.dataclass(frozen=True, eq=False)
class QueryRename(StructurallyEqual):
    """The input with dims renamed, old name to new name, as ``Array.rename`` gives."""

    input: QueryPlan
    names: frozendict[str, str]


@dataclasses.dataclass(frozen=True, eq=False)
class QueryRetype(StructurallyEqual):
    """The input's rows under a type it is assignable to, as ``Array.retype`` gives.

    ``type`` may order the dims differently or widen the schema, both of which change the
    result's Arrow layout and content ID, so whoever runs the plan has to produce it; a change of
    presentation alone needs no node.
    """

    input: QueryPlan
    type: ArrayType


@dataclasses.dataclass(frozen=True, eq=False)
class QueryLimit(StructurallyEqual):
    """The first ``count`` rows under ``order_by``; ``ties="all"`` also keeps rows tied with the
    last one. Rows that ``order_by`` does not separate are ordered by their dimension keys."""

    input: QueryPlan
    count: int
    order_by: tuple[SortField, ...]
    ties: TiePolicy


@dataclasses.dataclass(frozen=True, eq=False)
class QueryAggregate(StructurallyEqual):
    """Named aggregations over the whole input, or over each group of ``group_by``.

    ``group_by`` is a tuple of pairs rather than a mapping because its order matters: the names
    become the result's dimensions in that order.
    """

    input: QueryPlan
    aggregations: frozendict[str, Aggregation]
    group_by: tuple[tuple[str, ValueExpression], ...]

    def __post_init__(self) -> None:
        names = [name for name, _ in self.group_by]
        if len(set(names)) != len(names):
            raise QueryError(f"Group names must be distinct, got {names}")


@dataclasses.dataclass(frozen=True, eq=False)
class QueryJoin(StructurallyEqual):
    """The inner equality join of two plans; see :func:`~fxtr.entity_defns.query.join_type`."""

    left: QueryPlan
    right: QueryPlan
    on: tuple[JoinKey, ...]
    left_name: str
    right_name: str


@dataclasses.dataclass(frozen=True, eq=False)
class QuerySQL(StructurallyEqual):
    """One read-only user query over named, lazy array inputs and a declared result type.

    Every way of making one, including decoding a workflow's query node, passes through the checks
    here: the table names, the parameter names and values, and that ``text`` is a single
    ``SELECT`` whose placeholders match ``parameters`` and which reads only ``inputs``.
    """

    inputs: frozendict[str, QueryPlan]
    text: str
    parameters: frozendict[str, FrozenValue]
    output_type: ArrayType

    def __post_init__(self) -> None:
        if not self.inputs:
            raise QueryError("Array SQL needs at least one named table")
        validate_sql_table_names(tuple(self.inputs))
        object.__setattr__(self, "parameters", normalize_sql_parameters(self.parameters))
        bind_sql(self.text, self.parameters)
        validate_sql_reads(self.text, self.inputs)


type QueryPlan = (
    QuerySource
    | QueryFilter
    | QuerySelect
    | QuerySelectValue
    | QuerySel
    | QueryRename
    | QueryRetype
    | QueryLimit
    | QueryAggregate
    | QueryJoin
    | QuerySQL
)


def require_source_index(index: object, count: int) -> int:
    """``index`` if it names one of ``count`` supplied input arrays.

    A plan can arrive from a worker, so the index is checked wherever one is followed, not
    trusted from the plan's own construction.

    Raises:
        QueryError: If it is not an int in range. A bool is not an index.
    """
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < count:
        raise QueryError(f"Query source {index!r} is outside the {count} supplied inputs")
    return index


@within_recursion_limit
def query_type(query: QueryPlan, source_types: Sequence[ArrayType]) -> ArrayType:
    """The type of ``query``'s result, given the type of the array each ``QuerySource`` indexes.

    Every operation's own type function applied to its inputs' types, the rule a plan's type
    follows wherever it is derived: :class:`PlannedQuery` applies these functions as a query is
    built, and the DuckDB compiler applies them again as it emits SQL. This derives the same type
    from a finished plan, for a caller holding one with nothing to run it on — a workflow query
    node records its type before anything executes.

    Raises:
        QueryError: If a source index does not name one of ``source_types``.
        ArrayTypeError: If an operation is ill-typed for its input.
    """

    def derived(query: QueryPlan) -> ArrayType:
        return query_type(query, source_types)

    match query:
        case QuerySource(index=index):
            return source_types[require_source_index(index, len(source_types))]
        case QueryFilter(input=input, predicate=predicate):
            return filter_type(derived(input), predicate)
        case QuerySelect(input=input, fields=fields):
            return select_type(derived(input), fields)
        case QuerySelectValue(input=input, expression=expression):
            return select_value_type(derived(input), expression)
        case QuerySel(input=input, key=key):
            return sel_query_type(derived(input), key)
        case QueryRename(input=input, names=names):
            return rename_query_type(derived(input), names)
        case QueryRetype(input=input, type=target):
            return retype_query_type(derived(input), target)
        case QueryLimit(input=input, count=count, order_by=order_by, ties=ties):
            return limit_type(derived(input), count, order_by, ties)
        case QueryAggregate(input=input, aggregations=aggregations, group_by=group_by):
            return aggregate_type(derived(input), aggregations, dict(group_by))
        case QueryJoin(left=left, right=right, on=on, left_name=name, right_name=right_name):
            return join_type(derived(left), derived(right), on, name, right_name)
        case QuerySQL(inputs=inputs, output_type=output_type):
            # Declared, not derived: SQL text has no type the array model can read. The sources
            # its tables read are still followed, so an unusable index is refused here too.
            for table in inputs.values():
                derived(table)
            require_distinct_names_ignoring_case(output_type)
            return output_type
        case _:
            raise QueryError(f"Unknown array query {query!r}")


def row_preserving_source(query: QueryPlan) -> int | None:
    """The index of the source whose rows ``query`` returns a subset of, unchanged.

    Filters and limits only drop rows, and selecting the whole value keeps it as it was. Any
    other operation changes values or dimensions, and the answer is ``None``.
    """
    while True:
        match query:
            case QuerySource(index=index):
                return index
            case QueryFilter(input=input) | QueryLimit(input=input):
                query = input
            case QuerySelectValue(input=input, expression=Column(source="value", path=())):
                query = input
            case _:
                return None


def shift_query_sources(query: QueryPlan, offset: int) -> QueryPlan:
    """Return ``query`` with every source index increased by ``offset``."""
    if offset < 0:
        raise ValueError(f"Source offset must be non-negative, got {offset}")
    return replace_query_sources(query, lambda index: QuerySource(index + offset))


def replace_query_sources(query: QueryPlan, replacement: Callable[[int], QueryPlan]) -> QueryPlan:
    """Return ``query`` with each ``QuerySource(i)`` replaced by ``replacement(i)``."""

    def replaced(query: QueryPlan) -> QueryPlan:
        match query:
            case QuerySource(index=index):
                return replacement(index)
            case QueryFilter(input=input, predicate=predicate):
                return QueryFilter(replaced(input), predicate)
            case QuerySelect(input=input, fields=fields):
                return QuerySelect(replaced(input), fields)
            case QuerySelectValue(input=input, expression=expression):
                return QuerySelectValue(replaced(input), expression)
            case QuerySel(input=input, key=key):
                return QuerySel(replaced(input), key)
            case QueryRename(input=input, names=names):
                return QueryRename(replaced(input), names)
            case QueryRetype(input=input, type=type):
                return QueryRetype(replaced(input), type)
            case QueryLimit(input=input, count=count, order_by=order_by, ties=ties):
                return QueryLimit(replaced(input), count, order_by, ties)
            case QueryAggregate(input=input, aggregations=aggregations, group_by=group_by):
                return QueryAggregate(replaced(input), aggregations, group_by)
            case QueryJoin(
                left=left, right=right, on=on, left_name=left_name, right_name=right_name
            ):
                return QueryJoin(replaced(left), replaced(right), on, left_name, right_name)
            case QuerySQL(inputs=inputs, text=text, parameters=parameters, output_type=output_type):
                return QuerySQL(
                    frozendict({name: replaced(input) for name, input in inputs.items()}),
                    text,
                    parameters,
                    output_type,
                )
            case _:
                raise QueryError(f"Unknown array query {query!r}")

    return replaced(query)


@dataclasses.dataclass(frozen=True)
class PlannedQuery[S]:
    """A plan, the arrays it reads, and its checked result type; no rows, and nowhere to run.

    ``sources[i]`` is what ``QuerySource(i)`` reads. ``S`` is whatever names an array where the
    query is built: a concrete array for a local query, a workflow's
    :class:`~fxtr.experiment.handles.ArrayHandle`, or the token of the node a handle refers to
    when a workflow records the query as a node. Each method
    type-checks one more operation and returns the extended query; combining two queries
    appends the other's sources and renumbers its plan to match.
    """

    sources: tuple[S, ...]
    plan: QueryPlan
    type: ArrayType

    @classmethod
    def over(cls, source: S, type: ArrayType) -> Self:
        """The query that reads ``source``, an array of ``type``, unchanged."""
        return cls((source,), QuerySource(0), type)

    @property
    def is_source(self) -> bool:
        """Whether this query is one source read unchanged, so there is nothing to execute."""
        return len(self.sources) == 1 and self.plan == QuerySource(0)

    def _extended(self, plan: QueryPlan, type: ArrayType) -> PlannedQuery[S]:
        return PlannedQuery(self.sources, plan, type)

    def _combined(self, other: PlannedQuery[S]) -> tuple[tuple[S, ...], QueryPlan]:
        """This query's sources followed by ``other``'s, and ``other``'s plan renumbered."""
        return (*self.sources, *other.sources), shift_query_sources(other.plan, len(self.sources))

    def filter(self, predicate: Condition) -> PlannedQuery[S]:
        predicate = as_predicate(predicate)
        return self._extended(QueryFilter(self.plan, predicate), filter_type(self.type, predicate))

    def select(self, fields: Mapping[str, ValueExpression] | ValueExpression) -> PlannedQuery[S]:
        if isinstance(fields, ValueExpression):
            return self._extended(
                QuerySelectValue(self.plan, fields), select_value_type(self.type, fields)
            )
        if isinstance(fields, Then):
            raise QueryError("A when(...).then(...) needs .otherwise(...) before it is selected")
        if not isinstance(fields, Mapping):
            raise QueryError(
                "select() takes a query expression or a mapping of field names to expressions, "
                f"got {described(fields)}"
            )
        frozen = frozendict(
            {
                name: as_value_expression(field, f"Selected field {name!r}")
                for name, field in fields.items()
            }
        )
        return self._extended(QuerySelect(self.plan, frozen), select_type(self.type, frozen))

    def field(self, name: str) -> PlannedQuery[S]:
        """Replace each row value with its struct field ``name``, as ``Array.field`` does."""
        return self.select(Column("value", (name,)))

    def sel(self, key: Mapping[str, KeyValue]) -> PlannedQuery[S]:
        """Keep the rows at the given dim values and drop those dims, as ``Array.sel`` does."""
        if not isinstance(key, Mapping):
            raise QueryError(
                "sel() takes a mapping from dim name to value, such as sel({'run': 1}), "
                f"got {described(key)}"
            )
        frozen = frozendict(key)
        return self._extended(QuerySel(self.plan, frozen), sel_query_type(self.type, frozen))

    def rename(self, names: Mapping[str, str]) -> PlannedQuery[S]:
        """Rename dims, old name to new name, as ``Array.rename`` does."""
        if not isinstance(names, Mapping):
            raise QueryError(
                "rename() takes a mapping from old dim name to new, such as "
                f"rename({{'run': 'trial'}}), got {described(names)}"
            )
        frozen = frozendict(names)
        return self._extended(QueryRename(self.plan, frozen), rename_query_type(self.type, frozen))

    def retype(self, type: ArrayType) -> PlannedQuery[S]:
        """This query declared as ``type``, which its type must be assignable to.

        Type equality ignores presentation, so an equal ``type`` only changes how collected
        values are presented and needs no plan node. Any other ``type`` (its dims in another
        order, a wider schema) is a different array with a different content ID, which
        whoever executes the plan has to produce.

        Raises:
            ArrayTypeError: If this query's type is not assignable to ``type``.
        """
        if type == self.type:
            return dataclasses.replace(self, type=type)
        stored = retype_query_type(self.type, type)
        return PlannedQuery(self.sources, QueryRetype(self.plan, stored), type)

    def limit(
        self, count: int, *, order_by: OrderBy = (), ties: TiePolicy = "first"
    ) -> PlannedQuery[S]:
        order = as_sort_fields(order_by)
        return self._extended(
            QueryLimit(self.plan, count, order, ties), limit_type(self.type, count, order, ties)
        )

    def aggregate(
        self,
        aggregations: Mapping[str, Aggregation],
        *,
        group_by: Mapping[str, ValueExpression] = frozendict(),
    ) -> PlannedQuery[S]:
        if not isinstance(aggregations, Mapping) or not isinstance(group_by, Mapping):
            raise QueryError(
                "aggregate() takes a mapping of result field names to aggregations, and group_by a "
                "mapping of result dimension names to expressions, such as "
                "aggregate({'n': count()}, group_by={'model': value().field('model')})"
            )
        for name, aggregation in aggregations.items():
            if not isinstance(aggregation, Aggregation):
                raise QueryError(
                    f"Aggregation {name!r} must be count() or an expression's aggregate such as "
                    f".sum() or .mean(), got {described(aggregation)}"
                )
        frozen = frozendict(aggregations)
        groups = tuple(
            (name, as_value_expression(group, f"Group {name!r}"))
            for name, group in group_by.items()
        )
        return self._extended(
            QueryAggregate(self.plan, frozen, groups),
            aggregate_type(self.type, frozen, dict(groups)),
        )

    def join(
        self,
        other: PlannedQuery[S],
        on: JoinOn,
        *,
        left_name: str = "left",
        right_name: str = "right",
    ) -> PlannedQuery[S]:
        keys = as_join_keys(on)
        output = join_type(self.type, other.type, keys, left_name, right_name)
        sources, right = self._combined(other)
        return PlannedQuery(
            sources, QueryJoin(self.plan, right, keys, left_name, right_name), output
        )

    @classmethod
    def sql(
        cls,
        text: str,
        *,
        tables: Mapping[str, PlannedQuery[S]],
        output_type: ArrayType,
        parameters: SQLParameters = (),
    ) -> PlannedQuery[S]:
        """One read-only ``SELECT`` over ``tables``, each a query the SQL reads by its name.

        Raises:
            QueryError: If there is no table, a table name is reserved or repeated, ``text`` is
                not one read-only ``SELECT`` reading only ``tables``, its placeholders do not
                match ``parameters``, or a type has names DuckDB cannot tell apart.
        """
        # Checked before the inputs are keyed by name, where two names that differ only by
        # case would otherwise both be kept.
        validate_sql_table_names(tuple(tables))
        for queried in (*(table.type for table in tables.values()), output_type):
            require_distinct_names_ignoring_case(queried)
        sources: tuple[S, ...] = ()
        inputs: dict[str, QueryPlan] = {}
        # In name order, so that the same tables give the same plan however the mapping was built.
        for name, table in sorted(tables.items()):
            inputs[name] = shift_query_sources(table.plan, len(sources))
            sources = (*sources, *table.sources)
        plan = QuerySQL(frozendict(inputs), text, normalize_sql_parameters(parameters), output_type)
        return cls(sources, plan, output_type)
