"""Tests for SQL query generation from resolved constraints."""

import sqlite3

import pytest
import sqlglot

from fxtr2.data.store_indexing.indexing_types import IndexerColumnType
from fxtr2.data.store_indexing.query_gen import (
    GeneratedDirectQuery,
    ResolvedConstraint,
    ResolvedView,
    ResolvedViewColumn,
    generate_direct_sql_query,
    generate_iter_matches_query,
)


def _rc(
    table: str,
    key_vars: tuple[str, ...],
    condition: str,
    key_types: tuple[IndexerColumnType, ...] | None = None,
    value_type: IndexerColumnType = IndexerColumnType.FLOAT,
) -> ResolvedConstraint:
    """Shorthand for creating a ResolvedConstraint."""
    if key_types is None:
        key_types = tuple(IndexerColumnType.INT for _ in key_vars)
    return ResolvedConstraint(
        table_name=table,
        key_variables=key_vars,
        key_column_types=key_types,
        value_column_type=value_type,
        condition=condition,
    )


# ---------------------------------------------------------------------------
# Structural tests: verify generated SQL shape and parameter order
# ---------------------------------------------------------------------------


class TestSingleConstraintNoPrefix:
    def test_basic_structure(self):
        q = generate_iter_matches_query(
            constraints=[_rc("NS_INDEX__42", ("t",), "_VAR_t > 3")],
            binding_constraint=None,
            namespace_internal_id=7,
            key_prefix="",
        )
        # Should have one JOIN, no outer wrapper, no LIKE
        assert "JOIN" in q.sql
        assert "NS_INDEX__42" in q.sql
        assert "_VAR_t > 3" in q.sql
        assert "LIKE" not in q.sql
        assert q.binding_variable_names == ("t",)

    def test_no_outer_wrapper_without_binding(self):
        q = generate_iter_matches_query(
            constraints=[_rc("NS_INDEX__42", ("t",), "TRUE")],
            binding_constraint=None,
            namespace_internal_id=1,
            key_prefix="",
        )
        # Without binding constraint, the query should be a single SELECT, not nested
        # Count SELECT keywords — should be the main one + subquery inner + subquery outer
        assert q.sql.count("SELECT") == 3  # main + inner + outer of subquery

    def test_parameters(self):
        q = generate_iter_matches_query(
            constraints=[_rc("NS_INDEX__42", ("t",), "TRUE")],
            binding_constraint=None,
            namespace_internal_id=7,
            key_prefix="",
        )
        # namespace_id once in JOIN ON, once in WHERE
        assert q.parameters == [7, 7]


class TestSingleConstraintWithPrefix:
    def test_like_clause_present(self):
        q = generate_iter_matches_query(
            constraints=[_rc("NS_INDEX__42", ("t",), "TRUE")],
            binding_constraint=None,
            namespace_internal_id=7,
            key_prefix="turns/",
        )
        assert "LIKE" in q.sql
        assert "turns/" in q.parameters

    def test_parameters_with_prefix(self):
        q = generate_iter_matches_query(
            constraints=[_rc("NS_INDEX__42", ("t",), "TRUE")],
            binding_constraint=None,
            namespace_internal_id=7,
            key_prefix="turns/",
        )
        # namespace_id in JOIN ON + namespace_id in WHERE + key_prefix
        assert q.parameters == [7, 7, "turns/"]


class TestTwoConstraintsSharedVariable:
    def test_equijoin_for_shared_variable(self):
        q = generate_iter_matches_query(
            constraints=[
                _rc("NS_INDEX__42", ("t", "m"), "_VAR_t > 3"),
                _rc("NS_INDEX__55", ("t", "n"), "TRUE"),
            ],
            binding_constraint=None,
            namespace_internal_id=7,
            key_prefix="",
        )
        # Should have unification condition for shared variable t
        assert "ix1._VAR_t = ix0._VAR_t" in q.sql
        # Variable ordering: t first (from C0), then m (from C0), then n (from C1)
        assert q.binding_variable_names == ("t", "m", "n")

    def test_parameters_two_constraints(self):
        q = generate_iter_matches_query(
            constraints=[
                _rc("NS_INDEX__42", ("t",), "TRUE"),
                _rc("NS_INDEX__55", ("t",), "TRUE"),
            ],
            binding_constraint=None,
            namespace_internal_id=7,
            key_prefix="",
        )
        # Two JOIN ONs + one WHERE = three namespace_id params
        assert q.parameters == [7, 7, 7]


class TestTwoConstraintsNoSharedVariables:
    def test_no_equijoin(self):
        q = generate_iter_matches_query(
            constraints=[
                _rc("NS_INDEX__42", ("a",), "TRUE"),
                _rc("NS_INDEX__55", ("b",), "TRUE"),
            ],
            binding_constraint=None,
            namespace_internal_id=7,
            key_prefix="",
        )
        # No unification conditions expected
        assert "ix1._VAR_a = ix0._VAR_a" not in q.sql
        assert "ix1._VAR_b = ix0._VAR_b" not in q.sql
        assert q.binding_variable_names == ("a", "b")


class TestThreeConstraintsMultipleSharedVars:
    def test_multi_way_unification(self):
        q = generate_iter_matches_query(
            constraints=[
                _rc("T1", ("x", "y"), "TRUE"),
                _rc("T2", ("x", "z"), "TRUE"),
                _rc("T3", ("y", "z"), "TRUE"),
            ],
            binding_constraint=None,
            namespace_internal_id=1,
            key_prefix="",
        )
        # x shared between C0 and C1
        assert "ix1._VAR_x = ix0._VAR_x" in q.sql
        # y shared between C0 and C2
        assert "ix2._VAR_y = ix0._VAR_y" in q.sql
        # z shared between C1 and C2 (canonical for z is ix1)
        assert "ix2._VAR_z = ix1._VAR_z" in q.sql
        assert q.binding_variable_names == ("x", "y", "z")


