"""Protocol + entity-type definitions for the dynamic dispatch system.

See ``plans/phase0/part3_frontend_ui/stage10b_dynamic_dispatch.md`` for
the original design and
``plans/phase0/part3_frontend_ui/stage10c_dynamic_dispatch_improvements.md``
for the hot-reload additions.

The dynamic dispatch system lets the frontend resolve a renderer
component for an entity / service type at runtime, by invoking a
remote dispatcher service that returns a renderer entity (compiled
ESM bytes). The wire shape is:

- :class:`WorkstationDispatcherProto` — the typed service protocol the
  frontend invokes. ``resolve(type_ref, dispatch_key_uuid)`` returns
  a :class:`RendererResolution` carrying a free-form ``subtype_key``
  plus an :class:`EntityRef` for the renderer entity itself. The
  per-key argument lets backends route different dispatch keys for
  the same type to different modules. The protocol subclasses
  :class:`fxtr2.services.subscription_types.EventSource` — the
  dispatcher is an event source for renderer-table changes,
  delivering a :class:`ReloadEverythingNotification` ref to each
  subscriber on every table change, until unsubscribed.
- :class:`RendererEntity` — the loadable renderer entity. Its only
  field is ``module: bytes`` — the compiled ESM bundle. The entity's
  own content-addressed FxtrID is what the frontend uses as the
  in-memory module cache key, so no separate ``module_id`` is needed.
- :class:`ReloadEverythingNotification` — the v1 payload broadcast to
  every subscribed listener when the dispatcher's table changes. An
  empty :class:`DataclassEntity`; later versions may carry narrower
  signals (changed type ids, changed keys, etc.).
"""

from __future__ import annotations

import dataclasses
from typing import Protocol

from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data.entity_ref_scope import EntityRef, ServiceRef
from fxtr2.experimental.dynamic_dispatch import (
    DYNAMIC_DISPATCH_NS,
    RELOAD_EVERYTHING_NOTIFICATION_TYPE_ID,
    RENDERER_ENTITY_TYPE_ID,
    WORKSTATION_DISPATCHER_TYPE_ID,
    ReloadEverythingNotification,
    RendererEntity,
)
from fxtr2.services.subscription_types import EventSource, Subscriber, UnsubscribeHandle
from fxtr2.services.typed_service import service_protocol

# The entity types + type-id constants live in ``fxtr2.experimental.dynamic_dispatch``
# (so a serving backend can mint/serve them without depending on the workstation);
# they are re-exported here for the workstation's existing import sites. The
# dispatcher protocol + the resolve-result dataclass below stay workstation-local.
__all__ = [
    "DYNAMIC_DISPATCH_NS",
    "RELOAD_EVERYTHING_NOTIFICATION_TYPE_ID",
    "RENDERER_ENTITY_TYPE_ID",
    "WORKSTATION_DISPATCHER_TYPE_ID",
    "ReloadEverythingNotification",
    "RendererEntity",
    "RendererResolution",
    "WorkstationDispatcherProto",
    "NixDispatcherProto",
]


@dataclasses.dataclass(frozen=True)
class RendererResolution:
    """Result of a single :meth:`WorkstationDispatcherProto.resolve`
    call.

    ``subtype_key`` is opaque to the frontend — the backend picks
    whichever subtype label it wants, and the frontend simply uses it
    as the outer-level key into the compiled module's 2-level map.
    """

    subtype_key: str
    renderer: EntityRef[RendererEntity]


