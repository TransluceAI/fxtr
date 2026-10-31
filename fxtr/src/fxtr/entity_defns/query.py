"""Typed expressions and type derivation for local array queries and a workflow's query nodes.

The query API is deliberately small and data-only: expressions and operations are immutable
objects that can be type-checked before rows exist and compiled to DuckDB for either execution
location. Query results have the same canonical serialization and content identity as ordinary
arrays built directly from the resulting rows.

Columns explicitly address either a dimension with ``dim("name")`` or the row's value with
``value()``. ``value().field("name")`` selects one struct field; repeated ``field`` calls select
nested fields. Dimension and value-field names can overlap without changing their meaning.
Predicates compose with ``&``, ``|``, and ``~``::

    rows.filter((value().field("score") >= 0.8) & value().field("error").is_null())
    rows.select(
        {
            "percent": value().field("score") * 100,
            "error": value().field("error").fill_null("none"),
        }
    )
    rows.aggregate(
        {"n": count(), "avg": value().field("score").mean()},
        group_by={"band": when(value().field("score") >= 0.8).then("high").otherwise("low")},
    )

Filtering preserves dimensions. Selecting one expression replaces the complete value; selecting a
mapping builds a struct value with the requested names. Both preserve dimensions. Grouping turns
its named integer/string columns into result dimensions. An inner join retains every left dimension
and every right dimension not used as an equality key; remaining name collisions receive the right
value-field name as a prefix.

**Null values follow SQL, not Python.** A comparison with a null operand is null rather than true
or false, and ``filter`` keeps only rows whose predicate is true. So
``value().field("error") != "bad"`` drops rows whose ``error`` is null, and so does its negation.
Write the null case out when those rows are wanted::

    rows.filter(value().field("error").is_null() | (value().field("error") != "bad"))

**Arithmetic and casts are SQL's, not Python's.** An expression compiles to the operator or cast
DuckDB would run for the same SQL, so ``//`` truncates toward zero, ``%`` takes the dividend's
sign, ``//`` between floats divides, ``cast(int)`` rounds, and an integer compared with a float is
widened to a double. A zero divisor gives infinity or null, neither of which an array of
non-optional numbers can hold, so such a query fails on its result rather than on the division.

DuckDB matches names without regard to case, so an array cannot be queried while two of its dims,
or two fields of one of its structs, differ only by case; building such a query raises
:class:`QueryError`.
"""

from __future__ import annotations

import dataclasses
import functools
import math
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Literal, TypeAliasType, cast, get_args

from frozendict import frozendict

from fxtr.entity_defns.array import (
    ArrayError,
    ArrayType,
    ArrayTypeError,
    Dim,
    KeyValue,
    is_assignable_array,
    rename_type,
    sel_type,
)
from fxtr.entity_defns.schema import (
    FrozenValue,
    IdSchema,
    LeafKind,
    LeafSchema,
    ListSchema,
    NullableSchema,
    Schema,
    SchemaSpec,
    StructSchema,
    as_schema,
    coerce_value,
    infer_schema,
    is_assignable,
)

if TYPE_CHECKING:
    from _typeshed import DataclassInstance


class QueryError(ArrayError):
    """A query is malformed or cannot be evaluated for its declared input types."""


class DuckDBResourceLimitError(QueryError):
    """A query exceeded a configured execution or result limit.

    A kind of :class:`QueryError`, so that ``except QueryError`` covers every way a query can
    fail. It is defined here and not with the executor that raises it so that code can name it
    without importing DuckDB.
    """


type ColumnSource = Literal["dim", "value"]
type ComparisonOperator = Literal["eq", "ne", "lt", "le", "gt", "ge"]
type BooleanOperator = Literal["and", "or"]
type ArithmeticOperator = Literal["add", "sub", "mul", "truediv", "floordiv", "mod", "pow"]
type UnaryArithmeticOperator = Literal["negate"]
type AggregateFunction = Literal[
    "row_count",
    "count",
    "n_unique",
    "sum",
    "mean",
    "min",
    "max",
    "std",
    "var",
    "quantile",
]
type SortDirection = Literal["asc", "desc"]
type Condition = ValueExpression
"""What ``filter`` and ``when`` take: a predicate, or a bool-typed expression such as a bool field,
which means "where this is true". Every predicate is a ``ValueExpression``."""
type JoinOn = Sequence[JoinKey | tuple[Column, Column]] | JoinKey | tuple[Column, Column]
"""What ``join(on=...)`` takes: one key or several, each a JoinKey or a (left, right) pair."""
type TiePolicy = Literal["first", "all"]

_CAST_TARGETS: dict[type, LeafKind] = {
    int: "int",
    float: "float",
    bool: "bool",
    str: "str",
    bytes: "bytes",
}
_CASTABLE_LEAF_KINDS: dict[LeafKind, frozenset[LeafKind]] = {
    "int": frozenset(("int", "float", "bool", "str")),
    "float": frozenset(("int", "float", "bool", "str")),
    "bool": frozenset(("int", "float", "bool", "str")),
    "str": frozenset(("int", "float", "bool", "str", "bytes")),
    "bytes": frozenset(("str", "bytes")),
}


def _require_member(value: object, allowed: TypeAliasType, what: str) -> None:
    """Raise unless ``value`` is one of the strings a ``Literal`` alias allows."""
    allowed_values = get_args(allowed.__value__)
    # The value itself, and only a str: an object that merely prints as 'add' is not one.
    if type(value) is not str or value not in allowed_values:
        options = [repr(option) for option in allowed_values]
        listed = " or ".join((", ".join(options[:-1]), options[-1]) if options[:-1] else options)
        raise QueryError(f"Unknown {what} {described(value)}; use {listed}")


