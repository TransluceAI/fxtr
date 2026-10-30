"""The runner's binding authority: what serves a handle key, and how.

:class:`BindingHost` is the one object behind every binding question in a
runner (`notes/sandboxed_bindings.md`): it holds the **ephemeral** class
(live connector services registered per key, with pinned dependencies and
config on the in-memory entry), consults the **sandboxed** class (durable
records owned by the session — `state_store.py`; this module only reads
them), owns the sandbox runtime state (the per-binding process cache with
in-flight dedup and launch states, served through the
:class:`SandboxLauncherProto` seam defined below — the real impl is
`sandbox_launcher.py`, this package's sole `fxtr_sandboxing` importer,
loaded lazily by the bootstrap), and funnels *every* spawn —
the executor's task-fn resolution (run + safety pass) and steps'
``connect`` — through :meth:`BindingHost.spawn`. Which class serves a key
is invisible to task code.

**The connect-time funnel** (uniform across classes): look the key up
(ephemeral entry, else durable record, else `HandleUnboundError`);
re-read and pin-check each pinned dependency cell (unrecorded session
reads — runner machinery, never a generation observation); spawn each
dependency through this same funnel (recursive — a dependency may itself
be bound in either class; an explicit in-progress set threaded through
the recursion detects cycles); assemble the one uniform
`fxtr_sandbox_types.BindingConnectRequest` (key + parsed config +
pre-spawned dependency refs — no spawner or registry capability ever
enters a guest); then connect — ``connector.connect(request)`` for
ephemeral, a fresh guest connect on the cached (or newly launched)
sandbox process for sandboxed. Dependencies resolve eagerly, *before*
the outer launch. All connect-time failures are fault-class
(`errors.py`): the demanding run interrupts and a later demand retries.

**Process cache and shutdown**: one cached process per *binding* (never
per defn), with in-flight dedup — concurrent demands share one launch,
including its failure; a *later* demand re-attempts a failed launch (no
negative caching). Launch states (unlaunched / launching / running /
faulted + the failure message) are runtime-only, never persisted (the
`run_log` precedent), and feed `user_api.BindingInfo`.
:meth:`shutdown_sandbox` is **release-and-forget**: it drops the cache
entry and signals teardown, returning without waiting — already-connected
guests stay connected (their scopes hold leases on the process's holder)
and the process exits in the background when the last one releases.

Cross-class exclusivity is the *capability's* job (`user_service.py`
serializes both bind orders under one lock); this module only stores and
serves. Scope discipline (`v2/notes/scope_discipline.md`): every stored
ref travels with the scope it resolves in; membership rationales are at
each resolve site.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Protocol

import anyio

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef
from fxtr2.data.system_entities import define_type
from fxtr2.services.service_cap_common import UnknownService
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method
from fxtr_sandbox_types import BindingConnectRequest

from fxtr_experiment_task._ns import WORKFLOWS3_NS
from fxtr_experiment_task.errors import (
    BindingCycleError,
    BindingDependencyFaultError,
    HandleUnboundError,
    SandboxLaunchError,
    SandboxRepoUnconfiguredError,
    UnknownBindingError,
)
from fxtr_experiment_task.model import SandboxDefn
from fxtr_experiment.state_store import SandboxBindingRow, SandboxDepRow, WorkflowSession

SINGLETON_CONNECTOR_TYPE_ID = define_type(WORKFLOWS3_NS, "singleton_connector")


class LaunchedSandboxProto(Protocol):
    """One launched sandbox process, connectable per use (the process half
    of the launcher seam — the real impl is `sandbox_launcher.py`'s;
    fakes implement it directly)."""

    async def connect(
        self, request: BindingConnectRequest, *, into_scope: FxtrScope
    ) -> ServiceRef[UnknownService]:
        """One fresh guest connect (a fresh guest dispatch scope per call —
        the promised-stateless contract).

        Args:
            request: The standard binding connect request. Refs inside it
                (pre-spawned dependency handles) must be resolvable via
                ``into_scope``.
            into_scope: The scope the returned service ref must resolve
                in (the run scope / per-step dispatch scope). Taking an
                independent lease on the process's holder is part of the
                contract: the process stays alive at least as long as
                ``into_scope``.

        Returns:
            The guest service ref, resolvable in ``into_scope``.
        """
        ...

    def shutdown(self) -> None:
        """Release-and-forget: begin teardown and return immediately.

        Already-connected guests stay connected; the process exits in the
        background once the last connected scope releases. Idempotent.
        """
        ...

    async def wait_closed(self) -> None:
        """Block until the process's holder has fully torn down."""
        ...


