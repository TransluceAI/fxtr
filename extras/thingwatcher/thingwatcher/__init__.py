"""Thingwatcher - A tool for watching things.

Thingwatcher is a viewer for the state of a large number of interacting objects
and async tasks. Easily instrument objects in your code and get a rich viewer
to help you navigate what is going on.

Basic usage:
    import thingwatcher as tw

    # Track an object
    tw.track(my_obj)

    # Record a state snapshot
    tw.tick("info", "something happened")

    # Pause for manual stepping
    await tw.pause(my_anchor)

    # Wait for user input
    result = await tw.receive(my_anchor)
"""

from thingwatcher.api import (  # Exceptions; Data types; ABCs/Protocols; Instrumentation API; Configuration API; Watcher
    NO_PUSHED_CHUNK,
    POP_ALL,
    AnchorCapturer,
    AsyncThingUpdate,
    BacklinkInfo,
    CborableTypes,
    FxtrSupportDisabledError,
    ImmutableStore,
    NoWatcherConnectedError,
    RendererBuilder,
    RendererBuilderResult,
    SnapshotResult,
    StatefulOwningSnapshotter,
    StatefulResult,
    StatelessResult,
    StatelessSnapshotter,
    ThingDelta,
    ThingLink,
    ThingRef,
    ThingState,
    Watcher,
    add_root,
    configure_rendering,
    pause,
    receive,
    register_anchor_capturer,
    register_immutable_store,
    register_renderer,
    register_renderer_builder,
    register_snapshotter,
    remove_root,
    streaming_update,
    tick,
    track,
    unregister_immutable_store,
    unregister_renderer_builder,
    untrack,
)

# Re-export fxtr integration for convenience
from thingwatcher.fxtr import fxtr_namespace_view, register_fxtr_renderer, track_id

__all__ = [
    # fxtr integration
    "fxtr_namespace_view",
    "register_fxtr_renderer",
    "track_id",
    # Exceptions
    "NoWatcherConnectedError",
    "FxtrSupportDisabledError",
    # Data types
    "BacklinkInfo",
    "CborableTypes",
    "SnapshotResult",
    "ThingDelta",
    "ThingLink",
    "ThingRef",
    "ThingState",
    # ThingDelta sentinels
    "NO_PUSHED_CHUNK",
    "POP_ALL",
    # Async update support
    "AsyncThingUpdate",
    "StatelessResult",
    "StatefulResult",
    # Snapshotter ABCs
    "StatelessSnapshotter",
    "StatefulOwningSnapshotter",
    # Anchor capturer protocol
    "AnchorCapturer",
    # Immutable store protocol
    "ImmutableStore",
    # Instrumentation API
    "add_root",
    "pause",
    "receive",
    "register_immutable_store",
    "remove_root",
    "streaming_update",
    "tick",
    "track",
    "unregister_immutable_store",
    "untrack",
    # Configuration API
    "configure_rendering",
    "register_anchor_capturer",
    "register_renderer",
    "register_renderer_builder",
    "register_snapshotter",
    "unregister_renderer_builder",
    # Renderer builder types
    "RendererBuilder",
    "RendererBuilderResult",
    # Watcher
    "Watcher",
]
