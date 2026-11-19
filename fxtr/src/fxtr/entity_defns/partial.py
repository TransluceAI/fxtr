"""Partial values of tagged dataclasses: a value still being produced, with fields not yet known.

A tagged dataclass (or dataclass entity) declares the fields a *finished* value has, and the
converter decodes strictly against that declaration: a field typed ``StopReason`` must hold a stop
reason. A value that is still being produced, such as a chat rollout whose model is mid-response,
has no stop reason, usage, or outcome yet. :class:`Partial` represents such a value without
loosening the class's own declaration: ``Partial[T]`` holds ``T``'s fields, any of which may be
``None``, and it is a distinct type with its own wire tag, so a partial value never decodes as a
finished ``T`` and never shares a content id with one.

**What may be partial.** In a ``Partial[T]``:

- every field of ``T`` may be ``None``, whatever its declared type;
- wherever a field's declared type admits a tagged dataclass ``D`` (``D`` itself, a union with
  ``D`` as a member, or inside the elements of a ``tuple``, ``list``, ``dict``, or ``frozendict``),
  the value there may be a ``Partial[D]`` instead, at any depth. A list grows by being shorter, so
  its elements may be partial but never ``None``;
- everything else holds what ``T`` declares, decoded with the converter's usual strictness. A
  finished ``D`` nested in a partial is decoded as ``D``, so it cannot hold partial values itself:
  partial values nest only through ``Partial``.

**Wire form.** ``{"$type": PARTIAL_TYPE_ID, "of": <T's tagged mapping>}``, where the inner mapping
carries ``T``'s own ``$type`` and every one of ``T``'s fields, unknown ones as ``null``. Decoding
``Partial[A | B]`` dispatches on the inner tag. A ``Partial`` is an untagged class implementing
the serialization-hook protocol (see :mod:`fxtr.entity_defns.dataclass_bases`), so it can annotate
a field of any tagged dataclass; it is inline only, not itself an entity.

**Finishing.** :meth:`Partial.complete` turns a partial value whose fields are all known into the
finished ``T``, completing nested partials, and decodes it as a load would, so a field left
``None`` that ``T`` does not allow to be ``None`` is an :class:`IncompletePartialError` naming its
path.

Field values are not checked when a ``Partial`` is constructed, as a tagged dataclass's are not:
they are checked when the value is decoded or completed.
"""

from __future__ import annotations

import dataclasses
import types
import typing
from collections.abc import Callable, Mapping
from typing import Any

import cattrs
from cattrs.dispatch import StructureHook, UnstructureHook
from frozendict import frozendict

from fxtr.core.entities import BoundID
from fxtr.core.entity_data import TYPE_KEY, InvalidEntityDataError, get_entity_type_tag
from fxtr.entity_defns.converter import DEFAULT_CONVERTER, FxtrConverter
from fxtr.entity_defns.dataclass_bases import ConversionError, StrictTypeError, TaggedDataclass

PARTIAL_TYPE_ID = "org.transluce.fxtr.Partial"
"""The ``$type`` of a partial value's wire form; the partial value's own type is in ``of``."""

_OF_KEY = "of"


class IncompletePartialError(ConversionError):
    """A partial value could not be completed: fields its class requires are still ``None``.

    Attributes:
        missing: The paths of the missing fields, such as ``"model_turns[1].usage"``.
    """

    def __init__(self, type_name: str, missing: tuple[str, ...]) -> None:
        super().__init__(
            f"Partial[{type_name}] is incomplete; these fields are None but required:"
            f" {', '.join(missing)}"
        )
        self.missing = missing


# ---- annotations ----


def _is_union(annotation: Any) -> bool:
    return typing.get_origin(annotation) in (types.UnionType, typing.Union)


def _tagged_class(annotation: Any) -> type[TaggedDataclass] | None:
    """The tagged dataclass an annotation names (a class or a generic alias of one), if any."""
    cls = typing.get_origin(annotation) or annotation
    if isinstance(cls, type) and issubclass(cls, TaggedDataclass):
        return cls
    return None