class SandboxLauncherProto(Protocol):
    """The launcher seam :class:`BindingHost` serves sandboxed bindings
    through. Defined here, with its consumer, so the core never imports
    `fxtr_sandboxing`; the real impl is
    `sandbox_launcher.FxtrSandboxLauncher` (that module is the package's
    sole `fxtr_sandboxing` importer, loaded lazily by ``open_local``),
    and tests substitute fakes."""

    async def launch(self, defn: SandboxDefn) -> LaunchedSandboxProto:
        """Launch one sandbox process for a defn triple.

        Raises:
            SandboxLaunchError: The launch pipeline failed (eval/build/
                deliver/spawn) — fault-class; the full underlying message
                is carried verbatim.
        """
        ...

    async def aclose(self) -> None:
        """Tear down every still-launched process, then the environment.

        Called from the runner bootstrap's exit stack; idempotent.
        """
        ...


@service_protocol
class ConnectorProto(Protocol):
    """Registrant-supplied factory for a bound (promised-stateless) service.

    The binding contract from `user_api.UserCapabilityProto.bind_handle`:
    a handle entity is durable data; what serves it in *this* runner
    process is a live connector whose ``connect(request)`` produces the
    service. The registrant promises the produced service is observably
    stateless across ``connect`` calls, and swapping connectors is
    governed by the compatibility ratchet
    (``notes/sandboxed_bindings.md``).
    """

    async def connect(self, request: BindingConnectRequest) -> ServiceRef[UnknownService]:
        """Produce the service to use (called once per spawn).

        Args:
            request: The standard connect request
                (`fxtr_sandbox_types.BindingConnectRequest` — the same
                shape a sandboxed service entrypoint receives). ``key``
                is the handle key id this spawn serves, its closure
                covered by the call arena; one connector may therefore
                be bound to a whole family of handles and dispatch on
                the key entity's contents. ``config`` / ``handles``
                carry the binding's config blob and pre-spawned pinned
                dependencies (`notes/sandboxed_bindings.md`).

        Returns:
            A ``ServiceRef`` to the service; chain-bridged to the caller by
            the service framework's return-value contract.
        """
        ...


class _SingletonConnector(TypedServiceImpl[ConnectorProto]):
    """Connector that always returns a known-stateless singleton service.

    Attributes:
        _ref: The wrapped ref. Resolvable wherever the wrapped service's
            own mount lives — the connector does not keep it alive; the
            registrant must (the binding is bounded by the bound service's
            lifetime, per `user_api.UserCapabilityProto.bind_handle`).
    """

    def __init__(self, ref: ServiceRef[UnknownService]) -> None:
        """Wrap ``ref`` (caller keeps the underlying service alive)."""
        super().__init__()
        self._ref = ref

    def _target_type_annotation(self) -> Any:
        """The proxy type this service resolves as."""
        return ConnectorProto

    def _service_type_id(self) -> FxtrID:
        """The singleton-connector service type id."""
        return SINGLETON_CONNECTOR_TYPE_ID

    @service_method
    async def connect(self, request: BindingConnectRequest) -> ServiceRef[UnknownService]:
        """Return the wrapped singleton ref (same ref every call).

        The request is ignored: a singleton serves whatever key it was
        bound to, and carries no per-spawn configuration.
        """
        del request
        return self._ref


