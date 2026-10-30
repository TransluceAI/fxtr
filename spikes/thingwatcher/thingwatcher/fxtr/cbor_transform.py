"""Transform fxtr CBOR entities to thingwatcher ThingStates.

This module provides utilities for converting fxtr entity data into a format
suitable for thingwatcher visualization. The main function is `fxtr_entity_to_thing_state()`,
which transforms an fxtr entity into a ThingState with:
- JSON-compatible data (fxtr IDs become marker objects)
- ThingLinks for each ID reference in the data
- A thing_type derived from the entity's node type and $fxtr.type field
"""

from __future__ import annotations

from typing import Any

from fxtr2.data.entity_core import (
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawStrongFxtrID,
    RawWeakFxtrID,
)
from thingwatcher.api import ThingLink, ThingState


def fxtr_key_to_thing_key(entity_id: FxtrID) -> list:
    """Convert FxtrID to thingwatcher key: ["fxtr_entity", id_string]."""
    return ["fxtr_entity", entity_id.id_string()]


def thing_key_to_fxtr_id(thing_key: Any) -> FxtrID | None:
    """Parse thingwatcher key back to FxtrID, or None if invalid.

    Args:
        thing_key: A thingwatcher key, expected to be ["fxtr_entity", id_string]

    Returns:
        The parsed FxtrID, or None if the key doesn't match the expected format
        or the id_string is invalid.
    """
    if not isinstance(thing_key, list) or len(thing_key) != 2:
        return None
    if thing_key[0] != "fxtr_entity":
        return None
    id_string = thing_key[1]
    if not isinstance(id_string, str):
        return None
    try:
        return FxtrID.from_id_string(id_string)
    except ValueError:
        return None


def _get_link_type_for_raw_id(
    raw_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
) -> str:
    """Get the link type string for a raw fxtr ID wrapper."""
    if isinstance(raw_id, RawWeakFxtrID):
        return "weak"
    elif isinstance(raw_id, RawStrongFxtrID):
        return "strong"
    elif isinstance(raw_id, RawFxtrEntityMarkerID):
        return "marker"
    else:
        raise TypeError(f"Unknown raw ID type: {type(raw_id)}")


def _get_marker_key_for_raw_id(
    raw_id: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID,
) -> str:
    """Get the JSON marker key for a raw fxtr ID wrapper."""
    if isinstance(raw_id, RawWeakFxtrID):
        return "$!fxtr.link.weak"
    elif isinstance(raw_id, RawStrongFxtrID):
        return "$!fxtr.link.strong"
    elif isinstance(raw_id, RawFxtrEntityMarkerID):
        return "$!fxtr.link.marker"
    else:
        raise TypeError(f"Unknown raw ID type: {type(raw_id)}")


def _transform_cbor_value(
    value: FxtrCbor,
    current_path: list,
    links: list[ThingLink],
) -> Any:
    """Recursively transform CBOR value to JSON, extracting links.

    Args:
        value: The CBOR value to transform
        current_path: The path from the root to this value (for link_key)
        links: List to append extracted links to (mutated in place)

    Returns:
        JSON-compatible value with fxtr IDs replaced by marker objects
    """
    if isinstance(value, (RawWeakFxtrID, RawStrongFxtrID, RawFxtrEntityMarkerID)):
        # Extract link
        inner_id = value.inner_id
        link_type = _get_link_type_for_raw_id(value)
        marker_key = _get_marker_key_for_raw_id(value)
        thing_key = fxtr_key_to_thing_key(inner_id)

        links.append(
            ThingLink(
                link_type=link_type,
                link_key=list(current_path),  # Copy to avoid mutation issues
                to_thing_key=thing_key,
            )
        )

        # Return marker object
        return {marker_key: thing_key}

    elif isinstance(value, dict):
        result = {}
        for k, v in value.items():
            result[k] = _transform_cbor_value(v, current_path + [k], links)
        return result

    elif isinstance(value, list):
        result = []
        for i, v in enumerate(value):
            result.append(_transform_cbor_value(v, current_path + [i], links))
        return result

    elif isinstance(value, bytes):
        # Convert bytes to base64 string for JSON compatibility
        import base64

        return {"$!bytes": base64.b64encode(value).decode("ascii")}

    elif isinstance(value, (str, int, float, bool)) or value is None:
        return value

    else:
        # Fallback for any unexpected types
        return {"$!unknown": str(type(value).__name__), "repr": repr(value)}


def _derive_thing_type(entity_id: FxtrID, cbor_data: FxtrCbor) -> str:
    """Derive the thing_type for an entity.

    For d-node entities that have a $fxtr.type field, returns:
        "fxtr.transluce.org/fxtr_entity/d/<type_id_string>"

    For other entities:
        "fxtr.transluce.org/fxtr_entity_fallback"
    """
    tag = entity_id.get_tag()

    # Only d-nodes can have typed thing_types
    if tag != "d":
        return "fxtr.transluce.org/fxtr_entity_fallback"

    # Check for $fxtr.type field
    if not isinstance(cbor_data, dict):
        return "fxtr.transluce.org/fxtr_entity_fallback"

    fxtr_type = cbor_data.get("$fxtr.type")
    if fxtr_type is None:
        return "fxtr.transluce.org/fxtr_entity_fallback"

    # Extract the inner ID from the raw wrapper
    if isinstance(fxtr_type, (RawWeakFxtrID, RawStrongFxtrID, RawFxtrEntityMarkerID)):
        type_id = fxtr_type.inner_id
        return f"fxtr.transluce.org/fxtr_entity/d/{type_id.id_string()}"

    return "fxtr.transluce.org/fxtr_entity_fallback"


def fxtr_entity_to_thing_state(
    entity_id: FxtrID,
    cbor_data: FxtrCbor,
) -> ThingState:
    """Transform an fxtr entity to a thingwatcher ThingState.

    This is the main transformation function. It:
    1. Derives the thing_type from the entity's node type and $fxtr.type
    2. Recursively transforms CBOR data to JSON, converting fxtr IDs to marker objects
    3. Extracts ThingLinks for each fxtr ID reference found
    4. Returns a complete ThingState ready for thingwatcher

    Args:
        entity_id: The FxtrID of the entity
        cbor_data: The loaded CBOR data for the entity

    Returns:
        A ThingState with key, thing_type, thing_data (JSON), and links
    """
    links: list[ThingLink] = []
    thing_data = _transform_cbor_value(cbor_data, [], links)
    thing_type = _derive_thing_type(entity_id, cbor_data)

    return ThingState(
        key=fxtr_key_to_thing_key(entity_id),
        thing_type=thing_type,
        thing_data=thing_data,
        links=links,
    )
