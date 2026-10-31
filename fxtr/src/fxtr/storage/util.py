"""Shared low-level helpers for storage engines."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

from fxtr.core import codec
from fxtr.core.entity_data import (
    StorableFxtrDataValue,
    UnknownReferenceError,
    get_entity_type_tag,
)


@dataclasses.dataclass(frozen=True)
class EncodedEntity:
    """One entity ready to be written by a storage engine.

    Attributes:
        encoded: The entity's DRISL bytes.
        type_tag: Its ``$type`` tag (the type id string).
    """

    encoded: bytes
    type_tag: str


def encode_for_store(
    data: StorableFxtrDataValue, is_known: Callable[[bytes], bool]
) -> tuple[bytes, EncodedEntity]:
    """Check and encode a value for storing.

    This is the store-side half of the storage contract, shared by engines that differ only in
    where the bytes end up: it validates the value's ``$type`` tag and requires every reference
    in it to be known already.

    Args:
        data: The value to store (any storable shape).
        is_known: Whether the engine already holds the entity with the given cid.

    Returns:
        The cid of ``data`` and the entity to write.

    Raises:
        InvalidEntityDataError: If ``data`` carries no valid tag.
        UnknownReferenceError: If a reference is not known.
    """
    tag = get_entity_type_tag(data)
    for ref in codec.iter_entity_ids(data):
        if not is_known(ref.cid):
            raise UnknownReferenceError(f"Cannot store a value referencing unknown entity {ref!r}")
    encoded = codec.encode(data)
    return codec.compute_cid(encoded), EncodedEntity(encoded, tag)