class ValueExpression:
    """An expression producing a row value."""

    def _arithmetic(self, other: object, operator: ArithmeticOperator) -> ValueExpression:
        return Arithmetic(self, operator, _as_expression(other))

    def _reverse_arithmetic(self, other: object, operator: ArithmeticOperator) -> ValueExpression:
        return Arithmetic(_as_expression(other), operator, self)

    def _compare(self, other: object, operator: ComparisonOperator) -> Predicate:
        return Comparison(self, operator, _as_expression(other))

    # Not hashable: a dict or set looks a key up with ``==``, which here builds a comparison and
    # has no truth value, so an expression stored as a key could never be found again.
    __hash__ = None  # type: ignore[assignment]

    # A bool-typed expression, such as a bool field, combines as a predicate does.
    def __and__(self, other: object) -> Predicate:
        return as_predicate(self) & as_predicate(other)

    def __rand__(self, other: object) -> Predicate:
        return as_predicate(other) & as_predicate(self)

    def __or__(self, other: object) -> Predicate:
        return as_predicate(self) | as_predicate(other)

    def __ror__(self, other: object) -> Predicate:
        return as_predicate(other) | as_predicate(self)

    def __invert__(self) -> Predicate:
        return ~as_predicate(self)

    # ``==`` builds a query comparison, so it cannot also answer whether two expressions are the
    # same, and the comparison has no truth value: giving it one would let Python's ``and`` and
    # ``or`` silently keep one side of ``(a == 1) and (b == 2)``. Classes that hold expressions
    # derive from :class:`StructurallyEqual` so that their own equality never asks.
    def __eq__(self, other: object) -> Predicate:  # type: ignore[reportIncompatibleMethodOverride]
        return self._compare(other, "eq")

    def __ne__(self, other: object) -> Predicate:  # type: ignore[reportIncompatibleMethodOverride]
        return self._compare(other, "ne")

    def __lt__(self, other: object) -> Predicate:
        return self._compare(other, "lt")

    def __le__(self, other: object) -> Predicate:
        return self._compare(other, "le")

    def __gt__(self, other: object) -> Predicate:
        return self._compare(other, "gt")

    def __ge__(self, other: object) -> Predicate:
        return self._compare(other, "ge")

    def __add__(self, other: object) -> ValueExpression:
        return self._arithmetic(other, "add")

    def __radd__(self, other: object) -> ValueExpression:
        return self._reverse_arithmetic(other, "add")

    def __sub__(self, other: object) -> ValueExpression:
        return self._arithmetic(other, "sub")

    def __rsub__(self, other: object) -> ValueExpression:
        return self._reverse_arithmetic(other, "sub")

    def __mul__(self, other: object) -> ValueExpression:
        return self._arithmetic(other, "mul")

    def __rmul__(self, other: object) -> ValueExpression:
        return self._reverse_arithmetic(other, "mul")

    def __truediv__(self, other: object) -> ValueExpression:
        return self._arithmetic(other, "truediv")

    def __rtruediv__(self, other: object) -> ValueExpression:
        return self._reverse_arithmetic(other, "truediv")

    def __floordiv__(self, other: object) -> ValueExpression:
        return self._arithmetic(other, "floordiv")

    def __rfloordiv__(self, other: object) -> ValueExpression:
        return self._reverse_arithmetic(other, "floordiv")

    def __mod__(self, other: object) -> ValueExpression:
        return self._arithmetic(other, "mod")

    def __rmod__(self, other: object) -> ValueExpression:
        return self._reverse_arithmetic(other, "mod")

    def __pow__(self, other: object) -> ValueExpression:
        return self._arithmetic(other, "pow")

    def __rpow__(self, other: object) -> ValueExpression:
        return self._reverse_arithmetic(other, "pow")

    def __neg__(self) -> ValueExpression:
        return UnaryArithmetic("negate", self)

    def is_null(self) -> Predicate:
        """Whether this expression is null."""
        return NullTest(self, negated=False)

    def is_not_null(self) -> Predicate:
        """Whether this expression is not null."""
        return NullTest(self, negated=True)

    def cast(
        self, target: type[int] | type[float] | type[bool] | type[str] | type[bytes]
    ) -> ValueExpression:
        """Strictly cast this expression to one fxtr leaf type."""
        try:
            kind = _CAST_TARGETS[target]
        except (KeyError, TypeError):
            raise QueryError(
                f"cast() target must be int, float, bool, str, or bytes, got {target!r}"
            ) from None
        return Cast(self, kind)

    def fill_null(self, replacement: object) -> ValueExpression:
        """Replace null values with ``replacement`` using a compatible result type."""
        return FillNull(self, _as_expression(replacement))

    def is_in(self, values: Sequence[object]) -> Predicate:
        """Whether this scalar expression equals one value in a fixed sequence."""
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise QueryError("is_in() expects a list or tuple of values")
        return InValues(self, tuple(_as_expression(item) for item in values))

    def between(self, lower: object, upper: object) -> Predicate:
        """Whether this numeric expression is inclusively between two bounds."""
        return Between(self, _as_expression(lower), _as_expression(upper))

    def count(self) -> Aggregation:
        """Count non-null values of this expression."""
        return Aggregation("count", self)

    def n_unique(self) -> Aggregation:
        """Count distinct values, treating null as one distinct value."""
        return Aggregation("n_unique", self)

    def sum(self) -> Aggregation:
        """Sum non-null numeric values; an empty input produces zero."""
        return Aggregation("sum", self)

    def mean(self) -> Aggregation:
        """Average non-null numeric values; no values produces null."""
        return Aggregation("mean", self)

    def min(self) -> Aggregation:
        """Return the least non-null numeric or string value, or null when empty."""
        return Aggregation("min", self)

    def max(self) -> Aggregation:
        """Return the greatest non-null numeric or string value, or null when empty."""
        return Aggregation("max", self)

    def std(self) -> Aggregation:
        """Return the sample standard deviation of non-null numeric values."""
        return Aggregation("std", self)

    def var(self) -> Aggregation:
        """Return the sample variance of non-null numeric values."""
        return Aggregation("var", self)

    def quantile(self, quantile: float) -> Aggregation:
        """Return the linearly interpolated quantile of non-null numeric values."""
        return Aggregation("quantile", self, quantile)

    def median(self) -> Aggregation:
        """Return the median of non-null numeric values."""
        return self.quantile(0.5)


