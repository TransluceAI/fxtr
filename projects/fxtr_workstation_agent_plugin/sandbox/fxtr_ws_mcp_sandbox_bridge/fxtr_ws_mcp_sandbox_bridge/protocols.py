"""Wire-format types shared between host (MCP server) and guest (sandbox).

Defines the service protocols and the :class:`RunnerConfig` carrier
that the host hands to the sandbox via :class:`SandboxConnectProto.connect`.
Both sides import this module — on the host via the installed
``fxtr_ws_mcp_sandbox_bridge`` package, in the sandbox via the
volume-mounted copy.

Imports require ``fxtr2`` (host) or ``fxtr2-portable`` (sandbox). Same
namespace + same ``define_type`` calls → same wire-form
``fxtr_type`` :class:`FxtrID` values on both sides.
"""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef, ServiceTypeAnnotation
from fxtr2.leases.leased_cap_ir import (
    IRFuture,
    IRHandle,
    IRValue,
    Request,
    UnwrapForPipelining,
)
from fxtr2.leases.lease_types import FreshLifetime, Lease, LifetimeToken
from fxtr2.leases.local_lease import STATIC_LIFETIME, StaticBrand
from fxtr2.services.service_cap_common import FxtrServiceData, UnknownService
from fxtr2.services.typed_service import service_protocol

#: Stable namespace UUID for fxtr_workstation_mcp's wire types.
WS_MCP_NS = uuid.UUID("c1f8a3e6-5d2b-4a91-8c34-7e9f2b1d6a8e")

MCP_RUNNER_TYPE_ID: FxtrID = system_entities.define_type(WS_MCP_NS, "ws_mcp_runner")
MCP_HOST_TYPE_ID: FxtrID = system_entities.define_type(WS_MCP_NS, "ws_mcp_host")

#: Type-id sentinel populated on every :class:`ServiceRef` built for a
#: workstation-known remote service inside the sandbox. The wire never
#: actually carries this id (a remote service rides as ``["service",
#: short_id]`` and the workstation knows its own service type from the
#: short_id), but :class:`ServiceRef` requires the field, so a stable
#: sentinel makes refs Eq-comparable.
WS_MCP_REMOTE_SERVICE_TYPE_ID: FxtrID = system_entities.define_type(
    WS_MCP_NS, "ws_mcp_remote_service"
)

#: Type-id sentinel for :class:`ServiceRef`\\s built for callbacks this
#: session owns. Like :data:`WS_MCP_REMOTE_SERVICE_TYPE_ID`, never
#: serialized — owned-callback wire shapes (``["callback_handle", k]``
#: / ``["callback", N]``) carry their identity through the slot
#: number, not a service_type_id.
WS_MCP_OWNED_CALLBACK_TYPE_ID: FxtrID = system_entities.define_type(
    WS_MCP_NS, "ws_mcp_owned_callback"
)


# --- NixWasmServiceDefn (sandbox-local dup of the canonical type) ------
#
# Wire-identical duplicate of
# ``fxtr_sandbox_types.entities.NixWasmServiceDefn``: same namespace UUID +
# ``define_type`` string + field names/types → same ``fxtr_type``
# :class:`FxtrID`, so an instance the agent constructs + ``.store()``s in the
# sandbox decodes against the canonical type on the workstation/spawner side.
# We dup (rather than import ``fxtr_sandbox_types``) because the sandbox venv
# carries only fxtr2-portable + this bridge. Mirrors the established
# :class:`WorkstationSessionEventInbox` dup pattern below.

#: Must equal ``fxtr_sandbox_types._namespace.SHARED_SANDBOX_NS``.
SHARED_SANDBOX_NS = uuid.UUID("4d8f2c6e-7a91-4f3b-bc5e-1a2b3c4d5e6f")


@dataclass(frozen=True)
class NixWasmServiceDefn(
    DataclassEntity,
    fxtr_type=system_entities.define_type(SHARED_SANDBOX_NS, "nix_wasm_service_defn"),
):
    """A git-referenced sandbox service: ``(git_commit, flake_attr,
    flake_path?)``. The agent constructs this in ``run_script``, ``.store()``s
    it, and passes it to ``spawn``; the spawner builds the code from the
    pinned commit. ``entry_point`` is **not** here — it lives on the flake
    attr. See the canonical
    ``fxtr_sandbox_types.entities.NixWasmServiceDefn`` +
    ``projects/fxtr_workstation/plans/phase0/part5_nix_workdir/design_notes/12_nix_drv_reference.md``.
    """

    git_commit: str
    flake_attr: str
    flake_path: str | None = None


