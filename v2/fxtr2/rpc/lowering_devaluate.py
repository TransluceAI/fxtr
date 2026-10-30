"""Outbound devaluator: user ``IRCallArg`` → ``Expression[T]`` for sending across the wire.

Runs synchronously from user code (``push`` / ``dispatch``). Walks an ``IRCallArg`` produced
by the user and builds an ``Expression[Any]`` suitable for sending over the wire, allocating
new export IDs / consulting the re-export tables as it goes.

Speculative: collects table updates, export-ID allocations, and lease takes into a local
batch and commits atomically. If any error is raised during the walk (invalid lease, foreign
handle, unsupported type), the batch is rolled back — including discarding any lease clones
made during the walk — and the error is re-raised to the caller. This keeps the connection
state consistent even on bad user input.

All public / module-private functions take ``conn`` as their first positional argument rather
than being methods on ``LeasedCapRPCConnection``, so that this module can be imported without
a circular dependency on ``lowering.py`` (the connection is referenced only under
``TYPE_CHECKING``).
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence, cast

from fxtr2.leases.lease_types import Lease, LifetimeToken
from fxtr2.leases.leased_cap_ir import (
    IRCallArg,
    IRException,
    IRFuture,
    IRHandle,
    IRStatic,
    IRTagged,
    UnwrapForPipelining,
)
from fxtr2.rpc.lowering_errors import RPCConnectionAborted
from fxtr2.rpc.lowering_remote_proxies import (
    RemoteGrant,
    RemoteIRFuture,
    RemoteIRHandle,
    RemoteLifetimeToken,
)
from fxtr2.rpc.lowering_state import (
    FutureExportEntry,
    GrantExportEntry,
    HandleExportEntry,
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
# Devaluator bookkeeping
####################################################################################################


@dataclasses.dataclass
class _DevaluateContext:
    """Speculative-mutation tracking for a single :func:`devaluate` walk.

    Every mutation the walk makes also appends a zero-arg undo callable to
    :attr:`undo_log`. That includes export-ID allocations, export-entry inserts,
    reverse-lookup-dict inserts, ``outstanding_count`` bumps, AND discards of leases
    we took to serve as ``_GrantExportEntry.backing_lease`` (the discard goes into the
    undo log rather than a separate "taken leases" list so rollback is a single
    ordered replay of the log).

    :attr:`bounce_back_leases` holds the outcome of ``take()`` for any bounce-back
    lease encountered in the walk (a lease whose grant is a ``RemoteGrant`` on this
    connection, i.e. the sender is handing back an imported lease via
    :class:`ImportedLeaseExpr`). These leases are special: we can't just discard them
    in-walk, because that would trigger the ``DiscardMessage`` for the import to land
    on the wire *before* the caller enqueues their expression-containing message,
    letting the peer tear down the export entry the ``ImportedLeaseExpr`` references
    and fail phase-1 lookup. Instead we take the lease (which substitutes our clone
    for the caller's original in ``live_leases``, so the discard-on-empty trigger
    doesn't fire mid-walk) and return the clones to the caller, who discards them
    *after* enqueueing their message — ensuring ``DiscardMessage`` lands second.

    On rollback: replay ``undo_log`` (which discards backing leases along with undoing
    table mutations) and discard any bounce-back leases we took (they're our property
    at that point; the caller won't see them). On success: the caller receives
    ``bounce_back_leases`` alongside the expression and is responsible for the
    post-enqueue discard.

    The undo log is strictly LIFO-correct because the export-ID allocator reserves
    IDs by decrementing a counter — undoing a later allocation first, then an earlier
    one, is the only order that keeps the counter consistent with the set of live
    allocations.

    ``undo_log`` entries return ``object`` (rather than ``None``) so that inline
    ``dict.pop(key, None)`` lambdas can be appended directly: ``pop`` returns the
    popped value, which we discard. The replay site (:func:`devaluate`) ignores
    return values.
    """

    undo_log: list[Callable[[], object]] = dataclasses.field(default_factory=list)
    bounce_back_leases: list[Lease[Any]] = dataclasses.field(default_factory=list)


####################################################################################################
# Public entry point
####################################################################################################


def devaluate(
    conn: "LeasedCapRPCConnection",
    value: IRCallArg,
) -> tuple[Expression[Any], list[Lease[Any]]]:
    """Walk ``value`` and produce an equivalent ``Expression[Any]`` for sending across
    the wire, plus a list of bounce-back leases the caller must discard
    *after* enqueueing the expression-containing message.

    The walk mutates connection state directly — allocating export IDs, inserting
    into the export tables and reverse-lookup dicts, bumping ``outstanding_count``
    on re-exports, and taking ownership of caller-provided leases (as either
    ``_GrantExportEntry.backing_lease`` for fresh exports, or as deferred-discard
    clones for bounce-backs). To keep the table state consistent in the face of
    partial failure, each mutation records an undo in ``ctx.undo_log``. If the
    walk raises — invalid lease, unsupported type, etc. — we replay the undo log
    in reverse to restore tables, discard any bounce-back clones we took (they
    belong to us on the failure path), and re-raise.

    Returns:
        ``(expression, bounce_back_leases)``. ``expression`` is the devaluated
        tree ready to embed in a message. ``bounce_back_leases`` is a list of
        cloned leases — every user-provided lease whose grant is a ``RemoteGrant``
        on this connection produces one entry here.

        Caller contract:

        - MUST discard each lease in ``bounce_back_leases`` eventually, to avoid
          leaking peer-side ``backing_lease`` references.
        - MUST enqueue the expression-containing message *before* discarding any
          bounce-back lease whose discard would cause the containing
          ``_GrantImportEntry.live_leases`` to empty — because that final discard
          is what triggers the outbound ``DiscardMessage``, and the peer must see
          that message *after* the one that references the ``ImportedLeaseExpr``.
          The simplest way to honor this: enqueue the caller's message first,
          then walk the list and discard.
        - If the caller's own code raises between receiving the tuple and
          enqueueing (e.g. message construction fails), the caller must still
          discard every bounce-back lease. In that case no message went out, so
          the ordering invariant is vacuously satisfied — the ``DiscardMessage``
          that the discards trigger just goes out standalone.
    """
    ctx = _DevaluateContext()
    expression = _run_with_rollback(conn, ctx, lambda: _devaluate_walk(conn, value, ctx))
    return expression, ctx.bounce_back_leases


def devaluate_handle_request(
    conn: "LeasedCapRPCConnection",
    *,
    handle_import_id: int,
    handle_lease: Lease[Any] | UnwrapForPipelining[Any, Any],
    request: Call[IRCallArg] | GetItem,
    wait_for: Sequence[UnwrapForPipelining[Any, Any]] = (),
) -> tuple[Expression[Any], list[Lease[Any]]]:
    """Build a :class:`HandleRequestExpr` from the components of an outbound dispatch.

    Used by :meth:`RemoteIRHandle.dispatch`. Devaluates ``handle_lease``, ``request``'s
    ``Call.arg`` (if any), and each :class:`UnwrapForPipelining` in ``wait_for`` under a
    single shared :class:`_DevaluateContext` so that any failure part-way through the walk
    rolls back atomically — including discarding any ``_GrantExportEntry.backing_lease``
    we took.

    ``handle_lease`` may be either a plain :class:`Lease` (the common case) or an
    :class:`UnwrapForPipelining` whose resolution is a :class:`Lease` (for outbound
    pipelined dispatches whose outer lease comes from an upstream future). The
    devaluator's :func:`_devaluate_walk` already encodes the latter as an
    :class:`AwaitFutureExpr` in the lease slot — no extra branch needed at this layer.

    The ``handle`` slot is always an :class:`ImportedIRHandleExpr` pointing at the
    caller-provided ``handle_import_id``; we don't re-devaluate the handle itself,
    because its identity is fixed at the proxy level. See
    :func:`devaluate_pipeline_dispatch` for the variant whose ``handle`` slot is
    built from an ``AwaitFutureExpr`` (for pipelined calls on remote futures).

    Returns ``(expression, bounce_back_leases)`` with the same caller contract as
    :func:`devaluate` — discard the bounce-back leases *after* enqueueing the message
    that carries the returned expression.
    """
    ctx = _DevaluateContext()

    def _build() -> Expression[Any]:
        handle_expr = ImportedIRHandleExpr(import_id=handle_import_id)
        return _build_handle_request_expr(
            conn,
            ctx,
            handle_expr=handle_expr,
            handle_lease=handle_lease,
            request=request,
            wait_for=wait_for,
        )

    expression = _run_with_rollback(conn, ctx, _build)
    return expression, ctx.bounce_back_leases


def devaluate_pipeline_dispatch(
    conn: "LeasedCapRPCConnection",
    *,
    future_import_id: int,
    future_attr_path: Sequence[str | int],
    future_lease: Lease[Any],
    inner_handle_lease: Lease[Any] | UnwrapForPipelining[Any, Any],
    request: Call[IRCallArg] | GetItem,
    wait_for: Sequence[UnwrapForPipelining[Any, Any]] = (),
) -> tuple[Expression[Any], list[Lease[Any]]]:
    """Build a :class:`HandleRequestExpr` for a pipelined call on a remote future.

    Used by :meth:`RemoteIRFuture.pipeline_dispatch`. The ``handle`` slot of the
    resulting :class:`HandleRequestExpr` is an :class:`AwaitFutureExpr` over the
    remote future (optionally wrapped in :class:`WalkFutureExpr` when
    ``future_attr_path`` is non-empty) and the devaluated ``future_lease``; the
    peer evaluates this to the walked future's eventual resolution (expected to
    be an :class:`IRHandle`) and dispatches ``request`` on it.

    ``inner_handle_lease`` names the lease the peer should use when dispatching
    the inner call. It may be a plain :class:`Lease` (passed through
    :func:`_devaluate_walk` as usual — bounce-back or fresh export depending on
    where the lease came from) or an :class:`UnwrapForPipelining` whose
    resolution is itself a :class:`Lease` (the devaluator encodes this as an
    :class:`AwaitFutureExpr` in the lease slot).

    Devaluator semantics (atomic-on-failure rollback, ``bounce_back_leases``
    deferred-discard contract) match :func:`devaluate` and
    :func:`devaluate_handle_request`.
    """
    ctx = _DevaluateContext()

    def _build() -> Expression[Any]:
        # Build the handle slot: AwaitFutureExpr(WalkFutureExpr?(ImportedIRFutureExpr(id),
        # path), <devaluated future_lease>). WalkFutureExpr is omitted when the
        # ``attr_path`` is empty, matching the devaluator's treatment of a bounce-back
        # IRFuture without a walk.
        future_slot_expr: Expression[IRFuture[Any, Any]] = ImportedIRFutureExpr(
            import_id=future_import_id
        )
        if future_attr_path:
            future_slot_expr = WalkFutureExpr(
                future=future_slot_expr,
                attr_path=tuple(future_attr_path),
            )
        future_lease_expr = _devaluate_walk(conn, future_lease, ctx)
        handle_expr: Expression[Any] = AwaitFutureExpr(
            future=future_slot_expr,
            lease=cast(Expression[Lease[Any]], future_lease_expr),
        )
        return _build_handle_request_expr(
            conn,
            ctx,
            handle_expr=handle_expr,
            handle_lease=inner_handle_lease,
            request=request,
            wait_for=wait_for,
        )

    expression = _run_with_rollback(conn, ctx, _build)
    return expression, ctx.bounce_back_leases


def _build_handle_request_expr(
    conn: "LeasedCapRPCConnection",
    ctx: _DevaluateContext,
    *,
    handle_expr: Expression[Any],
    handle_lease: Lease[Any] | UnwrapForPipelining[Any, Any],
    request: Call[IRCallArg] | GetItem,
    wait_for: Sequence[UnwrapForPipelining[Any, Any]],
) -> Expression[Any]:
    """Shared inner builder for :class:`HandleRequestExpr` construction.

    Devaluates ``handle_lease`` (a plain :class:`Lease` or
    :class:`UnwrapForPipelining`), ``request``'s ``Call.arg`` (if any), and each
    ``wait_for`` entry under the shared ``ctx`` so all mutations roll back together.
    ``handle_expr`` is used verbatim — each caller produces its own handle-slot shape
    (a plain :class:`ImportedIRHandleExpr` for normal dispatch, an
    :class:`AwaitFutureExpr` for pipeline dispatch).
    """
    lease_expr = _devaluate_walk(conn, handle_lease, ctx)
    if isinstance(request, Call):
        arg_expr = _devaluate_walk(conn, request.arg, ctx)
        request_expr: Call[Expression[Any]] | GetItem = Call[Expression[Any]](
            method=request.method, arg=arg_expr
        )
    else:
        request_expr = request
    wait_for_exprs: list[AwaitFutureExpr[Any]] = []
    for unwrap in wait_for:
        fut_expr = _devaluate_walk(conn, unwrap.future, ctx)
        uwlease_expr = _devaluate_walk(conn, unwrap.lease, ctx)
        # The documented protocol invariant on
        # ``FutureExportEntry.resolve_obligation_count`` is "+1 per outbound
        # ``AwaitFutureExpr`` the devaluator emits referencing this export",
        # symmetric with the receiver's
        # ``FutureImportEntry.resolve_obligation_refcount`` rule which honors
        # *every* inbound ``AwaitFutureExpr`` uniformly (via
        # ``_phase1_walk``). Without these two calls, an ``AwaitFutureExpr``
        # in the ``wait_for`` slot of a :class:`HandleRequestExpr` /
        # :class:`HandleDispatchExpr` ships an obligation to the peer (peer
        # bumps refcount on receipt and skips its own :class:`PullMessage`)
        # without spawning the local resolver that's supposed to deliver it
        # — and the peer's ``_eval_wait_for`` blocks forever. Same shape as
        # the ``UnwrapForPipelining`` branch of :func:`_devaluate_walk`; see
        # ``plans/extras/extra6_devaluator_obligation_bug.md``.
        _pair_grant_with_future_export(conn, fut_expr, uwlease_expr, ctx)
        _bump_await_future_obligation(conn, fut_expr, ctx)
        wait_for_exprs.append(
            AwaitFutureExpr(
                future=cast(Expression[IRFuture[Any, Any]], fut_expr),
                lease=cast(Expression[Lease[Any]], uwlease_expr),
            )
        )
    return HandleRequestExpr(
        handle=cast(Expression[IRHandle[Any]], handle_expr),
        lease=cast(Expression[Lease[Any]], lease_expr),
        request=request_expr,
        wait_for=tuple(wait_for_exprs),
    )


def _run_with_rollback(
    conn: "LeasedCapRPCConnection",
    ctx: _DevaluateContext,
    build: Callable[[], Expression[Any]],
) -> Expression[Any]:
    """Run ``build`` under ``ctx``'s speculative-mutation tracking; roll back on error.

    Shared machinery for :func:`devaluate` and :func:`devaluate_handle_request`.
    Rollback replays ``ctx.undo_log`` in reverse (which includes discarding backing
    leases of any ``_GrantExportEntry`` inserts we rolled back), then drops any
    bounce-back clones we took. If an undo itself raises, tables are in a partial
    state the connection can't reconcile — abort the connection and escalate.
    """
    try:
        return build()
    except BaseException:
        try:
            for undo in reversed(ctx.undo_log):
                undo()
        except Exception as undo_exc:
            abort_reason = IRException(
                message=f"devaluate rollback failed, aborting: {undo_exc}",
                category="DevaluatorRollbackError",
            )
            conn._abort(abort_reason)
            for lease in ctx.bounce_back_leases:
                lease.discard()
            raise RPCConnectionAborted(abort_reason.message) from undo_exc
        for lease in ctx.bounce_back_leases:
            lease.discard()
        raise


####################################################################################################
# Walk helpers (module-private)
####################################################################################################


def _devaluate_walk(
    conn: "LeasedCapRPCConnection", value: IRCallArg, ctx: _DevaluateContext
) -> Expression[Any]:
    """Recursive workhorse for :func:`devaluate`. See parent docstring for semantics."""
    # Primitives → StaticExpr. Share by reference (immutable).
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return StaticExpr(cast(IRStatic, value))
    # IRException — satisfies IRStatic, so a plain StaticExpr is sufficient. Does not
    # require recursing into its ``data`` because IRException's data field is
    # guaranteed static by typing.
    if isinstance(value, IRException):
        return StaticExpr(cast(IRStatic, value))
    # IRTagged — static tag, recursive value.
    if isinstance(value, IRTagged):
        inner = _devaluate_walk(conn, value.value, ctx)
        return IRTaggedExpr(tag=value.tag, value=inner)
    # UnwrapForPipelining → AwaitFutureExpr. Both fields recurse through the walker
    # so that the future and lease inside get devaluated as their own Expressions.
    if isinstance(value, UnwrapForPipelining):
        future_expr = _devaluate_walk(conn, value.future, ctx)
        lease_expr = _devaluate_walk(conn, value.lease, ctx)
        # If the devaluated future slot references one of our future exports
        # (direct or walked), bump that export's ``resolve_obligation_count``:
        # each outbound ``AwaitFutureExpr`` referring to one of our exports
        # creates an implicit resolve obligation per the :class:`UnpullMessage`
        # protocol rule. Recorded with an undo in ``ctx.undo_log`` so a later
        # devaluator failure rolls the bump back atomically along with any
        # entry allocations made for this expression.
        #
        # Order is load-bearing: pair the freshly-devaluated grant onto the
        # future entry *before* the bump, because the bump's 0 → positive
        # crossing spawns a resolver that needs ``grant_export_id`` populated.
        _pair_grant_with_future_export(conn, future_expr, lease_expr, ctx)
        _bump_await_future_obligation(conn, future_expr, ctx)
        return AwaitFutureExpr(
            future=cast(Expression[IRFuture[Any, Any]], future_expr),
            lease=cast(Expression[Lease[Any]], lease_expr),
        )
    # Leases — distinguish bounce-back (a lease imported from this connection) from
    # fresh export (any other lease, which will be re-granted on the peer side).
    if isinstance(value, Lease):
        return _devaluate_lease(conn, value, ctx)
    # Handles, futures, tokens — export by identity.
    if isinstance(value, IRHandle):
        return _devaluate_handle(conn, value, ctx)
    if isinstance(value, IRFuture):
        return _devaluate_future(conn, value, ctx)
    if isinstance(value, LifetimeToken):
        return _devaluate_token(conn, value, ctx)
    # Containers — recurse. Mapping check before Sequence because dicts satisfy
    # Mapping but not Sequence; ``str`` / ``bytes`` are already handled by the
    # primitive branch above so we don't end up iterating their characters.
    # The isinstance narrowing here needs an explicit cast because ``IRCallArg``
    # is a wide union where several branches look like Sequence (list, bytes, str)
    # and Mapping (dict) without pyright being able to narrow cleanly.
    if isinstance(value, Mapping):
        mapping_value = cast(Mapping[str, IRCallArg], value)
        items: dict[str, Expression[Any]] = {
            k: _devaluate_walk(conn, v, ctx) for k, v in mapping_value.items()
        }
        return DictExpr(items=items)
    if isinstance(value, Sequence):
        sequence_value = cast(Sequence[IRCallArg], value)
        elements = tuple(_devaluate_walk(conn, v, ctx) for v in sequence_value)
        return ListExpr(elements=elements)
    raise TypeError(f"_devaluate: unsupported value of type {type(value).__name__}: {value!r}")


def _devaluate_lease(
    conn: "LeasedCapRPCConnection", lease: Lease[Any], ctx: _DevaluateContext
) -> Expression[Any]:
    """Devaluate a Lease: bounce-back (our import) or fresh export."""
    lease.ensure_active()
    grant = lease.grant
    # Bounce-back: lease's grant is a ``RemoteGrant`` that names this connection.
    # Emit an ``ImportedLeaseExpr`` referring to the original import. Instead of
    # discarding the lease now (which would empty ``_GrantImportEntry.live_leases``
    # and trigger an outbound ``DiscardMessage`` before the caller's message has
    # been enqueued — letting the peer tear down the export the
    # ``ImportedLeaseExpr`` references before phase-1 can resolve it), we
    # ``take()`` the lease: that substitutes our clone for the caller's original
    # in ``live_leases`` so the count stays constant and no ``DiscardMessage``
    # fires here. The clone ends up in ``ctx.bounce_back_leases``, returned to
    # the caller who discards it post-enqueue.
    if isinstance(grant, RemoteGrant) and grant.conn is conn:
        ctx.bounce_back_leases.append(lease.take())
        return ImportedLeaseExpr(grant_import_id=grant.grant_import_id)
    # Fresh export path. Check whether we've exported this grant before.
    existing_id = conn._grant_export_id_by_grant.get(grant)
    if existing_id is not None:
        # Re-introduction: bump the grant's outstanding_count AND the subject
        # token's — an ``ExportedLeaseExpr`` references both IDs, and the
        # protocol rule is "each ``Exported…Expr`` that references this ID"
        # = one introduction. Drop the caller's lease (the backing lease from
        # the first introduction is still authoritative).
        entry = conn._exports[existing_id]
        assert isinstance(
            entry, GrantExportEntry
        ), f"_grant_export_id_by_grant pointed at a non-grant entry for {grant!r}"
        _bump_outstanding_count(entry, ctx)
        _bump_subject_token_outstanding_count(conn, entry.token_export_id, ctx)
        lease.discard()
        return ExportedLeaseExpr(
            grant_export_id=existing_id,
            token_export_id=entry.token_export_id,
        )
    # First-time export of this grant. We need both a grant export ID and an
    # export ID for its subject token (allocated only if the token isn't already
    # exported); take the caller's lease into the grant entry's ``backing_lease``.
    grant_export_id = conn._allocate_export_id()
    ctx.undo_log.append(lambda: setattr(conn, "_next_negative_export_id", grant_export_id))
    subject_token = grant.subject
    token_export_id = _ensure_token_exported(conn, subject_token, ctx)
    # Take the lease and register an undo to discard it on rollback. On success
    # the lease lives on as ``_GrantExportEntry.backing_lease`` and is eventually
    # released when the peer's final ``DiscardMessage`` arrives (§2f).
    backing_lease = lease.take()
    ctx.undo_log.append(lambda: backing_lease.discard())
    grant_entry = GrantExportEntry(
        export_id=grant_export_id,
        token_export_id=token_export_id,
        backing_lease=backing_lease,
        outstanding_count=1,
    )
    conn._exports[grant_export_id] = grant_entry
    ctx.undo_log.append(lambda: conn._exports.pop(grant_export_id, None))
    conn._grant_export_id_by_grant[grant] = grant_export_id
    ctx.undo_log.append(lambda: conn._grant_export_id_by_grant.pop(grant, None))
    conn._active_grant_exports.add(grant_export_id)
    ctx.undo_log.append(lambda: conn._active_grant_exports.discard(grant_export_id))
    # Subscribe to the backing lease's broken-callback so local revocation of the
    # underlying lifetime propagates to the peer as a ``BreakMessage``. Done after
    # the entry is in ``_exports`` so the callback's lookup finds something. Also
    # registered in the undo log: on rollback we tear down the subscription so the
    # discarded backing lease doesn't fire our callback while re-winding.
    conn._subscribe_backing_lease_break(grant_entry)

    def _undo_break_subscription() -> None:
        dereg = grant_entry.broken_callback_deregister
        if dereg is not None:
            dereg()
            grant_entry.broken_callback_deregister = None

    ctx.undo_log.append(_undo_break_subscription)
    return ExportedLeaseExpr(
        grant_export_id=grant_export_id,
        token_export_id=token_export_id,
    )


def _devaluate_handle(
    conn: "LeasedCapRPCConnection", handle: IRHandle[Any], ctx: _DevaluateContext
) -> Expression[Any]:
    """Devaluate an IRHandle: bounce-back (our import) or fresh export."""
    if isinstance(handle, RemoteIRHandle) and handle.conn is conn:
        return ImportedIRHandleExpr(import_id=handle.handle_import_id)
    handle_key = id(handle)
    existing_id = conn._handle_export_id_by_handle_id.get(handle_key)
    if existing_id is not None:
        entry = conn._exports[existing_id]
        assert isinstance(
            entry, HandleExportEntry
        ), f"_handle_export_id_by_handle_id pointed at a non-handle entry for {handle!r}"
        _bump_outstanding_count(entry, ctx)
        _bump_subject_token_outstanding_count(conn, entry.token_export_id, ctx)
        return ExportedIRHandleExpr(
            handle_export_id=existing_id,
            token_export_id=entry.token_export_id,
        )
    # First-time export.
    handle_export_id = conn._allocate_export_id()
    ctx.undo_log.append(lambda: setattr(conn, "_next_negative_export_id", handle_export_id))
    token_export_id = _ensure_token_exported(conn, handle.token, ctx)
    handle_entry = HandleExportEntry(
        export_id=handle_export_id,
        token_export_id=token_export_id,
        local_handle=handle,
        outstanding_count=1,
    )
    conn._exports[handle_export_id] = handle_entry
    ctx.undo_log.append(lambda: conn._exports.pop(handle_export_id, None))
    conn._handle_export_id_by_handle_id[handle_key] = handle_export_id
    ctx.undo_log.append(lambda: conn._handle_export_id_by_handle_id.pop(handle_key, None))
    return ExportedIRHandleExpr(
        handle_export_id=handle_export_id,
        token_export_id=token_export_id,
    )


def _devaluate_future(
    conn: "LeasedCapRPCConnection", future: IRFuture[Any, Any], ctx: _DevaluateContext
) -> Expression[Any]:
    """Devaluate an IRFuture: bounce-back (our import) or fresh export."""
    if isinstance(future, RemoteIRFuture) and future.conn is conn:
        # Bounce-back — preserve any accumulated walk path via a WalkFutureExpr wrap.
        base = ImportedIRFutureExpr(import_id=future.future_import_id)
        if not future.attr_path:
            return base
        return WalkFutureExpr(
            future=cast(Expression[IRFuture[Any, Any]], base),
            attr_path=future.attr_path,
        )
    future_key = id(future)
    existing_id = conn._future_export_id_by_future_id.get(future_key)
    if existing_id is not None:
        entry = conn._exports[existing_id]
        assert isinstance(
            entry, FutureExportEntry
        ), f"_future_export_id_by_future_id pointed at a non-future entry for {future!r}"
        _bump_outstanding_count(entry, ctx)
        _bump_subject_token_outstanding_count(conn, entry.token_export_id, ctx)
        return ExportedIRFutureExpr(
            future_export_id=existing_id,
            token_export_id=entry.token_export_id,
        )
    future_export_id = conn._allocate_export_id()
    ctx.undo_log.append(lambda: setattr(conn, "_next_negative_export_id", future_export_id))
    token_export_id = _ensure_token_exported(conn, future.token, ctx)
    future_entry = FutureExportEntry(
        export_id=future_export_id,
        token_export_id=token_export_id,
        local_future=future,
        outstanding_count=1,
        resolve_obligation_count=0,
        resolve_task_cancel_scope=None,
        # ``grant_export_id`` is left ``None`` here; it is populated lazily by
        # :func:`_pair_grant_with_future_export` once an accompanying lease has been
        # devaluated in the same walk (or by :func:`_on_push` when the future +
        # grant pair are registered together).
        grant_export_id=None,
    )
    conn._exports[future_export_id] = future_entry
    ctx.undo_log.append(lambda: conn._exports.pop(future_export_id, None))
    conn._future_export_id_by_future_id[future_key] = future_export_id
    ctx.undo_log.append(lambda: conn._future_export_id_by_future_id.pop(future_key, None))
    return ExportedIRFutureExpr(
        future_export_id=future_export_id,
        token_export_id=token_export_id,
    )


def _devaluate_token(
    conn: "LeasedCapRPCConnection", token: LifetimeToken[Any], ctx: _DevaluateContext
) -> Expression[Any]:
    """Devaluate a LifetimeToken: bounce-back (our import) or fresh export."""
    if isinstance(token, RemoteLifetimeToken) and token.conn is conn:
        return ImportedLifetimeTokenExpr(import_id=token.token_import_id)
    token_export_id = _ensure_token_exported(conn, token, ctx)
    # ``_ensure_token_exported`` bumps the outstanding_count on re-introduction and
    # allocates on first introduction, so we don't need additional bookkeeping here.
    return ExportedLifetimeTokenExpr(token_export_id=token_export_id)


def _ensure_token_exported(
    conn: "LeasedCapRPCConnection", token: LifetimeToken[Any], ctx: _DevaluateContext
) -> int:
    """Find (or create) the export ID for ``token``, bumping ``outstanding_count``
    on an existing entry or registering a new one. All mutations are recorded in
    the undo log so the caller can roll back on failure downstream.

    Per the protocol's refcount rule, every ``Exported…Expr`` that references this
    token ID — whether a standalone :class:`ExportedLifetimeTokenExpr` or the
    ``token_export_id`` field on a grant/handle/future introduction — counts as
    exactly one introduction and bumps ``outstanding_count`` by 1.
    """
    existing_id = conn._token_export_id_by_token.get(token)
    if existing_id is not None:
        entry = conn._exports[existing_id]
        assert isinstance(
            entry, TokenExportEntry
        ), f"_token_export_id_by_token pointed at a non-token entry for {token!r}"
        _bump_outstanding_count(entry, ctx)
        return existing_id
    token_export_id = conn._allocate_export_id()
    ctx.undo_log.append(lambda: setattr(conn, "_next_negative_export_id", token_export_id))
    token_entry = TokenExportEntry(
        export_id=token_export_id,
        local_token=token,
        outstanding_count=1,
        watch_deregister=None,
    )
    conn._exports[token_export_id] = token_entry
    ctx.undo_log.append(lambda: conn._exports.pop(token_export_id, None))
    conn._token_export_id_by_token[token] = token_export_id
    ctx.undo_log.append(lambda: conn._token_export_id_by_token.pop(token, None))
    return token_export_id


def _bump_outstanding_count(
    entry: TokenExportEntry | GrantExportEntry | HandleExportEntry | FutureExportEntry,
    ctx: _DevaluateContext,
) -> None:
    """Increment an export entry's ``outstanding_count`` and record the reverse op."""
    entry.outstanding_count += 1
    # The inline-lambda rollback reads ``entry.outstanding_count`` at rollback time
    # and subtracts 1. LIFO-correct: if the same entry is bumped twice, the second
    # rollback decrements first (restoring to +1), then the first rollback
    # decrements again (restoring to 0).
    ctx.undo_log.append(lambda: setattr(entry, "outstanding_count", entry.outstanding_count - 1))


def _bump_subject_token_outstanding_count(
    conn: "LeasedCapRPCConnection", token_export_id: int, ctx: _DevaluateContext
) -> None:
    """Bump ``outstanding_count`` on the token export entry at ``token_export_id``.

    Used on the re-export path of grants/handles/futures: each ``Exported…Expr`` we
    emit carries both the object's ID and its subject token's ID, and per the
    protocol each reference is one introduction. First-time exports use
    :func:`_ensure_token_exported` instead (which allocates the entry at
    outstanding_count=1); this helper is just the bump half of that in a form
    suitable for re-use paths.
    """
    token_entry = conn._exports[token_export_id]
    assert isinstance(
        token_entry, TokenExportEntry
    ), f"token_export_id {token_export_id} does not point at a TokenExportEntry"
    _bump_outstanding_count(token_entry, ctx)


def _bump_await_future_obligation(
    conn: "LeasedCapRPCConnection", future_expr: Expression[Any], ctx: _DevaluateContext
) -> None:
    """Bump ``resolve_obligation_count`` on the future export entry, if any, that
    ``future_expr`` references. Spawns a resolver on the 0 → positive transition,
    symmetric with :func:`_on_pull`'s spawn behavior.

    Recognized shapes (symmetric with the inbound phase-1 side):

    - :class:`ExportedIRFutureExpr` — direct reference to one of our future exports.
    - :class:`WalkFutureExpr` wrapping an ``ExportedIRFutureExpr`` — walks into a
      sub-value of the same future; the root future is what carries the obligation.

    Anything else (e.g. an ``ImportedIRFutureExpr`` bounce-back, a nested
    ``AwaitFutureExpr``) doesn't create an outbound obligation: the former lives
    entirely on the peer side (they're re-using one of their own exports), the
    latter is an optimization hint that the peer is free to forward without
    pulling.

    Routes the count change through
    :meth:`LeasedCapRPCConnection._increment_resolve_obligation_count`, which folds in
    the resolver-spawn invariant: on the 0 → positive crossing it spawns a resolver
    task that will eventually emit a :class:`ResolveMessage` / :class:`RejectMessage`.
    The :attr:`FutureExportEntry.grant_export_id` must already be populated by the
    caller (see :func:`_pair_grant_with_future_export` invoked just above in
    :func:`_devaluate_walk`'s ``UnwrapForPipelining`` branch).

    Records an undo in ``ctx.undo_log`` so a later devaluator failure rolls the
    increment back (LIFO, along with any entry allocation in the same walk). The
    rollback goes through :meth:`_decrement_resolve_obligation_count`, which
    symmetrically cancels the resolver if the undo crosses positive → 0 — the call
    site itself stays oblivious to resolver lifecycle.
    """
    target = future_expr
    if isinstance(target, WalkFutureExpr):
        target = target.future
    if not isinstance(target, ExportedIRFutureExpr):
        return
    entry = conn._exports.get(target.future_export_id)
    assert isinstance(entry, FutureExportEntry)
    if entry.resolved_sent:
        # Late reference to an already-resolved future export — our
        # ResolveMessage / RejectMessage is already on the wire and the peer
        # evaluates the AwaitFutureExpr against it; no obligation to add, no
        # resolver to spawn (and nothing to undo). Mirrors :func:`_on_pull`'s
        # ``resolved_sent`` no-op; required by
        # :meth:`_increment_resolve_obligation_count`'s contract ("after
        # ``resolved_sent`` is set this method must never be called").
        return
    conn._increment_resolve_obligation_count(entry)
    ctx.undo_log.append(lambda: conn._decrement_resolve_obligation_count(entry))


def _pair_grant_with_future_export(
    conn: "LeasedCapRPCConnection",
    future_expr: Expression[Any],
    lease_expr: Expression[Any],
    ctx: _DevaluateContext,
) -> None:
    """Populate :attr:`FutureExportEntry.grant_export_id` from an accompanying lease.

    Called from the ``UnwrapForPipelining`` branch of :func:`_devaluate_walk` after
    both the future slot and the lease slot have been devaluated. The grant whose
    backing lease was just taken (now living on the connection as a
    :class:`GrantExportEntry`) is the one the future export's resolver task should
    clone from. If the future entry already has ``grant_export_id`` set (from a prior
    pairing — e.g. :func:`_on_push` registration or an earlier outbound expression),
    this is a no-op.

    Shapes recognized:

    - ``future_expr`` is an :class:`ExportedIRFutureExpr` (or :class:`WalkFutureExpr`
      wrapping one) and ``lease_expr`` is an :class:`ExportedLeaseExpr`. Anything else
      means the future is not one of our exports (no obligation arises) or the lease
      is a bounce-back / not yet exported (the lease is then logically on the peer's
      side, and pairing doesn't apply).

    Records an undo in ``ctx.undo_log`` clearing the field if we set it here, so a
    rollback restores the pre-walk state.
    """
    target = future_expr
    if isinstance(target, WalkFutureExpr):
        target = target.future
    if not isinstance(target, ExportedIRFutureExpr):
        return
    if not isinstance(lease_expr, ExportedLeaseExpr):
        return
    entry = conn._exports.get(target.future_export_id)
    if not isinstance(entry, FutureExportEntry):
        return
    if entry.grant_export_id is not None:
        return
    entry.grant_export_id = lease_expr.grant_export_id

    def _undo() -> None:
        entry.grant_export_id = None

    ctx.undo_log.append(_undo)
