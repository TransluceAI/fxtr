"""Remote-side proxy classes for LeasedCapRPCConnection.

These are stateless frozen dataclasses keyed by ``(conn, id)``. Two proxies with the same
``(conn, id)`` compare equal, giving bounce-back identity for free. All mutable state lives
on the corresponding ``_Import…Entry`` inside ``conn._imports``.

Naming: "Remote…" rather than "Imported…" — the shape of each proxy is "a thing that lives
on the remote side of this connection," from our perspective. The word "imported" is reserved
for the bookkeeping side (``_GrantImportEntry`` etc.).
"""

from __future__ import annotations

import dataclasses
import weakref
from typing import TYPE_CHECKING, Any, Callable, Sequence, cast

from fxtr2.leases.lease_types import FreshLifetime, Grant, Lease, LifetimeBrand, LifetimeToken
from fxtr2.leases.leased_cap_common import discard_leases_in
from fxtr2.leases.leased_cap_ir import (
    Call,
    GetItem,
    IRCallArg,
    IRFuture,
    IRHandle,
    IRValue,
    Request,
    UnwrapForPipelining,
    WaitAndIgnoreFutureThen,
)
from fxtr2.leases.local_lease import LocalLifetimeLease
from fxtr2.rpc.lowering_state import (
    FutureImportEntry,
    GrantImportEntry,
    HandleImportEntry,
    TokenImportEntry,
)
from fxtr2.rpc.lowering_types import DiscardMessage, PullMessage, PushMessage, WatchMessage

# `lowering` imports this module; keep the connection class type-only to avoid a cycle.
if TYPE_CHECKING:
    from fxtr2.rpc.lowering import LeasedCapRPCConnection


####################################################################################################
# Proxy-GC tracking for best-effort handle/future/token discards (§2j)
####################################################################################################
#
# Each :class:`Remote…` proxy bumps ``live_proxy_count`` on its import entry in
# ``__post_init__`` and installs a :func:`weakref.finalize` that decrements
# the count when the proxy is garbage-collected. When the count drops to zero
# while ``introduction_refcount`` is still non-zero, the finalizer enqueues
# one :class:`DiscardMessage` for the full outstanding count and zeroes it.
# This lets the peer reclaim their export-table slot in long-running sessions
# instead of letting it grow unboundedly as we keep receiving re-introductions.
#
# Introduction refcounts are a *wire-clock*, not a live-references counter.
# The protocol says every ``Exported…Expr`` that carries a ``token_export_id``
# — i.e. every ``ExportedIRHandleExpr`` / ``ExportedIRFutureExpr`` /
# ``ExportedLeaseExpr`` — counts as one introduction of the subject token on
# top of one introduction of the dependent itself. Matching counts on both
# sides prevent discard-vs-reintroduction races: a :class:`DiscardMessage`
# carrying ``refcount=N`` retires exactly the N introductions the importer
# has already seen, so any re-introduction emitted after that discard is
# accounted for separately on both sides.
#
# That framing lets us decouple the *timing* of dependent discards from
# token discards. When a handle/future/grant retires, we emit only its own
# :class:`DiscardMessage` — we do *not* also emit a paired one for the
# token. The token-introductions those ``Exported…Expr`` carried stay
# accumulated in ``token_entry.introduction_refcount``, and are all
# discharged at once when the token proxy itself GCs (its finalizer emits
# a single ``DiscardMessage(token_id, <accumulated refcount>)``).
#
# Pin chain: handle/future/grant proxies each hold a strong reference to
# their subject token proxy (the ``_token`` dataclass field, provided at
# construction by the connection's cache helpers). That guarantees the
# token proxy cannot GC while any dependent for its subject token is still
# live — so the one-shot final token discard always fires *after* every
# dependent discard has already gone out.
#
# Between a dependent retire and the final token retire, the peer's
# ``token_export.outstanding_count`` legitimately exceeds the sum of its
# live dependents' counts — the surplus is exactly the token-introductions
# we still owe to retire in the eventual final token discard. No underflow,
# no paired emission.
#
# Handles and futures don't pin cross-process resources (only grants/leases
# do, via lease-side discard of their backing lease), so this whole proxy-GC
# layer is pure optimization on the peer's export-table footprint. A slow-
# to-fire finalizer or a peer that ignores handle/future/token discards
# causes no correctness issues.


