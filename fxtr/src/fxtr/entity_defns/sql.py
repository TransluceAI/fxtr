"""Checked SQL text and immutable parameters for array queries.

User SQL may read only the tables it was given by name. The arrays themselves are registered
with DuckDB under private names, and a named table can be a query over one of them (a filter,
say), so SQL that could name a registered relation would read rows its table does not hold.
:func:`validate_sql_reads` therefore checks DuckDB's own parse tree: every table read is a given
name or a ``WITH`` query of the statement, and the only table functions are ones that read
nothing but their arguments.
"""

from __future__ import annotations

import dataclasses
import json
import math
import string
from collections.abc import Collection, Mapping, Sequence
from functools import lru_cache
from typing import cast

from frozendict import frozendict

from fxtr.core.entities import BoundID
from fxtr.core.entity_data import EntityID
from fxtr.entity_defns.query import QueryError, require_storable_value
from fxtr.entity_defns.schema import FrozenValue, ValueInput

type SQLParameters = Mapping[str, ValueInput] | Sequence[ValueInput]

PRIVATE_NAME_PREFIX = "__fxtr_"
"""Start of every name fxtr gives a DuckDB relation or alias; user SQL may not use it."""

# Table functions that compute rows from their arguments alone. Every other one is refused,
# because DuckDB's include ones that read a relation by a name built at run time, run SQL
# passed as a string or as a serialized statement, and list the catalog.
_TABLE_FUNCTIONS = frozenset({"generate_series", "range", "unnest"})

# Scalar functions refused, with why. Four concern the host and not the data: they report the
# SQL fxtr generated around the author's statement (with the names it registered the arrays
# under) and the host's DuckDB settings and variables, or write into its DuckDB log. A seed
# stays on the connection, which the executor reuses, so it would fix the random values of
# later, unrelated queries. A sleep computes nothing, and a workflow's query nodes run with no
# deadline to interrupt one.
_HOST = "it reads or writes the host that runs the query, not the query's tables"
_REFUSED_FUNCTIONS: Mapping[str, str] = {
    "current_query": _HOST,
    "current_setting": _HOST,
    "getvariable": _HOST,
    "write_log": _HOST,
    "setseed": "its seed outlives the query and would decide other queries' random values",
    "sleep_ms": "a query computes from its tables and does not wait",
    "pg_sleep": "a query computes from its tables and does not wait",
}


def _freeze_parameter(value: object, where: str) -> FrozenValue:
    if value is None or type(value) in (bool, int, str, bytes):
        return cast("FrozenValue", value)
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, BoundID):
        value = value.entity_id
    if isinstance(value, EntityID):
        return value
    if isinstance(value, Mapping):
        fields: dict[str, FrozenValue] = {}
        for name, item in cast("Mapping[object, object]", value).items():
            if not isinstance(name, str):
                raise QueryError(f"{where} struct keys must be strings, got {name!r}")
            fields[name] = _freeze_parameter(item, f"{where}.{name}")
        if not fields:
            # DuckDB would bind an empty mapping as a MAP, not a struct.
            raise QueryError(f"{where} cannot be an empty struct")
        return frozendict(fields)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = tuple(
            _freeze_parameter(item, f"{where}[{index}]")
            for index, item in enumerate(cast("Sequence[object]", value))
        )
        # DuckDB types a list parameter from its items, converting to one type where they
        # differ: [1, "a"] would bind as two strings, and structs with other fields gain nulls.
        shapes = {_shape(item) for item in items if item is not None}
        if len(shapes) > 1:
            raise QueryError(f"{where} must hold values of one type, got {sorted(shapes)}")
        return items
    raise QueryError(f"{where} must be a canonical fxtr value, got {value!r}")