@dataclass(frozen=True)
class NixContainerServiceDefn(
    DataclassEntity,
    fxtr_type=system_entities.define_type(SHARED_SANDBOX_NS, "nix_container_service_defn"),
):
    """The container-kind (OCI) sibling of :class:`NixWasmServiceDefn`,
    identical field shape. Points at a ``fxtr.services.<name>`` attr whose
    buildable leaf is a nix2container ``image`` (``kind = "container"``);
    the spawner builds the image manifest and delivers it into the container
    runtime. See the canonical
    ``fxtr_sandbox_types.entities.NixContainerServiceDefn`` +
    ``projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage7_nix_container.md``.
    """

    git_commit: str
    flake_attr: str
    flake_path: str | None = None


@dataclass(frozen=True)
class NixPyodideServiceDefn(
    DataclassEntity,
    fxtr_type=system_entities.define_type(SHARED_SANDBOX_NS, "nix_pyodide_service_defn"),
):
    """The pyodide-kind sibling of :class:`NixWasmServiceDefn`, identical
    field shape. Points at a ``fxtr.services.<name>`` attr built by
    ``mkPyodideService`` (``kind = "pyodide"``): pyodide CPython-wasm run in
    a permission-locked deno subprocess on the host; the guest site-packages
    may include PEP 783 ``pyemscripten_*_wasm32`` binary wheels. See the
    canonical ``fxtr_sandbox_types.entities.NixPyodideServiceDefn`` +
    ``projects/fxtr_sandboxing/plans/stage2/2p5_deno_py_sandbox/plan.md``.
    """

    git_commit: str
    flake_attr: str
    flake_path: str | None = None


# --- Entity-arg handle namespace --------------------------------------
#
# The host's per-script :class:`McpHostImpl` stashes copied-out entities
# under string *handles*. Handles live in a namespace distinct from
# agent-facing slot_keys and from workstation external-names — see the
# stage-4 design notes. Each kind has a fixed prefix so the three
# namespaces can't silently collide:
#
# - ``slot:{slot_key}`` for ``export_entity(slot_key, ...)``.
# - ``invoke_arg:{uuid4}`` for an entity arg of a ``schedule_invoke``.
#
# The wire op records continue to carry the agent slot_key (for
# ``ExportEntityOp``) or the synthesized handle (for
# ``ScheduleInvokeOp.entity_arg_handles``); the host uses these
# helpers to derive the matching handle when looking entities up in
# ``copied_entities`` and the ``entity_short_ids`` map.
#
# Callback handles (stage 6) live in a separate namespace — they
# ride as :class:`_OwnedCallbackHandleStub` inside the sandbox-side
# :class:`ServiceRef`, and the wire walker emits the
# ``["callback_handle", slot_key]`` / ``["callback", slot_n]``
# placeholder accordingly. No host-side ``copied_entities`` slot is
# needed because callbacks aren't entities — the workstation owns the
# receive-side service and the slot number is enough to reach it.


def slot_handle(slot_key: str) -> str:
    """Handle for the entity backing ``export_entity(slot_key, ...)``."""
    return f"slot:{slot_key}"


def fresh_invoke_arg_handle() -> str:
    """Fresh synthesized handle for one entity arg of a
    ``schedule_invoke``. UUID-suffixed so multiple distinct args (and
    repeated calls) don't collide."""
    return f"invoke_arg:{uuid.uuid4()}"


# --- Sandbox-side IRHandle stubs --------------------------------------
#
# A sandbox-side :class:`ServiceRef` looks like a real fxtr2
# :class:`ServiceRef` but its ``handle`` is one of these stubs rather
# than a wire-dispatchable IRHandle. The sandbox never invokes the
# handle — it only inspects it during ``schedule_invoke``'s wire walk
# to decide which bracketed form to emit, and during
# ``retrieve_result`` decoding to choose how to construct the
# returned ref. ``request`` / ``dispatch`` therefore raise
# ``NotImplementedError`` deliberately; reaching them means we lost
# track of a stub at some intermediate layer.
#
# Both stubs use :data:`STATIC_LIFETIME` so the ``token`` property
# returns something cheap and hashable without setting up any of the
# real lifetime / lease machinery.


@dataclass(frozen=True)
class _RemoteServiceHandleStub(IRHandle[StaticBrand]):
    """Stub IRHandle for a workstation-known remote service.

    Carries only the workstation's ``short_id``. When the wire walker
    sees a :class:`ServiceRef` whose handle is this kind, it emits
    ``["service", short_id]`` for the invoke wire payload.

    Built by :func:`_make_remote_service_ref` (used by
    ``get_service`` / ``lookup_service_by_name`` and by
    ``_unescape_payload`` when decoding ``["service", short_id]`` /
    ``["exported_service", short_id]`` wire tags).
    """

    short_id: str

    @property
    def token(self) -> LifetimeToken[StaticBrand]:
        return STATIC_LIFETIME.token

    async def request(
        self,
        lease: Lease[StaticBrand] | UnwrapForPipelining[Lease[StaticBrand], Any],
        request: Request,
    ) -> IRValue:
        del lease, request
        raise NotImplementedError(
            "_RemoteServiceHandleStub.request is not dispatchable from the sandbox — "
            "stubs are inspected by the wire walker, never invoked."
        )

    def dispatch[
        RB: FreshLifetime[Any]
    ](
        self,
        lease: Lease[StaticBrand] | UnwrapForPipelining[Lease[StaticBrand], Any],
        request: Request,
    ) -> tuple[IRFuture[Any, RB], Lease[RB]]:
        del lease, request
        raise NotImplementedError(
            "_RemoteServiceHandleStub.dispatch is not dispatchable from the sandbox — "
            "stubs are inspected by the wire walker, never invoked."
        )


