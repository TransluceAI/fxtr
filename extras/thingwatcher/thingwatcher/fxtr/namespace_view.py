"""Main context manager for mounting fxtr namespaces in thingwatcher.

This module provides the primary API for integrating fxtr2 with thingwatcher,
allowing users to visualize fxtr entity graphs through thingwatcher's UI.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fxtr2.data.abstract_arena import ObservableStore, PersistentPinningBackend
from thingwatcher.api import (
    register_anchor_capturer,
    register_renderer_builder,
    register_snapshotter,
    unregister_renderer_builder,
)
from thingwatcher.context import get_current_watcher
from thingwatcher.fxtr.immutable_store import FxtrImmutableStore
from thingwatcher.fxtr.snapshotter import FxtrNamespaceAnchorCapturer, FxtrNamespaceSnapshotter
from thingwatcher.watcher import Watcher

if TYPE_CHECKING:
    from fxtr2.data.fxtr_context_api import FxtrNamespace


# Sentinel class for namespace object tracking
class _NamespaceMarker:
    """Marker class used for snapshotter/anchor capturer registration."""


@asynccontextmanager
async def fxtr_namespace_view(
    namespace: "PersistentPinningBackend | FxtrNamespace",
    name: str,
    watcher: Watcher | None = None,
):
    """Async context manager that mounts an fxtr namespace in thingwatcher.

    While active, registers:
    - An ImmutableStore for accessing fxtr entities
    - A snapshotter/anchor capturer for the namespace object
    - A root entry point for the namespace
    - A renderer builder for fxtr entity types

    On exit, unregisters everything.

    Args:
        namespace: The fxtr namespace to visualize. Can be either:
            - A PersistentPinningBackend (must also implement ObservableStore)
            - A FxtrNamespace (wraps PersistentPinningBackend with a scope)
            If FxtrNamespace is provided, uses its underlying backend.
        name: Display name for this namespace in thingwatcher
        watcher: The watcher to attach to. If None, uses the active watcher from context.

    Yields:
        The namespace object (pass-through for convenience)

    Raises:
        ValueError: If no watcher is active and none was provided
        TypeError: If the backend doesn't implement ObservableStore

    Example:
        async with watcher.watching_and_serving():
            async with fxtr_namespace_view(ns, "my_namespace", watcher):
                watcher.tick("info", "Initial state")
                # ... use the watcher normally ...
    """
    # Import here to avoid circular imports
    from fxtr2.data.fxtr_context_api import FxtrNamespace as FxtrNamespaceClass

    # Extract backend if FxtrNamespace provided
    if isinstance(namespace, FxtrNamespaceClass):
        backend = namespace._get_backend()
    else:
        backend = namespace

    # Verify backend implements ObservableStore
    if not isinstance(backend, ObservableStore):
        raise TypeError(f"Backend must implement ObservableStore, got {type(backend).__name__}")

    # Get watcher
    if watcher is None:
        watcher = get_current_watcher()
        if watcher is None:
            raise ValueError(
                "No watcher provided and no active watcher in context. "
                "Either pass a watcher or run inside watcher.watching_and_serving()."
            )

    # Create components
    immutable_store = FxtrImmutableStore(backend)
    snapshotter = FxtrNamespaceSnapshotter(backend, name)
    anchor_capturer = FxtrNamespaceAnchorCapturer(name)

    # Import renderer builder (lazy import to avoid issues if not needed)
    from thingwatcher.fxtr.renderers import fxtr_renderer_builder

    # Create a unique type name for this namespace instance
    typename = f"fxtr_namespace_view.{name}"

    # Create a marker object for tracking
    namespace_marker = _NamespaceMarker()

    try:
        # Register components
        register_snapshotter(typename, snapshotter)
        register_anchor_capturer(typename, anchor_capturer)
        watcher.register_immutable_store(f"fxtr:{name}", immutable_store)
        register_renderer_builder(fxtr_renderer_builder)

        # Track the marker and add as root
        watcher.track(namespace_marker, typename)
        watcher.add_root(name, namespace_marker, typename)

        yield namespace

    finally:
        # Cleanup
        try:
            watcher.remove_root(name)
        except KeyError:
            pass  # Root may already be removed

        watcher.untrack(namespace_marker)

        try:
            watcher.unregister_immutable_store(f"fxtr:{name}")
        except KeyError:
            pass  # Store may already be unregistered

        try:
            unregister_renderer_builder(fxtr_renderer_builder)
        except ValueError:
            pass  # Builder may already be unregistered
