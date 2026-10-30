"""SQL expression parsing, validation, and rewriting for namespace indexing.

Provides safe parsing of user-supplied SQL condition strings, rewriting
$-prefixed variable references to _VAR_ column names, and validating
that only allowed functions and columns are used.
"""

from __future__ import annotations

import functools

import sqlglot
from sqlglot import exp

from fxtr2.data.store_indexing.indexing_types import IndexerColumnType

_DIALECT = "sqlite"

# Statement types that are never allowed in a conditional expression.
_STATEMENT_TYPES = (
    exp.Select,
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.Command,
    exp.Union,
    exp.Except,
    exp.Intersect,
)

COLUMN_TYPE_TO_SQLITE: dict[IndexerColumnType, str] = {
    IndexerColumnType.INT: "INTEGER",
    IndexerColumnType.STR: "TEXT",
    IndexerColumnType.BOOL: "INTEGER",
    IndexerColumnType.BYTES: "BLOB",
    IndexerColumnType.FLOAT: "REAL",
    IndexerColumnType.FXTR_ID: "BLOB",
}

# Safe SQLite function allowlist. Combines sqlglot typed class names (matched via
# type(node).__name__.lower()) and anonymous function names (matched via node.name.lower())
# into a single frozenset. Connectors (AND/OR/NOT) are always allowed separately.
#
# Excluded functions (unsafe):
#   load_extension, changes, last_insert_rowid, total_changes, randomblob,
#   sqlite_compileoption_get, sqlite_compileoption_used, sqlite_version,
#   sqlite_source_id, sqlite_offset
SAFE_FUNCTIONS: frozenset[str] = frozenset(
    {
        # --- Typed class names: core scalar ---
        "abs",
        "cast",
        "chr",
        "coalesce",
        "concat",
        "concatws",
        "format",
        "hex",
        "case",
        "if",
        "length",
        "lower",
        "max",
        "min",
        "nullif",
        "rand",
        "replace",
        "round",
        "sign",
        "soundex",
        "strposition",
        "substring",
        "trim",
        "typeof",
        "unhex",
        "unicode",
        "upper",
        # --- Typed class names: math ---
        "acos",
        "acosh",
        "asin",
        "asinh",
        "atan",
        "atan2",
        "atanh",
        "ceil",
        "cos",
        "cosh",
        "degrees",
        "exp",
        "floor",
        "ln",
        "log",
        "pi",
        "pow",
        "radians",
        "sin",
        "sinh",
        "sqrt",
        "tan",
        "tanh",
        # --- Typed class names: date/time ---
        "date",
        "timetostr",
        # --- Typed class names: JSON (read-only) ---
        "jsonextract",
        "jsonobject",
        "jsonremove",
        "jsonset",
        "jsontype",
        # --- Typed class names: aggregates ---
        "avg",
        "count",
        "groupconcat",
        "sum",
        # --- Anonymous function names: core scalar ---
        "glob",
        "like",
        "likelihood",
        "likely",
        "unlikely",
        "octet_length",
        "printf",
        "quote",
        "zeroblob",
        # --- Anonymous function names: date/time ---
        "datetime",
        "time",
        "julianday",
        "unixepoch",
        "timediff",
        # --- Anonymous function names: math ---
        "trunc",
        # --- Anonymous function names: JSON (read-only) ---
        "json",
        "json_array",
        "json_array_length",
        "json_error_position",
        "json_insert",
        "json_patch",
        "json_pretty",
        "json_quote",
        "json_replace",
        "json_valid",
        # --- Anonymous function names: aggregates ---
        "total",
        "median",
        "percentile",
        "percentile_cont",
        "percentile_disc",
    }
)