@dataclass(frozen=True)
class _OwnedCallbackHandleStub(IRHandle[StaticBrand]):
    """Stub IRHandle for a callback this session owns.

    Two shapes share this class; exactly one of ``slot_key`` /
    ``slot_n`` is populated:

    - **Same-script create+use.** ``create_callback(slot_key)``
      returns a :class:`ServiceRef` with handle
      ``_OwnedCallbackHandleStub(slot_key=...)``. Passing this ref
      to ``schedule_invoke`` in the same script emits the
      ``["callback_handle", slot_key]`` placeholder; the host's
      mutation builder rewrites it to ``["callback", slot_n]``
      after allocating the slot from the matching
      :class:`CreateCallbackOp`.
    - **Cross-script use.** ``retrieve_result(slot_key)`` for a
      slot whose value is ``["callback_open", N]`` returns a
      :class:`ServiceRef` with handle
      ``_OwnedCallbackHandleStub(slot_n=N)``. The wire walker
      emits ``["callback", N]`` directly — no host-side
      substitution needed.

    Owned-callback refs are valid both as ``schedule_invoke`` *args*
    (emitted in the placeholder grammar above) and as the
    ``schedule_invoke`` *target* (queued as a
    :class:`ScheduleCallbackInvokeOp`; the host emits a
    callback-slot-targeted ``CallbackInvokeOp`` on the wire).
    """

    slot_key: str | None = None
    slot_n: int | None = None

    def __post_init__(self) -> None:
        if (self.slot_key is None) == (self.slot_n is None):
            raise ValueError(
                "_OwnedCallbackHandleStub: exactly one of slot_key / slot_n must be set"
            )

    @property
    def token(self) -> LifetimeToken[StaticBrand]:
        return STATIC_LIFETIME.token

    async def request(
        self,
        lease: Lease[StaticBrand] | UnwrapForPipelining[Lease[StaticBrand], Any],
        request: Request,
    ) -> IRValue:
        del lease, request
        raise NotImplementedError(
            "_OwnedCallbackHandleStub.request is not dispatchable from the sandbox — "
            "stubs are inspected by the wire walker, never invoked."
        )

    def dispatch[
        RB: FreshLifetime[Any]
    ](
        self,
        lease: Lease[StaticBrand] | UnwrapForPipelining[Lease[StaticBrand], Any],
        request: Request,
    ) -> tuple[IRFuture[Any, RB], Lease[RB]]:
        del lease, request
        raise NotImplementedError(
            "_OwnedCallbackHandleStub.dispatch is not dispatchable from the sandbox — "
            "stubs are inspected by the wire walker, never invoked."
        )


# --- WorkstationSessionEventInbox (sandbox-local mirror) ---------------
#
# Same shape as ``fxtr_workstation.protocols.WorkstationSessionEventInbox``,
# redeclared here because ``fxtr_workstation`` isn't in the sandbox
# dep closure. (The fxtr2 subscription primitives — ``Subscriber`` /
# ``UnsubscribeHandle`` / ``EventSource`` — are NOT duplicated:
# ``fxtr2.services.subscription_types`` ships in fxtr2-portable and is
# imported directly.) Class identities don't need to match the host —
# the wire never carries the owned-callback ``service_type_id`` (the
# slot number is the identity), and ``ServiceRef`` dialect encoding
# strips ``target_type`` to :data:`UnknownService` on the way out.
# The local declaration exists so the agent gets Pyright-typed
# callback refs (and typed ``schedule_invoke`` dispatch for
# ``subscribe_to``).
#
# Future cleanup: extract into a tiny shared package both host and
# sandbox can depend on, then drop this duplicate.


@service_protocol
class WorkstationSessionEventInbox[T: FxtrServiceData](Protocol):
    """An inbox for events in a workstation session.

    See ``fxtr_workstation.protocols.WorkstationSessionEventInbox``
    for the canonical docstring; this is the sandbox-local mirror used
    as the default ``target_type`` of an owned-callback
    :class:`ServiceRef`. Invoke ``subscribe_to`` / ``notify`` /
    ``listening`` on it via ``schedule_invoke`` (the callback ref is a
    valid invoke *target* — the host emits a callback-slot-targeted
    invoke op).
    """

    async def subscribe_to(
        self, event_source: ServiceRef[Any], method: str = "subscribe"
    ) -> None: ...
    async def notify(self, payload: T) -> None: ...
    async def listening(self) -> bool: ...