class TestBindingConstraint:
    def test_outer_wrapper_present(self):
        q = generate_iter_matches_query(
            constraints=[
                _rc("NS_INDEX__42", ("t", "m"), "TRUE"),
                _rc("NS_INDEX__55", ("t", "n"), "TRUE"),
            ],
            binding_constraint="_VAR_t + _VAR_n < 10",
            namespace_internal_id=7,
            key_prefix="",
        )
        # Should have an outer SELECT with the binding constraint
        assert q.sql.startswith("SELECT entity_id, pin_key")
        assert "_VAR_t + _VAR_n < 10" in q.sql
        # The binding constraint should be at the outermost level
        last_where_idx = q.sql.rfind("WHERE")
        assert "_VAR_t + _VAR_n < 10" in q.sql[last_where_idx:]

    def test_without_binding_no_wrapper(self):
        q = generate_iter_matches_query(
            constraints=[_rc("NS_INDEX__42", ("t",), "TRUE")],
            binding_constraint=None,
            namespace_internal_id=7,
            key_prefix="",
        )
        # Should NOT start with "SELECT entity_id, pin_key" (the outer wrapper form)
        # Instead starts with the middle query directly
        assert q.sql.startswith("SELECT p.entity_id")


class TestVariableOutputOrdering:
    def test_ordering_follows_first_appearance(self):
        q = generate_iter_matches_query(
            constraints=[
                _rc("T1", ("z", "a"), "TRUE"),
                _rc("T2", ("a", "b"), "TRUE"),
            ],
            binding_constraint=None,
            namespace_internal_id=1,
            key_prefix="",
        )
        # z appears first (C0), a second (C0), b third (C1)
        assert q.binding_variable_names == ("z", "a", "b")

    def test_canonical_source_in_select(self):
        q = generate_iter_matches_query(
            constraints=[
                _rc("T1", ("x",), "TRUE"),
                _rc("T2", ("x", "y"), "TRUE"),
            ],
            binding_constraint=None,
            namespace_internal_id=1,
            key_prefix="",
        )
        # x is canonical from ix0, y from ix1
        assert "ix0._VAR_x" in q.sql
        assert "ix1._VAR_y" in q.sql


class TestEmptyConstraints:
    def test_rejects_empty_list(self):
        with pytest.raises(ValueError, match="At least one constraint"):
            generate_iter_matches_query(
                constraints=[],
                binding_constraint=None,
                namespace_internal_id=1,
                key_prefix="",
            )


def _canonicalize(sql: str) -> str:
    """Parse SQL with sqlglot and render to canonical form."""
    return sqlglot.parse_one(sql, dialect="sqlite").sql(dialect="sqlite")


