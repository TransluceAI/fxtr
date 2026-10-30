"""Unknown fallback entity object and converters."""

from __future__ import annotations

import dataclasses
import typing

from fxtr2.conversion.base_registry import OverrideFxtrHandlers
from fxtr2.data.entity_core import (
    FrozenFxtrCbor,
    FxtrCbor,
    FxtrID,
    freeze_fxtr_cbor,
    thaw_fxtr_cbor,
)
from fxtr2.data.entity_ref_scope import (
    DeserializedEntity,
    Entity,
    EntityLoadHandler,
    EntityRef,
    EntityTypeAnnotation,
    get_active_scope,
    register_entity_load_handler,
)


@dataclasses.dataclass(frozen=True)
class UnknownEntity(Entity):
    """A fallback entity wrapping frozen entity data for an entity whose type is not known.

    UnknownEntity stores its CBOR data in an immutable form (FrozenFxtrCbor), making it hashable
    and cacheable. Use UnknownMutableEntity if you need mutable access to the raw CBOR data.
    """

    raw_cbor: FrozenFxtrCbor

    def _fxtr_store_impl(self) -> EntityRef[UnknownEntity]:
        scope = get_active_scope()
        return EntityRef(scope.store_cbor(thaw_fxtr_cbor(self.raw_cbor)), UnknownEntity)


@dataclasses.dataclass(frozen=True)
class UnknownEntityLoadHandler(EntityLoadHandler[UnknownEntity]):
    """Conversion handler for UnknownEntity."""

    @typing.override
    async def supports_entity_type(
        self, type_id: FxtrID, target_type: EntityTypeAnnotation[UnknownEntity]
    ) -> bool:
        return True

    @typing.override
    async def from_lowlevel_entity_data(
        self, cbor: FxtrCbor, target_type: EntityTypeAnnotation[UnknownEntity]
    ) -> DeserializedEntity[UnknownEntity]:
        return DeserializedEntity(
            value=UnknownEntity(raw_cbor=freeze_fxtr_cbor(cbor)), cacheable=True
        )


register_entity_load_handler(UnknownEntity, UnknownEntityLoadHandler())


@dataclasses.dataclass
class UnknownMutableEntity(Entity):
    """A fallback entity wrapping mutable entity data for an entity whose type is not known.

    Unlike UnknownEntity, this stores raw mutable FxtrCbor and is not cacheable. Use this when
    you need to inspect or modify the raw CBOR data without the overhead of freezing.
    """

    raw_cbor: FxtrCbor

    def _fxtr_store_impl(self) -> EntityRef[UnknownMutableEntity]:
        scope = get_active_scope()
        return EntityRef(scope.store_cbor(self.raw_cbor), UnknownMutableEntity)


@dataclasses.dataclass(frozen=True)
class UnknownMutableEntityLoadHandler(EntityLoadHandler[UnknownMutableEntity]):
    """Conversion handler for UnknownMutableEntity."""

    @typing.override
    async def supports_entity_type(
        self, type_id: FxtrID, target_type: EntityTypeAnnotation[UnknownMutableEntity]
    ) -> bool:
        return True

    @typing.override
    async def from_lowlevel_entity_data(
        self, cbor: FxtrCbor, target_type: EntityTypeAnnotation[UnknownMutableEntity]
    ) -> DeserializedEntity[UnknownMutableEntity]:
        return DeserializedEntity(value=UnknownMutableEntity(raw_cbor=cbor), cacheable=False)


register_entity_load_handler(UnknownMutableEntity, UnknownMutableEntityLoadHandler())


type LoadAsUnknownByDefault[T: Entity] = OverrideFxtrHandlers[T]
"""Type alias to indicate that an entity should be loaded as UnknownEntity by default.

This type alias bypasses any existing load handlers for T, and instead always loads the value as
UnknownEntity.

This is primarily useful as a type bound for generic type parameters, e.g. as

class MyClass[T: LoadAsUnknownByDefault[Entity]](...):
    some_ref: EntityRef[T]

The Python typechecker will allow MyClass[SomeEntity] for any entity type SomeEntity, and
(at least with the mashumaro conversion logic), will load MyClass[SomeEntity] using the SomeEntity
load handler for `some_ref`. However, if loaded as MyClass without any type parameters, the
conversion logic will instead load some_ref as EntityRef[LoadAsUnknownByDefault[Entity]], which
will load the value as UnknownEntity.
"""

register_entity_load_handler(
    LoadAsUnknownByDefault, UnknownEntityLoadHandler(), allow_implicit_overrides=True
)
