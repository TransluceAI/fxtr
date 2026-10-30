"""Output channels — a standard interface for streaming state out of a
computation across RPC, organized as a *tree* of channels.

A **channel** consists of:

- A **tag** — an :class:`~fxtr2.data.entity_ref_scope.EntityRef` whose
  *type* identifies the kind of stream (type parameter ``T``). Fetched once
  via the reader's ``get_tag``; it is not carried in the event stream.
- A **status** (type parameter ``S``) — a small, replace-only value describing
  the channel's current state. Subscribable cheaply on its own
  (:meth:`FxtrChannelReader.subscribe_state`).
- A sequence of **messages** (type parameter ``V``) emitted while the channel
  is open.
- A **final value** (type parameter ``R``) that *replaces* the message log
  when the channel finishes.

Channels form a tree: a **writer** can ``spawn_subchannel`` to mint a child
(identified by a fresh string **key**), and a **reader** can
``open_subchannel_reader(key)`` to reach a direct child. Channel payloads carry
**only data** (entity refs + scalars), never service refs — to expose a
sub-computation, spawn a subchannel and emit its *key*; a consumer recognizes
the key by application convention and opens the child reader. A child is
reachable *iff its parent is open*; once a parent finishes/aborts, its children
are gone, and the parent's final value is the complete record of them.

The two ends are distinct services:

- :class:`FxtrChannelWriter` — handed to **producers**: ``start`` / ``emit`` /
  ``finish`` / ``spawn_subchannel`` / ``abort``.
- :class:`FxtrChannelReader` — handed to **consumers**: ``get_tag`` /
  ``subscribe_full`` / ``subscribe_state`` / ``peek_full`` / ``peek_state`` /
  ``open_subchannel_reader`` / ``subscribe_debug_subchannels``.

Subscribers joining an open channel's **full** stream receive the message log
as a snapshot, then live batched deltas (each carrying the current status); a
subscriber joining a finished channel gets only the final value (or the abort
reason). The **state** stream is the cheap replace-only ``ChannelState`` view.

The pieces:

- :class:`ChannelTag` — abstract base for tag entity types; pins ``S`` / ``V``
  / ``R`` so they can be inferred from the tag.
- The event / snapshot dataclasses + :data:`ChannelUpdate` (full stream) and
  :class:`ChannelState` (state stream) — what subscribers receive (wire form =
  the mashumaro encoding of these dataclasses).
- :class:`FxtrChannelWriter` / :class:`FxtrChannelReader` — the two service
  protocols.

All channel writers share one fixed service type id
(:data:`CHANNEL_WRITER_TYPE_ID`), and all readers another
(:data:`CHANNEL_READER_TYPE_ID`) — the per-stream type identity lives in the
tag entity's type, not the service type.
"""

from __future__ import annotations

import contextlib
import functools
import math
import types
import typing
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import anyio
import anyio.abc

from fxtr2.conversion.base_registry import substitute_typevars
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import (
    GLOBAL_SCOPE,
    Entity,
    EntityRef,
    FxtrScope,
    ServiceRef,
    get_active_scope,
)
from fxtr2.services.service_cap_common import USE_REMOTE_DEFAULT, FxtrServiceData, _UseRemoteDefault
from fxtr2.services.service_dialect import OverrideThisTypeBeforeUsing, decode_from_service_data
from fxtr2.services.subscription_types import Subscriber, UnsubscribeHandle
from fxtr2.services.subscription_util import SubscriberHub
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method

__all__ = [
    "CHANNEL_READER_TYPE_ID",
    "CHANNEL_WRITER_TYPE_ID",
    "FXTR_STREAMS_NS",
    "AbortedChannelSnapshot",
    "ChannelAbortedBeforeStart",
    "ChannelAbortedEvent",
    "ChannelCompleteEvent",
    "ChannelState",
    "ChannelStateEvent",
    "ChannelTag",
    "ChannelUpdate",
    "ChannelUpdateEvent",
    "CompleteChannelSnapshot",
    "FxtrChannelData",
    "FxtrChannelReader",
    "FxtrChannelWriter",
    "OpenChannelSnapshot",
    "SubchannelOpened",
    "create_root_channel",
    "decode_channel_update",
    "infer_channel_reader_type",
    "infer_channel_types",
    "iter_channel",
    "start_channel_and_infer_types",
]

#: Stable namespace UUID for types defined by this module. Don't change.
FXTR_STREAMS_NS = uuid.UUID("3b2aabce-e5e5-49e4-baa1-29ee8f69344c")

#: Fixed service type id shared by *every* channel writer, regardless of
#: ``T``/``S``/``V``/``R``.
CHANNEL_WRITER_TYPE_ID = system_entities.define_type(FXTR_STREAMS_NS, "channel_writer")

#: Fixed service type id shared by *every* channel reader — what lets a frontend
#: recognize "this service is a channel" at all. Renderer dispatch by stream
#: kind happens one level up, on the tag entity's type.
CHANNEL_READER_TYPE_ID = system_entities.define_type(FXTR_STREAMS_NS, "channel_reader")

#: Runtime type identity of the internal subscriber :func:`iter_channel` mounts.
_ITER_CHANNEL_SUBSCRIBER_TYPE_ID = system_entities.define_type(
    FXTR_STREAMS_NS, "iter_channel_subscriber"
)


# A type alias used as the placeholder for the un-inferred type parameters of a
# spawned/opened subchannel ref (replace via the inference helpers before use).
_U = OverrideThisTypeBeforeUsing


type FxtrChannelData = (
    None
    | bool
    | int
    | float
    | str
    | bytes
    | FxtrID
    | EntityRef[Any]
    | Mapping[str, "FxtrChannelData"]
    | Sequence["FxtrChannelData"]
)
"""Documentation alias: what a channel's ``S`` / ``V`` / ``R`` values must
*serialize to*.

Not enforced at runtime — the system converts values via mashumaro against the
caller-provided (or :class:`ChannelTag`-inferred) annotations, but conceptually
an annotation is only *valid* for a channel's status / message / result type if
its values encode into this vocabulary.

Unlike a general service payload, this vocabulary has **no ``ServiceRef`` arm**:
channel payloads are pure data. A sub-computation is referenced by its
subchannel *key* (a ``str``), not by embedding a live service ref — see
:meth:`FxtrChannelWriter.spawn_subchannel`.
"""


class ChannelTag[S, V, R](Entity):
    """Abstract base for channel-tag entity types.

    Subclassing pins the status type ``S``, message type ``V``, and final-value
    type ``R`` for any channel started with a tag of this type, so the channel
    types can be inferred (statically and at runtime). Concrete tags subclass
    both ``DataclassEntity`` and ``ChannelTag``::

        @dataclasses.dataclass(frozen=True)
        class MyTag(DataclassEntity, ChannelTag[MyStatus, MyMsg, MyResult],
                    fxtr_type=define_type(MY_NS, "my_tag")):
            label: str

    Adds no fields and no ``_fxtr_store_impl`` — it stays abstract; the
    serialization machinery comes entirely from the other base.

    A tag class that leaves ``S`` / ``V`` / ``R`` generic (unbound at the use
    site) cannot be inferred from; channels using such a tag must pass explicit
    types.
    """


@functools.cache
def _channel_tag_specialization(tag_type: type) -> tuple[Any, Any, Any] | None:
    """Find the ``ChannelTag[S, V, R]`` specialization in ``tag_type``'s bases.

    Walks ``types.get_original_bases`` transitively (composing per-hop TypeVar
    substitutions, keyed on TypeVar objects). Memoized per tag class.

    Returns:
        The ``(S, V, R)`` annotations from the ``ChannelTag[...]``
        specialization (possibly still containing TypeVars, for generic tag
        classes), or ``None`` if ``tag_type`` does not derive from
        ``ChannelTag`` or derives from it unparameterized.
    """
    if not isinstance(tag_type, type) or not issubclass(tag_type, ChannelTag):
        return None
    for base in types.get_original_bases(tag_type):
        origin = typing.get_origin(base) or base
        if not isinstance(origin, type) or not issubclass(origin, ChannelTag):
            continue
        if origin is ChannelTag:
            args = typing.get_args(base)
            return (args[0], args[1], args[2]) if args else None
        inner = _channel_tag_specialization(origin)
        if inner is None:
            return None
        params = tuple(getattr(origin, "__parameters__", ()) or ())
        args = typing.get_args(base)
        subst: dict[Any, Any] = dict(zip(params, args)) if args else {}
        return (
            substitute_typevars(inner[0], subst),
            substitute_typevars(inner[1], subst),
            substitute_typevars(inner[2], subst),
        )
    return None


