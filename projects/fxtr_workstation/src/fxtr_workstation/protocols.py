"""Service protocols exposed by the workstation server.

Currently:

- :class:`WorkstationServiceProto` — the workstation's main service,
  mounted per-connection on every ``/rpc`` connection.
- :class:`ReleaseCallbackProto` — the small ref returned by
  ``register_service`` (and later ``copy_entity``). Holding the ref
  alive on the caller side keeps the registration alive; calling
  ``release()`` (or dropping the ref so the dispatch scope drains)
  releases it.
- :class:`WorkstationSessionEventInbox` — the protocol exposed by
  workstation-minted callback (inbox) services. RPC peers that
  receive an inbox ref invoke ``notify`` to push a payload into the
  owning session's event log (direct push), or hand the inbox an
  :class:`~fxtr2.services.subscription_types.EventSource` via
  ``subscribe_to`` for a durable feed whose subscription lifecycle
  the workstation owns.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, Protocol

import anyio
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef
from fxtr2.services.service_dialect import FxtrServiceData, OverrideThisTypeBeforeUsing
from fxtr2.services.typed_service import service_protocol

_log = logging.getLogger(__name__)

#: Stable namespace UUID for workstation-defined types. Don't change.
WORKSTATION_NS = uuid.UUID("25c68e62-9639-4f8d-941d-01b9a49e9247")


WORKSTATION_SERVICE_TYPE_ID = system_entities.define_type(WORKSTATION_NS, "workstation_service")
RELEASE_CALLBACK_TYPE_ID = system_entities.define_type(WORKSTATION_NS, "release_callback")
#: Type ID of the session-event-inbox service minted per callback slot
#: (the impl satisfying :class:`WorkstationSessionEventInbox`).
SESSION_EVENT_INBOX_TYPE_ID = system_entities.define_type(WORKSTATION_NS, "session_event_inbox")
#: Type ID of the workstation-internal
#: :class:`~fxtr2.services.subscription_types.Subscriber` impl an inbox
#: mounts per ``subscribe_to`` subscription.
SESSION_INBOX_SUBSCRIBER_TYPE_ID = system_entities.define_type(
    WORKSTATION_NS, "session_inbox_subscriber"
)

#: Type ID for persistence caps configured with
#: ``mode="no_provenance"`` — pin/retrieve operate on the raw FxtrID
#: as the namespace receives it; provenance information (if any) is
#: silently dropped on pin and absent on retrieve.
PERSISTENCE_NO_PROV_TYPE_ID = system_entities.define_type(WORKSTATION_NS, "persistence/no_prov")
#: Type ID for persistence caps configured with
#: ``mode="preserve_provenance"`` — bare data is lifted to a u-tag
#: prov node before pinning, layered IDs have their prov half pinned
#: directly, and retrieve re-attaches the prov parent so the caller
#: gets a layered FxtrID. Stage 7c implements the impl bodies; in 7b
#: the cap brings up but methods raise ``NotImplementedError``.
PERSISTENCE_PRESERVE_PROV_TYPE_ID = system_entities.define_type(
    WORKSTATION_NS, "persistence/preserve_prov"
)


@service_protocol
class WorkstationSessionEventInbox[T: FxtrServiceData](Protocol):
    """An inbox for events in a workstation session.

    A session creates a callback via the ``create_callback`` mutation
    op; the workstation mounts an impl satisfying this protocol and
    binds it to the session's slot. Two ways for events to arrive:

    - **Direct push** (:meth:`notify`) — RPC peers that receive a ref
      to the inbox (via invoke args, typically) push individual
      payloads into the owning session's event log. Suited to
      invoke-scoped, "here's a payload" interactions.
    - **Durable feed** (:meth:`subscribe_to`) — the inbox is handed an
      :class:`~fxtr2.services.subscription_types.EventSource` and
      subscribes itself to it, staying subscribed until the inbox
      closes. The workstation owns the subscription's lifecycle:
      session clients never manage unsubscribes themselves.

    The receive side closes when the originating session emits a
    ``close_callback`` op or when the session itself closes. After
    that, :meth:`notify` is a silent no-op and :meth:`listening`
    returns ``False`` even if the underlying service ref is still
    alive on remote peers (drained-but-alive); active subscriptions
    are unsubscribed and torn down by the workstation.

    Deliberate invariant: the push method is named ``notify``, **not**
    ``on_event`` — an inbox ref is structurally *not* a
    :class:`~fxtr2.services.subscription_types.Subscriber`, so it can
    never be handed straight to ``EventSource.subscribe``. Every
    durable subscription is forced through :meth:`subscribe_to`, where
    the workstation owns the lifecycle: if you have an
    ``EventSource``, use ``subscribe_to``.
    """

    async def subscribe_to(
        self, event_source: ServiceRef[Any], method: str = "subscribe", /, **kwargs: FxtrServiceData
    ) -> None:
        """Subscribe this inbox to ``event_source`` for the duration of
        the inbox.

        The workstation mounts an internal
        :class:`~fxtr2.services.subscription_types.Subscriber` whose
        events land in the owning session's event log exactly like
        :meth:`notify` payloads, calls the source's ``method`` with it,
        and holds the returned unsubscribe handle. When the inbox closes
        (close-callback op or session close), the workstation
        unsubscribes from the source (best-effort, with a timeout) and
        tears the subscription down.

        ``event_source`` and ``method`` are **positional-only**;
        ``method`` (default ``"subscribe"``) is the name of the source's
        subscription method. The source need not statically be an
        :class:`~fxtr2.services.subscription_types.EventSource`: the
        workstation dispatches ``method`` against the source as an
        untyped service, so any service exposing a ``method(subscriber)``
        that returns an
        :class:`~fxtr2.services.subscription_types.UnsubscribeHandle`-shaped
        ref works. With the default ``method`` this is exactly the
        ``EventSource.subscribe`` contract.

        ``**kwargs`` are forwarded to the source's ``method`` after the
        subscriber ref — ``method(subscriber, **kwargs)`` — for
        parameterized subscription surfaces. By convention such a source
        declares its extra parameters **keyword-only** (e.g. a source
        exposing ``watch(subscriber, *, key)``); keyword forwarding keeps
        the call order-independent and self-documenting (no positional
        ``*args`` whose order must line up with the subscriber-first
        slot). Values are ordinary wire data (entity / service refs
        included), decoded by the source's own typed dispatch.

        **Exactly once.** An inbox holds at most one *live* subscription
        at a time; any ``subscribe_to`` while one is live raises —
        including a repeat for the same source. To subscribe again,
        close/forget the inbox slot (or wait for the current
        subscription to break) first.

        Raises if the inbox is already closed, if a live subscription
        already exists, or if the source's ``method`` call fails (the
        subscription is not retained in that case).
        """
        ...

    async def notify(self, payload: T) -> None:
        """Push one payload into the owning session's event log.

        Args:
            payload: The event data, in the
                :data:`~fxtr2.services.service_dialect.FxtrServiceData`
                vocabulary. May contain entity / service refs that the
                workstation will track on the owning session.

        Silent no-op when the inbox is closed (or the owning session is
        gone) — callers that care probe :meth:`listening` first. There
        is no delivery acknowledgement; direct push is fire-and-forget.
        """
        ...

    async def listening(self) -> bool:
        """Return ``True`` if the inbox is still accepting events.

        Side-effect-free probe of the same closed-flag observed by
        :meth:`notify`. Useful when the caller wants to skip work
        (e.g. expensive payload assembly) before attempting a notify.
        """
        ...


@service_protocol
class PersistenceProto(Protocol):
    """Persistence cap exposed by the workstation for one configured
    namespace (stage 7).

    One persistence cap is mounted per ``PersistenceNamespaceConfig``
    passed to :class:`WorkstationServer`. Each cap is a regular
    workstation-tracked service: it gets an ``s{N}`` short ID and a
    permanent external service name (``config.external_name``) so
    sessions and RPC peers can find it via the standard
    ``get_service`` / ``get_service_by_external_name`` paths.

    The protocol is shared between the ``no_provenance`` and
    ``preserve_provenance`` modes — what differs is the impl class
    (and thus the service ``type_id``). Mode-specific details:

    - **No-prov mode** (:data:`PERSISTENCE_NO_PROV_TYPE_ID`): pins
      and retrieves are forwarded straight through to the underlying
      ``FxtrNamespace``. Pinning a layered (provenance-bearing)
      FxtrID silently drops the prov half — the namespace stores
      just the data ID.
    - **Preserve-prov mode** (:data:`PERSISTENCE_PRESERVE_PROV_TYPE_ID`):
      bare data is lifted to a ``u``-tag prov node before pinning;
      layered IDs have their prov half (``extract_provenance_parent``)
      pinned directly. On retrieve, the cap re-attaches the prov
      parent via ``embed_as_provenance_parent`` so callers get a
      layered FxtrID. Retrieving a non-prov-tagged pin raises.

    Lifetimes: the namespace is opened externally and passed in by
    the user. The workstation's ``root_scope`` includes each
    namespace's scope, so retrieved entities can be lazily loaded
    from the namespace through the standard inheritance chain. The
    workstation does **not** mint a tracked-entity record from inside
    the cap — when a session invokes ``retrieve``, the result FxtrID
    flows through the existing invoke-result delivery pipeline,
    which copies the entity into a per-record scope at commit time.
    RPC peers invoking ``retrieve`` get a chain-bridged FxtrID; the
    workstation never sees it again.
    """

    async def pin(self, key: str, entity: FxtrID, *, overwrite: bool = False) -> None:
        """Pin ``entity`` into the namespace under ``key``.

        Args:
            key: Pin key. Namespace-specific format (e.g.
                colon-separated paths for ``TsvFxtrNamespace``).
            entity: The entity to pin. Must be reachable from the
                cap's call scope (the standard typed-service
                framework decodes invoke args into the call scope).
            overwrite: If ``False`` (default), raises if a pin
                already exists at this key. If ``True``, replaces
                the existing pin.
        """
        ...

    async def unpin(self, key: str) -> None:
        """Drop a pin from the namespace.

        Args:
            key: Pin key to remove. Idempotency / "missing key"
                semantics are inherited from the underlying
                namespace backend.
        """
        ...

    async def retrieve(self, key: str) -> FxtrID:
        """Retrieve a pin from the namespace and bridge the FxtrID
        back to the caller.

        The entity remains lazily-loadable through the namespace's
        backing store — no eager copy. The returned FxtrID is
        bridged into the caller's scope through the normal
        chain-bridge path.

        Raises:
            KeyError: if ``key`` has no pin.
            ValueError: in preserve-prov mode if the pin's payload
                is not a provenance-tag node.
        """
        ...

    async def try_retrieve(self, key: str) -> FxtrID | None:
        """Like :meth:`retrieve`, but returns ``None`` instead of
        raising ``KeyError`` when ``key`` has no pin.
        """
        ...

    async def list_pins(self, prefix: str = "") -> list[str]:
        """Return all pin keys in the namespace whose key starts
        with ``prefix`` (empty prefix = all keys).

        The list reflects the namespace's current state at call
        time; each call goes straight through to the backend with
        no caching on the cap side.
        """
        ...


@service_protocol
class ReleaseCallbackProto(Protocol):
    """Returned by ``register_service`` (and later ``copy_entity``).

    The caller should keep the chain-bridged ref alive while it wants
    the registration to remain in effect; dropping the ref triggers
    auto-release via the dispatch scope's end callback. Calling
    :meth:`release` is the explicit (and immediate) form. Both paths
    are idempotent.
    """

    async def release(self) -> None: ...


@service_protocol
class WorkstationServiceProto(Protocol):
    """The main service exposed by the workstation on every ``/rpc``
    connection. One impl is mounted per connection so methods can
    attribute work to the calling :class:`RPCConnectionRecord`.
    """

    async def name_connection(self, name: str) -> None:
        """Set a human-readable name for this RPC connection.

        Last writer wins. Visible in the workstation's connection-list
        view (debug UI, etc.).
        """
        ...

    async def register_service(
        self, external_name: str, service: ServiceRef[Any]
    ) -> tuple[str, ServiceRef[ReleaseCallbackProto]]:
        """Register ``service`` with the workstation under ``external_name``.

        Returns ``(short_id, release_cb_ref)``. Multiple registrations
        for the same underlying service handle coalesce onto one
        workstation record but each has its own release callback.

        **External names are exclusive across a single namespace
        shared between services and entities.** If ``external_name``
        is already bound by an active registration of either kind
        (service via this method, or entity via :meth:`copy_entity`),
        this raises rather than silently overwriting — otherwise it
        would be impossible to reason about which registration "owns"
        a name. Release the existing registration first, or pick a
        different name. Note that registering the *same* underlying
        service handle twice still requires distinct external names.

        **Most callers should use the** :func:`expose_service` **async
        context manager helper instead of calling this method directly.**
        It handles explicit release on exit and forces a scope-side
        bridge drop as a backstop, sidestepping the cycle described
        below.

        **If you do call this directly: call ``release_cb_ref.release()``
        explicitly before tearing down the scope that owns ``service``.**
        Relying on auto-release-on-drop is structurally unsafe when the
        registered ``service`` is mounted in the same scope as the
        ``release_cb_ref`` (or any ancestor of it): the workstation
        holds a bridge to ``service`` whose source is the dispatch
        scope of this call, and that bridge pins the dispatch scope
        until the registration is released. The dispatch scope only
        drains after the holder drops ``release_cb_ref``, but in this
        layout the holder won't drop it until *its* scope drains, which
        won't complete until the workstation lets go of ``service`` —
        which only happens once the registration is released.
        Calling ``release()`` short-circuits the cycle.

        If you want the auto-release path to work, resolve the
        workstation ref into a *child* scope of the scope that owns
        ``service``, so the chain-bridged ``release_cb_ref`` lives in
        the inner scope and dropping it (by exiting the inner scope)
        releases the registration while the outer scope is still alive
        to receive the lease-end for ``service``.
        """
        ...

    async def get_service(self, service_short_id: str) -> ServiceRef[OverrideThisTypeBeforeUsing]:
        """Look up an active service by its workstation short ID.

        Bridges the underlying service into the caller's scope. The
        returned ref is annotated as
        :data:`OverrideThisTypeBeforeUsing`; the caller is expected to
        ``replace_service_type`` to a concrete protocol before
        ``resolve``-ing.

        Raises a generic protocol error if the short ID isn't
        currently active (no live registration).
        """
        ...

    async def get_service_by_external_name(
        self, external_name: str
    ) -> ServiceRef[OverrideThisTypeBeforeUsing]:
        """Look up an active service by an external name.

        Resolves the unique active registration that bound
        ``external_name`` and bridges its service into the caller's
        scope, with the same annotation contract as
        :meth:`get_service`. External names are exclusive — see
        :meth:`register_service` — so there's no ambiguity here.

        Raises a generic protocol error if no active registration
        currently has this external name.
        """
        ...

    async def copy_entity(
        self, external_name: str, entity: FxtrID
    ) -> tuple[str, ServiceRef[ReleaseCallbackProto]]:
        """Copy ``entity`` into the workstation under ``external_name``.

        Returns ``(short_id, release_cb_ref)``. The entity's full
        transitive subgraph is copied into the workstation's
        ``entities_scope`` (so peers can ``load()`` it without keeping
        the registering connection alive). Multiple registrations of
        the same FxtrID coalesce onto one workstation record; each
        registration owns its own release callback.

        **External names are exclusive across a single namespace
        shared between entities and services**, with the same
        rejection behavior as :meth:`register_service`: if
        ``external_name`` is already bound by an active registration
        of either kind, this raises rather than overwriting.

        **Most callers should use the** :func:`expose_entity` **async
        context manager helper** instead of calling this method
        directly — it does explicit release on exit plus an
        unconditional scope-side bridge drop, sidestepping the
        auto-release-on-drop cycle described under
        :meth:`register_service`.
        """
        ...

    async def get_entity(self, entity_short_id: str) -> FxtrID:
        """Look up a tracked entity by its workstation short ID.

        Returns the FxtrID; the actual entity data is reachable via
        the calling connection's user scope through ``entities_scope``'s
        inheritance bridge (so ``EntityRef(returned_id, T).load()``
        works without further bookkeeping).

        Raises a generic protocol error if the short ID isn't currently
        active (no live registration).
        """
        ...

    async def get_entity_by_external_name(self, external_name: str) -> FxtrID:
        """Look up a tracked entity by an external name.

        Same return / error contract as :meth:`get_entity`, indexed by
        external name. External names are exclusive (see
        :meth:`copy_entity`).
        """
        ...

    async def lookup_short_id_by_external_name(self, external_name: str) -> str:
        """Look up the workstation short_id (entity or service)
        registered under ``external_name``.

        External names live in a **single namespace** shared between
        :meth:`register_service` and :meth:`copy_entity`: at most one
        active registration of either kind binds any given name, and
        a registration attempt that collides with the other kind is
        rejected just like a same-kind collision. Callers that have a
        name and want to find the corresponding short_id without
        bridging the entity/service ref into their scope use this
        method; callers that want the bridged ref use
        :meth:`get_service_by_external_name` /
        :meth:`get_entity_by_external_name` directly.

        Raises if no active registration is bound to ``external_name``.
        """
        ...


@asynccontextmanager
async def expose_service(
    workstation_ref: ServiceRef[WorkstationServiceProto],
    external_name: str,
    service: ServiceRef[Any],
    scope: FxtrScope | None = None,
    *,
    release_timeout: float = 5.0,
) -> AsyncGenerator[str, None]:
    """Register ``service`` for the duration of the context block.

    The recommended way to expose a service to the workstation. On
    entry: resolves ``workstation_ref`` into ``scope``, calls
    ``register_service(external_name, service)``, resolves the
    chain-bridged release callback into ``scope``, and yields the
    workstation short ID. On exit: explicitly calls ``release()``
    (guarded by ``anyio.move_on_after(release_timeout)`` so a stuck
    release can't block the context exit forever) and then
    unconditionally drops the chain-bridge for the release callback
    from ``scope``, forcing any outstanding lease to be discarded
    even if the in-band release didn't complete.

    This deliberately avoids the auto-release-on-drop cycle described
    in ``register_service``'s docstring: by the time we exit the
    context the registration is gone, so the call_scope of
    ``register_service`` is free to drain and the connection close
    that follows can complete cleanly.

    Args:
        workstation_ref: A reference to the workstation main service
            (typically obtained from
            ``session.remote_service.replace_service_type(WorkstationServiceProto)``).
        external_name: Human-readable name to register under.
        service: The service ref to expose.
        scope: Scope to resolve refs into. Defaults to the active
            scope captured at context entry.
        release_timeout: Seconds to wait for ``release()`` to complete
            during cleanup before falling back on the unconditional
            scope-side drop. Default 5s.

    Yields:
        The workstation short ID assigned to ``service``.
    """
    if scope is None:
        scope = FxtrScope.get_active()
    workstation = await workstation_ref.resolve(scope)
    short_id, release_cb_ref = await workstation.register_service(external_name, service)
    rcb = await release_cb_ref.replace_service_type(ReleaseCallbackProto).resolve(scope)
    try:
        yield short_id
    finally:
        # Best-effort explicit release. ``move_on_after`` shields
        # against a stuck release blocking context exit.
        with anyio.move_on_after(release_timeout) as cancel_scope:
            try:
                await rcb.release()
            except Exception:
                _log.exception(
                    "expose_service(%r): explicit release() raised; "
                    "falling back on scope-side bridge drop",
                    external_name,
                )
        if cancel_scope.cancelled_caught:
            _log.warning(
                "expose_service(%r): release() did not complete within "
                "%.1fs; falling back on scope-side bridge drop",
                external_name,
                release_timeout,
            )
        # Unconditional cleanup: drop the chain-bridge for the release
        # callback from ``scope``. This forces the underlying lease to
        # be discarded immediately, even if the in-band release never
        # completed — protecting against the cycle described in
        # ``register_service``'s docstring.
        try:
            scope.forget_service(release_cb_ref)
        except ValueError:
            # Already forgotten (e.g., scope already dropped it). Fine.
            pass


@asynccontextmanager
async def expose_entity(
    workstation_ref: ServiceRef[WorkstationServiceProto],
    external_name: str,
    entity: FxtrID,
    scope: FxtrScope | None = None,
    *,
    release_timeout: float = 5.0,
) -> AsyncGenerator[str, None]:
    """Copy ``entity`` into the workstation for the duration of the context.

    The recommended way to expose an entity to the workstation. On
    entry: resolves ``workstation_ref`` into ``scope``, calls
    ``copy_entity(external_name, entity)``, resolves the chain-bridged
    release callback into ``scope``, and yields the workstation short
    ID. On exit: explicitly calls ``release()`` (guarded by
    ``anyio.move_on_after(release_timeout)``) and then unconditionally
    drops the chain-bridge for the release callback from ``scope``.
    Same cycle-avoidance behavior as :func:`expose_service`.

    Args:
        workstation_ref: A reference to the workstation main service.
        external_name: Human-readable name to register under.
        entity: The FxtrID to copy in.
        scope: Scope to resolve refs into. Defaults to the active
            scope captured at context entry.
        release_timeout: Seconds to wait for ``release()`` to complete
            during cleanup.

    Yields:
        The workstation short ID assigned to ``entity``.
    """
    if scope is None:
        scope = FxtrScope.get_active()
    workstation = await workstation_ref.resolve(scope)
    short_id, release_cb_ref = await workstation.copy_entity(external_name, entity)
    rcb = await release_cb_ref.replace_service_type(ReleaseCallbackProto).resolve(scope)
    try:
        yield short_id
    finally:
        with anyio.move_on_after(release_timeout) as cancel_scope:
            try:
                await rcb.release()
            except Exception:
                _log.exception(
                    "expose_entity(%r): explicit release() raised; "
                    "falling back on scope-side bridge drop",
                    external_name,
                )
        if cancel_scope.cancelled_caught:
            _log.warning(
                "expose_entity(%r): release() did not complete within "
                "%.1fs; falling back on scope-side bridge drop",
                external_name,
                release_timeout,
            )
        try:
            scope.forget_service(release_cb_ref)
        except ValueError:
            pass
