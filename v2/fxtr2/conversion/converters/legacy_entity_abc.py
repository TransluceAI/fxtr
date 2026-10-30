"""Legacy Entity ABC and its conversion handler.

This module provides backwards compatibility for entity types that were defined using the old
Entity abstract base class pattern (with _to_fxtr_cbor, _from_fxtr_cbor, etc.). New code should
generally subclass the new Entity base class + register a custom load handler instead of using this
class.
"""

from __future__ import annotations

import abc
import inspect
import typing
from typing import Any, Awaitable, Self

from fxtr2.data.entity_core import FxtrCbor, FxtrID
from fxtr2.data.entity_ref_scope import (
    DeserializedEntity,
    Entity,
    EntityLoadHandler,
    EntityRef,
    EntityTypeAnnotation,
    get_active_scope,
    register_entity_load_handler,
)


class SelfLoadingLegacyEntity(Entity):
    """Base class for python objects that should be serialized as entities in the fxtr graph.

    Deprecated: New code should use the new Entity base class for serialization +. This class is kept for backwards
    compatibility with existing entity types that use the ABC pattern.

    Fxtr will use this class to serialize and deserialize entities to/from the fxtr graph. The
    main restrictions are:
    - Entities that compare equal must have the same fxtr-CBOR representation, and vice versa.
    - Loading an entity and then storing that entity must produce the same fxtr-CBOR representation.
    - Usually, storing an entity and then loading it should produce the same entity object.
    - The serialized form must have exactly one RawFxtrEntityMarkerID, located at "$fxtr.ctx" at
      the root of the dictionary.
    - Each entity class must have a hardcoded type ID, and every serialized entity of that type must
      have that type ID.
    - If _fxtr_immutable() returns True, the entity must be actually immutable, and it will be
      cached and reused automatically.
    """

    @abc.abstractmethod
    def _to_fxtr_cbor(self, scope: Any) -> FxtrCbor:
        """Convert the entity to its fxtr-CBOR representation.

        Args:
            scope: The scope to use for storing child entities. In the new system, this will be
                the active FxtrScope.

        Returns:
            The fxtr-CBOR representation of the entity.
        """
        raise NotImplementedError

    @classmethod
    @abc.abstractmethod
    def _from_fxtr_cbor(
        cls,
        cbor: FxtrCbor,
        annotated_type: Any | None,
        scope: Any,
    ) -> Self | Awaitable[Self]:
        """Convert the entity from its fxtr-CBOR representation.

        May return an Awaitable[Self] if async operations are needed.

        Args:
            cbor: The fxtr-CBOR representation of the entity.
            annotated_type: The annotated type of the entity.
            scope: The scope to use for loading child entities. In the new system, this will be
                the active FxtrScope.

        Returns:
            The entity, or an Awaitable that resolves to the entity.
        """
        raise NotImplementedError

    def _fxtr_immutable(self) -> bool:
        """Whether the entity promises to be immutable. If so, it may be cached."""
        return False

    @classmethod
    @abc.abstractmethod
    def _fxtr_type_id(cls) -> FxtrID:
        """Get the type ID for the entity."""
        raise NotImplementedError

    @typing.override
    def _fxtr_store_impl(self) -> EntityRef[Self]:
        """Compatibility implementation of _fxtr_store_impl that delegates to _to_fxtr_cbor."""
        scope = get_active_scope()
        cbor = self._to_fxtr_cbor(scope)
        return EntityRef(scope.store_cbor(cbor), self.__class__)


class SelfLoadingLegacyEntityLoadHandler(EntityLoadHandler[SelfLoadingLegacyEntity]):
    """Conversion handler that bridges the old Entity ABC to the new registry system.

    This handler delegates to the Entity's _to_fxtr_cbor and _from_fxtr_cbor methods,
    adapting them to the new EntityConversionHandler protocol.
    """

    @typing.override
    async def supports_entity_type(
        self,
        type_id: FxtrID,
        target_type: EntityTypeAnnotation[SelfLoadingLegacyEntity],
    ) -> bool:
        base_type = typing.get_origin(target_type)
        if base_type is None:
            base_type = target_type
        if not (isinstance(base_type, type) and issubclass(base_type, SelfLoadingLegacyEntity)):
            return False
        if base_type is SelfLoadingLegacyEntity:
            # Don't match the abstract base itself
            return False
        return type_id == base_type._fxtr_type_id()

    @typing.override
    async def from_lowlevel_entity_data(
        self,
        cbor: FxtrCbor,
        target_type: EntityTypeAnnotation[SelfLoadingLegacyEntity],
    ) -> DeserializedEntity[SelfLoadingLegacyEntity]:
        from fxtr2.data.entity_ref_scope import get_active_scope

        base_type = typing.get_origin(target_type)
        if base_type is None:
            base_type = target_type
        assert isinstance(base_type, type) and issubclass(base_type, SelfLoadingLegacyEntity)

        scope = get_active_scope()
        result = base_type._from_fxtr_cbor(cbor, target_type, scope)
        if inspect.isawaitable(result):
            result = await result

        return DeserializedEntity(
            value=result,
            cacheable=result._fxtr_immutable(),
        )


register_entity_load_handler(SelfLoadingLegacyEntity, SelfLoadingLegacyEntityLoadHandler())
