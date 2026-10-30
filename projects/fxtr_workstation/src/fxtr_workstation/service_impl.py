"""Per-RPC-connection :class:`WorkstationServiceProto` impl plus the
companion :class:`ReleaseCallbackImpl`.

One :class:`WorkstationServiceImpl` is mounted per ``/rpc`` connection
on the connection's ``record.scope``. The impl keeps back-references
to the :class:`WorkstationState` and to its own
:class:`RPCConnectionRecord` so methods can attribute work to the
calling connection.

**Lifetime story for services.** Each tracked service has a
workstation-owned ``service_scope`` managed by a worker task on
``state.background_tg`` (see :func:`service_record_worker`).
``register_service`` spawns the worker on first registration and
awaits ``bridge_ready`` so the bridge into ``service_scope`` is
established before returning. ``get_service`` then bridges from
``service_scope`` (a workstation-owned, stable identity) into the
requester's call scope. Releasing the last registration sets
``shutdown_event``; the worker exits and ``service_scope`` drains.

The bridge into ``service_scope`` is "Independent" (per
``bridge_service_from_scope``'s docs), so the registering call's
dispatch scope can drain independently — that drain is what fires
the on_end callback that releases the registration.

See ``v2/notes/scope_discipline.md``: the only active-scope use here
is the documented exception inside service-method dispatch
(``get_active_scope()`` to reach the per-call dispatch scope).
"""

from __future__ import annotations

from typing import Any

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef, get_active_scope
from fxtr2.services.service_dialect import OverrideThisTypeBeforeUsing
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method

from .protocols import (
    RELEASE_CALLBACK_TYPE_ID,
    WORKSTATION_SERVICE_TYPE_ID,
    ReleaseCallbackProto,
    WorkstationServiceProto,
)
from .state import (
    EntityRegistration,
    Registration,
    RPCConnectionRecord,
    TrackedServiceRecord,
    WorkstationState,
    _ensure_entity_record_via_copy,
    _maybe_release_entity_record,
    describe_registration_source,
    get_or_create_service_record,
    maybe_release_service_record,
    provenanced_key_for,
    release_entity_registration,
    release_registration,
)


class ReleaseCallbackImpl(TypedServiceImpl[ReleaseCallbackProto]):
    """Per-registration release-callback ref.

    Mounted on the dispatch scope of the ``register_service`` /
    ``copy_entity`` call that produced it. Auto-release is wired via
    an end-callback on the dispatch scope's lifetime token; explicit
    ``release()`` is the fast-path. Both funnel through the
    appropriate idempotent ``release_*registration`` helper.

    Exactly one of ``registration`` / ``entity_registration`` should
    be provided, identifying which kind of release this callback owns.
    """

    def __init__(
        self,
        state: WorkstationState,
        *,
        registration: Registration | None = None,
        entity_registration: EntityRegistration | None = None,
    ) -> None:
        if (registration is None) == (entity_registration is None):
            raise ValueError(
                "ReleaseCallbackImpl requires exactly one of "
                "`registration` or `entity_registration`."
            )
        self._state = state
        self._registration = registration
        self._entity_registration = entity_registration

    def _target_type_annotation(self) -> Any:
        return ReleaseCallbackProto

    def _service_type_id(self) -> FxtrID:
        return RELEASE_CALLBACK_TYPE_ID

    @service_method
    async def release(self) -> None:
        if self._registration is not None:
            release_registration(self._state, self._registration)
        else:
            assert self._entity_registration is not None
            release_entity_registration(self._state, self._entity_registration)


