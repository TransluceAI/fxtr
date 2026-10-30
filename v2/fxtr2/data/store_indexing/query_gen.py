"""SQL query generation for namespace index queries.

Assembles validated, rewritten constraint conditions (from ``sql_expr.parse_condition``)
into full SQL queries that join index tables, unify shared variables, and apply optional
binding constraints.

Two query generators live here:

- ``generate_iter_matches_query``: Used by ``iter_matches`` to produce a flat query
  that returns ``(entity_internal_id, pin_key, var_0, var_1, ...)`` rows from a single
  set of constraints joined on shared variables.

- ``generate_direct_sql_query``: Used by ``FxtrQueryableNamespace.sql()`` to produce a
  ``WITH <cte1>, <cte2>, ... <user_query>`` statement where each CTE is a named view
  composed of joined index tables. Structurally similar to the iter_matches query but
  with table-name qualification (for ATTACH), value column renaming, and an entity
  LEFT JOIN.
"""

from __future__ import annotations

import dataclasses

from fxtr2.data.store_indexing.indexing_types import IndexerColumnType


@dataclasses.dataclass(frozen=True)
class ResolvedConstraint:
    """An IndexerConstraint resolved to its SQL table info.

    Created by the backend when it looks up the indexer's registration and table name.
    The ``condition`` field holds the rewritten SQL from ``parse_condition`` (with ``_VAR_``
    prefixed variable names and bare ``value``).
    """

    table_name: str  # e.g., "NS_INDEX__42"
    key_variables: tuple[str, ...]  # e.g., ("t", "m") — without $ prefix
    key_column_types: tuple[IndexerColumnType, ...]
    value_column_type: IndexerColumnType
    condition: str  # rewritten SQL from parse_condition


@dataclasses.dataclass(frozen=True)
class GeneratedQuery:
    """Result of query generation.

    Result row layout: ``(entity_internal_id, pin_key, var_0, var_1, ...)``.
    """

    sql: str
    parameters: list[int | str]
    binding_variable_names: tuple[str, ...]  # ordered unique variable names in SELECT


def generate_iter_matches_query(
    constraints: list[ResolvedConstraint],
    binding_constraint: str | None,
    namespace_internal_id: int,
    key_prefix: str,
) -> GeneratedQuery:
    """Generate a SQL query to find matching entities across index tables.

    Used by ``iter_matches`` on the backend. For the related ``sql()`` API that
    exposes named views as CTEs for ad-hoc queries, see ``generate_direct_sql_query``.

    Args:
        constraints: Resolved constraints with table info and rewritten conditions.
            Must have at least one constraint.
        binding_constraint: Optional global condition (rewritten SQL from
            ``parse_condition`` with ``allow_value=False``) applied to the joined result.
            Uses ``_VAR_`` names directly.
        namespace_internal_id: The internal SQLite ID of the namespace.
        key_prefix: Key prefix filter. Empty string means no prefix filtering.

    Returns:
        GeneratedQuery with SQL string, parameters, and variable name ordering.
    """
    if not constraints:
        raise ValueError("At least one constraint is required")

    # Step 1: Assign table aliases
    aliases = [f"ix{i}" for i in range(len(constraints))]

    # Step 2: Build variable registry
    # var_canonical: var_name -> alias of the first constraint that defines it
    var_canonical: dict[str, str] = {}
    all_vars_ordered: list[str] = []  # unique variables in first-appearance order

    for i, c in enumerate(constraints):
        for var in c.key_variables:
            if var not in var_canonical:
                var_canonical[var] = aliases[i]
                all_vars_ordered.append(var)

    # Step 3: Build per-constraint subqueries
    # Each subquery has an inner SELECT that aliases IXKEY columns to _VAR_ names
    # and an outer SELECT that applies the condition using those aliases.
    def _build_subquery(constraint: ResolvedConstraint) -> str:
        inner_cols = ["namespace_id", "entity_internal_id"]
        for j, var in enumerate(constraint.key_variables):
            inner_cols.append(f"IXKEY_{j} AS _VAR_{var}")
        inner_cols.append("VALUE AS value")
        inner_select = ", ".join(inner_cols)
        inner = f"SELECT {inner_select} FROM {constraint.table_name}"
        return f"(SELECT * FROM ({inner}) WHERE {constraint.condition})"

    # Step 4: Build JOINs and collect parameters
    parameters: list[int | str] = []
    join_clauses: list[str] = []

    for i, (c, alias) in enumerate(zip(constraints, aliases)):
        subquery = _build_subquery(c)

        on_parts = [
            f"{alias}.entity_internal_id = p.entity_id",
            f"{alias}.namespace_id = ?",
        ]
        parameters.append(namespace_internal_id)

        # Unification: for shared variables defined by an earlier constraint
        if i > 0:
            for var in c.key_variables:
                canonical = var_canonical[var]
                if canonical != alias:
                    on_parts.append(f"{alias}._VAR_{var} = {canonical}._VAR_{var}")

        on_clause = " AND ".join(on_parts)
        join_clauses.append(f"JOIN {subquery} {alias}\n    ON {on_clause}")

    # Step 5: Build middle WHERE
    where_parts = ["p.namespace_id = ?"]
    parameters.append(namespace_internal_id)

    if key_prefix:
        where_parts.append("p.name LIKE ? || '%'")
        parameters.append(key_prefix)

    where_clause = " AND ".join(where_parts)

    # Step 6: Build middle SELECT
    select_cols = ["p.entity_id", "p.name AS pin_key"]
    for var in all_vars_ordered:
        select_cols.append(f"{var_canonical[var]}._VAR_{var}")

    select_clause = ", ".join(select_cols)

    # Assemble middle query
    joins_str = "\n".join(join_clauses)
    middle_query = (
        f"SELECT {select_clause}\n"
        f"FROM namespace_pin p\n"
        f"{joins_str}\n"
        f"WHERE {where_clause}"
    )

    # Step 7: Wrap with binding constraint if present
    if binding_constraint is not None:
        outer_cols = ["entity_id", "pin_key"]
        for var in all_vars_ordered:
            outer_cols.append(f"_VAR_{var}")
        outer_select = ", ".join(outer_cols)
        sql = f"SELECT {outer_select}\n" f"FROM ({middle_query})\n" f"WHERE {binding_constraint}"
    else:
        sql = middle_query

    return GeneratedQuery(
        sql=sql,
        parameters=parameters,
        binding_variable_names=tuple(all_vars_ordered),
    )


