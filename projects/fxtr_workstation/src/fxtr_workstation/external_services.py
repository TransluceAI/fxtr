"""Stage 7 post-1: user-supplied external services.

The workstation can be configured at construction time with a list
of :class:`ExternalServiceConfig`s alongside the stage-7
``persistence_namespaces``. For each entry, the workstation:

1. Bridges the user-supplied :class:`ServiceRef` from the user's
   own :class:`FxtrScope` into ``state.root_scope``. The bridge
   pins the user's scope to ``root_scope``'s lifetime (i.e. the
   workstation's lifetime) — the user's outer scope CM can't
   drain until the workstation's ``__aexit__`` releases
   ``root_scope``.
2. Mints a :class:`TrackedServiceRecord` via
   :func:`get_or_create_service_record`, which spawns the
   standard ``service_record_worker`` to bridge from
   ``root_scope`` into a per-record ``service_scope``.
3. Installs a :class:`SystemSource`-sourced :class:`Registration`
   on the record under the user's ``external_name`` so callers
   can reach it via ``get_service_by_external_name``.

Why no per-config worker (unlike the persistence cap)? The
persistence cap owns an ``impl_scope`` where it mounts the impl,
and that scope has to live for the workstation's duration — a
worker is the natural way to own a scope's lifetime. External
services have no per-config scope to own: the user already owns
the source scope, and the only workstation-side lifetime claim is
the bridge installed on ``root_scope`` (which ``root_scope``'s own
drain releases automatically). So bring-up here is just sequential
inline async work at startup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef

from .state import (
    Registration,
    SystemSource,
    get_or_create_service_record,
    maybe_release_service_record,
)

if TYPE_CHECKING:
    from .state import WorkstationState

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExternalServiceConfig:
    """Per-service external mount configuration.

    Passed to :class:`WorkstationServer` as a list. Each entry
    bridges one user-supplied service into the workstation's
    ``root_scope`` at startup and exposes it under
    ``external_name``.

    Attributes:
        external_name: Permanent external service name. Must be
            unique across all external + persistence configs in a
            single workstation, and across any RPC peer
            registrations (the external-name namespace is shared
            with stage-1's RPC-peer registrations).
        service: The :class:`ServiceRef` the workstation should
            expose. Reachable in ``scope`` (registered there or in
            an ancestor). The workstation does not own this ref —
            the user is responsible for keeping the source
            available until they're ready to shut down.
        scope: A scope where ``service`` is reachable. Doesn't
            need to be mountable. The workstation pins this scope
            to ``root_scope``'s lifetime via
            :meth:`FxtrScope.bridge_service_from_scope`.
    """

    external_name: str
    service: ServiceRef[Any]
    scope: FxtrScope


async def attach_external_service(state: WorkstationState, config: ExternalServiceConfig) -> None:
    """Bring up one external service on ``state``.

    Called by :class:`WorkstationServer` once per
    :class:`ExternalServiceConfig` during startup, after
    ``state`` is constructed and ``state.background_tg`` is
    running. Steps:

    1. Bridge ``config.service`` from ``config.scope`` into
       ``state.root_scope`` via
       :meth:`FxtrScope.bridge_service_from_scope`. Pins
       ``config.scope`` to ``root_scope`` for the workstation's
       lifetime; bridge errors propagate to the caller (which is
       mid-``__aenter__``) so the unwind drains ``root_scope``
       and any earlier bridges release.
    2. Inside :meth:`WorkstationState.mutation_context`, mint a
       :class:`TrackedServiceRecord` via
       :func:`get_or_create_service_record`. The standard
       ``service_record_worker`` spawned there does the
       ``root_scope`` → ``service_scope`` bridge.
    3. ``await tracked.bridge_ready.wait()``; on bridge error,
       release the just-minted record via
       :func:`maybe_release_service_record` and re-raise.
    4. Install a :class:`SystemSource`-sourced
       :class:`Registration` (``kind="external"``,
       ``metadata={"external_name": ...}``) under
       ``config.external_name``. The same defensive
       "external_name already taken" check the persistence
       worker does — startup-only race against an RPC peer; on
       conflict, release the just-minted record and raise.

    On shutdown, the standard sweep in
    ``WorkstationServer.__aexit__`` releases this registration
    alongside every other ``SystemSource``-sourced one; the
    underlying ``service_record_worker`` wakes from its
    ``shutdown_event`` and lets ``service_scope`` drain. There's
    no explicit "unbridge" step — the bridge on ``root_scope``
    releases when ``root_scope`` itself drains.
    """
    await state.root_scope.bridge_service_from_scope(
        config.service, config.scope, keepalive_via="independent_lease"
    )

    with state.mutation_context():
        tracked = get_or_create_service_record(state, config.service, state.root_scope)
    await tracked.bridge_ready.wait()
    if tracked.bridge_error is not None:
        _log.error(
            "external service bridge failed for %r: %r",
            config.external_name,
            tracked.bridge_error,
        )
        with state.mutation_context():
            maybe_release_service_record(state, tracked)
        raise tracked.bridge_error

    registration: Registration | None = None
    try:
        with state.mutation_context():
            ext = config.external_name
            if ext in state.service_external_names:
                raise ValueError(
                    f"external service external name {ext!r} is already "
                    "registered (duplicate config or RPC-peer race?)"
                )
            state.touch("services", tracked.short_id)
            registration = Registration(
                service_key=tracked.service_key,
                source=SystemSource(
                    kind="external",
                    metadata={"external_name": ext},
                ),
                external_name=ext,
            )
            tracked.registrations.append(registration)
            state.service_external_names[ext] = registration
    except BaseException:
        with state.mutation_context():
            if registration is not None:
                if registration in tracked.registrations:
                    tracked.registrations.remove(registration)
            state.touch("services", tracked.short_id)
            maybe_release_service_record(state, tracked)
        raise