@functools.lru_cache(maxsize=1000)
def parse_condition(
    condition: str,
    allowed_variables: frozenset[str],
    *,
    allow_value: bool = True,
) -> str:
    """Parse, validate, and rewrite a SQL condition expression.

    Steps:
    1. Parse with sqlglot (SQLite dialect)
    2. Reject multiple statements
    3. Reject statement types (SELECT, INSERT, ...) and subqueries in AST
    4. Validate function calls against safe function allowlist
    5. Collect all column references; validate:
       - $-prefixed columns: strip $, check name is in allowed_variables
       - bare ``value``: only allowed if allow_value=True
       - no other bare column names
    6. Rewrite AST: $foo -> _VAR_foo (Column nodes)
    7. Render back to SQL string and return

    Args:
        condition: User-provided SQL condition, e.g. ``"$t > 3 AND value = 'foo'"``
        allowed_variables: Variable names without $ prefix, e.g. ``frozenset({"t", "m"})``
        allow_value: If True, bare ``value`` column is allowed. Set to False for
            BindingConstraint conditions which have no value column.

    Returns:
        Rewritten SQL string, e.g. ``"_VAR_t > 3 AND value = 'foo'"``

    Raises:
        ValueError: On any validation failure (descriptive message).
    """
    # Step 1: Parse
    try:
        results = sqlglot.parse(condition, dialect=_DIALECT)
    except Exception as e:
        raise ValueError(f"Failed to parse SQL expression: {e}") from e

    # Step 2: Reject multiple statements or empty input
    if len(results) != 1:
        raise ValueError(f"Expected a single expression, got {len(results)} statements")
    if results[0] is None:
        raise ValueError("Empty expression")

    tree = results[0]

    # Step 3 & 4: Walk the AST — reject statements/subqueries, validate functions
    if isinstance(tree, _STATEMENT_TYPES):
        raise ValueError(f"Expression is a SQL statement ({type(tree).__name__}), not a condition")

    for node in tree.walk():
        if isinstance(node, _STATEMENT_TYPES):
            raise ValueError(f"Expression contains a nested SQL statement ({type(node).__name__})")
        if isinstance(node, exp.Subquery):
            raise ValueError("Expression contains a subquery")

        # Validate function calls (skip connectors like AND/OR which are Func subclasses)
        if isinstance(node, exp.Func) and not isinstance(node, exp.Connector):
            if isinstance(node, exp.Anonymous):
                func_name = str(node.this).lower()
            else:
                func_name = type(node).__name__.lower()
            if func_name not in SAFE_FUNCTIONS:
                raise ValueError(f"Disallowed function: {func_name}")

    # Step 5: Validate column references
    for col in tree.find_all(exp.Column):
        if col.table:
            raise ValueError(
                f"Table-qualified column references are not allowed: {col.sql(dialect=_DIALECT)}"
            )

        name = col.name
        if name.startswith("$"):
            var_name = name[1:]
            if var_name not in allowed_variables:
                raise ValueError(f"Unknown variable: ${var_name}")
        elif name == "value":
            if not allow_value:
                raise ValueError(
                    "'value' column is not allowed in binding constraints"
                    " (only $-prefixed variables)"
                )
        else:
            raise ValueError(
                f"Unknown column reference: {name!r}. Use $-prefixed variable names or 'value'"
            )

    # Step 6: Rewrite $foo -> _VAR_foo
    def _rewrite_vars(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column) and not node.table and node.name.startswith("$"):
            new_name = "_VAR_" + node.name[1:]
            return exp.Column(this=exp.to_identifier(new_name))
        return node

    rewritten = tree.transform(_rewrite_vars)

    # Step 7: Render back to SQL
    return rewritten.sql(dialect=_DIALECT)


def validate_user_query(query: str) -> None:
    """Validate that a user-provided SQL query is syntactically well-formed.

    This is a lightweight check used by ``FxtrQueryableNamespace.sql()`` to catch
    obvious syntax errors before running the query against SQLite. It does NOT
    validate column references, table names, or semantic correctness — those errors
    will be caught by SQLite at execution time.

    Args:
        query: The user's SQL query string.

    Raises:
        ValueError: If the query cannot be parsed as valid SQL.
    """
    try:
        results = sqlglot.parse(query, dialect=_DIALECT)
    except Exception as e:
        raise ValueError(f"Failed to parse SQL query: {e}") from e

    if not results or results[0] is None:
        raise ValueError("Empty SQL query")
