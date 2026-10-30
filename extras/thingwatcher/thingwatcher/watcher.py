"""The main Watcher class that manages tracking and snapshotting."""

from __future__ import annotations

import json
import logging
import math
import threading
import time
import uuid
import weakref
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Iterator, Sequence

if TYPE_CHECKING:
    from fxtr2.data.entity_core import FxtrID
    from fxtr2.data.fxtr_scope import FxtrScope

import anyio
import cbor2
from anyio import CancelScope, Event
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from sqlalchemy.ext.asyncio import AsyncConnection


class DebugTriggerError(Exception):
    """Raised when debug trigger operations fail."""


from .api import (
    AnchorCapturer,
    AsyncThingUpdate,
    BacklinkInfo,
    FxtrSupportDisabledError,
    ImmutableStore,
    StatefulOwningSnapshotter,
    StatefulResult,
    StatelessSnapshotter,
    ThingDelta,
    ThingLink,
    ThingRef,
    ThingState,
    _NoPushedChunk,
    _PopAll,
)
from .context import reset_current_watcher, set_current_watcher
from .db import Database
from .registry import TypeRegistry, get_global_registry, resolve_typename

logger = logging.getLogger("thingwatcher")


@dataclass
class TrackedObject:
    """Internal representation of a tracked object."""

    typename: str
    obj_ref: weakref.ref[Any]


@dataclass
class SnapshotResult:
    """Result of a tick operation. Also used as the write job for the DB writer."""

    tick_number: int
    seq_number: int  # Global sequence number for this snapshot
    timestamp: float
    level: str
    desc: str | None
    states: Sequence[ThingState | ThingDelta] = field(default_factory=list)
    tick_type: str = "api_tick"
    on_complete: Callable[[dict[str, Any]], None] | None = None
    # Root bindings: root_name -> thing_key_bytes (CBOR canonical)
    root_bindings: dict[str, bytes] = field(default_factory=dict)
    # If True, this is a hard recapture: invalidate all live records before writing
    is_hard_recapture: bool = False


@dataclass
class StreamingUpdateResult:
    """Result of a streaming update operation.

    Used as the write job for streaming updates to a stream tick.
    Unlike SnapshotResult, this only updates a single tracked object
    and does not re-capture roots or pauses.
    """

    tick_number: int
    seq_number: int
    timestamp: float
    is_new_tick: bool  # True if this creates a new stream tick, False if updating existing
    obj_id: int  # The tracked object that was updated
    states: Sequence[ThingState | ThingDelta] = field(default_factory=list)
    # Keys that disappeared (previously owned by this obj but no longer emitted)
    disappeared_keys: set[bytes] = field(default_factory=set)
    on_complete: Callable[[dict[str, Any]], None] | None = None


@dataclass
class CurrentLinkInfo:
    """Info about a currently-valid link in the database."""

    link_id: int
    link_type: str
    to_thing_key_bytes: bytes  # CBOR canonical


@dataclass
class CurrentThingInfo:
    """In-memory tracking for a currently-valid thing in the database."""

    thingstate_id: int
    thing_type: str
    data_chunk_id: int  # Current data chunk ID
    last_was_state: bool  # True if last snapshot was ThingState (not ThingDelta)
    data_bytes: bytes | None  # CBOR-encoded data, only valid when last_was_state=True
    # Maps link_key (CBOR bytes) -> CurrentLinkInfo
    links: dict[bytes, CurrentLinkInfo] = field(default_factory=dict)


@dataclass
class PauseInfo:
    """Tracking info for an active pause."""

    event: Event  # Event to signal on trigger
    payload_holder: list[Any]  # Mutable holder for payload
    is_paused: bool  # True while actually awaiting
    typename: str  # Resolved typename for capturer lookup


@dataclass
class TriggerResult:
    """Result of a trigger_pause call."""

    ok: bool
    trigger_id: int | None = None  # ID of trigger record if successful
    error: str | None = None  # Error message if failed


@dataclass
class CurrentRootInfo:
    """In-memory tracking for a currently-valid root binding in the database."""

    root_db_id: int  # ID of current DB row
    thing_key_bytes: bytes  # CBOR canonical key of the resolved thing


@dataclass
class AsyncUpdateJob:
    """Job for running an async update in the background.

    Created when a snapshotter returns AsyncThingUpdate, queued to the
    async update runner which executes the async_updater and pushes the
    result to a streaming tick.
    """

    obj_id: int  # The tracked object ID
    cancel_scope: CancelScope  # Scope to cancel this job
    async_updater: Callable[[], Awaitable[StatefulResult]]


def _make_broken_thing(obj_id: int, tick_num: int, error_msg: str) -> ThingState:
    """Create a broken thing state for error cases (legacy key format)."""
    return ThingState(
        key=("__broken__", obj_id, tick_num),
        thing_type="__broken__",
        thing_data={"error": error_msg},
        links=[],
    )


def _make_broken_thing_with_key(key: Any, error_msg: str) -> ThingState:
    """Create a broken thing state preserving the original key.

    Used for unification conflicts where we want to keep the key
    but mark the thing as broken.
    """
    return ThingState(
        key=key,
        thing_type="__broken__",
        thing_data={"error": error_msg},
        links=[],
    )


def _thing_states_equal(a: ThingState, b: ThingState) -> bool:
    """Check if two ThingStates are equal (same key, type, data, links)."""
    if a.key != b.key:
        return False
    if a.thing_type != b.thing_type:
        return False
    if a.thing_data != b.thing_data:
        return False
    if len(a.links) != len(b.links):
        return False
    # Compare links - order matters
    for la, lb in zip(a.links, b.links):
        if la.link_type != lb.link_type:
            return False
        if la.link_key != lb.link_key:
            return False
        if la.to_thing_key != lb.to_thing_key:
            return False
    return True


def _serialize_key(key: Any) -> Any:
    """Serialize a key for JSON output. Complex keys get repr'd."""
    if isinstance(key, (str, int, float, bool, type(None))):
        return key
    if isinstance(key, (list, tuple)):
        return [_serialize_key(k) for k in key]
    if isinstance(key, dict):
        return {str(k): _serialize_key(v) for k, v in key.items()}
    return repr(key)


def _serialize_thing(thing: ThingState | ThingDelta) -> dict[str, Any]:
    """Serialize a ThingState or ThingDelta to a JSON-compatible dict."""
    if isinstance(thing, ThingState):
        return {
            "type": "state",
            "key": _serialize_key(thing.key),
            "thing_type": thing.thing_type,
            "thing_data": _serialize_key(thing.thing_data),
            "links": [
                {
                    "link_type": link.link_type,
                    "link_key": _serialize_key(link.link_key),
                    "to_thing_key": _serialize_key(link.to_thing_key),
                }
                for link in thing.links
            ],
        }
    else:  # ThingDelta
        has_push = not isinstance(thing.push_chunk, _NoPushedChunk)
        is_pop_all = isinstance(thing.pop_chunks, _PopAll)
        return {
            "type": "delta",
            "key": _serialize_key(thing.key),
            "push_chunk": _serialize_key(thing.push_chunk) if has_push else None,
            "pop_chunks": "POP_ALL" if is_pop_all else thing.pop_chunks,
            "added_links": [
                {
                    "link_type": link.link_type,
                    "link_key": _serialize_key(link.link_key),
                    "to_thing_key": _serialize_key(link.to_thing_key),
                }
                for link in thing.added_links
            ],
            "removed_link_keys": [_serialize_key(k) for k in thing.removed_link_keys],
        }


def _validate_cborable(value: Any, field_name: str) -> None:
    """Validate that a value can be CBOR-serialized.

    Raises ValueError if serialization fails.
    """
    try:
        cbor2.dumps(value, canonical=True)
    except Exception as e:
        raise ValueError(f"{field_name} is not CBOR-serializable: {e}")


def _validate_thing_link(link: ThingLink, context: str = "") -> None:
    """Validate a ThingLink is well-formed.

    Raises ValueError if validation fails.
    """
    prefix = f"{context}: " if context else ""

    if not isinstance(link, ThingLink):
        raise ValueError(f"{prefix}expected ThingLink, got {type(link).__name__}")

    if not isinstance(link.link_type, str):
        raise ValueError(f"{prefix}link_type must be a string, got {type(link.link_type).__name__}")

    _validate_cborable(link.link_key, f"{prefix}link_key")
    _validate_cborable(link.to_thing_key, f"{prefix}to_thing_key")


def _validate_thing_state(state: ThingState) -> None:
    """Validate a ThingState is well-formed and won't cause DB errors.

    Raises ValueError if validation fails.
    """
    if not isinstance(state, ThingState):
        raise ValueError(f"expected ThingState, got {type(state).__name__}")

    # Validate key is CBOR-serializable
    _validate_cborable(state.key, "key")

    # Validate thing_type is a string
    if not isinstance(state.thing_type, str):
        raise ValueError(f"thing_type must be a string, got {type(state.thing_type).__name__}")

    # Validate thing_data is CBOR-serializable
    _validate_cborable(state.thing_data, "thing_data")

    # Validate links is a list
    if not isinstance(state.links, list):
        raise ValueError(f"links must be a list, got {type(state.links).__name__}")

    # Validate each link
    for i, link in enumerate(state.links):
        _validate_thing_link(link, f"links[{i}]")


def _validate_thing_delta(delta: ThingDelta) -> None:
    """Validate a ThingDelta is well-formed and won't cause DB errors.

    Raises ValueError if validation fails. This includes both immediate
    validation (pop/push semantics) and CBOR serializability checks.

    Note: Some validation (pop_chunks > chain_length) requires DB access
    and is done during async processing.
    """
    if not isinstance(delta, ThingDelta):
        raise ValueError(f"expected ThingDelta, got {type(delta).__name__}")

    # Validate key is CBOR-serializable
    _validate_cborable(delta.key, "key")

    # Validate pop_chunks
    has_push = not isinstance(delta.push_chunk, _NoPushedChunk)
    is_pop_all = isinstance(delta.pop_chunks, _PopAll)

    if is_pop_all and not has_push:
        raise ValueError(
            f"ThingDelta for key {delta.key}: cannot pop all chunks without pushing new data"
        )

    if not is_pop_all:
        if not isinstance(delta.pop_chunks, int):
            raise ValueError(
                f"pop_chunks must be int or POP_ALL, got {type(delta.pop_chunks).__name__}"
            )
        if delta.pop_chunks < 0:
            raise ValueError(
                f"ThingDelta for key {delta.key}: pop_chunks must be >= 0, got {delta.pop_chunks}"
            )

    # Validate push_chunk is CBOR-serializable (if not the sentinel)
    if has_push:
        _validate_cborable(delta.push_chunk, "push_chunk")

    # Validate added_links is a list
    if not isinstance(delta.added_links, list):
        raise ValueError(f"added_links must be a list, got {type(delta.added_links).__name__}")

    # Validate each added link
    for i, link in enumerate(delta.added_links):
        _validate_thing_link(link, f"added_links[{i}]")

    # Validate removed_link_keys is a list
    if not isinstance(delta.removed_link_keys, list):
        raise ValueError(
            f"removed_link_keys must be a list, got {type(delta.removed_link_keys).__name__}"
        )

    # Validate each removed link key is CBOR-serializable
    for i, link_key in enumerate(delta.removed_link_keys):
        _validate_cborable(link_key, f"removed_link_keys[{i}]")


def _validate_thing_ref(ref: ThingRef) -> None:
    """Validate a ThingRef is well-formed and won't cause DB errors.

    Raises ValueError if validation fails.
    """
    if not isinstance(ref, ThingRef):
        raise ValueError(f"expected ThingRef, got {type(ref).__name__}")

    # Validate key is CBOR-serializable
    _validate_cborable(ref.key, "key")