def _iter_annotation_leaves(annotation: Any) -> typing.Iterator[Any]:
    """Yield ``annotation`` and every nested type argument, recursively."""
    yield annotation
    for arg in typing.get_args(annotation):
        yield from _iter_annotation_leaves(arg)


def infer_channel_types(tag_type: Any) -> tuple[Any, Any, Any]:
    """Infer a tag type's ``(status_type, message_type, result_type)``.

    Args:
        tag_type: A concrete :class:`ChannelTag` subclass (typically an
            ``EntityRef``'s ``target_type``, hence typed ``Any``).

    Returns:
        The ``(S, V, R)`` annotation triple pinned by the tag class's
        ``ChannelTag[S, V, R]`` base.

    Raises:
        TypeError: ``tag_type`` is not a ``ChannelTag`` subclass, derives from
            ``ChannelTag`` unparameterized, or its specialization still contains
            type variables (a generic tag class used without binding) — in all
            three cases the caller must pass explicit types.
    """
    spec = _channel_tag_specialization(tag_type)
    if spec is None:
        raise TypeError(
            f"Cannot infer status/message/result types from tag type {tag_type!r}: "
            "it is not a parameterized ChannelTag subclass. Pass explicit types."
        )
    has_typevars = any(
        isinstance(leaf, typing.TypeVar) for ann in spec for leaf in _iter_annotation_leaves(ann)
    )
    if has_typevars:
        raise TypeError(
            f"Cannot infer status/message/result types from tag type {tag_type!r}: its "
            f"ChannelTag specialization {spec!r} still contains type variables. "
            "Pass explicit types."
        )
    return spec


# =============================================================================
# State + event / snapshot dataclasses
# =============================================================================


@dataclass(frozen=True)
class ChannelState[S]:
    """The cheap replace-only view of a started channel: its current status plus
    lifecycle ``kind``. Delivered by ``subscribe_state`` / ``peek_state`` (which
    deliver :class:`ChannelAbortedBeforeStart` instead if the channel aborted
    before ever starting — see :data:`ChannelStateEvent`).

    ``kind`` doubles as the discriminator for the :data:`ChannelStateEvent` union."""

    status: S
    kind: Literal["open", "complete", "abort"]


@dataclass(frozen=True)
class OpenChannelSnapshot[S, V]:
    """First full-stream event delivered to a new subscriber of an open channel.

    Carries the current status plus every message emitted so far (or the
    condensed history installed by a ``replace_history`` emit); live
    :class:`ChannelUpdateEvent` deltas follow. The tag is *not* included —
    fetch it once via ``get_tag``.
    """

    status: S
    values: tuple[V, ...]
    kind: Literal["snapshot_open"] = "snapshot_open"


@dataclass(frozen=True)
class CompleteChannelSnapshot[S, R]:
    """First and only full-stream event delivered to a new subscriber of a
    complete channel."""

    status: S
    result: R
    kind: Literal["snapshot_complete"] = "snapshot_complete"


@dataclass(frozen=True)
class AbortedChannelSnapshot[S]:
    """First and only full-stream event delivered to a new subscriber of an
    aborted channel."""

    status: S
    reason: str
    kind: Literal["snapshot_abort"] = "snapshot_abort"


@dataclass(frozen=True)
class ChannelUpdateEvent[S, V]:
    """Live full-stream delta: a batch of newly emitted messages.

    One ``emit`` call surfaces as one of these, carrying the whole batch, the
    current status, and the ``replace_history`` flag (when ``True``, subscribers
    should drop their accumulated log and treat ``events`` as the new history).
    """

    events: tuple[V, ...]
    status: S
    replace_history: bool
    kind: Literal["update"] = "update"


@dataclass(frozen=True)
class ChannelCompleteEvent[S, R]:
    """Live full-stream delta: the channel finished with a final value.

    On receipt, subscribers should drop any references retained from earlier
    messages and use ``result`` instead.
    """

    result: R
    status: S
    kind: Literal["complete"] = "complete"


@dataclass(frozen=True)
class ChannelAbortedEvent[S]:
    """Live full-stream delta: a *started* channel aborted without a final value.

    Carries the last-known status (``start`` guaranteed one). The before-start
    case is :class:`ChannelAbortedBeforeStart` instead."""

    status: S
    reason: str
    kind: Literal["abort"] = "abort"


@dataclass(frozen=True)
class ChannelAbortedBeforeStart:
    """A channel aborted before it was ever ``start``ed — so it has no tag and no
    status, only the abort reason. Delivered on **both** the full and state streams
    (live and as the snapshot for a late subscriber) so consumers always receive a
    concrete event rather than a block/error. (``get_tag`` still raises for this
    case — there is no tag to return.)"""

    reason: str
    kind: Literal["aborted_before_start"] = "aborted_before_start"


type ChannelUpdate[S, V, R] = (
    OpenChannelSnapshot[S, V]
    | CompleteChannelSnapshot[S, R]
    | AbortedChannelSnapshot[S]
    | ChannelUpdateEvent[S, V]
    | ChannelCompleteEvent[S, R]
    | ChannelAbortedEvent[S]
    | ChannelAbortedBeforeStart
)
"""Everything a channel **full**-stream subscriber can receive, discriminated by
the ``kind`` literal.

Wire form: the mashumaro encoding of these dataclasses under the service dialect
(e.g. ``{"kind": "update", "events": [...], "status": ..., "replace_history":
false}``). No event carries the tag (fetch via ``get_tag``). The dataclasses are
transient event payloads, not stored entities.
"""


type ChannelStateEvent[S] = ChannelState[S] | ChannelAbortedBeforeStart
"""What the **state** stream (``subscribe_state`` / ``peek_state``) delivers: the
normal :class:`ChannelState` for a started channel, or
:class:`ChannelAbortedBeforeStart` if it aborted before starting. Discriminated by
the ``kind`` literal (``ChannelState``'s own ``kind`` field plus
``aborted_before_start``)."""


@dataclass(frozen=True)
class SubchannelOpened:
    """One element of a channel's **debug subchannel feed**
    (:meth:`FxtrChannelReader.subscribe_debug_subchannels`): a subchannel that has
    been spawned on this channel, identified by its ``key`` (plus the optional debug
    ``name`` passed to ``spawn_subchannel``).

    Data-only (no service ref): to actually reach the subchannel, call
    ``open_subchannel_reader(key)``. This feed is a schema-blind way to enumerate a
    channel's subchannels (the primary path is application convention — emit the key
    into the message stream); it exists so a generic consumer can surface subchannels
    without knowing the message shapes."""

    key: str
    name: str | None
    kind: Literal["subchannel_opened"] = "subchannel_opened"


# =============================================================================
# Service protocols
# =============================================================================
#
# `spawn_subchannel` / `open_subchannel_reader` return annotations reference the
# protocol class itself; `@service_protocol` resolves method hints eagerly (via
# `typing.get_type_hints`) at decoration time, and `_resolve_method_hints`
# injects the owning class by name into the resolution namespace so those
# self-references resolve.


