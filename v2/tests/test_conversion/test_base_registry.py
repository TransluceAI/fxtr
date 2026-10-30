"""Tests for TypeAnnotationHandlerRegistry in fxtr2.conversion.base_registry."""

from __future__ import annotations

import typing
from typing import Annotated, Generic, NewType, TypeVar

import pytest

from fxtr2.conversion.base_registry import OverrideFxtrHandlers, TypeAnnotationHandlerRegistry

# ---------------------------------------------------------------------------
# Helper types used across tests
# ---------------------------------------------------------------------------


class Base:
    pass


class Sub(Base):
    pass


class SubSub(Sub):
    pass


class Unrelated:
    pass


T = TypeVar("T")


class GenericClass(Generic[T]):
    pass


class GenericSub(GenericClass[T]):
    pass


# ===========================================================================
# Tests
# ===========================================================================


class TestSimpleLookup:
    def test_plain_type(self):
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(Base, "handler_base")
        result = reg.find_handler(Base)
        assert result.entry.handler == "handler_base"
        assert result.subquery is Base

    def test_subclass_via_mro(self):
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(Base, "handler_base", allow_implicit_overrides=True)
        result = reg.find_handler(Sub)
        assert result.entry.handler == "handler_base"
        assert result.subquery is Sub

    def test_generic_alias_resolves_via_mro(self):
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(GenericClass, "handler_generic", allow_implicit_overrides=True)
        query = GenericClass[int]
        result = reg.find_handler(query)
        assert result.entry.handler == "handler_generic"
        assert result.subquery is query

    def test_generic_subclass_resolves_via_mro(self):
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(GenericClass, "handler_generic", allow_implicit_overrides=True)
        query = GenericSub[int]
        result = reg.find_handler(query)
        assert result.entry.handler == "handler_generic"
        # subquery is the GenericAlias GenericSub[int], not GenericClass
        assert typing.get_origin(result.subquery) is GenericSub

    def test_no_handler_raises(self):
        reg = TypeAnnotationHandlerRegistry()
        with pytest.raises(LookupError, match="No handler registered"):
            reg.find_handler(Base)

    def test_most_specific_class_wins(self):
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(Base, "handler_base", allow_implicit_overrides=True)
        reg.register_handler(Sub, "handler_sub")
        result = reg.find_handler(Sub)
        assert result.entry.handler == "handler_sub"

    def test_subsub_finds_most_specific(self):
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(Base, "handler_base", allow_implicit_overrides=True)
        reg.register_handler(Sub, "handler_sub", allow_implicit_overrides=True)
        result = reg.find_handler(SubSub)
        assert result.entry.handler == "handler_sub"
        assert result.subquery is SubSub


class TestTypeAliases:
    def test_registered_alias(self):
        type MyAlias = Base
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(MyAlias, "handler_alias")
        result = reg.find_handler(MyAlias)
        assert result.entry.handler == "handler_alias"
        assert result.subquery is MyAlias

    def test_transparent_alias_unwraps(self):
        type MyAlias = Base
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(Base, "handler_base")
        result = reg.find_handler(MyAlias)
        assert result.entry.handler == "handler_base"
        # subquery is the unwrapped type (Base), not the alias
        assert result.subquery is Base

    def test_newtype_registered(self):
        MyNew = NewType("MyNew", Base)
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(MyNew, "handler_newtype")
        result = reg.find_handler(MyNew)
        assert result.entry.handler == "handler_newtype"
        assert result.subquery is MyNew

    def test_newtype_transparent(self):
        MyNew = NewType("MyNew", Base)
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(Base, "handler_base")
        result = reg.find_handler(MyNew)
        assert result.entry.handler == "handler_base"
        assert result.subquery is Base

    def test_parameterized_alias_registered(self):
        type MyAlias[T] = GenericClass[T]
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(MyAlias, "handler_alias")
        query = MyAlias[int]
        result = reg.find_handler(query)
        assert result.entry.handler == "handler_alias"
        # subquery is MyAlias[int] (the parameterized form)
        assert result.subquery is query

    def test_parameterized_alias_transparent(self):
        type MyAlias[T] = GenericClass[T]
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(GenericClass, "handler_generic")
        query = MyAlias[int]
        result = reg.find_handler(query)
        assert result.entry.handler == "handler_generic"
        # subquery is GenericClass[int] (unwrapped through the alias)
        assert typing.get_origin(result.subquery) is GenericClass
        assert typing.get_args(result.subquery) == (int,)

    def test_chained_aliases(self):
        type Inner = Base
        type Outer = Inner
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(Base, "handler_base")
        result = reg.find_handler(Outer)
        assert result.entry.handler == "handler_base"

    def test_registered_alias_to_never_returns_alias_handler(self):
        type UninhabitedAlias = typing.Never
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(UninhabitedAlias, "handler_never_alias")
        result = reg.find_handler(UninhabitedAlias)
        assert result.entry.handler == "handler_never_alias"
        assert result.subquery is UninhabitedAlias

    def test_unregistered_alias_to_never_errors(self):
        type UninhabitedAlias = typing.Never
        reg = TypeAnnotationHandlerRegistry()
        with pytest.raises(TypeError, match="Cannot process type annotation"):
            reg.find_handler(UninhabitedAlias)


