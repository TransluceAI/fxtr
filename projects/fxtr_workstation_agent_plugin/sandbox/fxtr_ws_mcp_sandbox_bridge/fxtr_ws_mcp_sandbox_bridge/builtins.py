"""Builds the per-call ``_fxtr_mcp_builtin`` module's contents.

Read-side builtins (no queue interaction):

- ``async lookup_entity(short_id) -> FxtrID`` — round-trips through
  ``host.lookup_entity``; host asks the workstation for the entity
  and bridges the FxtrID back into the sandbox.
- ``async lookup_service_by_name(name, service_type=UnknownService) -> ServiceRef[T]``
  — resolves a workstation external_name to a typed
  :class:`ServiceRef`. Pass a ``@service_protocol``-decorated
  Protocol as ``service_type`` for typed dispatch; defaults to
  :data:`UnknownService` for the untyped path.
- ``async lookup_entity_by_name(name) -> FxtrID`` — resolves a
  workstation external_name + bridges the FxtrID.
- ``def get_service(short_id, service_type=UnknownService) -> ServiceRef[T]``
  — pure-local wrapper around a workstation short_id with an
  optional type annotation.
- ``def get_id_for_service(service) -> str`` — pure-local; extracts
  the workstation short_id from a remote-service :class:`ServiceRef`.
- ``async retrieve_result(slot_key, result_type=None)`` — reads
  ``host.read_state_slot``, unescapes, and optionally decodes
  against ``result_type`` (a raw annotation or a
  :class:`_ResultTypeSentinel` from :func:`result_type_of`). Raises
  ``LookupError`` for ``invoke_pending``, ``RuntimeError`` for
  ``invoke_error``, ``KeyError`` for missing slot.
- ``async callback_notification_keys(slot_key) -> list[int]`` —
  enumerates received notification indices for a callback slot.
- ``async retrieve_callback_notification(slot_key, notif_index,
  result_type=None)`` — reads + unescapes one stored callback
  payload, optionally decoded against ``result_type``.
- ``async retrieve_nix_entity(name, *, nix_location="fxtr.entities") -> FxtrID``
  — host-side evals + ``nix build``s the DATA entity at
  ``<nix_location>.<name>`` in the workdir flake and bridges it into the
  script. Raises :class:`NixEntityBuildFailed` / :class:`LookupError`.
- ``async current_commit() -> str`` — the workdir's git HEAD sha, for
  building ``NixWasmServiceDefn`` / ``set_preset`` args. Raises
  :class:`WorkingTreeDirty` on a dirty tree / :class:`RuntimeError` if not
  a git repo.
- ``async get_full_exception(exc_id) -> str`` — fetches the full
  ``traceback.format_exception`` string for a previously-raised
  script exception. The agent reads the ``exc_id`` from the
  compressed-traceback line in a failed ``run_script``'s tool
  result. Raises :class:`LookupError` if the id is unknown or has
  been evicted from the host's bounded LRU.
- ``def result_type_of(target_type, method) -> _ResultTypeSentinel[R]``
  — pure-local; returns a phantom-typed sentinel pointing at the
  layer-2 single-field return wrapper of the named Protocol
  method, suitable to pass back into ``retrieve_result`` /
  ``retrieve_callback_notification`` as ``result_type``. Pass the
  method as a *callable* (e.g. ``MyProto.echo``) so pyright can
  infer the return type ``R`` from its signature.

Write-side builtins (append to per-call queue):

- ``async export_entity(slot_key, entity)`` — copies the entity to
  the host via ``copy_entity_in``; appends ``ExportEntityOp``.
- ``def export_service(slot_key, service)`` — pure-local; appends
  ``ExportServiceOp``. The argument must be a remote-service
  :class:`ServiceRef` (one built by ``get_service`` /
  ``lookup_service_by_name`` / decoded from an
  ``["exported_service", short_id]`` slot).
- ``def forget(slot_key)`` — pure-local; appends ``ForgetOp``. For
  a slot currently holding a ``["callback_open", N]`` value, the
  host's mutation builder additionally emits a workstation-side
  ``CloseCallbackOp(slot=N)`` so the receive side closes.
- ``async schedule_invoke(slot_key, service, action_lambda)`` —
  records the method call by passing a recording proxy to
  ``action_lambda``; type-encodes args (typed services) or walks
  them through the wire codec (untyped); copies any entity-typed
  args to the host; appends ``ScheduleInvokeOp`` with args /
  kwargs. Owned-callback :class:`ServiceRef` args ride as
  ``["callback_handle", slot_key]`` (same-script) or
  ``["callback", slot_n]`` (cross-script).
- ``def create_callback(slot_key) -> ServiceRef[WorkstationSessionEventInbox]``
  — pure-local; appends ``CreateCallbackOp``. Returns a typed
  :class:`ServiceRef` whose handle records the originating
  slot_key; the agent passes it into a same-script
  ``schedule_invoke`` arg position to register the callback.
- ``def forget_callback_notification(slot_key, notif_index)`` —
  pure-local; appends ``ForgetCallbackNotificationOp``.

Re-exported names (referenced by user scripts, not called):

- ``NixWasmServiceDefn`` / ``NixContainerServiceDefn`` —
  sandbox-constructible spawn references (wasm / container kind).
- ``NixDispatcherProto`` — agent-side renderer-dispatcher handle;
  ``replace_service_type`` a looked-up dispatcher ref to it and call
  ``set_preset(git_commit, flake_attr)`` to push a UI preset by git ref.
- ``WorkstationSessionEventInbox`` / ``Subscriber`` / ``UnsubscribeHandle``
  / ``EventSource`` / ``ServiceRef`` / ``UnknownService``.
- ``NixEntityBuildFailed`` / ``WorkingTreeDirty`` exceptions.
"""

from __future__ import annotations

import base64
import inspect
from collections.abc import Awaitable, Mapping
from dataclasses import asdict
from typing import Any, Callable, Concatenate, get_args, get_origin

import anyio
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import EntityRef, ServiceRef, ServiceTypeAnnotation
from fxtr2.leases.leased_cap_ir import IRException
from fxtr2.services.service_cap_common import (
    USE_REMOTE_DEFAULT,
    UnknownService,
    _UseRemoteDefault,  # pyright: ignore[reportPrivateUsage]
)
from fxtr2.services.service_dialect import (
    FxtrServicesDefaultDialect,
    decode_from_service_data,
    get_basic_decoder,
    get_basic_encoder,
)
from fxtr2.services.subscription_types import EventSource, Subscriber, UnsubscribeHandle