class Predicate(ValueExpression):
    """A Boolean expression, as ``filter`` and ``when`` take."""

    def __and__(self, other: object) -> Predicate:
        return Boolean(self, "and", as_predicate(other))

    def __or__(self, other: object) -> Predicate:
        return Boolean(self, "or", as_predicate(other))

    def __invert__(self) -> Predicate:
        return Not(self)

    def __eq__(self, other: object) -> bool:  # type: ignore[reportIncompatibleMethodOverride]
        """Whether ``other`` is the same predicate; two predicates are never compared as values.

        Raises:
            QueryError: If ``other`` is not a predicate. ``&`` and ``|`` bind tighter than ``==``,
                so ``a & b == 1`` arrives here as ``(a & b) == 1``; answering ``False`` would
                put the Python constant where the author meant a condition.
        """
        if not isinstance(other, Predicate):
            raise QueryError(
                f"A predicate was compared with {described(other)}. `&` and `|` bind tighter "
                "than `==`, so `a & b == 1` means `(a & b) == 1`; write `a & (b == 1)`"
            )
        return _structural_key(self) == _structural_key(other)

    def __ne__(self, other: object) -> bool:  # type: ignore[reportIncompatibleMethodOverride]
        return not self == other

    def __hash__(self) -> int:
        return hash(_structural_key(self))

    def __bool__(self) -> bool:
        raise TypeError(
            "A query predicate has no Python truth value: combine predicates with '&', '|', and "
            "'~', not 'and', 'or', and 'not', and test membership with is_in(), not 'in'"
        )


@dataclasses.dataclass(frozen=True, eq=False)
class Column(ValueExpression):
    """An explicitly named dimension, a fixed value-field path, or the whole value."""

    source: ColumnSource
    path: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_member(self.source, ColumnSource, "column source")
        if self.source == "dim" and len(self.path) != 1:
            raise QueryError(f"{self.source} columns need exactly one name")
        if any(not isinstance(part, str) or not part for part in self.path):
            raise QueryError(f"Column paths need non-empty string parts, got {self.path!r}")

    def field(self, name: str) -> Column:
        """Select one literal struct-field name from a value, without interpreting dots."""
        if self.source != "value":
            raise QueryError("Dimensions have no struct fields; use value().field(name)")
        if not isinstance(name, str) or not name:
            raise QueryError(f"Field names must be non-empty strings, got {name!r}")
        return Column("value", (*self.path, name))


@dataclasses.dataclass(frozen=True, eq=False)
class LiteralExpression(ValueExpression):
    """A constant, coerced to its schema; only a null may omit the schema."""

    value: FrozenValue
    schema: Schema | None

    def __post_init__(self) -> None:
        if self.schema is None:
            if self.value is not None:
                raise QueryError("A query literal without a schema must be null")
            return
        try:
            frozen = coerce_value(self.value, self.schema, "query literal")
        except ValueError as exc:
            raise QueryError(str(exc)) from exc
        require_storable_value(frozen)
        object.__setattr__(self, "value", frozen)


def require_storable_value(value: FrozenValue, what: str = "A query literal") -> None:
    """Refuse the values a fxtr array cannot hold: a non-finite float, an int beyond 64 bits,
    text that is not valid Unicode. ``what`` names the value in the error.

    A local query would hand them to DuckDB, where ``x < nan`` is true for every ``x``, while the
    wire encoding refuses them, so the same query would differ by where it ran.
    """
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise QueryError(
                f"{what} string must be valid Unicode text: {exc.reason} at position {exc.start}"
            ) from None
    if isinstance(value, float) and not math.isfinite(value):
        raise QueryError(f"{what} must be a finite number, got {value!r}")
    if type(value) is int and not -(2**63) <= value < 2**63:
        raise QueryError(f"{what} int must fit in 64 bits, got {value!r}")
    if isinstance(value, tuple):
        for item in value:
            require_storable_value(item, what)
    elif isinstance(value, Mapping):
        for item in value.values():
            require_storable_value(item, what)


@dataclasses.dataclass(frozen=True, eq=False)
class Arithmetic(ValueExpression):
    """A binary numeric operation."""

    left: ValueExpression
    operator: ArithmeticOperator
    right: ValueExpression

    def __post_init__(self) -> None:
        _require_member(self.operator, ArithmeticOperator, "arithmetic operator")


@dataclasses.dataclass(frozen=True, eq=False)
class UnaryArithmetic(ValueExpression):
    """A unary numeric operation."""

    operator: UnaryArithmeticOperator
    operand: ValueExpression

    def __post_init__(self) -> None:
        _require_member(self.operator, UnaryArithmeticOperator, "unary arithmetic operator")


@dataclasses.dataclass(frozen=True, eq=False)
class Cast(ValueExpression):
    """A strict cast to a fxtr leaf type."""

    operand: ValueExpression
    target: LeafKind

    def __post_init__(self) -> None:
        if self.target not in _CAST_TARGETS.values():
            raise QueryError(f"Unknown cast target {self.target!r}")


@dataclasses.dataclass(frozen=True, eq=False)
class FillNull(ValueExpression):
    """The first operand unless it is null, otherwise the replacement."""

    operand: ValueExpression
    replacement: ValueExpression


@dataclasses.dataclass(frozen=True, eq=False)
class Conditional(ValueExpression):
    """The true or false branch selected by a predicate."""

    predicate: Predicate
    true_value: ValueExpression
    false_value: ValueExpression


@dataclasses.dataclass(frozen=True, eq=False)
class Comparison(Predicate):
    """Two scalar expressions compared; null when either side is null."""

    left: ValueExpression
    operator: ComparisonOperator
    right: ValueExpression

    def __post_init__(self) -> None:
        _require_member(self.operator, ComparisonOperator, "comparison operator")


@dataclasses.dataclass(frozen=True, eq=False)
class Boolean(Predicate):
    """Two predicates joined by ``&`` or ``|``."""

    left: Predicate
    operator: BooleanOperator
    right: Predicate

    def __post_init__(self) -> None:
        _require_member(self.operator, BooleanOperator, "Boolean operator")


@dataclasses.dataclass(frozen=True, eq=False)
class Not(Predicate):
    """The negation of a predicate; null stays null."""

    operand: Predicate


@dataclasses.dataclass(frozen=True, eq=False)
class NullTest(Predicate):
    """Whether an expression is null (or, when ``negated``, is not); never null itself."""

    operand: ValueExpression
    negated: bool


@dataclasses.dataclass(frozen=True, eq=False)
class InValues(Predicate):
    """Whether a scalar expression equals one of a fixed tuple of options."""

    operand: ValueExpression
    options: tuple[ValueExpression, ...]


@dataclasses.dataclass(frozen=True, eq=False)
class Between(Predicate):
    """Whether a numeric expression lies between two bounds, inclusively."""

    operand: ValueExpression
    lower: ValueExpression
    upper: ValueExpression