@service_protocol
class FxtrChannelWriter[T: Entity, S, V, R](Protocol):
    """Writer side of a channel — handed to the producer that owns it.

    The producer holding the writer owns the channel: it ``start``s it once,
    ``emit``s messages / status, and ideally ``finish``es it. Dropping the
    writer (its mount scope draining) aborts the channel if still open. A
    coordinator that spawned the subchannel may ``abort`` it (idempotent) as a
    backstop.
    """

    async def start(self, tag: EntityRef[T], initial_status: S) -> None:
        """Start the channel with a tag and an initial status (once only).

        It is an error to call this more than once, or to ``emit`` / ``finish``
        before calling it.
        """
        ...

    async def emit(
        self,
        *,
        events: list[V] | None = None,
        status: S | _UseRemoteDefault = USE_REMOTE_DEFAULT,
        replace_history: bool = False,
    ) -> None:
        """Update the channel.

        Args:
            events: New messages to append (a batch).
            status: New status. If omitted, the current status is kept.
            replace_history: If ``True``, drop the existing message log and
                replace it with ``events`` (subscribers are told to reset).
        """
        ...

    async def finish(
        self, final_value: R, final_status: S | _UseRemoteDefault = USE_REMOTE_DEFAULT
    ) -> None:
        """Close the channel with a final value representing the whole stream.

        Once finished, no more messages can be emitted, the message log is
        dropped, and new subscribers see only ``final_value``. The final value
        must be durable, self-contained data (no service refs). Calling
        ``finish`` more than once is an error.
        """
        ...

    async def spawn_subchannel(
        self, name: str | None = None
    ) -> tuple[str, ServiceRef[FxtrChannelWriter[_U, _U, _U, _U]]]:
        """Spawn a subchannel of this channel.

        The subchannel is reachable (via the parent reader's
        ``open_subchannel_reader``) only while this parent channel is open; it
        aborts if the parent finishes without it having finished.

        Args:
            name: An optional debug label (a prefix of the returned key).

        Returns:
            ``(key, writer)`` — ``key`` is a fresh unique string identifying the
            subchannel (emit it into the parent's own message stream so
            subscribers can find it); ``writer`` is the un-started subchannel
            writer (un-inferred types; retype via ``start_channel_and_infer_types``).
        """
        ...

    async def abort(self, reason: str) -> None:
        """Abort this channel due to an unrecoverable error or usage violation.

        Idempotent: aborting an already-finished/aborted channel does nothing.
        The "main" user of a channel should prefer ``finish`` with a typed
        final value; ``abort`` is for teardown by a non-owner or when emitting a
        finish itself fails. Aborting runs the teardown walk over open children.
        """
        ...


@service_protocol
class FxtrChannelReader[T: Entity, S, V, R](Protocol):
    """Reader side of a channel — handed to consumers.

    Every channel reader shares the fixed service type id
    :data:`CHANNEL_READER_TYPE_ID`; the per-stream identity lives in the tag
    entity's type (fetch via :meth:`get_tag`).
    """

    async def get_tag(self) -> EntityRef[T]:
        """Return the channel's tag. Blocks until the channel has started."""
        ...

    async def subscribe_full(
        self, subscriber: ServiceRef[Subscriber[ChannelUpdate[S, V, R]]]
    ) -> ServiceRef[UnsubscribeHandle]:
        """Subscribe to the full event stream (snapshot then live deltas).

        Replay-then-live: a subscriber joining an open channel first receives an
        :class:`OpenChannelSnapshot`, then live :class:`ChannelUpdateEvent`
        deltas until the channel finishes (:class:`ChannelCompleteEvent`) or
        aborts (:class:`ChannelAbortedEvent`); one joining a finished/aborted
        channel receives only the corresponding terminal snapshot.
        """
        ...

    async def subscribe_state(
        self, subscriber: ServiceRef[Subscriber[ChannelStateEvent[S]]]
    ) -> ServiceRef[UnsubscribeHandle]:
        """Subscribe to the cheap replace-only status/stage stream (or a
        :class:`ChannelAbortedBeforeStart` if the channel aborted before starting)."""
        ...

    async def peek_full(self) -> ChannelUpdate[S, V, R]:
        """Return the current full-stream snapshot. Blocks until the channel has
        started (or aborted before starting)."""
        ...

    async def peek_state(self) -> ChannelStateEvent[S]:
        """Return the current status + stage (or :class:`ChannelAbortedBeforeStart`
        if it aborted before starting). Blocks until started-or-aborted."""
        ...

    async def open_subchannel_reader(
        self, key: str
    ) -> ServiceRef[FxtrChannelReader[_U, _U, _U, _U]]:
        """Retrieve a reader for a subchannel by key.

        Args:
            key: The key returned by ``spawn_subchannel`` on the writer end.
                Must be a direct subchannel of *this* channel.

        Returns:
            A reader for the subchannel (un-inferred types; retype via
            ``infer_channel_reader_type``).

        Raises:
            Errors if this channel has already finished/aborted (its children
            are gone) or the key is unknown.
        """
        ...

    async def subscribe_debug_subchannels(
        self, subscriber: ServiceRef[Subscriber[SubchannelOpened]]
    ) -> ServiceRef[UnsubscribeHandle]:
        """Subscribe to the **debug subchannel feed**: a :class:`SubchannelOpened`
        for each subchannel already spawned on this channel (the seed), then one per
        subchannel spawned afterwards.

        A schema-blind way to enumerate this channel's subchannels — orthogonal to
        the primary API (apps emit subchannel keys into the message stream by their
        own convention and call :meth:`open_subchannel_reader`). The feed is
        append-only over the channel's open life (a subchannel that finishes on its
        own is not removed); it is independent of ``start`` (it concerns children,
        not the tag, so it never blocks). The feed is closed once this channel
        finishes/aborts (its children are then gone), so subscribing to a terminal
        channel's feed errors rather than serving stale keys."""
        ...


# =============================================================================
# Engine: tree, node, thin impls
# =============================================================================
#
# The impls are *untyped data-shufflers*: the typed proxies on the producer
# (writer) and consumer (reader) ends do all encode/decode driven by their
# annotations (see ``start_channel_and_infer_types`` / ``infer_channel_reader_type``
# / ``create_root_channel``), so the impls receive and forward pre-encoded
# ``FxtrServiceData`` — they hold no per-channel codec and never call
# ``infer_channel_types`` at runtime. Per-channel state lives on the tree
# (``_ChannelNode``); the impls are thin routers bound to ``(tree, node_id)``,
# mounted on the per-call scope and returned to the caller.

#: The untyped specialization the impls declare so the dialect identity-passes
#: their method args/returns (the typed proxies on each end do real conversion).
_UNTYPED_WRITER = FxtrChannelWriter[Entity, FxtrServiceData, FxtrServiceData, FxtrServiceData]
_UNTYPED_READER = FxtrChannelReader[Entity, FxtrServiceData, FxtrServiceData, FxtrServiceData]


def _iter_data_leaves(data: Any) -> Iterator[FxtrID | EntityRef[Any]]:
    """Yield every ``FxtrID`` / ``EntityRef`` leaf in encoded channel data.

    Raises ``ValueError`` on a ``ServiceRef`` leaf — channel payloads are
    data-only (a sub-computation is referenced by its subchannel key, not by
    embedding a service ref)."""
    if isinstance(data, (FxtrID, EntityRef)):
        yield data
    elif isinstance(data, ServiceRef):
        raise ValueError(
            "channel payloads (status / message / final value) may not contain "
            f"service refs (got {data!r}); reference a sub-computation by its "
            "subchannel key instead"
        )
    elif isinstance(data, Mapping):
        for value in data.values():
            yield from _iter_data_leaves(value)
    elif isinstance(data, (str, bytes)):
        return
    elif isinstance(data, Sequence):
        for value in data:
            yield from _iter_data_leaves(value)


async def _copy_closure(
    dest: FxtrScope, encoded_items: Sequence[FxtrServiceData], source: FxtrScope
) -> None:
    """Copy the entity closure of every leaf in ``encoded_items`` into ``dest``
    from ``source`` (also validates data-only via :func:`_iter_data_leaves`)."""
    for item in encoded_items:
        for leaf in _iter_data_leaves(item):
            entity_id = leaf.entity_id if isinstance(leaf, EntityRef) else leaf
            await dest.copy_from_scope(entity_id, source)


def _has_entity_leaves(data: FxtrServiceData) -> bool:
    for _ in _iter_data_leaves(data):
        return True
    return False


async def _scope_holder(
    release: anyio.Event,
    name: str,
    *,
    task_status: anyio.abc.TaskStatus[FxtrScope] = anyio.TASK_STATUS_IGNORED,
) -> None:
    """Hold one isolated per-node residency scope open until ``release`` fires."""
    async with FxtrScope(name=name, include=[GLOBAL_SCOPE], isolated=True) as scope:
        task_status.started(scope)
        await release.wait()


