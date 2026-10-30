"""Tests for SQL expression parsing, validation, and rewriting."""

import pytest

from fxtr2.data.store_indexing.indexing_types import IndexerColumnType
from fxtr2.data.store_indexing.sql_expr import COLUMN_TYPE_TO_SQLITE, parse_condition

# ---------------------------------------------------------------------------
# Basic valid expressions
# ---------------------------------------------------------------------------


class TestBasicValidExpressions:
    def test_simple_variable_comparison(self):
        result = parse_condition("$t > 3", frozenset({"t"}))
        assert result == "_VAR_t > 3"

    def test_variable_and_value(self):
        result = parse_condition("$t > 3 AND value = 'foo'", frozenset({"t"}))
        assert result == "_VAR_t > 3 AND value = 'foo'"

    def test_between(self):
        result = parse_condition("$t BETWEEN 1 AND 10", frozenset({"t"}))
        assert result == "_VAR_t BETWEEN 1 AND 10"

    def test_in_list(self):
        result = parse_condition("$t IN (1, 2, 3)", frozenset({"t"}))
        assert result == "_VAR_t IN (1, 2, 3)"

    def test_is_null(self):
        result = parse_condition("$t IS NULL", frozenset({"t"}))
        assert result == "_VAR_t IS NULL"

    def test_is_not_null(self):
        result = parse_condition("value IS NOT NULL", frozenset(set()))
        # sqlglot may render as "NOT value IS NULL" or "value IS NOT NULL"
        assert "value" in result
        assert "NULL" in result
        assert "NOT" in result

    def test_true_literal(self):
        result = parse_condition("TRUE", frozenset(set()))
        assert result == "TRUE"

    def test_false_literal(self):
        result = parse_condition("FALSE", frozenset(set()))
        assert result == "FALSE"

    def test_numeric_literal(self):
        result = parse_condition("1", frozenset(set()))
        assert result == "1"

    def test_multiple_variables(self):
        result = parse_condition("$t > 0 AND $m = 'accuracy'", frozenset({"t", "m"}))
        assert "_VAR_t > 0" in result
        assert "_VAR_m = 'accuracy'" in result


# ---------------------------------------------------------------------------
# Arithmetic and functions
# ---------------------------------------------------------------------------


class TestArithmeticAndFunctions:
    def test_arithmetic_with_variables(self):
        result = parse_condition("$a + $b > $c", frozenset({"a", "b", "c"}))
        assert "_VAR_a" in result
        assert "_VAR_b" in result
        assert "_VAR_c" in result

    def test_abs_function(self):
        result = parse_condition("ABS($t) > 3", frozenset({"t"}))
        assert "ABS(_VAR_t) > 3" == result

    def test_length_function(self):
        result = parse_condition("LENGTH(value) > 5", frozenset(set()))
        assert "LENGTH(value) > 5" == result

    def test_coalesce_function(self):
        result = parse_condition("COALESCE($t, 0) > 0", frozenset({"t"}))
        assert "COALESCE(_VAR_t, 0) > 0" == result

    def test_cast_function(self):
        result = parse_condition("CAST($t AS INTEGER) > 0", frozenset({"t"}))
        assert "CAST(_VAR_t AS INT)" in result or "CAST(_VAR_t AS INTEGER)" in result

    def test_nested_functions(self):
        result = parse_condition("ABS(ROUND($t, 2)) > 1.0", frozenset({"t"}))
        assert "_VAR_t" in result


# ---------------------------------------------------------------------------
# Rewriting correctness
# ---------------------------------------------------------------------------


class TestRewriting:
    def test_dollar_prefix_rewritten(self):
        result = parse_condition("$t > 3", frozenset({"t"}))
        assert "$t" not in result
        assert "_VAR_t" in result

    def test_value_not_rewritten(self):
        result = parse_condition("value = 'foo'", frozenset(set()))
        assert result == "value = 'foo'"

    def test_multiple_vars_all_rewritten(self):
        result = parse_condition("$x > $y AND $z = 1", frozenset({"x", "y", "z"}))
        assert "$" not in result
        assert "_VAR_x" in result
        assert "_VAR_y" in result
        assert "_VAR_z" in result

    def test_string_literals_not_affected(self):
        result = parse_condition("value = '$not_a_var'", frozenset(set()))
        assert "'$not_a_var'" in result


# ---------------------------------------------------------------------------
# Variable validation
# ---------------------------------------------------------------------------