@dataclasses.dataclass(frozen=True)
class When:
    """An immutable conditional builder returned by :func:`when`."""

    branches: tuple[tuple[Predicate, ValueExpression], ...]
    predicate: Predicate

    def then(self, value: object) -> Then:
        return Then((*self.branches, (self.predicate, _as_expression(value))))


@dataclasses.dataclass(frozen=True)
class Then:
    """An immutable conditional builder waiting for another case or its false branch."""

    branches: tuple[tuple[Predicate, ValueExpression], ...]

    def when(self, predicate: Condition) -> When:
        """Add another predicate/value case before the final fallback."""
        return When(self.branches, as_predicate(predicate))

    def otherwise(self, value: object) -> ValueExpression:
        result = _as_expression(value)
        for predicate, true_value in reversed(self.branches):
            result = Conditional(predicate, true_value, result)
        return result


class StructurallyEqual:
    """Equality for a dataclass whose fields may hold query expressions.

    The equality ``@dataclass`` generates compares fields with ``==`` and takes the truth of the
    answer, which for two expressions is a :class:`Comparison` and has none. Declare the
    dataclass with ``eq=False`` and derive from this class instead.
    """

    def __eq__(self, other: object) -> bool:
        return type(other) is type(self) and _structural_key(self) == _structural_key(other)

    def __hash__(self) -> int:
        return hash(_structural_key(self))


def _structural_key(node: object) -> object:
    """A hashable key that is equal exactly when two expression or plan trees are the same.

    Every expression, and every :class:`StructurallyEqual` class, is a dataclass, so its class
    and fields describe it completely. Anything else is compared by its own equality.
    """
    if isinstance(node, (ValueExpression, StructurallyEqual)):
        fields = dataclasses.fields(cast("DataclassInstance", node))
        return (type(node), *(_structural_key(getattr(node, field.name)) for field in fields))
    if isinstance(node, tuple):
        return tuple(_structural_key(item) for item in cast("tuple[object, ...]", node))
    if isinstance(node, Mapping):
        items = cast("Mapping[object, object]", node).items()
        return frozenset((name, _structural_key(item)) for name, item in items)
    if isinstance(node, float):
        # 0.0 == -0.0, and they are different stored bytes.
        return (float, repr(node))
    if isinstance(node, (bool, int)):
        # 1 == True == 1.0, and they are different query values.
        return (type(node), node)
    return node


@dataclasses.dataclass(frozen=True, eq=False)
class SortField(StructurallyEqual):
    """One selected sort expression and its direction; null values always sort last."""

    expression: ValueExpression
    direction: SortDirection = "asc"

    def __post_init__(self) -> None:
        if not isinstance(self.expression, ValueExpression):
            raise QueryError(_sort_key_error(self.expression))
        _require_member(self.direction, SortDirection, "sort direction")


@dataclasses.dataclass(frozen=True, eq=False)
class Aggregation(StructurallyEqual):
    """One row or expression aggregation."""

    function: AggregateFunction
    expression: ValueExpression | None = None
    quantile: float | None = None

    def __post_init__(self) -> None:
        _require_member(self.function, AggregateFunction, "aggregate function")
        if self.function == "row_count":
            if self.expression is not None:
                raise QueryError(
                    "The row count takes no expression; use expression.count() to count its"
                    " non-null values"
                )
        elif self.expression is None:
            raise QueryError(f"{self.function} requires an expression")
        if self.function == "quantile":
            quantile = self.quantile
            if (
                not isinstance(quantile, (int, float))
                or isinstance(quantile, bool)
                or not 0 <= quantile <= 1
            ):
                raise QueryError(f"Quantile must be a number from 0 to 1, got {quantile!r}")
            # -0.0 equals 0.0 but encodes to different bytes; keep one encoding per quantile.
            object.__setattr__(self, "quantile", float(quantile) or 0.0)
        elif self.quantile is not None:
            raise QueryError(f"{self.function} does not take a quantile")


@dataclasses.dataclass(frozen=True, eq=False)
class JoinKey(StructurallyEqual):
    """One equality pairing between a left and right query column."""

    left: Column
    right: Column

    def __post_init__(self) -> None:
        if not isinstance(self.left, Column) or not isinstance(self.right, Column):
            raise QueryError("Join keys must explicitly use dim(name) or value().field(name)")


def dim(name: str) -> Column:
    """The dimension named ``name``."""
    return Column("dim", (name,))


def value() -> Column:
    """The whole row value; use chained ``field`` calls to select struct fields."""
    return Column("value", ())


def literal(value: object, schema: SchemaSpec | None = None) -> ValueExpression:
    """A constant query value, with an inferred or explicit fxtr schema.

    Pass an explicit nullable schema for a standalone null, for example
    ``literal(None, optional(int))``.
    """
    if schema is None:
        return _as_expression(value)
    try:
        resolved = as_schema(schema)
        frozen = coerce_value(value, resolved, "query literal")
    except ValueError as exc:
        raise QueryError(str(exc)) from exc
    return LiteralExpression(frozen, resolved)


def when(predicate: Condition) -> When:
    """Begin ``when(predicate).then(value).otherwise(value)``."""
    return When((), as_predicate(predicate))


def asc(expression: ValueExpression) -> SortField:
    """Order by ``expression``, least first."""
    return SortField(expression, "asc")


def desc(expression: ValueExpression) -> SortField:
    """Order by ``expression``, greatest first."""
    return SortField(expression, "desc")


type OrderBy = Sequence[SortField | ValueExpression] | SortField | ValueExpression
"""What ``limit(order_by=...)`` accepts: one sort key or several, each an expression (ascending)
or an :func:`asc`/:func:`desc` wrapper."""


def as_sort_fields(order_by: OrderBy) -> tuple[SortField, ...]:
    """Normalize an ``order_by`` argument; a bare expression orders ascending."""
    # A str is a sequence too, and is one mistaken key, not a key per character.
    single = isinstance(order_by, (SortField, ValueExpression, str))
    items = [order_by] if single else order_by
    fields: list[SortField] = []
    for item in items:
        if isinstance(item, SortField):
            fields.append(item)
        elif isinstance(item, ValueExpression):
            fields.append(SortField(item))
        else:
            raise QueryError(_sort_key_error(item))
    return tuple(fields)


