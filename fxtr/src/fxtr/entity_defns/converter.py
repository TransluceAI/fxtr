"""The cattrs-backed converter between tagged dataclasses and fxtr data values.

Serialization here is maximally strict: decode rejects extra keys, coercions (``"5"`` is not an
``int``, ``bool`` is not an ``int``), missing declared fields, and mismatched or missing
``$type`` tags. JSON fields accept only JSON values in either direction. Classes customize
their serialization through the
``__fxtr_make_structure_hook__`` / ``__fxtr_make_unstructure_hook__`` protocol (see
:mod:`fxtr.entity_defns.dataclass_bases` for the contract); entity-level
customization means implementing the ``Entity`` contract directly and registering a loader
factory.

Generic type parameters and serialization:

- **Invariant.** Serialization at the entity level must not depend on generic type variables of
  the entity type: it is fully determined by the runtime value. This is enforced, not merely
  conventional: unstructuring re-dispatches on each value's runtime type rather than the declared
  slot type (see :meth:`FxtrConverter._install_class_hooks`), so a subclass or a value in a
  base-typed or bound-typed parameter slot keeps its own wire form. Deserialization may depend on
  type variables only in ways that choose among Python representations of the *same content* or
  constrain the valid content — re-serializing the result must reproduce the input data regardless
  of which annotation was used. Validating that runtime types match specific static expectations
  (e.g. resolving a union to a specific member) is one instance of this; another allowed instance,
  to enable extensibility of unions, is that values whose serialized form includes a ``$type`` key
  may deserialize to a specific runtime type when configured via a generic parameter, but
  deserialize to a fallback class (such as UnknownEntity or DynamicData) when annotated with it.
- **Recommendation.** Generic type parameters should only fill *self-describing* slots — tagged
  values (which carry ``$type``) and ``BoundID`` targets (whose stored data carries it) — so
  the data disambiguates and the annotation only narrows or validates. Parameters ranging over
  raw value shapes (e.g. ``tuple[int, ...]`` vs ``frozenset[int]``, which decode from the same
  wire form) can technically satisfy the invariant, but their canonicalization is confusing and
  easy to get wrong; avoid them. Union parameters should list the specific members the caller is
  prepared to handle — there is deliberately no "resolve whatever the stored type turns out to
  be" annotation, since stored data can always be of a type the calling code has no means of
  handling. A slot whose type is genuinely decided by the producer should be annotated
  :class:`fxtr.entity_defns.dynamic_data.DynamicData`, which holds the tagged value undecoded
  (same wire form as the concrete class) until the consumer names the type it expects.
"""

from __future__ import annotations

import contextlib
import dataclasses
import types
import typing
from collections.abc import Callable, Iterator
from typing import Any

import cattrs
from cattrs.dispatch import StructureHook, UnstructureHook
from frozendict import frozendict

from fxtr.core.entities import BoundID, NoMatchingVariantError
from fxtr.core.entity_data import (
    TYPE_KEY,
    EntityID,
    FrozenFxtrDataValue,
    FxtrDataValue,
    get_entity_type_tag,
)
from fxtr.core.json import FrozenJSONObject, FrozenJSONValue, JSONObject, JSONValue
from fxtr.core.loader_registry import DEFAULT_ENTITY_LOADER_REGISTRY, EntityLoaderRegistry
from fxtr.entity_defns.dataclass_bases import (
    FXTR_HOOK_METHOD_NAMES,
    ConversionError,
    StrictTypeError,
    TaggedDataclass,
)


def _strict_leaf_hook(expected: type) -> StructureHook:
    """Structure hook requiring the value to be exactly ``expected`` (no coercion)."""

    def hook(value: Any, _annotation: Any) -> Any:
        # bool is a subclass of int; require exact type for primitives.
        if type(value) is not expected:
            raise StrictTypeError(f"Expected {expected.__name__}, got {type(value).__name__}")
        return value

    return hook


