"""Common logic for conversion registries."""

from __future__ import annotations

import dataclasses
import types
import typing
from typing import Annotated, Any, NewType, TypeAliasType, TypeVar, Union

type RegistryKey[T] = type[T] | TypeAliasType | NewType
"""The type of a _type registry key_ for a Python type or type annotation."""


type RegistryQueryAnnotation[T] = type[
    T
] | TypeAliasType | NewType | types.UnionType | types.GenericAlias
"""The type of a type annotation that is a subtype of T, e.g. `FooEnt | BarEnt[Baz]`.

A value of type RegistryQueryAnnotation[T] can be looked up in a type registry to find a handler
that expects or returns values of type T.

Note that the Python typechecker cannot guarantee that a given value can be looked up at runtime
in the registry. Annotating a value as RegistryQueryAnnotation[T] implies that the caller promises
the given value is a type annotation that can be looked up in the relevant registry.
"""

type OverrideFxtrHandlers[T] = T
"""A sentinel annotation which tells fxtr's type handler registry to ignore existing handlers.

OverrideFxtrHandlers is a special annotation that can be used to override the deserialization
behavior of a contained type, even if that type has existing handlers that do not opt in to
automatic override behavior. In particular, you can declare a new type annotation as something
like

type MyOverrideType = OverrideFxtrHandlers[SomeInternalType]

and then register your own handler for `MyOverrideType`. When fxtr walks a type annotation and
encounters `MyOverrideType`, it will find the handler for `MyOverrideType`, and then stop processing
the type when it encounters `OverrideFxtrHandlers`. This means that the deserialization logic for
`MyOverrideType` will always be used, and no handlers for `SomeInternalType` will be considered.
"""


def substitute_typevars(annotation: Any, subst_map: dict[Any, Any]) -> Any:
    """Substitute TypeVars in a type annotation tree.

    Walks the annotation, replacing TypeVar instances found in subst_map. Does not
    recurse into TypeAliasType.__value__ — that unwrapping is done by the walk itself.
    """
    if not subst_map:
        return annotation

    if isinstance(annotation, TypeVar):
        return subst_map.get(annotation, annotation)

    origin = typing.get_origin(annotation)
    if origin is None:
        # Plain type, bare TypeAliasType, bare NewType, or other non-generic: return as-is.
        return annotation

    args = typing.get_args(annotation)
    if not args:
        return annotation

    if origin is Annotated:
        # Only substitute in the first arg (the type), not metadata.
        new_first = substitute_typevars(args[0], subst_map)
        if new_first is args[0]:
            return annotation
        return Annotated[(new_first, *args[1:])]

    new_args = tuple(substitute_typevars(a, subst_map) for a in args)
    if new_args == args:
        return annotation

    # Rebuild the generic form.
    if origin is types.UnionType:
        result = new_args[0]
        for a in new_args[1:]:
            result = result | a
        return result

    if origin is Union:
        return Union[new_args]

    if len(new_args) == 1:
        return origin[new_args[0]]
    return origin[new_args]


@dataclasses.dataclass(frozen=True)
class RegistryEntry[T, V]:
    """A registry entry for a type annotation handler.

    Attributes:
        key: The registry key that determines what type annotations this handler supports.
        handler: The handler to use for it.
        allow_overrides: Whether to allow this handler to be overridden by a more specific handler.
            If a handler is registered for a class, then it is an error to also register a handler
            for a subclass of that class, unless the base class's handler has allow_overrides=True.
    """

    key: RegistryKey[T]
    handler: V
    allow_implicit_overrides: bool = False