from .protocols import (
    CreateCallbackOp,
    ExportEntityOp,
    ExportServiceOp,
    ForgetCallbackNotificationOp,
    ForgetOp,
    McpHostProto,
    NixContainerServiceDefn,
    NixDispatcherProto,
    NixEntityBuildFailed,
    NixWasmServiceDefn,
    PresentToUserOp,
    QueuedOp,
    ScheduleCallbackInvokeOp,
    ScheduleInvokeOp,
    WorkingTreeDirty,
    WorkstationSessionEventInbox,
    _make_owned_callback_ref,
    _make_remote_service_ref,
    _OwnedCallbackHandleStub,
    _RemoteServiceHandleStub,
    _ResultTypeSentinel,
    fresh_invoke_arg_handle,
    slot_handle,
)

# Suppress the `USE_REMOTE_DEFAULT` constant import as "unused" — it's
# referenced indirectly through the dialect path; the explicit import
# makes the dependency visible.
_ = USE_REMOTE_DEFAULT


class _RecordingProxy:
    """Records exactly one method call. Each attribute access
    returns a recorder that captures ``(name, args, kwargs)`` into
    ``_calls`` and returns a sentinel.

    Used by ``schedule_invoke`` to capture the method + args from
    the user's lambda ``lambda svc: svc.foo(bar, baz)``. The
    sentinel return value is checked against the lambda's actual
    return value; mismatch means the user didn't simply ``return``
    the call expression.
    """

    __slots__ = ("_calls", "_sentinel")

    def __init__(self) -> None:
        self._calls: list[tuple[str, tuple, dict]] = []
        self._sentinel = object()

    def __getattr__(self, name: str) -> Callable[..., object]:
        def _record(*args: Any, **kwargs: Any) -> object:
            self._calls.append((name, args, kwargs))
            return self._sentinel

        return _record


