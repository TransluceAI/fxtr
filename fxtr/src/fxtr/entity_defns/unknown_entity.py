"""A fallback entity for data whose specific type is not known.

``UnknownEntity`` wraps a raw :data:`FrozenFxtrDataValue` and stores it verbatim. It is useful when
loading an entity whose concrete Python type is unknown: the bytes round-trip faithfully
(store → load → store yields the same content id) even though nothing interprets them beyond the
``$type`` tag every stored entity carries.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from fxtr.core.entities import Entity, EntityLoader
from fxtr.core.entity_data import FrozenFxtrDataValue, StorableFxtrDataValue, freeze_fxtr_data
from fxtr.core.loader_registry import DEFAULT_ENTITY_LOADER_REGISTRY


@dataclasses.dataclass(frozen=True)
class UnknownEntity(Entity):
    """An entity that holds arbitrary serialized data without interpreting it.

    Attributes:
        raw_data: The entity's data in frozen (hashable) form. Like all stored entity data, it
            must be a mapping carrying a valid ``$type`` tag (the store enforces this).
    """

    raw_data: FrozenFxtrDataValue

    def _fxtr_serialize(self) -> StorableFxtrDataValue:
        return self.raw_data

    def _fxtr_loader(self) -> EntityLoader[UnknownEntity]:
        return UNKNOWN_ENTITY_LOADER


class UnknownEntityLoader(EntityLoader[UnknownEntity]):
    """Wildcard loader that deserializes any entity as an :class:`UnknownEntity`.

    Supports every type, since the raw data is wrapped without interpretation.
    """

    def supports_type(self, type_id: str) -> bool:
        return True

    def convert(self, data: StorableFxtrDataValue) -> UnknownEntity:
        """Wrap ``data`` (frozen, ids kept verbatim) as an :class:`UnknownEntity`."""
        return UnknownEntity(raw_data=freeze_fxtr_data(data))


UNKNOWN_ENTITY_LOADER = UnknownEntityLoader()
"""Shared stateless :class:`UnknownEntityLoader` instance used by unknown-entity references."""


class UnknownEntityLoaderFactory:
    """Registry factory resolving annotations to the shared wildcard loader."""

    def build_loader(self, annotation: Any) -> EntityLoader[Any]:
        return UNKNOWN_ENTITY_LOADER


DEFAULT_ENTITY_LOADER_REGISTRY.register(UnknownEntity, UnknownEntityLoaderFactory())