_JSON_SCALAR_TYPES = (str, int, float, bool, type(None))


def _json_converter(
    *,
    object_only: bool,
    mappings: tuple[type, ...],
    sequences: tuple[type, ...],
    freeze: bool,
) -> Callable[[Any], Any]:
    """Build a function copying JSON data, preserving exact scalar types and rejecting all other
    Python values.

    Containers are accepted only when their exact type is in ``mappings`` / ``sequences``; the
    copy uses frozendicts and tuples if ``freeze``, else dicts and lists. ``object_only``
    requires the root (not nested values) to be a mapping.
    """
    shapes = "/".join(t.__name__ for t in (*mappings, *sequences))

    def convert(value: Any, path: str) -> Any:
        if type(value) in mappings:
            result = {}
            for key, child in value.items():
                if type(key) is not str:
                    raise StrictTypeError(f"{path}: expected str key, got {type(key).__name__}")
                result[key] = convert(child, f"{path}[{key!r}]")
            return frozendict(result) if freeze else result
        if type(value) in sequences:
            items = [convert(child, f"{path}[{i}]") for i, child in enumerate(value)]
            return tuple(items) if freeze else items
        if type(value) in _JSON_SCALAR_TYPES:
            return value
        raise StrictTypeError(
            f"{path}: expected a JSON value (scalar or {shapes}), got {type(value).__name__}"
        )

    def convert_root(value: Any) -> Any:
        if object_only and type(value) not in mappings:
            expected = "/".join(t.__name__ for t in mappings)
            raise StrictTypeError(
                f"$: expected a JSON object ({expected}), got {type(value).__name__}"
            )
        return convert(value, "$")

    return convert_root


def _is_tagged_union(annotation: Any) -> bool:
    """A union whose members are all TaggedDataclass subclasses (optionally plus None)."""
    if typing.get_origin(annotation) not in (types.UnionType, typing.Union):
        return False
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    return len(args) >= 2 and all(
        isinstance(a, type) and issubclass(a, TaggedDataclass) for a in args
    )


def _canonical_set_sort_key(item: Any) -> Any:
    """Canonical ordering for set/frozenset serialization (homogeneous, recognized types only)."""
    if isinstance(item, (str, bytes, int)):
        return item
    if isinstance(item, EntityID):
        return item.cid
    if isinstance(item, BoundID):
        return item.entity_id.cid
    raise StrictTypeError(
        f"Canonical set serialization supports str, int, bytes, ids, and BoundID; got"
        f" {type(item).__name__}"
    )


def _freeze_raw_data(val: Any) -> Any:
    """Freeze a raw data value: lists become tuples, dicts become frozendicts. Ids pass through
    unchanged."""
    match val:
        case EntityID():
            return val
        case list() | tuple():
            return tuple(_freeze_raw_data(v) for v in val)
        case dict() | frozendict():
            return frozendict({k: _freeze_raw_data(v) for k, v in val.items()})
        case None | bool() | int() | float() | str() | bytes():
            return val
        case _:
            raise StrictTypeError(f"Unsupported raw data value type: {type(val)}")


def _thaw_raw_data(val: Any) -> Any:
    """Thaw a raw data value: tuples become lists, frozendicts become dicts."""
    match val:
        case EntityID():
            return val
        case list() | tuple():
            return [_thaw_raw_data(v) for v in val]
        case dict() | frozendict():
            return {k: _thaw_raw_data(v) for k, v in val.items()}
        case None | bool() | int() | float() | str() | bytes():
            return val
        case _:
            raise StrictTypeError(f"Unsupported raw data value type: {type(val)}")


