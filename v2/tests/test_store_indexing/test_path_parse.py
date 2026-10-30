"""Tests for path expression parsing (1b)."""

from __future__ import annotations

import uuid

import pytest

from fxtr2.data.store_indexing.indexers import (
    DerefMarker,
    PathIndex,
    PathKey,
    TypedDerefMarker,
    TypedPathExistsIndexer,
    TypedPathValueIndexer,
    VarMarker,
)
from fxtr2.data.store_indexing.indexing_types import IndexerColumnType
from fxtr2.data.store_indexing.path_parse import parse_path_expression
from fxtr2.data.system_entities import define_type

_TEST_NS = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
_TYPE_A = define_type(_TEST_NS, "parse_test_type_a")
_TYPE_B = define_type(_TEST_NS, "parse_test_type_b")


# ---------------------------------------------------------------------------
# Basic parsing (unnamed vars, parse mode)
# ---------------------------------------------------------------------------


class TestBasicParsing:
    def test_simple_path(self):
        r = parse_path_expression("!.name: str", named_vars=False, require_value_type=True)
        assert isinstance(r.initial_lookup, DerefMarker)
        assert r.path == (PathKey(key="name"),)
        assert r.value_type == IndexerColumnType.STR
        assert r.var_types == ()
        assert r.var_names is None

    def test_dotted_path(self):
        r = parse_path_expression("!.foo.bar.baz: int", named_vars=False, require_value_type=True)
        assert isinstance(r.initial_lookup, DerefMarker)
        assert r.path == (PathKey(key="foo"), PathKey(key="bar"), PathKey(key="baz"))
        assert r.value_type == IndexerColumnType.INT

    def test_unnamed_variable(self):
        r = parse_path_expression(
            "!.items[*: int].name: str", named_vars=False, require_value_type=True
        )
        assert r.path == (PathKey(key="items"), VarMarker(), PathKey(key="name"))
        assert r.var_types == (IndexerColumnType.INT,)
        assert r.value_type == IndexerColumnType.STR

    def test_multiple_unnamed_vars(self):
        r = parse_path_expression(
            "!.turns[*: int].metrics[*: str].score: float",
            named_vars=False,
            require_value_type=True,
        )
        assert r.path == (
            PathKey(key="turns"),
            VarMarker(),
            PathKey(key="metrics"),
            VarMarker(),
            PathKey(key="score"),
        )
        assert r.var_types == (IndexerColumnType.INT, IndexerColumnType.STR)
        assert r.value_type == IndexerColumnType.FLOAT

    def test_fixed_index(self):
        r = parse_path_expression("!.items[3].name: str", named_vars=False, require_value_type=True)
        assert r.path == (PathKey(key="items"), PathIndex(index=3), PathKey(key="name"))

    def test_all_column_types(self):
        for type_name, expected in [
            ("int", IndexerColumnType.INT),
            ("str", IndexerColumnType.STR),
            ("bool", IndexerColumnType.BOOL),
            ("bytes", IndexerColumnType.BYTES),
            ("float", IndexerColumnType.FLOAT),
            ("FxtrID", IndexerColumnType.FXTR_ID),
        ]:
            r = parse_path_expression(
                f"!.x: {type_name}", named_vars=False, require_value_type=True
            )
            assert r.value_type == expected, f"Failed for {type_name}"


# ---------------------------------------------------------------------------
# Named vars (query mode)
# ---------------------------------------------------------------------------


class TestNamedVars:
    def test_named_variables(self):
        r = parse_path_expression(
            "!.turns[$t: int].metrics[$m: str].score: float",
            named_vars=True,
            require_value_type=True,
        )
        assert r.var_names == ("t", "m")
        assert r.var_types == (IndexerColumnType.INT, IndexerColumnType.STR)
        assert r.path == (
            PathKey(key="turns"),
            VarMarker(),
            PathKey(key="metrics"),
            VarMarker(),
            PathKey(key="score"),
        )

    def test_single_named_var(self):
        r = parse_path_expression(
            "!.items[$i: int].value: float", named_vars=True, require_value_type=True
        )
        assert r.var_names == ("i",)
        assert r.var_types == (IndexerColumnType.INT,)


# ---------------------------------------------------------------------------
# Deref markers
# ---------------------------------------------------------------------------


class TestDerefParsing:
    def test_mid_path_deref(self):
        r = parse_path_expression("!.ref!.name: str", named_vars=False, require_value_type=True)
        assert r.path == (PathKey(key="ref"), DerefMarker(), PathKey(key="name"))

    def test_mid_path_typed_deref_strict(self):
        r = parse_path_expression(
            f"!.ref!(as {_TYPE_A:q} | {_TYPE_B:q}).name: str",
            named_vars=False,
            require_value_type=True,
        )
        assert len(r.path) == 3
        assert r.path[0] == PathKey(key="ref")
        td = r.path[1]
        assert isinstance(td, TypedDerefMarker)
        assert td.supported_types == frozenset({_TYPE_A, _TYPE_B})
        assert td.strict is True
        assert r.path[2] == PathKey(key="name")

    def test_mid_path_typed_deref_lax(self):
        r = parse_path_expression(
            f"!.ref!(as? {_TYPE_A:q}).name: str",
            named_vars=False,
            require_value_type=True,
        )
        td = r.path[1]
        assert isinstance(td, TypedDerefMarker)
        assert td.supported_types == frozenset({_TYPE_A})
        assert td.strict is False

    def test_strict_initial_typed_deref(self):
        r = parse_path_expression(
            f"!(as {_TYPE_A:q}).turns[*: int].name: str",
            named_vars=False,
            require_value_type=True,
        )
        assert isinstance(r.initial_lookup, TypedDerefMarker)
        assert r.initial_lookup.supported_types == frozenset({_TYPE_A})
        assert r.initial_lookup.strict is True
        assert r.path[0] == PathKey(key="turns")

    def test_lax_initial_typed_deref(self):
        r = parse_path_expression(
            f"!(as? {_TYPE_A:q}).turns[*: int].name: str",
            named_vars=False,
            require_value_type=True,
        )
        assert isinstance(r.initial_lookup, TypedDerefMarker)
        assert r.initial_lookup.strict is False

    def test_multiple_types_in_initial(self):
        r = parse_path_expression(
            f"!(as {_TYPE_A:q} | {_TYPE_B:q}).x: int",
            named_vars=False,
            require_value_type=True,
        )
        assert isinstance(r.initial_lookup, TypedDerefMarker)
        assert r.initial_lookup.supported_types == frozenset({_TYPE_A, _TYPE_B})