async def wrap_singleton(
    scope: FxtrScope, ref: ServiceRef[UnknownService]
) -> ServiceRef[ConnectorProto]:
    """Mount a connector whose ``connect()`` returns the singleton ``ref``.

    Use when you hold a service you know to be stateless and want to bind
    it directly.

    Args:
        scope: Scope to mount the connector on (governs the connector's
            lifetime; also where the returned ref is resolvable).
        ref: The singleton service ref the connector should hand out. Must
            stay alive independently (see :class:`_SingletonConnector`).

    Returns:
        A ``ServiceRef`` to the mounted connector, resolvable in ``scope``,
        ready to pass to :meth:`BindingHost.bind`.
    """
    return await scope.mount(_SingletonConnector(ref))


class BindingRelease:
    """Idempotent release for one ephemeral binding (the in-runner half;
    the wire release *capability* wraps this — `user_service.py`)."""

    def __init__(self, host: "BindingHost", key: str) -> None:
        """Bind to the host entry to release."""
        self._host = host
        self._key = key

    def release(self) -> None:
        """Remove the binding if still present (idempotent).

        The connector's keep-alive bridge into the host scope (if one
        was created at bind time) is *not* torn down — like wf2's provider,
        the connector stays retained until the host scope drains; only
        the handle mapping goes away.
        """
        self._host._release(self._key)


class _EphemeralEntry:
    """One ephemeral binding's in-memory record.

    Attributes:
        handle: The bound handle key id (kept beside the map key — its
            string — so listings serve typed ids without re-parsing).
        connector: The connector ref; resolvable in the host scope
            (:meth:`BindingHost.bind` establishes this).
        deps: Pinned dependencies, ``name``-sorted (same shape and pin
            semantics as the durable class; dies with the entry).
        config_json: The binding's config as canonical JSON text, or
            ``None``.
    """

    def __init__(
        self,
        handle: FxtrID,
        connector: ServiceRef[ConnectorProto],
        deps: tuple[SandboxDepRow, ...],
        config_json: str | None,
    ) -> None:
        self.handle = handle
        self.connector = connector
        self.deps = deps
        self.config_json = config_json


class _SandboxSlot:
    """One binding's process-cache slot (the in-flight dedup unit).

    ``ready`` is set when the launch attempt concluded either way;
    exactly one of ``sandbox`` / ``error`` is then set. A slot with
    ``error`` is the *faulted* marker: it stays (for `BindingInfo`)
    until the next demand replaces it or a shutdown clears it.
    """

    def __init__(self) -> None:
        self.ready = anyio.Event()
        self.sandbox: LaunchedSandboxProto | None = None
        self.error: str | None = None