def _on_handle_proxy_gc(
    conn_ref: weakref.ref[LeasedCapRPCConnection[Any, Any]],
    import_id: int,
) -> None:
    """:func:`weakref.finalize` callback for GC'd :class:`RemoteIRHandle` proxies.

    On the 1→0 transition of ``live_proxy_count`` with
    ``introduction_refcount > 0``, emits :class:`DiscardMessage` for the
    handle's outstanding refcount and zeroes it.

    No-ops if the connection has been garbage-collected or has aborted;
    otherwise asserts the entry is still live and of the expected kind.
    """
    conn = conn_ref()
    if conn is None or conn._aborted_reason is not None:
        return
    entry = conn._imports.get(import_id)
    assert isinstance(
        entry, HandleImportEntry
    ), f"_on_handle_proxy_gc(id={import_id}): entry kind changed to {type(entry).__name__}"
    _dependent_decrement_and_maybe_discard(conn, entry, import_id)


def _on_future_proxy_gc(
    conn_ref: weakref.ref[LeasedCapRPCConnection[Any, Any]],
    import_id: int,
) -> None:
    """:func:`weakref.finalize` callback for GC'd :class:`RemoteIRFuture` proxies.

    Symmetric to :func:`_on_handle_proxy_gc` but for :class:`FutureImportEntry`.
    Each :meth:`RemoteIRFuture.walk` produces a distinct proxy object pointing
    at the same ``future_import_id``; each such object bumps/decrements
    ``live_proxy_count`` independently, so walks correctly hold the import
    alive as long as any walked view remains reachable.
    """
    conn = conn_ref()
    if conn is None or conn._aborted_reason is not None:
        return
    entry = conn._imports.get(import_id)
    assert isinstance(
        entry, FutureImportEntry
    ), f"_on_future_proxy_gc(id={import_id}): entry kind changed to {type(entry).__name__}"
    _dependent_decrement_and_maybe_discard(conn, entry, import_id)


def _on_token_proxy_gc(
    conn_ref: weakref.ref[LeasedCapRPCConnection[Any, Any]], import_id: int
) -> None:
    """:func:`weakref.finalize` callback for GC'd :class:`RemoteLifetimeToken` proxies.

    On the 1→0 transition of ``live_proxy_count`` with
    ``introduction_refcount > 0``, emits a single :class:`DiscardMessage` for
    the full accumulated refcount (standalone introductions plus every
    token-introduction that a dependent's ``Exported…Expr`` brought in) and
    zeroes the counter.

    The pin-chain invariant (dependent proxies hold a strong reference to
    their subject token proxy) guarantees this only fires after every
    dependent has already retired its own ``DiscardMessage``.

    If the entry is now fully drained (no introductions left and no
    end-callbacks still registered), remove it from ``_imports``. A later
    re-introduction at the same id allocates a fresh entry via
    :meth:`LeasedCapRPCConnection._introduce_token_import`.
    """
    conn = conn_ref()
    if conn is None or conn._aborted_reason is not None:
        return
    entry = conn._imports.get(import_id)
    assert isinstance(
        entry, TokenImportEntry
    ), f"_on_token_proxy_gc(id={import_id}): entry kind changed to {type(entry).__name__}"
    entry.live_proxy_count -= 1
    assert entry.live_proxy_count >= 0, (
        f"_on_token_proxy_gc(id={import_id}): live_proxy_count went negative"
        f" ({entry.live_proxy_count}) — bump/decrement pairing is broken"
    )
    if entry.live_proxy_count > 0:
        return
    assert entry.introduction_refcount >= 0, (
        f"_on_token_proxy_gc(id={import_id}): introduction_refcount is negative"
        f" ({entry.introduction_refcount})"
    )
    if entry.introduction_refcount > 0:
        refcount = entry.introduction_refcount
        entry.introduction_refcount = 0
        conn._enqueue_message(DiscardMessage(import_id=import_id, refcount=refcount))
    _maybe_remove_token_import(conn, entry)


def _dependent_decrement_and_maybe_discard(
    conn: LeasedCapRPCConnection[Any, Any],
    entry: HandleImportEntry | FutureImportEntry,
    import_id: int,
) -> None:
    """Shared body for the handle-proxy and future-proxy GC callbacks.

    Decrements ``entry.live_proxy_count``; on the 1→0 transition with
    ``introduction_refcount > 0``, enqueues a single :class:`DiscardMessage`
    for the dependent's outstanding refcount and zeroes it. The subject
    token's share of introductions stays accumulated in its own
    ``introduction_refcount`` until the token proxy itself GCs — see the
    top-of-module comment.

    If the entry is now fully drained (no introductions left, and for
    futures no pending obligations / consumers / un-terminal resolution),
    remove it from ``_imports``.
    """
    entry.live_proxy_count -= 1
    assert entry.live_proxy_count >= 0, (
        f"dep proxy GC(id={import_id}): live_proxy_count went negative"
        f" ({entry.live_proxy_count}) — bump/decrement pairing is broken"
    )
    if entry.live_proxy_count > 0:
        return
    assert entry.introduction_refcount >= 0, (
        f"dep proxy GC(id={import_id}): introduction_refcount is negative"
        f" ({entry.introduction_refcount})"
    )
    if entry.introduction_refcount > 0:
        refcount = entry.introduction_refcount
        entry.introduction_refcount = 0
        conn._enqueue_message(DiscardMessage(import_id=import_id, refcount=refcount))
    if isinstance(entry, HandleImportEntry):
        _maybe_remove_handle_import(conn, entry)
    else:
        _maybe_remove_future_import(conn, entry)


