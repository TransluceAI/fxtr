"""Snapshotter and anchor capturer for fxtr namespace objects.

This module provides the components needed to track fxtr namespaces in thingwatcher,
producing ThingStates that show the current pins in the namespace.
"""

from __future__ import annotations

from typing import Any

from fxtr2.data.abstract_arena import PersistentPinningBackend
from fxtr2.data.entity_core import FxtrID
from thingwatcher.api import (
    AsyncThingUpdate,
    StatefulOwningSnapshotter,
    StatefulResult,
    StatelessResult,
    ThingLink,
    ThingRef,
    ThingState,
)
from thingwatcher.fxtr.cbor_transform import fxtr_key_to_thing_key


class FxtrNamespaceSnapshotter(StatefulOwningSnapshotter):
    """Snapshotter that captures the current state of an fxtr namespace.

    Uses AsyncThingUpdate to handle the async list_pins() call. On each snapshot:
    - Immediately returns a ThingState with cached pins (marked as stale)
    - Returns an async_updater that fetches fresh pins

    The resulting ThingState shows the namespace with links to each pinned entity.
    """

    def __init__(self, backend: PersistentPinningBackend, name: str):
        """Initialize the snapshotter.

        Args:
            backend: The persistent pinning backend to track
            name: Display name for this namespace in thingwatcher
        """
        self._backend = backend
        self._name = name
        self._cached_pins: dict[str, FxtrID] = {}

    def stateless_snapshot(self, obj: Any) -> AsyncThingUpdate[StatelessResult]:
        """Return async update that fetches current pins.

        The immediate update uses cached pins (marked as stale).
        The async updater fetches fresh pins from the backend.
        """
        # Immediate: use cached pins (marked as stale if we have no data yet)
        immediate = self._make_thing_state(self._cached_pins, stale=True)

        async def fetch_pins() -> ThingState:
            pins = await self._backend.list_pins()
            self._cached_pins = pins  # Update cache
            return self._make_thing_state(pins, stale=False)

        return AsyncThingUpdate(immediate_update=immediate, async_updater=fetch_pins)

    def snapshot_owned(self, obj: Any) -> AsyncThingUpdate[StatefulResult]:
        """Delegates to stateless_snapshot."""
        return self.stateless_snapshot(obj)

    def _make_thing_state(self, pins: dict[str, FxtrID], *, stale: bool) -> ThingState:
        """Build ThingState from pins dict.

        Args:
            pins: Mapping from pin names to entity IDs
            stale: Whether this data is from cache (True) or fresh fetch (False)

        Returns:
            ThingState representing the namespace with links to pinned entities
        """
        links = [
            ThingLink(
                link_type="pin",
                link_key=["pins", pin_name],
                to_thing_key=fxtr_key_to_thing_key(entity_id),
            )
            for pin_name, entity_id in sorted(pins.items())
        ]
        return ThingState(
            key=["fxtr_namespace", self._name],
            thing_type="fxtr.transluce.org/fxtr_namespace",
            thing_data={"name": self._name, "pin_count": len(pins), "stale": stale},
            links=links,
        )


class FxtrNamespaceAnchorCapturer:
    """Anchor capturer for fxtr namespace objects.

    Returns a ThingRef pointing to the namespace's thing key.
    Used when adding the namespace as a root in thingwatcher.
    """

    def __init__(self, name: str):
        """Initialize the anchor capturer.

        Args:
            name: Display name for this namespace in thingwatcher
        """
        self._name = name

    def handle(self, anchor: Any) -> ThingRef:
        """Return a reference to the namespace thing.

        Args:
            anchor: The namespace object (unused, we just return our key)

        Returns:
            ThingRef pointing to ["fxtr_namespace", name]
        """
        return ThingRef(key=["fxtr_namespace", self._name])