def _may_hold_partial(annotation: Any) -> bool:
    """Whether a slot of this type admits a ``Partial`` anywhere inside it."""
    if _tagged_class(annotation) is not None:
        return True
    origin = typing.get_origin(annotation)
    if origin in (BoundID, typing.Literal):
        return False
    return any(_may_hold_partial(arg) for arg in typing.get_args(annotation))


def _init_fields(cls: Any) -> list[dataclasses.Field[Any]]:
    """A dataclass's init fields, in declared order: the fields its wire form carries."""
    return [f for f in dataclasses.fields(cls) if f.init]


def _field_types(annotation: Any) -> dict[str, Any]:
    """The resolved types of a dataclass's init fields, with the annotation's type arguments
    substituted for the class's type parameters."""
    cls = typing.get_origin(annotation) or annotation
    hints = typing.get_type_hints(cls, include_extras=True)
    params = getattr(cls, "__parameters__", ())
    args = typing.get_args(annotation)
    mapping = dict(zip(params, args, strict=True)) if args else {}

    def substitute(t: Any) -> Any:
        if isinstance(t, typing.TypeVar):
            if t not in mapping:
                raise StrictTypeError(
                    f"Partial[{cls.__name__}] needs type arguments for {cls.__name__}'s"
                    " type parameters"
                )
            return mapping[t]
        if getattr(t, "__parameters__", ()):
            return t[tuple(substitute(p) for p in t.__parameters__)]
        return t

    return {f.name: substitute(hints[f.name]) for f in _init_fields(cls)}


def _require_partialable(annotation: Any) -> tuple[type[TaggedDataclass], str]:
    """Check that ``annotation`` names a tagged dataclass with a declared type; return the class
    and its type id."""
    cls = _tagged_class(annotation)
    if cls is None or not dataclasses.is_dataclass(cls):
        raise TypeError(f"Partial values are of tagged dataclasses; got {annotation!r}")
    return cls, cls._fxtr_require_type_id()


# ---- decoding ----