def _maybe_remove_token_import(
    conn: LeasedCapRPCConnection[Any, Any], entry: TokenImportEntry
) -> None:
    """Remove ``entry`` from ``conn._imports`` iff it is fully drained.

    Called from :func:`_on_token_proxy_gc` after the final discard has
    been enqueued. Preconditions the caller has just established:
    ``live_proxy_count == 0`` and ``introduction_refcount == 0``. We also
    require ``end_callbacks`` empty — a user that registered a callback
    and then dropped the proxy without deregistering still has a live
    subscription we shouldn't silently drop. (This is the pre-existing
    leak for that pattern; it's not made worse here.)
    """
    if entry.end_callbacks:
        return
    assert entry.live_proxy_count == 0
    assert entry.introduction_refcount == 0
    conn._active_token_imports.discard(entry.import_id)
    del conn._imports[entry.import_id]


def _maybe_remove_handle_import(
    conn: LeasedCapRPCConnection[Any, Any], entry: HandleImportEntry
) -> None:
    """Remove ``entry`` from ``conn._imports`` iff it is fully drained."""
    assert entry.live_proxy_count == 0
    assert entry.introduction_refcount == 0
    del conn._imports[entry.import_id]


def _maybe_remove_future_import(
    conn: LeasedCapRPCConnection[Any, Any], entry: FutureImportEntry
) -> None:
    """Remove ``entry`` from ``conn._imports`` iff it is fully drained.

    Requires, beyond the dep-kind conditions, that no resolve obligations
    remain and no local waiters are mid-``wait``. A future with pending
    obligations can still receive inbound :class:`ResolveMessage` /
    :class:`RejectMessage`; we can't remove the entry until the peer has
    been retired.

    On removal, also tears down the resolution box (releasing any stored
    leases) and deregisters its token-end subscription. The base
    :class:`PendingResultBox` has no GC-driven lease finalizer of its own —
    its discard is owned by the entry — so we have to release here or stored
    leases would leak when the entry's last reference drops.
    """
    assert entry.live_proxy_count == 0
    assert entry.introduction_refcount == 0
    if entry.resolve_obligation_refcount != 0:
        return
    if entry.local_consumer_count != 0:
        return
    if entry.resolution is not None:
        entry.resolution.discard()
    if entry.box_token_unsubscribe is not None:
        entry.box_token_unsubscribe()
        entry.box_token_unsubscribe = None
    conn._active_resolve_obligation_imports.discard(entry.import_id)
    del conn._imports[entry.import_id]