# ---------------------------------------------------------------------------
# Exists indexer (no value type)
# ---------------------------------------------------------------------------


class TestExistsParsing:
    def test_no_value_type(self):
        r = parse_path_expression("!.items[*: int]", named_vars=False, require_value_type=False)
        assert r.value_type is None
        assert r.path == (PathKey(key="items"), VarMarker())
        assert r.var_types == (IndexerColumnType.INT,)

    def test_typed_deref_only(self):
        """Exists with typed initial and no path at all."""
        r = parse_path_expression(f"!(as {_TYPE_A:q})", named_vars=False, require_value_type=False)
        assert isinstance(r.initial_lookup, TypedDerefMarker)
        assert r.path == ()
        assert r.value_type is None

    def test_bare_deref_only(self):
        r = parse_path_expression("!", named_vars=False, require_value_type=False)
        assert isinstance(r.initial_lookup, DerefMarker)
        assert r.path == ()


# ---------------------------------------------------------------------------
# Integration with indexer parse() / query()
# ---------------------------------------------------------------------------


class TestIndexerParseIntegration:
    def test_value_indexer_parse(self):
        idx = TypedPathValueIndexer.parse("!.items[*: int].score: float")
        assert isinstance(idx.initial_lookup, DerefMarker)
        assert idx.path == (PathKey(key="items"), VarMarker(), PathKey(key="score"))
        assert idx.var_types == (IndexerColumnType.INT,)
        assert idx.value_type == IndexerColumnType.FLOAT
        assert idx.strict is False

    def test_value_indexer_parse_strict(self):
        idx = TypedPathValueIndexer.parse("!.x: int", strict=True)
        assert idx.strict is True

    def test_value_indexer_query(self):
        c = TypedPathValueIndexer.query("!.items[$i: int].score: float", "value > 3")
        assert c.key_variables == ("i",)
        assert c.condition == "value > 3"
        assert isinstance(c.indexer, TypedPathValueIndexer)

    def test_exists_indexer_parse(self):
        idx = TypedPathExistsIndexer.parse(f"!(as {_TYPE_A:q}).items[*: int]")
        assert isinstance(idx.initial_lookup, TypedDerefMarker)
        assert idx.path == (PathKey(key="items"), VarMarker())

    def test_exists_indexer_query_default_where(self):
        c = TypedPathExistsIndexer.query(f"!(as {_TYPE_A:q}).items[$i: int]")
        assert c.condition == "TRUE"
        assert c.key_variables == ("i",)


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestParserErrors:
    def test_missing_initial_bang(self):
        with pytest.raises(ValueError, match="must start with '!'"):
            parse_path_expression("name: str", named_vars=False, require_value_type=True)

    def test_missing_value_type_when_required(self):
        with pytest.raises(ValueError, match="value type"):
            parse_path_expression("!.name", named_vars=False, require_value_type=True)

    def test_unclosed_bracket(self):
        with pytest.raises(ValueError, match="expected '\\]'"):
            parse_path_expression(
                "!.items[*: int.x: str", named_vars=False, require_value_type=True
            )

    def test_invalid_col_type(self):
        with pytest.raises(ValueError, match="expected column type"):
            parse_path_expression("!.x: nope", named_vars=False, require_value_type=True)

    def test_duplicate_var_names(self):
        with pytest.raises(ValueError, match="duplicate variable name"):
            parse_path_expression(
                "!.a[$x: int].b[$x: int]: str", named_vars=True, require_value_type=True
            )

    def test_star_in_named_mode(self):
        with pytest.raises(ValueError, match="expected '\\$varname'"):
            parse_path_expression("!.a[*: int]: str", named_vars=True, require_value_type=True)

    def test_dollar_in_unnamed_mode(self):
        with pytest.raises(ValueError, match="expected '\\*'"):
            parse_path_expression("!.a[$x: int]: str", named_vars=False, require_value_type=True)

    def test_trailing_characters(self):
        with pytest.raises(ValueError, match="unexpected trailing"):
            parse_path_expression("!.x: int extra", named_vars=False, require_value_type=True)

    def test_empty_expression(self):
        with pytest.raises(ValueError, match="must start with '!'"):
            parse_path_expression("", named_vars=False, require_value_type=True)

    def test_bad_after_initial_deref(self):
        with pytest.raises(ValueError, match="expected '\\.' or end"):
            parse_path_expression("!x: int", named_vars=False, require_value_type=True)