class TestASTStructure:
    """Compare generated SQL against expected SQL via sqlglot AST canonicalization."""

    def test_single_constraint_no_prefix_no_binding(self):
        q = generate_iter_matches_query(
            constraints=[_rc("NS_INDEX__42", ("t",), "_VAR_t > 3")],
            binding_constraint=None,
            namespace_internal_id=7,
            key_prefix="",
        )
        expected = """
            SELECT p.entity_id, p.name AS pin_key, ix0._VAR_t
            FROM namespace_pin p
            JOIN (
                SELECT * FROM (
                    SELECT namespace_id, entity_internal_id,
                           IXKEY_0 AS _VAR_t, VALUE AS value
                    FROM NS_INDEX__42
                ) WHERE _VAR_t > 3
            ) ix0
                ON ix0.entity_internal_id = p.entity_id
                AND ix0.namespace_id = 7
            WHERE p.namespace_id = 7
        """
        sql_with_params = q.sql
        for param in q.parameters:
            sql_with_params = sql_with_params.replace("?", repr(param), 1)
        assert _canonicalize(sql_with_params) == _canonicalize(expected)

    def test_single_constraint_with_prefix(self):
        q = generate_iter_matches_query(
            constraints=[_rc("NS_INDEX__42", ("t",), "TRUE")],
            binding_constraint=None,
            namespace_internal_id=5,
            key_prefix="data/",
        )
        # Parameters are placeholders in generated SQL, so we inline them for comparison
        expected = """
            SELECT p.entity_id, p.name AS pin_key, ix0._VAR_t
            FROM namespace_pin p
            JOIN (
                SELECT * FROM (
                    SELECT namespace_id, entity_internal_id,
                           IXKEY_0 AS _VAR_t, VALUE AS value
                    FROM NS_INDEX__42
                ) WHERE TRUE
            ) ix0
                ON ix0.entity_internal_id = p.entity_id
                AND ix0.namespace_id = 5
            WHERE p.namespace_id = 5
                AND p.name LIKE 'data/' || '%'
        """
        # Substitute parameters into generated SQL for comparison
        sql_with_params = q.sql
        for param in q.parameters:
            sql_with_params = sql_with_params.replace("?", repr(param), 1)
        assert _canonicalize(sql_with_params) == _canonicalize(expected)

    def test_two_constraints_shared_var_with_binding(self):
        """The plan's canonical example: two constraints sharing $t, plus binding."""
        q = generate_iter_matches_query(
            constraints=[
                _rc("NS_INDEX__42", ("t", "m"), "_VAR_t > 3 AND value = 'foo'"),
                _rc("NS_INDEX__55", ("t", "n"), "NOT value IS NULL"),
            ],
            binding_constraint="_VAR_t + _VAR_n < 10",
            namespace_internal_id=7,
            key_prefix="turns/",
        )
        expected = """
            SELECT entity_id, pin_key, _VAR_t, _VAR_m, _VAR_n
            FROM (
                SELECT
                    p.entity_id,
                    p.name AS pin_key,
                    ix0._VAR_t,
                    ix0._VAR_m,
                    ix1._VAR_n
                FROM namespace_pin p
                JOIN (SELECT * FROM (
                    SELECT namespace_id, entity_internal_id,
                           IXKEY_0 AS _VAR_t, IXKEY_1 AS _VAR_m, VALUE AS value
                    FROM NS_INDEX__42
                ) WHERE _VAR_t > 3 AND value = 'foo') ix0
                    ON ix0.entity_internal_id = p.entity_id AND ix0.namespace_id = 7
                JOIN (SELECT * FROM (
                    SELECT namespace_id, entity_internal_id,
                           IXKEY_0 AS _VAR_t, IXKEY_1 AS _VAR_n, VALUE AS value
                    FROM NS_INDEX__55
                ) WHERE NOT value IS NULL) ix1
                    ON ix1.entity_internal_id = p.entity_id AND ix1.namespace_id = 7
                    AND ix1._VAR_t = ix0._VAR_t
                WHERE p.namespace_id = 7
                    AND p.name LIKE 'turns/' || '%'
            ) WHERE _VAR_t + _VAR_n < 10
        """
        sql_with_params = q.sql
        for param in q.parameters:
            sql_with_params = sql_with_params.replace("?", repr(param), 1)
        assert _canonicalize(sql_with_params) == _canonicalize(expected)

    def test_two_constraints_no_shared_vars_no_binding(self):
        q = generate_iter_matches_query(
            constraints=[
                _rc("T1", ("a",), "value > 0"),
                _rc("T2", ("b",), "value < 100"),
            ],
            binding_constraint=None,
            namespace_internal_id=1,
            key_prefix="",
        )
        expected = """
            SELECT p.entity_id, p.name AS pin_key, ix0._VAR_a, ix1._VAR_b
            FROM namespace_pin p
            JOIN (SELECT * FROM (
                SELECT namespace_id, entity_internal_id,
                       IXKEY_0 AS _VAR_a, VALUE AS value
                FROM T1
            ) WHERE value > 0) ix0
                ON ix0.entity_internal_id = p.entity_id AND ix0.namespace_id = 1
            JOIN (SELECT * FROM (
                SELECT namespace_id, entity_internal_id,
                       IXKEY_0 AS _VAR_b, VALUE AS value
                FROM T2
            ) WHERE value < 100) ix1
                ON ix1.entity_internal_id = p.entity_id AND ix1.namespace_id = 1
            WHERE p.namespace_id = 1
        """
        sql_with_params = q.sql
        for param in q.parameters:
            sql_with_params = sql_with_params.replace("?", repr(param), 1)
        assert _canonicalize(sql_with_params) == _canonicalize(expected)

    def test_three_constraints_multi_unification(self):
        q = generate_iter_matches_query(
            constraints=[
                _rc("T1", ("x", "y"), "TRUE"),
                _rc("T2", ("x", "z"), "TRUE"),
                _rc("T3", ("y", "z"), "TRUE"),
            ],
            binding_constraint=None,
            namespace_internal_id=2,
            key_prefix="",
        )
        expected = """
            SELECT p.entity_id, p.name AS pin_key,
                   ix0._VAR_x, ix0._VAR_y, ix1._VAR_z
            FROM namespace_pin p
            JOIN (SELECT * FROM (
                SELECT namespace_id, entity_internal_id,
                       IXKEY_0 AS _VAR_x, IXKEY_1 AS _VAR_y, VALUE AS value
                FROM T1
            ) WHERE TRUE) ix0
                ON ix0.entity_internal_id = p.entity_id AND ix0.namespace_id = 2
            JOIN (SELECT * FROM (
                SELECT namespace_id, entity_internal_id,
                       IXKEY_0 AS _VAR_x, IXKEY_1 AS _VAR_z, VALUE AS value
                FROM T2
            ) WHERE TRUE) ix1
                ON ix1.entity_internal_id = p.entity_id AND ix1.namespace_id = 2
                AND ix1._VAR_x = ix0._VAR_x
            JOIN (SELECT * FROM (
                SELECT namespace_id, entity_internal_id,
                       IXKEY_0 AS _VAR_y, IXKEY_1 AS _VAR_z, VALUE AS value
                FROM T3
            ) WHERE TRUE) ix2
                ON ix2.entity_internal_id = p.entity_id AND ix2.namespace_id = 2
                AND ix2._VAR_y = ix0._VAR_y
                AND ix2._VAR_z = ix1._VAR_z
            WHERE p.namespace_id = 2
        """
        sql_with_params = q.sql
        for param in q.parameters:
            sql_with_params = sql_with_params.replace("?", repr(param), 1)
        assert _canonicalize(sql_with_params) == _canonicalize(expected)


class TestConditionInsideSubquery:
    def test_condition_in_subquery_not_outer_where(self):
        q = generate_iter_matches_query(
            constraints=[
                _rc("NS_INDEX__42", ("t",), "_VAR_t > 3 AND value = 'foo'"),
            ],
            binding_constraint=None,
            namespace_internal_id=7,
            key_prefix="",
        )
        # The condition should appear within the subquery (before the alias)
        # Find the subquery pattern
        assert "WHERE _VAR_t > 3 AND value = 'foo'" in q.sql
        # Make sure it's inside the subquery, not after the main WHERE
        subquery_end = q.sql.index(") ix0")
        condition_pos = q.sql.index("_VAR_t > 3 AND value = 'foo'")
        assert condition_pos < subquery_end


# ---------------------------------------------------------------------------
# End-to-end SQLite tests
# ---------------------------------------------------------------------------


