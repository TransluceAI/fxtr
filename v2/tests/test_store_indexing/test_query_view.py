"""Tests for QueryView validation."""

import pytest

from fxtr2.data.store_indexing.indexing_types import IndexerConstraint, QueryView


def _make_constraint(*key_vars: str) -> IndexerConstraint:
    """Create a dummy IndexerConstraint with the given key variables.

    Uses a None indexer — only used for QueryView validation which doesn't
    inspect the indexer itself.
    """
    return IndexerConstraint(
        indexer=None,  # type: ignore[arg-type]
        key_variables=tuple(key_vars),
        condition="TRUE",
    )


class TestQueryViewConstruction:
    def test_basic(self):
        view = QueryView("prefix/", score=_make_constraint("t", "m"))
        assert view.prefix == "prefix/"
        assert "score" in view.columns

    def test_default_prefix(self):
        view = QueryView(score=_make_constraint("t"))
        assert view.prefix == ""

    def test_multiple_columns(self):
        view = QueryView(
            "p/",
            score=_make_constraint("t", "m"),
            source=_make_constraint("t"),
        )
        assert set(view.columns.keys()) == {"score", "source"}

    def test_repr(self):
        view = QueryView("p/", score=_make_constraint("t"), name=_make_constraint("t"))
        r = repr(view)
        assert "QueryView" in r
        assert "p/" in r


class TestQueryViewValidation:
    def test_empty_columns_rejected(self):
        with pytest.raises(ValueError, match="at least one column"):
            QueryView("prefix/")

    def test_reserved_name_pin_name(self):
        with pytest.raises(ValueError, match="reserved names"):
            QueryView(pin_name=_make_constraint("t"))

    def test_reserved_name_entity_id(self):
        with pytest.raises(ValueError, match="reserved names"):
            QueryView(entity_id=_make_constraint("t"))

    def test_binding_var_collides_with_column_name(self):
        # Column "score" and binding variable "score" collide
        with pytest.raises(ValueError, match="conflict with column names"):
            QueryView(score=_make_constraint("score"))

    def test_binding_var_collides_with_column_name_cross_constraint(self):
        # Binding var "x" from one constraint collides with column name "x"
        with pytest.raises(ValueError, match="conflict with column names"):
            QueryView(x=_make_constraint("t"), other=_make_constraint("x"))

    def test_binding_var_collides_with_reserved_name(self):
        with pytest.raises(ValueError, match="reserved names"):
            QueryView(score=_make_constraint("pin_name"))

    def test_binding_var_collides_with_reserved_entity_id(self):
        with pytest.raises(ValueError, match="reserved names"):
            QueryView(score=_make_constraint("entity_id"))

    def test_no_collision_shared_binding_vars(self):
        # Same binding var in multiple constraints is fine (unification)
        view = QueryView(
            score=_make_constraint("t", "m"),
            source=_make_constraint("t"),
        )
        assert view.columns["score"].key_variables == ("t", "m")
        assert view.columns["source"].key_variables == ("t",)

    def test_no_collision_disjoint_binding_vars(self):
        view = QueryView(
            score=_make_constraint("a", "b"),
            source=_make_constraint("c"),
        )
        assert set(view.columns.keys()) == {"score", "source"}
