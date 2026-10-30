"""Thingwatcher integration for fxtr2.

This module provides visualization of fxtr entities through thingwatcher's debugging UI.

Basic usage:
    from thingwatcher.fxtr import fxtr_namespace_view

    async with watcher.watching_and_serving():
        async with fxtr_namespace_view(namespace, "my_namespace", watcher):
            watcher.tick("info", "Initial state")
            # ... use the watcher normally ...

See thingwatcher/fxtr/LOCAL_CODE_OVERVIEW.md for detailed documentation.
"""

from thingwatcher.fxtr.cbor_transform import (
    fxtr_entity_to_thing_state,
    fxtr_key_to_thing_key,
    thing_key_to_fxtr_id,
)
from thingwatcher.fxtr.immutable_store import FxtrImmutableStore
from thingwatcher.fxtr.namespace_view import fxtr_namespace_view
from thingwatcher.fxtr.renderers import (
    fxtr_renderer_builder,
    register_fxtr_renderer,
    unregister_fxtr_renderer,
)
from thingwatcher.fxtr.snapshotter import FxtrNamespaceAnchorCapturer, FxtrNamespaceSnapshotter
from thingwatcher.fxtr.track_helpers import track_id

__all__ = [
    # Main entry point
    "fxtr_namespace_view",
    # Tracking helpers
    "track_id",
    # Key conversion
    "fxtr_key_to_thing_key",
    "thing_key_to_fxtr_id",
    # Transformation
    "fxtr_entity_to_thing_state",
    # Stores
    "FxtrImmutableStore",
    # Snapshotters
    "FxtrNamespaceSnapshotter",
    "FxtrNamespaceAnchorCapturer",
    # Renderers
    "register_fxtr_renderer",
    "unregister_fxtr_renderer",
    "fxtr_renderer_builder",
]
