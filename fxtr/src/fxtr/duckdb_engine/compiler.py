"""Compile array query plans to host-generated DuckDB SQL.

For a query-builder plan, only relation names chosen by the host and identifiers derived from
validated array types enter the SQL text, and query literals become bound parameters. A
:class:`~fxtr.entity_defns.query_plan.QuerySQL` node is the exception: its author's ``SELECT`` is
embedded as written, with its placeholders renumbered. The compiler returns the output type it
derived while walking the plan so callers never trust type metadata supplied by a worker.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast

from fxtr.entity_defns.array import ArrayType
from fxtr.entity_defns.arrow import ArrowColumn, arrow_columns
from fxtr.entity_defns.query import (
    Arithmetic,
    Between,
    Boolean,
    Cast,
    Column,
    Comparison,
    ComparisonOperator,
    Conditional,
    FillNull,
    InValues,
    LiteralExpression,
    Not,
    NullTest,
    Predicate,
    QueryError,
    SortField,
    UnaryArithmetic,
    ValueExpression,
    aggregate_type,
    expression_schema,
    filter_type,
    join_dimensions,
    join_type,
    limit_type,
    rename_query_type,
    require_distinct_names_ignoring_case,
    resolve_column,
    retype_query_type,
    sel_predicate,
    sel_query_type,
    select_type,
    select_value_type,
)
from fxtr.entity_defns.query_plan import (
    QueryAggregate,
    QueryFilter,
    QueryJoin,
    QueryLimit,
    QueryPlan,
    QueryRename,
    QueryRetype,
    QuerySel,
    QuerySelect,
    QuerySelectValue,
    QuerySource,
    QuerySQL,
    require_source_index,
)
from fxtr.entity_defns.schema import (
    FrozenValue,
    IdSchema,
    LeafKind,
    LeafSchema,
    ListSchema,
    NullableSchema,
    Schema,
    StructSchema,
)
from fxtr.entity_defns.sql import bind_sql


@dataclasses.dataclass(frozen=True)
class SQLResultCheck:
    """One user ``SELECT`` with its input tables attached, and the result type it declared.

    The executor binds ``sql`` without running it, to compare the columns DuckDB reports (``dims``
    and ``value``, and their struct fields) with ``output_type``. Doing so before the main
    statement pulls those structs apart lets the error name what the author got wrong.
    """

    sql: str
    output_type: ArrayType


@dataclasses.dataclass(frozen=True)
class CompiledArrayQuery:
    """DuckDB SQL, its bound values, and the host-derived result type."""

    sql: str
    parameters: tuple[Any, ...]
    output_type: ArrayType
    sql_checks: tuple[SQLResultCheck, ...] = ()


def compile_array_query(
    query: QueryPlan,
    source_types: Sequence[ArrayType],
    relation_names: Sequence[str],
) -> CompiledArrayQuery:
    """Compile ``query`` against host-selected ``relation_names``."""
    if len(source_types) != len(relation_names):
        raise QueryError(
            f"Received {len(source_types)} source types but {len(relation_names)} relations"
        )
    parameters: list[Any] = []
    checks: list[SQLResultCheck] = []
    try:
        sql, output_type = _compile(query, source_types, relation_names, parameters, checks)
    except RecursionError as exc:
        raise QueryError("Query compilation exceeds Python's recursion limit") from exc
    return CompiledArrayQuery(sql, tuple(parameters), output_type, tuple(checks))


def _compile(
    query: QueryPlan,
    source_types: Sequence[ArrayType],
    relation_names: Sequence[str],
    parameters: list[Any],
    checks: list[SQLResultCheck],
) -> tuple[str, ArrayType]:
    match query:
        case QuerySource(index=index):
            array_type = source_types[require_source_index(index, len(source_types))]
            columns = ", ".join(
                quote_identifier(column.physical_name) for column in arrow_columns(array_type)
            )
            return f"SELECT {columns} FROM {quote_identifier(relation_names[index])}", array_type

        case QueryFilter(input=input, predicate=predicate):
            inner, input_type = _compile(input, source_types, relation_names, parameters, checks)
            output_type = filter_type(input_type, predicate)
            condition = _predicate_sql(predicate, input_type, "q", parameters)
            return f"SELECT * FROM ({inner}) AS q WHERE COALESCE({condition}, FALSE)", output_type

        case QuerySelect(input=input, fields=fields):
            inner, input_type = _compile(input, source_types, relation_names, parameters, checks)
            output_type = select_type(input_type, fields)
            output_columns = arrow_columns(output_type)
            projected = _retained_dimensions_sql(input_type, output_columns)
            output_values = {
                column.logical_name: column for column in output_columns[len(output_type.dims) :]
            }
            for name, expression in fields.items():
                target = output_values[name]
                projected.append(
                    f"{_expression_sql(expression, input_type, 'q', parameters)} AS"
                    f" {quote_identifier(target.physical_name)}"
                )
            if not fields:
                projected.append(f"FALSE AS {quote_identifier(output_columns[-1].physical_name)}")
            return f"SELECT {', '.join(projected)} FROM ({inner}) AS q", output_type

        case QuerySelectValue(input=input, expression=expression):
            inner, input_type = _compile(input, source_types, relation_names, parameters, checks)
            output_type = select_value_type(input_type, expression)
            output_columns = arrow_columns(output_type)
            projected = _retained_dimensions_sql(input_type, output_columns)
            selected_value = _expression_sql(expression, input_type, "q", parameters)
            projected.extend(_value_columns_sql(selected_value, output_type))
            return f"SELECT {', '.join(projected)} FROM ({inner}) AS q", output_type

        case QuerySel(input=input, key=key):
            inner, input_type = _compile(input, source_types, relation_names, parameters, checks)
            output_type = sel_query_type(input_type, key)
            kept = [i for i, name in enumerate(input_type.dim_names) if name not in key]
            projected = _carried_columns_sql(input_type, output_type, kept)
            predicate = sel_predicate(key)
            where = (
                ""
                if predicate is None
                else f" WHERE {_predicate_sql(predicate, input_type, 'q', parameters)}"
            )
            return f"SELECT {', '.join(projected)} FROM ({inner}) AS q{where}", output_type

        case QueryRename(input=input, names=names):
            inner, input_type = _compile(input, source_types, relation_names, parameters, checks)
            output_type = rename_query_type(input_type, names)
            projected = _carried_columns_sql(input_type, output_type, range(len(input_type.dims)))
            return f"SELECT {', '.join(projected)} FROM ({inner}) AS q", output_type

        case QueryRetype(input=input, type=target):
            inner, input_type = _compile(input, source_types, relation_names, parameters, checks)
            output_type = retype_query_type(input_type, target)
            input_dimensions = _dimension_columns(input_type)
            projected = [
                f"q.{quote_identifier(input_dimensions[input_type.dim_names.index(dim.name)].physical_name)}"
                f" AS {quote_identifier(column.physical_name)}"
                for dim, column in zip(
                    output_type.dims, _dimension_columns(output_type), strict=True
                )
            ]
            # Widening a struct to a nullable one changes its columns from one per field to one
            # for the value, so the value is rebuilt whole and laid out again.
            projected.extend(_value_columns_sql(_whole_value_sql(input_type, "q"), output_type))
            return f"SELECT {', '.join(projected)} FROM ({inner}) AS q", output_type

        case QueryLimit(input=input, count=count, order_by=order_by, ties=ties):
            inner, input_type = _compile(input, source_types, relation_names, parameters, checks)
            output_type = limit_type(input_type, count, order_by, ties)
            declared_order = _sort_sql(order_by, input_type, "q", parameters)
            key_order = [
                f"q.{quote_identifier(_dimension_column(input_type, i).physical_name)} ASC"
                for i in range(len(input_type.dims))
            ]
            if ties == "all":
                # QUALIFY filters on the rank without adding a column whose name could clash
                # with one of the array's own.
                rank_order = ", ".join(declared_order)
                return (
                    f"SELECT * FROM ({inner}) AS q"
                    f" QUALIFY RANK() OVER (ORDER BY {rank_order}) <= {count}",
                    output_type,
                )
            ordering = [*declared_order, *key_order]
            order_clause = "" if not ordering else f" ORDER BY {', '.join(ordering)}"
            return f"SELECT * FROM ({inner}) AS q{order_clause} LIMIT {count}", output_type

        case QueryAggregate(input=input, aggregations=aggregations, group_by=group_by):
            inner, input_type = _compile(input, source_types, relation_names, parameters, checks)
            output_type = aggregate_type(input_type, aggregations, dict(group_by))
            output_columns = arrow_columns(output_type)
            selected: list[str] = []
            grouped: list[str] = []
            for position, (_, column) in enumerate(group_by):
                expression = _expression_sql(column, input_type, "q", parameters)
                grouped.append(expression)
                selected.append(
                    f"{expression} AS {quote_identifier(output_columns[position].physical_name)}"
                )
            output_values = {
                column.logical_name: column for column in output_columns[len(output_type.dims) :]
            }
            for name, aggregation in aggregations.items():
                target = output_values[name]
                function = aggregation.function
                if function == "row_count":
                    expression = "CAST(COUNT(*) AS BIGINT)"
                else:
                    assert aggregation.expression is not None
                    source = _expression_sql(aggregation.expression, input_type, "q", parameters)
                    if function == "count":
                        expression = f"CAST(COUNT({source}) AS BIGINT)"
                    elif function == "n_unique":
                        expression = (
                            f"CAST(COUNT(DISTINCT {source}) + CASE WHEN COUNT(*) FILTER "
                            f"(WHERE {source} IS NULL) > 0 THEN 1 ELSE 0 END AS BIGINT)"
                        )
                    elif function == "mean":
                        expression = f"CAST(AVG({source}) AS DOUBLE)"
                    elif function in ("min", "max"):
                        expression = f"{function.upper()}({source})"
                    elif function == "std":
                        expression = f"CAST(STDDEV_SAMP({source}) AS DOUBLE)"
                    elif function == "var":
                        expression = f"CAST(VAR_SAMP({source}) AS DOUBLE)"
                    elif function == "quantile":
                        assert aggregation.quantile is not None
                        parameters.append(aggregation.quantile)
                        expression = f"CAST(QUANTILE_CONT({source}, ${len(parameters)}) AS DOUBLE)"
                    else:
                        assert function == "sum"
                        target_schema = target.schema
                        if isinstance(target_schema, NullableSchema):
                            target_schema = target_schema.inner
                        assert isinstance(target_schema, LeafSchema)
                        sql_type = "DOUBLE" if target_schema.kind == "float" else "BIGINT"
                        expression = f"CAST(COALESCE(SUM({source}), 0) AS {sql_type})"
                selected.append(f"{expression} AS {quote_identifier(target.physical_name)}")
            group_clause = "" if not grouped else f" GROUP BY {', '.join(grouped)}"
            if not aggregations:
                selected.append(f"FALSE AS {quote_identifier(output_columns[-1].physical_name)}")
                if not grouped:
                    # With no aggregate function DuckDB would return one row per input row;
                    # counting first collapses the input to the single row a dimensionless
                    # result needs, even when the input is empty.
                    inner = f"SELECT COUNT(*) FROM ({inner}) AS counted"
            return (
                f"SELECT {', '.join(selected)} FROM ({inner}) AS q{group_clause}",
                output_type,
            )

        case QueryJoin(
            left=left,
            right=right,
            on=keys,
            left_name=left_name,
            right_name=right_name,
        ):
            left_sql, left_type = _compile(left, source_types, relation_names, parameters, checks)
            right_sql, right_type = _compile(
                right, source_types, relation_names, parameters, checks
            )
            output_type = join_type(left_type, right_type, keys, left_name, right_name)
            conditions = [
                f"{_expression_sql(key.left, left_type, 'l', parameters)} = "
                f"{_expression_sql(key.right, right_type, 'r', parameters)}"
                for key in keys
            ]
            output_columns = arrow_columns(output_type)
            # The output's dimensions are every left one, then the right ones the join retains.
            _, retained = join_dimensions(left_type, right_type, keys, right_name)
            sources = [
                *(
                    f"l.{quote_identifier(column.physical_name)}"
                    for column in _dimension_columns(left_type)
                ),
                *(
                    f"r.{quote_identifier(_dimension_column(right_type, position).physical_name)}"
                    for position in retained
                ),
            ]
            selected = [
                f"{source} AS {quote_identifier(target.physical_name)}"
                for source, target in zip(sources, output_columns, strict=False)
            ]
            output_values = {
                column.logical_name: column for column in output_columns[len(output_type.dims) :]
            }
            selected.append(
                f"{_whole_value_sql(left_type, 'l')} AS"
                f" {quote_identifier(output_values[left_name].physical_name)}"
            )
            selected.append(
                f"{_whole_value_sql(right_type, 'r')} AS"
                f" {quote_identifier(output_values[right_name].physical_name)}"
            )
            return (
                f"SELECT {', '.join(selected)} FROM ({left_sql}) AS l INNER JOIN"
                f" ({right_sql}) AS r ON {' AND '.join(conditions)}",
                output_type,
            )

        case QuerySQL(inputs=inputs, text=text, parameters=bound, output_type=output_type):
            require_distinct_names_ignoring_case(output_type)
            bindings: list[str] = []
            for name, input in inputs.items():
                inner, input_type = _compile(
                    input, source_types, relation_names, parameters, checks
                )
                require_distinct_names_ignoring_case(input_type)
                dimensions = [
                    f"{quote_identifier(dim.name)} := "
                    f"q.{quote_identifier(_dimension_column(input_type, i).physical_name)}"
                    for i, dim in enumerate(input_type.dims)
                ]
                dims = f"struct_pack({', '.join(dimensions)})" if dimensions else "FALSE"
                bindings.append(
                    f"{quote_identifier(name)} AS (SELECT {dims} AS dims, "
                    f"{_whole_value_sql(input_type, 'q')} AS value FROM ({inner}) AS q)"
                )
            sql, values = bind_sql(text, bound, len(parameters))
            parameters.extend(_thawed(value) for value in values)
            raw = f"WITH {', '.join(bindings)} SELECT * FROM (\n{sql}\n) AS __fxtr_sql_output"
            checks.append(SQLResultCheck(raw, output_type))
            # Each column is cast to its declared type here, not left to the Arrow conversion of
            # the final result, so that an operation built on this SQL reads what collecting it
            # would give: `5` declared as a float is 5.0, and `// 2` of it is 2.5. The check
            # above lets through only a cast that widens a number or types a NULL.
            columns = arrow_columns(output_type)
            selected: list[str] = []
            dims = (
                "CASE WHEN q.dims IS NULL THEN error('Array SQL dimensions cannot be null') "
                "ELSE q.dims END"
            )
            for index, dim in enumerate(output_type.dims):
                selected.append(
                    f"CAST(struct_extract({dims}, {_quote_string(dim.name)}) AS "
                    f"{_SQL_TYPES[dim.kind]}) AS {quote_identifier(columns[index].physical_name)}"
                )
            value_sql = "q.value"
            if isinstance(output_type.schema, StructSchema) and output_type.schema.fields:
                value_sql = (
                    "CASE WHEN q.value IS NULL THEN error('Array SQL value cannot be null') "
                    "ELSE q.value END"
                )
                for column in columns[len(output_type.dims) :]:
                    assert column.logical_name is not None
                    selected.append(
                        f"CAST(struct_extract({value_sql}, {_quote_string(column.logical_name)}) "
                        f"AS {_duckdb_type(column.schema)}) "
                        f"AS {quote_identifier(column.physical_name)}"
                    )
            else:
                selected.append(
                    f"CAST({value_sql} AS {_duckdb_type(output_type.schema)}) "
                    f"AS {quote_identifier(columns[-1].physical_name)}"
                )
            return f"SELECT {', '.join(selected)} FROM ({raw}) AS q", output_type

        case _:
            raise QueryError(f"Unknown array query {query!r}")


def _expression_sql(
    expression: ValueExpression,
    input_type: ArrayType,
    alias: str,
    parameters: list[Any],
) -> str:
    match expression:
        case Column():
            return _column_sql(input_type, expression, alias)
        case LiteralExpression(value=value, schema=schema):
            parameters.append(_thawed(value))
            if isinstance(schema, NullableSchema):
                schema = schema.inner
            if isinstance(schema, LeafSchema):
                # A leaf literal carries its declared type. DuckDB would type a small Python int
                # as a 32-bit INTEGER, whose arithmetic overflows at 2**31 where fxtr's int is
                # 64-bit, and it refuses a bare parameter as a GROUP BY key.
                return f"CAST(${len(parameters)} AS {_SQL_TYPES[schema.kind]})"
            return f"${len(parameters)}"
        case Arithmetic(left=left, operator=operator, right=right):
            left_sql = _expression_sql(left, input_type, alias, parameters)
            right_sql = _expression_sql(right, input_type, alias, parameters)
            if operator == "pow":
                return f"POWER({left_sql}, {right_sql})"
            return f"({left_sql} {_ARITHMETIC_OPERATORS[operator]} {right_sql})"
        case UnaryArithmetic(operator="negate", operand=operand):
            return f"(-{_expression_sql(operand, input_type, alias, parameters)})"
        case Cast(operand=operand, target=target):
            source_schema = expression_schema(input_type, operand)
            if isinstance(source_schema, NullableSchema):
                source_schema = source_schema.inner
            if source_schema is None:
                # An untyped null, as ``literal(None)`` is: a null of the target type.
                return f"CAST(NULL AS {_SQL_TYPES[target]})"
            assert isinstance(source_schema, LeafSchema)
            source = _expression_sql(operand, input_type, alias, parameters)
            return _cast_sql(source, source_schema.kind, target)
        case FillNull(operand=operand, replacement=replacement):
            source = _expression_sql(operand, input_type, alias, parameters)
            fallback = _expression_sql(replacement, input_type, alias, parameters)
            return f"COALESCE({source}, {fallback})"
        case Conditional(predicate=predicate, true_value=true_value, false_value=false_value):
            condition = _predicate_sql(predicate, input_type, alias, parameters)
            true_sql = _expression_sql(true_value, input_type, alias, parameters)
            false_sql = _expression_sql(false_value, input_type, alias, parameters)
            return f"CASE WHEN {condition} THEN {true_sql} ELSE {false_sql} END"
        case Predicate():
            return _predicate_sql(expression, input_type, alias, parameters)
        case _:
            raise QueryError(f"Unknown value expression {expression!r}")


def _lambda_parameters(input_type: ArrayType, count: int) -> tuple[str, ...]:
    """``count`` identifiers to use as lambda parameters in an expression over ``input_type``.

    DuckDB resolves a bare name to a column before it considers a lambda parameter, so a
    parameter called ``x`` would read the user's column ``x``.
    """
    columns = {column.physical_name.casefold() for column in arrow_columns(input_type)}
    unused = (f"p{number}" for number in range(len(columns) + count))
    return tuple(name for name in unused if name not in columns)[:count]


_ARITHMETIC_OPERATORS: dict[str, str] = {
    "add": "+",
    "sub": "-",
    "mul": "*",
    "truediv": "/",
    "floordiv": "//",
    "mod": "%",
}


_SQL_TYPES: dict[LeafKind, str] = {
    "int": "BIGINT",
    "float": "DOUBLE",
    "bool": "BOOLEAN",
    "str": "VARCHAR",
    "bytes": "BLOB",
}


def _duckdb_type(schema: Schema) -> str:
    """The DuckDB type a value of ``schema`` has in a query: an entity ID as its CID bytes, and an
    empty struct as the ``false`` that stands for it."""
    match schema:
        case NullableSchema(inner):
            return _duckdb_type(inner)
        case LeafSchema(kind):
            return _SQL_TYPES[kind]
        case IdSchema():
            return "BLOB"
        case ListSchema(element):
            return f"{_duckdb_type(element)}[]"
        case StructSchema(fields) if fields:
            members = ", ".join(
                f"{quote_identifier(name)} {_duckdb_type(fields[name])}" for name in sorted(fields)
            )
            return f"STRUCT({members})"
        case _:
            return "BOOLEAN"


def _cast_sql(source: str, source_kind: LeafKind, target: LeafKind) -> str:
    """``cast`` as DuckDB casts, except that text and bytes convert by UTF-8.

    DuckDB converts between ``VARCHAR`` and ``BLOB`` by an escape syntax of its own, so a cast of
    the bytes ``b'\xc3\xa9'`` would give the eight characters ``\xc3\xa9`` rather than the
    character they encode. ``encode`` and ``decode`` are DuckDB's own UTF-8 conversions, and are
    what SQL that wants text out of bytes calls. A value the target cannot hold raises.
    """
    match source_kind, target:
        case "str", "bytes":
            return f"encode({source})"
        case "bytes", "str":
            return f"decode({source})"
        case _:
            return f"CAST({source} AS {_SQL_TYPES[target]})"


def _predicate_sql(
    predicate: Predicate,
    input_type: ArrayType,
    alias: str,
    parameters: list[Any],
) -> str:
    match predicate:
        case Comparison(left=left, operator=operator, right=right):
            return _comparison_sql(operator, left, right, input_type, alias, parameters)
        case Boolean(left=left, operator=operator, right=right):
            sql_operator = "AND" if operator == "and" else "OR"
            return (
                f"({_predicate_sql(left, input_type, alias, parameters)} {sql_operator}"
                f" {_predicate_sql(right, input_type, alias, parameters)})"
            )
        case Not(operand=operand):
            return f"(NOT {_predicate_sql(operand, input_type, alias, parameters)})"
        case NullTest(operand=operand, negated=negated):
            suffix = "IS NOT NULL" if negated else "IS NULL"
            return f"({_expression_sql(operand, input_type, alias, parameters)} {suffix})"
        case InValues(operand=operand, options=options):
            if not options:
                return "FALSE"
            source = _expression_sql(operand, input_type, alias, parameters)
            values = ", ".join(
                _expression_sql(option, input_type, alias, parameters) for option in options
            )
            return f"({source} IN ({values}))"
        case Between(operand=operand, lower=lower, upper=upper):
            # The operand is compared twice, so it is bound once in a lambda: written out in
            # each comparison, nested betweens would double the SQL per level.
            (bound,) = _lambda_parameters(input_type, 1)
            operands = ", ".join(
                f"'{name}': {_expression_sql(part, input_type, alias, parameters)}"
                for name, part in (("x", operand), ("lo", lower), ("hi", upper))
            )
            comparisons = f"{bound}.x >= {bound}.lo AND {bound}.x <= {bound}.hi"
            return f"list_transform([{{{operands}}}], lambda {bound}: ({comparisons}))[1]"
        case _:
            raise QueryError(f"Unknown predicate {predicate!r}")


_COMPARISON_OPERATORS: dict[ComparisonOperator, str] = {
    "eq": "=",
    "ne": "<>",
    "lt": "<",
    "le": "<=",
    "gt": ">",
    "ge": ">=",
}


def _comparison_sql(
    operator: ComparisonOperator,
    left: ValueExpression,
    right: ValueExpression,
    input_type: ArrayType,
    alias: str,
    parameters: list[Any],
) -> str:
    left_sql = _expression_sql(left, input_type, alias, parameters)
    right_sql = _expression_sql(right, input_type, alias, parameters)
    return f"({left_sql} {_COMPARISON_OPERATORS[operator]} {right_sql})"


def _sort_sql(
    order_by: Sequence[SortField],
    input_type: ArrayType,
    alias: str,
    parameters: list[Any],
) -> list[str]:
    return [
        f"{_expression_sql(sort.expression, input_type, alias, parameters)}"
        f" {sort.direction.upper()} NULLS LAST"
        for sort in order_by
    ]


def _column_sql(type: ArrayType, column: Column, alias: str) -> str:
    origin, position, _ = resolve_column(type, column)  # raises unless the column exists
    if origin == "dim":
        assert position is not None
        return f"{alias}.{quote_identifier(_dimension_column(type, position).physical_name)}"
    if not column.path:
        return _whole_value_sql(type, alias)
    path = column.path
    if isinstance(type.schema, StructSchema) and type.schema.fields:
        # A top-level struct is stored as one Arrow column per field.
        sql = f"{alias}.{quote_identifier(_value_column(type, path[0]).physical_name)}"
        path = path[1:]
    else:
        sql = f"{alias}.{quote_identifier(_value_column(type, None).physical_name)}"
    for part in path:
        sql = f"struct_extract({sql}, {_quote_string(part)})"
    return sql


def _whole_value_sql(type: ArrayType, alias: str) -> str:
    if isinstance(type.schema, StructSchema) and type.schema.fields:
        fields = []
        for name in type.schema.names:
            physical = _value_column(type, name).physical_name
            fields.append(f"{quote_identifier(name)} := {alias}.{quote_identifier(physical)}")
        return f"struct_pack({', '.join(fields)})"
    physical = _value_column(type, None).physical_name
    return f"{alias}.{quote_identifier(physical)}"


def _dimension_columns(type: ArrayType) -> tuple[ArrowColumn, ...]:
    return arrow_columns(type)[: len(type.dims)]


def _dimension_column(type: ArrayType, position: int) -> ArrowColumn:
    return arrow_columns(type)[position]


def _value_column(type: ArrayType, logical_name: str | None) -> ArrowColumn:
    for column in arrow_columns(type)[len(type.dims) :]:
        if column.logical_name == logical_name:
            return column
    raise QueryError(f"No value column {logical_name!r} in {type}")


def _retained_dimensions_sql(
    input_type: ArrayType, output_columns: Sequence[ArrowColumn]
) -> list[str]:
    """Select items carrying every dimension of ``q`` into an output with the same dimensions."""
    return [
        f"q.{quote_identifier(source.physical_name)} AS {quote_identifier(target.physical_name)}"
        for source, target in zip(_dimension_columns(input_type), output_columns, strict=False)
    ]


def _value_columns_sql(value_sql: str, output_type: ArrayType) -> list[str]:
    """Select items laying ``value_sql``, a whole row value, out as ``output_type``'s columns."""
    columns = arrow_columns(output_type)[len(output_type.dims) :]
    if isinstance(output_type.schema, StructSchema) and output_type.schema.fields:
        return [
            f"struct_extract({value_sql}, {_quote_string(cast('str', column.logical_name))}) AS"
            f" {quote_identifier(column.physical_name)}"
            for column in columns
        ]
    return [f"{value_sql} AS {quote_identifier(columns[-1].physical_name)}"]