def _validate_snapshotter_result(
    result: ThingState | ThingDelta | Sequence[ThingState | ThingDelta],
) -> None:
    """Validate a snapshotter result (from snapshot_owned or snapshot).

    Raises ValueError if any item fails validation.
    """
    if isinstance(result, ThingState):
        _validate_thing_state(result)
    elif isinstance(result, ThingDelta):
        _validate_thing_delta(result)
    elif isinstance(result, (list, tuple)):
        for i, item in enumerate(result):
            if isinstance(item, ThingState):
                _validate_thing_state(item)
            elif isinstance(item, ThingDelta):
                _validate_thing_delta(item)
            else:
                raise ValueError(
                    f"result[{i}]: expected ThingState or ThingDelta, got {type(item).__name__}"
                )
    else:
        raise ValueError(
            f"expected ThingState, ThingDelta, or sequence, got {type(result).__name__}"
        )


def _validate_anchor_capturer_result(result: ThingState | ThingRef) -> None:
    """Validate an anchor capturer result (from handle).

    Raises ValueError if validation fails.
    """
    if isinstance(result, ThingState):
        _validate_thing_state(result)
    elif isinstance(result, ThingRef):
        _validate_thing_ref(result)
    else:
        raise ValueError(f"expected ThingState or ThingRef, got {type(result).__name__}")