class TestOverrideRules:
    def test_base_allows_overrides_subclass_wins(self):
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(Base, "handler_base", allow_implicit_overrides=True)
        reg.register_handler(Sub, "handler_sub")
        result = reg.find_handler(Sub)
        assert result.entry.handler == "handler_sub"

    def test_base_disallows_overrides_raises(self):
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(Base, "handler_base", allow_implicit_overrides=False)
        reg.register_handler(Sub, "handler_sub")
        with pytest.raises(ValueError, match="Conflicting handlers"):
            reg.find_handler(Sub)

    def test_registered_alias_plus_inner_type_must_allow_overrides(self):
        type MyAlias = Base
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(MyAlias, "handler_alias")
        reg.register_handler(Base, "handler_base", allow_implicit_overrides=False)
        with pytest.raises(ValueError, match="Conflicting handlers"):
            reg.find_handler(MyAlias)

    def test_registered_alias_plus_inner_type_allows_overrides(self):
        type MyAlias = Base
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(MyAlias, "handler_alias")
        reg.register_handler(Base, "handler_base", allow_implicit_overrides=True)
        result = reg.find_handler(MyAlias)
        assert result.entry.handler == "handler_alias"

    def test_two_aliases_outer_wins(self):
        type Inner = Base
        type Outer = Inner
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(Outer, "handler_outer")
        reg.register_handler(Inner, "handler_inner", allow_implicit_overrides=True)
        reg.register_handler(Base, "handler_base", allow_implicit_overrides=True)
        result = reg.find_handler(Outer)
        assert result.entry.handler == "handler_outer"

    def test_duplicate_registration_raises(self):
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(Base, "handler1")
        with pytest.raises(ValueError, match="already registered"):
            reg.register_handler(Base, "handler2")


class TestOverrideFxtrHandlers:
    def test_short_circuits_past_inner_handler(self):
        type MyOverride = OverrideFxtrHandlers[Base]
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(MyOverride, "handler_override")
        reg.register_handler(Base, "handler_base")  # does NOT allow overrides
        result = reg.find_handler(MyOverride)
        assert result.entry.handler == "handler_override"

    def test_no_outer_handler_passes_through(self):
        type MyWrapper = OverrideFxtrHandlers[Base]
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(Base, "handler_base")
        # No handler for MyWrapper itself — OverrideFxtrHandlers has no effect.
        result = reg.find_handler(MyWrapper)
        assert result.entry.handler == "handler_base"

    def test_in_union_branch_only_affects_that_branch(self):
        type BranchA = OverrideFxtrHandlers[Base]
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(BranchA, "handler_branch_a")
        reg.register_handler(Base, "handler_base")  # doesn't allow overrides
        reg.register_handler(Unrelated, "handler_unrelated")

        # BranchA | Unrelated: BranchA short-circuits past Base, Unrelated resolves normally.
        results = reg.find_handler_or_union_handlers(BranchA | Unrelated)
        assert len(results) == 2
        assert results[0].entry.handler == "handler_branch_a"
        assert results[1].entry.handler == "handler_unrelated"

    def test_override_with_parameterized_inner(self):
        type MyOverride[T] = OverrideFxtrHandlers[GenericClass[T]]
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(MyOverride, "handler_override")
        reg.register_handler(GenericClass, "handler_generic")  # doesn't allow overrides
        result = reg.find_handler(MyOverride[int])
        assert result.entry.handler == "handler_override"
        # subquery is MyOverride[int]
        assert typing.get_origin(result.subquery) is MyOverride