def _resolve_field_annotations(annotation: Any) -> None:
    """Resolve a dataclass's field annotations in place, before cattrs builds its hooks.

    cattrs resolves a field's annotation only when the *whole* annotation is a string, so a
    quoted name nested inside another type (``tuple["Group", ...]``) would reach it unresolved
    and serialize through an identity hook. ``typing.get_type_hints`` resolves strings at any
    depth, evaluating each annotation in its declaring module's globals (so a function-local
    class cannot refer to itself or another local class by a quoted name). Writing the results
    back into the ``Field`` objects is idempotent, and ``include_extras`` keeps ``Annotated``
    metadata such as cattrs overrides.
    """
    cls = typing.get_origin(annotation) or annotation
    if not dataclasses.is_dataclass(cls):
        return
    hints = typing.get_type_hints(cls, include_extras=True)
    for field in dataclasses.fields(cls):
        field.type = hints[field.name]


class _ResolvingCattrsConverter(cattrs.Converter):
    """cattrs' converter, resolving field annotations before its built-in dataclass hooks.

    These two factories are how cattrs handles dataclasses that no fxtr hook factory claims
    (plain nested dataclasses); fxtr's protocol-class factories resolve for themselves.
    """

    def gen_structure_attrs_fromdict(self, cl: Any) -> Any:
        _resolve_field_annotations(cl)
        return super().gen_structure_attrs_fromdict(cl)

    def gen_unstructure_attrs_fromdict(self, cl: Any) -> Any:
        _resolve_field_annotations(cl)
        return super().gen_unstructure_attrs_fromdict(cl)


def _format_validation_leaf(exc: BaseException, annotation: Any) -> str:
    """Describe one failure inside a cattrs validation group: fxtr's own errors by their message
    (which says what was expected and found), others as cattrs describes them."""
    if isinstance(exc, ConversionError):
        return str(exc)
    return cattrs.v.format_exception(exc, annotation)


@contextlib.contextmanager
def _conversion_error_boundary() -> Iterator[None]:
    """Normalize everything escaping a conversion entry point to :class:`ConversionError`.

    fxtr's own errors already subclass it and propagate as themselves; foreign failures — cattrs
    field-validation groups, hook-build errors like an undeclared tag — are wrapped with the
    original as ``__cause__`` (the message carries the cause's text so it stays greppable). A
    validation group's message also lists each failure with its path in the value
    (``Expected float, got int @ $.items[1].v``).
    """
    try:
        yield
    except ConversionError:
        raise
    except cattrs.BaseValidationError as exc:
        failures = cattrs.transform_error(exc, format_exception=_format_validation_leaf)
        details = "".join(f"\n  {failure}" for failure in failures)
        raise ConversionError(f"{type(exc).__name__}: {exc}{details}") from exc
    except Exception as exc:
        raise ConversionError(f"{type(exc).__name__}: {exc}") from exc