class TestEndToEndSQLite:
    """Run generated queries against real in-memory SQLite databases."""

    @pytest.fixture
    def db(self):
        """Create an in-memory SQLite database with schema and test data."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE namespace_pin (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                namespace_id INTEGER NOT NULL,
                entity_id INTEGER NOT NULL,
                name VARCHAR NOT NULL,
                UNIQUE (namespace_id, name)
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE NS_INDEX__42 (
                namespace_id INTEGER NOT NULL,
                entity_internal_id INTEGER NOT NULL,
                IXKEY_0 INTEGER,
                VALUE REAL,
                PRIMARY KEY (namespace_id, entity_internal_id, IXKEY_0)
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE NS_INDEX__55 (
                namespace_id INTEGER NOT NULL,
                entity_internal_id INTEGER NOT NULL,
                IXKEY_0 INTEGER,
                IXKEY_1 TEXT,
                VALUE REAL,
                PRIMARY KEY (namespace_id, entity_internal_id, IXKEY_0, IXKEY_1)
            )
        """
        )

        ns_id = 1

        # Pin three entities
        conn.execute(
            "INSERT INTO namespace_pin (namespace_id, entity_id, name) VALUES (?, ?, ?)",
            (ns_id, 100, "data/entity_a"),
        )
        conn.execute(
            "INSERT INTO namespace_pin (namespace_id, entity_id, name) VALUES (?, ?, ?)",
            (ns_id, 200, "data/entity_b"),
        )
        conn.execute(
            "INSERT INTO namespace_pin (namespace_id, entity_id, name) VALUES (?, ?, ?)",
            (ns_id, 300, "other/entity_c"),
        )

        # Index data for NS_INDEX__42 (single key: turn index, value: score)
        # entity_a: turns 0,1,2 with scores 0.9, 0.8, 0.7
        for turn, score in [(0, 0.9), (1, 0.8), (2, 0.7)]:
            conn.execute(
                "INSERT INTO NS_INDEX__42 VALUES (?, ?, ?, ?)",
                (ns_id, 100, turn, score),
            )
        # entity_b: turns 0,1 with scores 0.5, 0.95
        for turn, score in [(0, 0.5), (1, 0.95)]:
            conn.execute(
                "INSERT INTO NS_INDEX__42 VALUES (?, ?, ?, ?)",
                (ns_id, 200, turn, score),
            )
        # entity_c: turn 0 with score 0.3
        conn.execute(
            "INSERT INTO NS_INDEX__42 VALUES (?, ?, ?, ?)",
            (ns_id, 300, 0, 0.3),
        )

        # Index data for NS_INDEX__55 (two keys: turn index + metric name, value: metric score)
        # entity_a: turn 0 metrics
        for turn, metric, score in [
            (0, "accuracy", 0.95),
            (0, "f1", 0.87),
            (1, "accuracy", 0.91),
        ]:
            conn.execute(
                "INSERT INTO NS_INDEX__55 VALUES (?, ?, ?, ?, ?)",
                (ns_id, 100, turn, metric, score),
            )
        # entity_b: turn 0 metric
        conn.execute(
            "INSERT INTO NS_INDEX__55 VALUES (?, ?, ?, ?, ?)",
            (ns_id, 200, 0, "accuracy", 0.88),
        )

        conn.commit()
        yield conn
        conn.close()

    def test_single_constraint_all_matches(self, db):
        """Single constraint, no prefix, score > 0.6."""
        q = generate_iter_matches_query(
            constraints=[_rc("NS_INDEX__42", ("t",), "value > 0.6")],
            binding_constraint=None,
            namespace_internal_id=1,
            key_prefix="",
        )
        rows = db.execute(q.sql, q.parameters).fetchall()
        # entity_a: turns 0 (0.9), 1 (0.8), 2 (0.7) — all > 0.6
        # entity_b: turn 1 (0.95) — > 0.6; turn 0 (0.5) — not > 0.6
        # entity_c: turn 0 (0.3) — not > 0.6
        assert len(rows) == 4
        entity_ids = {r[0] for r in rows}
        assert entity_ids == {100, 200}

    def test_single_constraint_with_prefix(self, db):
        """Single constraint with key prefix filter."""
        q = generate_iter_matches_query(
            constraints=[_rc("NS_INDEX__42", ("t",), "value > 0.0")],
            binding_constraint=None,
            namespace_internal_id=1,
            key_prefix="data/",
        )
        rows = db.execute(q.sql, q.parameters).fetchall()
        # Only entities with keys starting with "data/" (entity_a, entity_b)
        entity_ids = {r[0] for r in rows}
        assert 300 not in entity_ids
        assert 100 in entity_ids
        assert 200 in entity_ids

    def test_two_constraints_shared_variable(self, db):
        """Two constraints sharing $t, with unification."""
        q = generate_iter_matches_query(
            constraints=[
                _rc("NS_INDEX__42", ("t",), "value > 0.7"),
                _rc(
                    "NS_INDEX__55",
                    ("t", "m"),
                    "_VAR_m = 'accuracy'",
                    key_types=(IndexerColumnType.INT, IndexerColumnType.STR),
                ),
            ],
            binding_constraint=None,
            namespace_internal_id=1,
            key_prefix="",
        )
        rows = db.execute(q.sql, q.parameters).fetchall()
        # entity_a: NS_INDEX__42 turn 0 (0.9 > 0.7), turn 1 (0.8 > 0.7)
        #           NS_INDEX__55 accuracy at turn 0 and turn 1
        #   Unify t: turn 0 has both, turn 1 has both → 2 matches for entity_a
        # entity_b: NS_INDEX__42 turn 1 (0.95 > 0.7)
        #           NS_INDEX__55 accuracy at turn 0 only
        #   Unify t: turn 1 in 42 but no turn 1 accuracy in 55 → 0 matches
        #           turn 0 in 42 is 0.5, not > 0.7 → 0 matches
        assert len(rows) == 2
        for row in rows:
            assert row[0] == 100  # only entity_a matches
            assert row[1] == "data/entity_a"

    def test_binding_constraint(self, db):
        """Two constraints with a binding constraint on the joined result."""
        q = generate_iter_matches_query(
            constraints=[
                _rc("NS_INDEX__42", ("t",), "TRUE"),
                _rc(
                    "NS_INDEX__55",
                    ("t", "m"),
                    "TRUE",
                    key_types=(IndexerColumnType.INT, IndexerColumnType.STR),
                ),
            ],
            binding_constraint="_VAR_m = 'accuracy'",
            namespace_internal_id=1,
            key_prefix="",
        )
        rows = db.execute(q.sql, q.parameters).fetchall()
        # All entities/turns that have both a score and an accuracy metric at the same turn
        # entity_a turn 0: has both → match
        # entity_a turn 1: has both → match
        # entity_b turn 0: has both → match
        assert len(rows) == 3
        # Check that binding variables are correct in results
        assert q.binding_variable_names == ("t", "m")
        for row in rows:
            assert row[3] == "accuracy"  # _VAR_m value

    def test_result_row_layout(self, db):
        """Verify result row layout: (entity_id, pin_key, var_0, var_1, ...)."""
        q = generate_iter_matches_query(
            constraints=[
                _rc(
                    "NS_INDEX__55",
                    ("t", "m"),
                    "_VAR_m = 'accuracy' AND _VAR_t = 0",
                    key_types=(IndexerColumnType.INT, IndexerColumnType.STR),
                ),
            ],
            binding_constraint=None,
            namespace_internal_id=1,
            key_prefix="data/",
        )
        rows = db.execute(q.sql, q.parameters).fetchall()
        assert len(rows) == 2  # entity_a and entity_b, both at turn 0 accuracy
        for row in rows:
            entity_id, pin_key, var_t, var_m = row
            assert entity_id in (100, 200)
            assert pin_key.startswith("data/")
            assert var_t == 0
            assert var_m == "accuracy"