# =============================================================================
# Direct SQL query generation (for FxtrQueryableNamespace.sql())
# =============================================================================


@dataclasses.dataclass(frozen=True)
class ResolvedViewColumn:
    """A single named column within a view, resolved to its SQL table info.

    Similar to ``ResolvedConstraint`` but carries the user-chosen column name
    for value-column renaming in the generated CTE.
    """

    column_name: str  # user-chosen name (the kwarg key in QueryView)
    table_name: str  # e.g., "NS_INDEX__42" (unqualified — db alias added during generation)
    key_variables: tuple[str, ...]  # e.g., ("t", "m")
    key_column_types: tuple[IndexerColumnType, ...]
    value_column_type: IndexerColumnType
    condition: str  # rewritten SQL from parse_condition


@dataclasses.dataclass(frozen=True)
class ResolvedView:
    """A ``QueryView`` resolved to SQL table info, ready for CTE generation."""

    view_name: str
    prefix: str
    columns: list[ResolvedViewColumn]


@dataclasses.dataclass(frozen=True)
class GeneratedDirectQuery:
    """Result of direct SQL query generation.

    Contains a ``WITH <cte1>, <cte2>, ... <user_query>`` SQL string and the
    bound parameters for all CTEs.
    """

    sql: str
    parameters: list[int | str]