# --- NixDispatcherProto (sandbox-local mirror) ------------------------
#
# Agent-side mirror of ``fxtr_workstation.dynamic_dispatch.NixDispatcherProto``,
# redeclared here because ``fxtr_workstation`` isn't in the sandbox dep
# closure. Only ``set_preset`` is declared — the one method an agent invokes
# on a renderer dispatcher. The base ``resolve`` / ``subscribe``
# are frontend-facing and reference ``RendererResolution`` / ``RendererEntity``
# (workstation-only types), so they're deliberately omitted. Wire dispatch is
# by method name, so the agent reaches the workstation impl's ``set_preset``
# via ``ServiceRef.replace_service_type(NixDispatcherProto)`` regardless of
# the agent-side service-type-id. (The host proto is a genuine
# ``WorkstationDispatcherProto`` subclass since stage 13 part 0; this mirror
# stays standalone because the sandbox can't import the base's module.)
#
# Future cleanup: extract into a tiny shared package both host and sandbox
# can depend on, then drop this duplicate.


@service_protocol
class NixDispatcherProto(Protocol):
    """Agent-side handle for the nix/git renderer dispatcher.

    Usage in a ``run_script`` (the dispatcher is a workstation service the
    agent looks up by name, then re-types)::

        dispatcher = (await lookup_service_by_name("renderer-dispatcher"))\\
            .replace_service_type(NixDispatcherProto)
        await schedule_invoke(
            "set_preset", dispatcher,
            lambda d: d.set_preset(
                git_commit=current_commit(),
                flake_attr="fxtr.uiPresets.my_preset",
            ),
        )

    See ``fxtr_workstation.dynamic_dispatch.NixDispatcherProto`` for the
    canonical (full) protocol + behaviour.
    """

    async def set_preset(
        self,
        git_commit: str,
        flake_attr: str,
        flake_path: str | None = None,
    ) -> None: ...


# --- ServiceRef construction helpers ----------------------------------


def _make_remote_service_ref[
    T
](
    short_id: str,
    target_type: ServiceTypeAnnotation[T] = UnknownService,  # type: ignore[assignment]
) -> ServiceRef[T]:
    """Build a sandbox-side :class:`ServiceRef` for a workstation-known service.

    ``target_type`` defaults to :data:`UnknownService` so untyped
    callsites keep working. The dialect's ``ServiceRefServiceStrategy``
    will strip ``target_type`` back to ``UnknownService`` on encode
    regardless — typed callsites get Pyright-side benefits but the
    wire shape is identical.
    """
    return ServiceRef(
        handle=_RemoteServiceHandleStub(short_id=short_id),
        target_type=target_type,
        service_type_id=WS_MCP_REMOTE_SERVICE_TYPE_ID,
    )


def _make_owned_callback_ref(
    *,
    slot_key: str | None = None,
    slot_n: int | None = None,
) -> ServiceRef[WorkstationSessionEventInbox]:
    """Build a sandbox-side :class:`ServiceRef` for a session-owned callback.

    Exactly one of ``slot_key`` / ``slot_n`` must be set. See
    :class:`_OwnedCallbackHandleStub` for the same-script vs.
    cross-script semantics.

    Returns a typed ``ServiceRef[WorkstationSessionEventInbox]`` — the
    agent-visible default that parallels how :func:`create_callback`
    returns. Dialect-layer unescape paths
    (:func:`builtins._resolve_slot_to_fxtr_service_data` for the
    ``callback_open`` tag, :func:`builtins._assemble` for the
    ``["callback", N]`` tag) must downcast via
    :meth:`ServiceRef.replace_service_type` to
    :data:`UnknownService` so the canonical
    ``ServiceRef[UnknownService]`` wire shape reaches
    :func:`fxtr2.services.service_dialect.decode_from_service_data`,
    which re-attaches ``T`` from the caller's annotation.
    """
    return ServiceRef(
        handle=_OwnedCallbackHandleStub(slot_key=slot_key, slot_n=slot_n),
        target_type=WorkstationSessionEventInbox,
        service_type_id=WS_MCP_OWNED_CALLBACK_TYPE_ID,
    )


# --- Result-type sentinel ---------------------------------------------


_T_RESULT = TypeVar("_T_RESULT")


@dataclasses.dataclass(frozen=True)
class _ResultTypeSentinel(Generic[_T_RESULT]):
    """Holder for a typed-service return-wrapper class.

    Returned by ``result_type_of(proto, proto.method)`` and consumed
    by ``retrieve_result`` / ``retrieve_callback_notification`` to
    decode payloads through the layer-2 single-field result wrapper
    (the dataclass with a sole ``value: ReturnType`` field generated
    by ``fxtr2.services.typed_service`` per Protocol method).

    Phantom-typed over ``_T_RESULT`` so pyright can infer the return
    type of ``retrieve_result(..., result_type=result_type_of(...))``
    from the method object passed to :func:`result_type_of`. The
    ``wrapper_cls`` field is the actual generated dataclass at
    runtime; ``_T_RESULT`` exists only for the type-checker.
    Declared via explicit ``Generic[_T_RESULT]`` (rather than PEP-695
    ``class _ResultTypeSentinel[T]``) so the TypeVar is anchored in
    the base-class arg and pyright doesn't flag it as unused.

    The class-attribute marker pattern doesn't work for parameterized
    generic aliases (``setattr`` on a :class:`types.GenericAlias`
    raises), so we wrap the (possibly parameterized) wrapper class in
    this opaque holder. ``isinstance`` discrimination at
    decode-time picks this path over the raw-annotation
    :func:`decode_from_service_data` path.
    """

    wrapper_cls: Any