class _ChannelNode:
    """Per-channel durable state (the only stateful thing — impls are thin).

    Two residency scopes: ``durable_scope`` holds the tag (at ``start``) + the
    final value & terminal status (at ``finish``/``abort``) and lives until the
    node's *parent* is reclaimed; ``data_scope`` holds the current message log +
    live status and is swapped on ``replace_history`` / released on terminal.
    """

    def __init__(
        self,
        node_id: int,
        parent_id: int | None,
        key: str | None,
        full_hub: SubscriberHub[FxtrServiceData],
        state_hub: SubscriberHub[FxtrServiceData],
        subchannels_hub: SubscriberHub[FxtrServiceData],
        durable_scope: FxtrScope,
        durable_release: anyio.Event,
        data_scope: FxtrScope,
        data_release: anyio.Event,
    ) -> None:
        self.id = node_id
        self.parent_id = parent_id
        self.key = key
        self.children: dict[str, int] = {}
        # Wire envelopes (``SubchannelOpened``) for every subchannel spawned on this
        # node, in spawn order — the seed for the debug subchannel feed.
        self.subchannel_log: list[FxtrServiceData] = []
        self.ready = anyio.Event()  # set on start / finish / abort
        self.stage: Literal["unstarted", "open", "complete", "abort"] = "unstarted"
        # The tag is held as a bare FxtrID (its wire form); the typed reader
        # proxy reattaches EntityRef[T] on get_tag.
        self.tag_id: FxtrID | None = None
        self.status_encoded: FxtrServiceData = None
        self.log: list[FxtrServiceData] = []
        self.final_encoded: FxtrServiceData = None
        self.abort_reason: str | None = None
        self.full_hub = full_hub
        self.state_hub = state_hub
        self.subchannels_hub = subchannels_hub
        self.durable_scope = durable_scope
        self.durable_release = durable_release
        self.data_scope = data_scope
        self.data_release = data_release
        self.lock = anyio.Lock()