def _generate_view_cte(
    view: ResolvedView,
    namespace_internal_id: int,
    db_alias: str,
) -> tuple[str, list[int | str]]:
    """Generate a single CTE definition for a resolved view.

    Structurally similar to ``generate_iter_matches_query`` — both join index
    tables via ``namespace_pin`` using variable unification (equijoins on shared
    ``_VAR_`` columns). Key differences:

    - Table names are qualified with *db_alias* (for ATTACH).
    - Each constraint's VALUE is projected as its user-chosen column name.
    - ``_VAR_`` columns are projected as bare variable names.
    - A LEFT JOIN on the ``entity`` table exposes ``entity_id`` (the ``public_id``
      BLOB), structured to admit SQLite's "Omit OUTER JOIN Optimization" when the
      user's query doesn't reference ``entity_id``.

    Returns:
        ``(cte_sql, parameters)`` where *cte_sql* is ``"view_name AS (SELECT ...)"``
        and *parameters* are the bind values for this CTE.
    """
    if not view.columns:
        raise ValueError("View must have at least one column")

    constraints = view.columns
    aliases = [f"ix{i}" for i in range(len(constraints))]

    # Build variable registry (same logic as generate_iter_matches_query)
    var_canonical: dict[str, str] = {}
    all_vars_ordered: list[str] = []

    for i, c in enumerate(constraints):
        for var in c.key_variables:
            if var not in var_canonical:
                var_canonical[var] = aliases[i]
                all_vars_ordered.append(var)

    # Build per-constraint subqueries
    # Inner SELECT: alias IXKEY columns to _VAR_ names, keep VALUE as value
    # Outer SELECT: apply the per-constraint condition
    def _build_subquery(col: ResolvedViewColumn) -> str:
        inner_cols = ["namespace_id", "entity_internal_id"]
        for j, var in enumerate(col.key_variables):
            inner_cols.append(f"IXKEY_{j} AS _VAR_{var}")
        inner_cols.append("VALUE AS value")
        inner_select = ", ".join(inner_cols)
        inner = f"SELECT {inner_select} FROM {db_alias}.{col.table_name}"
        return f"(SELECT * FROM ({inner}) WHERE {col.condition})"

    # Build JOINs
    parameters: list[int | str] = []
    join_clauses: list[str] = []

    for i, (c, alias) in enumerate(zip(constraints, aliases)):
        subquery = _build_subquery(c)

        on_parts = [
            f"{alias}.entity_internal_id = p.entity_id",
            f"{alias}.namespace_id = ?",
        ]
        parameters.append(namespace_internal_id)

        # Unification: shared variables defined by an earlier constraint
        if i > 0:
            for var in c.key_variables:
                canonical = var_canonical[var]
                if canonical != alias:
                    on_parts.append(f"{alias}._VAR_{var} = {canonical}._VAR_{var}")

        on_clause = " AND ".join(on_parts)
        join_clauses.append(f"JOIN {subquery} {alias}\n      ON {on_clause}")

    # LEFT JOIN on entity table for entity_id (public_id BLOB)
    # Structured to admit SQLite's Omit OUTER JOIN Optimization:
    # if the user's query doesn't reference entity_id, SQLite can skip this join.
    join_clauses.append(f"LEFT JOIN {db_alias}.entity e ON e.id = p.entity_id")

    # WHERE clause: namespace filter + prefix filter
    where_parts = [f"p.namespace_id = ?"]
    parameters.append(namespace_internal_id)

    if view.prefix:
        where_parts.append("p.name LIKE ? || '%'")
        parameters.append(view.prefix)

    where_clause = " AND ".join(where_parts)

    # Build outer SELECT with user-friendly column names
    select_cols: list[str] = []
    # Binding variables (bare names)
    for var in all_vars_ordered:
        select_cols.append(f"{var_canonical[var]}._VAR_{var} AS {var}")
    # Value columns (named after the kwarg)
    for c, alias in zip(constraints, aliases):
        select_cols.append(f"{alias}.value AS {c.column_name}")
    # Reserved columns
    select_cols.append("p.name AS pin_name")
    select_cols.append("e.public_id AS entity_id")

    select_clause = ",\n      ".join(select_cols)
    joins_str = "\n    ".join(join_clauses)

    cte_body = (
        f"SELECT\n      {select_clause}\n"
        f"    FROM {db_alias}.namespace_pin p\n"
        f"    {joins_str}\n"
        f"    WHERE {where_clause}"
    )

    cte_sql = f"{view.view_name} AS (\n  {cte_body}\n  )"

    return cte_sql, parameters


def generate_direct_sql_query(
    views: list[ResolvedView],
    user_query: str,
    namespace_internal_id: int,
    db_alias: str = "_fxtr_internal_store",
) -> GeneratedDirectQuery:
    """Generate a ``WITH ... <user_query>`` SQL statement from resolved views.

    Each view becomes a CTE that joins its index tables on shared variables
    (same unification logic as ``generate_iter_matches_query``), with
    user-friendly column names and a LEFT JOIN on the entity table for
    ``entity_id``.

    See ``_generate_view_cte`` for the per-view CTE structure.

    Args:
        views: Resolved views with table info and rewritten conditions.
            Must have at least one view.
        user_query: The user's SQL query, substituted verbatim after the WITH clause.
        namespace_internal_id: The internal SQLite ID of the namespace.
        db_alias: The ATTACH alias for the namespace store database.

    Returns:
        ``GeneratedDirectQuery`` with the full SQL and all bound parameters.
    """
    if not views:
        raise ValueError("At least one view is required")

    all_ctes: list[str] = []
    all_parameters: list[int | str] = []

    for view in views:
        cte_sql, params = _generate_view_cte(view, namespace_internal_id, db_alias)
        all_ctes.append(cte_sql)
        all_parameters.extend(params)

    ctes_str = ",\n".join(all_ctes)
    sql = f"WITH\n{ctes_str}\n{user_query}"

    return GeneratedDirectQuery(sql=sql, parameters=all_parameters)
