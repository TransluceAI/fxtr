"""Inbound evaluator: ``Expression[T]`` → ``IRValue``, in two phases.

Phase 1 is synchronous and runs inline in the reader loop. It walks the incoming
``Expression[Any]`` once, substituting every ``Imported…Expr`` / ``Exported…Expr``
leaf with a :class:`_ConstantExpr` carrying the resolved runtime value. This is the
only place the import/export tables mutate on the receive path. ``Imported…Expr``
looks up an existing local object (ours by export); ``Exported…Expr`` either
registers a new import entry + mints a ``Remote…`` proxy, or validates an existing
entry (kind and associated-token-id mismatches trigger abort).

Phase 2 (:func:`eval_as_value`, :func:`eval_as_call_arg`, :func:`eval_forwardable`) is
async. It operates on phase-1-substituted trees, so :class:`ImportedLeaseExpr`-family
leaves have all been replaced by :class:`ConstantExpr`; residuals are treated as
phase-1 bugs. The three modes differ only at
``AwaitFutureExpr`` / ``HandleRequestExpr`` and at container-child recursion — see
``phase_d_prep_todos.md`` §K.2 for the per-node table.

Type mismatches (e.g. a :class:`ConstantExpr` whose value is the wrong kind for its
slot) raise :class:`IRException`, which the top-level spawn plumbing converts to a
per-future failure rather than a connection abort. See ``phase_d_prep_todos.md`` §K.3.

:func:`eval_static` is sync and standalone — used by ``_on_reject`` / ``_on_abort``
for protocol-guaranteed-static payloads. It runs on the raw (pre-phase-1) tree to
avoid table allocations that a malformed peer could leak.

All public / module-private functions take ``conn`` as their first positional argument
rather than being methods on ``LeasedCapRPCConnection``. The one exception is
:func:`eval_static`, which needs no table access.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, NoReturn, Sequence, cast

from fxtr2.leases.lease_types import Lease
from fxtr2.leases.leased_cap_common import discard_leases_in
from fxtr2.leases.leased_cap_ir import (
    IRCallArg,
    IRException,
    IRFuture,
    IRHandle,
    IRStatic,
    IRTagged,
    IRValue,
    UnwrapForPipelining,
)
from fxtr2.leases.local_lease import LocalLifetimeLease, make_broken_lease
from fxtr2.rpc.lowering_errors import RPCConnectionAborted
from fxtr2.rpc.lowering_remote_proxies import RemoteGrant
from fxtr2.rpc.lowering_state import (
    ConstantExpr,
    FutureExportEntry,
    FutureImportEntry,
    GrantExportEntry,
    HandleExportEntry,
    ImportEntryKindMismatch,
    TokenExportEntry,
)
from fxtr2.rpc.lowering_types import (
    AwaitFutureExpr,
    Call,
    DictExpr,
    ExportedIRFutureExpr,
    ExportedIRHandleExpr,
    ExportedLeaseExpr,
    ExportedLifetimeTokenExpr,
    Expression,
    GetItem,
    HandleDispatchExpr,
    HandleRequestExpr,
    ImportedIRFutureExpr,
    ImportedIRHandleExpr,
    ImportedLeaseExpr,
    ImportedLifetimeTokenExpr,
    IRTaggedExpr,
    ListExpr,
    StaticExpr,
    WalkFutureExpr,
)

if TYPE_CHECKING:
    from fxtr2.rpc.lowering import LeasedCapRPCConnection


####################################################################################################
# Phase 1: synchronous substitution pass
####################################################################################################


def phase1_substitute(
    conn: "LeasedCapRPCConnection", expr: Expression[Any]
) -> tuple[Expression[Any], list[FutureImportEntry]]:
    """Sync walk over an incoming expression tree. Substitutes every ``Imported…Expr`` /
    ``Exported…Expr`` leaf with a :class:`_ConstantExpr` holding the resolved runtime
    value; leaves all other nodes intact with their children recursively substituted.

    Mutates the import/export tables as a side effect: ``Imported…Expr`` looks up an
    existing export; ``Exported…Expr`` either registers a new import entry + mints a
    ``Remote…`` proxy, or validates against an existing entry. Kind / associated-token
    mismatches call :func:`phase1_protocol_violation` — these are protocol violations, not
    user-input errors, so aborting the whole connection is appropriate.

    Also accumulates resolve-obligation bumps for every inbound ``AwaitFutureExpr`` whose
    ``future`` slot references one of our future imports (directly via
    :class:`ExportedIRFutureExpr` or through an intervening :class:`WalkFutureExpr`).
    Each such occurrence bumps the target :class:`FutureImportEntry`'s
    ``resolve_obligation_refcount`` and ``local_consumer_count`` per the
    :class:`UnpullMessage` protocol rule. The bumped entries are returned as a list
    alongside the substituted expression so the caller (a message handler) can
    release the ``local_consumer_count`` slots in the phase-2 spawn's ``finally``
    block — see :func:`_on_push` / :func:`_on_resolve`.

    Sync to keep it race-free with the writer loop and the user-facing devaluator.
    """
    obligations: list[FutureImportEntry] = []
    substituted = _phase1_walk(conn, expr, obligations)
    return substituted, obligations


def _phase1_walk(
    conn: "LeasedCapRPCConnection",
    expr: Expression[Any],
    obligations: list[FutureImportEntry],
) -> Expression[Any]:
    """Shared recursive implementation of :func:`phase1_substitute`. ``obligations``
    is mutated with every :class:`FutureImportEntry` whose
    ``resolve_obligation_refcount`` / ``local_consumer_count`` was bumped by an
    ``AwaitFutureExpr`` referencing our imports — see the public wrapper for why.
    """
    # Leaves that don't need substitution.
    if isinstance(expr, StaticExpr):
        return expr
    # Leaves that resolve Imported*Expr (sender's import = our export).
    if isinstance(expr, ImportedLifetimeTokenExpr):
        return _phase1_resolve_our_export(
            conn,
            expr.import_id,
            expr_name=type(expr).__name__,
            kind=TokenExportEntry,
            extract=lambda e: e.local_token,
        )
    if isinstance(expr, ImportedIRHandleExpr):
        return _phase1_resolve_our_export(
            conn,
            expr.import_id,
            expr_name=type(expr).__name__,
            kind=HandleExportEntry,
            extract=lambda e: e.local_handle,
        )
    if isinstance(expr, ImportedIRFutureExpr):
        return _phase1_resolve_our_export(
            conn,
            expr.import_id,
            expr_name=type(expr).__name__,
            kind=FutureExportEntry,
            extract=lambda e: e.local_future,
        )
    if isinstance(expr, ImportedLeaseExpr):
        # Break-race tolerance: if the referenced grant export was torn down
        # because its backing lease broke (owner-side teardown the sender can't
        # have seen yet — their request crossed our BreakMessage on the wire),
        # substitute an already-broken lease on the original grant. The request
        # then degrades request-scoped — phase 2's dispatch take() raises the
        # LeaseBrokenError the requester already handles — instead of aborting
        # the connection. Mirrors the _on_pull / _on_discard teardown-race
        # tolerance; see ``_broken_grant_export_tombstones`` in ``lowering.py``.
        if expr.grant_import_id not in conn._exports:
            tombstone = conn._broken_grant_export_tombstones.get(expr.grant_import_id)
            if tombstone is not None:
                grant, reason = tombstone
                return ConstantExpr(value=cast(IRValue, make_broken_lease(grant, reason)))
        # Cloning the backing lease hands the sender back a fresh lease on the
        # grant they previously imported. The clone has the same ``grant`` as the
        # backing lease — a local grant from our side.
        return _phase1_resolve_our_export(
            conn,
            expr.grant_import_id,
            expr_name=type(expr).__name__,
            kind=GrantExportEntry,
            extract=lambda e: e.backing_lease.clone(),
        )
    # Leaves that register or look up an Exported*Expr (sender's export = our import).
    if isinstance(expr, ExportedLifetimeTokenExpr):
        _phase1_register_token_import(conn, expr.token_export_id)
        return ConstantExpr(
            value=cast(IRValue, conn._get_or_create_token_proxy(expr.token_export_id))
        )
    if isinstance(expr, ExportedIRHandleExpr):
        _phase1_register_handle_import(conn, expr.handle_export_id, expr.token_export_id)
        return ConstantExpr(
            value=cast(IRValue, conn._get_or_create_handle_proxy(expr.handle_export_id))
        )
    if isinstance(expr, ExportedIRFutureExpr):
        _phase1_register_future_import(conn, expr.future_export_id, expr.token_export_id)
        return ConstantExpr(
            value=cast(IRValue, conn._get_or_create_future_proxy(expr.future_export_id))
        )
    if isinstance(expr, ExportedLeaseExpr):
        _phase1_register_grant_import(conn, expr.grant_export_id, expr.token_export_id)
        # Mint a fresh lease against the newly-registered (or reused) grant
        # import. Pin the subject-token proxy on ``_token`` so the token
        # can't GC while any lease on this grant is alive — see
        # "Proxy-GC tracking" in ``lowering_remote_proxies.py``.
        token_proxy = conn._get_or_create_token_proxy(expr.token_export_id)
        lease = LocalLifetimeLease(
            owner=conn,
            grant=RemoteGrant(conn=conn, grant_import_id=expr.grant_export_id, _token=token_proxy),
        )
        return ConstantExpr(value=cast(IRValue, lease))
    # Non-leaf expressions — recurse into children. Each returns a rebuilt node of
    # the same kind with substituted subexpressions.
    if isinstance(expr, ListExpr):
        return ListExpr(elements=tuple(_phase1_walk(conn, e, obligations) for e in expr.elements))
    if isinstance(expr, DictExpr):
        return DictExpr(
            items={k: _phase1_walk(conn, v, obligations) for k, v in expr.items.items()}
        )
    if isinstance(expr, IRTaggedExpr):
        return IRTaggedExpr(tag=expr.tag, value=_phase1_walk(conn, expr.value, obligations))
    if isinstance(expr, AwaitFutureExpr):
        # Walk the future slot FIRST so its contained ExportedIRFutureExpr
        # (if any) registers the future import via _phase1_register_future_import.
        # Then bump resolve-obligation + local_consumer_count on that entry —
        # per the :class:`UnpullMessage` protocol rule, inbound
        # AwaitFutureExpr referencing one of our imports implicitly pulls.
        future_substituted = _phase1_walk(conn, expr.future, obligations)
        lease_substituted = _phase1_walk(conn, expr.lease, obligations)
        _phase1_bump_await_future_obligation(conn, expr.future, obligations)
        return AwaitFutureExpr(
            future=cast(Expression[IRFuture[Any, Any]], future_substituted),
            lease=cast(Expression[Lease[Any]], lease_substituted),
        )
    if isinstance(expr, WalkFutureExpr):
        return WalkFutureExpr(
            future=cast(
                Expression[IRFuture[Any, Any]],
                _phase1_walk(conn, expr.future, obligations),
            ),
            attr_path=expr.attr_path,
        )
    if isinstance(expr, HandleRequestExpr):
        return HandleRequestExpr(
            handle=cast(
                Expression[IRHandle[Any]],
                _phase1_walk(conn, expr.handle, obligations),
            ),
            lease=cast(Expression[Lease[Any]], _phase1_walk(conn, expr.lease, obligations)),
            request=_phase1_substitute_request(conn, expr.request, obligations),
            wait_for=tuple(_phase1_substitute_await(conn, w, obligations) for w in expr.wait_for),
        )
    if isinstance(expr, HandleDispatchExpr):
        return HandleDispatchExpr(
            handle=cast(
                Expression[IRHandle[Any]],
                _phase1_walk(conn, expr.handle, obligations),
            ),
            lease=cast(Expression[Lease[Any]], _phase1_walk(conn, expr.lease, obligations)),
            request=_phase1_substitute_request(conn, expr.request, obligations),
            wait_for=tuple(_phase1_substitute_await(conn, w, obligations) for w in expr.wait_for),
        )
    # Nothing else is a valid top-level ``Expression`` — protocol violation.
    phase1_protocol_violation(
        conn,
        f"phase-1 substitute: unsupported expression type {type(expr).__name__}",
    )


def _phase1_bump_await_future_obligation(
    conn: "LeasedCapRPCConnection",
    future_slot: Expression[Any],
    obligations: list[FutureImportEntry],
) -> None:
    """If ``future_slot`` references one of our future imports, bump its
    resolve-obligation refcount + local_consumer_count and add to ``obligations``.

    Called AFTER the future slot has been walked (so the underlying
    ``ExportedIRFutureExpr`` has been registered as a :class:`FutureImportEntry`).

    Recognized shapes: ``ExportedIRFutureExpr(id)`` (direct reference) or
    ``WalkFutureExpr(ExportedIRFutureExpr(id), ...)`` (walk into a sub-value of
    the same imported future — the walk doesn't change which future's lifetime
    carries the obligation). Any other shape (e.g. a nested ``AwaitFutureExpr``,
    an :class:`ImportedIRFutureExpr` bounce-back, a :class:`ConstantExpr` already
    substituted) doesn't create an obligation on our import-side:

    - ``ImportedIRFutureExpr`` is a bounce-back (sender references one of our
      exports) — the obligation lives on the export side, not here.
    - Nested ``AwaitFutureExpr``-as-future is a more complex pipelining shape
      the protocol doesn't currently require us to pre-bump; phase-2 evaluation
      of such a nested expression will itself trigger a :meth:`RemoteIRFuture.wait`
      call which carries its own obligation bump.
    """
    target = future_slot
    # Peel a single WalkFutureExpr if present; the root future is what counts.
    if isinstance(target, WalkFutureExpr):
        target = target.future
    if not isinstance(target, ExportedIRFutureExpr):
        return
    # ``target.future_export_id`` is the sender's export id, which on our side
    # is the future IMPORT id. The ExportedIRFutureExpr branch has already
    # registered (or validated) the entry via :func:`_phase1_register_future_import`.
    entry = conn._imports.get(target.future_export_id)
    if not isinstance(entry, FutureImportEntry):
        phase1_protocol_violation(
            conn,
            f"AwaitFutureExpr references future import id {target.future_export_id},"
            " which is not a registered FutureImportEntry (expected registration"
            " via the ExportedIRFutureExpr walk immediately preceding this bump).",
        )
    conn._bump_resolve_obligation_refcount(entry)
    entry.local_consumer_count += 1
    obligations.append(entry)


# --- Phase 1 helpers ---


def _phase1_resolve_our_export[
    E
](
    conn: "LeasedCapRPCConnection",
    id_: int,
    *,
    expr_name: str,
    kind: type[E],
    extract: Callable[[E], IRValue],
) -> Expression[Any]:
    """Resolve a sender-side ``Imported*Expr`` by looking up our own export entry.

    Verifies the export entry exists and matches the expected kind (``kind``), then
    returns a ``_ConstantExpr`` holding the value produced by ``extract``. Kind
    mismatches or missing entries are protocol violations and abort the connection.
    ``expr_name`` is the referencing expression's class name, used only in the
    violation messages (the expr-name ↔ entry-kind mapping isn't mechanically
    derivable — e.g. ``ImportedLeaseExpr`` resolves a ``GrantExportEntry``).
    """
    entry = conn._exports.get(id_)
    if entry is None:
        phase1_protocol_violation(
            conn,
            f"{expr_name} references export id {id_}, expected a"
            f" {kind.__name__}: no such export",
        )
    if not isinstance(entry, kind):
        phase1_protocol_violation(
            conn,
            f"{expr_name} references export id {id_}, expected a"
            f" {kind.__name__}: export is a {type(entry).__name__}",
        )
    return ConstantExpr(value=extract(entry))


def _phase1_register_token_import(conn: "LeasedCapRPCConnection", token_import_id: int) -> None:
    """Phase-1 wrapper around :meth:`LeasedCapRPCConnection._introduce_token_import`.

    Catches :class:`ImportEntryKindMismatch` and translates it into
    :func:`phase1_protocol_violation` — a kind mismatch on an inbound
    ``Exported…Expr`` means the peer contradicted an earlier introduction,
    which is a protocol violation rather than a local bug.
    """
    try:
        conn._introduce_token_import(token_import_id)
    except ImportEntryKindMismatch as e:
        phase1_protocol_violation(conn, str(e))


def _phase1_register_handle_import(
    conn: "LeasedCapRPCConnection", handle_import_id: int, token_import_id: int
) -> None:
    """Phase-1 wrapper around :meth:`LeasedCapRPCConnection._introduce_handle_import`."""
    try:
        conn._introduce_handle_import(handle_import_id, token_import_id)
    except ImportEntryKindMismatch as e:
        phase1_protocol_violation(conn, str(e))


def _phase1_register_future_import(
    conn: "LeasedCapRPCConnection", future_import_id: int, token_import_id: int
) -> None:
    """Phase-1 wrapper around :meth:`LeasedCapRPCConnection._introduce_future_import`."""
    try:
        conn._introduce_future_import(future_import_id, token_import_id)
    except ImportEntryKindMismatch as e:
        phase1_protocol_violation(conn, str(e))


def _phase1_register_grant_import(
    conn: "LeasedCapRPCConnection", grant_import_id: int, token_import_id: int
) -> None:
    """Phase-1 wrapper around :meth:`LeasedCapRPCConnection._introduce_grant_import`."""
    try:
        conn._introduce_grant_import(grant_import_id, token_import_id)
    except ImportEntryKindMismatch as e:
        phase1_protocol_violation(conn, str(e))


def _phase1_substitute_request(
    conn: "LeasedCapRPCConnection",
    request: Call[Expression[Any]] | GetItem,
    obligations: list[FutureImportEntry],
) -> Call[Expression[Any]] | GetItem:
    """Phase-1 substitute inside a ``Request``. ``Call.arg`` is an ``Expression`` we
    need to recurse into; ``GetItem`` and ``WaitAndIgnoreFutureThen`` are rebuilt
    analogously.

    (We only support the shapes ``HandleRequestExpr.request`` / ``HandleDispatchExpr.
    request`` actually carry, per :mod:`lowering_types` — that's ``Call[Expression
    [Any]] | GetItem``, not including ``WaitAndIgnoreFutureThen`` at the Expression
    level.)
    """
    if isinstance(request, Call):
        return Call[Expression[Any]](
            method=request.method,
            arg=_phase1_walk(conn, request.arg, obligations),
        )
    if isinstance(request, GetItem):
        return request
    # At the Expression level we shouldn't see WaitAndIgnoreFutureThen — the
    # protocol carries it only inside locally-constructed Request values. Protocol
    # violation if we do.
    phase1_protocol_violation(
        conn,
        f"phase-1 substitute_request: unsupported request type {type(request).__name__}",
    )


def _phase1_substitute_await(
    conn: "LeasedCapRPCConnection",
    await_expr: AwaitFutureExpr[Any],
    obligations: list[FutureImportEntry],
) -> AwaitFutureExpr[Any]:
    """Phase-1 substitute inside an ``AwaitFutureExpr`` appearing in a
    ``HandleRequestExpr.wait_for`` / ``HandleDispatchExpr.wait_for`` sequence. Just
    a typed recursive wrapper around :func:`_phase1_walk`.
    """
    substituted = _phase1_walk(conn, await_expr, obligations)
    assert isinstance(
        substituted, AwaitFutureExpr
    ), "phase-1 substitute of an AwaitFutureExpr should still be an AwaitFutureExpr"
    return substituted


def phase1_protocol_violation(conn: "LeasedCapRPCConnection", reason: str) -> NoReturn:
    """Uniform abort path for phase-1 protocol violations.

    Always raises :class:`RPCConnectionAborted` after delegating local cleanup to
    ``conn._abort``. Annotated as ``-> NoReturn`` so pyright treats call sites as
    unreachable past the call, which is important for type narrowing after
    ``if not isinstance(…): phase1_protocol_violation(…)`` checks.
    """
    exc = IRException(message=reason, category="ProtocolError")
    conn._abort(exc)
    raise RPCConnectionAborted(exc.message)


####################################################################################################
# Standalone static evaluator (no conn, no phase-1 substitution)
####################################################################################################


def eval_static(expr: Expression[Any]) -> IRStatic:
    """Fully synchronous evaluator for "must be pure static data" contexts.

    Used by ``_on_reject`` and ``_on_abort`` — payloads that the protocol
    guarantees do not contain stubs and do not require async work. Runs on the **raw**
    expression tree (no phase-1 substitution), so a malformed peer cannot trick us
    into allocating any imports/exports/leases before rejection.

    Accepts only ``StaticExpr``, ``ListExpr``, ``DictExpr``, and ``IRTaggedExpr``
    (recursively evaluated in the same mode). Every other expression kind —
    ``Imported…Expr`` / ``Exported…Expr`` / ``AwaitFutureExpr`` / ``HandleRequestExpr``
    / ``HandleDispatchExpr`` / ``WalkFutureExpr`` — raises a ``ValueError``, which the
    caller catches and wraps as an :class:`IRException` to serve as the replacement
    rejection/abort payload.
    """
    if isinstance(expr, StaticExpr):
        # ``StaticExpr.value`` is typed as ``IRStatic``; we trust that typing here.
        # The trust boundary is the wire codec (Stage E), which is responsible for
        # refusing to construct a ``StaticExpr`` whose ``value`` contains stubs,
        # leases, handles, futures, or tokens — by the time a ``StaticExpr``
        # reaches us in the lowering layer, its payload has already been validated
        # as pure :class:`IRStatic`. An untrusted peer that managed to bypass the
        # codec could still smuggle a stub in here, but that would be a codec bug
        # rather than something we need to re-validate on every receive.
        return expr.value
    if isinstance(expr, ListExpr):
        return [eval_static(child) for child in expr.elements]
    if isinstance(expr, DictExpr):
        return {k: eval_static(v) for k, v in expr.items.items()}
    if isinstance(expr, IRTaggedExpr):
        return IRTagged(tag=expr.tag, value=eval_static(expr.value))
    raise ValueError(
        f"_eval_static: unsupported expression type {type(expr).__name__} in static-only context"
    )


####################################################################################################
# Phase 2: async evaluators (three modes)
####################################################################################################


# Stub residual leaf types that phase 1 is supposed to have substituted away. Used to
# produce consistent "phase-1 bug" IRExceptions if phase 2 encounters one.
_PHASE1_STUB_EXPR_TYPES: tuple[type, ...] = (
    ImportedLifetimeTokenExpr,
    ImportedIRHandleExpr,
    ImportedIRFutureExpr,
    ImportedLeaseExpr,
    ExportedLifetimeTokenExpr,
    ExportedIRHandleExpr,
    ExportedIRFutureExpr,
    ExportedLeaseExpr,
)


def _phase2_unsupported(expr: Expression[Any], *, context: str) -> IRException[None]:
    """Build an :class:`IRException` for an expression kind that phase 2 didn't expect.

    Two flavors, all raised as per-future failures by the spawn plumbing (not connection
    aborts — see ``phase_d_prep_todos.md`` §K.3):

    - Residual ``Imported…Expr`` / ``Exported…Expr`` → phase-1 bug. The wire message was
      well-formed but our phase-1 pass failed to substitute the leaf before handing the
      tree off to phase 2.
    - Anything else → malformed tree the phase-2 walker can't handle.
    """
    if isinstance(expr, _PHASE1_STUB_EXPR_TYPES):
        return IRException(
            message=(
                f"{context}: residual {type(expr).__name__} after phase-1"
                " substitution (phase-1 bug)"
            ),
            category="InternalError",
        )
    return IRException(
        message=f"{context}: unsupported expression type {type(expr).__name__}",
        category="EvalError",
    )


def _phase2_type_mismatch(*, context: str, expected: str, value: Any) -> IRException[None]:
    """Build an :class:`IRException` describing a type mismatch at a typed sub-slot.

    Used when a :class:`ConstantExpr` landing in (e.g.) an ``AwaitFutureExpr.future``
    slot holds a non-future — the wire message itself was structurally valid but its
    runtime values don't match the shape the evaluator needs.
    """
    return IRException(
        message=(f"{context}: expected {expected}, got {type(value).__name__}"),
        category="TypeMismatch",
    )


async def eval_as_value(conn: "LeasedCapRPCConnection", expr: Expression[Any]) -> IRValue:
    """Phase-2 evaluator in "must evaluate to :class:`IRValue`" mode.

    Used wherever the current context requires a concrete runtime value — for example
    the inner ``future`` / ``lease`` slots of an :class:`AwaitFutureExpr`, the
    ``handle`` / ``lease`` slots of a :class:`HandleRequestExpr`, and the top-level
    expression of a :class:`ResolveMessage` whose recipient box cannot forward.

    On :class:`AwaitFutureExpr` we actually await the future. On
    :class:`HandleRequestExpr` we dispatch and await the result. Containers recurse
    in ``as_value`` mode — the container itself must be an :class:`IRValue`, so no
    :class:`UnwrapForPipelining` leaves may appear.
    """
    if isinstance(expr, StaticExpr):
        return cast(IRValue, expr.value)
    if isinstance(expr, ConstantExpr):
        return expr.value
    if isinstance(expr, ListExpr):
        return [await eval_as_value(conn, e) for e in expr.elements]
    if isinstance(expr, DictExpr):
        return {k: await eval_as_value(conn, v) for k, v in expr.items.items()}
    if isinstance(expr, IRTaggedExpr):
        # ``expr.tag`` is typed as :class:`IRStatic` on the expression dataclass but
        # we do not re-validate its runtime shape here. The wire codec (Stage E) is
        # the trust boundary that must refuse to decode a tag containing stubs,
        # leases, handles, futures, or tokens — by the time an :class:`IRTaggedExpr`
        # reaches phase 2 we trust the tag is already pure static data.
        inner = await eval_as_value(conn, expr.value)
        return IRTagged(tag=expr.tag, value=inner)
    if isinstance(expr, WalkFutureExpr):
        inner_future = await _eval_future_subexpr(
            conn, expr.future, context="WalkFutureExpr.future"
        )
        return inner_future.walk(expr.attr_path)
    if isinstance(expr, AwaitFutureExpr):
        future, lease = await _eval_await_fut_and_lease(conn, expr)
        return await future.wait(lease)
    if isinstance(expr, HandleRequestExpr):
        future, result_lease = await _dispatch_handle_request(conn, expr)
        # `request` semantics: wait for the result and discard our result lease. Same
        # shape as ``IRHandleLocalImplBase.request`` — a cancelled ``wait`` doesn't
        # cancel the underlying dispatch.
        try:
            return await future.wait(result_lease.clone())
        finally:
            result_lease.discard()
    if isinstance(expr, HandleDispatchExpr):
        future, result_lease = await _dispatch_handle_request(conn, expr)
        # A 2-list of (future, lease) is a valid IRValue: IRFuture and Lease are both
        # IRValue leaves, and a plain list is an IRValue container.
        return cast(IRValue, [future, result_lease])
    raise _phase2_unsupported(expr, context="eval_as_value")


async def eval_as_call_arg(conn: "LeasedCapRPCConnection", expr: Expression[Any]) -> IRCallArg:
    """Phase-2 evaluator in "call-arg" mode (allows :class:`UnwrapForPipelining`).

    Used when evaluating an expression that will be passed as the ``arg`` of an
    :class:`IRHandle.dispatch` call. Differs from :func:`eval_as_value` only in how
    :class:`AwaitFutureExpr` / :class:`HandleRequestExpr` resolve and in container
    recursion — both the expressions themselves and nested container children may
    evaluate to :class:`UnwrapForPipelining`, which the local dispatch's
    ``_unwrap_request_futures`` resolves at the handle boundary.

    TODO: partial-walk failure cleanup. If a container eval mints several leases
    (via :class:`ConstantExpr` leaves that phase 1 minted from
    :class:`ExportedLeaseExpr`) and a later sibling's eval raises, already-minted
    leases in earlier siblings are not discarded. Per-future failure routing keeps
    the connection up, but each orphaned lease will fire a leak-warning finalizer.
    If that warning-noise becomes annoying in practice, thread a "tentative leases"
    accumulator through the walk (similar to :class:`_DevaluateContext` on the
    outbound side) and discard on rollback.
    """
    if isinstance(expr, StaticExpr):
        return cast(IRCallArg, expr.value)
    if isinstance(expr, ConstantExpr):
        return cast(IRCallArg, expr.value)
    if isinstance(expr, ListExpr):
        return [await eval_as_call_arg(conn, e) for e in expr.elements]
    if isinstance(expr, DictExpr):
        return {k: await eval_as_call_arg(conn, v) for k, v in expr.items.items()}
    if isinstance(expr, IRTaggedExpr):
        inner = await eval_as_call_arg(conn, expr.value)
        return IRTagged(tag=expr.tag, value=inner)
    if isinstance(expr, WalkFutureExpr):
        inner_future = await _eval_future_subexpr(
            conn, expr.future, context="WalkFutureExpr.future"
        )
        return inner_future.walk(expr.attr_path)
    if isinstance(expr, AwaitFutureExpr):
        future, lease = await _eval_await_fut_and_lease(conn, expr)
        return UnwrapForPipelining(future=future, lease=lease)
    if isinstance(expr, HandleRequestExpr):
        future, result_lease = await _dispatch_handle_request(conn, expr)
        return UnwrapForPipelining(future=future, lease=result_lease)
    if isinstance(expr, HandleDispatchExpr):
        future, result_lease = await _dispatch_handle_request(conn, expr)
        return cast(IRCallArg, [future, result_lease])
    raise _phase2_unsupported(expr, context="eval_as_call_arg")


async def eval_forwardable(
    conn: "LeasedCapRPCConnection", expr: Expression[Any]
) -> IRValue | UnwrapForPipelining[IRValue, Any]:
    """Phase-2 evaluator in "top-level forwardable" mode.

    Used for the top-level expression of a :class:`PushMessage` / :class:`ResolveMessage`.
    A top-level :class:`AwaitFutureExpr` or :class:`HandleRequestExpr` returns
    :class:`UnwrapForPipelining` so :func:`spawn_ir_future_in_task_group` (or the
    :class:`LocalPendingResultBox.resolve_from` delivery path) can forward — chaining
    boxes rather than eagerly pulling — which is what makes multi-hop pipelining
    avoid intermediate round-trips (see ``phase_d_prep_todos.md`` §K.6).

    Everything else defers to :func:`eval_as_value`: container children must be
    concrete :class:`IRValue`, not forwardable carriers, because the container is the
    shape actually delivered to the box.
    """
    if isinstance(expr, AwaitFutureExpr):
        future, lease = await _eval_await_fut_and_lease(conn, expr)
        return UnwrapForPipelining(future=future, lease=lease)
    if isinstance(expr, HandleRequestExpr):
        future, result_lease = await _dispatch_handle_request(conn, expr)
        return UnwrapForPipelining(future=future, lease=result_lease)
    return await eval_as_value(conn, expr)


# --- Phase 2 helpers ---


async def _eval_future_subexpr(
    conn: "LeasedCapRPCConnection", expr: Expression[Any], *, context: str
) -> IRFuture[Any, Any]:
    """Evaluate a sub-expression expected to produce an :class:`IRFuture`.

    ``context`` names the slot (e.g. ``"AwaitFutureExpr.future"``) for mismatch errors.
    """
    value = await eval_as_value(conn, expr)
    if not isinstance(value, IRFuture):
        raise _phase2_type_mismatch(context=context, expected="IRFuture", value=value)
    return value


async def _eval_lease_subexpr(
    conn: "LeasedCapRPCConnection", expr: Expression[Any], *, context: str
) -> Lease[Any]:
    """Evaluate a sub-expression expected to produce a :class:`Lease`."""
    value = await eval_as_value(conn, expr)
    if not isinstance(value, Lease):
        raise _phase2_type_mismatch(context=context, expected="Lease", value=value)
    return value


async def _eval_lease_or_pipelined(
    conn: "LeasedCapRPCConnection",
    expr: Expression[Any],
    *,
    context: str,
) -> Lease[Any] | UnwrapForPipelining[Any, Any]:
    """Evaluate the dispatch-lease slot of a :class:`HandleRequestExpr` /
    :class:`HandleDispatchExpr`, returning either a concrete :class:`Lease`
    (the common case — :class:`ImportedLeaseExpr` / :class:`ExportedLeaseExpr`
    resolves through phase 1 to a :class:`ConstantExpr`) or an
    :class:`UnwrapForPipelining` that the local-impl ``handle.dispatch``
    runner will resolve in its spawned task.

    Mirrors :func:`eval_as_call_arg` for the shapes that produce
    :class:`UnwrapForPipelining`: :class:`AwaitFutureExpr` and
    :class:`HandleRequestExpr`. (:class:`HandleDispatchExpr` evaluates to a
    ``[future, lease]`` list, not a :class:`Lease`, so it has no pipelined
    form here.) Any other expression resolving to a :class:`Lease` falls
    back to the eager :func:`_eval_lease_subexpr` path.

    The caller (:func:`_dispatch_handle_request`) is responsible for
    discarding the resulting lease (or the inner-lease slot of an
    :class:`UnwrapForPipelining`) on any subsequent error before
    handing it off to ``handle.dispatch(...)``.
    """
    if isinstance(expr, AwaitFutureExpr):
        future, lease = await _eval_await_fut_and_lease(conn, expr)
        return UnwrapForPipelining(future=future, lease=lease)
    if isinstance(expr, HandleRequestExpr):
        future, result_lease = await _dispatch_handle_request(conn, expr)
        return UnwrapForPipelining(future=future, lease=result_lease)
    return await _eval_lease_subexpr(conn, expr, context=context)


async def _eval_handle_subexpr(
    conn: "LeasedCapRPCConnection", expr: Expression[Any], *, context: str
) -> IRHandle[Any]:
    """Evaluate a sub-expression expected to produce an :class:`IRHandle`."""
    value = await eval_as_value(conn, expr)
    if not isinstance(value, IRHandle):
        raise _phase2_type_mismatch(context=context, expected="IRHandle", value=value)
    return value


async def _eval_await_fut_and_lease(
    conn: "LeasedCapRPCConnection", expr: AwaitFutureExpr[Any]
) -> tuple[IRFuture[Any, Any], Lease[Any]]:
    """Evaluate an :class:`AwaitFutureExpr`'s ``future`` and ``lease`` sub-slots.

    Returns them as a pair for the caller to consume (via ``future.wait(lease)`` or a
    :class:`UnwrapForPipelining`). On failure evaluating the ``lease`` slot, any
    already-minted lease inside the ``future`` sub-tree would already have been
    discarded by that sub-evaluator's own cleanup — our only local cleanup is for
    the lease we're in the middle of computing. The future itself has no lease we
    own, so no cleanup is needed on the future side.
    """
    future = await _eval_future_subexpr(conn, expr.future, context="AwaitFutureExpr.future")
    lease = await _eval_lease_subexpr(conn, expr.lease, context="AwaitFutureExpr.lease")
    return future, lease


async def _dispatch_handle_request(
    conn: "LeasedCapRPCConnection",
    expr: HandleRequestExpr | HandleDispatchExpr,
) -> tuple[IRFuture[Any, Any], Lease[Any]]:
    """Evaluate the shared shape of :class:`HandleRequestExpr` /
    :class:`HandleDispatchExpr` and invoke ``handle.dispatch(lease, request)``.

    Steps:

    1. Evaluate ``handle`` (``_as_value``) → :class:`IRHandle`.
    2. Evaluate ``lease`` via :func:`_eval_lease_or_pipelined` → either a
       concrete :class:`Lease` *or* an :class:`UnwrapForPipelining` whose
       resolution is a :class:`Lease`. The latter is passed straight through
       to ``handle.dispatch`` so the dispatch's spawned runner awaits the
       inner future without blocking the reader-side phase-2 evaluator.
    3. Evaluate ``request`` (``Call.arg`` in ``_as_call_arg`` mode; :class:`GetItem`
       passes through).
    4. Sequentially await each :class:`AwaitFutureExpr` in ``wait_for`` (results
       discarded — ``wait_for`` is a sequencing construct).
    5. Hand the fully-resolved ``(lease, request)`` pair to ``handle.dispatch(...)``,
       which takes ownership per the standard lease-transfer convention.

    Cleanup on partial failure: if any step after ``lease`` has been resolved throws,
    we discard the lease (handling either a plain :class:`Lease` or the inner-lease
    slot of an :class:`UnwrapForPipelining`) and any leases inside the partially-
    resolved request before re-raising. Handles carry no lease of their own so no
    handle cleanup is needed.
    """
    handle = await _eval_handle_subexpr(conn, expr.handle, context=f"{type(expr).__name__}.handle")
    lease_or_pipelined = await _eval_lease_or_pipelined(
        conn, expr.lease, context=f"{type(expr).__name__}.lease"
    )
    try:
        request = await _eval_request(conn, expr.request)
        try:
            await _eval_wait_for(conn, expr.wait_for)
        except BaseException:
            discard_leases_in(request)
            raise
    except BaseException:
        if isinstance(lease_or_pipelined, UnwrapForPipelining):
            lease_or_pipelined.lease.discard()
        else:
            lease_or_pipelined.discard()
        raise
    # ``handle.dispatch`` consumes the lease (via ``.take()`` for the concrete
    # form, or the inner-lease take for the pipelined form) and any leases
    # inside ``request`` per the widened :meth:`IRHandle.dispatch` contract.
    return handle.dispatch(lease_or_pipelined, request)


async def _eval_request(
    conn: "LeasedCapRPCConnection",
    request: Call[Expression[Any]] | GetItem,
) -> Call[IRCallArg] | GetItem:
    """Evaluate a phase-1-substituted request to a concrete :class:`Request`.

    ``Call.arg`` evaluates in ``_as_call_arg`` mode so :class:`UnwrapForPipelining`
    leaves may appear (the handle's ``_unwrap_request_futures`` resolves them at
    dispatch boundary). :class:`GetItem` is immutable and passes through.
    """
    if isinstance(request, GetItem):
        return request
    if isinstance(request, Call):
        arg = await eval_as_call_arg(conn, request.arg)
        return Call(method=request.method, arg=arg)
    raise IRException(
        message=f"_eval_request: unsupported request type {type(request).__name__}",
        category="InternalError",
    )


async def _eval_wait_for(
    conn: "LeasedCapRPCConnection", wait_for: Sequence[AwaitFutureExpr[Any]]
) -> None:
    """Sequentially evaluate each :class:`AwaitFutureExpr` in ``wait_for`` in
    ``_as_value`` mode (actually awaits the future), discarding its result.

    ``wait_for`` is a protocol-level sequencing construct: the request body shouldn't
    start until these futures have resolved. Results are discarded (including any
    leases inside) — they're not observable to the request body.
    """
    for await_expr in wait_for:
        result = await eval_as_value(conn, await_expr)
        discard_leases_in(result)
