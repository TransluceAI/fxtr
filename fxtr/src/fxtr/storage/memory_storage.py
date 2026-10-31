"""In-memory entity storage.

The simplest concrete :class:`FxtrEntityStorage`: it keeps every entity's serialized bytes in a
dict keyed by content id. Everything in that dict is "known"; nothing else is. It is the reference
implementation of the storage contract and the default backing for tests.
"""

from __future__ import annotations

from fxtr.core import codec
from fxtr.core.entities import BoundID, Entity, FxtrEntityStorage
from fxtr.core.entity_data import (
    EntityID,
    EntityNotFoundError,
    FxtrDataValue,
    StorableFxtrDataValue,
    get_entity_type_tag,
)
from fxtr.storage.util import encode_for_store


class MemoryStorage(FxtrEntityStorage):
    """Storage that holds serialized entity bytes in an in-process dict.

    Content ids map to their DRISL-encoded bytes. An id is known exactly when its content id is a
    key in the dict.

    Nothing here needs to await, so besides the contract's async methods it offers synchronous
    twins — :meth:`store_raw_data_sync`, :meth:`store_sync`, :meth:`get_raw_data` — for
    in-process callers that cannot (a synchronous settle loop, say).
    """

    def __init__(self) -> None:
        self._entities: dict[bytes, bytes] = {}

    def is_known(self, entity_id: EntityID) -> bool:
        return entity_id.cid in self._entities

    def store_raw_data_sync(self, data: StorableFxtrDataValue) -> EntityID:
        """Store a value synchronously; see :meth:`store_raw_data`."""
        cid, entity = encode_for_store(data, self._entities.__contains__)
        self._entities[cid] = entity.encoded
        return EntityID(cid=cid)

    async def store_raw_data(self, data: StorableFxtrDataValue) -> EntityID:
        return self.store_raw_data_sync(data)

    def store_sync[T: Entity](self, entity: T) -> BoundID[T]:
        """Store an entity synchronously; see
        :meth:`~fxtr.core.entities.FxtrEntityStorage.store`."""
        entity_id = self.store_raw_data_sync(entity._fxtr_serialize())
        return BoundID(entity_id=entity_id, loader=entity._fxtr_loader())

    def get_raw_data(self, entity_id: EntityID) -> FxtrDataValue:
        """Load an entity's data synchronously; see :meth:`load_raw_data`.

        Raises:
            EntityNotFoundError: If the entity is not held.
        """
        try:
            encoded = self._entities[entity_id.cid]
        except KeyError:
            raise EntityNotFoundError(f"Entity not found in storage: {entity_id!r}") from None
        data = codec.decode(encoded)
        get_entity_type_tag(data)
        return data

    async def load_raw_data(self, entity_id: EntityID) -> FxtrDataValue:
        return self.get_raw_data(entity_id)