def _is_partial_data(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get(TYPE_KEY) == PARTIAL_TYPE_ID


def _lazy_structure_hook(conv: cattrs.Converter, annotation: Any) -> Callable[[Any], Any]:
    """Structure as ``annotation``, building its hook on first use (so recursive classes, whose
    hooks refer to their own, do not recurse while being built)."""
    hook: StructureHook | None = None

    def structure(value: Any) -> Any:
        nonlocal hook
        if hook is None:
            hook = conv.get_structure_hook(annotation)
        return hook(value, annotation)

    return structure


def _slot_hook(conv: cattrs.Converter, annotation: Any) -> Callable[[Any], Any]:
    """Build the decoder for one slot of a partial value: ``annotation``'s value, or a partial
    value where ``annotation`` admits a tagged dataclass (see the module docstring)."""
    if not _may_hold_partial(annotation):
        return _lazy_structure_hook(conv, annotation)
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is typing.Annotated:
        return _slot_hook(conv, args[0])

    tagged = [m for m in (args if _is_union(annotation) else (annotation,)) if _tagged_class(m)]
    if tagged:
        whole = _lazy_structure_hook(conv, annotation)
        partial_of_tagged = typing.cast(Any, Partial)[typing.Union[tuple(tagged)]]  # noqa: UP007
        partial = _lazy_structure_hook(conv, partial_of_tagged)

        def tagged_slot(value: Any) -> Any:
            return partial(value) if _is_partial_data(value) else whole(value)

        return tagged_slot

    if origin in (tuple, list):
        if origin is tuple and not (len(args) == 2 and args[1] is Ellipsis):
            positions = [_slot_hook(conv, arg) for arg in args]

            def fixed_tuple(value: Any) -> tuple[Any, ...]:
                if not isinstance(value, (list, tuple)) or len(value) != len(positions):
                    raise StrictTypeError(
                        f"Expected a sequence of {len(positions)} items for {annotation!r}"
                    )
                return tuple(hook(item) for hook, item in zip(positions, value, strict=True))

            return fixed_tuple

        element = _slot_hook(conv, args[0])
        build = tuple if origin is tuple else list

        def sequence(value: Any) -> Any:
            if not isinstance(value, (list, tuple)):
                raise StrictTypeError(
                    f"Expected a sequence for {annotation!r}, got {type(value).__name__}"
                )
            return build(element(item) for item in value)

        return sequence

    if origin in (dict, frozendict):
        key = _lazy_structure_hook(conv, args[0])
        item = _slot_hook(conv, args[1])
        build_mapping = frozendict if origin is frozendict else dict

        def mapping(value: Any) -> Any:
            if not isinstance(value, Mapping):
                raise StrictTypeError(
                    f"Expected a mapping for {annotation!r}, got {type(value).__name__}"
                )
            return build_mapping({key(k): item(v) for k, v in value.items()})

        return mapping

    # Other containers (sets, abstract sequences) hold finished values only.
    return _lazy_structure_hook(conv, annotation)


@dataclasses.dataclass(frozen=True)
class _Layout:
    """How one partial class's fields decode and encode."""

    annotation: Any
    type_id: str
    structure: Mapping[str, Callable[[Any], Any]]
    unstructure: Mapping[str, Callable[[Any], Any]]


def _layout(conv: cattrs.Converter, annotation: Any) -> _Layout:
    _, type_id = _require_partialable(annotation)
    fields = _field_types(annotation)

    def unstructure_hook(t: Any) -> Callable[[Any], Any]:
        # A field's declared hook re-dispatches on the runtime type of a tagged value (so a
        # nested Partial serializes as itself); a bare type variable has no declared hook.
        return conv.unstructure if isinstance(t, typing.TypeVar) else conv.get_unstructure_hook(t)

    return _Layout(
        annotation=annotation,
        type_id=type_id,
        structure={name: _slot_hook(conv, t) for name, t in fields.items()},
        unstructure={name: unstructure_hook(t) for name, t in fields.items()},
    )


# ---- the partial value ----


class Partial[T: TaggedDataclass]:
    """A value of the tagged dataclass ``T`` still being produced: any field may be ``None``.

    Build one with the class and whichever fields are known; a field not given takes its
    dataclass default, or ``None`` if it has none::

        rollout = Partial(ChatRollout, context_turns=turns, model_turns=(Partial(ModelTurn, ...),))
        rollout.status        # None
        rollout.complete()    # IncompletePartialError: status is required

    Fields read as attributes, except where ``Partial``'s own members (``complete``, ``replace``,
    ``from_value``, ``partial_type``, ``partial_fields``) shadow them; :attr:`partial_fields`
    always has every field. See the module docstring for what may be partial and the wire form.

    Equality and hashing are by class and fields, so a partial value equals its counterpart
    decoded from its own wire form.
    """

    __slots__ = ("_annotation", "_fields")

    _annotation: Any
    _fields: frozendict[str, Any]

    def __init__(self, partial_type: type[T], /, **fields: Any) -> None:
        cls, _ = _require_partialable(partial_type)
        declared = {f.name: f for f in _init_fields(cls)}
        unknown = sorted(fields.keys() - declared.keys())
        if unknown:
            raise TypeError(f"{cls.__name__} has no field(s) {unknown!r}")
        values: dict[str, Any] = {}
        for name, field in declared.items():
            if name in fields:
                values[name] = fields[name]
            elif field.default is not dataclasses.MISSING:
                values[name] = field.default
            elif field.default_factory is not dataclasses.MISSING:
                values[name] = field.default_factory()
            else:
                values[name] = None
        object.__setattr__(self, "_annotation", partial_type)
        object.__setattr__(self, "_fields", frozendict(values))

    @classmethod
    def _from_fields(cls, annotation: Any, fields: Mapping[str, Any]) -> Partial[Any]:
        """Build a partial value from every field of its class, already in declared order."""
        partial = cls.__new__(cls)
        object.__setattr__(partial, "_annotation", annotation)
        object.__setattr__(partial, "_fields", frozendict(fields))
        return partial

    @classmethod
    def from_value[U: TaggedDataclass](cls, value: U) -> Partial[U]:
        """The partial value with every field of a finished ``value``."""
        _require_partialable(type(value))
        names = (f.name for f in _init_fields(type(value)))
        return Partial._from_fields(type(value), {name: getattr(value, name) for name in names})

    # ---- reading ----

    @property
    def partial_type(self) -> type[T]:
        """The class (or generic alias) this is a partial value of."""
        return self._annotation

    @property
    def partial_fields(self) -> frozendict[str, Any]:
        """Every field of the class, in declared order, ``None`` where not yet known."""
        return self._fields

    def __getattr__(self, name: str) -> Any:
        # Only reached when ordinary lookup fails, so never for the slots once they are set.
        if not name.startswith("_"):
            fields = self._fields
            if name in fields:
                return fields[name]
        raise AttributeError(f"Partial value has no field {name!r}")

    def replace(self, **changes: Any) -> Partial[T]:
        """A copy with the given fields changed."""
        unknown = sorted(changes.keys() - self._fields.keys())
        if unknown:
            raise TypeError(f"{self._class_name()} has no field(s) {unknown!r}")
        return Partial._from_fields(self._annotation, {**self._fields, **changes})

    def complete(self, converter: FxtrConverter = DEFAULT_CONVERTER) -> T:
        """The finished value, with nested partial values completed too.

        Every field left ``None`` must be one the class allows to be ``None``. The result is
        decoded from its serialized form as a load would decode it, with the converter's usual
        strictness.

        Raises:
            IncompletePartialError: If any field, at any depth, is ``None`` where its class does
                not allow it.
            ConversionError: If the finished value does not decode as its class.
        """
        missing: list[str] = []
        value = _complete(self, "", missing, converter)
        if missing:
            raise IncompletePartialError(self._class_name(), tuple(missing))
        return converter.structure(converter.unstructure_entity(value), self._annotation)

    # ---- object protocol ----

    def _class_name(self) -> str:
        cls = typing.get_origin(self._annotation) or self._annotation
        return cls.__name__

    def __setattr__(self, name: str, value: Any) -> None:
        raise dataclasses.FrozenInstanceError(f"cannot assign to field {name!r}")

    def __delattr__(self, name: str) -> None:
        raise dataclasses.FrozenInstanceError(f"cannot delete field {name!r}")

    def _origin(self) -> type:
        return typing.get_origin(self._annotation) or self._annotation

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Partial):
            return NotImplemented
        return self._origin() is other._origin() and self._fields == other._fields

    def __hash__(self) -> int:
        return hash((self._origin(), self._fields))

    def __repr__(self) -> str:
        fields = ", ".join(f"{name}={value!r}" for name, value in self._fields.items())
        return f"Partial[{self._class_name()}]({fields})"

    def __reduce__(self) -> tuple[Any, ...]:
        return (Partial._from_fields, (self._annotation, dict(self._fields)))

    # ---- the serialization-hook protocol (see fxtr.entity_defns.dataclass_bases) ----
    # Partial is an untagged protocol class: its hooks produce and consume the whole wire form,
    # {"$type": PARTIAL_TYPE_ID, "of": <the class's tagged mapping, unknown fields null>}.

    @classmethod
    def __fxtr_make_structure_hook__(cls, conv: cattrs.Converter, annotation: Any) -> StructureHook:
        args = typing.get_args(annotation)
        if not args:
            raise StrictTypeError(
                "Cannot deserialize a bare Partial: name the class it is a partial value of,"
                " e.g. Partial[MyClass]"
            )
        (target,) = args
        members = typing.get_args(target) if _is_union(target) else (target,)
        layouts: dict[str, _Layout] = {}
        for member in members:
            layout = _layout(conv, member)
            if layout.type_id in layouts:
                raise TypeError(f"{annotation!r} has two members with the type {layout.type_id!r}")
            layouts[layout.type_id] = layout

        def structure(value: Any, _target: Any) -> Partial[Any]:
            if not _is_partial_data(value) or value.keys() != {TYPE_KEY, _OF_KEY}:
                raise StrictTypeError(
                    f"Expected a mapping with {TYPE_KEY!r} {PARTIAL_TYPE_ID!r} and {_OF_KEY!r}"
                    f" for {annotation!r}"
                )
            inner = value[_OF_KEY]
            try:
                tag = get_entity_type_tag(inner)
            except InvalidEntityDataError as exc:
                raise StrictTypeError(f"{annotation!r}: {exc}") from exc
            layout = layouts.get(tag)
            if layout is None:
                raise StrictTypeError(
                    f"A partial value of {tag!r} does not decode as {annotation!r}"
                )
            names = inner.keys() - {TYPE_KEY}
            if names != layout.structure.keys():
                missing = sorted(layout.structure.keys() - names)
                extra = sorted(names - layout.structure.keys())
                raise StrictTypeError(
                    f"Partial {tag!r} data has missing field(s) {missing!r} and unknown"
                    f" field(s) {extra!r}; every field is written, unknown ones as null"
                )
            fields = {
                name: None if inner[name] is None else hook(inner[name])
                for name, hook in layout.structure.items()
            }
            return Partial._from_fields(layout.annotation, fields)

        return structure

    @classmethod
    def __fxtr_make_unstructure_hook__(
        cls, conv: cattrs.Converter, annotation: Any
    ) -> UnstructureHook:
        # Serialization depends only on the runtime value, so layouts are by the value's class.
        layouts: dict[Any, _Layout] = {}

        def unstructure(value: Partial[Any]) -> dict[str, Any]:
            layout = layouts.get(value._annotation)
            if layout is None:
                layout = layouts[value._annotation] = _layout(conv, value._annotation)
            inner: dict[str, Any] = {
                name: None if field is None else layout.unstructure[name](field)
                for name, field in value._fields.items()
            }
            inner[TYPE_KEY] = layout.type_id
            return {TYPE_KEY: PARTIAL_TYPE_ID, _OF_KEY: inner}

        return unstructure