# ===========================================================================
# Direct SQL query generation (generate_direct_sql_query)
# ===========================================================================


def _rvc(
    column_name: str,
    table: str,
    key_vars: tuple[str, ...],
    condition: str = "TRUE",
    key_types: tuple[IndexerColumnType, ...] | None = None,
    value_type: IndexerColumnType = IndexerColumnType.FLOAT,
) -> ResolvedViewColumn:
    """Shorthand for creating a ResolvedViewColumn."""
    if key_types is None:
        key_types = tuple(IndexerColumnType.INT for _ in key_vars)
    return ResolvedViewColumn(
        column_name=column_name,
        table_name=table,
        key_variables=key_vars,
        key_column_types=key_types,
        value_column_type=value_type,
        condition=condition,
    )


def _rv(name: str, prefix: str, *columns: ResolvedViewColumn) -> ResolvedView:
    """Shorthand for creating a ResolvedView."""
    return ResolvedView(view_name=name, prefix=prefix, columns=list(columns))


class TestDirectSQLStructure:
    """Structural tests for generate_direct_sql_query."""

    def test_single_view_single_column(self):
        q = generate_direct_sql_query(
            views=[_rv("turns", "turns/", _rvc("score", "NS_INDEX__42", ("t",)))],
            user_query="SELECT * FROM turns",
            namespace_internal_id=7,
        )
        assert isinstance(q, GeneratedDirectQuery)
        assert q.sql.startswith("WITH")
        assert "turns AS" in q.sql
        assert "SELECT * FROM turns" in q.sql
        # DB alias qualification
        assert "_fxtr_internal_store.NS_INDEX__42" in q.sql
        assert "_fxtr_internal_store.namespace_pin" in q.sql
        assert "_fxtr_internal_store.entity" in q.sql

    def test_single_view_column_names(self):
        q = generate_direct_sql_query(
            views=[_rv("turns", "", _rvc("score", "NS_INDEX__42", ("t",)))],
            user_query="SELECT * FROM turns",
            namespace_internal_id=7,
        )
        # Should have bare variable name, value column name, pin_name, entity_id
        assert "AS t" in q.sql
        assert "AS score" in q.sql
        assert "AS pin_name" in q.sql
        assert "AS entity_id" in q.sql

    def test_single_view_left_join_entity(self):
        q = generate_direct_sql_query(
            views=[_rv("v", "", _rvc("s", "T1", ("a",)))],
            user_query="SELECT * FROM v",
            namespace_internal_id=1,
        )
        assert "LEFT JOIN _fxtr_internal_store.entity e ON e.id = p.entity_id" in q.sql

    def test_single_view_prefix_filtering(self):
        q = generate_direct_sql_query(
            views=[_rv("turns", "turns/", _rvc("score", "T1", ("t",)))],
            user_query="SELECT * FROM turns",
            namespace_internal_id=7,
        )
        assert "LIKE" in q.sql
        assert "turns/" in q.parameters

    def test_single_view_no_prefix(self):
        q = generate_direct_sql_query(
            views=[_rv("v", "", _rvc("s", "T1", ("a",)))],
            user_query="SELECT * FROM v",
            namespace_internal_id=1,
        )
        assert "LIKE" not in q.sql

    def test_two_columns_shared_variable_unification(self):
        q = generate_direct_sql_query(
            views=[
                _rv(
                    "turns",
                    "turns/",
                    _rvc("score", "NS_INDEX__42", ("t", "m")),
                    _rvc("source", "NS_INDEX__55", ("t",)),
                ),
            ],
            user_query="SELECT * FROM turns",
            namespace_internal_id=7,
        )
        # Should have equijoin for shared variable t
        assert "ix1._VAR_t = ix0._VAR_t" in q.sql
        # Both value columns named
        assert "AS score" in q.sql
        assert "AS source" in q.sql

    def test_two_views(self):
        q = generate_direct_sql_query(
            views=[
                _rv("turns", "turns/", _rvc("score", "T1", ("t",))),
                _rv("metrics", "metrics/", _rvc("desc", "T2", ("x",))),
            ],
            user_query="SELECT * FROM turns JOIN metrics ON turns.t = metrics.x",
            namespace_internal_id=7,
        )
        assert "turns AS" in q.sql
        assert "metrics AS" in q.sql
        assert q.sql.count("WITH") == 1  # single WITH clause

    def test_per_constraint_condition_inside_subquery(self):
        q = generate_direct_sql_query(
            views=[_rv("v", "", _rvc("s", "T1", ("a",), condition="value > 3"))],
            user_query="SELECT * FROM v",
            namespace_internal_id=1,
        )
        # Condition should appear inside the subquery
        assert "WHERE value > 3" in q.sql

    def test_parameters_single_view_with_prefix(self):
        q = generate_direct_sql_query(
            views=[_rv("v", "p/", _rvc("s", "T1", ("a",)))],
            user_query="SELECT * FROM v",
            namespace_internal_id=7,
        )
        # namespace_id in JOIN ON + namespace_id in WHERE + prefix
        assert q.parameters == [7, 7, "p/"]

    def test_parameters_two_columns_with_prefix(self):
        q = generate_direct_sql_query(
            views=[
                _rv(
                    "v",
                    "p/",
                    _rvc("s1", "T1", ("a",)),
                    _rvc("s2", "T2", ("a",)),
                ),
            ],
            user_query="SELECT * FROM v",
            namespace_internal_id=7,
        )
        # Two JOIN ONs + WHERE + prefix
        assert q.parameters == [7, 7, 7, "p/"]

    def test_empty_views_rejected(self):
        with pytest.raises(ValueError, match="At least one view"):
            generate_direct_sql_query(
                views=[],
                user_query="SELECT 1",
                namespace_internal_id=1,
            )