def _sort_key_error(key: object) -> str:
    hint = (
        f"; write value().field({key!r}) for a field"
        if isinstance(key, str) and len(key) <= 40
        else ""
    )
    return (
        "order_by takes query expressions, optionally wrapped in asc() or desc(), "
        f"got {described(key)}{hint}"
    )


def count() -> Aggregation:
    """Count all rows, including rows containing null fields."""
    return Aggregation("row_count")


def _as_expression(value: object) -> ValueExpression:
    if isinstance(value, ValueExpression):
        return value
    if value is None:
        return LiteralExpression(None, None)
    if isinstance(value, Aggregation):
        raise QueryError(_AGGREGATION_OUTSIDE_AGGREGATE)
    try:
        schema = infer_schema(value)
        frozen = coerce_value(value, schema)
    except ValueError as exc:
        raise QueryError(f"Cannot use {value!r} as a query literal: {exc}") from exc
    return LiteralExpression(frozen, schema)


def described(value: object) -> str:
    """``value`` for an error message: its repr if that is short, else only its type.

    The values that reach a message by mistake include whole arrays, whose repr holds every row.
    """
    if value is None or type(value) in (bool, int, float, str, bytes):
        text = repr(value)
        if len(text) <= 80:
            return text
    return f"a value of type {type(value).__name__}"


_TRUE = LiteralExpression(True, LeafSchema("bool"))

_AGGREGATION_OUTSIDE_AGGREGATE = (
    "an aggregation such as mean() or count() reduces many rows to one value, so it can only be "
    "a result field of aggregate(), such as aggregate({'mean': value().mean()})"
)


def as_predicate(value: object) -> Predicate:
    """``value`` as a predicate: a predicate itself, or a bool-typed expression, which means
    "where this is true".

    Raises:
        QueryError: If ``value`` is neither.
    """
    if isinstance(value, Predicate):
        return value
    if isinstance(value, ValueExpression):
        # A bool column or other bool-typed expression, such as a selected comparison. The type
        # check that follows rejects an expression of any other type.
        return Comparison(value, "eq", _TRUE)
    if isinstance(value, Then):
        raise QueryError("A when(...).then(...) needs .otherwise(...) before it is an expression")
    if isinstance(value, bool):
        # Python evaluated something itself: an `is` test, or a comparison whose operands `&`
        # or `|` had already combined.
        raise QueryError(
            f"Expected a query predicate, got {value!r}. Test for null with is_null() or "
            "is_not_null(), not `is None`. `&` and `|` bind tighter than `==` and `<`, so "
            "`a == 1 & b == 2` means `a == (1 & b) == 2`; parenthesize each comparison"
        )
    if isinstance(value, str):
        raise QueryError(
            f"Expected a query predicate, got the text {described(value)}; build it from "
            "expressions, such as value().field('score') > 0.5, or write SQL with fxtr.sql"
        )
    raise QueryError(
        f"Expected a query predicate, got {described(value)}. If this is part of a comparison, "
        "parenthesize it: `&` and `|` bind tighter than `==` and `<`, so `a == 1 & b == 2` "
        "means `a == (1 & b) == 2`"
    )


def as_value_expression(value: object, what: str) -> ValueExpression:
    """``value`` if it is a query expression.

    Raises:
        QueryError: Otherwise, saying how to write the constant or field the caller meant.
    """
    if isinstance(value, ValueExpression):
        return value
    if isinstance(value, Then):
        raise QueryError(f"{what} is a when(...).then(...) that needs .otherwise(...)")
    if isinstance(value, Aggregation):
        raise QueryError(f"{what}: {_AGGREGATION_OUTSIDE_AGGREGATE}")
    hint = (
        f"write value().field({value!r}) for a field, or literal({value!r}) for the text"
        if isinstance(value, str) and len(value) <= 40
        else "wrap a constant in literal(...)"
    )
    raise QueryError(f"{what} must be a query expression, got {described(value)}; {hint}")


def _nullable(schema: Schema) -> bool:
    return isinstance(schema, NullableSchema)


def _inner(schema: Schema) -> Schema:
    return schema.inner if isinstance(schema, NullableSchema) else schema


def require_distinct_names_ignoring_case(type: ArrayType) -> None:
    """Raise unless DuckDB can tell ``type``'s dims apart, and the fields of each of its structs.

    Raises:
        QueryError: If two dims, or two fields of one struct, differ only by case, or a name
            holds a NUL character, which no quoted SQL identifier can.
    """

    def check(names: Sequence[str], where: str) -> None:
        seen: dict[str, str] = {}
        for name in names:
            if "\0" in name:
                raise QueryError(
                    f"{where} name {name!r} holds a NUL character, which DuckDB cannot name;"
                    f" rename it before querying {type}"
                )
            other = seen.setdefault(name.casefold(), name)
            if other != name:
                raise QueryError(
                    f"{where} {other!r} and {name!r} differ only by case, which DuckDB cannot"
                    f" tell apart; rename one before querying {type}"
                )

    check(type.dim_names, "Dimensions")
    pending: list[Schema] = [type.schema]
    while pending:
        match pending.pop():
            case NullableSchema(inner) | ListSchema(inner):
                pending.append(inner)
            case StructSchema(fields):
                check(tuple(fields), "Struct fields")
                pending.extend(fields.values())
            case _:
                pass


def _queryable(type: ArrayType) -> ArrayType:
    require_distinct_names_ignoring_case(type)
    return type


def resolve_column(
    type: ArrayType, column: Column
) -> tuple[Literal["dim", "value"], int | None, Schema]:
    """Where ``column`` reads from: ``"dim"`` with its position, or ``"value"``, and its schema.

    A field read through a nullable struct is itself nullable.

    Raises:
        ArrayTypeError: If ``type`` has no such dimension or value field path.
    """
    source = column.source
    if source == "dim":
        name = column.path[0]
        try:
            position = type.dim_names.index(name)
        except ValueError:
            raise ArrayTypeError(f"No dimension {name!r} in {type}") from None
        return "dim", position, LeafSchema(type.dims[position].kind)

    schema = type.schema
    nullable = False
    for part in column.path:
        if isinstance(schema, NullableSchema):
            nullable = True
            schema = schema.inner
        if not isinstance(schema, StructSchema) or part not in schema.fields:
            rendered = ".".join(column.path)
            raise ArrayTypeError(f"No value field path {rendered!r} in {type.schema}")
        schema = schema.fields[part]
    if nullable and not isinstance(schema, NullableSchema):
        schema = NullableSchema(schema)
    return "value", None, schema