def build_builtins(
    host: McpHostProto,
    queued_ops: list[dict[str, Any]],
) -> Mapping[str, Any]:
    """Construct the closure mapping installed as ``_fxtr_mcp_builtin``
    for one ``run_script`` invocation.

    Args:
        host: Already-resolved :class:`McpHostProto` proxy. Read-side
            builtins call ``host.lookup_entity``; write-side
            ``export_entity`` calls ``host.copy_entity_in``.
        queued_ops: Per-call collector. Closures append asdict'd
            :data:`QueuedOp` records to this list as the script runs.
            Returned to the host via :class:`RunResult.queued_ops`.

    Returns:
        A name → value mapping suitable for
        :func:`script_runner.run_preamble_and_script`'s ``builtins``
        argument.
    """

    used_slot_keys: set[str] = set()

    def _check_slot_key(name: str, slot_key: str) -> None:
        # Defensive runtime check — sandbox code is user-driven so
        # the annotated type doesn't guarantee the runtime type;
        # the pyright ignores suppress the "unnecessary isinstance"
        # (on the check) / "unreachable code" (on the body)
        # diagnostics this pattern naturally trips.
        if not isinstance(slot_key, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(  # pyright: ignore[reportUnreachable]
                f"{name}: slot_key must be a string, got {type(slot_key).__name__}"
            )
        if slot_key in used_slot_keys:
            raise ValueError(
                f"{name}: slot_key {slot_key!r} is already used in this "
                "script. Each agent-facing slot key must be unique per "
                "run_script call."
            )

    def _enqueue(op: QueuedOp) -> None:
        queued_ops.append(asdict(op))

    async def lookup_entity(short_id: str) -> FxtrID:
        if isinstance(short_id, FxtrID):
            raise TypeError(
                "lookup_entity takes a workstation short_id string "
                f"(e.g. 'dd3gz2jk'), not a FxtrID. Got: {short_id!r}. "
                "lookup_entity *resolves* a short_id *into* a FxtrID; "
                "it does not load entity data. If you already have a "
                "FxtrID and want to load the entity, do "
                "``await EntityRef(entity_id=fid, target_type=T).load()`` "
                "directly (use ``UnknownEntity`` for ``target_type`` "
                "if you don't know the type)."
            )
        if not isinstance(short_id, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(  # pyright: ignore[reportUnreachable]
                f"lookup_entity: short_id must be a string, got {type(short_id).__name__}"
            )
        return await host.lookup_entity(short_id)

    async def lookup_service_by_name[
        T
    ](
        name: str,
        service_type: ServiceTypeAnnotation[T] = UnknownService,  # type: ignore[assignment]
    ) -> ServiceRef[T]:
        """Resolve a workstation external_name to a typed
        :class:`ServiceRef`. The agent passes the ref to
        ``schedule_invoke`` / ``export_service`` like any other.

        ``service_type`` defaults to :data:`UnknownService` for
        untyped callsites; pass a ``@service_protocol``-decorated
        Protocol for typed dispatch.

        Raises if the name isn't bound to an active service
        registration. (If it's bound to an entity instead, the
        workstation raises too — external names live in one
        namespace.)
        """
        if not isinstance(name, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(  # pyright: ignore[reportUnreachable]
                f"lookup_service_by_name: name must be a string, got {type(name).__name__}"
            )
        short_id = await host.lookup_short_id_by_external_name(name)
        return _make_remote_service_ref(short_id, service_type)

    async def lookup_entity_by_name(name: str) -> FxtrID:
        """Resolve a workstation external_name to a :class:`FxtrID`.

        Performs two RPCs: ``lookup_short_id_by_external_name``
        followed by ``lookup_entity(short_id)`` to bridge the
        FxtrID into the per-call scope. Raises if the name isn't
        bound to an active entity registration.
        """
        if not isinstance(name, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(  # pyright: ignore[reportUnreachable]
                f"lookup_entity_by_name: name must be a string, got {type(name).__name__}"
            )
        short_id = await host.lookup_short_id_by_external_name(name)
        return await host.lookup_entity(short_id)

    def get_service[
        T
    ](
        short_id: str,
        service_type: ServiceTypeAnnotation[T] = UnknownService,  # type: ignore[assignment]
    ) -> ServiceRef[T]:
        """Build a typed :class:`ServiceRef` for a workstation short_id.

        ``service_type`` defaults to :data:`UnknownService`; pass a
        ``@service_protocol``-decorated Protocol for typed dispatch.
        """
        if not isinstance(short_id, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(  # pyright: ignore[reportUnreachable]
                f"get_service: short_id must be a string, got {type(short_id).__name__}"
            )
        return _make_remote_service_ref(short_id, service_type)

    def get_id_for_service(service: ServiceRef[Any]) -> str:
        """Return the workstation short_id of a remote-service
        :class:`ServiceRef`. Errors if the ref isn't a remote service
        (e.g. an owned-callback ref doesn't have a short_id)."""
        if not isinstance(service, ServiceRef):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(  # pyright: ignore[reportUnreachable]
                f"get_id_for_service: argument must be a ServiceRef, "
                f"got {type(service).__name__}"
            )
        handle = service.handle
        if not isinstance(handle, _RemoteServiceHandleStub):
            raise TypeError(
                "get_id_for_service: argument must be a remote-service "
                "ServiceRef (built via get_service / lookup_service_by_name / "
                "exported_service retrieval); got a ref whose handle is a "
                f"{type(handle).__name__}."
            )
        return handle.short_id

    async def export_entity(slot_key: str, entity: FxtrID | EntityRef[Any]) -> None:
        _check_slot_key("export_entity", slot_key)
        if isinstance(entity, EntityRef):
            entity_id = entity.entity_id
        elif isinstance(entity, FxtrID):
            entity_id = entity
        else:
            raise TypeError(  # pyright: ignore[reportUnreachable]
                f"export_entity: entity must be a FxtrID or EntityRef, "
                f"got {type(entity).__name__}"
            )
        # Round-trip first; if the host rejects (e.g. duplicate
        # handle, which would only happen on a bug since we
        # checked our own slot_key set), surface that error before
        # marking the slot used locally.
        await host.copy_entity_in(slot_handle(slot_key), entity_id)
        used_slot_keys.add(slot_key)
        _enqueue(ExportEntityOp(slot_key=slot_key))

    def export_service(slot_key: str, service: ServiceRef[Any]) -> None:
        _check_slot_key("export_service", slot_key)
        if not isinstance(service, ServiceRef):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(  # pyright: ignore[reportUnreachable]
                f"export_service: argument must be a ServiceRef, " f"got {type(service).__name__}"
            )
        handle = service.handle
        if not isinstance(handle, _RemoteServiceHandleStub):
            raise TypeError(
                "export_service: argument must be a remote-service "
                "ServiceRef (built via get_service / lookup_service_by_name / "
                "exported_service retrieval); got a ref whose handle is a "
                f"{type(handle).__name__}."
            )
        used_slot_keys.add(slot_key)
        _enqueue(ExportServiceOp(slot_key=slot_key, short_id=handle.short_id))

    def forget(slot_key: str) -> None:
        _check_slot_key("forget", slot_key)
        used_slot_keys.add(slot_key)
        _enqueue(ForgetOp(slot_key=slot_key))

    async def schedule_invoke(
        slot_key: str,
        service: ServiceRef[Any],
        action: Callable[[Any], Any],
    ) -> None:
        """Schedule a method call against ``service`` and bind the
        result to ``slot_key``.

        Usage::

            await schedule_invoke(
                "my_result",
                get_service("s5", MyServiceProto),
                lambda svc: svc.echo(42, opt=True),
            )

        The ``action`` callable is invoked with a recording proxy
        that captures exactly one method call. The proxy returns a
        sentinel; ``action`` must return that sentinel (i.e. it
        must end with ``return svc.method(...)`` — no transforms
        in between).

        Typed dispatch path (``service.target_type`` is a
        ``@service_protocol`` Protocol):
            Args + kwargs are bound to the method's signature,
            type-encoded through the dialect's
            :func:`get_basic_encoder` (each ``EntityRef[T]`` →
            ``FxtrID``, each ``ServiceRef[T]`` →
            ``ServiceRef[UnknownService]``, etc.), then walked
            through the wire codec.

        Untyped dispatch path (``service.target_type is UnknownService``):
            Args + kwargs are walked directly through the wire codec
            without dialect-side conversion. ``FxtrID`` / ``EntityRef``
            args still round-trip through ``host.copy_entity_in`` for
            the wire.

        The *target* may also be an owned-callback ref (from
        :func:`create_callback`, or :func:`retrieve_result` of a
        ``callback_open`` slot) — the host emits a callback-slot-
        targeted invoke (``CallbackInvokeOp``), which is how the
        agent calls ``subscribe_to`` / ``notify`` / ``listening`` on
        its own inboxes. Same-script ``create_callback`` +
        ``schedule_invoke`` on the fresh ref works (one mutation).
        """
        _check_slot_key("schedule_invoke", slot_key)
        if not isinstance(service, ServiceRef):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(  # pyright: ignore[reportUnreachable]
                f"schedule_invoke: service must be a ServiceRef, " f"got {type(service).__name__}"
            )
        handle = service.handle
        if not isinstance(handle, (_RemoteServiceHandleStub, _OwnedCallbackHandleStub)):
            raise TypeError(
                "schedule_invoke: service must be a remote-service "
                "ServiceRef (built via get_service / lookup_service_by_name) "
                "or an owned-callback ServiceRef (from create_callback / "
                "retrieve_result of a callback slot); got a ref whose handle "
                f"is a {type(handle).__name__}."
            )
        proxy = _RecordingProxy()
        result = action(proxy)
        if result is not proxy._sentinel:
            raise ValueError(
                "schedule_invoke: action must directly return the result "
                "of one method call on the proxy (got something else; "
                "did you transform the return value?)"
            )
        if len(proxy._calls) != 1:
            raise ValueError(
                f"schedule_invoke: action must call exactly one method "
                f"on the proxy; got {len(proxy._calls)} calls"
            )
        method_name, raw_args, raw_kwargs = proxy._calls[0]

        # Walk args, copying entity refs out via host.copy_entity_in.
        # Each unique entity gets a fresh synthesized internal handle
        # which the mutation builder will substitute for the real
        # workstation short_id once expose_entity returns.
        entity_arg_handles: list[str] = []
        entity_handle_by_id: dict[FxtrID, str] = {}

        async def _maybe_register_entity(entity_id: FxtrID) -> str:
            existing = entity_handle_by_id.get(entity_id)
            if existing is None:
                fresh = fresh_invoke_arg_handle()
                entity_handle_by_id[entity_id] = fresh
                entity_arg_handles.append(fresh)
                await host.copy_entity_in(fresh, entity_id)
                return fresh
            return existing

        # Typed vs untyped split: dial up the typed encoder only for
        # protocols decorated with @service_protocol. UnknownService
        # (the default) → run the user's raw args/kwargs straight
        # through the wire walker (today's behavior, now with kwargs
        # support).
        if service.target_type is UnknownService:
            args_for_wire: list[Any] = list(raw_args)
            kwargs_for_wire: dict[str, Any] = dict(raw_kwargs)
        else:
            args_for_wire, kwargs_for_wire = _typed_encode_invoke_args(
                service.target_type, method_name, raw_args, raw_kwargs
            )

        encoded_args = [await _walk_for_wire(a, _maybe_register_entity) for a in args_for_wire]
        encoded_kwargs = {
            k: await _walk_for_wire(v, _maybe_register_entity) for k, v in kwargs_for_wire.items()
        }
        used_slot_keys.add(slot_key)
        if isinstance(handle, _OwnedCallbackHandleStub):
            # Target is one of the session's own callbacks: queue the
            # callback-invoke variant. The target rides in the same
            # placeholder grammar arg-side callback refs use
            # (``["callback_handle", k]`` same-script /
            # ``["callback", N]`` cross-script).
            _enqueue(
                ScheduleCallbackInvokeOp(
                    slot_key=slot_key,
                    callback=_service_ref_to_wire(service),
                    method_name=method_name,
                    args=encoded_args,
                    kwargs=encoded_kwargs,
                    entity_arg_handles=entity_arg_handles,
                )
            )
        else:
            _enqueue(
                ScheduleInvokeOp(
                    slot_key=slot_key,
                    service_short_id=handle.short_id,
                    method_name=method_name,
                    args=encoded_args,
                    kwargs=encoded_kwargs,
                    entity_arg_handles=entity_arg_handles,
                )
            )

    async def retrieve_result(slot_key: str, result_type: Any = None) -> Any:
        """Read the slot's current value via ``host.read_state_slot``
        and decode for sandbox use.

        Two-stage flow:
            1. Resolve the slot's wire-tagged value to canonical
               :data:`FxtrServiceData` (or raise terminal errors for
               ``invoke_pending`` / ``invoke_error``).
            2. If ``result_type`` is None, return the canonical
               value (with a slot-tag-specific default applied for
               ``callback_open`` → ``ServiceRef[WorkstationSessionEventInbox]``).
               Otherwise decode the canonical value against
               ``result_type``.

        ``result_type`` accepts:
            - ``None``: return canonical FxtrServiceData (with the
              ``callback_open`` slot-default).
            - A raw type annotation
              (``EntityRef[Note]`` / ``ServiceRef[MyProto]`` /
              ``list[int]`` / ...): decoded via
              :func:`decode_from_service_data`.
            - A :class:`_ResultTypeSentinel` from
              :func:`result_type_of`: decoded via the layer-2
              single-field return wrapper.

        Stage-1 outputs by tag:
            ``["exported_entity", short_id]``  → resolved :class:`FxtrID`.
            ``["exported_service", short_id]`` → ``ServiceRef[UnknownService]``
                with a :class:`_RemoteServiceHandleStub`.
            ``["callback_open", N]``           → ``ServiceRef[UnknownService]``
                with a :class:`_OwnedCallbackHandleStub(slot_n=N)`. The
                default ``result_type`` (when not supplied) is
                ``ServiceRef[WorkstationSessionEventInbox]`` so the agent-visible
                shape stays typed.
            ``["invoke_result", payload]``     → unescaped FxtrServiceData
                tree.

        Raises:
            ``KeyError(slot_key)`` if the slot doesn't exist.
            ``LookupError(slot_key)`` for ``["invoke_pending", n]``.
            ``RuntimeError(message)`` for ``["invoke_error", msg]``.
        """
        if not isinstance(
            slot_key, str
        ):  # pyright: ignore[reportUnnecessaryIsInstance,reportUnreachable]
            raise TypeError(
                f"retrieve_result: slot_key must be a string, got {type(slot_key).__name__}"
            )
        val = await host.read_state_slot(slot_key)
        if val is None:
            raise KeyError(slot_key)
        fsd = await _resolve_slot_to_fxtr_service_data(val, slot_key, lookup_entity)
        effective_type = result_type if result_type is not None else _slot_default_result_type(val)
        return _apply_result_type(fsd, effective_type)

    def create_callback(slot_key: str) -> ServiceRef[WorkstationSessionEventInbox]:
        """Open a workstation-managed callback at ``slot_key``.

        Records a queued ``create_callback`` op; the host's
        mutation builder allocates a slot number, emits the
        workstation-side ``CreateCallbackOp(slot=N)``, and writes
        ``["callback_open", N]`` into the session's
        ``data_by_key`` at ``slot_key``.

        Returns a :class:`ServiceRef` with target type
        :class:`WorkstationSessionEventInbox` and a same-script-shape
        :class:`_OwnedCallbackHandleStub`. The agent passes the ref
        into a same-script ``schedule_invoke`` arg position; the
        wire walker emits ``["callback_handle", slot_key]`` and the
        host's mutation builder rewrites it to ``["callback", N]``
        once the slot is allocated.

        Cross-script: in a later ``run_script`` call,
        ``retrieve_result(slot_key)`` returns a
        :class:`ServiceRef[WorkstationSessionEventInbox]` with the
        cross-script-shape handle (``slot_n=N``).
        """
        _check_slot_key("create_callback", slot_key)
        used_slot_keys.add(slot_key)
        _enqueue(CreateCallbackOp(slot_key=slot_key))
        return _make_owned_callback_ref(slot_key=slot_key)

    async def callback_notification_keys(slot_key: str) -> list[int]:
        """Return the sorted list of ``notif_index`` values for
        notifications received against the callback at
        ``slot_key``. Empty list if the slot has no stored
        notifications.
        """
        if not isinstance(
            slot_key, str
        ):  # pyright: ignore[reportUnnecessaryIsInstance,reportUnreachable]
            raise TypeError(
                f"callback_notification_keys: slot_key must be a string, "
                f"got {type(slot_key).__name__}"
            )
        return await host.list_callback_notification_indices(slot_key)

    async def retrieve_callback_notification(
        slot_key: str, notif_index: int, result_type: Any = None
    ) -> Any:
        """Retrieve the stored payload for one callback notification.

        Reads the wire-escaped payload via
        ``host.read_callback_notification`` and decodes it through
        the same two-stage pipeline ``retrieve_result`` uses for
        ``invoke_result`` payloads. No slot-tag default (the payload
        is always invoke-result-shaped — pass ``result_type``
        explicitly if you want typed decoding).

        Raises ``KeyError((slot_key, notif_index))`` if the
        notification isn't stored (either was never received or
        has been forgotten).
        """
        if not isinstance(
            slot_key, str
        ):  # pyright: ignore[reportUnnecessaryIsInstance,reportUnreachable]
            raise TypeError(
                f"retrieve_callback_notification: slot_key must be a string, "
                f"got {type(slot_key).__name__}"
            )
        if not isinstance(
            notif_index, int
        ):  # pyright: ignore[reportUnnecessaryIsInstance,reportUnreachable]
            raise TypeError(
                f"retrieve_callback_notification: notif_index must be an int, "
                f"got {type(notif_index).__name__}"
            )
        try:
            payload = await host.read_callback_notification(slot_key, notif_index)
        except IRException as exc:
            if exc.category == "KeyError":
                raise KeyError((slot_key, notif_index)) from None
            raise
        fsd = await _unescape_payload(payload, lookup_entity)
        return _apply_result_type(fsd, result_type)

    # Tracks whether the workdir's untracked-files notice has already been
    # printed this script, so repeat retrievals don't re-print it.
    _untracked_notice_shown = [False]

    async def retrieve_nix_entity(name: str, *, nix_location: str = "fxtr.entities") -> FxtrID:
        """Materialize a nix-declared DATA entity and return its
        :class:`FxtrID`.

        Resolves ``<nix_location>.<name>`` against the agent's workdir flake
        (built on demand; nix's eval cache makes repeats cheap), bridging the
        entity into this script's scope. The returned ``FxtrID`` behaves like
        any other entity ref — load it with
        ``await EntityRef(fid, MyType).load()``, pass it to ``schedule_invoke``
        / ``export_entity`` / ``present_to_user``, etc.

        ``nix_location`` is keyword-only (default ``"fxtr.entities"``); any
        attr that evaluates to the entity shape ``{ type, layer, layerKey }``
        is accepted.

        Raises :class:`NixEntityBuildFailed` if the flake is missing / doesn't
        evaluate / the layer build fails (the nix stderr is in the message),
        or :class:`LookupError` if the built layer has no such ``layerKey``.
        """
        if not isinstance(name, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(  # pyright: ignore[reportUnreachable]
                f"retrieve_nix_entity: name must be a string, got {type(name).__name__}"
            )
        if not isinstance(nix_location, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(  # pyright: ignore[reportUnreachable]
                f"retrieve_nix_entity: nix_location must be a string, "
                f"got {type(nix_location).__name__}"
            )
        try:
            fxtr_id, notice = await host.retrieve_nix_entity(name, nix_location)
        except IRException as exc:
            if exc.category == "NixEntityBuildFailed":
                raise NixEntityBuildFailed(exc.message) from None
            if exc.category == "LookupError":
                raise LookupError(exc.message) from None
            raise
        if notice and not _untracked_notice_shown[0]:
            _untracked_notice_shown[0] = True
            print(notice)
        return fxtr_id

    async def current_commit() -> str:
        """Return the workdir's git HEAD commit sha.

        Use it to construct ``NixWasmServiceDefn`` / ``set_preset`` args
        correct-by-default, e.g.
        ``NixWasmServiceDefn(git_commit=current_commit(), flake_attr=...)``.

        Raises :class:`WorkingTreeDirty` if the working tree has any
        uncommitted change (including untracked files) — nix builds git
        flakes from the committed tree, so commit (and ``git add`` new
        files) first — or :class:`RuntimeError` if the workdir isn't a git
        repo / has no commits.
        """
        try:
            return await host.current_commit()
        except IRException as exc:
            if exc.category == "WorkingTreeDirty":
                raise WorkingTreeDirty(exc.message) from None
            if exc.category in ("NotAGitRepo", "RuntimeError"):
                raise RuntimeError(exc.message) from None
            raise

    async def get_full_exception(exc_id: str) -> str:
        """Return the full ``traceback.format_exception`` string for
        a previously-raised script exception.

        When a ``run_script`` raises, the MCP's host-side error
        formatter compresses the traceback to a two-frame summary
        (the deepest user-code frame plus the innermost frame
        where the exception came out) and surfaces an ``exc_id``
        in the tool result. Call this builtin in a subsequent
        ``run_script`` with that ``exc_id`` to fetch the full
        text.

        Raises :class:`LookupError` if the id is unknown — either
        it was never stored or it has been evicted from the
        bounded LRU (default capacity 20; oldest entries fall off
        first). Copy the returned string out of this call if you
        need to keep it longer than the LRU might.
        """
        if not isinstance(exc_id, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(  # pyright: ignore[reportUnreachable]
                f"get_full_exception: exc_id must be a string, got {type(exc_id).__name__}"
            )
        try:
            return await host.get_full_exception(exc_id)
        except IRException as exc:
            if exc.category == "LookupError":
                raise LookupError(exc.message) from None
            raise

    def forget_callback_notification(slot_key: str, notif_index: int) -> None:
        """Drop one stored callback notification.

        Records a queued ``forget_callback_notification`` op; the
        host's mutation builder emits a JSON Patch ``remove`` for
        ``/callback_notifications/<slot_key>/<notif_index>``. No
        workstation-side mutation op is needed — the
        ``callback_notifications`` field is a MCP-side extension.

        Each ``forget_callback_notification`` call is a distinct
        op even when ``slot_key`` repeats, so the same-script
        slot-key-uniqueness check is *not* applied here.
        """
        if not isinstance(
            slot_key, str
        ):  # pyright: ignore[reportUnnecessaryIsInstance,reportUnreachable]
            raise TypeError(
                f"forget_callback_notification: slot_key must be a string, "
                f"got {type(slot_key).__name__}"
            )
        if not isinstance(
            notif_index, int
        ):  # pyright: ignore[reportUnnecessaryIsInstance,reportUnreachable]
            raise TypeError(
                f"forget_callback_notification: notif_index must be an int, "
                f"got {type(notif_index).__name__}"
            )
        _enqueue(ForgetCallbackNotificationOp(slot_key=slot_key, notif_index=notif_index))

    async def present_to_user(payload: Any) -> None:
        """Surface ``payload`` to a human user watching the session.

        Records a queued ``present_to_user`` op. The host's log
        writer turns it into a ``PresentToUserOpRecord`` entry on
        the committed script's ``ScriptExecutionLogEvent.ops`` list,
        which is what watchers of ``agent_mcp:{session_id}`` see.

        ``payload`` accepts the same vocabulary as a
        ``schedule_invoke`` arg: scalars / ``None`` / lists / dicts
        of strings to walked values, ``bytes``, :class:`FxtrID` /
        :class:`EntityRef` (round-tripped through
        ``host.copy_entity_in`` for the wire), and :class:`ServiceRef`
        (remote-service or owned-callback handle).

        No workstation-side mutation is emitted — the op is
        MCP-side only.
        """
        entity_arg_handles: list[str] = []
        entity_handle_by_id: dict[FxtrID, str] = {}

        async def _maybe_register_entity(entity_id: FxtrID) -> str:
            existing = entity_handle_by_id.get(entity_id)
            if existing is None:
                fresh = fresh_invoke_arg_handle()
                entity_handle_by_id[entity_id] = fresh
                entity_arg_handles.append(fresh)
                await host.copy_entity_in(fresh, entity_id)
                return fresh
            return existing

        walked = await _walk_for_wire(payload, _maybe_register_entity)
        _enqueue(PresentToUserOp(payload=walked, entity_arg_handles=entity_arg_handles))

    return {
        "lookup_entity": lookup_entity,
        "lookup_service_by_name": lookup_service_by_name,
        "lookup_entity_by_name": lookup_entity_by_name,
        "get_service": get_service,
        "get_id_for_service": get_id_for_service,
        "export_entity": export_entity,
        "export_service": export_service,
        "forget": forget,
        "schedule_invoke": schedule_invoke,
        "retrieve_result": retrieve_result,
        "create_callback": create_callback,
        "callback_notification_keys": callback_notification_keys,
        "retrieve_callback_notification": retrieve_callback_notification,
        "forget_callback_notification": forget_callback_notification,
        "present_to_user": present_to_user,
        "retrieve_nix_entity": retrieve_nix_entity,
        "current_commit": current_commit,
        "get_full_exception": get_full_exception,
        "result_type_of": result_type_of,
        # Re-exports so user code can refer to these by name from
        # scripts and isinstance-check / annotate against them.
        "ServiceRef": ServiceRef,
        "WorkstationSessionEventInbox": WorkstationSessionEventInbox,
        # fxtr2 subscription primitives (imported from
        # fxtr2.services.subscription_types — fxtr2-portable ships
        # them): annotate subscriber impls / event-source refs, and
        # re-type looked-up services that subclass (or are
        # wire-compatible with) `EventSource`.
        "Subscriber": Subscriber,
        "UnsubscribeHandle": UnsubscribeHandle,
        "EventSource": EventSource,
        "UnknownService": UnknownService,
        # Sandbox-constructible spawn references (built in-script, `.store()`d,
        # passed to spawner.spawn) — wire-identical to the canonical
        # fxtr_sandbox_types.entities types of the same names.
        "NixWasmServiceDefn": NixWasmServiceDefn,
        "NixContainerServiceDefn": NixContainerServiceDefn,
        # Agent-side handle for the renderer dispatcher: re-type a
        # looked-up dispatcher ServiceRef via replace_service_type and call
        # set_preset(git_commit, flake_attr) to push a UI preset by git ref.
        # Wire-compatible (method-name dispatch) with the workstation's
        # fxtr_workstation.dynamic_dispatch.NixDispatcherProto.
        "NixDispatcherProto": NixDispatcherProto,
        # Agent-facing exceptions for the nix-entity / commit builtins, so
        # scripts can `except NixEntityBuildFailed` / `except WorkingTreeDirty`.
        "NixEntityBuildFailed": NixEntityBuildFailed,
        "WorkingTreeDirty": WorkingTreeDirty,
    }


# --- Typed-arg encoding for schedule_invoke ---------------------------


def _typed_encode_invoke_args(
    target_type: Any,
    method_name: str,
    raw_args: tuple,
    raw_kwargs: dict,
) -> tuple[list[Any], dict[str, Any]]:
    """Run the typed-service dialect against one invoke's args.

    Looks up the protocol's per-method ``_MethodSpec`` from the
    ``_fxtr_service_protocol_metadata`` attribute set by
    :func:`fxtr2.services.typed_service.service_protocol`, builds
    the parameterized args wrapper, encodes the user's args via
    :func:`get_basic_encoder`, strips :data:`USE_REMOTE_DEFAULT`
    entries, and splits the result through
    :class:`inspect.BoundArguments` into the natural positional /
    keyword wire form.

    Returns ``(args_for_wire, kwargs_for_wire)`` where each leaf is
    a :data:`FxtrServiceData`-vocabulary value
    (``FxtrID`` / ``ServiceRef[UnknownService]`` / scalar /
    list / dict / bytes), suitable for handing to
    :func:`_walk_for_wire`.
    """
    proto = get_origin(target_type) or target_type
    meta = getattr(proto, "_fxtr_service_protocol_metadata", None)
    if meta is None:
        raise NotImplementedError(
            f"schedule_invoke: service.target_type {target_type!r} is not "
            "decorated with @service_protocol; cannot dispatch typed args."
        )
    spec = meta.method_specs.get(method_name)
    if spec is None:
        raise IRException(
            message=(
                f"schedule_invoke: method {method_name!r} is not declared on "
                f"{getattr(proto, '__name__', proto)!r}; available: "
                f"{sorted(meta.method_specs.keys())!r}."
            ),
            category="UnknownMethod",
        )

    parameterization = get_args(target_type) or None
    args_cls = (
        spec.args_wrapper_cls[*parameterization]  # type: ignore[index]
        if parameterization
        else spec.args_wrapper_cls
    )
    encoder = get_basic_encoder(args_cls, meta.dialect)

    # `inspect.Signature.bind` against the Protocol method's full
    # signature including `self`. We supply `None` for `self`; the
    # method body itself never runs here, so the value is inert.
    try:
        bound = spec.signature.bind(None, *raw_args, **raw_kwargs)
    except TypeError as exc:
        raise IRException(
            message=f"schedule_invoke: failed to bind args for {method_name}: {exc}",
            category="MalformedArgs",
        ) from exc
    bound.apply_defaults()
    wrapper_kwargs = {k: v for k, v in bound.arguments.items() if k != "self"}
    try:
        instance = args_cls(**wrapper_kwargs)
    except TypeError as exc:
        raise IRException(
            message=f"schedule_invoke: failed to build args wrapper for {method_name}: {exc}",
            category="MalformedArgs",
        ) from exc
    encoded = encoder.encode(instance)
    encoded = {k: v for k, v in encoded.items() if not isinstance(v, _UseRemoteDefault)}

    new_bound = inspect.BoundArguments(spec.signature_no_self, encoded)  # type: ignore[arg-type]
    return list(new_bound.args), dict(new_bound.kwargs)


# --- Wire walker ------------------------------------------------------


async def _walk_for_wire(
    value: Any,
    register_entity: Callable[[FxtrID], Any],
) -> Any:
    """Walk a Python value, emitting the bracketed wire form.

    Accepts the union of (raw user-supplied types) ∪
    (dialect-output vocabulary):

    - ``FxtrID`` → ``["entity_handle", <handle>]`` (after
      ``register_entity``).
    - ``EntityRef`` → same (untyped path only — the dialect strips
      to FxtrID on its own).
    - :class:`ServiceRef` → dispatch on handle subtype:
        - :class:`_RemoteServiceHandleStub` → ``["service", short_id]``.
        - :class:`_OwnedCallbackHandleStub(slot_key=...)`` →
          ``["callback_handle", slot_key]``.
        - :class:`_OwnedCallbackHandleStub(slot_n=N)`` →
          ``["callback", N]``.
    - ``bytes`` → ``["bytes", <b64>]``.
    - lists / tuples → ``[[<recursed items>]]``.
    - dicts → recurse over values (keys must be strings).
    - scalars / None → passthrough.
    - anything else → :exc:`TypeError`.
    """
    if isinstance(value, FxtrID):
        handle = await register_entity(value)
        return ["entity_handle", handle]
    if isinstance(value, EntityRef):
        handle = await register_entity(value.entity_id)
        return ["entity_handle", handle]
    if isinstance(value, ServiceRef):
        return _service_ref_to_wire(value)
    if isinstance(value, bytes):
        return ["bytes", base64.b64encode(value).decode("ascii")]
    if isinstance(value, list):
        walked = [await _walk_for_wire(v, register_entity) for v in value]
        return [walked]
    if isinstance(value, tuple):
        walked = [await _walk_for_wire(v, register_entity) for v in value]
        return [walked]
    if isinstance(value, dict):
        for k in value:
            if not isinstance(k, str):
                raise TypeError(
                    "schedule_invoke: dict keys in args must be " f"strings; got {type(k).__name__}"
                )
        return {k: await _walk_for_wire(v, register_entity) for k, v in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(
        f"schedule_invoke: cannot encode arg of type {type(value).__name__} for the wire"
    )


def _service_ref_to_wire(ref: ServiceRef[Any]) -> list[Any]:
    handle = ref.handle
    if isinstance(handle, _RemoteServiceHandleStub):
        return ["service", handle.short_id]
    if isinstance(handle, _OwnedCallbackHandleStub):
        if handle.slot_n is not None:
            return ["callback", handle.slot_n]
        return ["callback_handle", handle.slot_key]
    raise TypeError(
        f"schedule_invoke: ServiceRef handle must be a sandbox stub "
        f"(_RemoteServiceHandleStub / _OwnedCallbackHandleStub), got "
        f"{type(handle).__name__}."
    )


# --- result_type_of ---------------------------------------------------


def result_type_of[
    _T_proto, _R, **_P
](
    target_type: type[_T_proto],
    method: Callable[Concatenate[_T_proto, _P], Awaitable[_R]],
) -> _ResultTypeSentinel[_R]:
    """Return a sentinel for the named typed-service method's return type.

    Pass the result to :func:`retrieve_result` /
    :func:`retrieve_callback_notification` as ``result_type`` to
    decode the slot's payload against the method's declared return
    annotation (through the layer-2 single-field return wrapper).

    Two-arg shape so that:

    - **Pyright** infers ``R`` from ``method``'s return type via
      standard generic inference. The ``Concatenate[T_proto, P]``
      binding pins ``T_proto`` to the method's ``self`` parameter,
      which lets pyright substitute through generic Protocols
      automatically — passing ``MyProto[int]`` plus
      ``MyProto.echo`` (whose return is ``T_inner``) unifies
      ``T_inner = int`` and yields ``R = int``.
    - **Runtime** uses ``target_type`` to find the right
      parameterized return wrapper. ``get_args(target_type)``
      extracts the type arguments (e.g. ``(int,)`` for
      ``MyProto[int]``); ``get_origin(target_type)`` gives the
      bare Protocol class for the
      ``_fxtr_service_protocol_metadata`` lookup. ``method.__name__``
      keys into the method specs.

    Usage::

        result_type_of(MyProto, MyProto.echo)         # non-generic
        result_type_of(MyProto[int], MyProto.echo)    # generic

    Sanity-checks that ``method`` actually belongs to ``target_type``
    by looking it up via ``__name__`` on the origin class — catches
    ``result_type_of(MyProto, OtherProto.echo)``-style mistakes.

    Raises:
        :exc:`NotImplementedError` if ``target_type`` isn't a
            ``@service_protocol``-decorated Protocol.
        :exc:`ValueError` if ``method`` is not the function reachable
            via ``target_type.<method.__name__>``.
        :exc:`IRException` (category ``UnknownMethod``) if
            ``method.__name__`` isn't declared on the Protocol.
    """
    parameterization = get_args(target_type) or None
    proto = get_origin(target_type) or target_type
    meta = getattr(proto, "_fxtr_service_protocol_metadata", None)
    if meta is None:
        raise NotImplementedError(
            f"result_type_of: target_type {target_type!r} is not "
            "decorated with @service_protocol."
        )
    method_name = getattr(method, "__name__", None)
    if not isinstance(method_name, str):
        raise ValueError(
            f"result_type_of: method {method!r} has no __name__; pass an "
            "unbound Protocol method (e.g. MyProto.echo)."
        )
    on_proto = getattr(proto, method_name, None)
    if on_proto is not method:
        raise ValueError(
            f"result_type_of: method {method!r} doesn't belong to "
            f"protocol {proto!r} (got {on_proto!r} at "
            f"{proto.__name__ if hasattr(proto, '__name__') else proto!r}"
            f".{method_name})."
        )
    spec = meta.method_specs.get(method_name)
    if spec is None:
        raise IRException(
            message=(
                f"result_type_of: method {method_name!r} is not declared on "
                f"{getattr(proto, '__name__', proto)!r}; available: "
                f"{sorted(meta.method_specs.keys())!r}."
            ),
            category="UnknownMethod",
        )
    wrapper_cls = (
        spec.return_wrapper_cls[*parameterization]  # type: ignore[index]
        if parameterization
        else spec.return_wrapper_cls
    )
    return _ResultTypeSentinel(wrapper_cls=wrapper_cls)


# --- retrieve_result stage helpers ------------------------------------


async def _resolve_slot_to_fxtr_service_data(
    val: list[Any],
    slot_key: str,
    lookup_entity_fn: Callable[[str], Any],
) -> Any:
    """Stage 1 of ``retrieve_result``: turn a slot's wire-tagged value
    into a canonical FxtrServiceData. Raises terminal errors for the
    non-value tags."""
    if not isinstance(val, list) or len(val) != 2:  # pyright: ignore[reportUnnecessaryIsInstance]
        raise RuntimeError(  # pyright: ignore[reportUnreachable]
            f"retrieve_result: slot {slot_key!r}: unrecognized wire shape {val!r}"
        )
    tag, payload = val[0], val[1]
    if tag == "invoke_pending":
        raise LookupError(f"slot {slot_key!r}: result not yet available")
    if tag == "invoke_error":
        raise RuntimeError(f"invoke for {slot_key!r} failed: {payload}")
    if tag == "exported_entity":
        if not isinstance(payload, str):
            raise RuntimeError(
                f"retrieve_result: slot {slot_key!r}: exported_entity payload "
                f"must be a string, got {payload!r}"
            )
        return await lookup_entity_fn(payload)
    if tag == "exported_service":
        if not isinstance(payload, str):
            raise RuntimeError(
                f"retrieve_result: slot {slot_key!r}: exported_service payload "
                f"must be a string, got {payload!r}"
            )
        return _make_remote_service_ref(payload, UnknownService)
    if tag == "callback_open":
        if not isinstance(payload, int):
            raise RuntimeError(
                f"retrieve_result: slot {slot_key!r}: callback_open payload "
                f"must be int, got {payload!r}"
            )
        # Canonical FxtrServiceData form is ``ServiceRef[UnknownService]``;
        # stage 2 re-types via the slot default (or a caller-supplied
        # ``result_type``) through the dialect.
        return _make_owned_callback_ref(slot_n=payload).replace_service_type(UnknownService)
    if tag == "invoke_result":
        return await _unescape_payload(payload, lookup_entity_fn)
    raise RuntimeError(f"retrieve_result: slot {slot_key!r}: unknown tag {tag!r}")


def _slot_default_result_type(val: list[Any]) -> Any:
    """Return the slot-tag-specific default ``result_type`` used when
    the caller passes ``None``. Currently only ``callback_open`` has a
    non-None default — its canonical FxtrServiceData is a
    ``ServiceRef[UnknownService]``, but the agent-visible default is
    a typed ``ServiceRef[WorkstationSessionEventInbox]`` (parity with how
    ``create_callback`` returns).
    """
    if isinstance(val, list) and len(val) == 2 and val[0] == "callback_open":
        return ServiceRef[WorkstationSessionEventInbox]
    return None


def _apply_result_type(fsd: Any, result_type: Any) -> Any:
    """Stage 2 of ``retrieve_result``: optionally decode ``fsd``
    against ``result_type``. ``None`` → return ``fsd`` unchanged.
    :class:`_ResultTypeSentinel` → decode via the layer-2 single-field
    return wrapper. Anything else → :func:`decode_from_service_data`.
    """
    if result_type is None:
        return fsd
    if isinstance(result_type, _ResultTypeSentinel):
        decoder = get_basic_decoder(
            result_type.wrapper_cls,
            FxtrServicesDefaultDialect,
            pre_decoder_func=_wrap_result_value,
        )
        return decoder.decode(fsd).value
    return decode_from_service_data(fsd, result_type)


def _wrap_result_value(v: Any) -> Mapping[str, Any]:
    """Mirror of ``fxtr2.services.typed_service._wrap_result_value`` —
    the single-field return wrapper expects its sole field
    (``value: T``) wrapped before mashumaro's decoder takes over."""
    return {"value": v}


# --- Payload unescape (used by retrieve_result + retrieve_callback_notification) -----


async def _unescape_payload(
    payload: Any,
    lookup_entity_fn: Callable[[str], Any],
) -> Any:
    """Walk an invoke-result payload from wire form to canonical
    :data:`FxtrServiceData`. Three-pass design so
    ``lookup_entity_fn`` round-trips can interleave: pass 1 collects
    distinct entity short_ids, pass 2 resolves all in parallel via a
    task group, pass 3 rewrites the structure substituting resolved
    values."""
    # Pass 1: collect distinct entity short_ids.
    entity_ids: set[str] = set()
    _collect_entity_ids(payload, entity_ids)

    # Pass 2: resolve in parallel.
    resolved: dict[str, Any] = {}
    if entity_ids:
        async with anyio.create_task_group() as tg:

            async def _resolve(eid: str) -> None:
                resolved[eid] = await lookup_entity_fn(eid)

            for eid in entity_ids:
                tg.start_soon(_resolve, eid)

    # Pass 3: walk + substitute.
    return _assemble(payload, resolved)


def _collect_entity_ids(value: Any, into: set[str]) -> None:
    """Walk a wire-escape-encoded value, adding any
    ``["entity", short_id]`` short_id to ``into``.

    Strict grammar: every list is either a 2-element tagged form
    or a 1-element literal-list-escape; anything else raises.
    Tags other than ``"entity"`` (``"service"`` / ``"bytes"`` /
    ``"callback"``) are skipped without recursing. An unrecognized
    tag raises — better to fail loudly than miss an entity ref
    nested inside a tag we don't know how to walk."""
    if isinstance(value, list):
        if len(value) == 2 and isinstance(value[0], str):
            tag, inner = value[0], value[1]
            if tag == "entity":
                if not isinstance(inner, str):
                    raise ValueError(f"entity tag payload must be a string, got {inner!r}")
                into.add(inner)
                return
            if tag in ("service", "bytes", "callback"):
                return  # no nested entity refs to collect
            raise ValueError(f"unrecognized wire tag {tag!r} in invoke-result payload")
        if len(value) == 1 and isinstance(value[0], list):
            for v in value[0]:
                _collect_entity_ids(v, into)
            return
        raise ValueError(
            f"malformed wire-escape list: {value!r} (must be a tagged "
            "2-element form or a 1-element literal-list-escape)"
        )
    if isinstance(value, dict):
        for v in value.values():
            _collect_entity_ids(v, into)


def _assemble(value: Any, resolved: dict[str, Any]) -> Any:
    """Pass 3 of :func:`_unescape_payload`: walk a wire-escape-
    encoded value, substituting:

    - ``["entity", short_id]`` → resolved :class:`FxtrID`.
    - ``["service", short_id]`` → ``ServiceRef[UnknownService]`` with
      a :class:`_RemoteServiceHandleStub` (canonical
      FxtrServiceData; stage 2 of ``retrieve_result`` re-attaches
      ``T`` if ``result_type`` is provided).
    - ``["bytes", b64]`` → decoded ``bytes``.
    - ``["callback", N]`` → ``ServiceRef[UnknownService]`` with a
      :class:`_OwnedCallbackHandleStub(slot_n=N)` (defensive — not
      expected on the workstation return path; see scratch doc).
    - ``[[inner]]`` literal-list-escape → ``[inner_assembled]``.

    Strict grammar (matches :func:`_collect_entity_ids`):
    unrecognized tags and malformed list shapes raise."""
    if isinstance(value, list):
        if len(value) == 2 and isinstance(value[0], str):
            tag, inner = value[0], value[1]
            if tag == "entity":
                if not isinstance(inner, str):
                    raise ValueError(f"entity tag payload must be a string, got {inner!r}")
                return resolved[inner]
            if tag == "service":
                if not isinstance(inner, str):
                    raise ValueError(f"service tag payload must be a string, got {inner!r}")
                return _make_remote_service_ref(inner, UnknownService)
            if tag == "bytes":
                if not isinstance(inner, str):
                    raise ValueError(f"bytes tag payload must be a base64 string, got {inner!r}")
                return base64.b64decode(inner)
            if tag == "callback":
                if not isinstance(inner, int):
                    raise ValueError(f"callback tag payload must be an int, got {inner!r}")
                # Canonical FxtrServiceData form is
                # ``ServiceRef[UnknownService]``; the caller of
                # ``retrieve_result`` retypes via the slot default or
                # an explicit ``result_type`` through the dialect.
                return _make_owned_callback_ref(slot_n=inner).replace_service_type(UnknownService)
            raise ValueError(f"unrecognized wire tag {tag!r} in invoke-result payload")
        if len(value) == 1 and isinstance(value[0], list):
            return [_assemble(v, resolved) for v in value[0]]
        raise ValueError(
            f"malformed wire-escape list: {value!r} (must be a tagged "
            "2-element form or a 1-element literal-list-escape)"
        )
    if isinstance(value, dict):
        return {k: _assemble(v, resolved) for k, v in value.items()}
    return value