class TestDirectSQLAST:
    """Compare direct SQL generation against expected SQL via AST canonicalization."""

    def test_single_view_single_column_no_prefix(self):
        q = generate_direct_sql_query(
            views=[_rv("turns", "", _rvc("score", "NS_INDEX__42", ("t",), "value > 0.5"))],
            user_query="SELECT t, score FROM turns",
            namespace_internal_id=7,
            db_alias="s",
        )
        expected = """
            WITH turns AS (
              SELECT
                ix0._VAR_t AS t,
                ix0.value AS score,
                p.name AS pin_name,
                e.public_id AS entity_id
              FROM s.namespace_pin p
              JOIN (SELECT * FROM (
                SELECT namespace_id, entity_internal_id, IXKEY_0 AS _VAR_t, VALUE AS value
                FROM s.NS_INDEX__42
              ) WHERE value > 0.5) ix0
                ON ix0.entity_internal_id = p.entity_id AND ix0.namespace_id = 7
              LEFT JOIN s.entity e ON e.id = p.entity_id
              WHERE p.namespace_id = 7
            )
            SELECT t, score FROM turns
        """
        sql_with_params = q.sql
        for param in q.parameters:
            sql_with_params = sql_with_params.replace("?", repr(param), 1)
        assert _canonicalize(sql_with_params) == _canonicalize(expected)

    def test_single_view_two_columns_shared_var(self):
        q = generate_direct_sql_query(
            views=[
                _rv(
                    "turns",
                    "turns/",
                    _rvc("score", "NS_INDEX__42", ("t", "m")),
                    _rvc("source", "NS_INDEX__55", ("t",)),
                ),
            ],
            user_query="SELECT * FROM turns",
            namespace_internal_id=7,
            db_alias="s",
        )
        expected = """
            WITH turns AS (
              SELECT
                ix0._VAR_t AS t,
                ix0._VAR_m AS m,
                ix0.value AS score,
                ix1.value AS source,
                p.name AS pin_name,
                e.public_id AS entity_id
              FROM s.namespace_pin p
              JOIN (SELECT * FROM (
                SELECT namespace_id, entity_internal_id,
                       IXKEY_0 AS _VAR_t, IXKEY_1 AS _VAR_m, VALUE AS value
                FROM s.NS_INDEX__42
              ) WHERE TRUE) ix0
                ON ix0.entity_internal_id = p.entity_id AND ix0.namespace_id = 7
              JOIN (SELECT * FROM (
                SELECT namespace_id, entity_internal_id,
                       IXKEY_0 AS _VAR_t, VALUE AS value
                FROM s.NS_INDEX__55
              ) WHERE TRUE) ix1
                ON ix1.entity_internal_id = p.entity_id AND ix1.namespace_id = 7
                AND ix1._VAR_t = ix0._VAR_t
              LEFT JOIN s.entity e ON e.id = p.entity_id
              WHERE p.namespace_id = 7 AND p.name LIKE 'turns/' || '%'
            )
            SELECT * FROM turns
        """
        sql_with_params = q.sql
        for param in q.parameters:
            sql_with_params = sql_with_params.replace("?", repr(param), 1)
        assert _canonicalize(sql_with_params) == _canonicalize(expected)

    def test_two_views_joined(self):
        q = generate_direct_sql_query(
            views=[
                _rv("turns", "turns/", _rvc("score", "T1", ("t",))),
                _rv(
                    "metrics",
                    "metrics/",
                    _rvc("desc", "T2", ("x",), value_type=IndexerColumnType.STR),
                ),
            ],
            user_query="SELECT turns.t, turns.score, metrics.desc FROM turns JOIN metrics ON 1=1",
            namespace_internal_id=3,
            db_alias="s",
        )
        expected = """
            WITH
            turns AS (
              SELECT
                ix0._VAR_t AS t,
                ix0.value AS score,
                p.name AS pin_name,
                e.public_id AS entity_id
              FROM s.namespace_pin p
              JOIN (SELECT * FROM (
                SELECT namespace_id, entity_internal_id, IXKEY_0 AS _VAR_t, VALUE AS value
                FROM s.T1
              ) WHERE TRUE) ix0
                ON ix0.entity_internal_id = p.entity_id AND ix0.namespace_id = 3
              LEFT JOIN s.entity e ON e.id = p.entity_id
              WHERE p.namespace_id = 3 AND p.name LIKE 'turns/' || '%'
            ),
            metrics AS (
              SELECT
                ix0._VAR_x AS x,
                ix0.value AS desc,
                p.name AS pin_name,
                e.public_id AS entity_id
              FROM s.namespace_pin p
              JOIN (SELECT * FROM (
                SELECT namespace_id, entity_internal_id, IXKEY_0 AS _VAR_x, VALUE AS value
                FROM s.T2
              ) WHERE TRUE) ix0
                ON ix0.entity_internal_id = p.entity_id AND ix0.namespace_id = 3
              LEFT JOIN s.entity e ON e.id = p.entity_id
              WHERE p.namespace_id = 3 AND p.name LIKE 'metrics/' || '%'
            )
            SELECT turns.t, turns.score, metrics.desc
            FROM turns JOIN metrics ON 1 = 1
        """
        sql_with_params = q.sql
        for param in q.parameters:
            sql_with_params = sql_with_params.replace("?", repr(param), 1)
        assert _canonicalize(sql_with_params) == _canonicalize(expected)