def _shape(value: FrozenValue) -> str:
    """The type DuckDB would give a frozen parameter value, as text to compare and show."""
    if isinstance(value, frozendict):
        fields = cast("frozendict[str, FrozenValue]", value)
        return "{" + ", ".join(f"{name}: {_shape(item)}" for name, item in fields.items()) + "}"
    if isinstance(value, tuple):
        items = cast("tuple[FrozenValue, ...]", value)
        shapes = {_shape(item) for item in items if item is not None}
        return f"list[{shapes.pop() if shapes else '?'}]"
    return "id" if isinstance(value, EntityID) else type(value).__name__


def normalize_sql_parameters(parameters: SQLParameters) -> frozendict[str, FrozenValue]:
    """Freeze parameters; positional parameters use DuckDB's one-based names."""
    if isinstance(parameters, Mapping):
        items = parameters.items()
    elif isinstance(parameters, Sequence) and not isinstance(parameters, (str, bytes)):
        items = ((str(index), item) for index, item in enumerate(parameters, start=1))
    else:
        raise QueryError("SQL parameters must be a name mapping or a positional sequence")
    normalized: dict[str, FrozenValue] = {}
    for name, value in items:
        if not isinstance(name, str) or not name:
            raise QueryError(f"SQL parameter names must be non-empty strings, got {name!r}")
        folded = _folded(name)
        if folded in normalized:
            raise QueryError(f"SQL parameter names differ only in case: {name!r}")
        frozen = _freeze_parameter(value, f"SQL parameter {name!r}")
        # The same rules as a literal written in an expression, and for the same reason:
        # the wire refuses these values, so a local query must refuse them too.
        require_storable_value(frozen, f"SQL parameter {name!r}")
        normalized[folded] = frozen
    return frozendict(normalized)


_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _folded(name: str) -> str:
    """``name`` as DuckDB compares relation names: ASCII letters lowered, nothing else changed.

    ``str.casefold`` would also equate ``"duckdb_view\u017f"`` (a long s) with ``duckdb_views``.
    DuckDB does not, so a ``WITH`` query of the first name would pass for the catalog view of the
    second.
    """
    return name.translate(_ASCII_LOWER)


def validate_sql_table_names(names: Sequence[str]) -> None:
    seen: set[str] = set()
    for name in names:
        if isinstance(name, str):
            _require_unicode(name, "SQL table name")
        if not isinstance(name, str) or not name or "\0" in name:
            shown = repr(name) if isinstance(name, str) else f"a {type(name).__name__}"
            raise QueryError(
                f"SQL table names must be non-empty strings without NUL characters, got {shown}"
            )
        folded = _folded(name)
        if folded.startswith(PRIVATE_NAME_PREFIX):
            raise QueryError(f"SQL table name {name!r} is reserved for fxtr")
        if folded in seen:
            raise QueryError(f"SQL table names must be distinct ignoring case: {name!r}")
        seen.add(folded)


@dataclasses.dataclass(frozen=True)
class _ParsedSQL:
    """One checked ``SELECT``: its text without a trailing ``;``, and what it refers to."""

    text: str
    placeholders: tuple[tuple[int, int, str], ...]
    """``(start, end, name folded as DuckDB folds it)`` of each placeholder in ``text``."""
    tables_read: frozenset[str]
    """Names, folded as DuckDB folds them, of the tables read other than the statement's own
    ``WITH`` queries."""


