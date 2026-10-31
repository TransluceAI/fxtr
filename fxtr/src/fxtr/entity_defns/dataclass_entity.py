"""Dataclass entities: frozen dataclasses stored and loaded through the fxtr converter.

This module is the convenient way to define entities: declare a frozen dataclass, give it a type
via the ``fxtr_type`` class keyword (see :class:`fxtr.entity_defns.dataclass_bases.TaggedDataclass`
for the declaration surface), and it can be stored into any
:class:`fxtr.core.entities.FxtrEntityStorage` and loaded back through a ``BoundID`` — no per-class
registration needed. Two class kinds:

- :class:`TaggedDataclass`: carries a ``$type`` tag and serializes inline; *not* storable by
  itself — tagging is deliberately independent of storage.
- :class:`DataclassEntity` (here): a ``TaggedDataclass`` that is also an
  :class:`fxtr.core.entities.Entity` — storable, and loadable via annotation-driven loaders
  resolved through the default loader registry.

Serialization runs through the strict cattrs-backed converter in
:mod:`fxtr.entity_defns.converter` (see its docstring for the serialization rules, strictness
guarantees, and the invariants governing generic type parameters). This module contributes the
storage contract implementation, including the check that an entity's data loads back when it is
serialized (see :class:`DataclassEntity`), and the loaders that deserialize entity data by
annotation.
"""

from __future__ import annotations

import dataclasses
import typing
from typing import Any, ClassVar, Self

from fxtr.core.entities import Entity, EntityLoader, StaticTypeLoader
from fxtr.core.entity_data import FxtrDataValue, StorableFxtrDataValue
from fxtr.core.loader_registry import DEFAULT_ENTITY_LOADER_REGISTRY
from fxtr.entity_defns.converter import DEFAULT_CONVERTER, FxtrConverter
from fxtr.entity_defns.dataclass_bases import ConversionError, TaggedDataclass


class DataclassEntityLoader[T](StaticTypeLoader[T]):
    """Loader for a concrete ``DataclassEntity`` annotation, backed by a converter.

    ``annotation`` must be a class (or generic alias of one) with a declared type — that type id
    is what the loader claims in its support check, and union dispatch relies on it. Conversion is
    ``converter.structure`` against the annotation, so the class's tagged structure hook
    re-validates the ``$type`` tag even on a direct (non-union) load.
    """

    def __init__(self, annotation: Any, converter: FxtrConverter) -> None:
        cls = typing.get_origin(annotation) or annotation
        self._static_type_id = cls._fxtr_require_type_id()
        self._annotation = annotation
        self._converter = converter

    def convert(self, data: StorableFxtrDataValue) -> T:
        return self._converter.structure(data, self._annotation)

    def __repr__(self) -> str:
        return f"<DataclassEntityLoader {self._annotation!r}>"


@dataclasses.dataclass(frozen=True)
class DataclassEntityLoaderFactory:
    """Registry factory covering every ``DataclassEntity`` subclass (via MRO walking).

    Registered for the ``DataclassEntity`` base class with ``allow_implicit_overrides=True``, so
    a specific class (or alias) can take over its own loading by registering its own factory.
    """

    converter: FxtrConverter

    def build_loader(self, annotation: Any) -> EntityLoader[Any]:
        return DataclassEntityLoader(annotation, self.converter)


def _infer_default_annotation(cls: type) -> Any:
    """Default loading annotation for storing: the class, with erased type params filled in.

    Each parameter is filled with its bound; an unbounded parameter is an error. A bound is often
    an abstract class that cannot itself be decoded, so such instances load only through an
    explicit annotation.
    """
    type_params = getattr(cls, "__type_params__", ())
    if not type_params:
        return cls
    args = []
    for param in type_params:
        bound = getattr(param, "__bound__", None)
        if bound is None:
            raise TypeError(
                f"Cannot infer a default loading annotation for {cls.__name__}: type parameter"
                f" {param!r} has no bound"
            )
        args.append(bound)
    return typing.cast(Any, cls)[tuple(args) if len(args) > 1 else args[0]]


def _check_loads_back(cls: type, data: dict[str, FxtrDataValue]) -> None:
    """Require that ``data``, serialized from an instance of ``cls``, decodes as ``cls`` and
    re-encodes to itself.

    Raises:
        ConversionError: If it does not; a decoding failure is the ``__cause__``.
    """
    try:
        decoded = DEFAULT_CONVERTER.structure(data, cls)
    except ConversionError as exc:
        raise ConversionError(
            f"{cls.__name__} would not load back from the data it serializes to: {exc}"
        ) from exc
    redone = DEFAULT_CONVERTER.unstructure_entity(decoded)
    if redone != data:
        fields = sorted(k for k in data.keys() | redone.keys() if data.get(k) != redone.get(k))
        raise ConversionError(
            f"{cls.__name__} does not serialize reproducibly: loading its data back and"
            f" serializing again changes field(s) {fields!r}"
        )


@dataclasses.dataclass(frozen=True, kw_only=True)
class DataclassEntity(TaggedDataclass, Entity):
    """A tagged dataclass that is storable as an entity.

    Subclasses are ordinary frozen dataclasses declaring ``fxtr_type`` (see
    :class:`TaggedDataclass`); they store through any :class:`FxtrEntityStorage` and load back
    through the returned ``BoundID`` with no per-class registration.

    Serializing checks that the entity would load back: the data is decoded as the class, under
    the same strict rules a load applies, and re-encoding the decoded value must reproduce it. A
    value the decoder rejects (an ``int`` in a ``float`` field, a subclass in a field annotated
    with its base, a union cattrs cannot decode) so fails when it is stored, not when it is next
    read. A generic class is not checked: the code loading it chooses its type arguments, and a
    value in a type-parameter slot may be narrower than the parameter's bound. A class whose
    values are already checked as strictly when constructed, and are costly to decode, opts out
    by setting ``_fxtr_check_decodes_on_store`` to ``False``.
    """

    _fxtr_check_decodes_on_store: ClassVar[bool] = True

    def _fxtr_serialize(self) -> StorableFxtrDataValue:
        data = DEFAULT_CONVERTER.unstructure_entity(self)
        cls = type(self)
        # A generic class's data is loaded with type arguments the loading code chooses, so it has
        # no one annotation to check against (see the converter's docstring).
        if cls._fxtr_check_decodes_on_store and not getattr(cls, "__parameters__", ()):
            _check_loads_back(cls, data)
        return data

    def _fxtr_loader(self) -> EntityLoader[Self]:
        # Resolved from the class's default loading annotation through the default registry — the
        # same resolution (and cache) a `BoundID[...]` field annotation would hit, so
        # store-returned and decoded refs to the same target compare equal.
        return DEFAULT_ENTITY_LOADER_REGISTRY.loader_for(_infer_default_annotation(type(self)))


DEFAULT_ENTITY_LOADER_REGISTRY.register(
    DataclassEntity, DataclassEntityLoaderFactory(DEFAULT_CONVERTER), allow_implicit_overrides=True
)
