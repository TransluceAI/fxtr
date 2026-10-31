"""Declaring array types in the annotations of a step or workflow body.

A step or workflow needs a :class:`~fxtr.experiment.signatures.Signature`: the
:class:`~fxtr.entity_defns.array.ArrayType` of each argument and of the result. Those types can be
written in the decorator (``@step(inputs={...}, output=...)``) or read off the body's annotations,
which keeps each argument's type next to the argument:

* The **value schema** comes from the Python type, through
  :func:`~fxtr.entity_defns.schema.schema_from_annotation`: the leaf types, ``Sequence[X]`` /
  ``tuple[X, ...]`` / ``list[X]``, ``X | None``, ``EntityID``, ``BoundID[T]``, TypedDicts. For an
  ``Array[X]`` or ``ArrayHandle[X]`` argument it is ``X``'s schema. Two extra forms are read at
  the top level of an argument or result only, matching the calling conventions of
  :meth:`~fxtr.experiment.steps.Step.invoke`: a bare entity class ``T`` means ``id_of(T)`` (the
  argument arrives loaded), and ``T | None`` means ``optional(id_of(T))``.
* The **dims** come from a bare string in :data:`~typing.Annotated`, only on an ``Array`` or
  ``ArrayHandle``: ``Annotated[ArrayHandle[BoundID[Bio]], "[bio: str]"]``. These are the only
  annotations whose string metadata fxtr reads, so a string meant for some other tool is never
  mistaken for dims. A value (any other annotation) has no dims; a bare ``Array[X]`` or
  ``ArrayHandle[X]`` declares its schema but leaves its dims unknown.

A schema no Python type expresses (an id of a type that has no class, say) is declared in
the decorator's ``inputs=`` / ``output=`` instead.

Annotations either *declare* the signature (when the decorator gives no ``inputs``/``output``,
in which case every argument and the result must end up with a complete type) or are *checked*
against the signature the decorator gives. The check follows function subtyping: dims, when
annotated, must match exactly (they were spelled out); for an input the declared schema must be
assignable to the annotated one (``model: EntityID`` accepts a declared ``id[ModelConfig]``); for
the result the annotated schema must be assignable to the declared one (``-> str`` fits a declared
``str?``). ``Any``, ``object``, or no annotation declares nothing.
:func:`~fxtr.experiment.signatures.resolve_signature` applies those rules; this module only reads
the annotations.
"""

from __future__ import annotations

import dataclasses
import inspect
import types
import typing
from typing import Annotated, Any, TypeVar

from fxtr.core.entities import Entity
from fxtr.entity_defns.array import (
    Array,
    ArrayType,
    ArrayTypeError,
    Dims,
    parse_dims,
)
from fxtr.entity_defns.schema import (
    FrozenValue,
    NullableSchema,
    Schema,
    SchemaError,
    expand_type_alias,
    id_of,
    schema_from_annotation,
)
from fxtr.experiment.handles import ArrayHandle


@dataclasses.dataclass(frozen=True)
class AnnotatedType:
    """What a body's annotation says about one argument's (or the result's) array type.

    Either part may be unknown: a value annotation knows its dims are ``()`` but a bare
    ``Array[X]`` does not know its dims, and an ``Array[Any]`` knows neither.

    Attributes:
        dims: The dims, or ``None`` when the annotation does not say.
        schema: The value schema, or ``None`` when the annotation does not say.
    """

    dims: Dims | None
    schema: Schema | None

    def complete(self, where: str) -> ArrayType:
        """This as an :class:`~fxtr.entity_defns.array.ArrayType`.

        Raises:
            TypeError: If the dims or the schema are unknown.
        """
        if self.dims is None or self.schema is None:
            missing = "dims" if self.dims is None else "value schema"
            raise TypeError(
                f"{where}: the annotation does not give the {missing} (see"
                " fxtr.experiment.annotations)"
            )
        return ArrayType(self.dims, self.schema)

    def __str__(self) -> str:
        dims = "?" if self.dims is None else f"[{', '.join(map(str, self.dims))}]"
        return f"{dims}: {'?' if self.schema is None else self.schema}"


def is_array_annotation(annotation: Any) -> bool:
    """Whether an annotation is a :class:`~fxtr.entity_defns.array.Array` or a
    :class:`~fxtr.experiment.handles.ArrayHandle`, parameterized or not.

    These are the annotations whose bare-string ``Annotated`` metadata is read as dims.
    """
    return (typing.get_origin(annotation) or annotation) in (Array, ArrayHandle)