# ---- completion ----


def _admits_none(converter: FxtrConverter, annotation: Any) -> bool:
    try:
        converter.structure(None, annotation)
    except ConversionError:
        return False
    return True


def _complete(value: Any, path: str, missing: list[str], converter: FxtrConverter) -> Any:
    """Replace every partial value inside ``value`` with its finished value, recording the path
    of each field that is ``None`` where its class does not allow it."""
    match value:
        case Partial():
            fields = _field_types(value.partial_type)
            finished: dict[str, Any] = {}
            for name, field in value.partial_fields.items():
                field_path = f"{path}.{name}" if path else name
                if field is None and not _admits_none(converter, fields[name]):
                    missing.append(field_path)
                finished[name] = _complete(field, field_path, missing, converter)
            if missing:
                return None
            try:
                return value._origin()(**finished)
            except Exception as exc:
                raise ConversionError(
                    f"{path or value._class_name()}: the finished value could not be built: {exc}"
                ) from exc
        case tuple() | list():
            items = [_complete(v, f"{path}[{i}]", missing, converter) for i, v in enumerate(value)]
            return type(value)(items)
        case frozendict() | dict():
            return type(value)(
                {k: _complete(v, f"{path}[{k!r}]", missing, converter) for k, v in value.items()}
            )
        case _:
            return value