class TestUnions:
    def test_simple_union(self):
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(Base, "handler_base")
        reg.register_handler(Unrelated, "handler_unrelated")
        results = reg.find_handler_or_union_handlers(Base | Unrelated)
        assert len(results) == 2
        assert results[0].entry.handler == "handler_base"
        assert results[1].entry.handler == "handler_unrelated"

    def test_union_missing_branch_raises(self):
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(Base, "handler_base")
        with pytest.raises(LookupError, match="No handler registered"):
            reg.find_handler_or_union_handlers(Base | Unrelated)

    def test_alias_resolving_to_union_registered(self):
        type MyUnion = Base | Unrelated
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(MyUnion, "handler_union")
        result = reg.find_handler(MyUnion)
        assert result.entry.handler == "handler_union"

    def test_alias_resolving_to_union_registered_checks_branches(self):
        type MyUnion = Base | Unrelated
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(MyUnion, "handler_union")
        reg.register_handler(Base, "handler_base")  # doesn't allow overrides
        with pytest.raises(ValueError, match="Conflicting handlers"):
            reg.find_handler(MyUnion)

    def test_alias_resolving_to_union_registered_branches_allow_overrides(self):
        type MyUnion = Base | Unrelated
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(MyUnion, "handler_union")
        reg.register_handler(Base, "handler_base", allow_implicit_overrides=True)
        result = reg.find_handler(MyUnion)
        assert result.entry.handler == "handler_union"

    def test_alias_resolving_to_union_transparent(self):
        type MyUnion = Base | Unrelated
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(Base, "handler_base")
        reg.register_handler(Unrelated, "handler_unrelated")
        results = reg.find_handler_or_union_handlers(MyUnion)
        assert len(results) == 2
        assert results[0].entry.handler == "handler_base"
        assert results[1].entry.handler == "handler_unrelated"

    def test_nested_union_flattened(self):
        """type Bar = Foo | C where type Foo = A | B, all transparent."""

        class A:
            pass

        class B:
            pass

        class C:
            pass

        type Foo = A | B
        type Bar = Foo | C
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(A, "handler_a")
        reg.register_handler(B, "handler_b")
        reg.register_handler(C, "handler_c")
        results = reg.find_handler_or_union_handlers(Bar)
        assert len(results) == 3
        assert [r.entry.handler for r in results] == ["handler_a", "handler_b", "handler_c"]

    def test_nested_union_inner_alias_registered(self):
        """type Bar = Foo | C where Foo is registered."""

        class A:
            pass

        class B:
            pass

        class C:
            pass

        type Foo = A | B
        type Bar = Foo | C
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(Foo, "handler_foo")
        reg.register_handler(C, "handler_c")
        results = reg.find_handler_or_union_handlers(Bar)
        assert len(results) == 2
        assert results[0].entry.handler == "handler_foo"
        assert results[1].entry.handler == "handler_c"
        assert results[0].subquery is Foo


class TestGenericSubstitution:
    def test_parameterized_union_alias(self):
        """type Foo[T] = A[T] | B[T], query Foo[int]."""

        class A(Generic[T]):
            pass

        class B(Generic[T]):
            pass

        type Foo[U] = A[U] | B[U]
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(A, "handler_a")
        reg.register_handler(B, "handler_b")
        results = reg.find_handler_or_union_handlers(Foo[int])
        assert len(results) == 2
        assert results[0].entry.handler == "handler_a"
        assert results[1].entry.handler == "handler_b"
        # subqueries should be A[int] and B[int]
        assert typing.get_origin(results[0].subquery) is A
        assert typing.get_args(results[0].subquery) == (int,)
        assert typing.get_origin(results[1].subquery) is B
        assert typing.get_args(results[1].subquery) == (int,)

    def test_nested_parameterized_union(self):
        """type Bar[T] = Foo[T] | C[T] where type Foo[T] = A[T] | B[T]."""

        class A(Generic[T]):
            pass

        class B(Generic[T]):
            pass

        class C(Generic[T]):
            pass

        type Foo[U] = A[U] | B[U]
        type Bar[U] = Foo[U] | C[U]
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(A, "handler_a")
        reg.register_handler(B, "handler_b")
        reg.register_handler(C, "handler_c")
        results = reg.find_handler_or_union_handlers(Bar[int])
        assert len(results) == 3
        assert [r.entry.handler for r in results] == ["handler_a", "handler_b", "handler_c"]
        # Check subqueries have correct type args
        for r in results:
            assert typing.get_args(r.subquery) == (int,)

    def test_nested_parameterized_inner_alias_registered(self):
        """type Bar[T] = Foo[T] | C[T] where Foo is registered."""

        class A(Generic[T]):
            pass

        class B(Generic[T]):
            pass

        class C(Generic[T]):
            pass

        type Foo[U] = A[U] | B[U]
        type Bar[U] = Foo[U] | C[U]
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(Foo, "handler_foo")
        reg.register_handler(C, "handler_c")
        results = reg.find_handler_or_union_handlers(Bar[int])
        assert len(results) == 2
        assert results[0].entry.handler == "handler_foo"
        assert results[1].entry.handler == "handler_c"
        # Foo's subquery should be Foo[int]
        assert typing.get_origin(results[0].subquery) is Foo
        assert typing.get_args(results[0].subquery) == (int,)

    def test_simple_parameterized_alias_substitution(self):
        """type Alias[T] = GenericClass[T], query Alias[str]."""
        type Alias[U] = GenericClass[U]
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(GenericClass, "handler_generic")
        result = reg.find_handler(Alias[str])
        assert result.entry.handler == "handler_generic"
        assert typing.get_origin(result.subquery) is GenericClass
        assert typing.get_args(result.subquery) == (str,)


