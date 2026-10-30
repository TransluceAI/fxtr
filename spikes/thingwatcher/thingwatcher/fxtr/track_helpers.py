"""Convenience functions for tracking fxtr entities in thingwatcher.

This module provides the track_id() function which allows snapshotters to
easily link to fxtr entities without requiring users to set up explicit
namespace views.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from thingwatcher.api import NoWatcherConnectedError
from thingwatcher.context import get_current_watcher

if TYPE_CHECKING:
    from fxtr2.data.entity_core import FxtrID
    from fxtr2.data.fxtr_scope import FxtrScope


def track_id(
    fxtr_id: FxtrID,
    from_scope: FxtrScope | None = None,
) -> list:
    """Track an fxtr entity for visualization in thingwatcher.

    This queues the entity for async copy into an internal namespace,
    making it available for graph navigation. The returned thing_key
    can be used in ThingLink.to_thing_key.

    Example usage in a snapshotter:
        class MySnapshotter(StatelessSnapshotter):
            def snapshot(self, obj) -> ThingState:
                return ThingState(
                    key=["my_thing", obj.id],
                    thing_type="my_type",
                    thing_data={"name": obj.name},
                    links=[
                        ThingLink(
                            link_type="fxtr_ref",
                            link_key=["data"],
                            to_thing_key=track_id(obj.fxtr_data_id),
                        )
                    ],
                )

    Args:
        fxtr_id: The fxtr entity ID to track.
        from_scope: Scope to copy from. If None, uses ACTIVE_SCOPE.

    Returns:
        The thingwatcher thing_key: ["fxtr_entity", id_string]

    Raises:
        NoWatcherConnectedError: If no watcher is active.
        FxtrSupportDisabledError: If active watcher has fxtr_support=False.
        ValueError: If fxtr_id is not tracked in the source scope.
    """
    watcher = get_current_watcher()
    if watcher is None:
        raise NoWatcherConnectedError("track_id requires an active watcher")

    return watcher.track_fxtr_id(fxtr_id, from_scope)
