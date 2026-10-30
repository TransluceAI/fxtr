"""Stage-7 persistence caps.

The workstation can be configured with one or more
:class:`PersistenceNamespaceConfig`s at construction time. For each
config, :func:`persistence_cap_worker` mounts a typed service that
speaks :class:`PersistenceProto` and forwards ``pin`` / ``retrieve`` /
etc. to the underlying ``FxtrNamespace``. Each cap has its own
``s{N}`` short ID and a permanent external service name; sessions and
RPC peers reach it via the existing ``get_service`` /
``get_service_by_external_name`` paths.

Two impls share the same protocol but carry distinct
``service_type_id``s:

- :class:`NoProvPersistenceImpl` — defers everything to the
  underlying namespace; provenance information (if any) is silently
  dropped on pin and absent on retrieve.
- :class:`PreserveProvPersistenceImpl` — stage-7b ships a
  :class:`NotImplementedError`-stub; stage-7c implements the real
  pin/retrieve bodies (lift bare data to a u-tag prov node before
  pinning; restore via ``embed_as_provenance_parent`` on retrieve;
  reject retrieve of non-prov-tagged payloads).

Lifetime: the namespace is opened externally by the user. The
workstation includes each ``ns.scope`` in its ``root_scope.include``
so retrieved entities can be lazily loaded through the standard
inheritance chain. The cap itself does **not** mint workstation
entity records — the result FxtrID flows through the existing
invoke-result delivery (for sessions) or the chain-bridge return
path (for non-session RPC peers).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import anyio
import anyio.abc
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope, get_active_scope
from fxtr2.data.fxtr_context_api import FxtrNamespace
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method

from .protocols import (
    PERSISTENCE_NO_PROV_TYPE_ID,
    PERSISTENCE_PRESERVE_PROV_TYPE_ID,
    PersistenceProto,
)
from .state import (
    Registration,
    SystemSource,
    get_or_create_service_record,
    maybe_release_service_record,
)

if TYPE_CHECKING:
    from .state import WorkstationState

_log = logging.getLogger(__name__)


PersistenceMode = Literal["no_provenance", "preserve_provenance"]


@dataclass(frozen=True)
class PersistenceNamespaceConfig:
    """Per-namespace persistence configuration.

    Passed to :class:`WorkstationServer` as a list. Each entry mounts
    one persistence cap on workstation startup. The user is
    responsible for opening the underlying ``FxtrNamespace`` (e.g.
    via ``async with SQLiteFxtrNamespace(...) as ns``) before the
    workstation server starts and keeping it open for the
    workstation's lifetime.

    Attributes:
        external_name: Permanent external service name. The cap is
            registered under this name so callers can find it via
            ``get_service_by_external_name``. Must be unique across
            all configs in a single workstation, and across any RPC
            peer registrations (external-name namespace is shared).
        namespace: An already-opened ``FxtrNamespace``. Its
            ``.scope`` is added to the workstation's
            ``root_scope.include`` so retrievals can lift via
            inheritance.
        mode: ``"no_provenance"`` (default) or ``"preserve_provenance"``.
            Stage-7b implements only the no-provenance path; stage-7c
            adds the preserve-provenance impl.
    """

    external_name: str
    namespace: FxtrNamespace
    mode: PersistenceMode = "no_provenance"


class NoProvPersistenceImpl(TypedServiceImpl[PersistenceProto]):
    """Persistence cap impl for ``mode="no_provenance"``.

    Defers every method to the underlying ``FxtrNamespace``. Pinning
    a layered FxtrID drops the provenance half (the namespace
    already strips ``_provenance_parent`` internally); retrieve
    returns whatever bare FxtrID the namespace stored.

    The active scope captured by each method is the per-call
    ``call_scope`` opened by the RPC layer for this method dispatch.
    The framework decoded inputs into that scope, and outputs are
    chain-bridged back via it. The namespace's ``pin(scope=...)`` and
    ``retrieve(into_scope=..., copy_into_scope=False)`` calls thread
    that ``call_scope`` through; lifting works because the
    workstation arranged for ``call_scope``'s inheritance chain to
    include ``namespace.scope``.
    """

    def __init__(self, state: WorkstationState, namespace: FxtrNamespace) -> None:
        self._state = state
        self._namespace = namespace

    def _target_type_annotation(self) -> Any:
        return PersistenceProto

    def _service_type_id(self) -> FxtrID:
        return PERSISTENCE_NO_PROV_TYPE_ID

    @service_method
    async def pin(self, key: str, entity: FxtrID, *, overwrite: bool = False) -> None:
        call_scope = get_active_scope()
        await self._namespace.pin(key, entity, overwrite=overwrite, scope=call_scope)

    @service_method
    async def unpin(self, key: str) -> None:
        await self._namespace.unpin(key)

    @service_method
    async def retrieve(self, key: str) -> FxtrID:
        call_scope = get_active_scope()
        # ``copy_into_scope=False`` (default) ⇒ lift the result into
        # ``call_scope`` via inheritance. Requires ``call_scope`` to
        # be a descendant of ``namespace.scope``; the workstation
        # guarantees this by listing each namespace's scope in
        # ``root_scope.include`` (and ``call_scope`` inherits from
        # the cap's ``service_scope`` which inherits from
        # ``root_scope``).
        return await self._namespace.retrieve(key, into_scope=call_scope)

    @service_method
    async def try_retrieve(self, key: str) -> FxtrID | None:
        call_scope = get_active_scope()
        return await self._namespace.try_retrieve(key, into_scope=call_scope)

    @service_method
    async def list_pins(self, prefix: str = "") -> list[str]:
        return list((await self._namespace.list_pins(prefix)).keys())


_PROVENANCE_NODE_TAGS = frozenset({"n", "c", "p", "u"})


def _layer_for_storage(call_scope: FxtrScope, entity: FxtrID) -> FxtrID:
    """Project ``entity`` to the bare provenance form to pin.

    - Layered input ``(data, prov)``: returns the bare prov half.
    - Bare input (any tag, no ``_provenance_parent``): wraps to
      ``u(entity)``.

    Both cases go through the single
    :meth:`FxtrScope.extract_provenance_parent` primitive (with
    ``extract_as_u_if_missing=True``). The branch is purely on the
    layering, never on the tag — a bare ``n``/``c``/``p``/``u`` node
    is wrapped to ``u(node)`` just like a bare ``d``.
    """
    return call_scope.extract_provenance_parent(entity, extract_as_u_if_missing=True)


def _restore_layered(call_scope: FxtrScope, prov_node: FxtrID, key: str) -> FxtrID:
    """Re-attach ``prov_node`` as the prov-parent of its unwrapped
    data half, returning the layered FxtrID. Validates the tag.

    Used on the retrieve side of preserve-prov mode.

    Relies on the documented invariant of
    :meth:`FxtrScope.embed_as_provenance_parent` and the underlying
    ``_adopt_inheritance_entry`` cascade: lifting a provenance node
    from a foreign arena pulls the unwrapped data half along via the
    ``unwraps_to`` propagation, leaving both halves tracked in this
    scope — so ``embed`` finds them and the layered form round-trips
    (including cross-process / restart-durability, where the namespace
    is the only source of the data half).
    """
    tag = prov_node.get_tag()
    if tag not in _PROVENANCE_NODE_TAGS:
        raise ValueError(
            f"persistence cap (preserve_provenance): pin {key!r} resolves "
            f"to {tag!r}-tag, not a provenance node. The cap's invariant "
            "is that every pin is a provenance node — was this namespace "
            "previously written by a no_provenance cap?"
        )
    return call_scope.embed_as_provenance_parent(prov_node)


class PreserveProvPersistenceImpl(TypedServiceImpl[PersistenceProto]):
    """Persistence cap impl for ``mode="preserve_provenance"``.

    Pin pathway:

    - Layered ``(data, prov)``: extract the prov half and pin the
      bare prov node directly.
    - Bare id (any tag, no ``_provenance_parent``): wrap to a
      ``u``-tagged prov node and pin that. Restoring the layered
      form on retrieve produces ``(data, u(data))`` (or ``(node,
      u(node))`` for a bare prov-tag input).

    Both go through :func:`_layer_for_storage`, i.e. a single
    ``call_scope.extract_provenance_parent(.., extract_as_u_if_missing=True)``
    call — the branch is on the layering, never on the tag.

    Retrieve pathway: the namespace stores a bare prov node;
    ``call_scope.embed_as_provenance_parent`` re-attaches the prov
    parent so the caller gets a layered ``(data, prov)`` FxtrID.
    Pins resolving to non-prov-tagged payloads are rejected — they
    indicate the namespace was previously written by a no-prov cap.

    ``unpin`` and ``list_pins`` are mode-agnostic and pass through
    unchanged.
    """

    def __init__(self, state: WorkstationState, namespace: FxtrNamespace) -> None:
        self._state = state
        self._namespace = namespace

    def _target_type_annotation(self) -> Any:
        return PersistenceProto

    def _service_type_id(self) -> FxtrID:
        return PERSISTENCE_PRESERVE_PROV_TYPE_ID

    @service_method
    async def pin(self, key: str, entity: FxtrID, *, overwrite: bool = False) -> None:
        call_scope = get_active_scope()
        prov_form = _layer_for_storage(call_scope, entity)
        await self._namespace.pin(key, prov_form, overwrite=overwrite, scope=call_scope)

    @service_method
    async def unpin(self, key: str) -> None:
        await self._namespace.unpin(key)

    @service_method
    async def retrieve(self, key: str) -> FxtrID:
        call_scope = get_active_scope()
        bare = await self._namespace.retrieve(key, into_scope=call_scope)
        return _restore_layered(call_scope, bare, key)

    @service_method
    async def try_retrieve(self, key: str) -> FxtrID | None:
        call_scope = get_active_scope()
        bare = await self._namespace.try_retrieve(key, into_scope=call_scope)
        if bare is None:
            return None
        return _restore_layered(call_scope, bare, key)

    @service_method
    async def list_pins(self, prefix: str = "") -> list[str]:
        return list((await self._namespace.list_pins(prefix)).keys())


def _build_persistence_impl(
    state: WorkstationState, config: PersistenceNamespaceConfig
) -> NoProvPersistenceImpl | PreserveProvPersistenceImpl:
    """Return the impl class matching ``config.mode``."""
    if config.mode == "no_provenance":
        return NoProvPersistenceImpl(state, config.namespace)
    if config.mode == "preserve_provenance":
        return PreserveProvPersistenceImpl(state, config.namespace)
    # Defensive — `Literal` should make this unreachable, but a
    # caller passing a freshly-constructed `PersistenceNamespaceConfig`
    # via an untyped path could reach here.
    raise ValueError(f"unknown persistence mode {config.mode!r}")


async def persistence_cap_worker(
    state: WorkstationState,
    config: PersistenceNamespaceConfig,
    *,
    task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED,
) -> None:
    """Background task per persistence cap. Owns ``impl_scope`` and
    parks for the workstation's lifetime.

    Started via ``await background_tg.start(...)`` so the caller
    (``WorkstationServer.__asynccontextmanager__``) blocks until the
    cap is fully ready: bridge bridged, ``Registration`` installed
    in ``state.service_external_names`` under ``config.external_name``.

    Setup steps (mirroring :func:`callback_record_worker`):

    1. Open an isolated mountable ``impl_scope`` with
       ``include=[state.root_scope]``. ``root_scope`` already
       includes the namespace's scope, so the impl can call into
       the namespace through inheritance.
    2. Mount the appropriate impl class on ``impl_scope``.
    3. ``get_or_create_service_record`` mints the underlying
       :class:`TrackedServiceRecord` and spawns its
       ``service_record_worker`` to bridge the impl ref into
       ``service_scope``.
    4. Await ``bridge_ready``; on bridge failure, release the
       just-minted record and re-raise so ``task_group.start(...)``
       propagates the error to the bring-up caller.
    5. Build a :class:`Registration` with a :class:`SystemSource`
       source and install it in
       ``state.service_external_names[config.external_name]``.
       External-name conflicts raise (caller is the server
       bring-up, so this surfaces as a startup error).
    6. Signal readiness via ``task_status.started()``.
    7. Park on ``tracked.shutdown_event`` until shutdown releases
       the registration; then let ``impl_scope`` drain.

    Shutdown ordering: ``WorkstationServer``'s ``__aexit__`` walks
    every active service record and releases each ``SystemSource``-
    sourced registration via :func:`release_registration`. That
    fires ``shutdown_event``; this worker wakes, exits the
    ``async with`` block, and ``impl_scope`` drains. The bridge in
    ``service_scope`` releases its lease on ``impl_scope`` as part
    of ``service_scope``'s own drain (driven by the same
    ``shutdown_event`` on the parallel ``service_record_worker``).
    """
    async with FxtrScope(
        isolated=True,
        include=[state.root_scope],
        name=f"persistence_cap/{config.external_name}",
    ) as impl_scope:
        impl = _build_persistence_impl(state, config)
        impl_ref = await impl_scope.mount(impl)
        with state.mutation_context():
            tracked = get_or_create_service_record(state, impl_ref, impl_scope)
        await tracked.bridge_ready.wait()
        if tracked.bridge_error is not None:
            _log.error(
                "persistence cap bridge failed for %r: %r",
                config.external_name,
                tracked.bridge_error,
            )
            with state.mutation_context():
                maybe_release_service_record(state, tracked)
            raise tracked.bridge_error

        # Install the SystemSource-sourced Registration. External
        # name uniqueness is enforced; if it's already taken (by a
        # peer race or duplicate config), we roll back and raise.
        registration: Registration | None = None
        try:
            with state.mutation_context():
                ext = config.external_name
                if ext in state.service_external_names:
                    raise ValueError(
                        f"persistence cap external name {ext!r} is already "
                        "registered (duplicate config or RPC-peer race?)"
                    )
                state.touch("services", tracked.short_id)
                registration = Registration(
                    service_key=tracked.service_key,
                    source=SystemSource(
                        kind="persistence",
                        metadata={
                            "external_name": ext,
                            "mode": config.mode,
                        },
                    ),
                    external_name=ext,
                )
                tracked.registrations.append(registration)
                state.service_external_names[ext] = registration
        except BaseException:
            # Bring-up failed after the bridge — release the record
            # we just minted so we don't leak a stuck entry.
            with state.mutation_context():
                if registration is not None:
                    # Defensive — shouldn't have appended yet on the
                    # error path, but be safe.
                    if registration in tracked.registrations:
                        tracked.registrations.remove(registration)
                state.touch("services", tracked.short_id)
                maybe_release_service_record(state, tracked)
            raise

        task_status.started()
        await tracked.shutdown_event.wait()