class _ChannelTree:
    """In-process owner of a channel tree: the node registry + the task group
    hosting every node's hubs and residency-scope holders. Created by
    :func:`create_root_channel` (root) via a scope lifespan."""

    def __init__(self, mount_scope: FxtrScope, task_group: anyio.abc.TaskGroup) -> None:
        self.mount_scope = mount_scope
        self.tg = task_group
        self.nodes: dict[int, _ChannelNode] = {}
        self._next_id = 0
        self.root_id: int | None = None
        self._closing = False  # set in the tree-close teardown; gates writer-release aborts

    async def make_node(
        self, *, parent_id: int | None, key: str | None, debug_name: str
    ) -> _ChannelNode:
        """Create + register a fresh (unstarted) node, with its two residency
        scopes and two hubs running on the tree task group.

        Args:
            parent_id: The parent node's id, or ``None`` for the root.
            key: The subchannel key this node is registered under in its parent
                (``None`` for the root).
            debug_name: A label used only in the residency scopes' debug names.

        Returns:
            The new ``_ChannelNode`` (already in ``self.nodes``; the caller links
            it into its parent's ``children`` map).
        """
        node_id = self._next_id
        self._next_id += 1
        durable_release = anyio.Event()
        durable_scope: FxtrScope = await self.tg.start(
            _scope_holder, durable_release, f"channel[{debug_name}].durable"
        )
        data_release = anyio.Event()
        data_scope: FxtrScope = await self.tg.start(
            _scope_holder, data_release, f"channel[{debug_name}].data"
        )
        full_hub = SubscriberHub[FxtrServiceData](task_group=self.tg, mode="queue")
        state_hub = SubscriberHub[FxtrServiceData](task_group=self.tg, mode="coalesce")
        # Debug subchannel feed: queue mode (append-only deltas), seeded per
        # subscriber from the node's ``subchannel_log``.
        subchannels_hub = SubscriberHub[FxtrServiceData](task_group=self.tg, mode="queue")
        node = _ChannelNode(
            node_id,
            parent_id,
            key,
            full_hub,
            state_hub,
            subchannels_hub,
            durable_scope,
            durable_release,
            data_scope,
            data_release,
        )
        self.nodes[node_id] = node
        return node

    # -- envelopes (must equal the mashumaro encoding of the dataclasses) --

    @staticmethod
    def _open_snapshot(node: _ChannelNode) -> FxtrServiceData:
        """Build the ``snapshot_open`` wire envelope (current status + log) — the
        seed delivered to a full-stream subscriber joining an open channel. Byte-
        equal to the mashumaro encoding of :class:`OpenChannelSnapshot`."""
        return {"kind": "snapshot_open", "status": node.status_encoded, "values": list(node.log)}

    @staticmethod
    def _complete_snapshot(node: _ChannelNode) -> FxtrServiceData:
        """Build the ``snapshot_complete`` wire envelope (final status + result) —
        the seed for a full-stream subscriber joining a finished channel. Byte-
        equal to the mashumaro encoding of :class:`CompleteChannelSnapshot`."""
        return {
            "kind": "snapshot_complete",
            "status": node.status_encoded,
            "result": node.final_encoded,
        }

    @staticmethod
    def _abort_snapshot(node: _ChannelNode) -> FxtrServiceData:
        """Build the ``snapshot_abort`` wire envelope (final status + reason) — the
        seed for a full-stream subscriber joining an aborted channel. Byte-equal to
        the mashumaro encoding of :class:`AbortedChannelSnapshot`."""
        return {
            "kind": "snapshot_abort",
            "status": node.status_encoded,
            "reason": node.abort_reason or "",
        }

    @staticmethod
    def _state(node: _ChannelNode) -> FxtrServiceData:
        """Build the :class:`ChannelState` wire envelope (current status + lifecycle
        ``kind``) for the state stream. Only used for *started* channels (``kind`` is
        one of open/complete/abort); a never-started abort uses
        :meth:`_aborted_before_start` instead."""
        return {"status": node.status_encoded, "kind": node.stage}

    @staticmethod
    def _aborted_before_start(node: _ChannelNode) -> FxtrServiceData:
        """Build the :class:`ChannelAbortedBeforeStart` wire envelope — delivered on
        both streams when a channel aborted before it ever started (no tag/status)."""
        return {"kind": "aborted_before_start", "reason": node.abort_reason or ""}

    @staticmethod
    def _never_started(node: _ChannelNode) -> bool:
        """True if ``node`` reached a terminal stage without ever starting (its tag
        was never set) — i.e. an abort-before-start."""
        return node.tag_id is None

    # -- writer-side ops --

    async def start_node(
        self,
        node: _ChannelNode,
        tag_id: FxtrID,
        status_encoded: FxtrServiceData,
        from_scope: FxtrScope,
    ) -> None:
        """Start ``node`` (open it). Copies the tag into ``durable_scope`` and the
        initial status into ``data_scope`` (from ``from_scope``), flips the stage to
        ``open``, sets ``ready``, and publishes the initial open snapshot + state so
        subscribers that joined pre-start (and got no seed) converge.

        Args:
            node: The node to start (must be ``unstarted``).
            tag_id: The channel's tag id (held + re-served by ``get_tag``).
            status_encoded: The pre-encoded initial status.
            from_scope: The scope the tag + status closures are copied from (the
                writer dispatch's input scope).

        Raises:
            RuntimeError: ``node`` has already been started.
        """
        async with node.lock:
            if node.stage != "unstarted":
                raise RuntimeError("channel already started")
            await node.durable_scope.copy_from_scope(tag_id, from_scope)
            await _copy_closure(node.data_scope, [status_encoded], from_scope)
            node.tag_id = tag_id
            node.status_encoded = status_encoded
            node.stage = "open"
            node.ready.set()
            # Reach already-waiting subscribers (subscribed pre-start, no seed).
            origin = node.data_scope
            node.full_hub.publish(self._open_snapshot(node), origin_scope=origin)
            node.state_hub.publish(self._state(node), origin_scope=origin)

    async def emit_node(
        self,
        node: _ChannelNode,
        encoded_events: list[FxtrServiceData],
        status_encoded: FxtrServiceData,
        status_changed: bool,
        replace_history: bool,
        from_scope: FxtrScope,
    ) -> None:
        """Emit a batch (and optionally a new status) to an open ``node``.

        Retains the batch's (and changed status's) closures in ``data_scope`` and
        publishes one ``update`` envelope to the full hub + the new state to the
        state hub. With ``replace_history`` the message log is replaced: a fresh
        ``data_scope`` is opened, the new batch + current status copied in, then
        swapped and the old scope released (so condensed-away residency is freed).

        Args:
            node: The (open) node to emit on.
            encoded_events: The pre-encoded message batch.
            status_encoded: The pre-encoded new status, or ``None`` if unchanged.
            status_changed: Whether ``status_encoded`` is a real new status (vs the
                caller omitting it — then the current status is kept and re-sent).
            replace_history: Replace the log with this batch instead of appending.
            from_scope: The scope message/status closures are copied from (the
                writer dispatch's input scope).

        Raises:
            RuntimeError: ``node`` is not started, or already finished/aborted.
        """
        async with node.lock:
            if node.stage == "unstarted":
                raise RuntimeError("cannot emit before start")
            if node.stage != "open":
                raise RuntimeError(f"cannot emit on a {node.stage} channel")
            new_status = status_encoded if status_changed else node.status_encoded
            if replace_history:
                new_release = anyio.Event()
                new_scope: FxtrScope = await self.tg.start(
                    _scope_holder, new_release, "channel.data"
                )
                try:
                    await _copy_closure(new_scope, encoded_events, from_scope)
                    await _copy_closure(
                        new_scope, [new_status], from_scope if status_changed else node.data_scope
                    )
                except BaseException:
                    new_release.set()
                    raise
                old_release = node.data_release
                node.log = list(encoded_events)
                node.data_scope = new_scope
                node.data_release = new_release
                old_release.set()
            else:
                await _copy_closure(node.data_scope, encoded_events, from_scope)
                if status_changed:
                    await _copy_closure(node.data_scope, [new_status], from_scope)
                node.log.extend(encoded_events)
            node.status_encoded = new_status
            envelope: FxtrServiceData = {
                "kind": "update",
                "events": list(encoded_events),
                "status": new_status,
                "replace_history": replace_history,
            }
            node.full_hub.publish(envelope, origin_scope=node.data_scope)
            node.state_hub.publish(self._state(node), origin_scope=node.data_scope)

    async def finish_node(
        self,
        node: _ChannelNode,
        final_encoded: FxtrServiceData,
        status_encoded: FxtrServiceData,
        status_changed: bool,
        from_scope: FxtrScope,
    ) -> None:
        """Finish an open ``node`` with a final value. Copies the final value (and
        the final status) into ``durable_scope`` (so post-finish snapshots survive
        the ``data_scope`` release), marks it complete, publishes the ``complete``
        event + state, drops the log, releases ``data_scope``, runs the teardown
        walk over its children (they become unreachable), and closes the debug
        subchannel feed (the full/state hubs stay open to serve terminal snapshots).

        Args:
            node: The (open) node to finish.
            final_encoded: The pre-encoded final value.
            status_encoded: The pre-encoded final status, or ``None`` if unchanged
                (then the current status is mirrored into ``durable_scope``).
            status_changed: Whether ``status_encoded`` is a real new status.
            from_scope: The scope the final value/status closures are copied from
                (the writer dispatch's input scope).

        Raises:
            RuntimeError: ``node`` is not started, or already finished/aborted.
        """
        async with node.lock:
            if node.stage == "unstarted":
                raise RuntimeError("cannot finish before start")
            if node.stage != "open":
                raise RuntimeError(f"cannot finish a {node.stage} channel")
            await _copy_closure(node.durable_scope, [final_encoded], from_scope)
            if status_changed:
                await _copy_closure(node.durable_scope, [status_encoded], from_scope)
                node.status_encoded = status_encoded
            else:
                await _copy_closure(node.durable_scope, [node.status_encoded], node.data_scope)
            node.final_encoded = final_encoded
            node.stage = "complete"
            node.full_hub.publish(
                {"kind": "complete", "result": final_encoded, "status": node.status_encoded},
                origin_scope=node.durable_scope,
            )
            node.state_hub.publish(self._state(node), origin_scope=node.durable_scope)
            node.log.clear()
            node.data_release.set()
            node.ready.set()
            await self._reclaim_children(node)
            # The debug subchannel feed is done once terminal: children are
            # reclaimed and no further spawns are possible, so close it (tears down
            # live debug subscribers; a late subscribe raises rather than seeing
            # historical keys for now-gone children). Idempotent — the eventual
            # _reclaim_node aclose is a no-op. The full/state hubs stay open to serve
            # terminal snapshots to late subscribers.
            await node.subchannels_hub.aclose()

    async def abort_node(self, node: _ChannelNode, reason: str) -> None:
        """Abort ``node`` if still open (no-op if already complete/aborted).

        Mirrors a ref-bearing status into ``durable_scope`` (so post-abort
        snapshots/state survive the ``data_scope`` release), marks it aborted,
        publishes the ``abort`` event + state, drops the log, releases
        ``data_scope``, runs the teardown walk over its children, and closes the
        debug subchannel feed. Used by the
        explicit ``abort`` method, the writer-release mount-lifespan teardown, and
        the parent-reclaim cascade — all of which may run unauthorized for any
        outside scope (it only touches this tree's still-open node scopes/hubs).

        Args:
            node: The node to abort.
            reason: Human-readable reason, delivered to subscribers.
        """
        async with node.lock:
            if node.stage in ("complete", "abort"):
                return
            never_started = node.stage == "unstarted"
            node.stage = "abort"
            node.abort_reason = reason
            if never_started:
                # No tag, no status — deliver the statusless before-start variant
                # on both streams (no entity payload, so origin can be None).
                env = self._aborted_before_start(node)
                node.full_hub.publish(env, origin_scope=None)
                node.state_hub.publish(env, origin_scope=None)
            else:
                status_has_refs = node.status_encoded is not None and _has_entity_leaves(
                    node.status_encoded
                )
                if status_has_refs:
                    # Move the live status closure to durable_scope so post-abort
                    # snapshots/state survive the data_scope release.
                    await _copy_closure(node.durable_scope, [node.status_encoded], node.data_scope)
                origin = node.durable_scope if status_has_refs else None
                node.full_hub.publish(
                    {"kind": "abort", "status": node.status_encoded, "reason": reason},
                    origin_scope=origin,
                )
                node.state_hub.publish(self._state(node), origin_scope=origin)
            node.log.clear()
            node.data_release.set()
            node.ready.set()
            await self._reclaim_children(node)
            # The debug subchannel feed is done once terminal: children are
            # reclaimed and no further spawns are possible, so close it (tears down
            # live debug subscribers; a late subscribe raises rather than seeing
            # historical keys for now-gone children). Idempotent — the eventual
            # _reclaim_node aclose is a no-op. The full/state hubs stay open to serve
            # terminal snapshots to late subscribers.
            await node.subchannels_hub.aclose()

    # -- subchannel ops --

    async def spawn_subchannel(
        self, parent_id: int, name: str | None, mount_into: FxtrScope
    ) -> tuple[str, ServiceRef[Any]]:
        """Mint a child node under ``parent_id`` (fresh unique key), mount a writer
        for it on ``mount_into``, and return ``(key, writer_ref)``.

        Args:
            parent_id: The parent node (must be open).
            name: Optional debug label; becomes a prefix of the generated key.
            mount_into: The scope to mount the child writer on (the spawn
                dispatch's active scope) — the returned ref's lifetime is the
                caller's hold on it.

        Returns:
            ``(key, writer_ref)`` — the key to emit into the parent stream, and the
            un-started, un-inferred (``_U``-typed) child writer ref.

        Raises:
            RuntimeError: ``parent_id`` is already finished/aborted.
        """
        parent = self.nodes[parent_id]
        async with parent.lock:
            if parent.stage in ("complete", "abort"):
                raise RuntimeError("cannot spawn a subchannel on a finished/aborted channel")
            key = f"{name}-{uuid.uuid4().hex}" if name else uuid.uuid4().hex
            child = await self.make_node(parent_id=parent_id, key=key, debug_name=name or key)
            parent.children[key] = child.id
            # Record + announce on the debug subchannel feed (data-only, no closure).
            envelope: FxtrServiceData = {"kind": "subchannel_opened", "key": key, "name": name}
            parent.subchannel_log.append(envelope)
            parent.subchannels_hub.publish(envelope, origin_scope=None)
        writer_ref = await mount_into.mount(_WriterImpl(self, child.id))
        return key, writer_ref.replace_service_type(FxtrChannelWriter[_U, _U, _U, _U])

    async def open_subchannel_reader(
        self, parent_id: int, key: str, mount_into: FxtrScope
    ) -> ServiceRef[Any]:
        """Mount + return a reader for the direct child of ``parent_id`` registered
        under ``key`` (reachable only while the parent is open).

        Args:
            parent_id: The parent node whose child is requested.
            key: The subchannel key from ``spawn_subchannel`` (per-parent namespace).
            mount_into: The scope to mount the child reader on (the open dispatch's
                active scope).

        Returns:
            The un-inferred (``_U``-typed) child reader ref.

        Raises:
            RuntimeError: ``parent_id`` has finished/aborted (children are gone).
            KeyError: No child is registered under ``key``.
        """
        parent = self.nodes[parent_id]
        async with parent.lock:
            if parent.stage in ("complete", "abort"):
                raise RuntimeError("channel has finished; its subchannels are gone")
            child_id = parent.children.get(key)
            if child_id is None:
                raise KeyError(f"unknown subchannel key {key!r}")
        reader_ref = await mount_into.mount(_ReaderImpl(self, child_id))
        return reader_ref.replace_service_type(FxtrChannelReader[_U, _U, _U, _U])

    # -- reclaim (parent-terminal / tree-close teardown walk) --

    async def _reclaim_children(self, node: _ChannelNode) -> None:
        """Reclaim (tear down) every child of ``node`` and clear its child map —
        the explicit teardown walk run when ``node`` finishes/aborts (children are
        unreachable once their parent is terminal)."""
        for child_id in list(node.children.values()):
            child = self.nodes.get(child_id)
            if child is not None:
                await self._reclaim_node(child)
        node.children.clear()

    async def _reclaim_node(self, node: _ChannelNode) -> None:
        """Fully tear down ``node`` and its subtree: abort it (clean event) if still
        open, recursively reclaim its children, close both hubs, release both
        residency scopes, and drop it from the registry. Used by the parent-terminal
        cascade and the tree-close teardown."""
        if node.stage in ("unstarted", "open"):
            await self.abort_node(node, "parent closed")  # publishes abort + reclaims its children
        else:
            await self._reclaim_children(node)
        await node.full_hub.aclose()
        await node.state_hub.aclose()
        await node.subchannels_hub.aclose()
        node.durable_release.set()
        node.data_release.set()
        self.nodes.pop(node.id, None)

    # -- reader-side ops --

    async def _await_ready(self, node: _ChannelNode) -> None:
        """Block until ``node`` has started (or reached a terminal stage) — the
        gate for ``get_tag`` / ``peek_*`` so they never observe the pre-start
        ``unstarted`` stage."""
        await node.ready.wait()

    async def get_tag(self, node: _ChannelNode, into: FxtrScope) -> FxtrID:
        """Return ``node``'s tag id (copied into ``into`` so the caller can return
        it). Blocks until started.

        Args:
            node: The node whose tag is requested.
            into: The scope to copy the tag closure into (the reader dispatch's
                active scope, so the return arena covers it).

        Raises:
            RuntimeError: ``node`` aborted before it ever started (no tag).
        """
        await self._await_ready(node)
        if node.tag_id is None:
            raise RuntimeError("channel aborted before it started; no tag")
        await into.copy_from_scope(node.tag_id, node.durable_scope)
        return node.tag_id

    async def peek_full(self, node: _ChannelNode, into: FxtrScope) -> FxtrServiceData:
        """Return ``node``'s current full-stream snapshot envelope (open / complete
        / abort per stage), copying its closure into ``into``. Blocks until started.

        Args:
            node: The node to peek.
            into: The scope to copy the snapshot's closure into (the reader
                dispatch's active scope).
        """
        await self._await_ready(node)
        async with node.lock:
            if node.stage == "abort" and self._never_started(node):
                return self._aborted_before_start(node)  # statusless, no closure
            if node.stage == "complete":
                env = self._complete_snapshot(node)
                source = node.durable_scope
            elif node.stage == "abort":
                env = self._abort_snapshot(node)
                source = node.durable_scope
            else:
                env = self._open_snapshot(node)
                source = node.data_scope
            await _copy_closure(into, [env], source)
        return env

    async def peek_state(self, node: _ChannelNode, into: FxtrScope) -> FxtrServiceData:
        """Return ``node``'s current :class:`ChannelState` envelope (status +
        stage) — or :class:`ChannelAbortedBeforeStart` if it aborted before
        starting — copying any closure into ``into``. Blocks until started-or-aborted.

        Args:
            node: The node to peek.
            into: The scope to copy the status closure into (the reader dispatch's
                active scope).
        """
        await self._await_ready(node)
        async with node.lock:
            if node.stage == "abort" and self._never_started(node):
                return self._aborted_before_start(node)  # statusless, no closure
            env = self._state(node)
            source = node.durable_scope if node.stage in ("complete", "abort") else node.data_scope
            await _copy_closure(into, [env], source)
        return env

    async def subscribe_full(
        self, node: _ChannelNode, subscriber: ServiceRef[Any], source_scope: FxtrScope
    ) -> ServiceRef[UnsubscribeHandle]:
        """Subscribe ``subscriber`` to ``node``'s full event stream and return its
        unsubscribe handle. Seeds the current snapshot under the node lock (so the
        seed + live stream can't tear); a pre-start subscriber gets no seed and
        converges when ``start`` publishes the open snapshot.

        Args:
            node: The node to subscribe to.
            subscriber: The (already retyped to ``Subscriber[FxtrServiceData]``)
                subscriber ref, reachable from ``source_scope``.
            source_scope: The scope the subscriber is bridged from (the reader
                dispatch's active scope).
        """
        async with node.lock:
            if node.stage == "unstarted":
                return await node.full_hub.subscribe(subscriber, source_scope)
            if node.stage == "abort" and self._never_started(node):
                return await node.full_hub.subscribe(
                    subscriber, source_scope, seed=[self._aborted_before_start(node)]
                )
            if node.stage == "complete":
                return await node.full_hub.subscribe(
                    subscriber,
                    source_scope,
                    seed=[self._complete_snapshot(node)],
                    seed_origin=node.durable_scope,
                )
            if node.stage == "abort":
                return await node.full_hub.subscribe(
                    subscriber,
                    source_scope,
                    seed=[self._abort_snapshot(node)],
                    seed_origin=node.durable_scope,
                )
            return await node.full_hub.subscribe(
                subscriber,
                source_scope,
                seed=[self._open_snapshot(node)],
                seed_origin=node.data_scope,
            )

    async def subscribe_state(
        self, node: _ChannelNode, subscriber: ServiceRef[Any], source_scope: FxtrScope
    ) -> ServiceRef[UnsubscribeHandle]:
        """Subscribe ``subscriber`` to ``node``'s cheap replace-only state stream
        (coalesce) and return its unsubscribe handle. Like :meth:`subscribe_full`
        but seeds a single :class:`ChannelState` and a pre-start subscriber gets no
        seed (converges on ``start``).

        Args:
            node: The node to subscribe to.
            subscriber: The (retyped) subscriber ref, reachable from ``source_scope``.
            source_scope: The scope the subscriber is bridged from.
        """
        async with node.lock:
            if node.stage == "unstarted":
                return await node.state_hub.subscribe(subscriber, source_scope)
            if node.stage == "abort" and self._never_started(node):
                return await node.state_hub.subscribe(
                    subscriber, source_scope, seed=[self._aborted_before_start(node)]
                )
            origin = node.durable_scope if node.stage in ("complete", "abort") else node.data_scope
            return await node.state_hub.subscribe(
                subscriber, source_scope, seed=[self._state(node)], seed_origin=origin
            )

    async def subscribe_debug_subchannels(
        self, node: _ChannelNode, subscriber: ServiceRef[Any], source_scope: FxtrScope
    ) -> ServiceRef[UnsubscribeHandle]:
        """Subscribe ``subscriber`` to ``node``'s debug subchannel feed (queue) and
        return its unsubscribe handle. Seeds the current ``subchannel_log`` (one
        :class:`SubchannelOpened` per already-spawned child); live spawns follow.
        Held under the node lock so the seed + live stream can't tear against a
        concurrent ``spawn_subchannel`` (which appends + publishes under the same
        lock). The payloads are data-only, so no seed origin is needed.

        Args:
            node: The node to subscribe to.
            subscriber: The (retyped) subscriber ref, reachable from ``source_scope``.
            source_scope: The scope the subscriber is bridged from.
        """
        async with node.lock:
            return await node.subchannels_hub.subscribe(
                subscriber, source_scope, seed=list(node.subchannel_log)
            )


