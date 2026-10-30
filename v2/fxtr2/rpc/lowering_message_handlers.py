"""Per-message handlers for LeasedCapRPCConnection.

All public / module-private functions take ``conn`` as their first positional argument
rather than being methods on ``LeasedCapRPCConnection``. The connection delegates to
:func:`dispatch_message` via a thin ``_dispatch_message`` wrapper; ``dispatch_message``
in turn routes to the appropriate ``_on_*`` module-private function.

The ``_on_*`` handlers are synchronous and non-blocking: they only mutate tables, fire
synchronous local callbacks, and optionally spawn follow-up async work into the
connection's task group. Anything that needs to ``await`` (evaluating a
``HandleRequestExpr``, delivering a resolution, etc.) is spawned rather than awaited
inline, so the reader loop stays free to receive the next message.

Error-handling policy
---------------------

Each handler distinguishes two kinds of anomaly:

- **Protocol violation** — the peer's message implies state we've never had (e.g.
  ``PullMessage`` for an ID we never allocated, ``DiscardMessage`` with a refcount
  that overshoots our ``outstanding_count``, ``BreakMessage`` for a grant we never
  exported, kind-mismatched re-introduction of an ID). Abort the connection with a
  descriptive :class:`IRException` via :func:`phase1_protocol_violation` or an
  explicit ``conn._abort(...)`` + ``raise RPCConnectionAborted(...)``.

- **Teardown race** — the peer's message refers to state we *used* to have but have
  already torn down through our own local path (e.g. ``DiscardMessage`` for a grant
  we've just locally revoked via ``on_break`` and removed from ``_exports``,
  ``ClosedMessage`` for a token we already ended). Silent no-op: the peer couldn't
  have known we'd torn it down before their message was in flight, and our local
  state is already correct.

In practice the distinction is "peer references state we've never had" (abort) vs
"peer references state we no longer have" (no-op). Duplicate redundant messages
(e.g. a second :class:`BreakMessage` for an already-broken grant) count as teardown
races — the protocol leaves "re-send or not" to the sender's discretion.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import anyio

from fxtr2.leases.lease_types import Lease
from fxtr2.leases.leased_cap_common import (
    IRFutureLocalImpl,
    PendingResultBox,
    discard_leases_in,
    spawn_ir_future_in_task_group,
    wrap_exception,
)
from fxtr2.leases.leased_cap_ir import IRException, IRFuture, IRValue, UnwrapForPipelining
from fxtr2.rpc.lowering_devaluate import devaluate
from fxtr2.rpc.lowering_errors import HandshakeError, RPCConnectionAborted
from fxtr2.rpc.lowering_evaluate import (
    eval_forwardable,
    eval_static,
    phase1_protocol_violation,
    phase1_substitute,
)
from fxtr2.rpc.lowering_remote_proxies import RemoteIRFuture, RemoteLifetimeToken
from fxtr2.rpc.lowering_state import (
    FutureExportEntry,
    FutureImportEntry,
    GrantExportEntry,
    GrantImportEntry,
    HandleExportEntry,
    TokenExportEntry,
    TokenImportEntry,
)
from fxtr2.rpc.lowering_types import (
    AbortMessage,
    BreakMessage,
    ClosedMessage,
    DiscardMessage,
    HelloMessage,
    PullMessage,
    PushMessage,
    RejectMessage,
    ResolveMessage,
    RPCMessage,
    UnpullMessage,
    WatchMessage,
)

if TYPE_CHECKING:
    from fxtr2.rpc.lowering import LeasedCapRPCConnection


logger = logging.getLogger(__name__)


def dispatch_message(conn: "LeasedCapRPCConnection", msg: RPCMessage) -> None:
    """Route an incoming RPCMessage to the appropriate ``_on_*`` handler.

    Enforces the protocol rule that the peer's first inbound message must be a
    :class:`HelloMessage`: any other message received before we've seen a Hello
    aborts the connection with a :class:`HandshakeError`.
    """
    if not conn._peer_hello_received and not isinstance(msg, HelloMessage):
        reason = IRException(
            message=f"peer's first message was {type(msg).__name__}, expected HelloMessage",
            category="HandshakeError",
        )
        conn._abort(reason)
        raise HandshakeError(reason.message)
    if isinstance(msg, HelloMessage):
        _on_hello(conn, msg)
    elif isinstance(msg, PushMessage):
        _on_push(conn, msg)
    elif isinstance(msg, PullMessage):
        _on_pull(conn, msg)
    elif isinstance(msg, ResolveMessage):
        _on_resolve(conn, msg)
    elif isinstance(msg, RejectMessage):
        _on_reject(conn, msg)
    elif isinstance(msg, WatchMessage):
        _on_watch(conn, msg)
    elif isinstance(msg, ClosedMessage):
        _on_closed(conn, msg)
    elif isinstance(msg, BreakMessage):
        _on_break(conn, msg)
    elif isinstance(msg, DiscardMessage):
        _on_discard(conn, msg)
    elif isinstance(msg, UnpullMessage):
        _on_unpull(conn, msg)
    elif isinstance(msg, AbortMessage):
        _on_abort(conn, msg)
    else:  # pyright: ignore[reportUnnecessaryComparison]
        raise RuntimeError(f"unknown RPCMessage type: {type(msg).__name__}")


def _on_hello(conn: "LeasedCapRPCConnection", msg: HelloMessage) -> None:
    """Handle an incoming :class:`HelloMessage`.

    First Hello: just record that we've seen one. We don't gate the connection on it —
    the peer is free to start using our main interface before their Hello reaches us,
    so the handshake is best-effort detection of a desynced peer rather than a
    synchronization barrier. Subsequent Hello: abort + raise :class:`HandshakeError`.
    """
    del msg
    if conn._peer_hello_received:
        reason = IRException(
            message="peer sent a second HelloMessage (desynced or reinitializing)",
            category="HandshakeError",
        )
        conn._abort(reason)
        raise HandshakeError(reason.message)
    conn._peer_hello_received = True


def _on_push(conn: "LeasedCapRPCConnection", msg: PushMessage) -> None:
    """Handle an incoming :class:`PushMessage`.

    Shape (see §K in ``phase_d_prep_todos.md``):

    1. Run phase-1 substitute on ``msg.expression`` — resolves every
       ``Imported…Expr`` / ``Exported…Expr`` to its runtime value, wrapped as
       :class:`ConstantExpr`. Sync; only this step mutates tables.
    2. Consume three sequential positive export IDs ``(token_id, future_id,
       grant_id)`` from ``_allocate_peer_push_export_ids`` — mirroring the sender's
       implicit import allocation.
    3. Call :func:`spawn_ir_future_in_task_group` with ``work =
       lambda: eval_forwardable(conn, substituted)`` to get ``(local_future,
       root_lease)``. The helper routes the work function's outcome:
       ``IRValue → set_result``; ``UnwrapForPipelining → resolve_from``;
       ``IRException → set_exception``; other ``Exception → wrapped
       IRException → set_exception``. So ill-typed peer input surfaces as a
       per-push failure, not a connection abort.
    4. Register the three export entries wired to that future's lifetime.

    Subsequent ``PullMessage`` against ``future_id`` spawns a resolver that
    unwraps any ``IRFutureLocalImpl`` forward chain (§2g) and then either sends the
    resolved value as a :class:`ResolveMessage`, forward-delegates via
    :class:`AwaitFutureExpr` when the chain terminates at a :class:`RemoteIRFuture`
    on this same connection, or falls back to eager ``wait()``.
    """
    tg = conn._tg
    if tg is None:
        raise RuntimeError("_on_push invoked before the connection's task group was opened")
    # Phase 1 substitute mutates tables synchronously. Phase 2 runs in a task.
    substituted, phase1_obligations = phase1_substitute(conn, msg.expression)
    token_id, future_id, grant_id = conn._allocate_peer_push_export_ids()

    async def work() -> IRValue | UnwrapForPipelining[IRValue, Any]:
        try:
            return await eval_forwardable(conn, substituted)
        finally:
            # Release the phase-1-accumulated consumer slots for inbound
            # ``AwaitFutureExpr`` references (see :func:`phase1_substitute`);
            # each bump is paired with exactly one decrement at spawn-task
            # completion, and may emit an :class:`UnpullMessage` if this was
            # the last consumer on a still-pending obligation.
            for future_entry in phase1_obligations:
                conn._decrement_local_consumer(future_entry)

    local_future, root_lease = spawn_ir_future_in_task_group(
        name=f"on_push[future={future_id}]", work=work, task_group=tg
    )
    # Register the three export entries via the connection's registration
    # helpers, which own the full (table insert + reverse-lookup + active set
    # + — for grants — broken-callback subscription) transition per kind.
    #
    # Bounce-back identity depends on the reverse-lookup dicts: A pushes to B,
    # then later sends the future / token / result-lease back to B as a call
    # arg; B's phase-1 resolves each of those to the concrete local object /
    # a clone on ``root_lease.grant``; B's handler forwards the value back to
    # A. On that last hop B's devaluator sees a local object (not a
    # ``Remote…`` proxy, so not the bounce-back-of-an-import path) and must
    # emit ``Exported…Expr(*_export_id=<positive push id>, …)`` rather than a
    # freshly-allocated negative export ID.
    token_export_entry = conn._register_token_export(
        export_id=token_id, local_token=local_future.token
    )
    conn._register_future_export(
        export_id=future_id,
        token_export_id=token_id,
        local_future=local_future,
        grant_export_id=grant_id,
    )
    conn._register_grant_export(
        export_id=grant_id, token_export_id=token_id, backing_lease=root_lease
    )
    # Pair-bump the token's ``outstanding_count`` for the future + grant
    # registrations so the push token's export refcount matches what the
    # peer's importer tracks (see pair-bump comment in
    # ``LeasedCapRPCConnection.__init__`` main-interface block and §2j).
    token_export_entry.outstanding_count += 2


def _on_pull(conn: "LeasedCapRPCConnection", msg: PullMessage) -> None:
    """Handle an incoming :class:`PullMessage`.

    Validate the named future + grant exports, then route the count change through
    :meth:`LeasedCapRPCConnection._increment_resolve_obligation_count`. That
    primitive spawns a resolver task on the 0 → 1 transition; subsequent pulls
    just accumulate for :class:`UnpullMessage` accounting. The resolver itself
    (see :func:`spawn_resolver_for_future_export`) awaits the local future and
    emits a :class:`ResolveMessage` / :class:`RejectMessage` (with §2g forward-
    delegation when the chain terminates at a :class:`RemoteIRFuture` on this
    connection).

    Late pulls on an already-resolved future export are silently ignored — the
    peer's view will catch up once our in-flight :class:`ResolveMessage` lands.

    The :class:`PullMessage` also names a grant; we validate the named grant
    matches the future's subject token, but we do not use it to find the
    backing lease for the spawn. The future export's
    :attr:`FutureExportEntry.grant_export_id` is the canonical source there,
    populated when the future was registered (via :func:`_on_push`) or paired
    with an accompanying lease (via the devaluator's ``UnwrapForPipelining``
    walk). Re-validation against ``msg.grant_import_id`` is kept as a defense
    against a peer naming a grant that no longer exists or refers to a different
    subject token.
    """
    if conn._tg is None:
        raise RuntimeError("_on_pull invoked before the connection's task group was opened")

    future_entry = conn._exports.get(msg.future_import_id)
    if future_entry is None:
        # Teardown race: future export was already torn down (e.g. via
        # _on_discard zeroing outstanding_count). Silently ignore.
        return
    if not isinstance(future_entry, FutureExportEntry):
        phase1_protocol_violation(
            conn,
            f"PullMessage: id {msg.future_import_id} is not a future export"
            f" entry (got {type(future_entry).__name__})",
        )
    grant_entry = conn._exports.get(msg.grant_import_id)
    if grant_entry is None:
        # Teardown race: our backing lease may have broken locally and
        # torn down the grant export via _subscribe_backing_lease_break
        # before the peer's already-in-flight PullMessage landed. The
        # peer will see our outbound BreakMessage and clean up; no
        # connection-level abort needed.
        return
    if not isinstance(grant_entry, GrantExportEntry):
        phase1_protocol_violation(
            conn,
            f"PullMessage: id {msg.grant_import_id} is not a grant export"
            f" entry (got {type(grant_entry).__name__})",
        )
    if grant_entry.token_export_id != future_entry.token_export_id:
        phase1_protocol_violation(
            conn,
            f"PullMessage: grant {msg.grant_import_id} subject token"
            f" {grant_entry.token_export_id} does not match future"
            f" {msg.future_import_id} token {future_entry.token_export_id}",
        )
    # Late pull on an already-resolved future export — our :class:`ResolveMessage`
    # (or :class:`RejectMessage`) is already on the wire; the peer will accept it.
    # Silently no-op: don't increment, don't spawn a resolver.
    if future_entry.resolved_sent:
        return
    conn._increment_resolve_obligation_count(future_entry)


def spawn_resolver_for_future_export(
    conn: "LeasedCapRPCConnection", future_entry: FutureExportEntry
) -> None:
    """Spawn the background resolver task for ``future_entry``; store its cancel scope.

    Called from :meth:`LeasedCapRPCConnection._ensure_resolver_running_for`, which is in
    turn called from :meth:`_increment_resolve_obligation_count` on the 0 → positive
    crossing of ``resolve_obligation_count``. Preconditions (asserted by the caller):

    - ``future_entry.resolved_sent`` is False.
    - ``future_entry.resolve_task_cancel_scope is None`` (no resolver running).
    - ``future_entry.grant_export_id is not None`` (a grant has been paired so the
      backing lease can be cloned).

    Resolver behaviour:

    - Clones the paired grant's backing lease twice: one ``wait_lease`` consumed by
      the chain traversal (``wait_or_forward`` takes it) and one ``survival_lease``
      held by the resolver's ``finally`` for the full duration of its run. The
      survival lease keeps the push lifetime alive even if the chain-traversal path
      discards its lease early — e.g. when the box has already transitioned to
      ``"forwarded"`` and ``wait_or_forward`` returns immediately. Without it, a
      prompt-discard from the importer (§2h design) would tear the lifetime down
      while the resolver is mid-chain, spuriously ending the importer-side token
      (and thus breaking its pending wait).
    - §2g forward-delegation: unwraps an :class:`IRFutureLocalImpl` forward chain via
      :meth:`IRFutureLocalImpl.wait_or_forward` as long as each step is itself an
      ``IRFutureLocalImpl``. Three terminal cases:

      - A step resolves to a concrete :class:`IRValue` → send a normal
        :class:`ResolveMessage`.
      - The chain terminates at a :class:`RemoteIRFuture` on *this* connection →
        emit a forwarding ``ResolveMessage(AwaitFutureExpr(ImportedIRFutureExpr,
        ImportedLeaseExpr))`` so the peer resolves against its own local future
        directly, saving a round-trip of the value back across the wire.
      - The chain terminates at anything else → fall back to eager ``wait()`` and
        send the resulting value as a normal resolve.

    Cancellation: the stored cancel scope is what
    :meth:`_decrement_resolve_obligation_count` cancels on the positive → 0 crossing
    triggered by :class:`UnpullMessage` (or by a devaluator rollback). The
    ``survival_lease.discard()`` in the resolver's ``finally`` still runs on
    cancellation, releasing our hold on the lifetime even when the resolver never
    emitted Resolve/Reject.
    """
    tg = conn._tg
    assert tg is not None, "spawn_resolver_for_future_export with no task group"
    grant_export_id = future_entry.grant_export_id
    assert grant_export_id is not None
    grant_entry = conn._exports.get(grant_export_id)
    assert isinstance(grant_entry, GrantExportEntry), (
        f"FutureExportEntry.grant_export_id {grant_export_id} does not point at a"
        f" live GrantExportEntry"
    )
    wait_lease = grant_entry.backing_lease.clone()
    survival_lease = grant_entry.backing_lease.clone()  # noqa — held by resolver's finally
    future_export_id = future_entry.export_id

    async def resolver() -> None:
        current_future: IRFuture[Any, Any] = future_entry.local_future
        current_lease: Lease[Any] = wait_lease
        try:
            while True:
                if isinstance(current_future, IRFutureLocalImpl):
                    outcome = await current_future.wait_or_forward(current_lease)
                    if isinstance(outcome, UnwrapForPipelining):
                        # Both current_lease ownership and wait_or_forward's
                        # returned lease are correctly exchanged here: the
                        # previous ``current_lease`` was consumed by
                        # ``wait_or_forward``; the new one is the forward clone.
                        current_future = outcome.future
                        current_lease = outcome.lease
                        continue
                    value: IRValue = outcome
                    break
                # Chain terminated at a non-IRFutureLocalImpl.
                if isinstance(current_future, RemoteIRFuture) and current_future.conn is conn:
                    _enqueue_forwarding_resolve_for(
                        conn, future_export_id, current_future, current_lease
                    )
                    return
                # Third-party or custom IRFuture — eager wait as a fallback.
                value = await current_future.wait(current_lease)
                break
        except IRException as exc:
            _enqueue_reject_for(conn, future_export_id, exc)
            return
        except Exception as exc:
            # `wait` / `wait_or_forward` raised a non-IRException error that we
            # can't serialize. Likely an IRFuture-implementation bug; make a
            # best-effort attempt to serialize it by wrapping it.
            _enqueue_reject_for(
                conn,
                future_export_id,
                wrap_exception(exc),
            )
            return
        # Cancellation or other BaseExceptions propagate up; no need to wrap.
        _enqueue_resolve_for(conn, future_export_id, value)

    cancel_scope = anyio.CancelScope()

    async def resolver_with_scope() -> None:
        try:
            with cancel_scope:
                await resolver()
        finally:
            # Release our survival-lease hold on the push's lifetime. If this
            # was the last lease, the push lifetime's token ends here and an
            # outbound :class:`ClosedMessage` is enqueued for any importer-side
            # watch — the resolver having just emitted Resolve/Reject (or
            # having been cancelled by :class:`UnpullMessage` / devaluator
            # rollback) is the signal that we no longer need to keep the
            # lifetime alive.
            survival_lease.discard()

    future_entry.resolve_task_cancel_scope = cancel_scope
    tg.start_soon(resolver_with_scope, name=f"resolver[future={future_export_id}]")


def _enqueue_resolve_for(
    conn: "LeasedCapRPCConnection", future_export_id: int, value: IRValue
) -> None:
    """Devaluate ``value`` and enqueue a :class:`ResolveMessage` for the given future.

    If devaluation raises (e.g. a malformed lease somehow ended up in the result), we
    fall back to a :class:`RejectMessage` describing the failure — that's far better
    than aborting the whole connection over a bad result shape.

    Also zeros the export entry's ``resolve_obligation_count`` and drops it from the
    active set: a single :class:`ResolveMessage` discharges every outstanding
    obligation on the peer side, so late inbound :class:`PullMessage` / AwaitFutureExpr
    references arriving after we've enqueued this resolve are silently ignored (see
    :func:`_on_pull`).
    """
    try:
        expression, bounce_backs = devaluate(conn, value)
    except Exception as exc:
        logger.exception(
            "Failed to devaluate resolve payload for future export id %s on %r",
            future_export_id,
            conn,
        )
        # Discard any leases still in the result (devaluate rolled back its own take
        # side; we still own the original ``value``'s leases).
        discard_leases_in(value)
        _enqueue_reject_for(
            conn,
            future_export_id,
            IRException(
                message=f"resolve payload could not be devaluated: {exc}",
                category="ResolveDevaluateError",
            ),
        )
        return
    entry = conn._exports.get(future_export_id)
    if isinstance(entry, FutureExportEntry):
        conn._zero_future_export_obligation(entry)
    conn._enqueue_message(ResolveMessage(future_export_id=future_export_id, expression=expression))
    for bb in bounce_backs:
        bb.discard()


def _enqueue_forwarding_resolve_for(
    conn: "LeasedCapRPCConnection",
    future_export_id: int,
    forward_future: RemoteIRFuture[Any, Any],
    forward_lease: Lease[Any],
) -> None:
    """Emit a forwarding :class:`ResolveMessage` for §2g forward-delegation.

    The message's expression is an :class:`AwaitFutureExpr` over the bounce-back
    encoding of ``forward_future`` + ``forward_lease`` — both reference the peer's
    own exports (they're ``Remote…`` objects on this connection), so the devaluator
    emits :class:`ImportedIRFutureExpr` / :class:`ImportedLeaseExpr`. The peer's
    phase-1 substitute resolves those back to its local future/lease, then
    ``_eval_forwardable`` turns ``AwaitFutureExpr`` into
    :class:`UnwrapForPipelining` which delivers via ``box.resolve_from`` — no
    value round-trip.

    On devaluation failure: discard ``forward_lease`` (the devaluator's rollback
    would have taken it already on success or already restored it on partial
    failure; we still own its original reference) and fall back to a
    :class:`RejectMessage` describing the failure.
    """
    try:
        unwrap: UnwrapForPipelining[Any, Any] = UnwrapForPipelining(
            future=forward_future, lease=forward_lease
        )
        expression, bounce_backs = devaluate(conn, unwrap)
    except Exception as exc:
        logger.exception(
            "Failed to devaluate forwarding resolve payload for future export id %s on %r",
            future_export_id,
            conn,
        )
        # On devaluator failure the original ``forward_lease`` has been ``.take()``'d
        # into a bounce-back clone that rollback already discarded — there's nothing
        # left for us to free here.
        _enqueue_reject_for(
            conn,
            future_export_id,
            IRException(
                message=f"forwarding resolve payload could not be devaluated: {exc}",
                category="ResolveDevaluateError",
            ),
        )
        return
    entry = conn._exports.get(future_export_id)
    if isinstance(entry, FutureExportEntry):
        conn._zero_future_export_obligation(entry)
    conn._enqueue_message(ResolveMessage(future_export_id=future_export_id, expression=expression))
    for bb in bounce_backs:
        bb.discard()


def _enqueue_reject_for(
    conn: "LeasedCapRPCConnection", future_export_id: int, exc: IRException[Any]
) -> None:
    """Enqueue a :class:`RejectMessage` carrying a static devaluation of ``exc``.

    :class:`IRException` satisfies :class:`IRStatic`, so a plain :class:`StaticExpr` is
    sufficient. Stage E's wire codec is responsible for ensuring the exception's
    ``data`` field is stub-free.

    Also zeros the export entry's ``resolve_obligation_count`` (symmetric with
    :func:`_enqueue_resolve_for`): a reject is terminal on the obligation channel.
    """
    from fxtr2.rpc.lowering_types import StaticExpr

    entry = conn._exports.get(future_export_id)
    if isinstance(entry, FutureExportEntry):
        conn._zero_future_export_obligation(entry)
    conn._enqueue_message(
        RejectMessage(
            future_export_id=future_export_id,
            expression=StaticExpr(exc),
        )
    )


def _on_resolve(conn: "LeasedCapRPCConnection", msg: ResolveMessage) -> None:
    """Handle an incoming :class:`ResolveMessage`.

    Phase-1 substitute, then spawn a task in the connection's task group that runs
    :func:`eval_forwardable` on the substituted expression and routes the outcome
    into the target ``_FutureImportEntry.resolution`` box:
    ``IRValue`` (including a returned :class:`IRException` as a value) →
    ``box.set_result``; ``UnwrapForPipelining → box.resolve_from``; a *raised*
    :class:`IRException` or other ``Exception`` from evaluation → ``box.set_exception``
    (non-:class:`IRException` failures are wrapped first). The ``resolve_from`` path is how the protocol's
    forward-delegation semantics (§K.6) are realized — multi-hop pipelining chains
    resolve by chaining ``resolve_from`` calls rather than eagerly pulling.

    Double-resolve (a second :class:`ResolveMessage` or :class:`RejectMessage` for a
    future whose box is already non-pending) is a protocol violation — aborts the
    whole connection (§2e checklist).
    """
    tg = conn._tg
    if tg is None:
        raise RuntimeError("_on_resolve invoked before the connection's task group was opened")

    future_entry = conn._imports.get(msg.future_export_id)
    if future_entry is None:
        # Teardown race: we already drained the future import (proxy GC +
        # outbound DiscardMessage) while the peer's resolver was still
        # running. Per the UnpullMessage docstring's symmetric description
        # ("the in-flight resolve will reach the importer and discharge
        # everything"), silently ignore.
        return
    if not isinstance(future_entry, FutureImportEntry):
        phase1_protocol_violation(
            conn,
            f"ResolveMessage: id {msg.future_export_id} is not a future"
            f" import entry (got {type(future_entry).__name__})",
        )
    # Materialize the resolution box if the peer is resolving preemptively (before
    # anyone on our side called ``wait``). Pipelining scenarios trigger this.
    box = conn._materialize_future_box(future_entry)
    if box.state != "pending":
        _abort_on_double_resolve(
            conn,
            f"ResolveMessage for future id {msg.future_export_id}: box is already {box.state!r}",
        )
    if future_entry.delivery_in_flight:
        _abort_on_double_resolve(
            conn,
            f"ResolveMessage for future id {msg.future_export_id}: delivery is already in flight",
        )

    # A single resolve discharges every outstanding pull obligation (both the ones
    # our waits sent and the ones received via ``AwaitFutureExpr``). Zero the
    # refcount + drop from the active set + re-check drain *before* spawning the
    # deliver task so a cancelled wait whose finally runs in the interim doesn't
    # see a spurious non-zero refcount and emit a bogus :class:`UnpullMessage`.
    conn._zero_future_import_obligation(future_entry)
    substituted, phase1_obligations = phase1_substitute(conn, msg.expression)

    # Mark the entry as having a delivery in flight *before* spawning so a peer
    # ``ClosedMessage`` arriving while ``deliver`` is mid-``eval_forwardable``
    # finds the gate set and defers the box discard until set_result lands. See
    # ``plans/extras/extra5_future_box_race_fix.md``.
    future_entry.delivery_in_flight = True

    async def deliver() -> None:
        try:
            try:
                result = await eval_forwardable(conn, substituted)
            except IRException as exc:
                box.set_exception(exc)
                return
            except Exception as exc:
                box.set_exception(wrap_exception(exc))
                return
            else:
                # BaseExceptions propagate up; no need to wrap.
                _deliver_result_to_box(box, result)
        finally:
            # Release phase-1-accumulated consumer slots (see :func:`phase1_substitute`).
            for future_entry_bumped in phase1_obligations:
                conn._decrement_local_consumer(future_entry_bumped)
            # Clear the delivery gate and re-check the discard policy. If the
            # peer's Close already arrived while we were evaluating, this is
            # where the deferred discard finally fires.
            future_entry.delivery_in_flight = False
            conn._maybe_discard_future_box(future_entry)

    tg.start_soon(deliver, name=f"on_resolve[future={msg.future_export_id}]")


def _deliver_result_to_box(
    box: PendingResultBox[Any],
    outcome: IRValue | UnwrapForPipelining[Any, Any],
) -> None:
    """Route a successful eval outcome into the box (value or pipelining forward).

    :class:`IRException` is a valid :class:`IRValue` and is delivered with
    ``set_result`` — distinct from evaluation failures, which use
    ``set_exception`` in the surrounding ``try``/``except``. Gracefully no-ops if the box has already been
    resolved/rejected/forwarded/discarded — the box handles idempotent writes and lease
    cleanup.
    """
    if isinstance(outcome, UnwrapForPipelining):
        box.resolve_from(outcome.future, outcome.lease)
        return
    box.set_result(outcome)


def _abort_on_double_resolve(conn: "LeasedCapRPCConnection", reason: str) -> None:
    """Protocol-violation path for a second resolve/reject on an already-resolved
    future. Aborts the connection with an :class:`IRException` describing ``reason``
    and raises :class:`RPCConnectionAborted` so the reader loop unwinds.
    """
    exc = IRException(message=reason, category="ProtocolError")
    conn._abort(exc)
    raise RPCConnectionAborted(exc.message)


def _on_reject(conn: "LeasedCapRPCConnection", msg: RejectMessage) -> None:
    """Handle an incoming :class:`RejectMessage`.

    Fully synchronous. Runs :func:`eval_static` directly on ``msg.expression`` (no
    phase-1 substitution, no spawn), expecting an :class:`IRException` result. The
    protocol guarantees reject payloads are static — see :class:`RejectMessage`'s
    docstring — so skipping phase 1 here is both simpler and leak-safe (a malformed
    peer can't trick us into allocating imports we'll never consume).

    If evaluation raises, or the result is not an :class:`IRException`, synthesize a
    replacement :class:`IRException` describing the malformed payload and use that as
    the rejection. Deliver the resulting :class:`IRException` to the target future's
    resolution box via ``set_exception``.

    Double-reject (or reject after resolve) aborts the connection, matching the
    double-resolve behaviour in :func:`_on_resolve`.
    """
    future_entry = conn._imports.get(msg.future_export_id)
    if future_entry is None:
        # Teardown race: same shape as in :func:`_on_resolve`.
        # Silently ignore.
        return
    if not isinstance(future_entry, FutureImportEntry):
        phase1_protocol_violation(
            conn,
            f"RejectMessage: id {msg.future_export_id} is not a future"
            f" import entry (got {type(future_entry).__name__})",
        )
    try:
        result = eval_static(msg.expression)
    except Exception as exc:
        result = IRException(
            message=f"peer sent malformed RejectMessage payload: {exc}",
            category="MalformedReject",
        )
    if not isinstance(result, IRException):
        result = IRException(
            message=(
                "peer RejectMessage payload did not evaluate to IRException"
                f" (got {type(result).__name__!r})"
            ),
            category="MalformedReject",
        )
    # Materialize the resolution box if the peer rejected preemptively (no local
    # ``wait`` yet) — symmetric with :func:`_on_resolve`.
    box = conn._materialize_future_box(future_entry)
    if box.state != "pending":
        _abort_on_double_resolve(
            conn,
            f"RejectMessage for future id {msg.future_export_id}: box is already {box.state!r}",
        )
    # A reject also discharges every outstanding pull obligation (see :func:`_on_resolve`).
    conn._zero_future_import_obligation(future_entry)
    box.set_exception(result)
    # No async deliver to track: ``_on_reject`` runs ``eval_static`` synchronously,
    # so by the time we return the box state has committed. Re-check the discard
    # gate so a token-end that already arrived (or arrives shortly after) drives
    # cleanup correctly — symmetric with the resolve path's deliver-finally hook.
    conn._maybe_discard_future_box(future_entry)


def _on_watch(conn: "LeasedCapRPCConnection", msg: WatchMessage) -> None:
    """Handle an incoming :class:`WatchMessage`.

    Subscribe to the local token's end via ``register_end_callback``, storing the
    deregister callback on the :class:`TokenExportEntry`. When the token ends, we
    enqueue a :class:`ClosedMessage` so the peer's token import can fire its own
    end-callbacks. If the local token has already ended, ``register_end_callback``
    fires synchronously — we send the :class:`ClosedMessage` immediately.

    Duplicate watches are no-ops: the first one already wired the subscription and
    the peer only needs a single :class:`ClosedMessage` per token.
    """
    entry = conn._exports.get(msg.token_import_id)
    if not isinstance(entry, TokenExportEntry):
        phase1_protocol_violation(
            conn,
            f"WatchMessage: no token export entry at id {msg.token_import_id}",
        )
    if entry.watch_deregister is not None:
        return
    our_export_id = msg.token_import_id

    def on_end() -> None:
        # ``ClosedMessage.token_export_id`` is the *sender*'s export id — ours, since
        # we're emitting the message. Same numeric value as the peer's token import id
        # that triggered the watch.
        conn._enqueue_message(ClosedMessage(token_export_id=our_export_id))
        # Once the local token has ended, the deregister handle points at a
        # now-stale slot in the (already-drained) token's end-callback list.
        # Scrub it so the entry no longer counts as "watched", and drop it
        # from the active-exports set so abort doesn't iterate it needlessly.
        # Safe to mutate ``entry`` here: we're inside the token's own
        # callback fire, which has already cleared its callback list.
        entry.watch_deregister = None
        conn._watched_token_exports.discard(our_export_id)

    entry.watch_deregister = entry.local_token.register_end_callback(on_end)
    conn._watched_token_exports.add(our_export_id)


def _on_closed(conn: "LeasedCapRPCConnection", msg: ClosedMessage) -> None:
    """Handle an incoming :class:`ClosedMessage`.

    Mark the token import entry ended and fire its local end-callbacks synchronously.
    Idempotent: if the entry has already been torn down (e.g. by abort) this is a
    no-op. Duplicate :class:`ClosedMessage` from the peer is also tolerated — we're
    already ended, callbacks already drained, nothing to do.
    """
    # ``ClosedMessage.token_export_id`` is the sender's export id, which equals our
    # token import id.
    entry = conn._imports.get(msg.token_export_id)
    if not isinstance(entry, TokenImportEntry):
        return
    if entry.ended:
        return
    conn._mark_token_import_ended(entry)


def _on_break(conn: "LeasedCapRPCConnection", msg: BreakMessage) -> None:
    """Handle an incoming :class:`BreakMessage`.

    Mark the grant import entry broken and fire broken-callbacks on every live lease
    under that grant. Iteration uses ``finalizer.peek()`` to avoid pinning leases
    strongly (mirrors ``_abort``); each lease's ``_mark_broken`` drains its own
    broken-callbacks and flips it to the broken state. We detach each tracked
    finalizer as we go so the weakref-leak path doesn't also fire on the now-broken
    leases (broken is not a leak).

    After the entry's ``broken_reason`` is set and ``live_leases`` cleared, invoke
    :meth:`_check_drain` — a broken grant is satisfied drain-state even though we
    never sent (and never will send) an outbound :class:`DiscardMessage` for it.

    Error handling:

    - Break on an ID we have no import entry for: silent no-op. Per
      :class:`BreakMessage`'s own docstring, "the message may be sent
      even if the recipient does not have any remaining references to
      the grant, in which case the recipient should simply ignore it."
      Drained grant imports are removed from ``_imports`` (see
      :meth:`LeasedCapRPCConnection._maybe_remove_grant_import`), so
      ``_imports.get(...)`` returning ``None`` is a teardown race we
      tolerate — matching the policy used by :func:`_on_closed` and
      :func:`_on_discard`.
    - Break on an ID whose import entry is the *wrong kind* (token /
      handle / future): protocol violation. IDs are never reused, so a
      peer referencing one of our non-grant imports as a grant means
      their bookkeeping has diverged. :func:`phase1_protocol_violation`
      aborts with a descriptive reason.
    - Duplicate break (``broken_reason`` already set): tolerated no-op with a debug
      log. The protocol leaves "send again or not" to the sender's discretion — a
      peer whose bookkeeping is pessimistic may re-send; repeated breaks have no
      state effect on our side.
    """
    entry = conn._imports.get(msg.grant_export_id)
    if entry is None:
        # Teardown race: we already drained and removed this grant
        # import. Per BreakMessage's protocol-spec docstring, silently
        # ignore.
        return
    if not isinstance(entry, GrantImportEntry):
        phase1_protocol_violation(
            conn,
            f"BreakMessage: grant_export_id {msg.grant_export_id} is not a"
            f" grant import entry (got {type(entry).__name__})",
        )
    if entry.broken_reason is not None:
        logger.debug(
            "Ignoring duplicate BreakMessage for grant import id %s on %r (already broken: %r)",
            msg.grant_export_id,
            conn,
            entry.broken_reason,
        )
        return
    conn._mark_grant_import_broken(entry, msg.reason)


def _on_discard(conn: "LeasedCapRPCConnection", msg: DiscardMessage) -> None:
    """Handle an incoming :class:`DiscardMessage`.

    Decrement ``outstanding_count`` on the matching export-table entry by
    ``msg.refcount``. When the count reaches zero:

    - :class:`TokenExportEntry` → remove from ``_exports`` and
      ``_token_export_id_by_token``. (The outbound-emission side for tokens will
      land alongside handles/futures in the follow-up stage; until then we just
      accept the message symmetrically.)
    - :class:`GrantExportEntry` → deregister the broken-callback subscription we
      installed at export time (so a subsequent ``backing_lease`` break doesn't try
      to emit a stale :class:`BreakMessage`), discard ``backing_lease``, remove the
      entry from ``_exports`` and ``_grant_export_id_by_grant``, and re-check drain.
    - :class:`HandleExportEntry` → remove from ``_exports`` and
      ``_handle_export_id_by_handle_id``. (Not strictly required — handle discards
      are opportunistic in the protocol — but we honor them when received.)
    - :class:`FutureExportEntry` → remove from ``_exports`` and
      ``_future_export_id_by_future_id``. Same "opportunistic" caveat as handles.

    A :class:`DiscardMessage` for an ID we don't have is tolerated (the peer may
    have raced with our own teardown — e.g. we locally revoked the grant and sent
    :class:`BreakMessage`, and their discard was in flight before they saw it). A
    negative ``outstanding_count`` after decrement, on the other hand, means the
    peer discarded more introductions than we ever made — which can't come from a
    teardown race and is a genuine protocol violation. Abort in that case.
    """
    entry = conn._exports.get(msg.import_id)
    if entry is None:
        # If the id is break-tombstoned, this discard is the peer's
        # acknowledgment of our BreakMessage — it will never reference the
        # id again, so the tombstone can be dropped (see
        # ``_broken_grant_export_tombstones`` in ``lowering.py``).
        conn._broken_grant_export_tombstones.pop(msg.import_id, None)
        return
    entry.outstanding_count -= msg.refcount
    if entry.outstanding_count > 0:
        return
    if entry.outstanding_count < 0:
        phase1_protocol_violation(
            conn,
            f"DiscardMessage for export id {msg.import_id} overshot"
            f" outstanding_count by {-entry.outstanding_count} (peer sent"
            f" refcount={msg.refcount})",
        )
    # Teardown — delegate to the per-kind ``_tear_down_*_export`` helpers on
    # the connection, which own the full (table + reverse-lookup + active set
    # + ancillary cleanup like broken-callback or watch deregister) transition.
    # Shared with ``on_break`` (grant) and kept symmetric with the
    # ``_register_*_export`` setup helpers so invariants can't drift.
    if isinstance(entry, GrantExportEntry):
        conn._tear_down_grant_export(entry)
    elif isinstance(entry, HandleExportEntry):
        conn._tear_down_handle_export(entry)
    elif isinstance(entry, FutureExportEntry):
        conn._tear_down_future_export(entry)
    else:
        assert isinstance(entry, TokenExportEntry)
        conn._tear_down_token_export(entry)


def _on_unpull(conn: "LeasedCapRPCConnection", msg: UnpullMessage) -> None:
    """Handle an incoming :class:`UnpullMessage`.

    Routes ``msg.refcount`` through
    :meth:`LeasedCapRPCConnection._decrement_resolve_obligation_count`, which folds
    in the resolver-cancel invariant: on the positive → 0 crossing it cancels the
    resolver task so it stops before emitting anything. The underlying dispatched
    work keeps running per the protocol's "wait is a read, not a control op"
    contract — we just suppress the delivery of its result.

    Tolerance rules (the protocol says unpull is opportunistic; late or racy
    messages should not abort the connection):

    - :class:`UnpullMessage` for an export ID we no longer have (entry was torn
      down after a :class:`ResolveMessage` and subsequent :class:`DiscardMessage`):
      silent no-op.
    - :class:`UnpullMessage` for a future we've already resolved
      (``resolved_sent is True``): silent no-op; our resolve is in flight.
    - Refcount overshoots current outstanding count: protocol violation, raised
      from inside :meth:`_decrement_resolve_obligation_count` via
      :func:`phase1_protocol_violation`. Unpulls should never desync until
      after the resolve is delivered.
    """
    # ``msg.future_import_id`` is the sender's (importer's) ID — which on our
    # export side has the same numeric value (see :class:`UnpullMessage` /
    # protocol: the ID is shared across both tables).
    entry = conn._exports.get(msg.future_import_id)
    if not isinstance(entry, FutureExportEntry):
        return
    if entry.resolved_sent:
        return
    conn._decrement_resolve_obligation_count(entry, count=msg.refcount)


def _on_abort(conn: "LeasedCapRPCConnection", msg: AbortMessage) -> None:
    """Handle an incoming :class:`AbortMessage`.

    Fully synchronous, same shape as :meth:`_on_reject`: run :func:`eval_static` on
    ``msg.expression``, expect an :class:`IRException`, synthesize a "peer sent
    malformed abort" replacement on any failure. Then call ``conn._abort`` with
    ``notify_peer=False`` — the peer has already announced the abort and the
    protocol forbids a reply — and raise :class:`RPCConnectionAborted` so the
    reader propagates the error into the connection's task group.
    """
    try:
        result = eval_static(msg.expression)
    except Exception as exc:
        result = IRException(
            message=f"peer sent malformed AbortMessage payload: {exc}",
            category="MalformedAbort",
        )
    if not isinstance(result, IRException):
        result = IRException(
            message=(
                "peer AbortMessage payload did not evaluate to IRException"
                f" (got {type(result).__name__!r})"
            ),
            category="MalformedAbort",
        )
    conn._abort(result, notify_peer=False)
    raise RPCConnectionAborted(result.message)