class TestDirectSQLEndToEnd:
    """Run direct SQL queries against real in-memory SQLite databases."""

    @pytest.fixture
    def db(self):
        """Create an in-memory SQLite database with schema and test data.

        Simulates the structure that would exist when ATTACHed. Uses a
        single connection (no ATTACH needed since we're testing CTE logic).
        """
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE namespace_pin (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                namespace_id INTEGER NOT NULL,
                entity_id INTEGER NOT NULL,
                name VARCHAR NOT NULL,
                UNIQUE (namespace_id, name)
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE entity (
                id INTEGER PRIMARY KEY,
                public_id BLOB UNIQUE,
                storage_location VARCHAR(10) NOT NULL DEFAULT 'data1/1'
            )
        """
        )
        # Two index tables
        conn.execute(
            """
            CREATE TABLE NS_INDEX__42 (
                namespace_id INTEGER NOT NULL,
                entity_internal_id INTEGER NOT NULL,
                IXKEY_0 INTEGER,
                IXKEY_1 TEXT,
                VALUE REAL,
                PRIMARY KEY (namespace_id, entity_internal_id, IXKEY_0, IXKEY_1)
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE NS_INDEX__43 (
                namespace_id INTEGER NOT NULL,
                entity_internal_id INTEGER NOT NULL,
                IXKEY_0 INTEGER,
                VALUE TEXT,
                PRIMARY KEY (namespace_id, entity_internal_id, IXKEY_0)
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE NS_INDEX__50 (
                namespace_id INTEGER NOT NULL,
                entity_internal_id INTEGER NOT NULL,
                VALUE TEXT,
                PRIMARY KEY (namespace_id, entity_internal_id)
            )
        """
        )

        ns_id = 1

        # Entities
        conn.execute("INSERT INTO entity (id, public_id) VALUES (100, X'AABB')")
        conn.execute("INSERT INTO entity (id, public_id) VALUES (200, X'CCDD')")
        conn.execute("INSERT INTO entity (id, public_id) VALUES (300, X'EEFF')")

        # Pins
        conn.execute(
            "INSERT INTO namespace_pin (namespace_id, entity_id, name) VALUES (?, ?, ?)",
            (ns_id, 100, "turns/run_a"),
        )
        conn.execute(
            "INSERT INTO namespace_pin (namespace_id, entity_id, name) VALUES (?, ?, ?)",
            (ns_id, 200, "turns/run_b"),
        )
        conn.execute(
            "INSERT INTO namespace_pin (namespace_id, entity_id, name) VALUES (?, ?, ?)",
            (ns_id, 300, "metrics/accuracy"),
        )

        # NS_INDEX__42: turns[$t].metrics[$m].score (float)
        for eid, t, m, score in [
            (100, 0, "accuracy", 0.95),
            (100, 0, "f1", 0.87),
            (100, 1, "accuracy", 0.91),
            (200, 0, "accuracy", 0.88),
            (200, 0, "f1", 0.82),
        ]:
            conn.execute(
                "INSERT INTO NS_INDEX__42 VALUES (?, ?, ?, ?, ?)",
                (ns_id, eid, t, m, score),
            )

        # NS_INDEX__43: turns[$t].source (str)
        for eid, t, source in [
            (100, 0, "gpt-4"),
            (100, 1, "claude"),
            (200, 0, "gpt-4"),
        ]:
            conn.execute(
                "INSERT INTO NS_INDEX__43 VALUES (?, ?, ?, ?)",
                (ns_id, eid, t, source),
            )

        # NS_INDEX__50: metrics description (str, no key variables)
        conn.execute(
            "INSERT INTO NS_INDEX__50 VALUES (?, ?, ?)",
            (ns_id, 300, "Accuracy metric"),
        )

        conn.commit()
        yield conn
        conn.close()

    def _run(self, db, q: GeneratedDirectQuery) -> list[sqlite3.Row]:
        """Execute a direct SQL query, using no db_alias qualification."""
        return db.execute(q.sql, q.parameters).fetchall()

    def test_basic_single_view(self, db):
        """Simple query on a single-column view."""
        q = generate_direct_sql_query(
            views=[
                _rv(
                    "turns",
                    "turns/",
                    _rvc(
                        "score",
                        "NS_INDEX__42",
                        ("t", "m"),
                        key_types=(IndexerColumnType.INT, IndexerColumnType.STR),
                    ),
                ),
            ],
            user_query="SELECT t, m, score, pin_name FROM turns ORDER BY score DESC",
            namespace_internal_id=1,
            db_alias="main",  # Use "main" since we're testing in-memory
        )
        rows = self._run(db, q)
        assert len(rows) == 5
        # Check ordering
        scores = [r["score"] for r in rows]
        assert scores == sorted(scores, reverse=True)
        # All pins should start with turns/
        for r in rows:
            assert r["pin_name"].startswith("turns/")

    def test_two_columns_with_shared_variable(self, db):
        """Two columns joined on shared variable $t."""
        q = generate_direct_sql_query(
            views=[
                _rv(
                    "turns",
                    "turns/",
                    _rvc(
                        "score",
                        "NS_INDEX__42",
                        ("t", "m"),
                        key_types=(IndexerColumnType.INT, IndexerColumnType.STR),
                    ),
                    _rvc("source", "NS_INDEX__43", ("t",)),
                ),
            ],
            user_query="SELECT t, m, score, source FROM turns ORDER BY t, m",
            namespace_internal_id=1,
            db_alias="main",
        )
        rows = self._run(db, q)
        # entity 100: (0, accuracy, 0.95, gpt-4), (0, f1, 0.87, gpt-4),
        #             (1, accuracy, 0.91, claude)
        # entity 200: (0, accuracy, 0.88, gpt-4), (0, f1, 0.82, gpt-4)
        assert len(rows) == 5
        # Check a specific row
        row0 = rows[0]
        assert row0["t"] == 0

    def test_aggregation_across_views(self, db):
        """GROUP BY and AVG across a view."""
        q = generate_direct_sql_query(
            views=[
                _rv(
                    "turns",
                    "turns/",
                    _rvc(
                        "score",
                        "NS_INDEX__42",
                        ("t", "m"),
                        key_types=(IndexerColumnType.INT, IndexerColumnType.STR),
                    ),
                ),
            ],
            user_query="SELECT m, AVG(score) AS avg_score FROM turns GROUP BY m ORDER BY m",
            namespace_internal_id=1,
            db_alias="main",
        )
        rows = self._run(db, q)
        assert len(rows) == 2  # "accuracy" and "f1"
        by_name = {r["m"]: r["avg_score"] for r in rows}
        # accuracy: (0.95 + 0.91 + 0.88) / 3 ≈ 0.9133
        assert abs(by_name["accuracy"] - (0.95 + 0.91 + 0.88) / 3) < 0.001
        # f1: (0.87 + 0.82) / 2 = 0.845
        assert abs(by_name["f1"] - (0.87 + 0.82) / 2) < 0.001

    def test_two_views_joined(self, db):
        """Join two views from different prefixes."""
        q = generate_direct_sql_query(
            views=[
                _rv(
                    "turns",
                    "turns/",
                    _rvc(
                        "score",
                        "NS_INDEX__42",
                        ("t", "m"),
                        key_types=(IndexerColumnType.INT, IndexerColumnType.STR),
                    ),
                ),
                _rv(
                    "metric_info",
                    "metrics/",
                    _rvc("description", "NS_INDEX__50", (), value_type=IndexerColumnType.STR),
                ),
            ],
            user_query=(
                "SELECT turns.m, AVG(turns.score) AS avg_score, metric_info.description "
                "FROM turns "
                "JOIN metric_info ON metric_info.pin_name = ('metrics/' || turns.m) "
                "GROUP BY turns.m"
            ),
            namespace_internal_id=1,
            db_alias="main",
        )
        rows = self._run(db, q)
        # Only "accuracy" has a matching metric_info entry
        assert len(rows) == 1
        assert rows[0]["m"] == "accuracy"
        assert rows[0]["description"] == "Accuracy metric"

    def test_entity_id_column(self, db):
        """Verify entity_id returns the public_id BLOB."""
        q = generate_direct_sql_query(
            views=[
                _rv(
                    "v",
                    "turns/",
                    _rvc(
                        "score",
                        "NS_INDEX__42",
                        ("t", "m"),
                        key_types=(IndexerColumnType.INT, IndexerColumnType.STR),
                    ),
                ),
            ],
            user_query="SELECT DISTINCT entity_id, pin_name FROM v ORDER BY pin_name",
            namespace_internal_id=1,
            db_alias="main",
        )
        rows = self._run(db, q)
        assert len(rows) == 2
        # entity_id should be the BLOB public_id
        ids = {r["entity_id"] for r in rows}
        assert ids == {b"\xaa\xbb", b"\xcc\xdd"}

    def test_prefix_filtering(self, db):
        """Verify prefix limits which pins appear in the view."""
        # Only "turns/" prefix
        q = generate_direct_sql_query(
            views=[
                _rv(
                    "v",
                    "turns/",
                    _rvc(
                        "score",
                        "NS_INDEX__42",
                        ("t", "m"),
                        key_types=(IndexerColumnType.INT, IndexerColumnType.STR),
                    ),
                ),
            ],
            user_query="SELECT DISTINCT pin_name FROM v",
            namespace_internal_id=1,
            db_alias="main",
        )
        rows = self._run(db, q)
        pin_names = {r["pin_name"] for r in rows}
        assert all(p.startswith("turns/") for p in pin_names)
        assert "metrics/accuracy" not in pin_names

    def test_per_constraint_condition(self, db):
        """Verify per-constraint conditions are applied inside CTEs."""
        q = generate_direct_sql_query(
            views=[
                _rv(
                    "v",
                    "turns/",
                    _rvc(
                        "score",
                        "NS_INDEX__42",
                        ("t", "m"),
                        condition="value > 0.9",
                        key_types=(IndexerColumnType.INT, IndexerColumnType.STR),
                    ),
                ),
            ],
            user_query="SELECT t, m, score FROM v ORDER BY score",
            namespace_internal_id=1,
            db_alias="main",
        )
        rows = self._run(db, q)
        # Only scores > 0.9: 0.95 and 0.91
        assert len(rows) == 2
        for r in rows:
            assert r["score"] > 0.9