def _carried_columns_sql(
    input_type: ArrayType, output_type: ArrayType, kept_dimensions: Iterable[int]
) -> list[str]:
    """Select items for an output holding ``q``'s value and its dims at ``kept_dimensions``.

    Every column is renamed, not only the dims: a value column's physical name depends on the
    dim names it has to stay distinct from, so it can differ between the two types.
    """
    input_columns = arrow_columns(input_type)
    sources = [
        *(input_columns[position] for position in kept_dimensions),
        *input_columns[len(input_type.dims) :],
    ]
    return [
        f"q.{quote_identifier(source.physical_name)} AS {quote_identifier(target.physical_name)}"
        for source, target in zip(sources, arrow_columns(output_type), strict=True)
    ]


def _thawed(value: FrozenValue) -> Any:
    """A frozen value as the lists and dicts DuckDB binds as LIST and STRUCT parameters.

    Entity IDs pass through untouched: the executor binds each as its CID bytes.
    """
    match value:
        case tuple():
            return [_thawed(item) for item in value]
        case Mapping():
            return {name: _thawed(item) for name, item in value.items()}
        case _:
            return value


def quote_identifier(identifier: str) -> str:
    """``identifier`` as a quoted SQL identifier, whatever characters it holds."""
    return '"' + identifier.replace('"', '""') + '"'


def _quote_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


__all__ = ["CompiledArrayQuery", "SQLResultCheck", "compile_array_query", "quote_identifier"]