class WorkstationServiceImpl(TypedServiceImpl[WorkstationServiceProto]):
    def __init__(
        self,
        state: WorkstationState,
        record: RPCConnectionRecord,
    ) -> None:
        self._state = state
        self._record = record

    def _target_type_annotation(self) -> Any:
        return WorkstationServiceProto

    def _service_type_id(self) -> FxtrID:
        return WORKSTATION_SERVICE_TYPE_ID

    @service_method
    async def name_connection(self, name: str) -> None:
        with self._state.mutation_context():
            self._state.touch("rpc_connections", str(self._record.connection_id))
            self._record.name = name

    @service_method
    async def register_service(
        self, external_name: str, service: ServiceRef[Any]
    ) -> tuple[str, ServiceRef[ReleaseCallbackProto]]:
        # Active scope: the per-call dispatch scope. The typed-service
        # framework decoded ``service`` here, so ``service`` is loadable
        # in this scope. See ``scope_discipline.md`` — using the active
        # scope inside a service method is the documented OK case.
        call_scope = get_active_scope()

        # Sync get-or-create + worker-spawn-on-first-creation. The
        # helper opens its own ``mutation_context`` if a fresh record
        # is minted (touches the ``services`` resource for the
        # add); we wrap the *outer* registration-list mutation in
        # our own context so the post-bridge synchronous block emits
        # one ``resource_changes`` per resource per subscriber. Two
        # contexts is fine — they're separated by the
        # ``await bridge_ready.wait()``.
        with self._state.mutation_context():
            tracked = get_or_create_service_record(self._state, service, call_scope)
        service_key = tracked.service_key
        short_id = tracked.short_id

        registration: Registration | None = None
        try:
            await tracked.bridge_ready.wait()
            if tracked.bridge_error is not None:
                raise tracked.bridge_error

            # Synchronous from here through `register_end_callback`:
            # check name conflict, construct, append to both lists,
            # mount, register the end-callback. No await means no
            # cancellation can split the registration's "appended but
            # not callback-wired" state.
            with self._state.mutation_context():
                existing = self._state.service_external_names.get(external_name)
                if existing is not None:
                    raise ValueError(
                        f"external name {external_name!r} is already registered "
                        f"by {describe_registration_source(existing.source)}. "
                        f"Release the existing registration before re-registering, "
                        f"or pick a different external name."
                    )
                # External names share a single namespace across
                # services and entities (see protocol docstring). A
                # collision with an active entity registration is
                # rejected here just like a same-kind collision.
                existing_entity = self._state.entity_external_names.get(external_name)
                if existing_entity is not None and not existing_entity.released:
                    raise ValueError(
                        f"external name {external_name!r} is already registered "
                        f"as an entity by {describe_registration_source(existing_entity.source)}. "
                        f"Release the existing registration before re-registering, "
                        f"or pick a different external name."
                    )
                # The services list and the source connection's
                # service_registration_short_ids both change as a
                # result of the register-and-append below.
                self._state.touch("services", short_id)
                self._state.touch("rpc_connections", str(self._record.connection_id))
                registration = Registration(
                    service_key=service_key,
                    source=self._record,
                    external_name=external_name,
                )
                tracked.registrations.append(registration)
                self._record.registrations.append(registration)
                self._state.service_external_names[external_name] = registration

            cb_impl = ReleaseCallbackImpl(self._state, registration=registration)
            cb_ref = await call_scope.mount(cb_impl)

            # Auto-release when the caller drops the chain-bridged
            # release-callback ref: dispatch scope drains, end
            # callback fires, registration is released. Idempotent
            # with explicit ``release()`` and with the disconnect
            # sweep in ``add_rpc_connection``. See
            # ``notes/service_lifecycle.md`` for the cycle caveat.
            call_scope.scope_token.register_end_callback(
                lambda: release_registration(self._state, registration)
            )
            return short_id, cb_ref
        except BaseException:
            with self._state.mutation_context():
                if registration is not None:
                    # We attached a Registration before something failed
                    # (mount, end-callback wiring, or external
                    # cancellation). Roll it back so we don't leak.
                    release_registration(self._state, registration)
                else:
                    # No Registration was attached. If the record has no
                    # other holders (no other registrations, no tracking
                    # sessions) we should pop it so we don't leak a stuck
                    # entry. The conservative check covers two distinct
                    # cases:
                    #   - We just created the record via
                    #     ``get_or_create_service_record`` and the bridge
                    #     failed / await was cancelled before we attached.
                    #   - The record existed before our call but every
                    #     prior holder released between us looking it up
                    #     and reaching this rollback (rare).
                    # ``maybe_release_service_record`` does exactly this
                    # check; reusing it keeps the logic in one place.
                    self._state.touch("services", tracked.short_id)
                    maybe_release_service_record(self._state, tracked)
            raise

    @service_method
    async def get_service(self, service_short_id: str) -> ServiceRef[OverrideThisTypeBeforeUsing]:
        service_key = self._state.short_ids.lookup_active_service(service_short_id)
        if service_key is None:
            raise ValueError(f"unknown or inactive service short id {service_short_id!r}")
        tracked = self._state.service_records[service_key]
        return await self._bridge_tracked_into_call_scope(tracked, service_short_id)

    @service_method
    async def get_service_by_external_name(
        self, external_name: str
    ) -> ServiceRef[OverrideThisTypeBeforeUsing]:
        registration = self._state.service_external_names.get(external_name)
        if registration is None or registration.released:
            raise ValueError(f"unknown external service name {external_name!r}")
        tracked = self._state.service_records[registration.service_key]
        return await self._bridge_tracked_into_call_scope(tracked, external_name)

    async def _bridge_tracked_into_call_scope(
        self, tracked: TrackedServiceRecord, lookup_label: str
    ) -> ServiceRef[OverrideThisTypeBeforeUsing]:
        """Common tail of ``get_service`` / ``get_service_by_external_name``.

        Awaits ``bridge_ready``, surfaces ``bridge_error``, then
        bridges the workstation's ``service_scope`` ref into the
        active call scope and returns it for chain-bridging out.
        """
        call_scope = get_active_scope()

        # The record was made active in ``register_service`` *before*
        # awaiting ``bridge_ready``, so we may have raced it. Block on
        # ``bridge_ready`` rather than failing — the lookup caller
        # gets an answer once the bridge is up (or the bridge fails,
        # in which case we surface that error here too).
        await tracked.bridge_ready.wait()
        if tracked.bridge_error is not None:
            raise ValueError(f"service {lookup_label!r} failed to bridge") from tracked.bridge_error
        service_scope = tracked.service_scope
        assert service_scope is not None  # bridge_ready ⇒ service_scope set

        # Reconstruct the ServiceRef from the ServiceKey. The
        # ``target_type`` annotation we hand back is
        # ``OverrideThisTypeBeforeUsing`` per the protocol; the wire
        # carries (handle, service_type_id) and the caller is
        # expected to ``replace_service_type`` before resolving.
        ref = ServiceRef(
            handle=tracked.service_key.handle,
            target_type=OverrideThisTypeBeforeUsing,
            service_type_id=tracked.service_key.type_id,
        )
        # Bridge from service_scope into call_scope; the framework
        # chain-bridges the return value out.
        await call_scope.bridge_service_from_scope(
            ref, service_scope, keepalive_via="independent_lease"
        )
        return ref

    # ---------- Entities ------------------------------------------

    @service_method
    async def copy_entity(
        self, external_name: str, entity: FxtrID
    ) -> tuple[str, ServiceRef[ReleaseCallbackProto]]:
        # Active scope: the per-call dispatch scope. The typed-service
        # framework decoded ``entity`` into this scope, so it's
        # loadable here.
        call_scope = get_active_scope()

        # Fast-fail name check before the (potentially slow) copy.
        # Purely an optimization — a concurrent ``copy_entity`` /
        # ``register_service`` could claim the name between this
        # check and the authoritative one below, in which case
        # we'd hit the slow path and fail the second check. External
        # names share a single namespace across entities and services,
        # so this check looks in both tables.
        if external_name in self._state.entity_external_names:
            existing = self._state.entity_external_names[external_name]
            raise ValueError(
                f"external name {external_name!r} is already "
                f"registered as an entity by {describe_registration_source(existing.source)}. "
                f"Release the existing registration before re-registering, "
                f"or pick a different external name."
            )
        existing_service = self._state.service_external_names.get(external_name)
        if existing_service is not None and not existing_service.released:
            raise ValueError(
                f"external name {external_name!r} is already "
                f"registered as a service by {describe_registration_source(existing_service.source)}. "
                f"Release the existing registration before re-registering, "
                f"or pick a different external name."
            )

        # Get-or-create the per-record :class:`TrackedEntityRecord`,
        # copying from ``call_scope`` into a fresh per-record scope on
        # first mint. Async: the copy walks the subgraph and may take
        # time. The helper handles its own race-checked install +
        # ``mutation_context``.
        entity_key = provenanced_key_for(entity)
        tracked = await _ensure_entity_record_via_copy(self._state, entity, call_scope)
        short_id = tracked.short_id

        registration: EntityRegistration | None = None
        try:
            # Synchronous from here through ``register_end_callback``:
            # name-conflict check, construct, append, mount, wire end
            # callback. No await means no cancellation can split this.
            with self._state.mutation_context():
                existing = self._state.entity_external_names.get(external_name)
                if existing is not None:
                    raise ValueError(
                        f"external name {external_name!r} is already "
                        f"registered as an entity by {describe_registration_source(existing.source)}. "
                        f"Release the existing registration before re-registering, "
                        f"or pick a different external name."
                    )
                existing_service = self._state.service_external_names.get(external_name)
                if existing_service is not None and not existing_service.released:
                    raise ValueError(
                        f"external name {external_name!r} is already "
                        f"registered as a service by {describe_registration_source(existing_service.source)}. "
                        f"Release the existing registration before re-registering, "
                        f"or pick a different external name."
                    )
                # The entities list and the source connection's
                # entity_registration_short_ids both change as a result
                # of the registration append below.
                self._state.touch("entities", short_id)
                self._state.touch("rpc_connections", str(self._record.connection_id))
                registration = EntityRegistration(
                    fxtr_id=entity,
                    source=self._record,
                    external_name=external_name,
                )
                tracked.registrations.append(registration)
                self._record.entity_registrations.append(registration)
                self._state.entity_external_names[external_name] = registration

            cb_impl = ReleaseCallbackImpl(
                self._state, registration=None, entity_registration=registration
            )
            cb_ref = await call_scope.mount(cb_impl)

            call_scope.scope_token.register_end_callback(
                lambda: release_entity_registration(self._state, registration)
            )
            return short_id, cb_ref
        except BaseException:
            with self._state.mutation_context():
                if registration is not None:
                    release_entity_registration(self._state, registration)
                else:
                    # Setup failed before any registration was attached.
                    # If no concurrent ``copy_entity`` snuck a registration
                    # in, drop the bare record we created (which drains
                    # its per-record scope). ``_maybe_release_entity_record``
                    # short-circuits if other holders are now present.
                    if self._state.entity_records.get(entity_key) is tracked:
                        self._state.touch("entities", short_id)
                        _maybe_release_entity_record(self._state, tracked)
            raise

    @service_method
    async def get_entity(self, entity_short_id: str) -> FxtrID:
        fxtr_id = self._state.short_ids.lookup_active_entity(entity_short_id)
        if fxtr_id is None:
            raise ValueError(f"unknown or inactive entity short id {entity_short_id!r}")
        record = self._state.entity_records.get(provenanced_key_for(fxtr_id))
        if record is None:
            raise ValueError(f"unknown or inactive entity short id {entity_short_id!r}")
        # Per-record scope model: each record has its own scope; the
        # connection scope no longer includes a global entities
        # umbrella. ``track_from_scope`` extends a lifetime claim on
        # the layered FxtrID into ``call_scope`` (sync, same-store, no
        # byte copy — just lifetime-claim bookkeeping). The framework
        # then chain-bridges the FxtrID out via ``call_scope`` as
        # before.
        call_scope = get_active_scope()
        call_scope.track_from_scope(record.fxtr_id, source_scope=record.scope)
        return record.fxtr_id

    @service_method
    async def get_entity_by_external_name(self, external_name: str) -> FxtrID:
        registration = self._state.entity_external_names.get(external_name)
        if registration is None or registration.released:
            raise ValueError(f"unknown external entity name {external_name!r}")
        record = self._state.entity_records.get(provenanced_key_for(registration.fxtr_id))
        if record is None:
            raise ValueError(f"unknown external entity name {external_name!r}")
        # Same bridging story as ``get_entity`` above.
        call_scope = get_active_scope()
        call_scope.track_from_scope(record.fxtr_id, source_scope=record.scope)
        return record.fxtr_id

    @service_method
    async def lookup_short_id_by_external_name(self, external_name: str) -> str:
        service_registration = self._state.service_external_names.get(external_name)
        if service_registration is not None and not service_registration.released:
            tracked = self._state.service_records.get(service_registration.service_key)
            if tracked is not None:
                return tracked.short_id
        entity_registration = self._state.entity_external_names.get(external_name)
        if entity_registration is not None and not entity_registration.released:
            record = self._state.entity_records.get(
                provenanced_key_for(entity_registration.fxtr_id)
            )
            if record is not None:
                return record.short_id
        raise ValueError(f"unknown external name {external_name!r}")