class TestAnnotated:
    def test_annotated_strips_metadata(self):
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(Base, "handler_base")
        result = reg.find_handler(Annotated[Base, "some_metadata"])
        assert result.entry.handler == "handler_base"
        assert result.subquery is Base

    def test_annotated_with_generic(self):
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(GenericClass, "handler_generic")
        query = Annotated[GenericClass[int], "meta"]
        result = reg.find_handler(query)
        assert result.entry.handler == "handler_generic"
        assert typing.get_origin(result.subquery) is GenericClass


class TestFindHandler:
    def test_single_handler(self):
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(Base, "handler_base")
        result = reg.find_handler(Base)
        assert result.entry.handler == "handler_base"

    def test_union_raises(self):
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(Base, "handler_base")
        reg.register_handler(Unrelated, "handler_unrelated")
        with pytest.raises(ValueError, match="union of"):
            reg.find_handler(Base | Unrelated)


class TestSubquery:
    def test_plain_type_subquery(self):
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(Base, "h")
        result = reg.find_handler(Base)
        assert result.subquery is Base

    def test_subclass_subquery_is_subclass(self):
        """Handler on base, query subclass: subquery is the subclass."""
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(Base, "h", allow_implicit_overrides=True)
        result = reg.find_handler(Sub)
        assert result.subquery is Sub

    def test_generic_subquery(self):
        """Handler on GenericClass, query GenericClass[int]: subquery is GenericClass[int]."""
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(GenericClass, "h")
        query = GenericClass[int]
        result = reg.find_handler(query)
        assert result.subquery is query

    def test_registered_alias_subquery_is_alias(self):
        type MyAlias = Base
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(MyAlias, "h")
        result = reg.find_handler(MyAlias)
        assert result.subquery is MyAlias

    def test_transparent_alias_subquery_is_inner(self):
        type MyAlias = Base
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(Base, "h")
        result = reg.find_handler(MyAlias)
        assert result.subquery is Base

    def test_parameterized_registered_alias_subquery(self):
        type MyAlias[T] = GenericClass[T]
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(MyAlias, "h")
        query = MyAlias[int]
        result = reg.find_handler(query)
        assert result.subquery is query

    def test_parameterized_transparent_alias_subquery(self):
        """Transparent alias: subquery is the substituted inner form."""
        type MyAlias[T] = GenericClass[T]
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(GenericClass, "h")
        result = reg.find_handler(MyAlias[int])
        # subquery should be GenericClass[int], not MyAlias[int]
        assert typing.get_origin(result.subquery) is GenericClass
        assert typing.get_args(result.subquery) == (int,)

    def test_union_branch_subqueries(self):
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(Base, "h_base")
        reg.register_handler(Unrelated, "h_unrelated")
        results = reg.find_handler_or_union_handlers(Base | Unrelated)
        assert results[0].subquery is Base
        assert results[1].subquery is Unrelated

    def test_mro_subquery_with_generic(self):
        """Handler on GenericClass, query GenericSub[str]: subquery is GenericSub[str]."""
        reg = TypeAnnotationHandlerRegistry()
        reg.register_handler(GenericClass, "h", allow_implicit_overrides=True)
        query = GenericSub[str]
        result = reg.find_handler(query)
        assert typing.get_origin(result.subquery) is GenericSub
        assert typing.get_args(result.subquery) == (str,)
