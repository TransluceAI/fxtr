"""CBOR-based transfer formats for entity streams.

Two related wire formats live here:

- Full entity streams for pull/import operations:
  [fxtr_id_bytes, entity_bytes_or_null, [[dep_id_bytes, dep_kind_int], ...]]
- Push entity streams for upload operations:
  [fxtr_id_bytes, entity_bytes_or_null]

Entities are expected in topological order (deps before dependents).
"""

from __future__ import annotations

import io

import cbor2

from fxtr2.data.abstract_arena import DepKind, FxtrEntityFlatData
from fxtr2.data.entity_core import FxtrID

# DepKind -> integer mapping for compact serialization
_DEP_KIND_TO_INT: dict[DepKind, int] = {
    DepKind.STRONG_REF: 0,
    DepKind.WEAK_REF: 1,
    DepKind.CTX_NODE: 2,
    DepKind.BACKSTEP: 3,
    DepKind.UNWRAPS_TO: 4,
}

_INT_TO_DEP_KIND: dict[int, DepKind] = {v: k for k, v in _DEP_KIND_TO_INT.items()}


def encode_entity_stream(entities: list[FxtrEntityFlatData]) -> bytes:
    """Encode a list of entities as a concatenated CBOR stream.

    Each entity becomes a CBOR array:
      [fxtr_id_bytes, entity_bytes_or_null, [[dep_id_bytes, dep_kind_int], ...]]
    """
    buf = io.BytesIO()
    encoder = cbor2.CBOREncoder(buf)
    for entity in entities:
        dep_list = [[bytes(dep_id), _DEP_KIND_TO_INT[dep_kind]] for dep_id, dep_kind in entity.deps]
        record = [
            bytes(entity.entity_id),
            entity.entity_bytes,
            dep_list,
        ]
        encoder.encode(record)
    return buf.getvalue()


def encode_push_entity_stream(entities: list[FxtrEntityFlatData]) -> bytes:
    """Encode entities for push uploads without dependency metadata."""
    buf = io.BytesIO()
    encoder = cbor2.CBOREncoder(buf)
    for entity in entities:
        encoder.encode([bytes(entity.entity_id), entity.entity_bytes])
    return buf.getvalue()


def decode_entity_stream(data: bytes) -> list[FxtrEntityFlatData]:
    """Decode a CBOR entity stream back into FxtrEntityFlatData objects.

    Raises ValueError if the data is not valid CBOR or doesn't match the
    expected entity record structure.
    """
    if not data:
        return []

    entities: list[FxtrEntityFlatData] = []
    decoder = cbor2.CBORDecoder(io.BytesIO(data))

    while True:
        try:
            record = decoder.decode()
        except cbor2.CBORDecodeEOF:
            break
        except cbor2.CBORError as exc:
            raise ValueError(f"Malformed CBOR entity stream: {exc}") from exc

        try:
            raw_id_bytes, entity_bytes, dep_list = record  # type: ignore[misc]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid entity record structure: {exc}") from exc

        entity_id = FxtrID(raw_data_id=bytes(raw_id_bytes))
        try:
            deps: list[tuple[FxtrID, DepKind]] = [
                (FxtrID(raw_data_id=bytes(dep_id_bytes)), _INT_TO_DEP_KIND[dep_kind_int])
                for dep_id_bytes, dep_kind_int in dep_list
            ]
        except KeyError as exc:
            raise ValueError(f"Unknown dependency kind: {exc}") from exc

        entities.append(
            FxtrEntityFlatData(
                entity_id=entity_id,
                entity_bytes=bytes(entity_bytes) if entity_bytes is not None else None,
                deps=deps,
            )
        )

    if not entities:
        raise ValueError("CBOR stream contained no entity records")

    return entities


def decode_push_entity_stream(data: bytes) -> list[FxtrEntityFlatData]:
    """Decode a push entity stream back into flat-data objects with empty deps."""
    if not data:
        return []

    entities: list[FxtrEntityFlatData] = []
    decoder = cbor2.CBORDecoder(io.BytesIO(data))

    while True:
        try:
            record = decoder.decode()
        except cbor2.CBORDecodeEOF:
            break
        except cbor2.CBORError as exc:
            raise ValueError(f"Malformed CBOR push entity stream: {exc}") from exc

        try:
            raw_id_bytes, entity_bytes = record  # type: ignore[misc]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid push entity record structure: {exc}") from exc

        entities.append(
            FxtrEntityFlatData(
                entity_id=FxtrID(raw_data_id=bytes(raw_id_bytes)),
                entity_bytes=bytes(entity_bytes) if entity_bytes is not None else None,
                deps=[],
            )
        )

    if not entities:
        raise ValueError("CBOR push stream contained no entity records")

    return entities