@dataclasses.dataclass(frozen=True)
class MatchedHandler[K, V]:
    """A handler that was matched to a component of a query.

    MatchedHandler can be used to identify the specific type that matched a handler under the
    resolution rules, with type parameters and subclasses bound appropriately if applicable. For
    instance, if we have a hierarchy

    ```
    class Base:
        pass
    class SubclassOne[T](Base):
        pass
    class SubclassTwo(Base):
        pass

    type Foo[T] = SubclassOne[T]
    type Bar = Foo[int] | SubclassTwo
    type Baz = Foo[str]
    ```

    and we register a handler for Base, then looking up a handler for `Bar` would match this handler
    to either `SubclassOne[int]` or `SubclassTwo`. If we registered a handler for `Foo`, then
    looking up a handler for `Baz` would match this handler to `Foo[int]`.

    Attributes:
        entry: The registry entry that matched the query.
        subquery: The part of the query that matched the handler. For handlers registered to Python
            types, `subquery` will either be the registered type itself, some subclass of the
            registered type, or a GenericAlias object that binds type parameter values to the
            class/subclass. For handlers registered to type annotations, `subquery` will be either
            the type annotation itself or a GenericAlias object pointing at the registered type
            annotation.
    """

    entry: RegistryEntry[K, V]
    subquery: RegistryQueryAnnotation[K]