# --- Agent-facing exceptions (raised inside the script) ---------------
#
# The host RPC methods raise ``IRException`` with a ``category`` string;
# the sandbox builtins catch those and re-raise these Python exceptions so
# the agent's script can ``except`` them by name. Re-exported into
# ``_fxtr_mcp_builtin`` by ``builtins.build_builtins``.


class NixEntityBuildFailed(Exception):
    """Raised when ``retrieve_nix_entity`` fails to eval/build an entity.

    Covers a missing ``flake.nix``, a flake/attr that doesn't evaluate (or
    isn't entity-shaped), and a failed ``nix build`` of the entity's
    ``.layer``. The message carries the offending attr path plus a tail of
    the nix stderr; the full stderr is in the captured ``run_script``
    output. (A missing ``layerKey`` *inside* a successfully-built layer
    raises :class:`LookupError`, not this.)
    """


class WorkingTreeDirty(Exception):
    """Raised by ``current_commit()`` when the workdir has uncommitted
    changes (including untracked files). nix git flakes only see the
    committed tree, so a commit can't be pinned until the tree is clean."""


@service_protocol
class McpHostProto(Protocol):
    """Host-side capability bridged into the sandbox via
    :class:`RunnerConfig`.

    Read-side methods (``lookup_entity``, ``read_state_slot``) let
    the sandbox query host-resident data. Write-side
    (``copy_entity_in``) lets the sandbox hand entities to the host
    so they survive past the per-RPC-call dispatch scope, ready for
    the end-of-script mutation pipeline.
    """

    async def lookup_entity(self, short_id: str) -> FxtrID:
        """Resolve a workstation short_id to its :class:`FxtrID`.

        The host implementation delegates to the workstation's
        ``get_entity``; the resulting ``FxtrID`` is bridged across
        both RPC connections back into the sandbox's connect-dispatch
        scope. The sandbox can then ``EntityRef(returned, T).load()``
        to inspect the entity.

        Raises a fxtr-RPC error inside the sandbox if the short_id
        doesn't resolve (workstation-side error propagates).
        """
        ...

    async def copy_entity_in(self, handle: str, entity_id: FxtrID) -> None:
        """Hand an entity to the host so it survives past the per-RPC-
        call dispatch scope.

        Used by the sandbox's ``export_entity`` builtin and by
        ``schedule_invoke`` for each entity-typed arg. The host
        copies the entity into the per-script ``script_scope`` and
        stashes ``(handle → fxtr_id)`` on the per-script
        :class:`McpHostImpl`. The ``handle`` lives in a namespace
        distinct from agent-facing slot_keys — see
        :func:`slot_handle` / :func:`fresh_invoke_arg_handle`. The
        host raises (over RPC) if the same handle is reused inside
        one script.
        """
        ...

    async def read_state_slot(self, slot_key: str) -> list[Any] | None:
        """Return the local mirror's value for ``slot_key`` (as a
        list-tagged wire form: ``["exported_entity", id]`` /
        ``["exported_service", id]`` / ``["invoke_pending", n]`` /
        ``["invoke_result", payload]`` / ``["invoke_error", msg]``),
        or ``None`` if the slot doesn't exist.

        Used by the sandbox's ``retrieve_result`` builtin to read
        the current value of an agent-facing slot. The local mirror
        is authoritative — no round-trip to the workstation needed
        (the mirror always reflects the workstation view + any
        unflushed local effects, by invariant).
        """
        ...

    async def lookup_short_id_by_external_name(self, external_name: str) -> str:
        """Resolve ``external_name`` to the workstation short_id
        registered under it (either an entity or a service —
        external names live in a single namespace, see
        :meth:`WorkstationServiceProto.lookup_short_id_by_external_name`).

        Used by the sandbox-side ``lookup_service_by_name`` and
        ``lookup_entity_by_name`` builtins to discover stable
        short_ids without needing them embedded out-of-band.
        Raises if no active registration is bound to the name.
        """
        ...

    async def read_callback_notification(self, slot_key: str, notif_index: int) -> Any:
        """Return the wire-escaped payload stored at
        ``mirror.callback_notifications[slot_key][str(notif_index)]``.

        The payload is the value originally carried by the
        :class:`CallbackEvent` — any wire-form shape (scalar,
        dict, tagged form, literal-list-escape). The
        sandbox-side ``retrieve_callback_notification`` builtin
        runs the result through the same three-pass unescape
        that ``retrieve_result`` uses for ``invoke_result``
        payloads.

        Raises :class:`IRException` with ``category="KeyError"``
        when the notification isn't stored (no entry exists, or
        it was previously dropped via
        :class:`ForgetCallbackNotificationOp`). The sandbox-side
        builtin catches that and re-raises a Python
        :class:`KeyError` to the agent.
        """
        ...

    async def list_callback_notification_indices(self, slot_key: str) -> list[int]:
        """Return the sorted list of ``notif_index`` values currently
        stored in ``mirror.callback_notifications[slot_key]``.

        Used by the sandbox-side ``callback_notification_keys``
        builtin so the agent can enumerate received notifications
        for a given callback slot. Returns an empty list if no
        notifications have been stored (either the slot_key is
        unknown or every notification has been forgotten).
        """
        ...

    async def get_full_exception(self, exc_id: str) -> str:
        """Return the full ``traceback.format_exception`` string for
        a previously-stored script-execution exception.

        Host-side ``_format_script_error`` compresses every raised
        script's traceback to a two-frame summary and stashes the
        full string in a lifespan-scoped bounded LRU keyed by
        ``exc_id`` (``"exc_1"``, ``"exc_2"``, …). The tool result
        for the failed ``run_script`` includes the ``exc_id``; the
        agent passes it back here from a subsequent ``run_script``
        to retrieve the full text.

        Raises a fxtr-RPC ``LookupError`` if the id was never
        stored or has been evicted from the LRU. The sandbox-side
        builtin re-raises a Python :class:`LookupError` to the
        agent's script.
        """
        ...

    async def retrieve_nix_entity(self, name: str, nix_location: str) -> tuple[FxtrID, str]:
        """Materialize a nix-declared DATA entity and bridge it in.

        Resolves ``<nix_location>.<name>`` against the agent's workdir
        flake (the **bare workdir path** — git-form inside a repo,
        non-frozen, ``path:`` otherwise): evals the entity's metadata
        (validating it's entity-shaped ``{ type, layer, layerKey }``),
        ``nix build``s its ``.layer``, opens a layered-store reader over
        the result, resolves ``layerKey`` to a :class:`FxtrID` copied into
        the per-script ``script_scope``, and returns it.

        Returns ``(fxtr_id, untracked_notice)``: the bridged entity id plus
        a short notice string (``""`` unless the workdir is a git repo with
        untracked files — those are invisible to nix until ``git add``). The
        sandbox-side builtin prints the notice and returns just the
        ``FxtrID``, which behaves like any other entity ref.

        Raises a fxtr-RPC error (``category="NixEntityBuildFailed"`` for
        missing flake / eval / build failures, ``category="LookupError"``
        for a missing ``layerKey``); the sandbox builtin re-raises
        :class:`NixEntityBuildFailed` / :class:`LookupError`.
        """
        ...

    async def current_commit(self) -> str:
        """Return the workdir's git HEAD commit sha.

        Read-only (``git rev-parse HEAD`` + a clean-tree guard). Used to
        construct ``NixWasmServiceDefn`` / ``set_preset`` args
        correct-by-default. **Errors if the working tree has any
        uncommitted change (including untracked files)** — nix git flakes
        only see tracked files, so pinning HEAD on a dirty tree would ship
        a stale flake — and if the workdir isn't a git repo.

        Raises a fxtr-RPC error (``category="WorkingTreeDirty"`` /
        ``"NotAGitRepo"`` / ``"RuntimeError"``); the sandbox builtin
        re-raises :class:`WorkingTreeDirty` / :class:`RuntimeError`.
        """
        ...


