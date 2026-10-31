"""The class-side surface of fxtr serialization: tag declarations and the hook protocol.

Everything a class author touches lives here; the converter machinery that consumes it lives in
:mod:`fxtr.entity_defns.converter`, and the storable entity kind built on top lives in
:mod:`fxtr.entity_defns.dataclass_entity`.

**Tag declarations.** A *tagged dataclass* is a frozen dataclass whose serialized form is a
mapping carrying the reserved ``$type`` tag (see :data:`fxtr.core.entity_data.TYPE_KEY`).
:class:`TaggedDataclass` is the base; a class declares its type id with the ``fxtr_type`` class
keyword argument, a string (by convention a reverse-domain name)::

    class Circle(TaggedDataclass, fxtr_type="com.example.shapes.Circle"): ...

**The serialization-hook protocol.** A class customizes how its *content* (de)serializes by
defining a classmethod pair, one per conversion direction (mirroring cattrs' own per-direction
dispatch)::

    @classmethod
    def __fxtr_make_structure_hook__(
        cls, conv: cattrs.Converter, annotation: Any
    ) -> StructureHook: ...

    @classmethod
    def __fxtr_make_unstructure_hook__(
        cls, conv: cattrs.Converter, annotation: Any
    ) -> UnstructureHook: ...

Each receives the converter's underlying ``cattrs.Converter`` and the annotation being resolved
— a parameterized generic alias, or the bare class (unstructure hooks are also built for bare
generic classes, since unstructuring dispatches on runtime types and never depends on type
parameters) — and returns the hook for the class's content. cattrs' own gen API
(``make_dict_structure_fn`` / ``make_dict_unstructure_fn`` with ``override(...)``, ...) is the
customization vocabulary, with :func:`make_dataclass_structure_hook` /
:func:`make_dataclass_unstructure_hook` covering the common cases. The contract:

- The two directions must be customized *together*, so serialize and deserialize cannot
  silently diverge: a :class:`TaggedDataclass` class body defining one method without the other
  is rejected at class definition (inheriting both from a shared base or mixin is fine), and
  any other class defining either method must define both (checked when its hooks are first
  built).
- Hooks are built once per distinct annotation (cattrs caches them) and must not mutate or
  register anything on the converter.
- For :class:`TaggedDataclass` subclasses (which inherit default implementations returning the
  generated dataclass hooks), the hooks govern the plain field mapping only: the framework's
  ``$type`` layer — splice on serialize, validate + strip on deserialize — is applied *around*
  them by the converter and cannot be bypassed, so a class controls its field format but never
  its tag mechanics.
- Any other class defining the pair is dispatched through it directly: the hooks produce and
  consume the class's whole wire form, which must be a valid
  :data:`fxtr.core.entity_data.FxtrDataValue` shape. This is how custom leaf value types
  participate; strict validation of incoming data is the class's own responsibility.

Plain dataclasses that neither subclass :class:`TaggedDataclass` nor define the classmethods
serialize through cattrs' builtin dataclass handling (which shares the converter's strictness),
as an untagged plain field mapping.

Field annotations of tagged and plain dataclasses alike may use quoted names anywhere, including
nested inside other types (``tuple["Group", ...]``); the converter resolves them in the defining
module before building hooks, so the class must be defined at module level to refer to itself.
"""

from __future__ import annotations

import dataclasses
import typing
from collections.abc import Mapping
from typing import Any, ClassVar

import cattrs
from cattrs.dispatch import StructureHook, UnstructureHook
from cattrs.gen import make_dict_structure_fn, make_dict_unstructure_fn

FXTR_HOOK_METHOD_NAMES = ("__fxtr_make_structure_hook__", "__fxtr_make_unstructure_hook__")
"""The serialization-hook protocol's classmethod names (see the module docstring)."""


class ConversionError(Exception):
    """A value could not be converted to or from its serialized data form.

    The single exception boundary of the conversion layer: anything that escapes a converter
    entry point (``structure`` / ``unstructure_entity`` and the loader-side variants) is a
    ``ConversionError``. The underlying failure is either this exception itself (a subclass,
    such as :class:`StrictTypeError`, raised directly by fxtr hooks) or attached as
    ``__cause__`` (cattrs field-validation exception groups, hook-build errors such as an
    undeclared tag). Class-*definition*-time errors are ordinary ``TypeError``\\ s, not
    ``ConversionError``\\ s — they are programming errors surfacing eagerly, not conversion
    failures.
    """


class StrictTypeError(ConversionError, TypeError):
    """Raised when a value's runtime type or shape does not exactly match the annotated type."""