def expression_schema(type: ArrayType, expression: ValueExpression) -> Schema | None:
    """The schema ``expression`` produces over rows of ``type``; ``None`` for an untyped null.

    Raises:
        ArrayTypeError: If the expression does not fit ``type`` (a missing column, a non-numeric
            arithmetic operand, incompatible branches, ...).
        QueryError: If the expression itself is malformed.
    """
    match expression:
        case Column():
            return resolve_column(type, expression)[2]
        case LiteralExpression(schema=schema):
            return schema
        case Arithmetic(left=left, operator=operator, right=right):
            left_schema = _numeric_expression_schema(type, left, "Arithmetic left operand")
            right_schema = _numeric_expression_schema(type, right, "Arithmetic right operand")
            left_inner = cast("LeafSchema", _inner(left_schema))
            right_inner = cast("LeafSchema", _inner(right_schema))
            kind: LeafKind = (
                "float"
                if operator in ("truediv", "pow")
                or left_inner.kind == "float"
                or right_inner.kind == "float"
                else "int"
            )
            return _with_nullability(
                LeafSchema(kind), _nullable(left_schema) or _nullable(right_schema)
            )
        case UnaryArithmetic(operand=operand):
            return _numeric_expression_schema(type, operand, "Arithmetic operand")
        case Cast(operand=operand, target=target):
            source = expression_schema(type, operand)
            if source is None:
                return NullableSchema(LeafSchema(target))
            source_inner = _inner(source)
            if not isinstance(source_inner, LeafSchema):
                raise ArrayTypeError(f"cast() requires a leaf value, got {source}")
            if source_inner.kind not in _CASTABLE_LEAF_KINDS[target]:
                raise ArrayTypeError(f"Cannot cast {source} to {target}")
            return _with_nullability(LeafSchema(target), _nullable(source))
        case FillNull(operand=operand, replacement=replacement):
            operand_schema = expression_schema(type, operand)
            replacement_schema = expression_schema(type, replacement)
            return _common_value_schema(
                operand_schema,
                replacement_schema,
                nullable=_nullable_or_untyped(operand_schema)
                and _nullable_or_untyped(replacement_schema),
                where="fill_null()",
            )
        case Conditional(predicate=predicate, true_value=true_value, false_value=false_value):
            _predicate_schema(type, predicate)
            true_schema = expression_schema(type, true_value)
            false_schema = expression_schema(type, false_value)
            return _common_value_schema(
                true_schema,
                false_schema,
                nullable=_nullable_or_untyped(true_schema) or _nullable_or_untyped(false_schema),
                where="conditional branches",
            )
        case Predicate():
            return _predicate_schema(type, expression)
        case _:
            raise QueryError(f"Unknown value expression {expression!r}")


def _is_scalar(schema: Schema) -> bool:
    return isinstance(_inner(schema), (LeafSchema, IdSchema))


def _nullable_or_untyped(schema: Schema | None) -> bool:
    return schema is None or _nullable(schema)


def _with_nullability(schema: Schema, nullable: bool) -> Schema:
    return NullableSchema(schema) if nullable else schema


def _common_value_schema(
    left: Schema | None,
    right: Schema | None,
    *,
    nullable: bool,
    where: str,
) -> Schema:
    if left is None and right is None:
        raise ArrayTypeError(f"{where} cannot infer a type from two untyped null values")
    if left is None:
        assert right is not None
        return _with_nullability(_inner(right), nullable)
    if right is None:
        return _with_nullability(_inner(left), nullable)

    left_inner = _inner(left)
    right_inner = _inner(right)
    if (
        isinstance(left_inner, LeafSchema)
        and isinstance(right_inner, LeafSchema)
        and left_inner.kind in ("int", "float")
        and right_inner.kind in ("int", "float")
    ):
        kind: LeafKind = (
            "float" if left_inner.kind == "float" or right_inner.kind == "float" else "int"
        )
        return _with_nullability(LeafSchema(kind), nullable)
    if is_assignable(left_inner, right_inner):
        return _with_nullability(right_inner, nullable)
    if is_assignable(right_inner, left_inner):
        return _with_nullability(left_inner, nullable)
    raise ArrayTypeError(f"{where} must have compatible types, got {left} and {right}")


def _numeric_expression_schema(type: ArrayType, expression: ValueExpression, where: str) -> Schema:
    schema = expression_schema(type, expression)
    inner = None if schema is None else _inner(schema)
    if not isinstance(inner, LeafSchema) or inner.kind not in ("int", "float"):
        raise ArrayTypeError(f"{where} must be an int or float, got {schema}")
    assert schema is not None
    return schema


def _compatible_scalars(left: Schema, right: Schema) -> bool:
    left_inner = _inner(left)
    right_inner = _inner(right)
    if (
        isinstance(left_inner, LeafSchema)
        and isinstance(right_inner, LeafSchema)
        and left_inner.kind in ("int", "float")
        and right_inner.kind in ("int", "float")
    ):
        return True
    return is_assignable(left_inner, right_inner) or is_assignable(right_inner, left_inner)