class FxtrConverter:
    """A strictly-configured cattrs converter for fxtr tagged dataclasses.

    Built once and never mutated afterwards. Entity target annotations (e.g. of ``BoundID``
    fields) resolve to loaders through the given :class:`EntityLoaderRegistry`; classes
    customize their own serialization via the ``__fxtr_make_structure_hook__`` /
    ``__fxtr_make_unstructure_hook__`` protocol (see
    :mod:`fxtr.entity_defns.dataclass_bases`).

    How the cattrs machinery is used: ``unstructure`` maps a Python object to its wire-shaped
    :data:`FxtrDataValue` and ``structure`` maps wire-shaped data plus a *target annotation* back
    to a Python object. Behavior is customized via hooks registered on the converter instance —
    either directly for a concrete type, or as a *hook factory*: a ``(predicate, factory)`` pair
    where, on first encountering an annotation matching the predicate, cattrs calls the factory
    to build the hook and caches it per annotation. Factories are the mechanism for anything
    generic (the annotation's type arguments are available at build time) and for anything
    class-family-wide (the predicate can match every subclass, so user classes need no
    registration). Dataclass fields recurse through the same dispatch, so every rule below
    applies uniformly at any nesting depth. The installed predicates are mutually exclusive, so
    dispatch does not depend on registration order.

    Before any dataclass's hooks are built, its field annotations are resolved as
    ``typing.get_type_hints`` resolves them, including quoted names nested inside other types
    (``tuple["Group", ...]``), with or without ``from __future__ import annotations`` (see
    :func:`_resolve_field_annotations`).

    Error policy: anything that escapes a conversion entry point is a :class:`ConversionError`
    (see its docstring) — fxtr's own strictness errors propagate as themselves, and foreign
    failures are wrapped with the original as ``__cause__``.
    """

    def __init__(self, registry: EntityLoaderRegistry) -> None:
        self._registry = registry
        # forbid_extra_keys applies to every generated dataclass hook: decoding data with fields
        # the class does not declare is an error (strictness guards against silent data loss).
        self._cattrs = _ResolvingCattrsConverter(forbid_extra_keys=True)
        self._install_leaf_hooks()
        self._install_collection_hooks()
        self._install_class_hooks()
        self._install_bound_id_hooks()

    # ---- public API ----

    def unstructure_entity(self, entity: TaggedDataclass) -> dict[str, FxtrDataValue]:
        """Serialize a tagged dataclass (entity or inline value) to its data value form.

        Raises:
            ConversionError: If the value cannot be serialized (see the class's error policy).
        """
        with _conversion_error_boundary():
            data = self._cattrs.unstructure(entity)
        assert isinstance(data, dict)
        return data

    def structure(self, data: Any, annotation: Any) -> Any:
        """Deserialize ``data`` as ``annotation`` (strictly).

        Raises:
            ConversionError: If the data does not decode as ``annotation`` (see the class's
                error policy).
        """
        with _conversion_error_boundary():
            return self._cattrs.structure(data, annotation)

    # ---- hook installation ----

    def _install_leaf_hooks(self) -> None:
        """Install hooks for leaf values: primitives, JSON values, entity ids, and raw-data fields.

        Primitives pass through unchanged in both directions (the wire type *is* the Python
        type — notably ``bytes`` stay raw bytes, never base64 strings), and structuring requires
        the exact type: cattrs' defaults coerce (``"5"`` structures as the int 5), which is
        unacceptable under content addressing because distinct wire values must not alias to one
        Python value (and vice versa on re-store).
        """
        for prim in (int, float, str, bool, bytes):
            self._cattrs.register_structure_hook(prim, _strict_leaf_hook(prim))
            self._cattrs.register_unstructure_hook(prim, lambda v: v)

        # JSON fields (see fxtr.core.json) hold JSON data only, with the same exact-type rule
        # for scalars. Saving requires the alias's own container shapes, so a round trip
        # reproduces an equal value. Loading also accepts the other shape (``EntityLoader.convert``
        # may be given frozen data) and returns the alias's shape.
        for alias, object_only, frozen in (
            (JSONValue, False, False),
            (JSONObject, True, False),
            (FrozenJSONValue, False, True),
            (FrozenJSONObject, True, True),
        ):
            structure_json = _json_converter(
                object_only=object_only,
                mappings=(dict, frozendict),
                sequences=(list, tuple),
                freeze=frozen,
            )
            self._cattrs.register_structure_hook(
                alias, lambda value, _annotation, convert=structure_json: convert(value)
            )
            self._cattrs.register_unstructure_hook(
                alias,
                _json_converter(
                    object_only=object_only,
                    mappings=(frozendict,) if frozen else (dict,),
                    sequences=(tuple,) if frozen else (list,),
                    freeze=False,
                ),
            )

        def structure_entity_id(value: Any, _annotation: Any) -> EntityID:
            if isinstance(value, EntityID):
                return value
            raise StrictTypeError(f"Expected an entity id, got {type(value).__name__}")

        self._cattrs.register_structure_hook(EntityID, structure_entity_id)
        self._cattrs.register_unstructure_hook(EntityID, lambda v: v)

        primitives = (int, float, str, bool, bytes)

        def is_primitive_union(t: Any) -> bool:
            if typing.get_origin(t) not in (types.UnionType, typing.Union):
                return False
            members = [a for a in typing.get_args(t) if a is not type(None)]
            return bool(members) and all(m in primitives for m in members)

        def primitive_union_structure_factory(annotation: Any) -> StructureHook:
            # A field typed as a union of primitives (``int | str``, say) has a self-describing
            # wire form, so structuring is membership by exact runtime type, as strict as the
            # single-primitive hooks: no coercion between members, and ``bool`` is not ``int``.
            # cattrs has no default for such unions. Unstructuring needs nothing: each value
            # passes through under its own type.
            args = typing.get_args(annotation)
            members = tuple(a for a in args if a is not type(None))
            allow_none = len(members) != len(args)

            def hook(value: Any, _annotation: Any) -> Any:
                if value is None and allow_none:
                    return None
                if type(value) in members:
                    return value
                raise StrictTypeError(
                    f"Expected one of {[m.__name__ for m in members]}, got {type(value).__name__}"
                )

            return hook

        self._cattrs.register_structure_hook_factory(
            is_primitive_union, primitive_union_structure_factory
        )

        # Fields annotated with the raw-data alias hold arbitrary wire-shaped values verbatim:
        # structure freezes, unstructure thaws back to the plain mutable form.
        self._cattrs.register_structure_hook_factory(
            lambda t: t is FrozenFxtrDataValue,
            lambda _t: lambda value, _annotation: _freeze_raw_data(value),
        )
        self._cattrs.register_unstructure_hook_factory(
            lambda t: t is FrozenFxtrDataValue, lambda _t: _thaw_raw_data
        )

        # The mutable raw-data alias decodes wire data to plain mutable form (lists/dicts) — the
        # counterpart of the frozen alias for a field that wants
        # ordinary mutable data. Only the structure direction is registered: without a mutable
        # alias hook cattrs cannot decode a ``FxtrDataValue`` slot at all (its structure hooks for
        # the frozen alias don't match it). Unstructure is deliberately left to cattrs' union
        # dispatch, which serializes each value by its runtime type — so an *erased* slot typed
        # ``FxtrDataValue`` (e.g. a generic row default) that actually holds a richer value keeps
        # that value's own wire form, consistent with serialization being runtime-value-directed.
        self._cattrs.register_structure_hook_factory(
            lambda t: t is FxtrDataValue,
            lambda _t: lambda value, _annotation: _thaw_raw_data(value),
        )

    def _install_collection_hooks(self) -> None:
        """Install hooks for the collection shapes cattrs does not already handle right.

        cattrs natively covers list/tuple/dict (and structures set/frozenset from lists); what
        needs custom handling is ``frozendict`` (unknown to cattrs) and the *unstructure* side of
        sets, whose iteration order is arbitrary — the wire form must be deterministic for
        content addressing, so sets serialize as canonically sorted lists (see
        :func:`_canonical_set_sort_key`). All of these are hook *factories* rather than
        class-keyed hooks because field annotations are generic aliases (``frozendict[str, X]``),
        which class-keyed hooks do not match. As with cattrs' own list/dict hooks, elements go
        through the hook for the declared element type, so element-level checks (such as a
        JSON field's) apply inside these collections too.
        """
        conv = self._cattrs

        def frozendict_structure_factory(annotation: Any) -> StructureHook:
            # Delegate to the dict hook for the same key/value args so element handling is
            # identical to a dict-annotated field, then freeze to keep the field hashable.
            args = typing.get_args(annotation) or (Any, Any)
            dict_hook = conv.get_structure_hook(dict[args[0], args[1]])  # type: ignore[valid-type]

            def hook(value: Any, _annotation: Any) -> frozendict:
                return frozendict(dict_hook(value, dict))

            return hook

        def element_unstructure_hook(annotation: Any) -> UnstructureHook:
            # cattrs' hook for a bare type variable (an unsubstituted generic parameter) is the
            # identity, which would hand e.g. a tagged dataclass to the codec unserialized; such
            # elements dispatch on their runtime type instead.
            if isinstance(annotation, typing.TypeVar):
                return conv.unstructure
            return conv.get_unstructure_hook(annotation)

        def frozendict_unstructure_factory(annotation: Any) -> UnstructureHook:
            args = typing.get_args(annotation)
            value_hook = element_unstructure_hook(args[1] if args else Any)

            def hook(value: frozendict) -> dict:
                return {k: value_hook(v) for k, v in value.items()}

            return hook

        conv.register_structure_hook_factory(
            lambda t: (typing.get_origin(t) or t) is frozendict, frozendict_structure_factory
        )
        conv.register_unstructure_hook_factory(
            lambda t: (typing.get_origin(t) or t) is frozendict, frozendict_unstructure_factory
        )

        def set_unstructure_factory(annotation: Any) -> UnstructureHook:
            args = typing.get_args(annotation)
            item_hook = element_unstructure_hook(args[0] if args else Any)

            def hook(value: set | frozenset) -> list:
                return [item_hook(v) for v in sorted(value, key=_canonical_set_sort_key)]

            return hook

        for set_type in (set, frozenset):
            conv.register_unstructure_hook_factory(
                lambda t, st=set_type: (typing.get_origin(t) or t) is st, set_unstructure_factory
            )

    def _install_class_hooks(self) -> None:
        """Install (de)serialization for classes implementing the serialization-hook protocol.

        One predicate covers every fxtr-converted class — anything defining the
        ``__fxtr_make_structure_hook__`` / ``__fxtr_make_unstructure_hook__`` pair (all tagged
        dataclasses do, via the defaults on ``TaggedDataclass``) — and each direction's factory
        consults its own classmethod, branching on the class kind:

        - **Tagged classes**: the wire form is the class's content hooks plus the reserved
          ``$type`` key holding the class-declared type id. Because the tag comes from the class
          declaration (not instance state), it is spliced in *around* the content hooks:
          appended after unstructuring, and validated + stripped before structuring — so a class
          controls its field format via the protocol, never its tag mechanics. Both factories
          call ``_fxtr_require_type_id`` at hook-build time, so a class without a declared type
          (an abstract base, or a subclass that didn't re-declare) fails fast rather than
          mis-serializing.
        - **Untagged protocol classes** (e.g. custom leaf value types): the content hooks are
          the whole wire form and are used directly.

        Unstructuring re-dispatches on the value's runtime type. cattrs bakes each field's
        *declared*-type hook and does not dispatch on the value on its own, so both factories'
        unstructure hooks check whether the value's runtime type is exactly the annotation's
        class; if not (a subclass, or a value in a base-typed or defaulted-type-parameter slot)
        they route the value through its own type's hook. This keeps serialization a pure
        function of the runtime value — never of the annotation — so a value always keeps its own
        wire form and ``$type``. (A tagged slot additionally validates that the result is a
        mapping, catching a non-tagged value early; untagged leaves have no fixed shape to check.)

        Inline tagged *unions* get their own dispatch hook, keyed on the value's ``$type`` tag.
        """
        conv = self._cattrs

        def is_protocol_class(t: Any) -> bool:
            # Matching the origin makes generic aliases (MyEntity[Foo]) hit this factory too;
            # the class's hooks receive the full annotation and substitute type parameters.
            # Matching when *either* protocol method exists makes a half-defined class fail
            # loudly at hook-build time (via require_both_hook_methods) instead of silently
            # serializing without being able to deserialize (or vice versa).
            cls = typing.get_origin(t) or t
            return isinstance(cls, type) and any(
                hasattr(cls, name) for name in FXTR_HOOK_METHOD_NAMES
            )

        def require_both_hook_methods(cls: type) -> None:
            if not all(hasattr(cls, name) for name in FXTR_HOOK_METHOD_NAMES):
                raise TypeError(
                    f"{cls.__name__} must define both __fxtr_make_structure_hook__ and"
                    " __fxtr_make_unstructure_hook__"
                )

        def structure_factory(annotation: Any) -> StructureHook:
            cls = typing.get_origin(annotation) or annotation
            require_both_hook_methods(cls)
            _resolve_field_annotations(cls)
            if not issubclass(cls, TaggedDataclass):
                return cls.__fxtr_make_structure_hook__(conv, annotation)
            # Require the tag before building content hooks, so an abstract class (no declared
            # type, possibly not even a dataclass) fails with the informative error.
            declared = cls._fxtr_require_type_id()
            content_hook = cls.__fxtr_make_structure_hook__(conv, annotation)

            def hook(value: Any, target: Any) -> Any:
                tag = get_entity_type_tag(value)
                if tag != declared:
                    raise StrictTypeError(
                        f"{TYPE_KEY!r} tag {tag!r} does not match {cls.__name__}'s declared"
                        f" type {declared!r}"
                    )
                remaining = {k: v for k, v in value.items() if k != TYPE_KEY}
                return content_hook(remaining, target)

            return hook

        def unstructure_factory(annotation: Any) -> UnstructureHook:
            # Serialization is a pure function of the runtime value, never of the annotation (a
            # field's declared type, a substituted type parameter, or a base class). cattrs bakes
            # the *declared* type's hook into each field and does not runtime-dispatch on its own,
            # so a value whose runtime type is not exactly `cls` — a subclass, or a value sitting
            # in a base-typed or defaulted-type-parameter slot — would otherwise be serialized
            # under `cls`'s hook. Both protocol kinds re-dispatch on the value's own type so it
            # keeps its own wire form; the kinds differ only in what they can validate afterwards.
            cls = typing.get_origin(annotation) or annotation
            require_both_hook_methods(cls)
            _resolve_field_annotations(cls)

            if not issubclass(cls, TaggedDataclass):
                # Untagged custom leaf value types produce their whole wire form, which has no
                # fixed shape (an Angle serializes to a bare float), so there is nothing to
                # validate after a re-dispatch — a mis-typed value simply serializes as itself and
                # is caught on load.
                content_hook = cls.__fxtr_make_unstructure_hook__(conv, annotation)

                def leaf_hook(value: Any) -> Any:
                    if type(value) is not cls:
                        return conv.unstructure(value)
                    return content_hook(value)

                return leaf_hook

            # Require the tag before building content hooks, so an abstract class (no declared
            # type) fails with the informative error.
            declared = cls._fxtr_require_type_id()
            content_hook = cls.__fxtr_make_unstructure_hook__(conv, annotation)

            def hook(value: Any) -> dict[str, Any]:
                if type(value) is not cls:
                    # A tagged value is always a mapping, so a non-dict result means a non-tagged
                    # value landed in a tagged slot — catch it here rather than on load.
                    result = conv.unstructure(value)
                    if not isinstance(result, dict):
                        raise StrictTypeError(
                            f"A value in a {cls.__name__} slot has runtime type"
                            f" {type(value).__name__}, which is not a serializable tagged value"
                        )
                    return result
                # Emitting the declared type id keeps re-serialization of a loaded entity
                # byte-identical (the tag never depends on where the instance came from).
                data = content_hook(value)
                if TYPE_KEY in data:
                    raise StrictTypeError(
                        f"{cls.__name__}'s content hooks emitted {TYPE_KEY!r}; the tag is"
                        " framework-managed and content hooks must not produce it"
                    )
                return {**data, TYPE_KEY: declared}

            return hook

        conv.register_structure_hook_factory(is_protocol_class, structure_factory)
        conv.register_unstructure_hook_factory(is_protocol_class, unstructure_factory)

        def union_structure_factory(annotation: Any) -> StructureHook:
            # Inline tagged unions: a field annotated `A | B` (all members TaggedDataclass,
            # optionally plus None) dispatches on the value's $type tag. Only structuring needs
            # a hook — unstructuring dispatches on the runtime class. Unions that mix in
            # non-tagged members fall through to cattrs' default union handling (see
            # _is_tagged_union), and dispatch over *entity* targets belongs to the loader
            # registry / UnionEntityLoader, not here.
            members = [a for a in typing.get_args(annotation) if a is not type(None)]
            allow_none = len(members) != len(typing.get_args(annotation))
            by_type: dict[str, type[TaggedDataclass]] = {}
            for member in members:
                # Requiring distinct tags at hook-build time is what makes dispatch
                # deterministic; a duplicate would silently shadow one member.
                declared = member._fxtr_require_type_id()
                if declared in by_type:
                    raise TypeError(
                        f"Tagged union {annotation!r} has two members with the same type tag:"
                        f" {by_type[declared].__name__} and {member.__name__}"
                    )
                by_type[declared] = member

            def hook(value: Any, _annotation: Any) -> Any:
                if value is None and allow_none:
                    return None
                member = by_type.get(get_entity_type_tag(value))
                if member is None:
                    raise NoMatchingVariantError(
                        f"{TYPE_KEY!r} tag {get_entity_type_tag(value)!r} matches no member of"
                        f" {annotation!r}"
                    )
                # Delegate to the member's own hook, which re-validates the tag and applies
                # the member's field handling.
                return conv.structure(value, member)

            return hook

        conv.register_structure_hook_factory(_is_tagged_union, union_structure_factory)

    def _install_bound_id_hooks(self) -> None:
        """Install (de)serialization for ``BoundID[T]`` fields.

        On the wire a reference is just its id (loaders are runtime objects, never serialized).
        Structuring reattaches a loader resolved from the field's *target annotation* ``T``
        through the loader registry — this is where the containing class's annotation determines
        how a nested entity will load, and it requires ``T``'s loader factory to be registered
        by the time the field is first structured (registration convention: at import time,
        alongside the type). Resolution happens once per distinct annotation (at hook-build
        time) and the registry caches loaders, so refs built from equal annotations share a
        loader and compare equal.
        """
        conv = self._cattrs

        def is_bound_id(t: Any) -> bool:
            # Matches bare `BoundID` too, so the factory can reject it informatively instead
            # of cattrs falling back to confusing default dataclass handling.
            return t is BoundID or typing.get_origin(t) is BoundID

        def structure_factory(annotation: Any) -> StructureHook:
            # A loader can only be resolved from a concrete target type: reject the shapes that
            # carry none. These typically arise from a generic entity loaded without type
            # parameters.
            if annotation is BoundID:
                raise StrictTypeError(
                    "Cannot deserialize a bare BoundID field: a concrete target type is"
                    " needed to resolve its loader, e.g. BoundID[MyEntity]."
                )
            (target,) = typing.get_args(annotation)
            if target is Any:
                raise StrictTypeError(
                    "Cannot deserialize a BoundID[Any] field: a concrete target type is"
                    " needed to resolve its loader. Use BoundID[UnknownEntity] to load"
                    " arbitrary entity data, or a plain EntityID field to pass around an id"
                    " without loading it."
                )
            loader = self._registry.loader_for(target)

            def hook(value: Any, _annotation: Any) -> BoundID[Any]:
                entity_id = self._cattrs.structure(value, EntityID)
                return BoundID(entity_id=entity_id, loader=loader)

            return hook

        def unstructure_factory(annotation: Any) -> UnstructureHook:
            def hook(ref: BoundID[Any]) -> EntityID:
                return ref.entity_id

            return hook

        conv.register_structure_hook_factory(is_bound_id, structure_factory)
        conv.register_unstructure_hook_factory(
            lambda t: (typing.get_origin(t) or t) is BoundID, unstructure_factory
        )


DEFAULT_CONVERTER = FxtrConverter(DEFAULT_ENTITY_LOADER_REGISTRY)
"""The shared default converter, resolving entity annotations against the default registry."""