class _WriterImpl(
    TypedServiceImpl[FxtrChannelWriter[Entity, FxtrServiceData, FxtrServiceData, FxtrServiceData]]
):
    """Thin writer router. Carries an abort-on-release lifespan: when its mount
    scope drains (the caller released the writer ref) an unfinished channel is
    aborted."""

    def __init__(self, tree: _ChannelTree, node_id: int) -> None:
        super().__init__()
        self._tree = tree
        self._node_id = node_id

    def _target_type_annotation(self) -> Any:
        return _UNTYPED_WRITER

    def _service_type_id(self) -> FxtrID:
        return CHANNEL_WRITER_TYPE_ID

    @contextlib.asynccontextmanager
    async def _fxtr_mount_lifespan(self, scope: FxtrScope) -> AsyncIterator[None]:
        yield None
        # Teardown (post-drain, unauthorized for `scope`): abort the node if it's
        # still open — the release-without-finish net. Skip during tree close
        # (the tree-close teardown reclaims everything).
        if self._tree._closing:  # noqa: SLF001
            return
        node = self._tree.nodes.get(self._node_id)
        if node is not None:
            await self._tree.abort_node(node, "writer released without finishing")

    def _node(self) -> _ChannelNode:
        node = self._tree.nodes.get(self._node_id)
        if node is None:
            raise RuntimeError("channel is no longer available (its parent closed)")
        return node

    @service_method
    async def start(self, tag: FxtrID, initial_status: FxtrServiceData) -> None:
        await self._tree.start_node(self._node(), tag, initial_status, self._fxtr_get_input_scope())

    @service_method
    async def emit(
        self,
        *,
        events: list[FxtrServiceData] | None = None,
        status: FxtrServiceData | _UseRemoteDefault = USE_REMOTE_DEFAULT,
        replace_history: bool = False,
    ) -> None:
        status_changed = not isinstance(status, _UseRemoteDefault)
        await self._tree.emit_node(
            self._node(),
            list(events or []),
            status if status_changed else None,
            status_changed,
            replace_history,
            self._fxtr_get_input_scope(),
        )

    @service_method
    async def finish(
        self,
        final_value: FxtrServiceData,
        final_status: FxtrServiceData | _UseRemoteDefault = USE_REMOTE_DEFAULT,
    ) -> None:
        status_changed = not isinstance(final_status, _UseRemoteDefault)
        await self._tree.finish_node(
            self._node(),
            final_value,
            final_status if status_changed else None,
            status_changed,
            self._fxtr_get_input_scope(),
        )

    @service_method
    async def spawn_subchannel(self, name: str | None = None) -> tuple[str, ServiceRef[Any]]:
        return await self._tree.spawn_subchannel(self._node_id, name, get_active_scope())

    @service_method
    async def abort(self, reason: str) -> None:
        await self._tree.abort_node(self._node(), reason)