def make_dataclass_structure_hook(conv: cattrs.Converter, annotation: Any) -> StructureHook:
    """Build the standard generated structure hook for a dataclass annotation.

    This is the default content deserialization for fxtr dataclasses (and the building block
    for ``__fxtr_make_structure_hook__`` overrides that tweak it): cattrs' generated
    field-by-field dict hook, plus **strict field presence** — every declared field must be
    present in the data, so a missing field is an error rather than being silently filled from
    its dataclass default. Distinct wire forms must not alias to one Python value: a canonical
    writer always emits every field, so data missing one was written under a different schema
    and is rejected.

    Args:
        conv: The underlying cattrs converter, used to resolve the field hooks.
        annotation: The dataclass annotation. Must carry concrete type arguments for a generic
            class (a bare generic class is rejected here, at hook-build time: there is nothing
            to substitute for its parameters).

    Returns:
        The structure hook.
    """
    cls = typing.get_origin(annotation) or annotation
    if annotation is cls and getattr(cls, "__parameters__", ()):
        raise StrictTypeError(
            f"Cannot deserialize {cls.__name__} without type parameters; use a parameterized"
            " annotation"
        )
    field_names = tuple(f.name for f in dataclasses.fields(cls) if f.init)
    base_structure = make_dict_structure_fn(annotation, conv)

    def structure(value: Any, target: Any) -> Any:
        if not isinstance(value, Mapping):
            raise StrictTypeError(
                f"Expected a mapping for {cls.__name__}, got {type(value).__name__}"
            )
        missing = [name for name in field_names if name not in value]
        if missing:
            raise StrictTypeError(
                f"Field(s) {missing!r} missing from serialized {cls.__name__} data; every"
                " declared field is required (defaults are not filled in)"
            )
        return base_structure(value, target)

    return structure


def make_dataclass_unstructure_hook(conv: cattrs.Converter, annotation: Any) -> UnstructureHook:
    """Build the standard generated unstructure hook for a dataclass annotation.

    The default content serialization for fxtr dataclasses: cattrs' generated field-by-field
    dict hook, emitting every declared field (the counterpart of
    :func:`make_dataclass_structure_hook`'s strict field presence).

    Args:
        conv: The underlying cattrs converter, used to resolve the field hooks.
        annotation: The dataclass annotation (a parameterized generic alias, or a bare class —
            serialization never depends on type parameters).

    Returns:
        The unstructure hook.
    """
    return make_dict_unstructure_fn(annotation, conv)


class TaggedDataclass:
    """Base for frozen dataclasses carrying a ``$type`` tag.

    Not storable by itself; tagged dataclasses serialize inline (a dict with a ``$type`` string)
    and may be members of tagged unions anywhere in a value. Subclasses declare their type with
    the ``fxtr_type`` class keyword argument::

        @dataclasses.dataclass(frozen=True, kw_only=True)
        class Circle(TaggedDataclass, fxtr_type="com.example.shapes.Circle"):
            radius: float

    The tag is class-level metadata, not a field: it is added unconditionally when serializing
    and validated and stripped when deserializing. A subclass that does not pass ``fxtr_type``
    declares no type of its own (it does *not* inherit its parent's — that would silently store
    subclasses under the parent's identity) and cannot be serialized directly.
    """

    _fxtr_known_type_id: ClassVar[str | None] = None

    def __init_subclass__(cls, fxtr_type: str | None = None, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if fxtr_type is not None and not (isinstance(fxtr_type, str) and fxtr_type):
            raise TypeError(
                f"{cls.__name__}: fxtr_type must be a non-empty string, got {fxtr_type!r}"
            )
        cls._fxtr_known_type_id = fxtr_type
        # The two hook directions must be customized together (see the module docstring): a
        # class body overriding one without the other would silently pair its custom half with
        # an inherited default half, letting serialize and deserialize disagree.
        defined = [name for name in FXTR_HOOK_METHOD_NAMES if name in cls.__dict__]
        if len(defined) == 1:
            raise TypeError(
                f"{cls.__name__} defines {defined[0]} without its counterpart; the structure"
                " and unstructure hooks must be customized together"
            )

    @classmethod
    def __fxtr_make_structure_hook__(cls, conv: cattrs.Converter, annotation: Any) -> StructureHook:
        """Build this class's content structure hook.

        Half of the serialization-customization pair (see the module docstring for the full
        contract): the returned hook decodes the plain field mapping, and the converter
        validates + strips the ``$type`` layer before it runs. The default is
        :func:`make_dataclass_structure_hook`; overrides are inherited by subclasses like any
        classmethod.
        """
        return make_dataclass_structure_hook(conv, annotation)

    @classmethod
    def __fxtr_make_unstructure_hook__(
        cls, conv: cattrs.Converter, annotation: Any
    ) -> UnstructureHook:
        """Build this class's content unstructure hook.

        Half of the serialization-customization pair (see the module docstring for the full
        contract): the returned hook produces the plain field mapping, and the converter
        splices the ``$type`` layer in after it runs. The default is
        :func:`make_dataclass_unstructure_hook`; overrides are inherited by subclasses like any
        classmethod.
        """
        return make_dataclass_unstructure_hook(conv, annotation)

    @classmethod
    def fxtr_type_id(cls) -> str | None:
        """The type id declared with ``fxtr_type``, or ``None`` if the class declared none. Same
        contract as :meth:`fxtr.core.entities.Entity.fxtr_type_id`; defined here so inline tagged
        values answer it too."""
        return cls._fxtr_known_type_id

    @classmethod
    def _fxtr_require_type_id(cls) -> str:
        """Return the class's declared type id, raising if it declares none."""
        if cls._fxtr_known_type_id is None:
            raise TypeError(f"{cls.__name__} does not declare fxtr_type; is it abstract?")
        return cls._fxtr_known_type_id