@service_protocol(allow_protocol_subtypes=True)
class WorkstationDispatcherProto(EventSource[EntityRef[ReloadEverythingNotification]], Protocol):
    """Service the dynamic dispatcher invokes to find a renderer for a
    given entity / service type.

    Subclasses :class:`fxtr2.services.subscription_types.EventSource`:
    the dispatcher is an event source whose payload is an
    :class:`EntityRef[ReloadEverythingNotification]` (exactly what the
    impls' ``SubscriberHub``s publish). ``subscribe`` is re-declared
    below purely for its docstring. Marked
    ``allow_protocol_subtypes=True`` because it is itself a base
    (:class:`NixDispatcherProto`).

    The frontend obtains the type ref by either:

    - **Entity stub**: loading the entity and reading its
      ``$fxtr.type`` field.
    - **Service stub**: invoking the framework method ``$fxtr.type()``
      on the service and using the returned FxtrID.

    Either way, the result is an :class:`EntityRef` for the type
    entity. The frontend passes that ref to :meth:`resolve`; the
    backend is free to ``id_string()`` it for a lookup or load it
    dynamically. The parameter is typed
    :class:`EntityRef[UnknownEntity]` because the dispatcher is
    generic over the entity's specific Python class — backends that
    want a strongly-typed load can re-attach a class via
    :meth:`EntityRef.replace_target_type`.

    Backends return ``None`` to indicate that no renderer is registered
    for the supplied type — the frontend surfaces that as the dispatch
    key's ``NoMatchFallback``. Backends should reserve raised
    :class:`IRException`s for unexpected failure conditions (e.g.
    "dispatcher not yet registered"); the frontend surfaces those as
    an ``errored`` terminal phase on the affected entry.

    ``resolve`` takes both the entity type ref *and* the call-site
    dispatch key uuid (the stringified UUID of the
    :class:`DispatchKey` the frontend is trying to fill). Backends
    can use this to route different dispatch keys for the same type
    to different renderer modules — handy when an agent shards
    visualizers for one entity across several files. Backends that
    don't need this can simply ignore the parameter.
    """

    async def resolve(
        self,
        type_ref: EntityRef[UnknownEntity],
        dispatch_key_uuid: str,
    ) -> RendererResolution | None: ...

    async def subscribe(
        self, subscriber: ServiceRef[Subscriber[EntityRef[ReloadEverythingNotification]]]
    ) -> ServiceRef[UnsubscribeHandle]:
        """Subscribe ``subscriber`` to renderer-table changes.

        Re-declaration of the inherited
        :meth:`fxtr2.services.subscription_types.EventSource.subscribe`
        purely for this docstring (same annotations, same wire shape).

        The dispatcher delivers a
        :class:`ReloadEverythingNotification` ref to the subscriber's
        ``on_event`` on every table change, coalescing (every
        notification is identical, so skip-ahead latest-value delivery
        is exactly the old semantics). The frontend's
        ``DynamicDispatcher`` reacts by dropping its per-type session
        caches and re-resolving every entry.

        The standard subscription contract applies: the caller holds
        the returned unsubscribe handle and invokes it when done
        (frontends route this through their workstation inbox's
        ``subscribe_to``, which owns the lifecycle); the dispatcher
        additionally drops a subscription whose ``on_event`` raises
        (crash backstop). Subscriptions are not deduplicated — a
        duplicate subscribe just duplicates deliveries.
        """
        ...


@service_protocol
class NixDispatcherProto(WorkstationDispatcherProto, Protocol):
    """:class:`WorkstationDispatcherProto` plus nix/git preset loading.

    Only the nix/git impl (``NixConfigurableDispatchImpl``) exposes this;
    the frontend + other resolution-only clients target the base
    :class:`WorkstationDispatcherProto` (so the frontend needs no work).

    A genuine subclass: ``resolve`` / ``subscribe`` are inherited (the
    base is marked ``allow_protocol_subtypes=True``); this protocol
    adds only ``set_preset``. Wire dispatch is by method name, so the
    impl (which emits ``WORKSTATION_DISPATCHER_TYPE_ID``) serves both
    protocols, and the agent reaches ``set_preset`` via
    ``replace_service_type(NixDispatcherProto)`` on the looked-up ref.
    See ``design_notes/06_ui_presets.md``.
    """

    async def set_preset(
        self,
        git_commit: str,
        flake_attr: str,
        flake_path: str | None = None,
    ) -> None:
        """Load (or reload) the preset at ``(git_commit, flake_attr,
        flake_path)`` against the dispatcher's configured repo.

        Evals the routing table, eagerly builds + validates each entry
        (renderer ESMs minted into ``RendererEntity``s, ``typeEntity``
        id checked against the type-key), atomically swaps the resolution
        table, and broadcasts a reload to every subscribed listener.

        Replace-not-merge: each call rebuilds the table from the named
        preset attr. Raises with the nix/git stderr in the message on any
        failure (bad ref / eval / build / a key↔``typeEntity`` mismatch),
        surfacing to the agent as an ``invoke_error``.
        """
        ...