class Watcher:
    """The main watcher object that manages tracking and serves the UI.

    When active, coordinates tracking of objects, manages the backing store,
    and serves the GraphQL API and frontend.
    """

    def __init__(
        self,
        *,
        db_path: str | None = None,
        port: int | None = 8765,
        fxtr_support: bool = False,
    ) -> None:
        """Initialize a watcher.

        Args:
            db_path: Path to the SQLite database file. If None, uses an in-memory database.
            port: Port to serve the UI on. If None, no HTTP server is started.
            fxtr_support: If True, enables fxtr entity tracking helpers. Creates an internal
                fxtr namespace for storing tracked entities.
        """
        self.db_path = db_path
        self.port = port
        self._fxtr_support = fxtr_support

        # Thread safety: RLock to protect tracking state
        # RLock is reentrant so GC finalizers won't deadlock if triggered while lock is held
        self._lock = threading.RLock()

        # Tracking state (protected by _lock)
        self._tracked: dict[int, TrackedObject] = {}
        self._tick_counter: int = 0
        self._seq_counter: int = (
            0  # Global sequence number (increments on every tick/streaming update)
        )
        self._current_tick_type: str | None = (
            None  # Type of most recent tick ("stream", "api_tick", etc.)
        )

        # Local type registry (overrides global)
        self._local_registry = TypeRegistry()

        # Testing support
        self._tick_interceptor: Callable[[SnapshotResult], None] | None = None

        # DB state (initialized in watch_and_serve_until)
        self._db: Database | None = None

        # Write stream for queueing snapshots and streaming updates
        # Created immediately with infinite buffer so ticks can be queued
        # even before watch_and_serve_until is called
        # WriteJob = SnapshotResult | StreamingUpdateResult
        send_stream, receive_stream = anyio.create_memory_object_stream[
            SnapshotResult | StreamingUpdateResult
        ](max_buffer_size=math.inf)
        self._write_stream: MemoryObjectSendStream[SnapshotResult | StreamingUpdateResult] = (
            send_stream
        )
        self._receive_stream: MemoryObjectReceiveStream[SnapshotResult | StreamingUpdateResult] = (
            receive_stream
        )

        # In-memory tracking for change detection: thing_key (CBOR bytes) -> CurrentThingInfo
        self._current_things: dict[bytes, CurrentThingInfo] = {}

        # Subscription support: streams that receive tick notifications
        self._tick_streams: list[MemoryObjectSendStream[dict[str, Any]]] = []
        self._tick_streams_lock = threading.Lock()

        # Debug trigger support: only one waiter per trigger name allowed
        self._debug_triggers: dict[str, Event] = {}
        self._debug_payloads: dict[str, Any] = {}
        self._debug_lock = threading.Lock()

        # Pause/trigger support
        # Paused anchors: id(anchor) -> (anchor_obj, PauseInfo)
        # Strong reference to anchor to keep it alive during pause
        self._paused_anchors: dict[int, tuple[Any, PauseInfo]] = {}

        # Temp map rebuilt each tick: thing_key_bytes -> list of (anchor_id, PauseInfo)
        # Used for trigger lookups between ticks
        self._triggerable_at_tick: dict[bytes, list[tuple[int, PauseInfo]]] = {}

        # Root tracking: name -> (anchor_obj, typename)
        # Strong reference to anchor to keep it alive while mounted
        self._roots: dict[str, tuple[Any, str]] = {}

        # In-memory tracking for change detection: root_name -> CurrentRootInfo
        self._current_roots: dict[str, CurrentRootInfo] = {}

        # Ownership tracking for StatefulOwningSnapshotter
        # Maps tracked object id -> set of thing key bytes it currently owns
        self._owned_things: dict[int, set[bytes]] = {}
        # Maps thing key bytes -> owning tracked object id (if any)
        self._thing_owners: dict[bytes, int] = {}
        # Set of thing key bytes produced by stateless sources (snapshotters + anchor capturers)
        # Used for fast conflict detection during streaming subticks
        self._stateless_keys: set[bytes] = set()

        # Immutable object stores: name -> store instance
        self._immutable_stores: dict[str, ImmutableStore] = {}

        # Async update support
        # Pending async updates: obj_id -> CancelScope (only non-cancelled, non-completed)
        self._pending_async_updates: dict[int, CancelScope] = {}

        # Stream for async update jobs (buffered, consumed by runner task)
        # Jobs are queued here and the _async_update_runner processes them
        async_send, async_receive = anyio.create_memory_object_stream[AsyncUpdateJob](
            max_buffer_size=math.inf
        )
        self._async_update_send: MemoryObjectSendStream[AsyncUpdateJob] = async_send
        self._async_update_receive: MemoryObjectReceiveStream[AsyncUpdateJob] = async_receive

        # Fxtr entity tracking support (when fxtr_support=True)
        # These are initialized in watching_and_serving() when fxtr_support is enabled
        self._fxtr_namespace: Any = None  # SQLiteFxtrNamespace, typed as Any to avoid import
        self._fxtr_copy_send: MemoryObjectSendStream[tuple[FxtrID, FxtrScope]] | None = None
        self._fxtr_copy_recv: MemoryObjectReceiveStream[tuple[FxtrID, FxtrScope]] | None = None
        self._fxtr_tracked_ids: set[FxtrID] = set()  # For deduplication

    def register_snapshotter(
        self,
        typename: str | type[Any],
        snapshotter: StatelessSnapshotter | StatefulOwningSnapshotter,
    ) -> None:
        """Register a snapshotter locally for this watcher.

        Local registrations override global registrations.

        Args:
            typename: The typename to register for (type or string).
            snapshotter: The snapshotter to use.
        """
        self._local_registry.register_snapshotter(typename, snapshotter)

    def get_snapshotter(
        self, typename: str
    ) -> StatelessSnapshotter | StatefulOwningSnapshotter | None:
        """Get the snapshotter for a typename.

        Checks local registry first, then falls back to global.

        Args:
            typename: The typename to look up.

        Returns:
            The snapshotter, or None if not found.
        """
        result = self._local_registry.get_snapshotter(typename)
        if result is not None:
            return result
        return get_global_registry().get_snapshotter(typename)

    def register_anchor_capturer(
        self,
        typename: str | type[Any],
        capturer: AnchorCapturer,
    ) -> None:
        """Register an anchor capturer locally for this watcher.

        Local registrations override global registrations.

        Args:
            typename: The typename to register for (type or string).
            capturer: The anchor capturer to use.
        """
        self._local_registry.register_anchor_capturer(typename, capturer)

    def get_anchor_capturer(self, typename: str) -> AnchorCapturer | None:
        """Get the anchor capturer for a typename.

        Checks local registry first, then falls back to global.

        Args:
            typename: The typename to look up.

        Returns:
            The anchor capturer, or None if not found.
        """
        result = self._local_registry.get_anchor_capturer(typename)
        if result is not None:
            return result
        return get_global_registry().get_anchor_capturer(typename)

    def register_immutable_store(self, name: str, store: ImmutableStore) -> None:
        """Register an immutable object store with this watcher.

        Immutable stores provide lazy access to things that are assumed constant
        over time. Things in stores are not captured during ticks but can be
        queried on-demand via GraphQL.

        Args:
            name: Unique name for this store (shown in UI as source)
            store: The store instance implementing ImmutableStore protocol

        Raises:
            ValueError: If a store with this name is already registered
        """
        if name in self._immutable_stores:
            raise ValueError(f"Immutable store '{name}' is already registered")
        self._immutable_stores[name] = store

    def unregister_immutable_store(self, name: str) -> None:
        """Unregister an immutable object store.

        Args:
            name: Name of the store to unregister

        Raises:
            KeyError: If no store with this name exists
        """
        if name not in self._immutable_stores:
            raise KeyError(f"No immutable store named '{name}' is registered")
        del self._immutable_stores[name]

    async def lookup_in_stores(self, key: Any) -> list[tuple[str, ThingState]]:
        """Look up a key in all immutable stores.

        Args:
            key: The thing key (CBOR-serializable, decoded)

        Returns:
            List of (store_name, ThingState) tuples for stores that have this key.
            Empty list if no store has this key.
        """
        results: list[tuple[str, ThingState]] = []
        for name, store in self._immutable_stores.items():
            try:
                state = await store.get_thing_state(key)
                if state is not None:
                    results.append((name, state))
            except Exception as e:
                # Store error - create a broken thing entry
                logger.warning(f"Store '{name}' raised exception for key {key!r}: {e}")
                # We could create a broken thing here, but for now just skip
                # The caller can decide how to handle missing data
        return results

    async def get_store_backlinks(self, key: Any) -> list[tuple[str, BacklinkInfo]]:
        """Get backlinks to a key from all immutable stores.

        Args:
            key: The thing key to find backlinks for (decoded)

        Returns:
            List of (store_name, BacklinkInfo) tuples for backlinks found in stores.
        """
        results: list[tuple[str, BacklinkInfo]] = []
        for name, store in self._immutable_stores.items():
            try:
                backlinks = await store.get_backlinks(key)
                for backlink in backlinks:
                    results.append((name, backlink))
            except Exception as e:
                logger.warning(
                    f"Store '{name}' raised exception getting backlinks for {key!r}: {e}"
                )
        return results

    def add_root(
        self,
        name: str,
        anchor: Any,
        typename: str | type[Any] | None = None,
        replace: bool = False,
    ) -> None:
        """Add a named root anchor.

        Roots are named entry points that resolve to tracked things.
        They are captured during each tick and stored with temporal tracking.

        Args:
            name: The name for this root (e.g., "currentUser", "mainTask").
            anchor: The anchor object to resolve via AnchorCapturer.
            typename: Optional typename for capturer lookup. If None, inferred from anchor.
            replace: If True, replace existing root with same name. If False (default),
                raise ValueError if name already exists.

        Raises:
            ValueError: If name already exists and replace=False, or no capturer registered.
        """
        # Resolve typename
        if typename is None:
            resolved_typename = f"{type(anchor).__module__}.{type(anchor).__qualname__}"
        else:
            resolved_typename = resolve_typename(typename)

        # Verify capturer exists
        capturer = self.get_anchor_capturer(resolved_typename)
        if capturer is None:
            raise ValueError(f"no anchor capturer registered for {resolved_typename}")

        # Check for existing root
        with self._lock:
            if name in self._roots and not replace:
                raise ValueError(f"root '{name}' already exists (use replace=True to overwrite)")

            # Store the root anchor
            self._roots[name] = (anchor, resolved_typename)

    def remove_root(self, name: str) -> None:
        """Remove a named root.

        The root will be invalidated at the next tick.

        Args:
            name: The name of the root to remove.

        Raises:
            KeyError: If no root with the given name exists.
        """
        with self._lock:
            if name not in self._roots:
                raise KeyError(f"no root named '{name}'")
            del self._roots[name]

    def set_tick_interceptor(self, interceptor: Callable[[SnapshotResult], None] | None) -> None:
        """Set a tick interceptor for testing.

        The interceptor is called with the SnapshotResult before logging.

        Args:
            interceptor: Function to call on each tick, or None to clear.
        """
        self._tick_interceptor = interceptor

    # =========================================================================
    # Subscription Support
    # =========================================================================

    def _add_tick_stream(self, stream: MemoryObjectSendStream[dict[str, Any]]) -> None:
        """Register a stream to receive tick notifications."""
        with self._tick_streams_lock:
            self._tick_streams.append(stream)

    def _remove_tick_stream(self, stream: MemoryObjectSendStream[dict[str, Any]]) -> None:
        """Unregister a tick stream."""
        with self._tick_streams_lock:
            if stream in self._tick_streams:
                self._tick_streams.remove(stream)

    def _notify_tick_subscribers(self, tick_data: dict[str, Any]) -> None:
        """Send tick data to all registered streams (called from background writer)."""
        with self._tick_streams_lock:
            streams = list(self._tick_streams)  # Copy to avoid holding lock during sends
        for stream in streams:
            try:
                stream.send_nowait(tick_data)
            except anyio.WouldBlock:
                pass  # Buffer full, drop this tick for this subscriber
            except anyio.ClosedResourceError:
                logger.warning("Tick stream closed unexpectedly - subscriber may have leaked")

    async def _close_all_tick_streams(self) -> None:
        """Close all tick streams during shutdown."""
        with self._tick_streams_lock:
            streams = list(self._tick_streams)
            self._tick_streams.clear()
        for stream in streams:
            await stream.aclose()

    # =========================================================================
    # Async Update Support
    # =========================================================================

    def _cancel_async_update_for(self, obj_id: int) -> None:
        """Cancel and remove any pending async update for this object.

        Removes from dict immediately since there may be a delay before the
        runner task actually exits. The dict only contains non-cancelled,
        non-completed scopes.

        Thread-safe: acquires _lock.
        """
        with self._lock:
            scope = self._pending_async_updates.pop(obj_id, None)
            if scope is not None:
                scope.cancel()

    def _cancel_all_async_updates(self) -> None:
        """Cancel all pending async updates.

        Used during shutdown and hard_recapture.

        Thread-safe: acquires _lock.
        """
        with self._lock:
            for scope in self._pending_async_updates.values():
                scope.cancel()
            self._pending_async_updates.clear()

    async def _async_update_runner(self) -> None:
        """Background task that runs async update jobs.

        Consumes jobs from _async_update_receive and runs them concurrently.
        Each job's async_updater is executed, and if successful, its result
        is pushed to a streaming tick.
        """
        async with anyio.create_task_group() as tg:
            async for job in self._async_update_receive:
                tg.start_soon(self._run_single_async_update, job)

    async def _run_single_async_update(self, job: AsyncUpdateJob) -> None:
        """Execute a single async update job.

        If the job was cancelled before starting, returns immediately.
        If an error occurs, pushes a broken thing to the streaming tick.
        """
        # Check if already cancelled before we started
        # (scope was removed from dict when cancelled, so just check the flag)
        if job.cancel_scope.cancel_called:
            return

        try:
            with job.cancel_scope:
                result = await job.async_updater()

                # Validate: cannot return another AsyncThingUpdate
                if isinstance(result, AsyncThingUpdate):
                    raise ValueError("async_updater cannot return AsyncThingUpdate")

                # Normalize to list
                if isinstance(result, (ThingState, ThingDelta)):
                    states: list[ThingState | ThingDelta] = [result]
                else:
                    states = list(result)

                # Validate the results
                _validate_snapshotter_result(states)

                # Push result to streaming tick (bypassing snapshotter)
                self._push_streaming_states(job.obj_id, states)

        except anyio.get_cancelled_exc_class():
            pass  # Cancelled, result not applied
        except Exception as e:
            # Error in async_updater - push broken thing
            logger.warning(f"async_updater for obj_id={job.obj_id} raised: {e}")
            broken = _make_broken_thing(job.obj_id, self._tick_counter, str(e))
            self._push_streaming_states(job.obj_id, [broken])
        finally:
            # Clean up from dict if we're still the current entry
            # (might not be if a new async update was queued for this obj)
            with self._lock:
                if self._pending_async_updates.get(job.obj_id) is job.cancel_scope:
                    del self._pending_async_updates[job.obj_id]

    async def _fxtr_copy_worker(self) -> None:
        """Background task that copies fxtr entities to the internal namespace.

        Consumes (fxtr_id, from_scope) jobs from _fxtr_copy_recv and pins
        each entity into the internal namespace. Pinning handles the copy
        automatically.
        """
        assert self._fxtr_copy_recv is not None
        async for fxtr_id, from_scope in self._fxtr_copy_recv:
            try:
                # Pin entity using from_scope - pinning copies if needed
                await self._fxtr_namespace.pin(
                    fxtr_id.id_string(),
                    fxtr_id,
                    overwrite=True,
                    scope=from_scope,
                )
            except Exception:
                # Log error but don't crash - entity just won't be available
                logger.warning(
                    f"Failed to copy fxtr entity {fxtr_id}",
                    exc_info=True,
                )

    def _push_streaming_states(
        self,
        obj_id: int,
        states: Sequence[ThingState | ThingDelta],
    ) -> None:
        """Push states to a streaming tick.

        Used by both streaming_update() (after calling the snapshotter) and
        async update completion (with pre-computed results). Creates or updates
        a stream tick with the given states for the specified object.

        Handles:
        - Tick/seq counter management (creates new stream tick if needed)
        - Ownership conflict detection
        - Ownership map updates
        - Queueing the result to the write stream

        Thread-safe: acquires _lock.
        """
        with self._lock:
            # 1. Increment seq always, tick only if needed
            self._seq_counter += 1
            seq_num = self._seq_counter
            timestamp = time.time()

            if self._current_tick_type == "stream":
                # Reuse existing stream tick
                tick_num = self._tick_counter
                is_new_tick = False
            else:
                # Create new stream tick
                self._tick_counter += 1
                tick_num = self._tick_counter
                is_new_tick = True
                self._current_tick_type = "stream"

            # 2. Detect disappeared keys
            prev_owned = self._owned_things.get(obj_id, set())
            current_keys: set[bytes] = set()
            for item in states:
                key_bytes = cbor2.dumps(item.key, canonical=True)
                current_keys.add(key_bytes)
            disappeared_keys = prev_owned - current_keys

            # 3. Check for ownership conflicts with stateless keys
            final_states: Sequence[ThingState | ThingDelta] = states
            for key_bytes in current_keys:
                if key_bytes in self._stateless_keys:
                    # Conflict with stateless source - create broken thing
                    final_states = [
                        ThingState(
                            key=cbor2.loads(key_bytes),
                            thing_type="__broken__",
                            thing_data={
                                "error": "ownership conflict: key already produced by stateless source"
                            },
                            links=[],
                        )
                    ]
                    break

            # 4. Update ownership maps
            # Remove ownership of disappeared keys
            for key_bytes in disappeared_keys:
                self._thing_owners.pop(key_bytes, None)

            # Update ownership for current keys
            new_owned: set[bytes] = set()
            for key_bytes in current_keys:
                existing_owner = self._thing_owners.get(key_bytes)
                if existing_owner is not None and existing_owner != obj_id:
                    # Another object owns this key - conflict
                    final_states = [
                        ThingState(
                            key=cbor2.loads(key_bytes),
                            thing_type="__broken__",
                            thing_data={
                                "error": "ownership conflict: key owned by another tracked object"
                            },
                            links=[],
                        )
                    ]
                    break
                self._thing_owners[key_bytes] = obj_id
                new_owned.add(key_bytes)

            self._owned_things[obj_id] = new_owned

            # 5. Queue the result
            result = StreamingUpdateResult(
                tick_number=tick_num,
                seq_number=seq_num,
                timestamp=timestamp,
                is_new_tick=is_new_tick,
                obj_id=obj_id,
                states=list(final_states),
                disappeared_keys=disappeared_keys,
            )
            self._write_stream.send_nowait(result)

    # =========================================================================
    # Debug Trigger Support
    # =========================================================================

    def _fire_debug_trigger(self, name: str, payload: Any) -> None:
        """Fire a debug trigger, waking any waiters.

        Raises:
            DebugTriggerError: If no waiter is registered for this trigger name.
        """
        with self._debug_lock:
            if name not in self._debug_triggers:
                raise DebugTriggerError(f"No waiter registered for debug trigger: {name}")
            self._debug_payloads[name] = payload
            self._debug_triggers[name].set()

    async def debug_receive(self, name: str, timeout: float | None = None) -> Any:
        """Wait for a debug trigger and return its payload.

        Only one waiter per trigger name is allowed at a time.

        Args:
            name: The trigger name to wait for.
            timeout: Optional timeout in seconds.

        Returns:
            The payload sent with the trigger.

        Raises:
            DebugTriggerError: If another waiter is already registered for this name.
            TimeoutError: If timeout expires.
        """
        with self._debug_lock:
            if name in self._debug_triggers:
                raise DebugTriggerError(f"Another waiter already registered for: {name}")
            event = Event()
            self._debug_triggers[name] = event

        try:
            if timeout is not None:
                with anyio.fail_after(timeout):
                    await event.wait()
            else:
                await event.wait()

            with self._debug_lock:
                return self._debug_payloads.pop(name, None)
        finally:
            # Always clean up the trigger registration
            with self._debug_lock:
                self._debug_triggers.pop(name, None)

    # =========================================================================
    # Pause/Receive Support
    # =========================================================================

    async def pause(
        self,
        anchor: Any,
        typename: str | type[Any] | None = None,
        tick_after: bool = True,
    ) -> None:
        """Pause until triggered. Implementation for api.pause()."""
        await self._pause_or_receive(anchor, typename, tick_after, want_payload=False)

    async def receive(
        self,
        anchor: Any,
        typename: str | type[Any] | None = None,
        tick_after: bool = True,
    ) -> Any:
        """Pause and return trigger payload. Implementation for api.receive()."""
        return await self._pause_or_receive(anchor, typename, tick_after, want_payload=True)

    async def _pause_or_receive(
        self,
        anchor: Any,
        typename: str | type[Any] | None,
        tick_after: bool,
        want_payload: bool,
    ) -> Any:
        """Core pause/receive implementation."""
        # 1. Resolve typename
        if typename is None:
            resolved_typename = resolve_typename(type(anchor))
        else:
            resolved_typename = resolve_typename(typename)

        # 2. Verify capturer exists (we'll run it during tick, but check now)
        capturer = self.get_anchor_capturer(resolved_typename)
        if capturer is None:
            raise ValueError(f"no anchor capturer registered for {resolved_typename}")

        anchor_id = id(anchor)

        # 3. Check not already paused (under lock)
        with self._lock:
            if anchor_id in self._paused_anchors:
                raise ValueError("cannot pause on same anchor object twice")

            # Create pause info and register
            event = Event()
            pause_info = PauseInfo(
                event=event,
                payload_holder=[],
                is_paused=False,
                typename=resolved_typename,
            )
            self._paused_anchors[anchor_id] = (anchor, pause_info)

        try:
            # 4. Tick if requested (builds _triggerable_at_tick including this pause)
            if tick_after:
                self.tick("pause", f"Paused: {resolved_typename}")

            # 5. Mark as paused and await
            pause_info.is_paused = True
            await event.wait()

            # 6. Return payload if receive()
            if want_payload:
                return pause_info.payload_holder[0] if pause_info.payload_holder else None
            return None
        finally:
            # 7. Cleanup
            pause_info.is_paused = False
            with self._lock:
                self._paused_anchors.pop(anchor_id, None)

    async def trigger_pause(self, key_bytes: bytes, payload: Any) -> TriggerResult:
        """Trigger pauses for a thing key. Returns TriggerResult.

        Flow:
        1. Set payloads on live pauses (but don't wake them yet)
        2. Do a post_trigger tick (captures state at trigger moment)
        3. Write trigger record to DB (associated with post_trigger tick)
        4. Set events to wake the paused tasks
        """
        events_to_set: list[Event] = []

        with self._lock:
            pause_list = self._triggerable_at_tick.get(key_bytes, [])

            # Filter to actually paused entries
            live_pauses = [(aid, pi) for aid, pi in pause_list if pi.is_paused]

            if not live_pauses:
                return TriggerResult(ok=False, error="No active pause for this key")

            # Set payloads but DON'T set events yet - we need to do a tick first
            for anchor_id, pause_info in live_pauses:
                pause_info.payload_holder.append(payload)
                events_to_set.append(pause_info.event)

            # Clear from triggerable map (already triggered this tick)
            self._triggerable_at_tick[key_bytes] = [
                (aid, pi) for aid, pi in pause_list if pi not in [p for _, p in live_pauses]
            ]

        # Get short ID for the triggered thing (for description)
        async with self._db.begin() as conn:
            short_id = await self._db.get_short_id(conn, key_bytes)
        short_id_str = f"#{short_id}" if short_id else "(unknown)"

        # Do post_trigger tick FIRST - captures state at the trigger moment, before tasks wake
        tick_data = await self._async_tick(
            "post_trigger", f"Triggered thing {short_id_str}", "post_trigger"
        )
        post_trigger_tick = tick_data["tick"]

        # Write trigger record to DB, associated with the post_trigger tick
        async with self._db.begin() as conn:
            thing = await self._db.get_thing_data(conn, post_trigger_tick, key_bytes)
            thingstate_id = thing.id if thing else None

            record_id = await self._db.write_trigger_record(
                conn, key_bytes, thingstate_id, post_trigger_tick, payload
            )

        # NOW set events to wake the paused tasks
        for event in events_to_set:
            event.set()

        return TriggerResult(ok=True, trigger_id=record_id)

    def get_triggerable_things(self) -> list[tuple[bytes, str]]:
        """Get list of currently triggerable things as (key_bytes, thing_type) tuples."""
        result = []
        with self._lock:
            for key_bytes, pause_list in self._triggerable_at_tick.items():
                live = [pi for _, pi in pause_list if pi.is_paused]
                if live:
                    # Only include if the thing exists at the current tick
                    current_info = self._current_things.get(key_bytes)
                    if current_info is not None:
                        result.append((key_bytes, current_info.thing_type))
        return result

    # =========================================================================
    # Snapshot Helper
    # =========================================================================

    def _take_snapshot(
        self,
        level: str,
        desc: str | None,
        tick_type: str = "api_tick",
        on_complete: Callable[[dict[str, Any]], None] | None = None,
    ) -> SnapshotResult:
        """Capture a snapshot of all tracked objects and pause anchors.

        This is the core snapshot logic shared by tick() and _async_tick().
        It runs snapshotters on tracked objects and capturers on pause anchors,
        then unifies the results by key.

        Args:
            level: The level/severity of this tick.
            desc: Optional description.
            tick_type: The tick type (e.g., "api_tick", "remote_client").
            on_complete: Optional callback when write completes.

        Returns:
            SnapshotResult with all captured states.
        """
        with self._lock:
            self._tick_counter += 1
            self._seq_counter += 1
            tick_num = self._tick_counter
            seq_num = self._seq_counter
            timestamp = time.time()
            self._current_tick_type = tick_type

            # 1. Snapshot tracked objects
            # Separate stateless results from owned results
            stateless_results: list[ThingState] = []
            # owned_results: obj_id -> list of (ThingState | ThingDelta)
            owned_results: dict[int, list[ThingState | ThingDelta]] = {}

            for obj_id in list(self._tracked.keys()):
                tracked = self._tracked.get(obj_id)
                if tracked is None:
                    continue

                obj = tracked.obj_ref()
                if obj is None:
                    del self._tracked[obj_id]
                    continue

                snapshotter = self.get_snapshotter(tracked.typename)
                if snapshotter is None:
                    # Broken things go to stateless (they don't claim ownership)
                    stateless_results.append(
                        _make_broken_thing(
                            obj_id, tick_num, f"snapshotter removed for {tracked.typename}"
                        )
                    )
                    continue

                try:
                    if isinstance(snapshotter, StatefulOwningSnapshotter):
                        # Cancel any pending async update before snapshotting
                        self._cancel_async_update_for(obj_id)

                        result = snapshotter.snapshot_owned(obj)

                        # Handle AsyncThingUpdate
                        if isinstance(result, AsyncThingUpdate):
                            # Invariant: we just cancelled any pending async update
                            assert obj_id not in self._pending_async_updates

                            # Use immediate_update for this tick
                            immediate = result.immediate_update
                            if isinstance(immediate, (ThingState, ThingDelta)):
                                items: list[ThingState | ThingDelta] = [immediate]
                            else:
                                items = list(immediate)
                            _validate_snapshotter_result(items)

                            # Queue async work
                            cancel_scope = CancelScope()
                            self._pending_async_updates[obj_id] = cancel_scope
                            job = AsyncUpdateJob(obj_id, cancel_scope, result.async_updater)
                            self._async_update_send.send_nowait(job)

                            owned_results[obj_id] = items
                        elif isinstance(result, (ThingState, ThingDelta)):
                            items = [result]
                            _validate_snapshotter_result(items)
                            owned_results[obj_id] = items
                        else:
                            items = list(result)
                            _validate_snapshotter_result(items)
                            owned_results[obj_id] = items
                    elif isinstance(snapshotter, StatelessSnapshotter):
                        result = snapshotter.snapshot(obj)
                        # Validate before adding to results
                        _validate_snapshotter_result(result)
                        if isinstance(result, ThingState):
                            stateless_results.append(result)
                        else:
                            stateless_results.extend(result)
                    else:
                        # Unknown snapshotter type - treat as error
                        stateless_results.append(
                            _make_broken_thing(
                                obj_id,
                                tick_num,
                                f"snapshotter must be StatelessSnapshotter or StatefulOwningSnapshotter",
                            )
                        )
                except Exception as e:
                    # Errors go to stateless (they don't claim ownership)
                    stateless_results.append(_make_broken_thing(obj_id, tick_num, str(e)))

            # 2. Capture pause anchors
            # pause_results: list of (anchor_id, ThingState | ThingRef)
            pause_results: list[tuple[int, ThingState | ThingRef]] = []

            for anchor_id, (anchor, pause_info) in list(self._paused_anchors.items()):
                capturer = self.get_anchor_capturer(pause_info.typename)
                if capturer is None:
                    # Capturer removed - create broken thing
                    broken = _make_broken_thing(
                        anchor_id, tick_num, f"anchor capturer removed for {pause_info.typename}"
                    )
                    pause_results.append((anchor_id, broken))
                    continue

                try:
                    result = capturer.handle(anchor)
                    # Validate before adding to results
                    _validate_anchor_capturer_result(result)
                    pause_results.append((anchor_id, result))
                except Exception as e:
                    broken = _make_broken_thing(anchor_id, tick_num, str(e))
                    pause_results.append((anchor_id, broken))

            # 3. Capture root anchors
            # root_results: list of (root_name, ThingState | ThingRef)
            root_results: list[tuple[str, ThingState | ThingRef]] = []

            for root_name, (anchor, typename) in list(self._roots.items()):
                capturer = self.get_anchor_capturer(typename)
                if capturer is None:
                    # Capturer removed - create broken thing
                    broken = _make_broken_thing(
                        hash(root_name) & 0xFFFFFFFF,
                        tick_num,
                        f"anchor capturer removed for {typename}",
                    )
                    root_results.append((root_name, broken))
                    continue

                try:
                    result = capturer.handle(anchor)
                    # Validate before adding to results
                    _validate_anchor_capturer_result(result)
                    root_results.append((root_name, result))
                except Exception as e:
                    broken = _make_broken_thing(hash(root_name) & 0xFFFFFFFF, tick_num, str(e))
                    root_results.append((root_name, broken))

            # 4. Unify by key, check ownership conflicts, and build triggerable map
            final_states, triggerable_map, root_bindings = self._unify_snapshots(
                tick_num, stateless_results, owned_results, pause_results, root_results
            )

            # 5. Store triggerable map for trigger lookups
            self._triggerable_at_tick = triggerable_map

            return SnapshotResult(
                tick_number=tick_num,
                seq_number=seq_num,
                timestamp=timestamp,
                level=level,
                desc=desc,
                states=final_states,
                tick_type=tick_type,
                on_complete=on_complete,
                root_bindings=root_bindings,
            )

    def _take_hard_recapture_snapshot(
        self,
        on_complete: Callable[[dict[str, Any]], None] | None = None,
    ) -> SnapshotResult:
        """Capture a fresh snapshot for hard recapture.

        This clears all in-memory tracking state before taking a snapshot,
        and uses stateless_snapshot() for all owning snapshotters to ensure
        we get fresh ThingStates rather than deltas.

        The resulting SnapshotResult has is_hard_recapture=True, which tells
        _process_snapshot to invalidate all live DB records before writing.

        Args:
            on_complete: Optional callback when write completes.

        Returns:
            SnapshotResult with is_hard_recapture=True and all fresh states.
        """
        with self._lock:
            self._tick_counter += 1
            self._seq_counter += 1
            tick_num = self._tick_counter
            seq_num = self._seq_counter
            timestamp = time.time()

            # Clear all in-memory tracking state BEFORE taking snapshot
            # This ensures we start fresh - no deltas, no previous state comparisons
            self._current_things.clear()
            self._current_roots.clear()
            self._owned_things.clear()
            self._thing_owners.clear()
            self._stateless_keys.clear()
            self._current_tick_type = None

            # 1. Snapshot tracked objects using stateless_snapshot for owning snapshotters
            # All results are ThingState (no deltas for hard recapture)
            stateless_results: list[ThingState] = []
            # owned_results still map obj_id -> list, but only ThingState allowed
            owned_results: dict[int, list[ThingState | ThingDelta]] = {}

            for obj_id in list(self._tracked.keys()):
                tracked = self._tracked.get(obj_id)
                if tracked is None:
                    continue

                obj = tracked.obj_ref()
                if obj is None:
                    del self._tracked[obj_id]
                    continue

                snapshotter = self.get_snapshotter(tracked.typename)
                if snapshotter is None:
                    stateless_results.append(
                        _make_broken_thing(
                            obj_id, tick_num, f"snapshotter removed for {tracked.typename}"
                        )
                    )
                    continue

                try:
                    if isinstance(snapshotter, StatefulOwningSnapshotter):
                        # Use stateless_snapshot for hard recapture
                        # Note: all async updates were cancelled at start of hard_recapture
                        result = snapshotter.stateless_snapshot(obj)

                        # Handle AsyncThingUpdate
                        if isinstance(result, AsyncThingUpdate):
                            # Invariant: all async updates were cancelled at start
                            assert obj_id not in self._pending_async_updates

                            # Use immediate_update for this tick
                            immediate = result.immediate_update
                            if isinstance(immediate, ThingState):
                                items: list[ThingState | ThingDelta] = [immediate]
                            else:
                                items = list(immediate)
                            _validate_snapshotter_result(items)

                            # Queue async work
                            cancel_scope = CancelScope()
                            self._pending_async_updates[obj_id] = cancel_scope
                            job = AsyncUpdateJob(obj_id, cancel_scope, result.async_updater)
                            self._async_update_send.send_nowait(job)

                            owned_results[obj_id] = items
                        elif isinstance(result, ThingState):
                            owned_results[obj_id] = [result]
                        else:
                            owned_results[obj_id] = list(result)
                    elif isinstance(snapshotter, StatelessSnapshotter):
                        result = snapshotter.snapshot(obj)
                        # Validate before adding to results
                        _validate_snapshotter_result(result)
                        if isinstance(result, ThingState):
                            stateless_results.append(result)
                        else:
                            stateless_results.extend(result)
                    else:
                        stateless_results.append(
                            _make_broken_thing(
                                obj_id,
                                tick_num,
                                f"snapshotter must be StatelessSnapshotter or StatefulOwningSnapshotter",
                            )
                        )
                except Exception as e:
                    stateless_results.append(_make_broken_thing(obj_id, tick_num, str(e)))

            # 2. Capture pause anchors (same as normal snapshot)
            pause_results: list[tuple[int, ThingState | ThingRef]] = []

            for anchor_id, (anchor, pause_info) in list(self._paused_anchors.items()):
                capturer = self.get_anchor_capturer(pause_info.typename)
                if capturer is None:
                    broken = _make_broken_thing(
                        anchor_id, tick_num, f"anchor capturer removed for {pause_info.typename}"
                    )
                    pause_results.append((anchor_id, broken))
                    continue

                try:
                    result = capturer.handle(anchor)
                    # Validate before adding to results
                    _validate_anchor_capturer_result(result)
                    pause_results.append((anchor_id, result))
                except Exception as e:
                    broken = _make_broken_thing(anchor_id, tick_num, str(e))
                    pause_results.append((anchor_id, broken))

            # 3. Capture root anchors (same as normal snapshot)
            root_results: list[tuple[str, ThingState | ThingRef]] = []

            for root_name, (anchor, typename) in list(self._roots.items()):
                capturer = self.get_anchor_capturer(typename)
                if capturer is None:
                    broken = _make_broken_thing(
                        hash(root_name) & 0xFFFFFFFF,
                        tick_num,
                        f"anchor capturer removed for {typename}",
                    )
                    root_results.append((root_name, broken))
                    continue

                try:
                    result = capturer.handle(anchor)
                    # Validate before adding to results
                    _validate_anchor_capturer_result(result)
                    root_results.append((root_name, result))
                except Exception as e:
                    broken = _make_broken_thing(hash(root_name) & 0xFFFFFFFF, tick_num, str(e))
                    root_results.append((root_name, broken))

            # 4. Unify by key (same as normal snapshot)
            final_states, triggerable_map, root_bindings = self._unify_snapshots(
                tick_num, stateless_results, owned_results, pause_results, root_results
            )

            # 5. Store triggerable map
            self._triggerable_at_tick = triggerable_map

            return SnapshotResult(
                tick_number=tick_num,
                seq_number=seq_num,
                timestamp=timestamp,
                level="hard_recapture",
                desc="Hard recapture - reset all state",
                states=final_states,
                tick_type="hard_recapture",
                on_complete=on_complete,
                root_bindings=root_bindings,
                is_hard_recapture=True,
            )

    def _unify_snapshots(
        self,
        tick_num: int,
        stateless_results: list[ThingState],
        owned_results: dict[int, list[ThingState | ThingDelta]],
        pause_results: list[tuple[int, ThingState | ThingRef]],
        root_results: list[tuple[str, ThingState | ThingRef]] | None = None,
    ) -> tuple[
        list[ThingState | ThingDelta], dict[bytes, list[tuple[int, PauseInfo]]], dict[str, bytes]
    ]:
        """Unify stateless, owned, pause, and root results, checking for conflicts.

        Rules:
        - Owned keys (from StatefulOwningSnapshotter) must be unique: cannot overlap with
          any other source (other owned keys, stateless keys, anchor capturers, roots)
        - Stateless overlap is OK: multiple stateless sources can emit the same key if
          the ThingState values are identical
        - ThingRefs just reference - need a matching state or delta to exist
        - Conflicts produce a broken thing with original key, type="__broken__", data=error

        After validation, this method updates the ownership and stateless key tracking maps.

        Returns:
            (final_states, triggerable_map, root_bindings) where:
            - triggerable_map is key_bytes -> list of (anchor_id, pause_info)
            - root_bindings is root_name -> thing_key_bytes
        """
        if root_results is None:
            root_results = []

        # Group by key for stateless sources only
        stateless_by_key: dict[bytes, list[ThingState]] = {}
        # Track owned keys: key_bytes -> (obj_id, ThingState | ThingDelta)
        owned_by_key: dict[bytes, tuple[int, ThingState | ThingDelta]] = {}
        # Track keys that have ownership conflicts
        ownership_conflicts: dict[bytes, str] = {}

        pause_keys: dict[bytes, list[tuple[int, PauseInfo]]] = {}
        ref_keys: set[bytes] = set()
        root_key_map: dict[str, bytes] = {}

        # Track new ownership: obj_id -> set of key_bytes (to update maps later)
        new_owned_things: dict[int, set[bytes]] = {}

        # 1. Process owned results first to establish ownership claims
        for obj_id, items in owned_results.items():
            new_owned_things[obj_id] = set()
            for item in items:
                key_bytes = cbor2.dumps(item.key, canonical=True)
                new_owned_things[obj_id].add(key_bytes)

                if key_bytes in owned_by_key:
                    # Conflict: two owning snapshotters claim same key
                    other_obj_id, _ = owned_by_key[key_bytes]
                    ownership_conflicts[key_bytes] = (
                        f"ownership conflict: object {obj_id} and object {other_obj_id} "
                        f"both claim ownership of key"
                    )
                else:
                    owned_by_key[key_bytes] = (obj_id, item)

        # 2. Process stateless results
        for item in stateless_results:
            key_bytes = cbor2.dumps(item.key, canonical=True)

            if key_bytes in owned_by_key:
                # Conflict: stateless source overlaps with owned key
                obj_id, _ = owned_by_key[key_bytes]
                ownership_conflicts[key_bytes] = (
                    f"ownership conflict: stateless snapshotter emitted key owned by object {obj_id}"
                )
            else:
                stateless_by_key.setdefault(key_bytes, []).append(item)

        # 3. Process pause results (anchor capturers are always stateless)
        for anchor_id, item in pause_results:
            _, pause_info = self._paused_anchors.get(anchor_id, (None, None))
            if pause_info is None:
                continue  # Anchor was removed during snapshot

            if isinstance(item, ThingRef):
                key_bytes = cbor2.dumps(item.key, canonical=True)
                ref_keys.add(key_bytes)
                pause_keys.setdefault(key_bytes, []).append((anchor_id, pause_info))
            else:  # ThingState (anchor capturers don't return ThingDelta)
                key_bytes = cbor2.dumps(item.key, canonical=True)

                if key_bytes in owned_by_key:
                    # Conflict: anchor capturer overlaps with owned key
                    obj_id, _ = owned_by_key[key_bytes]
                    ownership_conflicts[key_bytes] = (
                        f"ownership conflict: anchor capturer emitted key owned by object {obj_id}"
                    )
                else:
                    stateless_by_key.setdefault(key_bytes, []).append(item)

                pause_keys.setdefault(key_bytes, []).append((anchor_id, pause_info))

        # 4. Process root results (anchor capturers are always stateless)
        for root_name, item in root_results:
            if isinstance(item, ThingRef):
                key_bytes = cbor2.dumps(item.key, canonical=True)
                ref_keys.add(key_bytes)
                root_key_map[root_name] = key_bytes
            else:  # ThingState (anchor capturers don't return ThingDelta)
                key_bytes = cbor2.dumps(item.key, canonical=True)

                if key_bytes in owned_by_key:
                    # Conflict: root anchor capturer overlaps with owned key
                    obj_id, _ = owned_by_key[key_bytes]
                    ownership_conflicts[key_bytes] = (
                        f"ownership conflict: root '{root_name}' emitted key owned by object {obj_id}"
                    )
                else:
                    stateless_by_key.setdefault(key_bytes, []).append(item)

                root_key_map[root_name] = key_bytes

        # 5. Check for orphan ThingRefs
        all_keys = set(owned_by_key.keys()) | set(stateless_by_key.keys())
        for key_bytes in ref_keys:
            if key_bytes not in all_keys:
                # Create broken thing - treat as stateless
                original_key = cbor2.loads(key_bytes)
                broken = _make_broken_thing_with_key(original_key, "ThingRef has no matching state")
                stateless_by_key[key_bytes] = [broken]

        # 6. Build final states list
        final_states: list[ThingState | ThingDelta] = []
        new_stateless_keys: set[bytes] = set()

        # Add owned results (checking for conflicts)
        for key_bytes, (obj_id, item) in owned_by_key.items():
            if key_bytes in ownership_conflicts:
                error_msg = ownership_conflicts[key_bytes]
                logger.error(f"Snapshot conflict at tick {tick_num}: {error_msg}")
                original_key = cbor2.loads(key_bytes)
                broken = _make_broken_thing_with_key(original_key, error_msg)
                final_states.append(broken)
            else:
                final_states.append(item)

        # Validate and add stateless results
        for key_bytes, items in stateless_by_key.items():
            if key_bytes in ownership_conflicts:
                # Already handled via owned_by_key
                continue

            original_key = cbor2.loads(key_bytes)
            error_msg = None

            # All items should be ThingState (stateless sources don't produce ThingDelta)
            # But check anyway for safety
            thing_states = [i for i in items if isinstance(i, ThingState)]
            thing_deltas = [i for i in items if isinstance(i, ThingDelta)]

            if thing_deltas:
                error_msg = "ThingDelta from stateless source (must use StatefulOwningSnapshotter)"
            elif len(thing_states) > 1:
                # Check all states are equal
                first = thing_states[0]
                for other in thing_states[1:]:
                    if not _thing_states_equal(first, other):
                        error_msg = "multiple ThingStates with different data for same key"
                        break

            if error_msg:
                logger.error(f"Snapshot conflict at tick {tick_num}: {error_msg}")
                broken = _make_broken_thing_with_key(original_key, error_msg)
                final_states.append(broken)
            elif thing_states:
                # One or more identical states - use first
                final_states.append(thing_states[0])
                new_stateless_keys.add(key_bytes)

        # 7. Update ownership and stateless tracking maps
        # Clear old ownership for objects that were snapshotted
        for obj_id in owned_results.keys():
            old_keys = self._owned_things.get(obj_id, set())
            for old_key in old_keys:
                if old_key not in new_owned_things.get(obj_id, set()):
                    self._thing_owners.pop(old_key, None)

        # Set new ownership (only for non-conflicting keys)
        for obj_id, key_set in new_owned_things.items():
            valid_keys = {k for k in key_set if k not in ownership_conflicts}
            self._owned_things[obj_id] = valid_keys
            for key_bytes in valid_keys:
                self._thing_owners[key_bytes] = obj_id

        # Update stateless keys
        self._stateless_keys = new_stateless_keys

        return final_states, pause_keys, root_key_map

    # =========================================================================
    # Async Remote Tick
    # =========================================================================

    async def _async_tick(self, level: str, desc: str | None, tick_type: str) -> dict[str, Any]:
        """Trigger a tick and wait for it to be written. Returns tick data.

        Unlike the public tick() method which is sync and fire-and-forget,
        this method waits for the write to complete and returns the tick info.

        Args:
            level: The level of the tick.
            desc: Optional description.
            tick_type: The tick type (e.g., "remote_client").

        Returns:
            Dict with tick info: {tick, timestamp, level, type, data}

        Raises:
            ValueError: If level is "stream" (reserved for streaming updates).
        """
        if level == "stream":
            raise ValueError("Level 'stream' is reserved for streaming updates")
        done_event = Event()
        result_holder: dict[str, Any] = {}

        def on_write_complete(tick_data: dict[str, Any]) -> None:
            result_holder.update(tick_data)
            done_event.set()

        snapshot = self._take_snapshot(level, desc, tick_type, on_complete=on_write_complete)
        self._write_stream.send_nowait(snapshot)
        await done_event.wait()

        return result_holder

    async def hard_recapture(self) -> dict[str, Any]:
        """Perform a hard recapture: reset all state and take a fresh snapshot.

        This is a "break glass" operation that:
        1. Cancels all pending async updates
        2. Clears all in-memory tracking state (current things, roots, ownership)
        3. Invalidates all live records in the database
        4. Takes a fresh snapshot using stateless_snapshot() for all owning snapshotters
        5. Writes the new state as a "hard_recapture" tick

        The result is that every tracked object gets a completely fresh snapshot
        as if it just started being tracked. Useful for recovering from state
        drift or forcing a clean baseline.

        Returns:
            Dict with tick info: {tick, timestamp, level, type, data}
        """
        # Cancel all pending async updates before hard recapture
        self._cancel_all_async_updates()

        done_event = Event()
        result_holder: dict[str, Any] = {}

        def on_write_complete(tick_data: dict[str, Any]) -> None:
            result_holder.update(tick_data)
            done_event.set()

        snapshot = self._take_hard_recapture_snapshot(on_complete=on_write_complete)
        self._write_stream.send_nowait(snapshot)
        await done_event.wait()

        return result_holder

    def track(self, obj: Any, typename: str | type[Any] | None = None) -> None:
        """Track an object with this watcher.

        Thread-safe: can be called from any thread.

        Args:
            obj: The object to track. Must be weakref-referenceable.
            typename: The typename for snapshotter lookup. Defaults to module.qualname.

        Raises:
            ValueError: If no snapshotter is registered for the typename.
            ValueError: If the object is not weakref-referenceable.
        """
        # Resolve typename
        if typename is None:
            resolved_typename = resolve_typename(type(obj))
        else:
            resolved_typename = resolve_typename(typename)

        # Check snapshotter exists at track time
        snapshotter = self.get_snapshotter(resolved_typename)
        if snapshotter is None:
            raise ValueError(f"no snapshotter registered for {resolved_typename}")

        # Create weak reference
        obj_id = id(obj)

        def _remove_on_finalize(ref: weakref.ref[Any]) -> None:
            # Remove from tracked dict when object is garbage collected
            # Must acquire lock since this can be called from any thread during GC
            with self._lock:
                self._tracked.pop(obj_id, None)
                # Clean up ownership maps if this object owned any things
                owned_keys = self._owned_things.pop(obj_id, None)
                if owned_keys:
                    for key_bytes in owned_keys:
                        self._thing_owners.pop(key_bytes, None)
                # Cancel any pending async update for this object
                self._cancel_async_update_for(obj_id)

        try:
            ref = weakref.ref(obj, _remove_on_finalize)
        except TypeError:
            raise ValueError("object must be weakref-referenceable")

        with self._lock:
            self._tracked[obj_id] = TrackedObject(typename=resolved_typename, obj_ref=ref)

    def untrack(self, obj: Any) -> None:
        """Stop tracking an object.

        Thread-safe: can be called from any thread.

        Args:
            obj: The object to stop tracking.
        """
        obj_id = id(obj)
        with self._lock:
            self._tracked.pop(obj_id, None)
            # Clean up ownership maps if this object owned any things
            owned_keys = self._owned_things.pop(obj_id, None)
            if owned_keys:
                for key_bytes in owned_keys:
                    self._thing_owners.pop(key_bytes, None)
            # Cancel any pending async update for this object
            self._cancel_async_update_for(obj_id)

    def track_fxtr_id(
        self,
        fxtr_id: FxtrID,
        from_scope: FxtrScope | None = None,
    ) -> list:
        """Track an fxtr entity for visualization.

        Queues the entity for async copy to the internal namespace and returns
        the thing_key immediately. The entity will become available for graph
        navigation once the copy completes.

        Args:
            fxtr_id: The fxtr entity ID to track.
            from_scope: Scope to copy from. If None, uses ACTIVE_SCOPE.

        Returns:
            The thingwatcher thing_key: ["fxtr_entity", id_string]

        Raises:
            FxtrSupportDisabledError: If fxtr_support=False on this watcher.
            ValueError: If fxtr_id is not tracked in the source scope.
        """
        if not self._fxtr_support:
            raise FxtrSupportDisabledError(
                "track_fxtr_id requires fxtr_support=True on the Watcher"
            )

        from fxtr2.data.fxtr_scope import ACTIVE_SCOPE

        from .fxtr.cbor_transform import fxtr_key_to_thing_key

        # Get scope
        if from_scope is None:
            from_scope = ACTIVE_SCOPE.get()

        # Verify entity is tracked in source scope (early error detection)
        if not from_scope.is_tracked(fxtr_id):
            raise ValueError(
                f"fxtr_id {fxtr_id} is not tracked in the source scope. "
                "Ensure the entity is saved/lifted into the scope before calling track_id()."
            )

        # Queue copy job (deduplicate)
        if fxtr_id not in self._fxtr_tracked_ids:
            self._fxtr_tracked_ids.add(fxtr_id)
            assert self._fxtr_copy_send is not None
            self._fxtr_copy_send.send_nowait((fxtr_id, from_scope))

        # Return thing key immediately
        return fxtr_key_to_thing_key(fxtr_id)

    def tick(self, level: str, desc: str | None = None) -> None:
        """Record a tick, snapshotting all tracked objects.

        Thread-safe: can be called from any thread. Only one tick runs at a time
        per watcher. Snapshotters run while the lock is held, so the user is
        responsible for ensuring their objects are safe to access.

        IMPORTANT: tick() must NEVER raise an exception when called with valid arguments.
        If a snapshotter raises an exception or returns invalid data (e.g., invalid
        ThingDelta), the error is captured as a "broken thing" rather than propagating
        to the caller. This ensures instrumented code can always continue running.

        Args:
            level: The level/severity of this tick.
            desc: Optional description of what happened.

        Raises:
            ValueError: If level is "stream" (reserved for streaming updates).
        """
        if level == "stream":
            raise ValueError("Level 'stream' is reserved for streaming updates")
        snapshot = self._take_snapshot(level, desc)

        # Call interceptor if set (for testing)
        if self._tick_interceptor is not None:
            self._tick_interceptor(snapshot)

        self._log_tick(snapshot)

        # Enqueue write job (buffered until writer starts in watch_and_serve_until)
        self._write_stream.send_nowait(snapshot)

    def streaming_update(self, obj: Any) -> None:
        """Update a single tracked object in streaming fashion.

        Thread-safe: can be called from any thread.

        This creates or updates a "stream" tick that only reflects changes to
        this specific object. Multiple streaming updates to the same stream tick
        overwrite previous state for this object while preserving other objects.

        The object must be tracked and have a StatefulOwningSnapshotter.

        May raise ValueError if `obj` is misconfigured (not tracked, no owning
        snapshotter). However, snapshotter errors and invalid ThingDelta values
        are captured as "broken things" rather than propagating to the caller.

        Args:
            obj: A tracked object with an owning snapshotter.

        Raises:
            ValueError: If object is not tracked or doesn't have an owning snapshotter.
        """
        self._take_streaming_update(obj)

    def _take_streaming_update(self, obj: Any) -> None:
        """Capture a streaming update for a single object and queue it.

        Validates the object, snapshots it, and pushes the result to a
        streaming tick via _push_streaming_states.

        Args:
            obj: The tracked object to snapshot.

        Raises:
            ValueError: If object is not tracked or doesn't have owning snapshotter.
        """
        with self._lock:
            obj_id = id(obj)

            # 1. Validate: object must be tracked
            tracked = self._tracked.get(obj_id)
            if tracked is None:
                raise ValueError(
                    f"Cannot streaming_update: object {type(obj).__name__} is not tracked"
                )

            # Verify obj_ref is still valid and points to this obj
            assert tracked.obj_ref() is obj, "impossible: weakref broke without cleanup or bad GC?"

            # 2. Validate: must have StatefulOwningSnapshotter
            snapshotter = self.get_snapshotter(tracked.typename)
            if not isinstance(snapshotter, StatefulOwningSnapshotter):
                raise ValueError(
                    f"Cannot streaming_update: {tracked.typename} does not have a "
                    f"StatefulOwningSnapshotter (has {type(snapshotter).__name__})"
                )

            # 3. Cancel any pending async update before snapshotting
            self._cancel_async_update_for(obj_id)

            # 4. Snapshot the single object
            try:
                result = snapshotter.snapshot_owned(obj)

                # Handle AsyncThingUpdate
                if isinstance(result, AsyncThingUpdate):
                    # Invariant: we just cancelled any pending async update
                    assert obj_id not in self._pending_async_updates

                    # Use immediate_update for the current streaming tick
                    immediate = result.immediate_update
                    if isinstance(immediate, (ThingState, ThingDelta)):
                        states: list[ThingState | ThingDelta] = [immediate]
                    else:
                        states = list(immediate)

                    # Validate immediate states
                    _validate_snapshotter_result(states)

                    # Queue async work
                    cancel_scope = CancelScope()
                    self._pending_async_updates[obj_id] = cancel_scope
                    job = AsyncUpdateJob(obj_id, cancel_scope, result.async_updater)
                    self._async_update_send.send_nowait(job)
                elif isinstance(result, (ThingState, ThingDelta)):
                    states = [result]
                    _validate_snapshotter_result(states)
                else:
                    states = list(result)
                    _validate_snapshotter_result(states)

            except Exception as e:
                # Error - produce broken thing
                states = [_make_broken_thing(obj_id, self._tick_counter, str(e))]

        # Push to streaming tick (handles tick/ownership/queuing)
        # Note: _push_streaming_states acquires _lock internally (RLock allows this)
        self._push_streaming_states(obj_id, states)

    def _log_tick(self, result: SnapshotResult) -> None:
        """Log a tick result as JSON."""
        try:
            output = {
                "tick": result.tick_number,
                "timestamp": result.timestamp,
                "level": result.level,
                "desc": result.desc,
                "things": [_serialize_thing(s) for s in result.states],
            }
            logger.info(json.dumps(output))
        except Exception:
            logger.error("Error while logging a tick; misconfigured?", exc_info=True)

    # =========================================================================
    # Background Writer (DB persistence)
    # =========================================================================

    async def _db_writer(
        self, receive_stream: MemoryObjectReceiveStream[SnapshotResult | StreamingUpdateResult]
    ) -> None:
        """Background task that writes to the database."""
        async for job in receive_stream:
            if isinstance(job, StreamingUpdateResult):
                await self._process_streaming_update(job)
            else:
                await self._process_snapshot(job)

    async def _process_snapshot(self, snapshot: SnapshotResult) -> None:
        """Process a snapshot result atomically in one transaction.

        Flow:
        1. Write tick record
        1b. (Hard recapture only) Invalidate all live records in DB
        2. Process each snapshot, comparing to previous state
        3. Process root bindings
        4. Invalidate things that disappeared
        5. Apply in-memory updates (after commit!)
        6. Notify subscribers and completion callback
        """
        tick = snapshot.tick_number
        seq = snapshot.seq_number
        seen_keys: set[bytes] = set()

        # Collect pending in-memory updates - apply ONLY after transaction commits
        pending_thing_updates: dict[bytes, CurrentThingInfo] = {}
        pending_thing_deletes: set[bytes] = set()

        async with self._db.begin() as conn:
            # 1. Write tick record
            await self._db.write_tick(
                conn,
                tick,
                snapshot.timestamp,
                snapshot.level,
                snapshot.tick_type,
                snapshot.desc,
                seq,
            )

            # 1b. (Hard recapture only) Invalidate all live records before writing new state
            if snapshot.is_hard_recapture:
                await self._db.invalidate_all_live(conn, tick, seq)

            # 2. Process each snapshot
            for item in snapshot.states:
                if isinstance(item, ThingState):
                    key_bytes, new_info = await self._process_thing_state(conn, tick, seq, item)
                else:  # ThingDelta
                    key_bytes, new_info = await self._process_thing_delta(conn, tick, seq, item)
                seen_keys.add(key_bytes)
                pending_thing_updates[key_bytes] = new_info
                # Ensure short ID exists for this thing
                await self._db.get_or_create_short_id(conn, key_bytes)

            # 3. Process root bindings
            pending_root_updates, pending_root_deletes = await self._process_roots(
                conn, tick, seq, snapshot.root_bindings
            )

            # 4. Invalidate things that disappeared
            for key_bytes in list(self._current_things.keys()):
                if key_bytes not in seen_keys:
                    await self._invalidate_thing(conn, tick, seq, key_bytes)
                    pending_thing_deletes.add(key_bytes)

            # Transaction commits automatically when context exits

        # 5. After commit: apply in-memory updates
        # This ensures consistency - if transaction rolled back, memory stays unchanged
        self._current_things.update(pending_thing_updates)
        for key_bytes in pending_thing_deletes:
            self._current_things.pop(key_bytes, None)
        self._current_roots.update(pending_root_updates)
        for root_name in pending_root_deletes:
            self._current_roots.pop(root_name, None)

        # 6. After commit: build tick_data dict and notify
        tick_data = {
            "tick": snapshot.tick_number,
            "timestamp": snapshot.timestamp,
            "level": snapshot.level,
            "type": snapshot.tick_type,
            "data": snapshot.desc,
        }

        # Notify subscribers
        self._notify_tick_subscribers(tick_data)

        # Call completion callback if present
        if snapshot.on_complete is not None:
            snapshot.on_complete(tick_data)

    async def _process_streaming_update(self, update: StreamingUpdateResult) -> None:
        """Process a streaming update atomically in one transaction.

        Flow:
        1. Create new tick or update existing tick seq/timestamp
        2. Process states for the single updated object
        3. Invalidate disappeared things owned by this object
        4. Apply in-memory updates (after commit!)
        5. Notify subscribers
        """
        tick = update.tick_number
        seq = update.seq_number

        # Collect pending in-memory updates - apply ONLY after transaction commits
        pending_thing_updates: dict[bytes, CurrentThingInfo] = {}
        pending_thing_deletes: set[bytes] = set()

        async with self._db.begin() as conn:
            if update.is_new_tick:
                # 1a. Create new stream tick
                await self._db.write_tick(
                    conn, tick, update.timestamp, "stream", "stream", None, seq
                )
            else:
                # 1b. Update existing stream tick's seq and timestamp
                await self._db.update_tick_seq(conn, tick, seq, update.timestamp)

            # 2. Process states for this object
            for item in update.states:
                if isinstance(item, ThingState):
                    key_bytes, new_info = await self._process_thing_state(conn, tick, seq, item)
                else:  # ThingDelta
                    key_bytes, new_info = await self._process_thing_delta(conn, tick, seq, item)
                pending_thing_updates[key_bytes] = new_info
                # Ensure short ID exists
                await self._db.get_or_create_short_id(conn, key_bytes)

            # 3. Invalidate things that disappeared (owned by this object)
            for key_bytes in update.disappeared_keys:
                if key_bytes in self._current_things:
                    await self._invalidate_thing(conn, tick, seq, key_bytes)
                    pending_thing_deletes.add(key_bytes)

            # Transaction commits automatically when context exits

        # 4. After commit: apply in-memory updates
        self._current_things.update(pending_thing_updates)
        for key_bytes in pending_thing_deletes:
            self._current_things.pop(key_bytes, None)

        # 5. After commit: build tick_data dict and notify
        tick_data = {
            "tick": update.tick_number,
            "timestamp": update.timestamp,
            "level": "stream",
            "type": "stream",
            "data": None,
            "is_streaming_update": True,
        }

        # Notify subscribers (they can filter based on is_streaming_update)
        self._notify_tick_subscribers(tick_data)

        # Call completion callback if present
        if update.on_complete is not None:
            update.on_complete(tick_data)

    async def _process_thing_state(
        self, conn: AsyncConnection, tick: int, seq: int, state: ThingState
    ) -> tuple[bytes, CurrentThingInfo]:
        """Process a ThingState snapshot, comparing to previous.

        Returns (thing_key as CBOR bytes, new CurrentThingInfo to apply after commit).
        Does NOT modify _current_things directly - caller must apply after transaction commits.
        """
        key_bytes = cbor2.dumps(state.key, canonical=True)
        new_data_bytes = cbor2.dumps(state.thing_data)
        prev = self._current_things.get(key_bytes)

        # Build new links map for comparison
        new_links: dict[bytes, ThingLink] = {}
        for link in state.links:
            link_key_bytes = cbor2.dumps(link.link_key, canonical=True)
            new_links[link_key_bytes] = link

        if prev is None:
            # New thing - create fresh data chunk and thing_state
            data_chunk_id = await self._db.write_data_chunk(conn, state.thing_data, extends=None)
            new_id = await self._db.write_thing_state(
                conn,
                valid_from=tick,
                valid_from_seq=seq,
                thing_key=state.key,
                thing_type=state.thing_type,
                data_chunk_id=data_chunk_id,
            )
            new_link_infos = await self._write_links(conn, tick, seq, state.key, new_links)
            new_info = CurrentThingInfo(
                thingstate_id=new_id,
                thing_type=state.thing_type,
                data_chunk_id=data_chunk_id,
                last_was_state=True,
                data_bytes=new_data_bytes,
                links=new_link_infos,
            )
        else:
            # Existing thing - determine if we can reuse data chunk
            if prev.last_was_state and prev.data_bytes == new_data_bytes:
                # Same data as previous ThingState - reuse data chunk
                data_chunk_id = prev.data_chunk_id
            else:
                # Different data or previous was ThingDelta - new chain
                data_chunk_id = await self._db.write_data_chunk(
                    conn, state.thing_data, extends=None
                )

            # Check if we need a new thingstate_id
            links_changed = self._links_changed(prev.links, new_links)
            data_chunk_changed = data_chunk_id != prev.data_chunk_id

            if data_chunk_changed or links_changed:
                # Data chunk or links changed - invalidate old state, write new state
                await self._db.invalidate_thing_state(conn, prev.thingstate_id, tick, seq)
                new_id = await self._db.write_thing_state(
                    conn,
                    valid_from=tick,
                    valid_from_seq=seq,
                    thing_key=state.key,
                    thing_type=state.thing_type,
                    data_chunk_id=data_chunk_id,
                )
            else:
                # Nothing changed, keep existing state
                new_id = prev.thingstate_id

            # Diff and update links (regardless of whether we wrote new state)
            new_link_infos = await self._diff_links(
                conn, tick, seq, state.key, prev.links, new_links
            )

            new_info = CurrentThingInfo(
                thingstate_id=new_id,
                thing_type=state.thing_type,
                data_chunk_id=data_chunk_id,
                last_was_state=True,
                data_bytes=new_data_bytes,
                links=new_link_infos,
            )

        return key_bytes, new_info

    async def _process_thing_delta(
        self, conn: AsyncConnection, tick: int, seq: int, delta: ThingDelta
    ) -> tuple[bytes, CurrentThingInfo]:
        """Process a ThingDelta with push_chunk/pop_chunks logic.

        Returns (thing_key as CBOR bytes, new CurrentThingInfo to apply after commit).
        Does NOT modify _current_things directly - caller must apply after transaction commits.

        Validation errors (pop_chunks > chain_length, etc.) produce a broken thing
        rather than raising, since validation requires DB access and we want to
        continue processing other things in the tick.
        """
        key_bytes = cbor2.dumps(delta.key, canonical=True)
        prev = self._current_things.get(key_bytes)

        if prev is None:
            # ThingDelta requires the thing to already exist
            # (new things must use ThingState, not ThingDelta)
            raise ValueError(f"ThingDelta for unknown thing: {delta.key}")

        thing_type = prev.thing_type

        # Parse push/pop operations
        has_push = not isinstance(delta.push_chunk, _NoPushedChunk)
        is_pop_all = isinstance(delta.pop_chunks, _PopAll)

        # Note: POP_ALL without push and negative pop_chunks are already validated
        # in _take_snapshot/_take_streaming_update before we get here.
        # Here we only validate things that require DB access (chain length).

        # Get pop_count as int (0 for POP_ALL case, which is handled separately)
        pop_count = 0 if is_pop_all else int(delta.pop_chunks)  # type: ignore[arg-type]

        assert self._db is not None
        try:
            if is_pop_all:
                # Pop everything, push new (validation already ensured has_push)
                data_chunk_id = await self._db.write_data_chunk(
                    conn, delta.push_chunk, extends=None
                )

            elif pop_count > 0:
                # Walk the chain to find the target
                # Returns int if chunks remain, None if popped exactly all, raises if too many
                walked_id = await self._db.walk_chunk_chain(conn, prev.data_chunk_id, pop_count)

                if has_push:
                    # Pop N, then push: walked_id can be None (fresh start) or int (extends)
                    data_chunk_id = await self._db.write_data_chunk(
                        conn, delta.push_chunk, extends=walked_id
                    )
                else:
                    # Pop N, no push: walked_id must be int (can't leave 0 chunks)
                    if walked_id is None:
                        raise ValueError(
                            f"Cannot pop {pop_count} chunks without push: would leave 0"
                        )
                    data_chunk_id = walked_id

            elif has_push:
                # Normal append (pop_count == 0)
                data_chunk_id = await self._db.write_data_chunk(
                    conn, delta.push_chunk, extends=prev.data_chunk_id
                )

            else:
                # No change (no push, no pop)
                data_chunk_id = prev.data_chunk_id

        except ValueError as e:
            # Async validation failed - produce broken thing
            logger.warning(f"ThingDelta validation failed for {delta.key}: {e}")
            data_chunk_id = await self._db.write_data_chunk(conn, {"error": str(e)}, extends=None)
            thing_type = "__broken__"

        # Check if links changed - easy check from delta fields
        links_changed = bool(delta.added_links or delta.removed_link_keys)

        # Handle link changes from delta
        new_link_infos = await self._apply_link_delta(
            conn, tick, seq, delta.key, prev.links, delta.added_links, delta.removed_link_keys
        )

        # Check if we need a new thingstate_id
        data_chunk_changed = data_chunk_id != prev.data_chunk_id

        if data_chunk_changed or links_changed:
            await self._db.invalidate_thing_state(conn, prev.thingstate_id, tick, seq)
            new_id = await self._db.write_thing_state(
                conn,
                valid_from=tick,
                valid_from_seq=seq,
                thing_key=delta.key,
                thing_type=thing_type,
                data_chunk_id=data_chunk_id,
            )
        else:
            new_id = prev.thingstate_id

        new_info = CurrentThingInfo(
            thingstate_id=new_id,
            thing_type=thing_type,
            data_chunk_id=data_chunk_id,
            last_was_state=False,  # ThingDelta, so next ThingState won't compare
            data_bytes=None,  # Not valid after ThingDelta
            links=new_link_infos,
        )

        return key_bytes, new_info

    def _links_changed(
        self,
        old_links: dict[bytes, CurrentLinkInfo],
        new_links: dict[bytes, ThingLink],
    ) -> bool:
        """Check if links have changed between old and new state.

        Compares by link_key, link_type, and to_thing_key.
        Returns True if any link was added, removed, or modified.
        """
        # Different number of links = changed
        if len(old_links) != len(new_links):
            return True

        # Check each new link against old
        for link_key_bytes, new_link in new_links.items():
            old_info = old_links.get(link_key_bytes)
            if old_info is None:
                # New link key that didn't exist before
                return True

            # Compare link_type
            if old_info.link_type != new_link.link_type:
                return True

            # Compare to_thing_key
            new_to_bytes = cbor2.dumps(new_link.to_thing_key, canonical=True)
            if old_info.to_thing_key_bytes != new_to_bytes:
                return True

        return False

    async def _write_links(
        self,
        conn: AsyncConnection,
        tick: int,
        seq: int,
        from_key: Any,
        links: dict[bytes, ThingLink],
    ) -> dict[bytes, CurrentLinkInfo]:
        """Write all links for a new thing. Returns link tracking info."""
        result: dict[bytes, CurrentLinkInfo] = {}
        for link_key_bytes, link in links.items():
            link_id = await self._db.write_link(
                conn,
                valid_from=tick,
                valid_from_seq=seq,
                from_thing_key=from_key,
                link_type=link.link_type,
                link_key=link.link_key,
                to_thing_key=link.to_thing_key,
            )
            result[link_key_bytes] = CurrentLinkInfo(
                link_id=link_id,
                link_type=link.link_type,
                to_thing_key_bytes=cbor2.dumps(link.to_thing_key, canonical=True),
            )
        return result

    async def _diff_links(
        self,
        conn: AsyncConnection,
        tick: int,
        seq: int,
        from_key: Any,
        old_links: dict[bytes, CurrentLinkInfo],
        new_links: dict[bytes, ThingLink],
    ) -> dict[bytes, CurrentLinkInfo]:
        """Diff old and new links, writing changes. Returns new link tracking info."""
        result: dict[bytes, CurrentLinkInfo] = {}

        # Check each new link
        for link_key_bytes, link in new_links.items():
            old_info = old_links.get(link_key_bytes)
            new_to_bytes = cbor2.dumps(link.to_thing_key, canonical=True)

            if old_info is None:
                # New link
                link_id = await self._db.write_link(
                    conn,
                    valid_from=tick,
                    valid_from_seq=seq,
                    from_thing_key=from_key,
                    link_type=link.link_type,
                    link_key=link.link_key,
                    to_thing_key=link.to_thing_key,
                )
                result[link_key_bytes] = CurrentLinkInfo(
                    link_id=link_id,
                    link_type=link.link_type,
                    to_thing_key_bytes=new_to_bytes,
                )
            elif (
                old_info.to_thing_key_bytes != new_to_bytes or old_info.link_type != link.link_type
            ):
                # Link changed - invalidate old, write new
                await self._db.invalidate_link(conn, old_info.link_id, tick, seq)
                link_id = await self._db.write_link(
                    conn,
                    valid_from=tick,
                    valid_from_seq=seq,
                    from_thing_key=from_key,
                    link_type=link.link_type,
                    link_key=link.link_key,
                    to_thing_key=link.to_thing_key,
                )
                result[link_key_bytes] = CurrentLinkInfo(
                    link_id=link_id,
                    link_type=link.link_type,
                    to_thing_key_bytes=new_to_bytes,
                )
            else:
                # Link unchanged - keep old info
                result[link_key_bytes] = old_info

        # Invalidate removed links
        for link_key_bytes, old_info in old_links.items():
            if link_key_bytes not in new_links:
                await self._db.invalidate_link(conn, old_info.link_id, tick, seq)

        return result

    async def _process_roots(
        self,
        conn: AsyncConnection,
        tick: int,
        seq: int,
        root_bindings: dict[str, bytes],
    ) -> tuple[dict[str, CurrentRootInfo], set[str]]:
        """Process root bindings, comparing to previous state.

        - New roots: write_root()
        - Changed roots (different key): invalidate old, write new
        - Removed roots: invalidate
        - Unchanged roots: no-op

        Returns (pending_updates, pending_deletes) to apply after commit.
        Does NOT modify _current_roots directly.
        """
        seen_roots: set[str] = set()
        pending_updates: dict[str, CurrentRootInfo] = {}
        pending_deletes: set[str] = set()

        # Process current root bindings
        for root_name, thing_key_bytes in root_bindings.items():
            seen_roots.add(root_name)
            prev = self._current_roots.get(root_name)

            if prev is None:
                # New root
                root_id = await self._db.write_root(
                    conn,
                    root_name=root_name,
                    thing_key_bytes=thing_key_bytes,
                    valid_from=tick,
                    valid_from_seq=seq,
                )
                pending_updates[root_name] = CurrentRootInfo(
                    root_db_id=root_id,
                    thing_key_bytes=thing_key_bytes,
                )
            elif prev.thing_key_bytes != thing_key_bytes:
                # Root changed target - invalidate old, write new
                await self._db.invalidate_root(conn, prev.root_db_id, tick, seq)
                root_id = await self._db.write_root(
                    conn,
                    root_name=root_name,
                    thing_key_bytes=thing_key_bytes,
                    valid_from=tick,
                    valid_from_seq=seq,
                )
                pending_updates[root_name] = CurrentRootInfo(
                    root_db_id=root_id,
                    thing_key_bytes=thing_key_bytes,
                )
            # else: unchanged, keep existing

        # Invalidate removed roots
        for root_name in list(self._current_roots.keys()):
            if root_name not in seen_roots:
                prev = self._current_roots[root_name]
                await self._db.invalidate_root(conn, prev.root_db_id, tick, seq)
                pending_deletes.add(root_name)

        return pending_updates, pending_deletes

    async def _apply_link_delta(
        self,
        conn: AsyncConnection,
        tick: int,
        seq: int,
        from_key: Any,
        old_links: dict[bytes, CurrentLinkInfo],
        added_links: list[ThingLink],
        removed_link_keys: list[Any],
    ) -> dict[bytes, CurrentLinkInfo]:
        """Apply explicit link delta. Returns new link tracking info."""
        result = dict(old_links)  # Start with existing

        # Remove links
        for link_key in removed_link_keys:
            link_key_bytes = cbor2.dumps(link_key, canonical=True)
            if link_key_bytes in result:
                await self._db.invalidate_link(conn, result[link_key_bytes].link_id, tick, seq)
                del result[link_key_bytes]

        # Add links
        for link in added_links:
            link_key_bytes = cbor2.dumps(link.link_key, canonical=True)
            # If link_key already exists, invalidate old first
            if link_key_bytes in result:
                await self._db.invalidate_link(conn, result[link_key_bytes].link_id, tick, seq)

            link_id = await self._db.write_link(
                conn,
                valid_from=tick,
                valid_from_seq=seq,
                from_thing_key=from_key,
                link_type=link.link_type,
                link_key=link.link_key,
                to_thing_key=link.to_thing_key,
            )
            result[link_key_bytes] = CurrentLinkInfo(
                link_id=link_id,
                link_type=link.link_type,
                to_thing_key_bytes=cbor2.dumps(link.to_thing_key, canonical=True),
            )

        return result

    async def _invalidate_thing(
        self, conn: AsyncConnection, tick: int, seq: int, key_bytes: bytes
    ) -> None:
        """Invalidate a thing that disappeared from tracking.

        Note: Does NOT modify _current_things - caller must delete key_bytes after commit.
        """
        info = self._current_things.get(key_bytes)
        if info is None:
            return  # Already gone, nothing to do

        # Invalidate thing state
        await self._db.invalidate_thing_state(conn, info.thingstate_id, tick, seq)

        # Invalidate all its links
        for link_info in info.links.values():
            await self._db.invalidate_link(conn, link_info.link_id, tick, seq)

    # =========================================================================
    # Main Entry Point
    # =========================================================================

    @contextmanager
    def watching(self) -> Iterator[None]:
        """Context manager that sets this watcher as the active global watcher.

        This is a lightweight context manager that only sets up the watcher context,
        without initializing the database or starting the HTTP server. May be useful for ensuring a
        running watcher can observe changes in a given async task even if it is not the task where
        the watcher's serving loop is running.

        Example:
            with watcher.watching():
                tw.track(obj)
                tw.tick("info", "something happened")
        """
        token = set_current_watcher(self)
        try:
            yield
        finally:
            reset_current_watcher(token)

    @asynccontextmanager
    async def watching_and_serving(self) -> AsyncIterator[None]:
        """Context manager that sets up the watcher and serves the UI.

        This sets up the watcher as the active global watcher, initializes the database,
        starts the background writer task, and optionally starts the HTTP server (if port is set).
        The watcher remains active until the context exits.

        Example:
            async with watcher.watching_and_serving():
                # Watcher is active, HTTP server is running (if port was set)
                await some_work()
        """
        from contextlib import AsyncExitStack

        token = set_current_watcher(self)
        async with AsyncExitStack() as exit_stack:
            try:
                # Initialize database
                self._db = Database(self.db_path)
                await self._db.connect()

                # Initialize fxtr namespace if fxtr_support is enabled
                if self._fxtr_support:
                    from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace

                    from .fxtr.immutable_store import FxtrImmutableStore

                    # Compute fxtr DB path (parallel to watcher's DB)
                    if self.db_path is not None:
                        fxtr_db_url = f"sqlite+aiosqlite:///{self.db_path}-fxtr.db"
                    else:
                        # In-memory with shared cache (matches thingwatcher's approach)
                        fxtr_db_url = f"sqlite+aiosqlite:///file:memdb_fxtr_{uuid.uuid4().hex}?mode=memory&cache=shared&uri=true"

                    # Create namespace with isolated scope (doesn't inherit from active scope)
                    self._fxtr_namespace = SQLiteFxtrNamespace(
                        fxtr_db_url,
                        "_thingwatcher_internal",
                        isolate_scope=True,
                    )
                    await exit_stack.enter_async_context(self._fxtr_namespace)

                    # Register as immutable store for entity lookups
                    self.register_immutable_store(
                        "_fxtr_internal",
                        FxtrImmutableStore(self._fxtr_namespace._get_backend()),
                    )

                    # Create copy queue
                    fxtr_send, fxtr_recv = anyio.create_memory_object_stream[
                        tuple["FxtrID", "FxtrScope"]
                    ](max_buffer_size=math.inf)
                    self._fxtr_copy_send = fxtr_send
                    self._fxtr_copy_recv = fxtr_recv

                # Start writer task using the pre-created receive stream
                # (write stream was created in __init__ so ticks can be queued early)
                async with anyio.create_task_group() as tg:
                    tg.start_soon(self._db_writer, self._receive_stream)
                    tg.start_soon(self._async_update_runner)

                    # Start fxtr copy worker if enabled
                    if self._fxtr_support:
                        tg.start_soon(self._fxtr_copy_worker)

                    # Optionally start HTTP server with hypercorn
                    graceful_shutdown_event = Event()
                    if self.port is not None:
                        import hypercorn.asyncio
                        import hypercorn.config

                        from .server import create_app

                        # Event to signal when server is ready
                        server_started_event = Event()

                        app = create_app(self, on_startup=server_started_event, dev_mode=True)

                        config = hypercorn.config.Config()
                        config.bind = [f"127.0.0.1:{self.port}"]
                        config.loglevel = "WARNING"

                        async def _run_server() -> None:
                            await hypercorn.asyncio.serve(
                                app,  # type: ignore
                                config,
                                shutdown_trigger=graceful_shutdown_event.wait,
                            )

                        tg.start_soon(_run_server)
                        # Wait for server to be ready before proceeding
                        await server_started_event.wait()

                    try:
                        yield
                    finally:
                        # Close all subscription streams first - this causes subscription
                        # generators to exit and WebSocket connections to close
                        await self._close_all_tick_streams()

                        # Now signal hypercorn to stop (connections should already be closing)
                        graceful_shutdown_event.set()

                        # Cancel all pending async updates and close the async update stream
                        # This must happen before closing the write stream since async updates
                        # send to the write stream when they complete
                        self._cancel_all_async_updates()
                        await self._async_update_send.aclose()

                        # Close fxtr copy stream if enabled (stops accepting new jobs)
                        if self._fxtr_support and self._fxtr_copy_send is not None:
                            await self._fxtr_copy_send.aclose()

                        # Close the write stream
                        print("Writer shutting down")
                        await self._write_stream.aclose()
                        print("Writer queue closed")

                    # Writer and server will exit
                print("Out of watcher task group")

            finally:
                # Clean up fxtr state (namespace cleanup handled by AsyncExitStack)
                if self._fxtr_support and self._fxtr_namespace is not None:
                    self.unregister_immutable_store("_fxtr_internal")
                    self._fxtr_namespace = None
                    self._fxtr_copy_send = None
                    self._fxtr_copy_recv = None
                    self._fxtr_tracked_ids.clear()

                if self._db:
                    await self._db.close()
                self._current_things.clear()
                self._tick_streams.clear()
                reset_current_watcher(token)

    async def watch_and_serve_until(self, done: Callable[[], Awaitable[Any]]) -> Any:
        """Run the watcher and serve the UI until the done coroutine completes.

        This sets up the watcher as the active global watcher, initializes the database,
        starts the background writer task, optionally starts the HTTP server (if port is set),
        and runs until the provided coroutine completes.

        Args:
            done: A coroutine factory that returns when watching should stop.

        Returns:
            The result of the done coroutine.
        """
        async with self.watching_and_serving():
            return await done()