def _predicate_schema(type: ArrayType, predicate: Predicate) -> Schema:
    boolean = LeafSchema("bool")
    match predicate:
        case Comparison(left=left, operator=operator, right=right):
            left_schema = expression_schema(type, left)
            right_schema = expression_schema(type, right)
            if left_schema is None or right_schema is None:
                raise QueryError("Use is_null() or is_not_null() for explicit null tests")
            if not _is_scalar(left_schema) or not _is_scalar(right_schema):
                raise ArrayTypeError("Query comparisons require scalar columns and literals")
            if not _compatible_scalars(left_schema, right_schema):
                if operator == "eq" and _structural_key(right) == _structural_key(_TRUE):
                    # What as_predicate builds from an expression used as a condition.
                    raise ArrayTypeError(
                        f"A condition must be a bool expression, got one of type {left_schema}"
                    )
                raise ArrayTypeError(f"Cannot compare {left_schema} with {right_schema}")
            if operator not in ("eq", "ne"):
                # The same kinds ``min``, ``max``, and ``order_by`` can order. Compatibility
                # above already rules out comparing a string with a number.
                for side in (left_schema, right_schema):
                    inner = _inner(side)
                    if not isinstance(inner, LeafSchema) or inner.kind not in (
                        "int",
                        "float",
                        "str",
                    ):
                        raise ArrayTypeError(
                            f"Ordering comparison {operator!r} requires int, float, or str"
                            f" operands, got {side}"
                        )
            return _with_nullability(boolean, _nullable(left_schema) or _nullable(right_schema))
        case Boolean(left=left, right=right):
            left_schema = _predicate_schema(type, left)
            right_schema = _predicate_schema(type, right)
            return _with_nullability(boolean, _nullable(left_schema) or _nullable(right_schema))
        case Not(operand=operand):
            return _predicate_schema(type, operand)
        case NullTest(operand=operand):
            expression_schema(type, operand)
            return boolean
        case InValues(operand=operand, options=options):
            operand_schema = expression_schema(type, operand)
            if operand_schema is None or not _is_scalar(operand_schema):
                raise ArrayTypeError(
                    f"is_in() requires a typed scalar expression, got {operand_schema}"
                )
            nullable = _nullable(operand_schema)
            for option in options:
                option_schema = expression_schema(type, option)
                if option_schema is None:
                    nullable = True
                    continue
                if not _is_scalar(option_schema) or not _compatible_scalars(
                    operand_schema, option_schema
                ):
                    raise ArrayTypeError(
                        "is_in() values must be scalar and compatible with "
                        f"{operand_schema}, got {option_schema}"
                    )
                nullable = nullable or _nullable(option_schema)
            return boolean if not options else _with_nullability(boolean, nullable)
        case Between(operand=operand, lower=lower, upper=upper):
            operand_schema = _numeric_expression_schema(type, operand, "between() expression")
            lower_schema = _numeric_expression_schema(type, lower, "between() lower bound")
            upper_schema = _numeric_expression_schema(type, upper, "between() upper bound")
            return _with_nullability(
                boolean,
                _nullable(operand_schema) or _nullable(lower_schema) or _nullable(upper_schema),
            )
        case _:
            raise QueryError(f"Unknown predicate {predicate!r}")