class TestVariableValidation:
    def test_reject_unknown_variable(self):
        with pytest.raises(ValueError, match="Unknown variable.*unknown"):
            parse_condition("$unknown > 3", frozenset({"t"}))

    def test_reject_variable_not_in_set(self):
        with pytest.raises(ValueError, match="Unknown variable.*m"):
            parse_condition("$t > 3 AND $m > 0", frozenset({"t"}))

    def test_allow_all_declared_variables(self):
        # Should not raise
        result = parse_condition("$a > $b", frozenset({"a", "b"}))
        assert "_VAR_a" in result
        assert "_VAR_b" in result

    def test_empty_allowed_variables_rejects_any_var(self):
        with pytest.raises(ValueError, match="Unknown variable"):
            parse_condition("$t > 3", frozenset(set()))


# ---------------------------------------------------------------------------
# value keyword handling
# ---------------------------------------------------------------------------


class TestValueKeyword:
    def test_value_allowed_by_default(self):
        result = parse_condition("value > 3", frozenset(set()))
        assert result == "value > 3"

    def test_value_rejected_when_disallowed(self):
        with pytest.raises(ValueError, match="'value' column is not allowed"):
            parse_condition("value > 3", frozenset(set()), allow_value=False)

    def test_value_as_variable_name_separate_from_keyword(self):
        # $value is a binding variable named "value", distinct from bare `value`
        result = parse_condition("$value > value", frozenset({"value"}), allow_value=True)
        assert "_VAR_value" in result
        assert "value" in result  # bare value still present

    def test_only_vars_allowed_in_binding_constraint(self):
        result = parse_condition("$t + $m > 10", frozenset({"t", "m"}), allow_value=False)
        assert "_VAR_t" in result
        assert "_VAR_m" in result


# ---------------------------------------------------------------------------
# Bare column rejection
# ---------------------------------------------------------------------------


class TestBareColumnRejection:
    def test_reject_arbitrary_column(self):
        with pytest.raises(ValueError, match="Unknown column reference.*some_column"):
            parse_condition("some_column > 3", frozenset({"t"}))

    def test_reject_table_qualified_column(self):
        with pytest.raises(ValueError, match="Table-qualified"):
            parse_condition("t.col > 3", frozenset({"t"}))


# ---------------------------------------------------------------------------
# Statement injection
# ---------------------------------------------------------------------------


class TestStatementInjection:
    def test_reject_select(self):
        with pytest.raises(ValueError, match="SQL statement"):
            parse_condition("SELECT * FROM t", frozenset(set()))

    def test_reject_insert(self):
        with pytest.raises(ValueError, match="SQL statement"):
            parse_condition("INSERT INTO t VALUES (1)", frozenset(set()))

    def test_reject_multi_statement(self):
        with pytest.raises(ValueError, match="statements"):
            parse_condition("1; DROP TABLE t", frozenset(set()))

    def test_reject_drop(self):
        with pytest.raises(ValueError, match="SQL statement"):
            parse_condition("DROP TABLE t", frozenset(set()))


# ---------------------------------------------------------------------------
# Subquery injection
# ---------------------------------------------------------------------------


class TestSubqueryInjection:
    def test_reject_subquery_in_comparison(self):
        with pytest.raises(ValueError, match="(subquery|SQL statement)"):
            parse_condition("$t > (SELECT 1)", frozenset({"t"}))

    def test_reject_exists(self):
        # EXISTS is caught either as a disallowed function or nested statement
        with pytest.raises(ValueError):
            parse_condition("EXISTS (SELECT 1)", frozenset(set()))

    def test_reject_in_subquery(self):
        with pytest.raises(ValueError, match="(subquery|SQL statement)"):
            parse_condition("$t IN (SELECT 1 FROM t)", frozenset({"t"}))


# ---------------------------------------------------------------------------
# Function safety
# ---------------------------------------------------------------------------


class TestFunctionSafety:
    def test_reject_load_extension(self):
        with pytest.raises(ValueError, match="Disallowed function"):
            parse_condition("load_extension('x')", frozenset(set()))

    def test_allow_abs(self):
        result = parse_condition("ABS($t) > 3", frozenset({"t"}))
        assert "_VAR_t" in result

    def test_allow_length(self):
        result = parse_condition("LENGTH(value) > 0", frozenset(set()))
        assert "LENGTH(value)" in result

    def test_allow_coalesce(self):
        result = parse_condition("COALESCE(value, 0) > 0", frozenset(set()))
        assert "COALESCE" in result

    def test_reject_unknown_function(self):
        with pytest.raises(ValueError, match="Disallowed function"):
            parse_condition("my_evil_func($t)", frozenset({"t"}))