def declared_array_type(annotation: Any, where: str) -> AnnotatedType | None:
    """The array type an annotation declares, or ``None`` if it declares nothing (``Any``,
    ``object``).

    See the module docstring for what is read. A bare string on anything but an array or handle
    annotation is metadata for some other tool and is ignored.

    Raises:
        TypeError: If the annotation is malformed: a dims string that does not parse or more than
            one of them, an array or handle inside a union (an array is never optional), or a
            value type that is not a schema annotation.
    """
    ann = expand_type_alias(annotation)
    dims: Dims | None = None
    if typing.get_origin(ann) is Annotated:
        base, *metadata = typing.get_args(ann)
        strings = [m for m in metadata if isinstance(m, str)]
        if strings and is_array_annotation(base):
            if len(strings) > 1:
                raise TypeError(f"{where}: declares {len(strings)} dims strings, expected one")
            try:
                dims = parse_dims(strings[0])
            except ArrayTypeError as exc:
                raise TypeError(f"{where}: {exc}") from exc
        ann = base
    if ann is Any or ann is object:
        return None
    if is_array_annotation(ann):
        args = typing.get_args(ann)
        arg = args[0] if args else Any
        if arg is Any or arg is FrozenValue or isinstance(arg, TypeVar):
            return AnnotatedType(dims, None)
        return AnnotatedType(dims, _schema(arg, where))
    if typing.get_origin(ann) in (typing.Union, types.UnionType) and any(
        is_array_annotation(m) for m in typing.get_args(ann)
    ):
        raise TypeError(
            f"{where}: an array or handle is never optional; write Annotated[Array[X | None], ...]"
            " for an array of optional values"
        )
    return AnnotatedType((), _value_schema(ann, where))


def _value_schema(annotation: Any, where: str) -> Schema:
    """The schema of a value (non-array) annotation, with the two top-level entity-class forms."""
    ann = annotation
    if typing.get_origin(ann) is Annotated:
        ann = typing.get_args(ann)[0]
    if typing.get_origin(ann) in (typing.Union, types.UnionType):
        members = [m for m in typing.get_args(ann) if m is not type(None)]
        if len(members) == 1 and _is_entity_class(members[0]):
            return NullableSchema(_schema(members[0], where))
    return _schema(ann, where)


def _schema(annotation: Any, where: str) -> Schema:
    try:
        if _is_entity_class(annotation):
            return id_of(annotation)
        return schema_from_annotation(annotation)
    except SchemaError as exc:
        raise TypeError(f"{where}: {exc}") from exc


def _is_entity_class(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, Entity)


def unwrap_annotation(annotation: Any) -> Any:
    """The type a parameter's annotation is really about: any :data:`~typing.Annotated` metadata and
    any ``| None`` stripped off, in whichever order they are nested.

    This is what the step and workflow argument classifiers key on: it turns
    ``Annotated[BoundID[Bio], "..."]`` and ``Bio | None`` into ``BoundID[Bio]`` and ``Bio``, and a
    PEP 695 ``type`` alias into the type it stands for (see :func:`expand_type_alias`).
    """
    ann = _strip_annotated(expand_type_alias(annotation))
    return _strip_annotated(_strip_optional(expand_type_alias(ann)))


def _strip_annotated(annotation: Any) -> Any:
    """``Annotated[X, ...]`` to ``X``; else unchanged."""
    if typing.get_origin(annotation) is Annotated:
        return typing.get_args(annotation)[0]
    return annotation


def _strip_optional(annotation: Any) -> Any:
    """``X | None`` to ``X`` (a nullable scalar entity reference may arrive as ``None``); else
    unchanged."""
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        members = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(members) == 1:
            return members[0]
    return annotation


def body_hints(fn: Any) -> tuple[tuple[str, ...], dict[str, Any]]:
    """A step or workflow body's argument names, in order, and its resolved type hints.

    The first parameter (the context) is dropped from the names; the hints keep ``Annotated``
    metadata and include the return annotation under ``"return"``.

    Raises:
        TypeError: If the annotations cannot be resolved, or the body takes ``*args`` or
            ``**kwargs``, which a step or workflow cannot be given an array for.
    """
    try:
        hints = typing.get_type_hints(fn, include_extras=True)
    except NameError as exc:
        raise TypeError(
            f"Cannot resolve the annotations of {fn.__qualname__}: {exc}. Define the types it"
            " names before it, or apply the decorator after they exist."
        ) from exc
    params = list(inspect.signature(fn).parameters.values())
    variadic = [
        p.name
        for p in params
        if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]
    if variadic:
        raise TypeError(
            f"{fn.__qualname__} takes {', '.join(variadic)}; a step or workflow body must name"
            " every argument, since it is called with one array per name"
        )
    return tuple(p.name for p in params[1:]), hints


def declared_signature_types(
    fn: Any,
) -> tuple[dict[str, AnnotatedType | None], AnnotatedType | None]:
    """Read the array types a step or workflow body's annotations give.

    Every argument gets an entry; ``None`` means the argument is unannotated or annotated ``Any``
    / ``object``, and the result likewise.

    Args:
        fn: The step or workflow body; its annotations are resolved here, so the types it names
            must exist by now.

    Returns:
        The type of each argument by name, in declaration order with the context parameter
        dropped, and the result's. Entries may be ``None`` or have unknown parts.

    Raises:
        TypeError: If the annotations cannot be resolved, or one is malformed (see
            :func:`declared_array_type`).
    """
    names, hints = body_hints(fn)
    where = fn.__qualname__
    args = {
        name: (
            declared_array_type(hints[name], f"{where} parameter {name!r}")
            if name in hints
            else None
        )
        for name in names
    }
    result = declared_array_type(hints["return"], f"{where} result") if "return" in hints else None
    return args, result