@dataclasses.dataclass(frozen=True, eq=True)
class RemoteLifetimeToken[B: LifetimeBrand](LifetimeToken[B]):
    """Proxy for a lifetime token that lives on the remote side of an RPC connection.

    Mostly stateless: ``ended()`` and ``register_end_callback`` delegate to the
    connection's ``_TokenImportEntry``. The token is marked ended when the
    connection receives a :class:`ClosedMessage` referring to its ID, or on
    connection abort.

    ``__post_init__`` bumps the import entry's ``live_proxy_count`` and installs
    a :func:`weakref.finalize` so the last live proxy's GC retires any
    remaining ``introduction_refcount`` via :class:`DiscardMessage` — see the
    top-of-module "Proxy-GC tracking" comment.
    """

    conn: LeasedCapRPCConnection[Any, Any]
    token_import_id: int

    def __post_init__(self) -> None:
        entry = self.conn._imports.get(self.token_import_id)
        assert isinstance(entry, TokenImportEntry), (
            f"RemoteLifetimeToken(id={self.token_import_id}):"
            f" no live TokenImportEntry at construction time"
        )
        entry.live_proxy_count += 1
        weakref.finalize(
            self,
            _on_token_proxy_gc,
            weakref.ref(self.conn),
            self.token_import_id,
        )

    def ended(self) -> bool:
        entry = self.conn._imports.get(self.token_import_id)
        assert isinstance(entry, TokenImportEntry), (
            f"{self!r}.ended(): no live TokenImportEntry at id"
            f" {self.token_import_id} — the proxy should keep the entry pinned"
            f" via ``live_proxy_count`` until it GCs"
        )
        return entry.ended

    def register_end_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to the remote token's end.

        Fires ``callback`` immediately (before returning) if the token is already known
        to have ended — the usual :class:`LifetimeToken` contract.

        On first registration for a given import entry, enqueues a :class:`WatchMessage`
        so the peer knows we want to hear about the end. Subsequent registrations reuse
        the already-sent watch — the peer only sends one :class:`ClosedMessage` per
        token regardless of how many local subscribers we have.
        """
        entry = self.conn._imports.get(self.token_import_id)
        assert isinstance(entry, TokenImportEntry), (
            f"{self!r}.register_end_callback: no live TokenImportEntry at id"
            f" {self.token_import_id} — the proxy should keep the entry pinned"
            f" via ``live_proxy_count`` until it GCs"
        )
        if entry.ended:
            callback()
            return lambda: None
        if not entry.watch_sent:
            self.conn._enqueue_message(WatchMessage(token_import_id=self.token_import_id))
            entry.watch_sent = True
        entry.end_callbacks.append(callback)

        def deregister() -> None:
            # Idempotent on the callback side: silently no-op if the callback
            # has already been removed (or already fired and been cleared).
            # The entry itself is guaranteed to still exist: this deregister
            # closure is handed back to the user, and the user's proxy
            # reference pins ``live_proxy_count`` which pins the entry.
            entry_now = self.conn._imports.get(self.token_import_id)
            assert isinstance(entry_now, TokenImportEntry), (
                f"{self!r}.register_end_callback deregister: no live"
                f" TokenImportEntry at id {self.token_import_id}"
            )
            try:
                entry_now.end_callbacks.remove(callback)
            except ValueError:
                pass

        return deregister


@dataclasses.dataclass(frozen=True, eq=True)
class RemoteGrant[B: LifetimeBrand](Grant[B]):
    """Proxy for a grant that lives on the remote side of an RPC connection.

    Used as the ``.grant`` of every :class:`LocalLifetimeLease` the connection hands out for
    imported leases. Two leases on the same remote grant have ``RemoteGrant`` objects that
    compare equal (value equality on ``(conn, grant_import_id)``), satisfying the
    ``Lease.grant`` equality contract.

    ``_token`` is a required constructor argument: a strong reference to the
    cached singleton :class:`RemoteLifetimeToken` proxy for this grant's
    subject token. Callers fetch it via
    :meth:`LeasedCapRPCConnection._get_or_create_token_proxy` before
    constructing the grant. The field is excluded from equality and hashing
    so that two ``RemoteGrant``\\ s for the same ``(conn, grant_import_id)``
    still compare equal.

    Holding ``_token`` as a strong reference is how the pin chain works: the
    token proxy cannot GC, and emit its final ``DiscardMessage``, while any
    ``RemoteGrant`` naming it is alive. That ordering is what makes it safe
    to defer the token's share of introductions to the token's own GC —
    see the top-of-module "Proxy-GC tracking" comment.
    """

    conn: LeasedCapRPCConnection[Any, Any]
    grant_import_id: int
    _token: RemoteLifetimeToken[B] = dataclasses.field(compare=False, hash=False, repr=False)

    @property
    def subject(self) -> LifetimeToken[B]:
        return cast("LifetimeToken[B]", self._token)


@dataclasses.dataclass(frozen=True, eq=True)
class RemoteIRHandle[B: LifetimeBrand](IRHandle[B]):
    """Proxy for an :class:`IRHandle` that lives on the remote side of an RPC connection.

    ``dispatch`` is synchronous: it takes ownership of the input lease and any leases inside the
    request, runs the devaluator (synchronously allocating any new export IDs and, for the push
    itself, three new positive import IDs for the result token/future/grant), and enqueues a
    :class:`PushMessage` whose expression is a ``HandleRequestExpr`` on this handle. It returns
    a :class:`RemoteIRFuture` + :class:`LocalLifetimeLease` pair pointing at the freshly
    allocated import IDs.

    ``request`` = ``dispatch`` + wait + discard-the-result-lease, matching the local-side
    semantics (abandoning a wait does not cancel the underlying work).
    """

    conn: LeasedCapRPCConnection[Any, Any]
    handle_import_id: int
    _token: RemoteLifetimeToken[B] = dataclasses.field(compare=False, hash=False, repr=False)

    def __post_init__(self) -> None:
        """Bump the entry's ``live_proxy_count`` and install the GC finalizer
        — see the top-of-module "Proxy-GC tracking" comment for the full
        lifecycle.

        ``_token`` is provided by the caller (typically via
        :meth:`LeasedCapRPCConnection._get_or_create_handle_proxy`) and
        directly holds the cached subject-token proxy that pins the token
        until every dependent retires.
        """
        entry = self.conn._imports.get(self.handle_import_id)
        assert isinstance(entry, HandleImportEntry), (
            f"RemoteIRHandle(id={self.handle_import_id}):"
            f" no live HandleImportEntry at construction time"
        )
        entry.live_proxy_count += 1
        weakref.finalize(
            self,
            _on_handle_proxy_gc,
            weakref.ref(self.conn),
            self.handle_import_id,
        )

    @property
    def token(self) -> LifetimeToken[B]:
        return cast("LifetimeToken[B]", self._token)

    async def request(
        self,
        lease: Lease[B] | UnwrapForPipelining[Lease[B], Any],
        request: Request,
    ) -> IRValue:
        """Dispatch the request and await the result. Matches the shape of
        :meth:`IRHandleLocalImplBase.request`: cancellation of the await does not cancel
        the underlying dispatch.
        """
        future, result_lease = self.dispatch(lease, request)
        return await future.wait(result_lease)

    def dispatch[
        RB: FreshLifetime[Any]
    ](
        self,
        lease: Lease[B] | UnwrapForPipelining[Lease[B], Any],
        request: Request,
    ) -> tuple[
        IRFuture[Any, RB], Lease[RB]
    ]:
        """Sync. Enqueues a :class:`PushMessage` that carries a
        :class:`HandleRequestExpr` on this remote handle. Allocates three new positive
        import IDs for the result lifetime/future/grant and returns
        ``(RemoteIRFuture, LocalLifetimeLease)`` pointing at the future+grant IDs.

        ``lease`` and any leases inside ``request`` are transferred in (standard
        :class:`IRHandle` contract). For an :class:`UnwrapForPipelining` outer lease,
        the inner lease is taken at entry and the devaluator emits an
        :class:`AwaitFutureExpr` in the wire-form lease slot; the peer's evaluator
        (or local-impl runner, on the same side) awaits the future to materialise
        the lease before invoking the underlying call. Also fails synchronously with
        :class:`RPCConnectionAborted` if the connection has aborted — the grant
        we'd be using is broken in that case, so any subsequent ``wait`` would
        fail anyway.
        """
        # `lowering_devaluate` imports this module; a top-level import would create a cycle.
        from fxtr2.rpc.lowering_devaluate import devaluate_handle_request

        self.conn._check_not_aborted()
        # Take ownership of the caller's lease up front; any leases inside ``request``
        # are consumed by the devaluator walk (via ``_devaluate_lease``'s ``lease.take()``
        # / ``lease.discard()`` depending on bounce-back vs fresh-export). Pipelined-
        # lease form: take the inner lease and rewrap (mirrors `pipeline_dispatch`'s
        # `inner_handle_lease` take).
        handle_lease: Lease[Any] | UnwrapForPipelining[Any, Any]
        if isinstance(lease, UnwrapForPipelining):
            handle_lease = UnwrapForPipelining(future=lease.future, lease=lease.lease.take())
        else:
            handle_lease = lease.take()
        wait_for, unwrapped_request = _flatten_wait_and_ignore(request)
        try:
            expression, bounce_backs = devaluate_handle_request(
                self.conn,
                handle_import_id=self.handle_import_id,
                handle_lease=handle_lease,
                request=unwrapped_request,
                wait_for=wait_for,
            )
        except BaseException:
            # Devaluator rolls back its own takes on error; we still own ``handle_lease``,
            # ``unwrapped_request``'s leases, and any ``wait_for`` leases that the
            # devaluator failed before reaching. Discard whatever didn't make it in.
            if isinstance(handle_lease, UnwrapForPipelining):
                handle_lease.lease.discard()
            else:
                handle_lease.discard()
            _discard_request_leases(unwrapped_request)
            for unwrap in wait_for:
                unwrap.lease.discard()
            raise
        # Allocate and register the three result-side import entries *after*
        # the devaluator commits, so on rollback we don't leak allocations.
        # Three sequential ``_introduce_*_import`` calls; the future and
        # grant helpers internally bump the subject token's
        # ``introduction_refcount``, so the token ends at refcount=3 (one
        # implicit for the token itself, plus one each from the future +
        # grant dep contributions). Peer arrives at the symmetric total via
        # ``_on_push``. The eventual token-proxy-GC discard retires all
        # three in one go.
        token_id, future_id, grant_id = self.conn._allocate_push_import_ids()
        self.conn._introduce_token_import(token_id)
        self.conn._introduce_future_import(future_id, token_id)
        self.conn._introduce_grant_import(grant_id, token_id)
        self.conn._enqueue_message(PushMessage(expression=expression))
        # Post-enqueue: discard bounce-back leases in order so any triggered
        # DiscardMessage lands after the push (caller contract of devaluate).
        for bb in bounce_backs:
            bb.discard()
        token_proxy = self.conn._get_or_create_token_proxy(token_id)
        future_proxy = self.conn._get_or_create_future_proxy(future_id)
        result_lease = LocalLifetimeLease(
            owner=self.conn,
            grant=RemoteGrant(conn=self.conn, grant_import_id=grant_id, _token=token_proxy),
        )
        return future_proxy, result_lease


def _flatten_wait_and_ignore(
    request: Request,
) -> tuple[list[UnwrapForPipelining[Any, Any]], Call[IRCallArg] | GetItem]:
    """Peel :class:`WaitAndIgnoreFutureThen` wrappers off ``request``, collecting their
    ``wait_for`` entries into a flat list (outermost-first) and returning the final
    :class:`Call`/:class:`GetItem` innermost request.

    Supports both the single-value and sequence shapes of ``WaitAndIgnoreFutureThen.
    wait_for``. Nested ``WaitAndIgnoreFutureThen`` is flattened recursively.
    """
    collected: list[UnwrapForPipelining[Any, Any]] = []
    current: Request = request
    while isinstance(current, WaitAndIgnoreFutureThen):
        wf = current.wait_for
        if isinstance(wf, UnwrapForPipelining):
            collected.append(wf)
        else:
            collected.extend(wf)
        current = current.then
    return collected, current


def _discard_request_leases(request: Call[IRCallArg] | GetItem) -> None:
    """Cleanup helper: discard every :class:`Lease` inside ``request``.

    Used on the failure path in :meth:`RemoteIRHandle.dispatch` when the devaluator
    rollback leaves us still holding the user's request leases. Delegates to
    :func:`discard_leases_in` for the inner walk.
    """
    if isinstance(request, Call):
        discard_leases_in(request.arg)


@dataclasses.dataclass(frozen=True, eq=True)
class RemoteIRFuture[T: IRValue, B: LifetimeBrand](IRFuture[T, B]):
    """Proxy for an :class:`IRFuture` that lives on the remote side of an RPC connection.

    The proxy carries an ``attr_path`` describing a (locally-accumulated) walk into the result
    tree. ``walk`` returns a new proxy with the path extended — no message is sent. The walk is
    only materialized when the future is pulled (``wait``) or pipelined (``pipeline_dispatch``),
    at which point it is baked into a :class:`WalkFutureExpr` inside the outbound expression.

    ``wait`` takes the supplied lease, ensures a pull for the root future has been sent (via the
    entry's resolution box), awaits the result, and returns the walked, deep-copied sub-value.

    ``pipeline_dispatch`` is the remote analogue of
    ``leased_cap_common.IRFutureLocalImpl.pipeline_dispatch``: it emits a
    ``HandleRequestExpr`` whose ``handle`` is an ``AwaitFutureExpr`` of this future (+ its
    lease), allocating three new positive import IDs for the pipelined result.
    """

    conn: LeasedCapRPCConnection[Any, Any]
    future_import_id: int
    attr_path: tuple[str | int, ...] = ()
    _token: RemoteLifetimeToken[B] = dataclasses.field(
        compare=False, hash=False, repr=False, kw_only=True
    )

    def __post_init__(self) -> None:
        """Bump the entry's ``live_proxy_count`` and install the GC finalizer
        — see the top-of-module "Proxy-GC tracking" comment for the full
        lifecycle.

        Each :meth:`walk` produces a distinct proxy object with its own
        bump/decrement pair, so a walked-and-held sub-future correctly keeps
        the import alive.
        """
        entry = self.conn._imports.get(self.future_import_id)
        assert isinstance(entry, FutureImportEntry), (
            f"RemoteIRFuture(id={self.future_import_id}):"
            f" no live FutureImportEntry at construction time"
        )
        entry.live_proxy_count += 1
        weakref.finalize(
            self,
            _on_future_proxy_gc,
            weakref.ref(self.conn),
            self.future_import_id,
        )

    @property
    def token(self) -> LifetimeToken[B]:
        return cast("LifetimeToken[B]", self._token)

    def walk(self, attr_path: str | int | Sequence[str | int]) -> IRFuture[Any, B]:
        """Extend the locally-accumulated walk path. No message is sent; the walk is
        materialized only when :meth:`wait` or :meth:`pipeline_dispatch` fires.
        """
        extra: tuple[str | int, ...]
        if isinstance(attr_path, (str, int)):
            extra = (attr_path,)
        else:
            extra = tuple(attr_path)
        return RemoteIRFuture[Any, Any](
            conn=self.conn,
            future_import_id=self.future_import_id,
            attr_path=self.attr_path + extra,
            _token=self._token,
        )

    async def wait(self, lease: Lease[B]) -> T:
        """Take the supplied lease, send (or piggyback on) a :class:`PullMessage`
        for this future's root import, discard the user-visible lease immediately,
        and await the walked result.

        The lease's grant must be a :class:`RemoteGrant` on this future's connection,
        backing a grant import entry whose ``token_import_id`` matches this future's
        subject-token id. That's the "import-side grant/future equality" contract
        (moved from ``LocalPendingResultBox`` into concrete futures in §1b).

        Ownership handoff: per the phase-D drain-semantics design, the user's lease is
        discarded as soon as the :class:`PullMessage` is in flight — holding it would
        delay any accompanying :class:`DiscardMessage` until after the resolve. We
        preserve prompt break-detection by subscribing a wait-break callback on the
        grant import entry (see :attr:`GrantImportEntry.wait_break_callbacks`) in
        place of the lease's own broken-callback; the connection fires both channels
        together on :class:`BreakMessage` / abort. ``local_consumer_count`` on the
        :class:`FutureImportEntry` blocks the "emit :class:`UnpullMessage` when last
        consumer drops" path until this wait returns.
        """
        # Validate the lease's grant shape before taking ownership; a mismatched
        # lease raises without consuming the caller's reference.
        grant = lease.grant
        if not isinstance(grant, RemoteGrant) or grant.conn is not self.conn:
            raise RuntimeError(
                f"{self!r}.wait: lease grant is not a RemoteGrant on this connection."
            )
        conn = self.conn
        taken_lease = lease.take()
        future_entry: FutureImportEntry
        try:
            future_entry_lookup = conn._imports.get(self.future_import_id)
            if not isinstance(future_entry_lookup, FutureImportEntry):
                raise RuntimeError(
                    f"{self!r}.wait: no live future import entry at id {self.future_import_id}."
                )
            grant_entry = conn._imports.get(grant.grant_import_id)
            if not isinstance(grant_entry, GrantImportEntry):
                raise RuntimeError(
                    f"{self!r}.wait: grant import entry {grant.grant_import_id} not live."
                )
            if grant_entry.token_import_id != future_entry_lookup.token_import_id:
                raise RuntimeError(
                    f"{self!r}.wait: lease grant's token"
                    f" {grant_entry.token_import_id} does not match future's token"
                    f" {future_entry_lookup.token_import_id}."
                )
            future_entry = future_entry_lookup
            # Materialize the resolution box on first wait. Subsequent waits share it.
            box = conn._materialize_future_box(future_entry)
            # Emit :class:`PullMessage` on the 0→1 transition of
            # ``resolve_obligation_refcount``. Skip if the box has already resolved
            # (late waiter on a completed future — just read from the box).
            if future_entry.resolve_obligation_refcount == 0 and box.state == "pending":
                conn._enqueue_message(
                    PullMessage(
                        future_import_id=self.future_import_id,
                        grant_import_id=grant.grant_import_id,
                    )
                )
                conn._bump_resolve_obligation_refcount(future_entry)
            # Reserve a local-consumer slot before discarding the user lease.
            future_entry.local_consumer_count += 1
        except BaseException:
            taken_lease.discard()
            raise
        grant_import_id = grant.grant_import_id

        def register_break(cb: Callable[[str], None]) -> Callable[[], None]:
            return conn._register_wait_break_callback(grant_import_id, cb)

        # Register a placeholder wait-break BEFORE discarding the taken
        # lease. This keeps the grant entry alive past the ``live_leases``
        # drain in :meth:`LeasedCapRPCConnection._clean_up_lease_data`
        # (which under the new "remove-on-drain" design would otherwise
        # delete the entry from ``_imports`` before :func:`register_break`
        # inside :meth:`await_resolution` could find it).
        #
        # TODO(§2j.7 polish): this noop-callback is really a "slot pin"
        # masquerading as a break callback. Consider adding a dedicated
        # ``conn._pin_grant_import(id) -> unpin`` API and using that
        # here instead; it would de-tangle the dual roles
        # ``wait_break_callbacks`` currently serves (actual break
        # subscribers + entry-liveness pins).
        _pin_dereg = register_break(lambda _reason: None)
        try:
            taken_lease.discard()
            # ``taken_lease`` just drained ``live_leases`` to empty, but the
            # placeholder wait-break subscription keeps the grant entry in
            # ``_imports``. The outbound :class:`DiscardMessage` was
            # enqueued synchronously by ``_clean_up_lease_data`` before we
            # reach the first ``await`` below.
            result = await box.await_resolution(self.attr_path, register_break=register_break)
        finally:
            _pin_dereg()
            conn._decrement_local_consumer(future_entry)
        return cast(T, result)

    def pipeline_dispatch[
        RB: FreshLifetime[Any]
    ](
        self: RemoteIRFuture[IRHandle[Any], B],
        future_lease: Lease[B],
        inner_handle_lease: Lease[Any] | UnwrapForPipelining[Lease[Any], Any],
        request: Request,
    ) -> tuple[IRFuture[Any, RB], Lease[RB]]:
        """Pipeline a dispatch on the (not-yet-resolved) result of this remote future.

        Enqueues a :class:`PushMessage` whose expression is
        ``HandleRequestExpr(handle=AwaitFutureExpr(<this future, walked if
        attr_path>, <devaluated future_lease>), lease=<devaluated inner_handle_lease>,
        request=<devaluated request>)``. Allocates three new positive import IDs for
        the pipelined result (token / future / grant) and returns
        ``(RemoteIRFuture, LocalLifetimeLease)`` pointing at the future+grant IDs.

        Ownership (standard transfer convention):

        - ``future_lease`` — consumed. Must be a lease on this future's lifetime
          (naming a :class:`RemoteGrant` on this connection with matching subject
          token); the devaluator's bounce-back path handles the discard ordering.
        - ``inner_handle_lease`` — consumed. Either a plain :class:`Lease` or an
          :class:`UnwrapForPipelining` whose resolution is a :class:`Lease`; the
          devaluator encodes the latter as an :class:`AwaitFutureExpr` in the lease
          slot.
        - any leases inside ``request`` — consumed by the devaluator walk.

        Also fails synchronously with :class:`RPCConnectionAborted` if the connection
        has aborted — we'd otherwise allocate import IDs against a connection that
        won't service the push.
        """
        from fxtr2.rpc.lowering_devaluate import devaluate_pipeline_dispatch

        self.conn._check_not_aborted()
        taken_future_lease = future_lease.take()
        taken_inner: Lease[Any] | UnwrapForPipelining[Any, Any]
        if isinstance(inner_handle_lease, UnwrapForPipelining):
            taken_inner = UnwrapForPipelining(
                future=inner_handle_lease.future,
                lease=inner_handle_lease.lease.take(),
            )
        else:
            taken_inner = inner_handle_lease.take()
        wait_for, unwrapped_request = _flatten_wait_and_ignore(request)
        try:
            expression, bounce_backs = devaluate_pipeline_dispatch(
                self.conn,
                future_import_id=self.future_import_id,
                future_attr_path=self.attr_path,
                future_lease=taken_future_lease,
                inner_handle_lease=taken_inner,
                request=unwrapped_request,
                wait_for=wait_for,
            )
        except BaseException:
            # Devaluator rolls back its own takes; we still own the top-level taken
            # leases, the inner unwrap's lease, and any ``wait_for`` / request leases
            # it didn't reach. Discard everything we can identify.
            taken_future_lease.discard()
            if isinstance(taken_inner, UnwrapForPipelining):
                taken_inner.lease.discard()
            else:
                taken_inner.discard()
            _discard_request_leases(unwrapped_request)
            for unwrap in wait_for:
                unwrap.lease.discard()
            raise
        # Allocate and register the three result-side import entries *after*
        # the devaluator commits, matching :meth:`RemoteIRHandle.dispatch`.
        token_id, future_id, grant_id = self.conn._allocate_push_import_ids()
        self.conn._introduce_token_import(token_id)
        self.conn._introduce_future_import(future_id, token_id)
        self.conn._introduce_grant_import(grant_id, token_id)
        self.conn._enqueue_message(PushMessage(expression=expression))
        for bb in bounce_backs:
            bb.discard()
        token_proxy = self.conn._get_or_create_token_proxy(token_id)
        future_proxy = self.conn._get_or_create_future_proxy(future_id)
        result_lease = LocalLifetimeLease(
            owner=self.conn,
            grant=RemoteGrant(conn=self.conn, grant_import_id=grant_id, _token=token_proxy),
        )
        return future_proxy, result_lease