# ---------------------------------------------------------------------------
# Duplicate variable rejection in bind()
# ---------------------------------------------------------------------------


class TestBindValidation:
    """Test that Indexer.bind() validates variables and conditions eagerly."""

    def _make_indexer(self, num_keys: int):
        """Create a minimal Indexer subclass for testing bind()."""
        from fxtr2.conversion.converters.legacy_entity_abc import SelfLoadingLegacyEntity
        from fxtr2.data.entity_core import FxtrID
        from fxtr2.data.store_indexing.indexing_types import Indexer, IndexerColumnType

        class FakeIndexer(SelfLoadingLegacyEntity, Indexer):
            def index_key_types(self):
                return [IndexerColumnType.INT] * num_keys

            def index_value_type(self):
                return IndexerColumnType.FLOAT

            async def unpack_entity(self, entity_data, scope):
                return []

            def _to_fxtr_cbor(self, scope):
                raise NotImplementedError

            @classmethod
            def _from_fxtr_cbor(cls, cbor, annotated_type, scope):
                raise NotImplementedError

            @classmethod
            def _fxtr_type_id(cls):
                return FxtrID.parse("!fake_indexer")

        return FakeIndexer()

    def test_duplicate_variable_names_rejected(self):
        indexer = self._make_indexer(2)
        with pytest.raises(ValueError, match="Duplicate variable names"):
            indexer.bind(("x", "x"), "TRUE")

    def test_bind_validates_condition(self):
        indexer = self._make_indexer(1)
        with pytest.raises(ValueError, match="Unknown variable"):
            indexer.bind(("t",), "$unknown > 3")

    def test_bind_validates_statement_injection(self):
        indexer = self._make_indexer(1)
        with pytest.raises(ValueError, match="SQL statement"):
            indexer.bind(("t",), "SELECT * FROM t")

    def test_bind_succeeds_with_valid_input(self):
        indexer = self._make_indexer(2)
        constraint = indexer.bind(("t", "m"), "$t > 3 AND value = 'foo'")
        assert constraint.key_variables == ("t", "m")
        assert constraint.condition == "$t > 3 AND value = 'foo'"


# ---------------------------------------------------------------------------
# Column type mapping
# ---------------------------------------------------------------------------


class TestColumnTypeMapping:
    def test_all_types_mapped(self):
        for ct in IndexerColumnType:
            assert ct in COLUMN_TYPE_TO_SQLITE

    def test_int_maps_to_integer(self):
        assert COLUMN_TYPE_TO_SQLITE[IndexerColumnType.INT] == "INTEGER"

    def test_str_maps_to_text(self):
        assert COLUMN_TYPE_TO_SQLITE[IndexerColumnType.STR] == "TEXT"

    def test_bool_maps_to_integer(self):
        assert COLUMN_TYPE_TO_SQLITE[IndexerColumnType.BOOL] == "INTEGER"

    def test_float_maps_to_real(self):
        assert COLUMN_TYPE_TO_SQLITE[IndexerColumnType.FLOAT] == "REAL"

    def test_bytes_maps_to_blob(self):
        assert COLUMN_TYPE_TO_SQLITE[IndexerColumnType.BYTES] == "BLOB"

    def test_fxtr_id_maps_to_blob(self):
        assert COLUMN_TYPE_TO_SQLITE[IndexerColumnType.FXTR_ID] == "BLOB"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_string_rejected(self):
        with pytest.raises(ValueError):
            parse_condition("", frozenset(set()))

    def test_whitespace_only_rejected(self):
        with pytest.raises(ValueError):
            parse_condition("   ", frozenset(set()))

    def test_complex_expression(self):
        result = parse_condition(
            "$t > 0 AND ($m = 'accuracy' OR $m = 'f1') AND value BETWEEN 0.5 AND 1.0",
            frozenset({"t", "m"}),
        )
        assert "_VAR_t" in result
        assert "_VAR_m" in result
        assert "$" not in result
        assert "value" in result

    def test_not_expression(self):
        result = parse_condition("NOT $t > 3", frozenset({"t"}))
        assert "NOT" in result
        assert "_VAR_t" in result

    def test_or_expression(self):
        result = parse_condition("$t > 3 OR $t < 1", frozenset({"t"}))
        assert "OR" in result
        assert "_VAR_t" in result