class BindingHost:
    """The runner's binding authority (see the module docstring).

    Attributes:
        _scope: The runner-owned scope bounding every ephemeral binding:
            connector refs are bridged into it at bind time (unless
            already tracked there), so they stay spawnable after the
            binding call's own scope closes. Passed at construction (the
            runner scope in the execution-semantics scope tree).
        _session: The workflow session — the durable sandboxed-binding
            records' owner; also serves the unrecorded pin-check reads.
        _launcher: The sandbox launcher, or ``None`` when the runner has
            no sandbox repo configured (`open_local` couples the two:
            a launcher exists iff ``sandbox_repo`` was passed).
        sandbox_repo: The runner's configured sandbox repo path, or
            ``None`` — ephemeral, machine-local runner configuration
            (never persisted, never exported), surfaced by the
            monitoring state.
    """

    def __init__(
        self,
        scope: FxtrScope,
        session: WorkflowSession,
        *,
        launcher: SandboxLauncherProto | None = None,
        sandbox_repo: Path | None = None,
    ) -> None:
        """A binding host for one runner (see class doc).

        Args:
            scope: The runner scope bounding every ephemeral binding.
            session: The workflow session (durable records + pin reads).
            launcher: The sandbox launcher; ``None`` = sandboxed bindings
                are inert (they fault repo-unconfigured on demand).
            sandbox_repo: The configured repo path the launcher was built
                from (reporting only here; the launcher already carries
                it operationally).
        """
        self._scope = scope
        self._session = session
        self._launcher = launcher
        self.sandbox_repo = sandbox_repo
        self._bindings: dict[str, _EphemeralEntry] = {}
        self._slots: dict[str, _SandboxSlot] = {}
        self._change_listener: "Callable[[], None] | None" = None

    # -- change notification (the `bindings` hint source) --------------------------

    def set_change_listener(self, listener: "Callable[[], None] | None") -> None:
        """Install (or clear) the binding-change callback.

        Called synchronously after every bind, release, and sandbox
        launch-state transition (the ``bindings`` hint source of
        ``notes/monitoring_ui.md``); must not block. One listener, like
        the session's event seam.
        """
        self._change_listener = listener

    def _notify_change(self) -> None:
        if self._change_listener is not None:
            self._change_listener()

    # -- the ephemeral class --------------------------------------------------------

    async def bind(
        self,
        handle: FxtrID,
        connector: ServiceRef[ConnectorProto],
        *,
        connector_scope: FxtrScope,
        deps: tuple[SandboxDepRow, ...] = (),
        config_json: str | None = None,
    ) -> BindingRelease:
        """Register a live connector for a handle entity.

        The connector is bridged from ``connector_scope`` into the host
        scope (``keepalive_via="independent_lease"``, the wf2-provider
        retention pattern), unless the host scope already tracks it —
        the in-process same-scope shortcut. Note the bridge nesting rule:
        ``connector_scope`` must not be block-nested *inside* the host
        scope in the same task (see ``bridge_service_from_scope``).

        Args:
            handle: The handle entity id.
            connector: The connector service whose ``connect()`` produces
                the bound service.
            connector_scope: Scope where ``connector`` is currently
                resolvable (per scope discipline, refs travel with their
                scope; the caller mounted or received it there).
            deps: Already-pinned dependencies, ``name``-sorted (the
                capability resolved + pinned them — same semantics as
                the durable class; `WorkflowSession.pin_dependency_cells`).
            config_json: The binding's config as canonical JSON text.

        Returns:
            The idempotent release for this binding.

        Raises:
            ValueError: The handle is *currently* bound (release the
                existing binding first — the decided exclusivity rule;
                rebinding after release is the promised-stateless
                contract).
        """
        key = handle.id_string()
        if key in self._bindings:
            raise ValueError(f"handle {key} is already bound; release it first")
        # Normalize the python-side target type: refs arriving over the
        # wire (``bind_handle``) decode as ``ServiceRef[UnknownService]``,
        # and an untyped proxy cannot encode the connect request (its args
        # must be FxtrServiceData). Retyping changes neither the handle nor
        # the wire ``service_type_id``, so tracking/bridging below is
        # unaffected — every stored ref resolves as a typed connector.
        connector = connector.replace_service_type(ConnectorProto)
        if not self._scope.is_tracked(connector):
            await self._scope.bridge_service_from_scope(
                connector, connector_scope, keepalive_via="independent_lease"
            )
        self._bindings[key] = _EphemeralEntry(handle, connector, deps, config_json)
        self._notify_change()
        return BindingRelease(self, key)

    def _release(self, key: str) -> None:
        """Drop one ephemeral entry (idempotent; `BindingRelease`'s half)."""
        existed = self._bindings.pop(key, None) is not None
        if existed:
            self._notify_change()

    def is_bound(self, handle: FxtrID) -> bool:
        """Whether a live ephemeral binding exists for a handle entity."""
        return handle.id_string() in self._bindings

    def bound_handles(self) -> tuple[FxtrID, ...]:
        """The handle ids with live ephemeral bindings, in id-string order
        — the introspection half of the ephemeral surface (runner state;
        see ``notes/monitoring_ui.md``)."""
        return tuple(self._bindings[key].handle for key in sorted(self._bindings))

    def ephemeral_entry(
        self, handle: FxtrID
    ) -> "tuple[tuple[SandboxDepRow, ...], str | None] | None":
        """An ephemeral binding's ``(deps, config_json)``, or ``None`` if
        the key has no ephemeral binding (the listing surface's detail)."""
        entry = self._bindings.get(handle.id_string())
        return None if entry is None else (entry.deps, entry.config_json)

    # -- the serving funnel -----------------------------------------------------------

    async def spawn(self, handle: FxtrID, *, into_scope: FxtrScope) -> ServiceRef[UnknownService]:
        """Spawn a fresh service for a bound handle (one connect per call).

        The funnel every consumption path goes through (module docstring):
        both binding classes, dependency resolution included.

        Args:
            handle: The handle entity id. Callers guarantee it is
                loadable via ``into_scope`` (both call sites read it
                from the session, whose serving re-attaches ids), so
                the connect call's arena covers the key for the
                connector / guest.
            into_scope: Scope to resolve everything in. Must be able to
                resolve refs known to the host scope, i.e. include it
                directly or transitively (the run scope in the
                execution-semantics scope tree qualifies: run scopes are
                children of the runner scope). Sandbox connects
                additionally take an independent lease on the process's
                holder through it (the release-and-forget contract).

        Returns:
            The service ref produced by the binding, resolvable in
            ``into_scope``.

        Raises:
            HandleUnboundError: No binding in either class.
            BindingDependencyFaultError: A pinned dependency cell is
                empty, missing, or no longer holds the pinned key.
            BindingCycleError: The bindings' dependencies form a cycle.
            SandboxRepoUnconfiguredError: A sandboxed binding was reached
                with no launcher configured.
            SandboxLaunchError: The sandbox launch pipeline failed.
        """
        return await self._spawn_inner(handle, into_scope, frozenset())

    async def _spawn_inner(
        self, handle: FxtrID, into_scope: FxtrScope, in_progress: frozenset[str]
    ) -> ServiceRef[UnknownService]:
        """The funnel body; ``in_progress`` is the cycle-detection set
        (explicit parameter threading — recursion is direct calls within
        one connect, so no ambient state is involved)."""
        key = handle.id_string()
        if key in in_progress:
            raise BindingCycleError(
                f"binding dependency cycle: {key} is already being served"
                f" in this connect (chain: {sorted(in_progress)})"
            )
        entry = self._bindings.get(key)
        if entry is not None:
            request = await self._assemble_request(
                handle, entry.deps, entry.config_json, into_scope, in_progress | {key}
            )
            # Membership: `entry.connector` is resolvable in the host
            # scope (bind establishes this), and `into_scope` includes
            # the host scope per this method's precondition.
            connector = await entry.connector.resolve(into_scope)
            return await connector.connect(request)
        row = await self._session.get_sandbox_binding(handle)
        if row is None:
            raise HandleUnboundError(f"no binding for handle {key}")
        request = await self._assemble_request(
            handle, row.deps, row.config_json, into_scope, in_progress | {key}
        )
        sandbox = await self._get_or_launch(key, row)
        return await sandbox.connect(request, into_scope=into_scope)

    async def _assemble_request(
        self,
        handle: FxtrID,
        deps: tuple[SandboxDepRow, ...],
        config_json: str | None,
        into_scope: FxtrScope,
        in_progress: frozenset[str],
    ) -> BindingConnectRequest:
        """Pin-check + resolve the dependencies, parse the config, and
        build the uniform connect request (eager — before any launch)."""
        handles: dict[str, ServiceRef[UnknownService]] = {}
        for dep in deps:  # name-sorted (both providers' contract)
            # Pin-check read: runner machinery — unrecorded, no
            # generation observation (`notes/sandboxed_bindings.md`).
            current = await self._session.read_cell(dep.grid_path, dep.coords)
            if current is None:
                raise BindingDependencyFaultError(
                    f"dependency {dep.name!r} of binding {handle.id_string()}:"
                    f" cell {dep.grid_path}{dep.coords} is empty or missing"
                    f" (pinned {dep.pinned_key_id.id_string()})"
                )
            if current != dep.pinned_key_id:
                raise BindingDependencyFaultError(
                    f"dependency {dep.name!r} of binding {handle.id_string()}:"
                    f" pin mismatch at {dep.grid_path}{dep.coords} — pinned"
                    f" {dep.pinned_key_id.id_string()}, the cell now holds"
                    f" {current.id_string()} (an identity-level change;"
                    f" remove and re-bind to re-pin)"
                )
            # The dep key rides the recursive connect request as its
            # `key`; re-attach it so the call arena can cover its closure
            # (the served-ids-are-re-attached rule — the pin's liveness
            # guarantee makes this total).
            await self._session.ensure_loadable(current)
            handles[dep.name] = await self._spawn_inner(current, into_scope, in_progress)
        config = None if config_json is None else json.loads(config_json)
        return BindingConnectRequest(key=handle, config=config, handles=handles)

    # -- the sandbox process cache -----------------------------------------------------

    async def _get_or_launch(self, key: str, row: SandboxBindingRow) -> LaunchedSandboxProto:
        """The per-binding process cache: reuse, share an in-flight
        launch (its failure included), or launch fresh; a previous
        attempt's *faulted* slot is replaced (re-attempt per demand)."""
        if self._launcher is None:
            raise SandboxRepoUnconfiguredError(
                f"handle {key} is sandbox-bound but this runner has no sandbox"
                " repo configured — open the workflow with"
                " open_local(sandbox_repo=...) and re-demand"
            )
        slot = self._slots.get(key)
        if slot is not None:
            if not slot.ready.is_set():
                # In-flight: share the launch, outcome included.
                await slot.ready.wait()
                if slot.sandbox is not None:
                    return slot.sandbox
                raise SandboxLaunchError(slot.error or "sandbox launch was cancelled")
            if slot.sandbox is not None:
                return slot.sandbox
            # A previous demand's faulted slot: this demand re-attempts.
            if self._slots.get(key) is slot:
                del self._slots[key]
        # Fresh launch. No awaits between the re-check and the insert, so
        # the slot is visible to concurrent demands before we suspend.
        slot = _SandboxSlot()
        self._slots[key] = slot
        self._notify_change()  # -> launching
        try:
            sandbox = await self._launcher.launch(row.defn)
        except BaseException as exc:
            if isinstance(exc, Exception):
                # Keep the faulted slot as the BindingInfo evidence; the
                # next demand replaces it.
                slot.error = str(exc)
            else:
                # Cancelled mid-launch: no attempt evidence to keep;
                # waiters fault retryably, the next demand relaunches.
                slot.error = "sandbox launch was cancelled"
                if self._slots.get(key) is slot:
                    del self._slots[key]
            slot.ready.set()
            self._notify_change()  # -> faulted / unlaunched
            raise
        slot.sandbox = sandbox
        slot.ready.set()
        self._notify_change()  # -> running
        return sandbox

    def sandbox_launch_state(self, handle: FxtrID) -> tuple[str, str | None]:
        """A sandboxed binding's runtime launch state.

        Returns:
            ``(state, fault_message)`` with state one of ``"unlaunched"``
            / ``"launching"`` / ``"running"`` / ``"faulted"``;
            ``fault_message`` is set exactly for ``"faulted"`` (the last
            attempt's full failure message).
        """
        slot = self._slots.get(handle.id_string())
        if slot is None:
            return ("unlaunched", None)
        if not slot.ready.is_set():
            return ("launching", None)
        if slot.sandbox is not None:
            return ("running", None)
        return ("faulted", slot.error)

    async def shutdown_sandbox(self, handle: FxtrID) -> None:
        """Tear down a binding's cached process (release-and-forget).

        Drops the cache entry and signals teardown, returning without
        waiting: already-connected guests stay connected and the process
        exits in the background when the last connected scope releases;
        the next demand relaunches fresh. A faulted slot is cleared the
        same way (back to ``unlaunched``). No cached process at all is a
        no-op for a key that *has* a sandboxed binding.

        Raises:
            UnknownBindingError: ``handle`` has no sandboxed binding.
        """
        if not await self._session.has_sandbox_binding(handle):
            raise UnknownBindingError(f"no sandboxed binding for handle {handle.id_string()}")
        await self.drop_process(handle)

    async def drop_process(self, handle: FxtrID) -> None:
        """The record-blind half of :meth:`shutdown_sandbox` (also run by
        ``remove_sandbox_binding`` after the record delete): drop + signal
        the cached process, if any. An in-flight launch is awaited first —
        the process needs to exist before it can be told to tear down."""
        slot = self._slots.pop(handle.id_string(), None)
        if slot is None:
            return
        if not slot.ready.is_set():
            await slot.ready.wait()
        if slot.sandbox is not None:
            slot.sandbox.shutdown()
        self._notify_change()
