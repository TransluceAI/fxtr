"""Dynamically-typed inline values: a tagged value held serialized until its type is asked for.

Field annotations normally fix the Python type of a value up front, and the converter decodes to
exactly that type (or to one member of a declared union). :class:`DynamicData` is the escape hatch
for a slot whose type is *not* known where the containing class is defined — a metadata field, an
extension point, a value that different producers fill with different shapes — while keeping the
value discriminated: it holds a tagged value's serialized form, ``$type`` tag included, and the
consumer asks for a concrete type when it knows what it expects (:meth:`DynamicData.as_type`
/ :meth:`DynamicData.try_as_type`). The tag check is what keeps distinct types distinct: data
written as one type never silently decodes as another. It is the inline analog of
:class:`fxtr.entity_defns.unknown_entity.UnknownEntity`, which plays the same role for stored
entities behind a reference.

**Transparent wire form.** A ``DynamicData`` serializes to exactly the tagged mapping its wrapped
value would produce on its own, so a field annotated ``DynamicData``, one annotated with the
concrete tagged class, and one annotated with a tagged union all have the same wire form and the
same content id; they differ only in the Python-side loading policy. Converting a value to
``DynamicData`` (:meth:`DynamicData.from_tagged`) and back (:meth:`DynamicData.as_type`) is
therefore a pure change of Python representation, and the one field a ``DynamicData`` holds is
that tagged mapping, frozen.

``DynamicData`` does not resolve the stored type on its own: there is deliberately no "decode as
whatever the stored type turns out to be" (see the :mod:`fxtr.entity_defns.converter` docstring) —
the consumer always names the type it can handle, and loading can only fail at that point.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

import cattrs
from cattrs.dispatch import StructureHook, UnstructureHook
from frozendict import frozendict

from fxtr.core.entity_data import (
    EntityTypeTag,
    FrozenFxtrDataValue,
    InvalidEntityDataError,
    StorableFxtrDataValue,
    freeze_fxtr_data,
    get_entity_type_tag,
    thaw_fxtr_data,
)
from fxtr.entity_defns.converter import DEFAULT_CONVERTER, FxtrConverter
from fxtr.entity_defns.dataclass_bases import StrictTypeError, TaggedDataclass


@dataclasses.dataclass(frozen=True)
class DynamicData:
    """A tagged value held in its serialized form, decodable on demand as a requested type.

    Build one from a Python value with :meth:`from_tagged`, or from a tagged mapping (wire form)
    with :meth:`from_data`; read it back with :meth:`as_type` / :meth:`try_as_type` once the
    expected type is known, or inspect :attr:`type` directly. Serialization is transparent (see the
    module docstring), so it can annotate any field of a tagged dataclass where the value's type is
    decided by the producer rather than the schema.

    Equality and hashing are by field, so a freshly built instance equals its counterpart loaded
    back from a store.

    Attributes:
        data: The wrapped value's serialized form, frozen: a mapping carrying its ``$type`` tag
            alongside its fields.
    """

    data: frozendict[str, FrozenFxtrDataValue]

    def __post_init__(self) -> None:
        if not isinstance(self.data, frozendict):
            raise TypeError(
                f"DynamicData.data must be a frozendict (use from_data / from_tagged to build"
                f" from other shapes); got {type(self.data).__name__}"
            )
        get_entity_type_tag(self.data)

    # ---- construction ----

    @classmethod
    def from_tagged(
        cls, value: TaggedDataclass, converter: FxtrConverter = DEFAULT_CONVERTER
    ) -> DynamicData:
        """Wrap a tagged dataclass value, serializing it with ``converter``.

        Args:
            value: The value to wrap. Its class must declare a tag.
            converter: The converter that serializes it; the shared default if omitted.

        Returns:
            A ``DynamicData`` holding the value's serialized form.

        Raises:
            ConversionError: If ``value`` cannot be serialized (e.g. its class declares no tag).
        """
        return cls.from_data(converter.unstructure_entity(value))

    @classmethod
    def from_data(cls, data: Mapping[str, StorableFxtrDataValue]) -> DynamicData:
        """Wrap a tagged mapping in wire form (any storable shape).

        Args:
            data: A mapping carrying a valid ``$type`` tag, e.g. the output of
                :meth:`fxtr.entity_defns.converter.FxtrConverter.unstructure_entity` or raw data
                from a store.

        Returns:
            A ``DynamicData`` holding the mapping, frozen.

        Raises:
            InvalidEntityDataError: If ``data`` is not a mapping carrying a valid ``$type`` tag.
        """
        get_entity_type_tag(data)
        return cls(data=frozendict({k: freeze_fxtr_data(v) for k, v in data.items()}))

    # ---- reading ----

    @property
    def type(self) -> EntityTypeTag:
        """The wrapped value's ``$type`` tag."""
        return get_entity_type_tag(self.data)

    def is_type(self, cls: type[TaggedDataclass]) -> bool:
        """Whether the wrapped value is tagged with ``cls``'s declared type.

        A cheap tag comparison without decoding the content; a ``True`` result means
        :meth:`as_type` will attempt the decode rather than reject the tag.

        Raises:
            TypeError: If ``cls`` declares no tag (an abstract tagged class).
        """
        return self.type == cls._fxtr_require_type_id()

    def as_type[T: TaggedDataclass](
        self, cls: type[T], converter: FxtrConverter = DEFAULT_CONVERTER
    ) -> T:
        """Decode the wrapped value as ``cls``, strictly.

        Args:
            cls: The tagged dataclass to decode as. Its declared tag must match :attr:`type` and
                its fields must match the rest of :attr:`data` exactly (the converter's usual
                strictness).
            converter: The converter to decode with; the shared default if omitted.

        Returns:
            The decoded value.

        Raises:
            ConversionError: If the tag does not match ``cls`` (a :class:`StrictTypeError`), or
                the content does not decode as ``cls``.
        """
        return converter.structure(self.data, cls)

    def try_as_type[T: TaggedDataclass](
        self, cls: type[T], converter: FxtrConverter = DEFAULT_CONVERTER
    ) -> T | None:
        """Decode as ``cls`` if the wrapped value is tagged with its type, else return ``None``.

        The ``None`` branch covers only a *tag* mismatch (see :meth:`is_type`); a matching tag with
        content that does not decode still raises, since that indicates corrupt or incompatible
        data rather than a different type. Handy with the walrus operator::

            if (circle := metadata.try_as_type(Circle)) is not None:
                ...

        Raises:
            ConversionError: If the tag matches but the content does not decode as ``cls``.
        """
        if not self.is_type(cls):
            return None
        return self.as_type(cls, converter)

    # ---- the serialization-hook protocol (see fxtr.entity_defns.dataclass_bases) ----
    # DynamicData is an untagged protocol class: its hooks produce and consume the whole wire
    # form, which is the wrapped value's own tagged mapping — so the field's wire form is exactly
    # what the concrete tagged class would have produced in that slot.

    @classmethod
    def __fxtr_make_structure_hook__(cls, conv: cattrs.Converter, annotation: Any) -> StructureHook:
        def structure(value: Any, _target: Any) -> DynamicData:
            try:
                return cls.from_data(value)
            except InvalidEntityDataError as exc:
                raise StrictTypeError(f"Expected a tagged mapping for DynamicData: {exc}") from exc

        return structure

    @classmethod
    def __fxtr_make_unstructure_hook__(
        cls, conv: cattrs.Converter, annotation: Any
    ) -> UnstructureHook:
        def unstructure(value: DynamicData) -> Any:
            # Plain mutable form, like every other unstructured value.
            return thaw_fxtr_data(value.data)

        return unstructure
