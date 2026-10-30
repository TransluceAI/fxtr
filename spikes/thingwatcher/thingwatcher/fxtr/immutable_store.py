"""ImmutableStore implementation for fxtr entities.

This module implements thingwatcher's ImmutableStore protocol for fxtr entities,
providing lazy access to entities stored in an ObservableStore backend.
"""

from __future__ import annotations

from typing import Any

from fxtr2.data.abstract_arena import EntityBackref, ObservableStore, OtherBackref
from thingwatcher.api import BacklinkInfo, ThingState
from thingwatcher.fxtr.cbor_transform import (
    fxtr_entity_to_thing_state,
    fxtr_key_to_thing_key,
    thing_key_to_fxtr_id,
)


class FxtrImmutableStore:
    """ImmutableStore that provides lazy access to fxtr entities.

    This store looks up entities by thingwatcher key (["fxtr_entity", id_string])
    and returns them as ThingStates. It also provides backlink information using
    the ObservableStore's backrefs() method.
    """

    def __init__(self, backend: ObservableStore):
        """Initialize the store.

        Args:
            backend: An ObservableStore that supports try_lookup() and backrefs()
        """
        self._backend = backend

    async def get_thing_state(self, key: Any) -> ThingState | None:
        """Look up an fxtr entity by thingwatcher key.

        Args:
            key: The thingwatcher key, expected to be ["fxtr_entity", id_string]

        Returns:
            ThingState if found, None if not in this store or invalid key format.
        """
        # Parse key to FxtrID
        fxtr_id = thing_key_to_fxtr_id(key)
        if fxtr_id is None:
            return None

        # Check for contentless node types (ground "." and builtin "!")
        tag = fxtr_id.get_tag()
        if tag in (".", "!"):
            # Return a minimal ThingState for contentless nodes
            return _make_contentless_thing_state(fxtr_id, tag)

        # Look up the entity
        arena = await self._backend.try_lookup(fxtr_id)
        if arena is None:
            return None

        # Load the entity data
        try:
            cbor_data = await arena.load(fxtr_id)
        finally:
            # Close the temporary arena to release resources
            arena.close()

        # Transform to ThingState
        return fxtr_entity_to_thing_state(fxtr_id, cbor_data)

    async def get_backlinks(self, key: Any) -> list[BacklinkInfo]:
        """Get backlinks to an entity from other entities in this store.

        Uses the ObservableStore's backrefs() method to find entities that
        reference the given entity.

        Args:
            key: The thingwatcher key to find backlinks for

        Returns:
            List of BacklinkInfo for entities that link to the given key.
        """
        # Parse key to FxtrID
        fxtr_id = thing_key_to_fxtr_id(key)
        if fxtr_id is None:
            return []

        # Get backrefs from the store
        backrefs = await self._backend.backrefs(fxtr_id)

        # Convert to BacklinkInfo
        result: list[BacklinkInfo] = []
        for backref in backrefs:
            if isinstance(backref, EntityBackref):
                # Convert EntityBackref to BacklinkInfo
                from_thing_key = fxtr_key_to_thing_key(backref.referrer)

                # Map backref kind to link type
                link_type = _backref_kind_to_link_type(backref.kind)

                result.append(
                    BacklinkInfo(
                        from_thing_key=from_thing_key,
                        link_type=link_type,
                        # We don't have the exact link_key (path in the referrer's data)
                        # so we use the backref kind as a marker
                        link_key=["backref", backref.kind],
                    )
                )
            elif isinstance(backref, OtherBackref):
                # OtherBackref represents non-entity sources (like pins)
                # We don't convert these to BacklinkInfo since they don't
                # have a thing key in our store
                pass

        return result


def _backref_kind_to_link_type(kind: str) -> str:
    """Map EntityBackref kind to thingwatcher link type."""
    match kind:
        case "strong_ref":
            return "strong"
        case "weak_ref":
            return "weak"
        case "unwrap":
            return "unwrap"
        case "context_data":
            return "context_data"
        case _:
            return kind  # Pass through unknown kinds


def _make_contentless_thing_state(fxtr_id: Any, tag: str) -> ThingState:
    """Create a ThingState for contentless node types (ground and builtin).

    Contentless nodes don't have data to load - they are identified purely
    by their tag and payload.

    Args:
        fxtr_id: The FxtrID of the contentless node
        tag: The node's tag ("." for ground, "!" for builtin)

    Returns:
        A ThingState with minimal payload describing the node type
    """
    thing_key = fxtr_key_to_thing_key(fxtr_id)

    if tag == ".":
        # Ground node - the root of all contexts
        return ThingState(
            key=thing_key,
            thing_type="fxtr.transluce.org/fxtr_entity_fallback",
            thing_data={
                "$fxtr.node_type": "ground",
                "$fxtr.description": "Ground node (root context)",
            },
            links=[],
        )
    elif tag == "!":
        # Builtin node - named type or constant
        _, payload = fxtr_id.split_tag_and_payload()
        builtin_name = payload.decode("utf-8")
        return ThingState(
            key=thing_key,
            thing_type="fxtr.transluce.org/fxtr_entity_fallback",
            thing_data={
                "$fxtr.node_type": "builtin",
                "$fxtr.builtin_name": builtin_name,
            },
            links=[],
        )
    else:
        # Shouldn't happen, but handle gracefully
        return ThingState(
            key=thing_key,
            thing_type="fxtr.transluce.org/fxtr_entity_fallback",
            thing_data={
                "$fxtr.node_type": f"unknown_contentless ({tag})",
            },
            links=[],
        )