class _ReaderImpl(
    TypedServiceImpl[FxtrChannelReader[Entity, FxtrServiceData, FxtrServiceData, FxtrServiceData]]
):
    """Thin reader router (no lifespan — reading never affects channel lifetime)."""

    def __init__(self, tree: _ChannelTree, node_id: int) -> None:
        super().__init__()
        self._tree = tree
        self._node_id = node_id

    def _target_type_annotation(self) -> Any:
        return _UNTYPED_READER

    def _service_type_id(self) -> FxtrID:
        return CHANNEL_READER_TYPE_ID

    def _node(self) -> _ChannelNode:
        node = self._tree.nodes.get(self._node_id)
        if node is None:
            raise RuntimeError("channel is no longer available (its parent closed)")
        return node

    @service_method
    async def get_tag(self) -> FxtrID:
        return await self._tree.get_tag(self._node(), get_active_scope())

    @service_method
    async def subscribe_full(self, subscriber: ServiceRef[Any]) -> ServiceRef[UnsubscribeHandle]:
        retyped = subscriber.replace_service_type(Subscriber[FxtrServiceData])
        return await self._tree.subscribe_full(self._node(), retyped, get_active_scope())

    @service_method
    async def subscribe_state(self, subscriber: ServiceRef[Any]) -> ServiceRef[UnsubscribeHandle]:
        retyped = subscriber.replace_service_type(Subscriber[FxtrServiceData])
        return await self._tree.subscribe_state(self._node(), retyped, get_active_scope())

    @service_method
    async def peek_full(self) -> FxtrServiceData:
        return await self._tree.peek_full(self._node(), get_active_scope())

    @service_method
    async def peek_state(self) -> FxtrServiceData:
        return await self._tree.peek_state(self._node(), get_active_scope())

    @service_method
    async def open_subchannel_reader(self, key: str) -> ServiceRef[Any]:
        return await self._tree.open_subchannel_reader(self._node_id, key, get_active_scope())

    @service_method
    async def subscribe_debug_subchannels(
        self, subscriber: ServiceRef[Any]
    ) -> ServiceRef[UnsubscribeHandle]:
        retyped = subscriber.replace_service_type(Subscriber[FxtrServiceData])
        return await self._tree.subscribe_debug_subchannels(
            self._node(), retyped, get_active_scope()
        )


# =============================================================================
# Helpers (typed-ref construction + consumer side)
# =============================================================================


@contextlib.asynccontextmanager
async def _tree_lifespan(mount_scope: FxtrScope) -> AsyncIterator[_ChannelTree]:
    """The channel tree's lifespan, attached to its mount scope. Setup opens the
    tree task group + the root node; teardown reclaims the whole tree."""
    async with anyio.create_task_group() as tg:
        tree = _ChannelTree(mount_scope, tg)
        root = await tree.make_node(parent_id=None, key=None, debug_name="root")
        tree.root_id = root.id
        try:
            yield tree
        finally:
            tree._closing = True  # noqa: SLF001
            await tree._reclaim_node(root)  # noqa: SLF001


@typing.overload
async def create_root_channel[
    S, V, R
](*, tag_type: type[ChannelTag[S, V, R]], scope: FxtrScope | None = None,) -> tuple[
    ServiceRef[FxtrChannelWriter[ChannelTag[S, V, R], S, V, R]],
    ServiceRef[FxtrChannelReader[ChannelTag[S, V, R], S, V, R]],
]: ...


@typing.overload
async def create_root_channel(
    *,
    tag_type: None = None,
    scope: FxtrScope | None = None,
) -> tuple[
    ServiceRef[FxtrChannelWriter[_U, _U, _U, _U]],
    ServiceRef[FxtrChannelReader[_U, _U, _U, _U]],
]: ...


