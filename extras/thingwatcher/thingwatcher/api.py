"""Public API for thingwatcher.

This module contains all the public interfaces for instrumenting code with thingwatcher
and configuring how objects are serialized and displayed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Generic, Mapping, Protocol, Sequence, TypeVar

from .context import get_current_watcher
from .registry import RendererBuilder, RendererBuilderResult
from .registry import configure_rendering as _configure_rendering
from .registry import register_anchor_capturer as _register_anchor_capturer
from .registry import register_renderer as _register_renderer
from .registry import register_renderer_builder as _register_renderer_builder
from .registry import register_snapshotter as _register_snapshotter
from .registry import unregister_renderer_builder as _unregister_renderer_builder

# Type alias for CBOR-serializable types
CborableTypes = Any  # In practice: str, int, float, bytes, list, dict, bool, None


class NoWatcherConnectedError(Exception):
    """Raised when receive() is called without an active watcher."""


class FxtrSupportDisabledError(Exception):
    """Raised when fxtr features are used but fxtr_support=False on the Watcher."""


# =============================================================================
# Data Types
# =============================================================================


@dataclass
class ThingLink:
    """A link from one thing to another.

    The from_thing_key is implicit (derived from the ThingState that contains this link).
    """

    link_type: str
    link_key: CborableTypes
    to_thing_key: CborableTypes


@dataclass
class ThingState:
    """Complete state of a thing at a point in time."""

    key: CborableTypes
    thing_type: str
    thing_data: CborableTypes
    links: list[ThingLink]


class _NoPushedChunk:
    """Sentinel indicating no chunk should be pushed.

    Use the NO_PUSHED_CHUNK singleton instance, not this class directly.
    """

    _instance: "_NoPushedChunk | None" = None

    def __new__(cls) -> "_NoPushedChunk":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "NO_PUSHED_CHUNK"


NO_PUSHED_CHUNK = _NoPushedChunk()


class _PopAll:
    """Sentinel indicating all chunks should be popped.

    Use the POP_ALL singleton instance, not this class directly.
    """

    _instance: "_PopAll | None" = None

    def __new__(cls) -> "_PopAll":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "POP_ALL"


POP_ALL = _PopAll()


@dataclass
class ThingDelta:
    """A change in a thing's state since the last snapshot.

    Data operations are controlled by `pop_chunks` and `push_chunk`:
    - No data change: pop_chunks=0, push_chunk=NO_PUSHED_CHUNK
    - Append: pop_chunks=0, push_chunk=data
    - Replace (fresh chain): pop_chunks=POP_ALL, push_chunk=data
    - Pop N then append: pop_chunks=N, push_chunk=data
    - Pop N (repoint): pop_chunks=N, push_chunk=NO_PUSHED_CHUNK (if chunks remain)

    Note: `None` is valid CBOR data and will push a chunk containing `None`.
    Use `NO_PUSHED_CHUNK` sentinel to indicate no chunk should be pushed.
    """

    key: CborableTypes

    # Data operations - default is no change
    pop_chunks: int | _PopAll = field(default=0, kw_only=True)
    """Number of most recent chunks to remove before pushing, or POP_ALL.

    Must be >= 0. After all operations, at least one chunk must remain.
    """

    push_chunk: CborableTypes | _NoPushedChunk = field(default=NO_PUSHED_CHUNK, kw_only=True)
    """Data to push as a new chunk, or NO_PUSHED_CHUNK to not push anything.

    Note: `None` is valid CBOR data. Use NO_PUSHED_CHUNK sentinel for "no push".
    """

    # Link operations
    added_links: list[ThingLink] = field(default_factory=list, kw_only=True)
    removed_link_keys: list[CborableTypes] = field(default_factory=list, kw_only=True)


@dataclass
class ThingRef:
    """A reference to an already-tracked thing."""

    key: CborableTypes


@dataclass
class BacklinkInfo:
    """Information about a backlink from a thing in an immutable store.

    Used by ImmutableStore.get_backlinks() to report which things in the store
    link to a given key.
    """

    from_thing_key: CborableTypes  # Key of the thing that has this link
    link_type: str
    link_key: CborableTypes


# Type aliases for snapshotter result types
type StatelessResult = ThingState | Sequence[ThingState]
"""Result type for stateless snapshots (no ThingDelta allowed)."""

type StatefulResult = ThingState | ThingDelta | Sequence[ThingState | ThingDelta]
"""Result type for stateful snapshots (ThingDelta allowed)."""


T = TypeVar("T", bound=StatelessResult | StatefulResult, covariant=True)


@dataclass(frozen=True)
class AsyncThingUpdate(Generic[T]):
    """Snapshot result that requires async work to compute final state.

    The immediate_update is recorded at the current tick. The async_updater
    runs in the background and its result appears in a subsequent streaming tick.

    Generic over T (the immediate_update type):
    - For stateless_snapshot: T = StatelessResult (ThingState only)
    - For snapshot_owned: T = StatefulResult (ThingState or ThingDelta)

    The async_updater can always return the full StatefulResult (including
    ThingDelta) regardless of which method returns this, since by the time
    it completes we're doing a streaming update.

    If another tick() or streaming_update() for this object occurs before
    async_updater completes, it is cancelled and its result is discarded.
    The snapshotter should not commit changes to internal state until after
    its final await.
    """

    immediate_update: T
    async_updater: Callable[[], Awaitable[StatefulResult]]


# =============================================================================
# Protocols
# =============================================================================


class StatelessSnapshotter(ABC):
    """Snapshotter that returns ThingStates only.

    Multiple stateless snapshotters (or anchor capturers) may return the same thing key,
    as long as the ThingState values are identical. This is useful for "derived" or
    "computed" things that multiple sources might independently produce.

    Snapshotters are allowed to maintain their own internal state, usually weakly keyed by the
    object, to efficiently track what has changed.
    """

    @abstractmethod
    def snapshot(self, obj: Any) -> ThingState | Sequence[ThingState]:
        """Snapshot a possibly-mutable object at a given point in time.

        Args:
            obj: An object we are tracking.

        Returns:
            The current state of the object as a ThingState, or a list of ThingStates
            if this Python object maps to multiple logical things.
        """
        ...


class StatefulOwningSnapshotter(ABC):
    """Snapshotter that exclusively owns its output keys.

    Can return ThingState or ThingDelta. It is an error if any key returned by this
    snapshotter is also returned by another snapshotter or anchor capturer. This
    exclusive ownership enables delta-based updates and streaming subticks.

    Both snapshot methods can also return AsyncThingUpdate for async work:
    - The immediate_update appears at the current tick
    - The async_updater runs in background; result appears in a streaming tick
    - Async updates are cancelled if the object is snapshotted again

    Snapshotters are allowed to maintain their own internal state, usually weakly keyed by the
    object, to efficiently compute deltas.

    Implementations must provide both `snapshot_owned()` for normal snapshots and
    `stateless_snapshot()` for hard recapture operations.
    """

    @abstractmethod
    def snapshot_owned(self, obj: Any) -> StatefulResult | AsyncThingUpdate[StatefulResult]:
        """Snapshot a possibly-mutable object at a given point in time.

        Args:
            obj: An object we are tracking.

        Returns:
            Either the current state of the object, or a delta describing changes since
            the last snapshot. Can also output a list if this Python object maps to
            multiple logical things. All returned keys are exclusively owned by this
            snapshotter.

            Can also return AsyncThingUpdate for async work - the immediate_update
            appears at the current tick, and the async_updater's result appears
            in a subsequent streaming tick.
        """
        ...

    @abstractmethod
    def stateless_snapshot(self, obj: Any) -> StatelessResult | AsyncThingUpdate[StatelessResult]:
        """Stateless snapshot for hard recapture - must return only ThingStates.

        This is called during hard recapture to get a fresh baseline without relying
        on any internal state or previous snapshots. The implementation should return
        the same keys as `snapshot_owned()` would, but always as complete ThingStates
        rather than deltas.

        Args:
            obj: An object we are tracking.

        Returns:
            The current state of the object as ThingState(s). Must not return ThingDelta.

            Can also return AsyncThingUpdate for async work - the immediate_update
            must be ThingState(s), and the async_updater can return ThingDelta.
        """
        ...


class AnchorCapturer(Protocol):
    """Handler that determines how to serialize an anchor object (for pause/receive/roots)."""

    def handle(self, anchor: Any) -> ThingState | ThingRef:
        """Snapshot an anchor object.

        Args:
            anchor: An anchor object passed to `pause`, `receive`, or `add_root`.

        Returns:
            Either the current state of the object, or a reference to an already-snapshotted thing.
            `ThingRef` can be used if `anchor` is also a tracked object, and we expect its state
            to be already captured by a `StateSnapshotter`.
        """
        ...


class ImmutableStore(Protocol):
    """Protocol for immutable object stores.

    Stores provide lazy access to things that are assumed constant over time.
    Things are looked up by key at query time, not captured during ticks.

    All methods are async to support disk/network-backed stores.

    Key properties:
    - Things in stores are assumed immutable (no temporal tracking needed)
    - Store lookup happens at query time, not during tick capture
    - Snapshot things override store things if the same key exists in both
    - Store-to-store conflicts require identical states
    """

    async def get_thing_state(self, key: Any) -> ThingState | None:
        """Look up a thing by its key.

        Args:
            key: The thing key (CBOR-serializable, will be passed as-is)

        Returns:
            ThingState if found, None if not in this store.
            The returned ThingState should have single-chunk data (no multi-chunk).
        """
        ...

    async def get_backlinks(self, key: Any) -> list[BacklinkInfo]:
        """Get backlinks to a thing from other things in this store.

        The contract:
        - Not required to return anything (default: empty list)
        - If returning backlinks, the SOURCE of each backlink must be in this store
        - Used to find things in the store that link to a given key

        Args:
            key: The thing key to find backlinks for

        Returns:
            List of BacklinkInfo for things in this store that link to the given key.
        """
        ...


# =============================================================================
# Instrumentation API
# =============================================================================


def track(object: Any, typename: str | type[Any] | None = None) -> None:
    """Tracks an object with thingwatcher.

    No-op unless a thingwatcher is active.

    All tracked objects must be weakref-referenceable.

    Args:
        object: The object to track. Will be tracked until the object is finalized or explicitly
            untracked.
        typename: The name of the type thingwatcher should use to resolve the object. Defaults to
            the module.qualname of the object's type.

    Raises:
        ValueError: If no snapshotter is registered for the typename (when watcher is active).
    """
    watcher = get_current_watcher()
    if watcher is not None:
        watcher.track(object, typename)


def untrack(object: Any) -> None:
    """Untrack an object if it is being tracked.

    Usually not necessary due to weakref-based tracking.

    Args:
        object: The object to untrack.
    """
    watcher = get_current_watcher()
    if watcher is not None:
        watcher.untrack(object)


def tick(level: str, desc: str | None = None) -> None:
    """Advance the thingwatcher with a given level and message.

    At each tick, if there is an active watcher, that watcher polls all of the objects to update
    their state and sends it off.

    No-op unless a thingwatcher is active.

    IMPORTANT: tick() must NEVER raise an exception when called with valid arguments.
    If a snapshotter raises an exception or returns invalid data (e.g., invalid ThingDelta),
    the error is captured as a "broken thing" rather than propagating to the caller.
    This ensures instrumented code can always continue running.

    Args:
        level: The level of the tick, for later filtering.
        desc: Optional description of what happened leading up to this tick.
    """
    watcher = get_current_watcher()
    if watcher is not None:
        watcher.tick(level, desc)


def streaming_update(obj: Any) -> None:
    """Update a single tracked object in streaming fashion.

    This creates or updates a "stream" tick that only reflects changes to
    this specific object. Multiple streaming updates to the same stream tick
    overwrite previous state for this object while preserving other objects.

    The object must be tracked and have a StatefulOwningSnapshotter.

    If no watcher is active, this is a no-op (same as tick()).

    May raise ValueError if `obj` is misconfigured (not tracked, no owning snapshotter).
    However, snapshotter errors and invalid ThingDelta values are captured as "broken
    things" rather than propagating to the caller.

    Raises:
        ValueError: If object is not tracked or doesn't have an owning snapshotter.

    Args:
        obj: A tracked object with an owning snapshotter.
    """
    watcher = get_current_watcher()
    if watcher is not None:
        watcher.streaming_update(obj)


async def pause(
    anchor: Any,
    typename: str | type[Any] | None = None,
    tick_after: bool = True,
) -> None:
    """Pause execution of the given task until given the go-ahead by thingwatcher.

    No-op if there is no active watcher.

    Args:
        anchor: An object that describes the pause reason. Similar to `track`, but the object will
            only be tracked for the duration of the pause.
        typename: The name of the type thingwatcher should use to resolve the object. Defaults to
            the module.qualname of the object's type.
        tick_after: Whether to trigger a tick immediately after pausing, to show the pause state in
            the UI. If false, will suspend without ticking; in this case the pause will only be
            visible after some other task ticks.
    """
    watcher = get_current_watcher()
    if watcher is None:
        return
    await watcher.pause(anchor, typename, tick_after)


async def receive(
    anchor: Any,
    typename: str | type[Any] | None = None,
    tick_after: bool = True,
) -> Any:
    """Wait for user input from thingwatcher.

    If there is no watcher, this raises a NoWatcherConnectedError.

    Args:
        anchor: An object that describes the pause reason. Similar to `track`, but the object will
            only be tracked for the duration of the pause.
        typename: The name of the type thingwatcher should use to resolve the object. Defaults to
            the module.qualname of the object's type.
        tick_after: Whether to trigger a tick immediately after pausing, to show the pause state in
            the UI. If false, will suspend without ticking; in this case the pause will only be
            visible after some other task ticks.

    Returns:
        The (CBOR-deserialized) payload sent back by thingwatcher.

    Raises:
        NoWatcherConnectedError: If no watcher is active.
    """
    watcher = get_current_watcher()
    if watcher is None:
        raise NoWatcherConnectedError("receive() requires an active watcher")
    return await watcher.receive(anchor, typename, tick_after)


def add_root(
    name: str,
    anchor: Any,
    typename: str | type[Any] | None = None,
    replace: bool = False,
) -> None:
    """Add a named root anchor.

    Roots are named entry points that resolve to tracked things. They provide
    consistent starting points for exploring the state graph (e.g., "currentUser",
    "mainTask").

    If there is no watcher, this raises a NoWatcherConnectedError.

    Args:
        name: The name for this root (e.g., "currentUser", "mainTask").
        anchor: The anchor object to resolve via AnchorCapturer.
        typename: Optional typename for capturer lookup. If None, inferred from anchor.
        replace: If True, replace existing root with same name. If False (default),
            raise ValueError if name already exists.

    Raises:
        NoWatcherConnectedError: If no watcher is active.
        ValueError: If name already exists and replace=False, or no capturer registered.
    """
    watcher = get_current_watcher()
    if watcher is None:
        raise NoWatcherConnectedError("add_root() requires an active watcher")
    watcher.add_root(name, anchor, typename, replace)


def remove_root(name: str) -> None:
    """Remove a named root.

    The root will be invalidated at the next tick.

    If there is no watcher, this raises a NoWatcherConnectedError.

    Args:
        name: The name of the root to remove.

    Raises:
        NoWatcherConnectedError: If no watcher is active.
        KeyError: If no root with the given name exists.
    """
    watcher = get_current_watcher()
    if watcher is None:
        raise NoWatcherConnectedError("remove_root() requires an active watcher")
    watcher.remove_root(name)


def register_immutable_store(name: str, store: ImmutableStore) -> None:
    """Register an immutable object store with the active watcher.

    Immutable stores provide lazy access to things that are assumed constant over time
    (e.g., content-addressed IPLD blobs, git commits). Things in stores are not captured
    during ticks but can be queried on-demand via GraphQL.

    Key properties:
    - Store lookup happens at query time, not during tick capture
    - Snapshot things override store things if the same key exists in both
    - Store-to-store conflicts require identical states

    If there is no watcher, this raises a NoWatcherConnectedError.

    Args:
        name: Unique name for this store (shown in UI as source)
        store: The store instance implementing ImmutableStore protocol

    Raises:
        NoWatcherConnectedError: If no watcher is currently active
        ValueError: If a store with this name is already registered
    """
    watcher = get_current_watcher()
    if watcher is None:
        raise NoWatcherConnectedError("register_immutable_store() requires an active watcher")
    watcher.register_immutable_store(name, store)


def unregister_immutable_store(name: str) -> None:
    """Unregister an immutable object store.

    If there is no watcher, this raises a NoWatcherConnectedError.

    Args:
        name: Name of the store to unregister

    Raises:
        NoWatcherConnectedError: If no watcher is currently active
        KeyError: If no store with this name exists
    """
    watcher = get_current_watcher()
    if watcher is None:
        raise NoWatcherConnectedError("unregister_immutable_store() requires an active watcher")
    watcher.unregister_immutable_store(name)


# =============================================================================
# Configuration API
# =============================================================================


def register_snapshotter(
    tracked_type: str | type[Any],
    snapshotter: StatelessSnapshotter | StatefulOwningSnapshotter,
) -> None:
    """Registers a snapshotter globally for a particular type, used for `track`.

    For watcher-local registration, use `watcher.register_snapshotter()`.

    Args:
        tracked_type: The typename of an object passed to `track`. If a type object is passed,
            it gets mapped to module.qualname for convenience.
        snapshotter: Snapshotter to use. Either a StatelessSnapshotter or StatefulOwningSnapshotter.
    """
    _register_snapshotter(tracked_type, snapshotter)


def register_anchor_capturer(
    anchor_type: str | type[Any],
    anchor_capturer: AnchorCapturer,
) -> None:
    """Registers an anchor capturer globally for a particular type.

    Used for `pause`, `receive`, and `add_root`.

    Args:
        anchor_type: The typename of an anchor object. If a type object is passed,
            it gets mapped to module.qualname for convenience.
        anchor_capturer: Capturer to use.
    """
    _register_anchor_capturer(anchor_type, anchor_capturer)


def configure_rendering(
    thing_type: str,
    color: str,
    link_colors: Mapping[str, str] | None = None,
) -> None:
    """Configure how a given thing should be rendered.

    Args:
        thing_type: The type of the thing to configure. Should match the thing_type in ThingState.
        color: The color to show the thing.
        link_colors: Optional colors for links by link type, for links from this thing.
    """
    _configure_rendering(thing_type, color, link_colors)


def register_renderer(
    thing_types: str | Mapping[str, str] | Sequence[str],
    tsx_source: str | Path,
    imports: Mapping[str, str | Path] | None = None,
) -> str:
    """Register a renderer module for one or more thing types.

    The renderer module is TSX source code that will be compiled in the browser.
    It must have a default export of type ThingWatcherRenderConfig:

        export default {
            dialect: "0.1",
            renderers: {
                "TypeName": { full: FullRenderer, ref: RefRenderer, ... },
            }
        } as ThingWatcherRenderConfig;

    For multi-file renderers, provide additional modules via the `imports` dict.
    The main TSX can import from these using relative paths (e.g., './styles.module.css').
    Import paths must include extensions to match Deno conventions.

    When paths are provided, files are read lazily on demand and changes are
    detected via mtime. Use the frontend refresh button to pick up changes.

    Examples:
    ```python
    # Single type
    register_renderer("Widget", tsx_source)

    # Multiple types in one bundle
    register_renderer(["Container", "Item"], tsx_source)

    # With CSS modules
    register_renderer(
        "StyledWidget",
        tsx_source,
        imports={"./styles.module.css": css_source}
    )

    # With paths (hot reloading support)
    register_renderer(
        "Widget",
        Path("./Widget.tsx"),
        imports={"./styles.module.css": Path("./styles.module.css")}
    )
    ```

    Args:
        thing_types: One of:
            - Single string: one thing type (renderer_key = thing_type)
            - Sequence of strings: multiple thing types (renderer_key = thing_type for each)
            - Mapping from thing_type to renderer_key (for when they differ)
        tsx_source: The TSX source code for the renderer module, or a Path to read from.
        imports: Optional dict mapping relative import paths to source code or Paths.

    Returns:
        The content-addressed module ID (for debugging/inspection).
    """
    return _register_renderer(thing_types, tsx_source, imports)


def register_renderer_builder(builder: RendererBuilder) -> None:
    """Register a dynamic renderer builder function globally.

    Builder functions are called for thing types that don't have a static
    renderer registration. They are tried in most-recent-first order (stack).

    The builder function takes a thing_type string and returns either:
    - RendererBuilderResult(tsx_source, imports) if it can handle this type
    - None if it can't handle this type

    Builders are called on every lookup (not cached), but the generated content
    gets a content-addressed module_id, so unchanged content won't recompile.

    Example:
    ```python
    def my_builder(thing_type: str) -> RendererBuilderResult | None:
        if thing_type.startswith("Dynamic"):
            return RendererBuilderResult(
                tsx_source=f'export default {{ dialect: "0.1", renderers: {{ "{thing_type}": ... }} }}'
            )
        return None

    register_renderer_builder(my_builder)
    ```

    Args:
        builder: A callable that takes thing_type and returns RendererBuilderResult or None.
    """
    _register_renderer_builder(builder)


def unregister_renderer_builder(builder: RendererBuilder) -> None:
    """Unregister a previously registered renderer builder.

    Args:
        builder: The builder function to unregister.

    Raises:
        ValueError: If the builder is not registered.
    """
    _unregister_renderer_builder(builder)


# =============================================================================
# Watcher (re-exported from watcher module)
# =============================================================================

from .watcher import SnapshotResult, Watcher

__all__ = [
    # Data types
    "CborableTypes",
    "ThingLink",
    "ThingState",
    "ThingDelta",
    "ThingRef",
    "BacklinkInfo",
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
    # Instrumentation
    "track",
    "untrack",
    "tick",
    "pause",
    "receive",
    "add_root",
    "remove_root",
    "register_immutable_store",
    "unregister_immutable_store",
    # Configuration
    "register_snapshotter",
    "register_anchor_capturer",
    "configure_rendering",
    "register_renderer",
    "register_renderer_builder",
    "unregister_renderer_builder",
    # Renderer builder types
    "RendererBuilder",
    "RendererBuilderResult",
    # Watcher
    "Watcher",
    "SnapshotResult",
    # Exceptions
    "NoWatcherConnectedError",
    "FxtrSupportDisabledError",
]