def _tables_read(tree: object) -> frozenset[str]:
    """Walk DuckDB's parse tree for the tables read, and refuse what no table name makes safe.

    A name is not a read where a ``WITH`` query of that name is in scope, and scope follows
    DuckDB: the body of a query node sees all of the node's ``WITH`` queries, each of those sees
    only the ones before it, and the recursive side of a recursive one also sees itself. Anywhere
    else the same name is looked up among DuckDB's relations, so ``WITH t AS (SELECT * FROM t)``
    reads a table, and so does the first, non-recursive side of a ``WITH RECURSIVE t``.

    Raises:
        QueryError: If the statement names a schema or catalog, uses ``SHOW``, calls a table
            function outside ``_TABLE_FUNCTIONS`` or a function in ``_REFUSED_FUNCTIONS``, or
            defines a ``WITH`` query under a private name.
    """
    read: set[str] = set()
    # An explicit stack: statements nest deeper than Python's recursion limit allows.
    pending: list[tuple[object, frozenset[str]]] = [(tree, frozenset())]
    while pending:
        node, visible = pending.pop()
        if isinstance(node, list):
            pending.extend((child, visible) for child in cast("list[object]", node))
            continue
        if not isinstance(node, dict):
            continue
        fields = cast("dict[str, object]", node)
        kind = fields.get("type")
        queries = fields.get("cte_map")
        if isinstance(queries, dict):
            entries = cast("dict[str, object]", queries).get("map") or []
            for entry in cast("list[dict[str, object]]", entries):
                name = str(entry.get("key"))
                if _folded(name).startswith(PRIVATE_NAME_PREFIX):
                    raise QueryError(f"WITH query name {name!r} is reserved for fxtr")
                pending.append((entry.get("value"), visible))
                visible = visible | {_folded(name)}
        if kind == "RECURSIVE_CTE_NODE":
            recursive_side = visible | {_folded(str(fields.get("cte_name")))}
            pending.extend(
                (child, recursive_side if key == "right" else visible)
                for key, child in fields.items()
                if key != "cte_map"
            )
            continue
        if kind == "BASE_TABLE":
            name = str(fields.get("table_name"))
            if fields.get("schema_name") or fields.get("catalog_name"):
                raise QueryError(
                    f"Array SQL reads {name!r} through a schema or catalog; "
                    "name a table passed to the query, with no qualifier"
                )
            if _folded(name) not in visible:
                read.add(_folded(name))
        elif kind == "TABLE_FUNCTION":
            function = cast("dict[str, object]", fields.get("function") or {})
            name = str(function.get("function_name"))
            if _folded(name) not in _TABLE_FUNCTIONS:
                raise QueryError(
                    f"Array SQL cannot call the table function {name}(); "
                    f"only {', '.join(sorted(_TABLE_FUNCTIONS))} are available"
                )
        elif kind == "FUNCTION" and _folded(str(fields.get("function_name"))) in _REFUSED_FUNCTIONS:
            name = str(fields.get("function_name"))
            raise QueryError(f"Array SQL cannot call {name}(); {_REFUSED_FUNCTIONS[_folded(name)]}")
        elif kind == "SHOW_REF" and fields.get("table_name"):
            raise QueryError("Array SQL cannot use SHOW; it lists relations other than its tables")
        pending.extend((child, visible) for key, child in fields.items() if key != "cte_map")
    return frozenset(read)


_MAX_CACHED_SQL_CHARACTERS = 64 * 1024
"""The longest statement whose parse is kept. The cache holds each text it has seen, and a worker
chooses the text, so without this 256 entries could pin gigabytes on a host."""