async def create_root_channel(
    *,
    tag_type: Any = None,
    scope: FxtrScope | None = None,
) -> tuple[ServiceRef[Any], ServiceRef[Any]]:
    """Create and mount a root channel; return its ``(writer, reader)`` refs.

    Args:
        tag_type: The channel's tag type (a concrete :class:`ChannelTag` subclass)
            used only to type the returned refs (so the proxies encode/decode
            ``S``/``V``/``R`` correctly). If ``None``, the refs come back
            un-inferred and must be retyped (e.g. via
            ``start_channel_and_infer_types`` / ``infer_channel_reader_type``).
        scope: The scope to mount on (default: the active scope) — the tree lives
            as long as it.

    Returns:
        ``(writer, reader)``. The writer must be ``start``ed before use.
    """
    mount_scope = scope or get_active_scope()
    tree = await mount_scope.attach_lifespan_to_scope(_tree_lifespan(mount_scope))
    assert tree.root_id is not None
    writer_ref = await mount_scope.mount(_WriterImpl(tree, tree.root_id))
    reader_ref = await mount_scope.mount(_ReaderImpl(tree, tree.root_id))
    if tag_type is not None:
        s, v, r = infer_channel_types(tag_type)
        writer_ref = writer_ref.replace_service_type(
            typing.cast(Any, FxtrChannelWriter)[tag_type, s, v, r]
        )
        reader_ref = reader_ref.replace_service_type(
            typing.cast(Any, FxtrChannelReader)[tag_type, s, v, r]
        )
    else:
        writer_ref = writer_ref.replace_service_type(FxtrChannelWriter[_U, _U, _U, _U])
        reader_ref = reader_ref.replace_service_type(FxtrChannelReader[_U, _U, _U, _U])
    return writer_ref, reader_ref


async def start_channel_and_infer_types[
    S, V, R
](
    untyped_writer: ServiceRef[Any],
    tag: EntityRef[ChannelTag[S, V, R]],
    initial_status: S,
    *,
    scope: FxtrScope | None = None,
) -> ServiceRef[FxtrChannelWriter[ChannelTag[S, V, R], S, V, R]]:
    """Start ``untyped_writer`` and return it retyped from the tag's ``(S, V, R)``.

    The producer-side counterpart to a freshly spawned subchannel writer (whose
    types are un-inferred): infers ``(S, V, R)`` from ``tag``'s type, retypes the
    writer so subsequent ``emit`` / ``finish`` calls encode correctly, resolves it,
    and calls ``start(tag, initial_status)``.

    Args:
        untyped_writer: An un-started writer ref (e.g. from ``spawn_subchannel``).
        tag: The tag entity ref; must be a concrete :class:`ChannelTag` subclass.
        initial_status: The channel's initial status.
        scope: The scope to resolve the writer in (default: the active scope).

    Returns:
        The writer ref retyped to its inferred ``FxtrChannelWriter[..., S, V, R]``.
    """
    s, v, r = infer_channel_types(tag.target_type)
    typed = untyped_writer.replace_service_type(
        typing.cast(Any, FxtrChannelWriter)[tag.target_type, s, v, r]
    )
    home = scope or get_active_scope()
    proxy = await typed.resolve(home)
    await proxy.start(tag, initial_status)
    return typed


def infer_channel_reader_type[
    S, V, R
](reader: ServiceRef[Any], as_type: type[ChannelTag[S, V, R]]) -> ServiceRef[
    FxtrChannelReader[ChannelTag[S, V, R], S, V, R]
]:
    """Retype a (wire-received) reader ref from its tag type.

    The consumer-side counterpart to ``infer_channel_types``: wire-received reader
    refs arrive un-inferred, so retype before passing to ``iter_channel`` / using
    ``peek_*`` so the proxy decodes ``S``/``V``/``R`` correctly.

    Args:
        reader: The un-inferred reader ref.
        as_type: The channel's tag type (a concrete :class:`ChannelTag` subclass).

    Returns:
        ``reader`` retyped to ``FxtrChannelReader[as_type, S, V, R]``.
    """
    s, v, r = infer_channel_types(as_type)
    return reader.replace_service_type(typing.cast(Any, FxtrChannelReader)[as_type, s, v, r])


def decode_channel_update(
    data: FxtrServiceData,
    *,
    tag_type: Any,
    status_type: Any = None,
    message_type: Any = None,
    result_type: Any = None,
) -> ChannelUpdate[Any, Any, Any]:
    """Decode one wire full-stream event dict into its :data:`ChannelUpdate` dataclass.

    Lower-level utility for dynamic consumers — typed subscribers / ``iter_channel``
    get this decode for free via their annotation.

    Args:
        data: The wire event (``kind``-discriminated dict).
        tag_type: The tag entity type; ``(S, V, R)`` are inferred from it for any
            of the type args not passed explicitly.
        status_type: Explicit override for ``S``.
        message_type: Explicit override for ``V``.
        result_type: Explicit override for ``R``.

    Returns:
        The decoded :data:`ChannelUpdate` dataclass.
    """
    if status_type is None or message_type is None or result_type is None:
        s, v, r = infer_channel_types(tag_type)
        status_type = status_type if status_type is not None else s
        message_type = message_type if message_type is not None else v
        result_type = result_type if result_type is not None else r
    return decode_from_service_data(data, ChannelUpdate[status_type, message_type, result_type])


def _reader_type_args(reader: ServiceRef[Any]) -> tuple[Any, Any, Any]:
    target = reader.target_type
    if typing.get_origin(target) is not FxtrChannelReader:
        raise TypeError(
            f"iter_channel needs a fully parameterized ServiceRef[FxtrChannelReader[T, S, V, R]]; "
            f"got target type {target!r}. Retype via infer_channel_reader_type first."
        )
    args = typing.get_args(target)
    if len(args) != 4 or any(isinstance(a, typing.TypeVar) for a in args):
        raise TypeError(
            f"iter_channel needs all FxtrChannelReader type args bound; got {target!r}."
        )
    return args[1], args[2], args[3]


class _IterSubscriberImpl(TypedServiceImpl[Subscriber[FxtrServiceData]]):
    """Internal subscriber backing :func:`iter_channel`: retains each event's
    entity closures into the consumer scope (inside the delivery dispatch) before
    decoding + forwarding, so refs in yielded events stay loadable."""

    def __init__(
        self, scope: FxtrScope, status_type: Any, message_type: Any, result_type: Any, send: Any
    ) -> None:
        super().__init__()
        self._scope = scope
        self._status_type = status_type
        self._message_type = message_type
        self._result_type = result_type
        self._send = send

    def _target_type_annotation(self) -> Any:
        return Subscriber[FxtrServiceData]

    def _service_type_id(self) -> FxtrID:
        return _ITER_CHANNEL_SUBSCRIBER_TYPE_ID

    @service_method
    async def on_event(self, payload: FxtrServiceData) -> None:
        input_scope = self._fxtr_get_input_scope()
        await _copy_closure(self._scope, [payload], input_scope)
        event = decode_from_service_data(
            payload, ChannelUpdate[self._status_type, self._message_type, self._result_type]
        )
        try:
            self._send.send_nowait(event)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            # The consumer stopped reading (left the iter_channel block); the
            # subscription is being torn down — drop the trailing event.
            pass


@contextlib.asynccontextmanager
async def iter_channel[
    T: Entity, S, V, R
](
    reader: ServiceRef[FxtrChannelReader[T, S, V, R]],
    scope: FxtrScope,
) -> AsyncIterator[
    AsyncIterator[ChannelUpdate[S, V, R]]
]:
    """Subscribe to a channel's full stream and yield its decoded events.

    ``reader`` must be a fully parameterized
    ``ServiceRef[FxtrChannelReader[T, S, V, R]]`` (retype a wire-received ref via
    :func:`infer_channel_reader_type`). Refs in yielded events are usable from
    ``scope`` (copied in during delivery). Unsubscribes on exit (best-effort).
    """
    status_type, message_type, result_type = _reader_type_args(reader)
    send, receive = anyio.create_memory_object_stream[Any](math.inf)
    subscriber_ref = await scope.mount(
        _IterSubscriberImpl(scope, status_type, message_type, result_type, send)
    )
    proxy = await reader.resolve(scope)
    typed_subscriber = subscriber_ref.replace_service_type(
        Subscriber[ChannelUpdate[status_type, message_type, result_type]]
    )
    handle_ref = await proxy.subscribe_full(typed_subscriber)
    try:
        yield receive
    finally:
        send.close()
        with anyio.move_on_after(10):
            try:
                handle = await handle_ref.resolve(scope)
                await handle.unsubscribe()
            except Exception:  # noqa: BLE001 — best-effort (source may be gone)
                pass