def within_recursion_limit[**P, R](type_function: Callable[P, R]) -> Callable[P, R]:
    """``type_function``, raising :class:`QueryError` where typing an expression or a plan nests
    deeper than Python's recursion limit, as encoding, decoding and compiling one do."""

    @functools.wraps(type_function)
    def typed(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return type_function(*args, **kwargs)
        except RecursionError as exc:
            raise QueryError("Query typing exceeds Python's recursion limit") from exc

    return typed


@within_recursion_limit
def filter_type(input: ArrayType, predicate: Predicate) -> ArrayType:
    """Type-check a filter and return its unchanged array type."""
    _predicate_schema(_queryable(input), as_predicate(predicate))
    return input


def sel_predicate(key: Mapping[str, KeyValue]) -> Predicate | None:
    """The rows ``sel(key)`` keeps: those where every named dim equals its value.

    ``None`` for an empty key, which keeps every row.
    """
    predicate: Predicate | None = None
    for name, wanted in key.items():
        matches = cast("Predicate", dim(name) == wanted)
        predicate = matches if predicate is None else predicate & matches
    return predicate


def sel_query_type(input: ArrayType, key: Mapping[str, KeyValue]) -> ArrayType:
    """The type of a lazy ``sel``: ``input`` without the dims ``key`` names.

    Raises:
        ArrayTypeError: If a named dim does not exist, or a value is not of its dim's
            kind.
        QueryError: If a value is not an int or a str, which is all a dim key can be.
    """
    output = sel_type(_queryable(input), key)
    for name, wanted in key.items():
        # Checked here and not left to the comparison below, which would let 1.0 equal an int
        # dim on the paths that do not encode the key.
        if type(wanted) not in (int, str):
            raise QueryError(
                f"sel value for dim {name!r} must be an int or a str, got {described(wanted)}"
            )
    predicate = sel_predicate(key)
    if predicate is not None:
        filter_type(input, predicate)
    return output


def rename_query_type(input: ArrayType, names: Mapping[str, str]) -> ArrayType:
    """The type of a lazy ``rename``: ``input`` with dims renamed, old name to new name.

    Raises:
        ArrayTypeError: If an old name does not exist, or the renamed dims are not unique.
        QueryError: If two of the renamed dims differ only by case.
    """
    return _queryable(rename_type(_queryable(input), names))


def retype_query_type(input: ArrayType, target: ArrayType) -> ArrayType:
    """The type of a lazy ``retype``: ``target`` as it is stored, without presentation.

    Raises:
        ArrayTypeError: If ``input`` is not assignable to ``target``.
    """
    if not is_assignable_array(_queryable(input), target):
        raise ArrayTypeError(f"Cannot retype a query of type {input} as {target}")
    return _queryable(target.strip_presentation())


@within_recursion_limit
def select_type(input: ArrayType, fields: Mapping[str, ValueExpression]) -> ArrayType:
    """The input dimensions and a struct containing the named selected expressions."""
    _queryable(input)
    schemas: dict[str, Schema] = {}
    for name, expression in fields.items():
        schema = expression_schema(input, expression)
        if schema is None:
            raise QueryError(f"Selected field {name!r} has an untyped null literal")
        schemas[name] = schema
    return _queryable(ArrayType(input.dims, StructSchema(frozendict(schemas))))


@within_recursion_limit
def select_value_type(input: ArrayType, expression: ValueExpression) -> ArrayType:
    """The input dimensions and the selected expression as the complete row value."""
    schema = expression_schema(_queryable(input), expression)
    if schema is None:
        raise QueryError("A selected value cannot be an untyped null literal")
    # A struct-valued expression can name fields that differ only by case, which DuckDB merges.
    return _queryable(ArrayType(input.dims, schema))


@within_recursion_limit
def limit_type(
    input: ArrayType,
    count: int,
    order_by: Sequence[SortField],
    ties: TiePolicy,
) -> ArrayType:
    """Type-check a limit operation and return its unchanged array type."""
    _queryable(input)
    if type(count) is not int or not 0 <= count < 2**63:
        raise QueryError(f"Limit count must be a non-negative 64-bit int, got {count!r}")
    _require_member(ties, TiePolicy, "tie policy")
    if ties == "all" and not order_by:
        # Without an ordering every row ties at the cutoff, so "all" could only mean no limit.
        raise QueryError("ties='all' needs an order_by that decides which rows tie at the cutoff")
    for sort in order_by:
        schema = expression_schema(input, sort.expression)
        if schema is None or not isinstance(_inner(schema), LeafSchema):
            raise ArrayTypeError(f"Sort fields must be scalar leaves, got {schema}")
    return input


@within_recursion_limit
def aggregate_type(
    input: ArrayType,
    aggregations: Mapping[str, Aggregation],
    group_by: Mapping[str, ValueExpression],
) -> ArrayType:
    """Type-check aggregations and derive their grouped result type.

    The result's dims are the ``group_by`` names in the order given.
    """
    _queryable(input)
    dims: list[Dim] = []
    for name, expression in group_by.items():
        schema = expression_schema(input, expression)
        if schema is None:
            raise ArrayTypeError(f"Group {name!r} must have a concrete type")
        inner = _inner(schema)
        if (
            _nullable(schema)
            or not isinstance(inner, LeafSchema)
            or inner.kind not in ("int", "str")
        ):
            raise ArrayTypeError(
                f"Group {name!r} must be a non-null integer or string column, got {schema}"
            )
        dims.append(Dim(name, cast("Literal['int', 'str']", inner.kind)))

    fields: dict[str, Schema] = {}
    for name, aggregation in aggregations.items():
        function = aggregation.function
        if function == "row_count":
            fields[name] = LeafSchema("int")
            continue
        assert aggregation.expression is not None
        schema = expression_schema(input, aggregation.expression)
        if function == "count":
            if schema is None:
                raise ArrayTypeError("count requires a typed expression")
            fields[name] = LeafSchema("int")
            continue
        inner = None if schema is None else _inner(schema)
        if function == "n_unique":
            if inner is None or not _is_scalar(inner):
                raise ArrayTypeError(f"n_unique requires a scalar column, got {schema}")
            fields[name] = LeafSchema("int")
            continue
        if function in ("min", "max"):
            if not isinstance(inner, LeafSchema) or inner.kind not in ("int", "float", "str"):
                raise ArrayTypeError(
                    f"{function} requires an int, float, or string column, got {schema}"
                )
            fields[name] = NullableSchema(inner)
            continue
        if not isinstance(inner, LeafSchema) or inner.kind not in ("int", "float"):
            raise ArrayTypeError(f"{function} requires an int or float column, got {schema}")
        fields[name] = inner if function == "sum" else NullableSchema(LeafSchema("float"))
    return _queryable(ArrayType(tuple(dims), StructSchema(frozendict(fields))))


def as_join_keys(
    on: JoinOn,
) -> tuple[JoinKey, ...]:
    """The join keys ``on`` names: one key or several, each a JoinKey or a pair of columns.

    Raises:
        QueryError: If ``on`` is anything else, or names no key.
    """
    example = "on=[(dim('model'), value().field('model'))]"
    if isinstance(on, JoinKey) or (
        isinstance(on, tuple) and len(on) == 2 and all(isinstance(side, Column) for side in on)
    ):
        on = [cast("JoinKey | tuple[Column, Column]", on)]
    if isinstance(on, (str, bytes, ValueExpression)) or not isinstance(on, Sequence):
        raise QueryError(
            f"Join keys are pairs of a left column and a right column, such as {example}; "
            f"got {described(on)}"
        )
    keys: list[JoinKey] = []
    for item in on:
        if isinstance(item, JoinKey):
            keys.append(item)
        elif isinstance(item, tuple) and len(item) == 2:
            left, right = item
            keys.append(JoinKey(left, right))
        else:
            raise QueryError(
                f"Each join key is a JoinKey or a pair of columns, such as {example}; "
                f"got {described(item)}"
            )
    if not keys:
        raise QueryError("An inner join needs at least one equality key")
    return tuple(keys)


def join_dimensions(
    left: ArrayType, right: ArrayType, on: Sequence[JoinKey], right_name: str
) -> tuple[tuple[Dim, ...], tuple[int, ...]]:
    """The dims of an inner join, and the positions of the right dims it retains.

    Every left dim comes first. A right dim used as an equality key is omitted, since its value
    equals the left column it was matched with. A retained right dim whose name is already taken
    (ignoring case, as DuckDB does) is prefixed with ``right_name``.
    """
    omitted_right: set[int] = set()
    for key in on:
        right_resolved = resolve_column(right, key.right)
        if right_resolved[0] == "dim":
            assert right_resolved[1] is not None
            omitted_right.add(right_resolved[1])

    dims = list(left.dims)
    taken = {name.casefold() for name in left.dim_names}
    retained: list[int] = []
    for position, source_dim in enumerate(right.dims):
        if position in omitted_right:
            continue
        name = source_dim.name
        if name.casefold() in taken:
            name = f"{right_name}_{name}"
            suffix = 2
            while name.casefold() in taken:
                name = f"{right_name}_{source_dim.name}_{suffix}"
                suffix += 1
        dims.append(Dim(name, source_dim.kind))
        taken.add(name.casefold())
        retained.append(position)
    return tuple(dims), tuple(retained)


@within_recursion_limit
def join_type(
    left: ArrayType,
    right: ArrayType,
    on: Sequence[JoinKey],
    left_name: str,
    right_name: str,
) -> ArrayType:
    """Derive the type of an inner equality join."""
    if not on:
        raise QueryError("An inner join needs at least one equality key")
    if not left_name or not right_name or left_name == right_name:
        raise QueryError("Join value-field names must be non-empty and distinct")
    _queryable(left)
    _queryable(right)
    for key in on:
        left_schema = resolve_column(left, key.left)[2]
        right_schema = resolve_column(right, key.right)[2]
        if not _is_scalar(left_schema) or not _is_scalar(right_schema):
            raise ArrayTypeError("Join keys must be dimensions or scalar value fields")
        if not (
            is_assignable(_inner(left_schema), _inner(right_schema))
            or is_assignable(_inner(right_schema), _inner(left_schema))
        ):
            raise ArrayTypeError(f"Cannot join {left_schema} to {right_schema}")
    dims, _ = join_dimensions(left, right, on, right_name)
    return _queryable(
        ArrayType(
            dims,
            StructSchema(frozendict({left_name: left.schema, right_name: right.schema})),
        )
    )