def _require_unicode(text: str, what: str) -> None:
    """Refuse ``text`` holding a lone surrogate, which DuckDB cannot take as a string."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise QueryError(f"{what} is not valid Unicode text: {exc.reason}") from None


def _parse_sql(text: str) -> _ParsedSQL:
    _require_unicode(text, "Array SQL")
    if len(text) > _MAX_CACHED_SQL_CHARACTERS:
        return _parse_sql_uncached(text)
    return _parse_sql_cached(text)


def _parse_sql_uncached(text: str) -> _ParsedSQL:
    # Imported here so that defining a step, which imports this module for its types, does
    # not load DuckDB into a process that never writes SQL.
    import duckdb

    # Use a private parser connection so SQL parsing never shares DuckDB's module-global
    # connection with application code or other threads. Cached entries contain no values.
    try:
        with duckdb.connect(
            ":memory:", config={"threads": 1, "enable_external_access": False}
        ) as c:
            statements = c.extract_statements(text)
            if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
                raise QueryError(
                    "Array SQL must contain exactly one read-only SELECT or WITH query"
                )
            serialized = c.execute("SELECT json_serialize_sql(?)", [text]).fetchone()
    except duckdb.Error as exc:
        raise QueryError(f"Invalid array SQL: {exc}") from exc
    assert serialized is not None
    tree = cast("dict[str, object]", json.loads(cast("str", serialized[0])))
    if tree.get("error"):
        raise QueryError(f"Invalid array SQL: {tree.get('error_message')}")
    tables_read = _tables_read(tree)
    raw_tokens = cast("list[tuple[int, duckdb.token_type]]", duckdb.tokenize(text))
    byte_positions: dict[int, int] = {}
    byte_offset = 0
    for index, character in enumerate(text):
        byte_positions[byte_offset] = index
        byte_offset += len(character.encode("utf-8"))
    # DuckDB reports UTF-8 byte offsets; Python string slicing uses Unicode code points.
    tokens = [(byte_positions[position], kind) for position, kind in raw_tokens]
    if tokens and text[tokens[-1][0]] == ";":
        text = text[: tokens[-1][0]] + text[tokens[-1][0] + 1 :]
        tokens.pop()
    placeholders: list[tuple[int, int, str]] = []
    positional = 0
    for start, kind in tokens:
        if kind != duckdb.token_type.operator or text[start] not in ("$", "?"):
            continue
        end = start + 1
        while end < len(text) and (
            text[end].isalnum() or text[end] == "_" or ord(text[end]) >= 128
        ):
            end += 1
        name = text[start + 1 : end]
        if not name:
            positional += 1
            name = str(positional)
        elif name.isdecimal():
            positional = max(positional, int(name))
        placeholders.append((start, end, _folded(name)))
    expected = {_folded(name) for name in statements[0].named_parameters}
    if {name for _, _, name in placeholders} != expected:
        raise QueryError("Cannot bind this SQL parameter syntax; use ?, $1, or $name")
    return _ParsedSQL(text, tuple(placeholders), tables_read)


_parse_sql_cached = lru_cache(maxsize=256)(_parse_sql_uncached)


def validate_sql_reads(text: str, tables: Collection[str]) -> None:
    """Require that ``text`` reads nothing but ``tables`` and its own ``WITH`` queries.

    Raises:
        QueryError: If ``text`` is not one read-only ``SELECT``, reads a table that is not in
            ``tables``, or uses a construct :func:`_tables_read` refuses.
    """
    allowed = {_folded(name) for name in tables}
    unknown = sorted(_parse_sql(text).tables_read - allowed)
    if unknown:
        raise QueryError(
            f"Array SQL reads {', '.join(map(repr, unknown))}, which is not one of its tables "
            f"({', '.join(map(repr, sorted(tables)))}) or a WITH query it defines"
        )


def bind_sql(
    text: str, parameters: Mapping[str, FrozenValue], offset: int = 0
) -> tuple[str, tuple[FrozenValue, ...]]:
    """Remap parsed placeholders to numbered bindings in a composed query.

    DuckDB's tokenizer excludes comments and distinguishes strings/quoted identifiers, so
    placeholder-looking text in literals is never changed.
    """
    if not isinstance(text, str) or not text.strip():
        raise QueryError("SQL query must be a non-empty string")
    parsed = _parse_sql(text)
    normalized, placeholders = parsed.text, parsed.placeholders
    expected = {name for _, _, name in placeholders}
    actual = set(parameters)
    if expected != actual:
        raise QueryError(
            f"SQL parameters do not match placeholders; missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )
    pieces: list[str] = []
    values: list[FrozenValue] = []
    indices: dict[str, int] = {}
    previous = 0
    for start, end, name in placeholders:
        if name not in indices:
            values.append(parameters[name])
            indices[name] = offset + len(values)
        pieces.extend((normalized[previous:start], f"${indices[name]}"))
        previous = end
    pieces.append(normalized[previous:])
    return "".join(pieces), tuple(values)