@service_protocol
class McpRunnerProto(Protocol):
    """Guest-side service the host invokes per ``run_script`` tool call.

    One method, ``run()``, executes the configured preamble + script
    and returns a JSON-shaped dict carrying any queued ops the script
    generated (stage 2: always empty). The host applies the queued
    ops as part of its end-of-script mutation pipeline (stage 3+).

    Returns a dict shaped::

        {
            "queued_ops": [<QueuedOp dict>, ...],
        }

    Stage 2 always returns ``{"queued_ops": []}`` — the agent-facing
    builtins exposed (``lookup_entity`` / ``get_service`` /
    ``get_id_for_service``) are read-only / pure-local.
    """

    async def run(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class RunnerConfig:
    """Per-spawn config the host hands the sandbox via ``connect``.

    Plain ``@dataclass(frozen=True)`` (not a fxtr ``DataclassEntity``) —
    same shape as the spike's ``McpRunnerConfig`` and
    ``examples/nested_relay_task``'s ``NestedRelayConfig``, because it
    carries a :class:`ServiceRef` and entity-shaped types can't.

    Fields:
        preamble_src: Source of the user's ``preamble.py`` (raw string).
        script_src: Source of the per-call user script (raw string).
        host_ref: Bridged :class:`McpHostProto` ref; resolved by the
            sandbox at the top of ``run()`` to obtain the host proxy
            used by the agent-facing builtins.
    """

    preamble_src: str
    script_src: str
    host_ref: ServiceRef[McpHostProto]


@dataclass(frozen=True)
class ExportEntityOp:
    """Sandbox queued ``export_entity(slot_key, entity)`` op.

    The sandbox builtin calls ``host.copy_entity_in(slot_key,
    entity_id)`` *before* appending this op so the host has the id
    stashed by the time the worker walks the queue at end-of-script.
    The op record itself only carries ``slot_key`` — the host already
    knows the FxtrID via its ``copied_entities`` dict.
    """

    slot_key: str
    kind: str = "export_entity"


@dataclass(frozen=True)
class ExportServiceOp:
    """Sandbox queued ``export_service(slot_key, stub)`` op. Services
    are workstation-tracked already; the op carries the short_id
    directly (no host roundtrip needed)."""

    slot_key: str
    short_id: str
    kind: str = "export_service"


@dataclass(frozen=True)
class ForgetOp:
    """Sandbox queued ``forget(slot_key)`` op. The host validates
    ``slot_key`` against the local mirror at mutation-build time;
    a missing key raises a structured error before sending."""

    slot_key: str
    kind: str = "forget"


@dataclass(frozen=True)
class ScheduleInvokeOp:
    """Sandbox queued ``schedule_invoke(slot_key, service, lambda)``
    op (stage 4).

    The sandbox's recording proxy captured exactly one method call
    on the ``service`` stub; ``method_name`` is the method, ``args``
    is the recorded positional-args list, ``kwargs`` is the keyword
    arg dict (added in stage 9 alongside typed-dispatch support),
    with each leaf walked through the escape codec:

    - ``FxtrID`` / ``EntityRef`` → ``["entity_handle",
      <entity_arg_handle>]`` where ``<entity_arg_handle>`` is a
      synthesized handle (see :func:`fresh_invoke_arg_handle`).
      The sandbox calls ``host.copy_entity_in(<entity_arg_handle>,
      fxtr_id)`` before appending this op (same path used by
      ``export_entity``, except ``export_entity``'s handle is
      derived from its agent slot_key via :func:`slot_handle`).
      A distinct tag (``"entity_handle"`` rather than
      ``"entity"``) is used because the value is a host-internal
      handle, not a workstation short_id; the mutation builder
      rewrites it to ``["entity", <real_short_id>]`` for the
      wire after ``expose_entity`` returns.
    - :class:`ServiceRef` whose handle is a
      :class:`_RemoteServiceHandleStub` →
      ``["service", handle.short_id]``.
    - :class:`ServiceRef` whose handle is a
      :class:`_OwnedCallbackHandleStub` → either
      ``["callback_handle", slot_key]`` (same-script, slot not yet
      allocated) or ``["callback", slot_n]`` (cross-script, slot
      already committed). The mutation builder rewrites the
      ``callback_handle`` form to ``["callback", N]`` once it
      allocates the slot for the matching
      :class:`CreateCallbackOp`.
    - ``bytes`` → ``["bytes", <base64>]``.
    - lists / tuples → ``[[<recursed items>]]`` (the
      literal-list-escape; one extra wrapper).
    - dicts → walk values; keys are strings.
    - scalars (str / int / float / bool / None) → pass through.
    - anything else → sandbox rejects with a descriptive error.

    The host's mutation builder substitutes each
    ``entity_handle`` placeholder with the workstation short_id
    (from ``expose_entity`` over /rpc) and rewrites the tag to
    ``"entity"`` before the args go on the wire as an
    :class:`InvokeOp`'s ``args`` / ``kwargs`` fields.

    ``entity_arg_handles`` is the de-duplicated list of internal
    handles that the host needs to register over /rpc. The
    sandbox appends each handle exactly once even if the same
    entity appears in args multiple times.
    """

    slot_key: str
    service_short_id: str
    method_name: str
    args: list[Any]
    kwargs: dict[str, Any]
    entity_arg_handles: list[str]
    kind: str = "schedule_invoke"


@dataclass(frozen=True)
class ScheduleCallbackInvokeOp:
    """Sandbox queued ``schedule_invoke`` whose *target* is one of the
    session's own callbacks (an owned-callback :class:`ServiceRef`)
    rather than a workstation-known remote service.

    Mirrors the workstation's ``CallbackInvokeOp`` / ``InvokeOp``
    split: a separate op kind rather than optional fields on
    :class:`ScheduleInvokeOp`. The host's mutation builder emits a
    wire ``CallbackInvokeOp(slot=N, callback_slot=M, ...)``.

    ``callback`` carries the target in the same placeholder grammar
    the arg-side already uses (see :class:`ScheduleInvokeOp`):

    - ``["callback_handle", slot_key]`` — same-script target: the
      callback was created by a ``create_callback`` earlier in this
      script; the host substitutes the slot number it allocates for
      the matching :class:`CreateCallbackOp`. This is what makes
      same-script ``create_callback`` + ``subscribe_to`` work in one
      mutation.
    - ``["callback", slot_n]`` — cross-script target: the callback's
      slot number is already committed; used directly.

    ``args`` / ``kwargs`` / ``entity_arg_handles`` are exactly as in
    :class:`ScheduleInvokeOp`.
    """

    slot_key: str
    callback: list[Any]
    method_name: str
    args: list[Any]
    kwargs: dict[str, Any]
    entity_arg_handles: list[str]
    kind: str = "schedule_callback_invoke"


@dataclass(frozen=True)
class CreateCallbackOp:
    """Sandbox queued ``create_callback(slot_key)`` op (stage 6).

    Records the agent's intent to open a workstation-managed
    callback at ``slot_key``. The host's mutation builder allocates
    a workstation slot number for this op (alongside any
    :class:`ScheduleInvokeOp` slots, in script order), emits a
    wire :class:`CreateCallbackOp(slot=N)` workstation op, and
    writes ``["callback_open", N]`` into ``data_by_key`` at
    ``slot_key``.

    Same-script use: any ``ScheduleInvokeOp`` whose ``args`` carry
    a ``["callback_handle", slot_key]`` placeholder is rewritten by
    the host to ``["callback", N]`` using the allocated slot, so
    the workstation's op-validation pass (which decodes callback
    args against the mutation's pending callbacks) succeeds.
    """

    slot_key: str
    kind: str = "create_callback"


@dataclass(frozen=True)
class ForgetCallbackNotificationOp:
    """Sandbox queued ``forget_callback_notification(slot_key,
    notif_index)`` op (stage 6).

    Records the agent's intent to drop one stored callback
    notification. The host's mutation builder emits a JSON Patch
    ``remove /callback_notifications/<slot_key>/<notif_index>``;
    no workstation-side mutation op is needed (the
    ``callback_notifications`` field is a pure MCP-side
    extension on top of the session state).
    """

    slot_key: str
    notif_index: int
    kind: str = "forget_callback_notification"


@dataclass(frozen=True)
class PresentToUserOp:
    """Sandbox queued ``present_to_user(payload)`` op (stage 10).

    Records the agent's intent to surface ``payload`` to a human
    user watching the session via the workstation-mounted
    ``agent_mcp:{session_id}`` service. The op is **MCP-side only**:
    no JSON Patch, no workstation mutation. The host's log writer
    builds a :class:`PresentToUserOpRecord` entry on the committed
    script's ``ScriptExecutionLogEvent.ops`` list, and the frontend
    sees it via the chain-head subscription (``subscribe``).

    ``payload`` is the user-supplied value walked through
    :func:`_walk_for_wire` — same vocabulary as
    :class:`ScheduleInvokeOp.args` / ``kwargs``, so it can carry
    entity refs, service refs, callback refs, bytes, nested
    lists / dicts, and scalars. ``entity_arg_handles`` is the
    de-duplicated list of internal handles the host needs to
    register over /rpc (same lifetime contract as
    :class:`ScheduleInvokeOp` — entities stay alive across the
    mutation commit because the workstation's invoke worker
    session-pins them, but in this case there's *no* workstation
    op, so the registrations only need to outlive the host-side
    log walker that resolves them; they release on the per-script
    :class:`AsyncExitStack` exit).
    """

    payload: dict[str, Any] | list[Any] | str | int | float | bool | None
    entity_arg_handles: list[str]
    kind: str = "present_to_user"


#: Tagged-union of sandbox-queued ops. Each carries a ``kind``
#: literal so the host can dispatch on the asdict'd wire shape.
QueuedOp = (
    ExportEntityOp
    | ExportServiceOp
    | ForgetOp
    | ScheduleInvokeOp
    | ScheduleCallbackInvokeOp
    | CreateCallbackOp
    | ForgetCallbackNotificationOp
    | PresentToUserOp
)


@dataclass
class RunResult:
    """Wire shape of :meth:`McpRunnerProto.run`'s return value.

    Stage 2: empty queued_ops. Stage 3a populates with
    :data:`QueuedOp` records (asdict'd by the runner before
    return; the host re-tags on dispatch via the ``kind`` field).

    ``stdout`` / ``stderr`` are whatever the preamble + script
    wrote during execution, captured via
    ``contextlib.redirect_stdout`` / ``redirect_stderr`` in the
    script runner. The host fences each into the tool result so the
    agent can use ``print()`` for inline observation. Empty if the
    script didn't write any output on that stream.

    ``error`` is set to a headline-plus-traceback string when the
    preamble or script raised an ``Exception``; ``None`` on
    success. On error, the sandbox zeros out ``queued_ops`` (script
    semantics are atomic) and the host returns the formatted error
    + captured streams as the tool result without sending any
    mutation.
    """

    queued_ops: list[dict[str, Any]] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
