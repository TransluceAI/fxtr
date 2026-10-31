"""The bytes a query node (:class:`~fxtr.runner.nodes.Query`) holds to describe its query plan,
and their strict decoding.

The public expressions live in :mod:`fxtr.entity_defns.query`; plans live in
:mod:`fxtr.entity_defns.query_plan`. A plan names inputs by position; the query node lists the
input nodes for those positions.

Plans have an explicit DAG-CBOR encoding independent of Python class names. DAG-CBOR sorts map
keys, so anything whose order matters (``group_by``, whose names become the result's dimensions
in order) is encoded as a list. The host decodes bytes it has no reason to trust: every map must
have exactly its expected keys.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import cast

from frozendict import frozendict

from fxtr.core import codec
from fxtr.core.entity_data import FxtrDataValue, StorableFxtrDataValue
from fxtr.entity_defns.array import ArrayType, KeyValue
from fxtr.entity_defns.query import (
    AggregateFunction,
    Aggregation,
    Arithmetic,
    ArithmeticOperator,
    Between,
    Boolean,
    BooleanOperator,
    Cast,
    Column,
    ColumnSource,
    Comparison,
    ComparisonOperator,
    Conditional,
    FillNull,
    InValues,
    JoinKey,
    LiteralExpression,
    Not,
    NullTest,
    Predicate,
    QueryError,
    SortDirection,
    SortField,
    TiePolicy,
    UnaryArithmetic,
    UnaryArithmeticOperator,
    ValueExpression,
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
)
from fxtr.entity_defns.schema import (
    LeafKind,
    coerce_value,
    schema_from_wire,
    schema_to_wire,
    value_to_wire,
)
from fxtr.entity_defns.sql import normalize_sql_parameters


def encode_query(query: QueryPlan) -> bytes:
    """Encode a query description as canonical DAG-CBOR bytes."""
    try:
        return codec.encode(_query_to_data(query))
    except RecursionError as exc:
        raise QueryError("Query encoding exceeds Python's recursion limit") from exc


def decode_query(data: bytes) -> QueryPlan:
    """Decode and strictly validate :func:`encode_query` output."""
    if not isinstance(data, bytes):
        raise QueryError(f"Encoded query must be bytes, got {type(data).__name__}")
    try:
        value = codec.decode(data)
    except RecursionError as exc:
        raise QueryError("Query decoding exceeds Python's recursion limit") from exc
    except Exception as exc:
        raise QueryError(f"Malformed query encoding: {exc}") from exc
    try:
        query = _query_from_data(value)
    except RecursionError as exc:
        raise QueryError("Query decoding exceeds Python's recursion limit") from exc
    # A query node's identity is the hash of these bytes, so one plan must have one encoding.
    # Decoding normalizes some values (a quantile to a float, a parameter name to its folded
    # form) rather than refusing them, so the check is against the encoding it gives back.
    if encode_query(query) != data:
        raise QueryError("Query encoding is not canonical")
    return query


def _query_to_data(query: QueryPlan) -> StorableFxtrDataValue:
    match query:
        case QuerySource(index=index):
            return {"op": "source", "index": index}
        case QuerySQL(inputs=inputs, text=text, parameters=parameters, output_type=output_type):
            return {
                "op": "sql",
                "inputs": {name: _query_to_data(input) for name, input in inputs.items()},
                "text": text,
                "parameters": {name: value_to_wire(value) for name, value in parameters.items()},
                "output_type": output_type.to_wire(),
            }
        case QueryFilter(input=input, predicate=predicate):
            return {
                "op": "filter",
                "input": _query_to_data(input),
                "predicate": _predicate_to_data(predicate),
            }
        case QuerySelect(input=input, fields=fields):
            return {
                "op": "select",
                "input": _query_to_data(input),
                "fields": {name: _expression_to_data(expr) for name, expr in fields.items()},
            }
        case QuerySelectValue(input=input, expression=expression):
            return {
                "op": "select_value",
                "input": _query_to_data(input),
                "expression": _expression_to_data(expression),
            }
        case QuerySel(input=input, key=key):
            return {"op": "sel", "input": _query_to_data(input), "key": dict(key)}
        case QueryRename(input=input, names=names):
            return {"op": "rename", "input": _query_to_data(input), "names": dict(names)}
        case QueryRetype(input=input, type=type):
            return {"op": "retype", "input": _query_to_data(input), "type": type.to_wire()}
        case QueryLimit(input=input, count=count, order_by=order_by, ties=ties):
            return {
                "op": "limit",
                "input": _query_to_data(input),
                "count": count,
                "order_by": [
                    {
                        "expression": _expression_to_data(sort.expression),
                        "direction": sort.direction,
                    }
                    for sort in order_by
                ],
                "ties": ties,
            }
        case QueryAggregate(input=input, aggregations=aggregations, group_by=group_by):
            return {
                "op": "aggregate",
                "input": _query_to_data(input),
                "aggregations": {
                    name: {
                        "function": aggregation.function,
                        "expression": None
                        if aggregation.expression is None
                        else _expression_to_data(aggregation.expression),
                        "quantile": aggregation.quantile,
                    }
                    for name, aggregation in aggregations.items()
                },
                "group_by": [[name, _expression_to_data(column)] for name, column in group_by],
            }
        case QueryJoin(left=left, right=right, on=on, left_name=left_name, right_name=right_name):
            return {
                "op": "join",
                "left": _query_to_data(left),
                "right": _query_to_data(right),
                "on": [
                    {
                        "left": _expression_to_data(key.left),
                        "right": _expression_to_data(key.right),
                    }
                    for key in on
                ],
                "left_name": left_name,
                "right_name": right_name,
            }
        case _:
            raise QueryError(f"Unknown array query {query!r}")


def _query_from_data(value: object) -> QueryPlan:
    data = _mapping(value, "query")
    op = _string(data.get("op"), "query op")
    if op == "sql":
        _keys(data, {"op", "inputs", "text", "parameters", "output_type"}, op)
        try:
            output_type = ArrayType.from_wire(data["output_type"])
        except ValueError as exc:
            raise QueryError(f"Invalid SQL output type: {exc}") from exc
        return QuerySQL(
            frozendict(_named_mapping(data["inputs"], "SQL inputs", _query_from_data)),
            _string(data["text"], "SQL text"),
            normalize_sql_parameters(_mapping(data["parameters"], "SQL parameters")),
            output_type,
        )
    if op == "source":
        _keys(data, {"op", "index"}, op)
        index = data["index"]
        if type(index) is not int:
            raise QueryError(f"Query source index must be an int, got {index!r}")
        return QuerySource(index)
    if op == "filter":
        _keys(data, {"op", "input", "predicate"}, op)
        return QueryFilter(
            _query_from_data(data["input"]),
            _predicate_from_data(data["predicate"]),
        )
    if op == "select":
        _keys(data, {"op", "input", "fields"}, op)
        fields = _named_mapping(data["fields"], "select fields", _expression_from_data)
        return QuerySelect(_query_from_data(data["input"]), frozendict(fields))
    if op == "select_value":
        _keys(data, {"op", "input", "expression"}, op)
        expression = _expression_from_data(data["expression"])
        return QuerySelectValue(_query_from_data(data["input"]), expression)
    if op == "sel":
        _keys(data, {"op", "input", "key"}, op)
        return QuerySel(
            _query_from_data(data["input"]),
            frozendict(_named_mapping(data["key"], "sel key", _key_value)),
        )
    if op == "rename":
        _keys(data, {"op", "input", "names"}, op)
        return QueryRename(
            _query_from_data(data["input"]),
            frozendict(
                _named_mapping(
                    data["names"], "rename names", lambda name: _string(name, "renamed dim")
                )
            ),
        )
    if op == "retype":
        _keys(data, {"op", "input", "type"}, op)
        try:
            target = ArrayType.from_wire(data["type"])
        except ValueError as exc:
            raise QueryError(f"Invalid retype type: {exc}") from exc
        return QueryRetype(_query_from_data(data["input"]), target)
    if op == "limit":
        _keys(data, {"op", "input", "count", "order_by", "ties"}, op)
        count = data["count"]
        if type(count) is not int:
            raise QueryError(f"Limit count must be an int, got {count!r}")
        order_values = _sequence(data["order_by"], "limit order_by")
        order: list[SortField] = []
        for item in order_values:
            sort = _mapping(item, "sort field")
            _keys(sort, {"expression", "direction"}, "sort field")
            order.append(
                SortField(
                    _expression_from_data(sort["expression"]),
                    cast("SortDirection", _string(sort["direction"], "sort direction")),
                )
            )
        return QueryLimit(
            _query_from_data(data["input"]),
            count,
            tuple(order),
            cast("TiePolicy", _string(data["ties"], "tie policy")),
        )
    if op == "aggregate":
        _keys(data, {"op", "input", "aggregations", "group_by"}, op)
        aggregation_values = _mapping(data["aggregations"], "aggregations")
        aggregations: dict[str, Aggregation] = {}
        for name, raw in aggregation_values.items():
            if not isinstance(name, str) or not name:
                raise QueryError(f"Aggregation names must be non-empty strings, got {name!r}")
            item = _mapping(raw, f"aggregation {name!r}")
            _keys(item, {"function", "expression", "quantile"}, f"aggregation {name!r}")
            expression = item["expression"]
            quantile = item["quantile"]
            if quantile is not None and (
                not isinstance(quantile, (int, float)) or isinstance(quantile, bool)
            ):
                raise QueryError(f"Aggregation quantile must be a number or null, got {quantile!r}")
            aggregations[name] = Aggregation(
                cast("AggregateFunction", _string(item["function"], "aggregate function")),
                None if expression is None else _expression_from_data(expression),
                cast("float | None", quantile),
            )
        groups: list[tuple[str, ValueExpression]] = []
        for raw in _sequence(data["group_by"], "group_by"):
            pair = _sequence(raw, "group")
            if len(pair) != 2 or not isinstance(pair[0], str) or not pair[0]:
                raise QueryError(f"A group must be a [name, expression] pair, got {raw!r}")
            groups.append((pair[0], _expression_from_data(pair[1])))
        return QueryAggregate(
            _query_from_data(data["input"]), frozendict(aggregations), tuple(groups)
        )
    if op == "join":
        _keys(data, {"op", "left", "right", "on", "left_name", "right_name"}, op)
        keys: list[JoinKey] = []
        for raw in _sequence(data["on"], "join keys"):
            item = _mapping(raw, "join key")
            _keys(item, {"left", "right"}, "join key")
            keys.append(JoinKey(_column_from_data(item["left"]), _column_from_data(item["right"])))
        return QueryJoin(
            _query_from_data(data["left"]),
            _query_from_data(data["right"]),
            tuple(keys),
            _string(data["left_name"], "left join field name"),
            _string(data["right_name"], "right join field name"),
        )
    raise QueryError(f"Unknown query operation {op!r}")


def _expression_to_data(expression: ValueExpression) -> StorableFxtrDataValue:
    match expression:
        case Column(source=source, path=path):
            return {"kind": "column", "source": source, "path": list(path)}
        case LiteralExpression(value=value, schema=schema):
            return {
                "kind": "literal",
                "value": value_to_wire(value),
                "schema": None if schema is None else schema_to_wire(schema),
            }
        case Arithmetic(left=left, operator=operator, right=right):
            return {
                "kind": "arithmetic",
                "left": _expression_to_data(left),
                "operator": operator,
                "right": _expression_to_data(right),
            }
        case UnaryArithmetic(operator=operator, operand=operand):
            return {
                "kind": "unary_arithmetic",
                "operator": operator,
                "operand": _expression_to_data(operand),
            }
        case Cast(operand=operand, target=target):
            return {
                "kind": "cast",
                "operand": _expression_to_data(operand),
                "target": target,
            }
        case FillNull(operand=operand, replacement=replacement):
            return {
                "kind": "fill_null",
                "operand": _expression_to_data(operand),
                "replacement": _expression_to_data(replacement),
            }
        case Conditional(predicate=predicate, true_value=true_value, false_value=false_value):
            return {
                "kind": "conditional",
                "predicate": _predicate_to_data(predicate),
                "true_value": _expression_to_data(true_value),
                "false_value": _expression_to_data(false_value),
            }
        case Predicate():
            return {"kind": "predicate", "predicate": _predicate_to_data(expression)}
        case _:
            raise QueryError(f"Unknown value expression {expression!r}")


def _expression_from_data(value: object) -> ValueExpression:
    data = _mapping(value, "value expression")
    kind = _string(data.get("kind"), "expression kind")
    if kind == "column":
        _keys(data, {"kind", "source", "path"}, kind)
        path = tuple(
            _string(part, "column path part") for part in _sequence(data["path"], "column path")
        )
        return Column(cast("ColumnSource", _string(data["source"], "column source")), path)
    if kind == "literal":
        _keys(data, {"kind", "value", "schema"}, kind)
        raw_schema = data["schema"]
        if raw_schema is None:
            if data["value"] is not None:
                raise QueryError("Only a null literal may omit its schema")
            return LiteralExpression(None, None)
        try:
            schema = schema_from_wire(raw_schema)
            literal = coerce_value(data["value"], schema, "query literal")
        except ValueError as exc:
            raise QueryError(str(exc)) from exc
        return LiteralExpression(literal, schema)
    if kind == "arithmetic":
        _keys(data, {"kind", "left", "operator", "right"}, kind)
        return Arithmetic(
            _expression_from_data(data["left"]),
            cast("ArithmeticOperator", _string(data["operator"], "arithmetic operator")),
            _expression_from_data(data["right"]),
        )
    if kind == "unary_arithmetic":
        _keys(data, {"kind", "operator", "operand"}, kind)
        return UnaryArithmetic(
            cast(
                "UnaryArithmeticOperator",
                _string(data["operator"], "unary arithmetic operator"),
            ),
            _expression_from_data(data["operand"]),
        )
    if kind == "cast":
        _keys(data, {"kind", "operand", "target"}, kind)
        return Cast(
            _expression_from_data(data["operand"]),
            cast("LeafKind", _string(data["target"], "cast target")),
        )
    if kind == "fill_null":
        _keys(data, {"kind", "operand", "replacement"}, kind)
        return FillNull(
            _expression_from_data(data["operand"]),
            _expression_from_data(data["replacement"]),
        )
    if kind == "conditional":
        _keys(data, {"kind", "predicate", "true_value", "false_value"}, kind)
        return Conditional(
            _predicate_from_data(data["predicate"]),
            _expression_from_data(data["true_value"]),
            _expression_from_data(data["false_value"]),
        )
    if kind == "predicate":
        _keys(data, {"kind", "predicate"}, kind)
        return _predicate_from_data(data["predicate"])
    raise QueryError(f"Unknown expression kind {kind!r}")


def _column_from_data(value: object) -> Column:
    expression = _expression_from_data(value)
    if not isinstance(expression, Column):
        raise QueryError(f"Expected a column, got {expression!r}")
    return expression


def _predicate_to_data(predicate: Predicate) -> StorableFxtrDataValue:
    match predicate:
        case Comparison(left=left, operator=operator, right=right):
            return {
                "kind": "comparison",
                "left": _expression_to_data(left),
                "operator": operator,
                "right": _expression_to_data(right),
            }
        case Boolean(left=left, operator=operator, right=right):
            return {
                "kind": "boolean",
                "left": _predicate_to_data(left),
                "operator": operator,
                "right": _predicate_to_data(right),
            }
        case Not(operand=operand):
            return {"kind": "not", "operand": _predicate_to_data(operand)}
        case NullTest(operand=operand, negated=negated):
            return {
                "kind": "null_test",
                "operand": _expression_to_data(operand),
                "negated": negated,
            }
        case InValues(operand=operand, options=options):
            return {
                "kind": "in_values",
                "operand": _expression_to_data(operand),
                "options": [_expression_to_data(option) for option in options],
            }
        case Between(operand=operand, lower=lower, upper=upper):
            return {
                "kind": "between",
                "operand": _expression_to_data(operand),
                "lower": _expression_to_data(lower),
                "upper": _expression_to_data(upper),
            }
        case _:
            raise QueryError(f"Unknown predicate {predicate!r}")


def _predicate_from_data(value: object) -> Predicate:
    data = _mapping(value, "predicate")
    kind = _string(data.get("kind"), "predicate kind")
    if kind == "comparison":
        _keys(data, {"kind", "left", "operator", "right"}, kind)
        return Comparison(
            _expression_from_data(data["left"]),
            cast("ComparisonOperator", _string(data["operator"], "comparison operator")),
            _expression_from_data(data["right"]),
        )
    if kind == "boolean":
        _keys(data, {"kind", "left", "operator", "right"}, kind)
        return Boolean(
            _predicate_from_data(data["left"]),
            cast("BooleanOperator", _string(data["operator"], "boolean operator")),
            _predicate_from_data(data["right"]),
        )
    if kind == "not":
        _keys(data, {"kind", "operand"}, kind)
        return Not(_predicate_from_data(data["operand"]))
    if kind == "null_test":
        _keys(data, {"kind", "operand", "negated"}, kind)
        negated = data["negated"]
        if type(negated) is not bool:
            raise QueryError(f"Null-test negated must be bool, got {negated!r}")
        return NullTest(_expression_from_data(data["operand"]), negated)
    if kind == "in_values":
        _keys(data, {"kind", "operand", "options"}, kind)
        return InValues(
            _expression_from_data(data["operand"]),
            tuple(
                _expression_from_data(option)
                for option in _sequence(data["options"], "membership options")
            ),
        )
    if kind == "between":
        _keys(data, {"kind", "operand", "lower", "upper"}, kind)
        return Between(
            _expression_from_data(data["operand"]),
            _expression_from_data(data["lower"]),
            _expression_from_data(data["upper"]),
        )
    raise QueryError(f"Unknown predicate kind {kind!r}")


def _mapping(value: object, where: str) -> Mapping[str, FxtrDataValue]:
    if not isinstance(value, Mapping):
        raise QueryError(f"{where} must be a mapping, got {type(value).__name__}")
    if not all(isinstance(key, str) for key in value):
        raise QueryError(f"{where} keys must be strings")
    return cast("Mapping[str, FxtrDataValue]", value)


def _sequence(value: object, where: str) -> Sequence[FxtrDataValue]:
    if not isinstance(value, (list, tuple)):
        raise QueryError(f"{where} must be a sequence, got {type(value).__name__}")
    return cast("Sequence[FxtrDataValue]", value)


def _key_value(value: object) -> KeyValue:
    if type(value) not in (int, str):
        raise QueryError(f"A sel key value must be an int or a str, got {value!r}")
    return cast("KeyValue", value)


def _string(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise QueryError(f"{where} must be a string, got {value!r}")
    return value


def _keys(data: Mapping[str, object], expected: set[str], where: str) -> None:
    if set(data) != expected:
        raise QueryError(f"{where} must have fields {sorted(expected)}, got {sorted(data)}")


def _named_mapping[T](value: object, where: str, convert: Callable[[object], T]) -> dict[str, T]:
    data = _mapping(value, where)
    result: dict[str, T] = {}
    for name, item in data.items():
        if not name:
            raise QueryError(f"{where} names must be non-empty strings")
        result[name] = convert(item)
    return result


__all__ = ["decode_query", "encode_query"]