class TypeAnnotationHandlerRegistry[K, V]:
    """A registry of type-annotation-based handlers.

    A TypeAnnotationHandlerRegistry associates type annotations with handlers that can convert
    values to or from the Python type described by the annotation. There are two types of entry:

    - Registry entries that handle values of a concrete Python type or their subclasses,
    - Registry entries that handle objects annotated with a runtime-introspectable type alias,
      including PEP 695-style `type Foo = ...` aliases (TypeAliasType) and also NewType objects.

    To find a handler for a given type annotation, we unwrap generic aliases, type aliases, and
    Annotated wrappers until we get to a plain Python type, and then walk the MRO of the type to
    find all base classes, collecting registered handlers as we go. We then find the most specific
    handler, and check whether all less-specific handlers have allow_implicit_overrides=True. If so,
    we return the most specific handler. Otherwise, we raise an error.

    It is possible to explicitly override the deserialization behavior of a type by using a
    combination of (a) a type annotation and (b) the OverrideFxtrHandlers sentinel annotation, even
    if the internal type has an existing handler that does not opt in to allowing overrides.

    The handler registry has automatic support for unions, and will try to find a handler for each
    branch of a union.
    """

    def __init__(self) -> None:
        self._handlers: dict[RegistryKey[K], RegistryEntry[K, V]] = {}
        self._cache: dict[Any, list[MatchedHandler[K, V]]] = {}

    def register_handler(
        self, key: RegistryKey[K], handler: V, allow_implicit_overrides: bool = False
    ) -> None:
        """Register a handler for the given key.

        Args:
            key: The key to register the handler for.
            handler: The handler to register.
            allow_implicit_overrides: Whether to allow this handler to be overridden by a more specific handler.
                In this case the given handler acts like a "default implementation" for types that do
                not have more specific handlers. If False (the default), it is an error for two handlers
                to overlap (e.g. to define a handler for both a base class and a subclass). Note that
                including OverrideFxtrHandlers in a type annotation will always override any existing handlers.
        """
        if key in self._handlers:
            raise ValueError(f"Handler already registered for key {key!r}")

        if isinstance(key, (type, TypeAliasType, NewType)):
            self._handlers[key] = RegistryEntry(key, handler, allow_implicit_overrides)
        else:
            raise TypeError(f"Invalid key {key!r} for type annotation handler registry")

        self._cache.clear()

    def find_handler_or_union_handlers(
        self, query: RegistryQueryAnnotation[K]
    ) -> list[MatchedHandler[K, V]]:
        """Find a handler or collection of union-variant handlers for the given query.

        If the query is a union type, we will walk all union branches and collect handlers for each
        branch (assumed to be disjoint). If not, we will return a single handler if we find it.
        Only the most specific handler for any given branch will be returned.

        Args:
            query: The type annotation to find handlers for.

        Returns:
            A list of handlers that each handle one union branch of the query. Always nonempty.

        Raises:
            LookupError: If no handler is found for the query or any required union branch.
            ValueError: If conflicting handlers are found (override rules violated).
            TypeError: If the annotation cannot be resolved to a recognized form.
        """
        cached = self._cache.get(query)
        if cached is not None:
            return cached
        result = self._walk(query, query, require_handlers=True, original_query=query)
        self._cache[query] = result
        return result

    def _walk(
        self,
        annotation: Any,
        subquery: Any,
        *,
        require_handlers: bool,
        original_query: Any,
    ) -> list[MatchedHandler[K, V]]:
        """Walk a type annotation and return matched handlers.

        In normal mode (require_handlers=True), returns the best handler(s) — one for
        non-unions, one per branch for unions. Raises LookupError if no handler found.

        In conflict-check mode (require_handlers=False), verifies all found handlers have
        allow_implicit_overrides=True. Returns empty list. Raises ValueError if any found
        handler doesn't allow overrides.
        """
        # The best match found so far during unwrapping (only used in normal mode).
        best_match: MatchedHandler[K, V] | None = None

        current = annotation
        sq = subquery

        while True:
            # --- Bare TypeAliasType or NewType ---
            if isinstance(current, (TypeAliasType, NewType)):
                if current in self._handlers:
                    entry = self._handlers[current]
                    if require_handlers:
                        if best_match is not None:
                            if not entry.allow_implicit_overrides:
                                raise ValueError(
                                    f"Conflicting handlers for {original_query!r}: handler"
                                    f" for {best_match.entry.key!r} would override handler"
                                    f" for {entry.key!r}, but {entry.key!r} does not have"
                                    " allow_implicit_overrides=True."
                                )
                        else:
                            best_match = MatchedHandler(entry, sq)

                    else:
                        if not entry.allow_implicit_overrides:
                            raise ValueError(
                                f"Conflicting handlers for {original_query!r}: an outer"
                                f" handler would override handler for {entry.key!r}, but"
                                f" {entry.key!r} does not have"
                                " allow_implicit_overrides=True."
                            )

                # Unwrap
                if isinstance(current, TypeAliasType):
                    inner = current.__value__
                else:
                    inner = current.__supertype__
                current = inner
                sq = inner
                continue

            # --- Generic / special forms ---
            origin = typing.get_origin(current)

            if origin is not None:
                args = typing.get_args(current)

                # Annotated[X, ...] -> strip metadata
                if origin is Annotated:
                    current = args[0]
                    sq = args[0]
                    continue

                # OverrideFxtrHandlers[X] -> short-circuit or pass-through
                if isinstance(origin, TypeAliasType) and origin is OverrideFxtrHandlers:
                    if require_handlers and best_match is not None:
                        return [best_match]
                    if not require_handlers:
                        # Conflict-check mode: override sentinel stops checking.
                        return []
                    # Normal mode, no handler yet: unwrap through.
                    current = args[0]
                    sq = args[0]
                    continue

                # Union (types.UnionType or typing.Union)
                if origin is types.UnionType or origin is Union:
                    branches = args
                    if best_match is not None:
                        # Already have a handler — check branches for conflicts only.
                        for branch in branches:
                            self._walk(
                                branch,
                                branch,
                                require_handlers=False,
                                original_query=original_query,
                            )
                        return [best_match]
                    elif require_handlers:
                        # Collect handlers from all branches.
                        all_handlers: list[MatchedHandler[K, V]] = []
                        for branch in branches:
                            result = self._walk(
                                branch,
                                branch,
                                require_handlers=True,
                                original_query=original_query,
                            )
                            all_handlers.extend(result)
                        return all_handlers
                    else:
                        # Conflict-check: check all branches.
                        for branch in branches:
                            self._walk(
                                branch,
                                branch,
                                require_handlers=False,
                                original_query=original_query,
                            )
                        return []

                # GenericAlias over TypeAliasType (e.g. Foo[int])
                if isinstance(origin, TypeAliasType):
                    if origin in self._handlers:
                        entry = self._handlers[origin]
                        if require_handlers:
                            if best_match is not None:
                                if not entry.allow_implicit_overrides:
                                    raise ValueError(
                                        f"Conflicting handlers for {original_query!r}:"
                                        f" handler for {best_match.entry.key!r} would"
                                        f" override handler for {entry.key!r}, but"
                                        f" {entry.key!r} does not have"
                                        " allow_implicit_overrides=True."
                                    )
                            else:
                                best_match = MatchedHandler(entry, sq)

                        else:
                            if not entry.allow_implicit_overrides:
                                raise ValueError(
                                    f"Conflicting handlers for {original_query!r}: an"
                                    " outer handler would override handler for"
                                    f" {entry.key!r}, but {entry.key!r} does not have"
                                    " allow_implicit_overrides=True."
                                )

                    # Unwrap with type parameter substitution.
                    type_params = origin.__type_params__
                    subst_map = dict(zip(type_params, args))
                    inner = substitute_typevars(origin.__value__, subst_map)
                    current = inner
                    sq = inner
                    continue

                # GenericAlias over a plain type (e.g. MyClass[int])
                if isinstance(origin, type):
                    # sq stays as the GenericAlias for MRO walk.
                    return self._finish_with_type(
                        origin, sq, best_match, require_handlers, original_query
                    )

                raise TypeError(
                    f"Cannot process type annotation {original_query!r}: reached"
                    f" generic alias {current!r} whose origin {origin!r}"
                    f" (type: {type(origin).__name__}) is not a recognized form."
                )

            # --- Plain type ---
            if isinstance(current, type):
                return self._finish_with_type(
                    current, sq, best_match, require_handlers, original_query
                )

            # ``typing.Never`` is uninhabited and not a plain ``type``, so MRO dispatch
            # does not apply. Alias / substitution steps may still land here; if a
            # handler was registered on an outer ``TypeAliasType``, that match wins.
            if current is typing.Never:
                if best_match is not None:
                    return [best_match] if require_handlers else []
                raise TypeError(
                    f"Cannot process type annotation {original_query!r}: reached"
                    f" typing.Never without a handler registered on an enclosing alias."
                )

            raise TypeError(
                f"Cannot process type annotation {original_query!r}: reached"
                f" {current!r} (type: {type(current).__name__}) which is not a"
                " recognized type form (type, TypeAliasType, NewType, GenericAlias,"
                " Annotated, Union, or OverrideFxtrHandlers)."
            )

    def _finish_with_type(
        self,
        cls: type[K],
        subquery: Any,
        best_match: MatchedHandler[K, V] | None,
        require_handlers: bool,
        original_query: Any,
    ) -> list[MatchedHandler[K, V]]:
        """Finish a walk by checking the MRO of a plain type."""
        mro_handlers: list[MatchedHandler[K, V]] = []
        for base in cls.__mro__:
            if base in self._handlers:
                mro_handlers.append(MatchedHandler(self._handlers[base], subquery))

        if require_handlers:
            all_handlers = ([best_match] if best_match else []) + mro_handlers
            if not all_handlers:
                raise LookupError(
                    f"No handler registered for {original_query!r} (resolved to"
                    f" {cls!r}, MRO: {[c.__name__ for c in cls.__mro__]})."
                )
            for h in all_handlers[1:]:
                if not h.entry.allow_implicit_overrides:
                    raise ValueError(
                        f"Conflicting handlers for {original_query!r}: handler for"
                        f" {all_handlers[0].entry.key!r} would override handler for"
                        f" {h.entry.key!r}, but {h.entry.key!r} does not have"
                        " allow_implicit_overrides=True."
                    )
            return [all_handlers[0]]
        else:
            all_handlers = ([best_match] if best_match else []) + mro_handlers
            for h in all_handlers:
                if not h.entry.allow_implicit_overrides:
                    raise ValueError(
                        f"Conflicting handlers for {original_query!r}: an outer handler"
                        f" would override handler for {h.entry.key!r}, but"
                        f" {h.entry.key!r} does not have allow_implicit_overrides=True."
                    )
            return []

    def find_handler(self, query: RegistryQueryAnnotation[K]) -> MatchedHandler[K, V]:
        """Find a handler for the given key.

        Args:
            query: The query to find a handler for.

        Returns:
            The handler for the given query.
        """
        handlers = self.find_handler_or_union_handlers(query)
        if len(handlers) > 1:
            match_summaries = []
            for handler in handlers:
                match_summaries.append(
                    f"- {handler.subquery!r} -> {handler.entry.key!r} via {handler.entry.handler!r}"
                )
            raise ValueError(
                f"Expected to find a single handler for query {query!r} but found a union of"
                f" possible handlers:\n{'\n'.join(match_summaries)}"
            )
        return handlers[0]
