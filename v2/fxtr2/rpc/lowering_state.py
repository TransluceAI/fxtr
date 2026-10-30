"""Typed value containers for the connection's import / export tables.

No logic — see ``lowering_devaluate.py`` / ``lowering_evaluate.py`` for how these entries
are produced and consumed, and ``lowering.py`` for the ``LeasedCapRPCConnection`` class that
owns the tables.
"""

from __future__ import annotations

import dataclasses
import weakref
from typing import Any, Callable

import anyio

from fxtr2.leases.lease_types import Lease, LifetimeToken
from fxtr2.leases.leased_cap_common import PendingResultBox
from fxtr2.leases.leased_cap_ir import IRFuture, IRHandle, IRValue
from fxtr2.leases.local_lease import _LeaseCleanupInfo
from fxtr2.rpc.lowering_types import Expression

####################################################################################################
# Reserved import/export IDs
####################################################################################################

MAIN_TOKEN_ID: int = 0
"""Reserved ID for the main-interface lifetime token."""

MAIN_HANDLE_ID: int = 1
"""Reserved ID for the main interface ``IRHandle``."""

MAIN_GRANT_ID: int = 2
"""Reserved ID for the grant on the main interface."""

####################################################################################################
# Import-table registration errors
####################################################################################################


class ImportEntryKindMismatch(Exception):
    """Raised by ``LeasedCapRPCConnection._introduce_*_import`` when an
    existing entry at the target id has the wrong kind, or when a dep
    entry's ``token_import_id`` does not match the value passed in.

    Phase-1 translates this into :func:`phase1_protocol_violation` (a peer-
    introduced expression that contradicts an earlier introduction is a
    protocol violation rather than a local bug). Other callers — main-
    interface init, push-allocators — pass arguments that cannot trigger
    this exception, so if it fires from one of those paths it propagates
    as a local bug.
    """


####################################################################################################
# Connection-specific lease cleanup info
####################################################################################################


@dataclasses.dataclass(frozen=True)
class ConnectionLeaseCleanupInfo(_LeaseCleanupInfo):
    """Cleanup info attached to every imported lease we hand out.

    The connection implements the ``_LeaseOwner`` protocol for every
    :class:`LocalLifetimeLease` it mints against a :class:`RemoteGrant`. Per-lease info is
    narrow: we only need to remember which grant-import-entry a leaked lease belongs to so
    that the weakref-finalizer path can update the entry's ``live_leases`` dict without
    having to walk the whole import table. No ``via``-style stranded resources to clean up —
    imports don't use the conditional-grant mechanism — so the default
    :meth:`_LeaseCleanupInfo.discard_stranded_resources` no-op is correct.

    Attributes:
        grant_import_id: The import ID of the grant the lease was minted against. Points at
            the :class:`_GrantImportEntry` whose ``live_leases`` dict owns this lease's
            tracking tuple.
    """

    grant_import_id: int


####################################################################################################
# Phase-1 substitution carrier
####################################################################################################


@dataclasses.dataclass(frozen=True)
class ConstantExpr[T: IRValue](Expression[T]):
    """Expression-tree carrier for a runtime value that has already been resolved.

    Produced by ``LeasedCapRPCConnection._phase1_substitute`` when it walks an incoming
    :class:`Expression` tree and replaces every ``Imported…Expr`` / ``Exported…Expr`` leaf
    with a concrete runtime value (a cloned lease, a ``Remote…`` proxy, an already-known
    local handle/future/token, etc.). The phase-2 async evaluator consumes these directly
    without further table lookup.

    Not wire-serializable. The wire codec (Stage E) must reject ``_ConstantExpr`` on encode.
    Kept private to this module for now; may be promoted to a public ``Expression`` subtype
    later if we want to expose "expressions that evaluate to runtime values" as a user-facing
    concept, but the wire-protocol type set in :mod:`lowering_types` stays clean until then.

    Attributes:
        value: The already-resolved runtime value. Phase 1 only substitutes ``IRValue``-typed
            leaves (leases, handles, futures, tokens) — ``UnwrapForPipelining`` stays in the
            tree as an ``AwaitFutureExpr`` node for phase 2 to handle.
    """

    value: T


####################################################################################################
# Import / export table entries
####################################################################################################


@dataclasses.dataclass
class TokenImportEntry:
    """Import-table entry for a lifetime token we have imported from the remote.

    We hold state locally so that :class:`RemoteLifetimeToken` proxies can remain stateless.

    Attributes:
        import_id: The import ID of this token.
        introduction_refcount: See :class:`GrantImportEntry`. Every
            ``Exported…Expr`` that references this token — whether standalone
            :class:`ExportedLifetimeTokenExpr`, or the ``token_export_id`` field
            on an :class:`ExportedLeaseExpr` / :class:`ExportedIRHandleExpr` /
            :class:`ExportedIRFutureExpr` — counts as exactly one introduction.
            On first introduction the count starts at 1; re-introductions bump
            it.

            Unlike :class:`GrantImportEntry.introduction_refcount`, this counter
            is *not* discharged by the dependent handle/future/grant discard
            paths. Token-introductions carried by dependent ``Exported…Expr``\\ s
            stay accumulated here until the cached :class:`RemoteLifetimeToken`
            proxy for this import GCs; its finalizer then emits a single
            :class:`DiscardMessage` for the whole refcount and zeroes it. The
            pin-chain invariant (``RemoteGrant`` / ``RemoteIRHandle`` /
            ``RemoteIRFuture`` each hold a strong reference to their subject
            token's cached proxy via their ``_token`` field) guarantees this
            final emission happens only after every dependent has already
            retired its own :class:`DiscardMessage` — see "Proxy-GC tracking"
            in ``lowering_remote_proxies.py``.

            The reserved main-interface token (id 0) starts at 1 plus the
            `+= 2` pair-bump for the main handle and main grant, and never
            discharges over the wire — it's pinned by
            ``_their_main_token_proxy`` for the connection's lifetime, and
            the connection's abort path short-circuits the finalizer.
        live_proxy_count: Number of live :class:`RemoteLifetimeToken` proxies
            referencing this import. Bumped by the proxy's ``__post_init__``;
            decremented by its :func:`weakref.finalize`. When it drops to
            zero while ``introduction_refcount > 0``, the finalizer emits a
            single :class:`DiscardMessage` retiring the full accumulated
            refcount — see ``introduction_refcount`` above.
        ended: True once we have received a :class:`ClosedMessage` for this token (or been
            aborted); set transitions firing ``end_callbacks`` synchronously.
        end_callbacks: Callbacks registered by ``RemoteLifetimeToken.register_end_callback``.
            Fires exactly once, when ``ended`` transitions from False to True, and then is
            cleared. Callbacks registered on an already-ended entry fire immediately.
        watch_sent: True once we have sent a :class:`WatchMessage` for this token. We send
            at most one Watch per token lifetime; subsequent registrations piggyback on
            the existing subscription.
    """

    import_id: int
    introduction_refcount: int
    ended: bool
    end_callbacks: list[Callable[[], None]]
    watch_sent: bool
    live_proxy_count: int = 0


@dataclasses.dataclass
class GrantImportEntry:
    """Import-table entry for a grant we have imported from the remote.

    Attributes:
        import_id: The import ID of this grant.
        token_import_id: The import ID of the grant's subject lifetime token. Fixed at the
            first introduction of the grant (any message introducing a grant also fixes its
            subject token).
        introduction_refcount: Count of outstanding introductions of this grant ID, per the
            protocol's refcount rule (one per allocation-by-push and one per received
            ``Exported…Expr`` referring to this ID — see :class:`DiscardMessage` for the
            full rule). When we send a :class:`DiscardMessage`, we include the current value
            and reset this to zero atomically with the enqueue.
        live_leases: Mapping from ``id(lease)`` to a ``(info, finalizer)`` tuple for each
            lease we have handed out under this grant. Shape mirrors the tracking dicts
            inside :class:`UnconditionalLocalLifetime` / :class:`DerivedLocalLifetime`:
            the finalizer (a :class:`weakref.finalize`) holds a weak reference to the lease
            so that the connection's strong reference in this dict does not defeat
            leak-detection. Iterating live leases is done via ``finalizer.peek()``.
            ``_register_lease`` inserts; explicit discard and the weakref-finalizer leak
            path remove. When this becomes empty and the grant is not broken, we send a
            :class:`DiscardMessage` for the current ``introduction_refcount``.
        wait_break_callbacks: Mapping from a unique subscription ID to a
            ``Callable[[str], None]`` break-notification callback registered by each
            in-flight :meth:`RemoteIRFuture.wait` against this grant. These are "lease-
            equivalent" break subscriptions used by waits that have already discarded
            their user-visible lease (per the no-round-trip design described in
            ``phase_d_plan.md``): the wait holds no :attr:`live_leases` slot, but still
            wants a prompt ``LeaseBrokenError`` if the grant breaks. Fired by
            :meth:`LeasedCapRPCConnection._mark_grant_import_broken` and
            :meth:`LeasedCapRPCConnection._abort` with ``broken_reason``, alongside the
            ``live_leases`` break fire. Does not contribute to ``introduction_refcount``
            or :class:`DiscardMessage` accounting — it's a pure notification channel.
            The wait's ``finally`` block deregisters its entry; this dict being
            non-empty does NOT by itself block drain (drain gates are on the symmetric
            future-level :attr:`FutureImportEntry.resolve_obligation_refcount`).
        broken_reason: Non-None once a :class:`BreakMessage` has been processed for this
            grant (or on connection abort). At that point every live lease in ``live_leases``
            is marked broken synchronously and then removed from the dict by the normal
            discard path, and every callback in ``wait_break_callbacks`` is fired and
            then cleared.
    """

    import_id: int
    token_import_id: int
    introduction_refcount: int
    live_leases: dict[int, tuple[ConnectionLeaseCleanupInfo, weakref.finalize]]
    wait_break_callbacks: dict[int, Callable[[str], None]]
    broken_reason: str | None


@dataclasses.dataclass
class HandleImportEntry:
    """Import-table entry for an :class:`IRHandle` we have imported from the remote.

    Attributes:
        import_id: The import ID of this handle.
        token_import_id: The import ID of the handle's lifetime token. Fixed at the first
            introduction of the handle (see ``ExportedIRHandleExpr``).
        introduction_refcount: See :class:`_GrantImportEntry`. We emit a
            :class:`DiscardMessage` for handles only opportunistically — implementors may
            ignore discard-of-handle messages — but we still track the count to cooperate
            with peers that care.
        live_proxy_count: Number of live :class:`RemoteIRHandle` proxies currently
            referencing this import. Bumped by each proxy's ``__post_init__``; decremented
            by a :func:`weakref.finalize` on the proxy. When this drops to zero while
            ``introduction_refcount`` is still non-zero, we emit a :class:`DiscardMessage`
            for the full outstanding count and zero ``introduction_refcount`` — see §2j in
            ``plans/phase3/stage8_lease_system/phase_d_plan.md``.
    """

    import_id: int
    token_import_id: int
    introduction_refcount: int
    live_proxy_count: int = 0


@dataclasses.dataclass
class FutureImportEntry:
    """Import-table entry for an :class:`IRFuture` we have imported from the remote.

    Attributes:
        import_id: The import ID of this future.
        token_import_id: The import ID of the future's lifetime token. Fixed at the first
            introduction of the future (see ``ExportedIRFutureExpr``).
        introduction_refcount: See :class:`_GrantImportEntry`.
        resolve_obligation_refcount: Count of outstanding "please resolve" obligations the
            exporter has to us (see :class:`UnpullMessage` for the full rule): +1 per
            :class:`PullMessage` we send, +1 per ``AwaitFutureExpr`` we *receive* that
            references this future. A single inbound :class:`ResolveMessage` /
            :class:`RejectMessage` discharges *all* of them (we zero the count on delivery);
            an outbound :class:`UnpullMessage` releases the current count and zeros it.
            While non-zero, the entry's import ID is tracked in
            :attr:`LeasedCapRPCConnection._active_resolve_obligation_imports` and blocks
            drain. Named "obligation" rather than "pull" because ``AwaitFutureExpr``
            receipt contributes even without a local :class:`PullMessage`.
        local_consumer_count: Running tally of in-flight local consumers that still
            want a resolution — incremented and decremented symmetrically by:
            (1) :meth:`RemoteIRFuture.wait` invocations, (2) the phase-2 spawn for a
            message whose phase-1 substitution bumped this entry for an inbound
            ``AwaitFutureExpr`` (the bump is held for the duration of the spawn's
            evaluation so that the intermediate "no local waiter yet" window doesn't
            trigger an unpull mid-evaluation). When this drops to zero while
            ``resolve_obligation_refcount > 0`` and the box has not been resolved, we
            emit :class:`UnpullMessage` for the full outstanding refcount, zero the
            refcount, and drop the entry from the active set.
        resolution: The :class:`PendingResultBox` that waiters subscribe to. Populated
            lazily the first time something is ``wait``ed or ``pipeline_dispatch``ed on
            this import; receipt of :class:`ResolveMessage` / :class:`RejectMessage`
            drives the box's state transitions. The box is *not* a
            :class:`LocalPendingResultBox`: that subclass auto-discards on its observed
            token's end-callback, but the peer's token can end while we're still
            locally producing a result, which would surface :class:`LeaseBrokenError`
            to a parked waiter. Instead the discard policy lives here, gated on
            ``delivery_in_flight`` and ``local_consumer_count`` — see
            ``plans/extras/extra5_future_box_race_fix.md``.
        delivery_in_flight: True between when ``_on_resolve`` spawns its ``deliver``
            task and when that task's ``finally`` clears the flag. Used by the
            box-discard gate so a peer-driven token-end during async ``eval_forwardable``
            doesn't tear down a box whose ``set_result`` is about to land.
        token_ended: Mirror of the subject token's ended state. Set by the box's
            token-end subscription installed at materialization time.
        box_token_unsubscribe: Deregister handle for the token-end subscription
            installed when the box is materialized. Cleared once the box has been
            discarded so the token's ``end_callbacks`` list doesn't keep a dead entry.
        live_proxy_count: Number of live :class:`RemoteIRFuture` proxies currently
            referencing this import (counts each ``walk()`` child as a separate proxy;
            they all target the same import id). Bumped by each proxy's ``__post_init__``;
            decremented by a :func:`weakref.finalize` on the proxy. When this drops to zero
            while ``introduction_refcount`` is still non-zero, we emit a
            :class:`DiscardMessage` for the full outstanding count and zero
            ``introduction_refcount`` — see §2j in
            ``plans/phase3/stage8_lease_system/phase_d_plan.md``.
    """

    import_id: int
    token_import_id: int
    introduction_refcount: int
    resolve_obligation_refcount: int
    local_consumer_count: int
    resolution: PendingResultBox[Any] | None
    live_proxy_count: int = 0
    delivery_in_flight: bool = False
    token_ended: bool = False
    box_token_unsubscribe: Callable[[], None] | None = None


@dataclasses.dataclass
class TokenExportEntry:
    """Export-table entry for a locally-owned lifetime token we have exported to the remote.

    Attributes:
        export_id: The export ID of this token.
        local_token: The concrete local token object. Held by strong reference so its
            identity is stable for the lifetime of the entry.
        outstanding_count: Symmetric with :class:`GrantExportEntry.outstanding_count`.
            Starts at 1 on first export and is bumped every time we emit an
            ``ExportedLifetimeTokenExpr`` / ``ExportedLeaseExpr`` / ``ExportedIRHandleExpr``
            / ``ExportedIRFutureExpr`` carrying this token's ``token_export_id``. An
            incoming :class:`DiscardMessage` for this ID decrements by ``refcount``; at
            zero the entry is removed and the reverse-lookup dict purged. (The
            corresponding outbound emission on the importer side is weakref-driven and
            lives in the follow-up stage, so in practice the count only grows today —
            accepting discards keeps the handler symmetric and the protocol spec honest.)
        watch_deregister: The deregister callback returned by
            ``local_token.register_end_callback(...)`` at the moment the remote sent us a
            :class:`WatchMessage` — None until that happens. Keeping the callback lets us
            unsubscribe cleanly on connection abort (or whenever the entry is torn down
            without having fired a :class:`ClosedMessage`).
    """

    export_id: int
    local_token: LifetimeToken[Any]
    outstanding_count: int
    watch_deregister: Callable[[], None] | None


@dataclasses.dataclass
class GrantExportEntry:
    """Export-table entry for a locally-owned grant we have exported to the remote.

    Attributes:
        export_id: The export ID of this grant.
        token_export_id: The export ID of the grant's subject token.
        backing_lease: The single lease we hold on the local grant for as long as the remote
            has any outstanding references. Its purpose is to keep the underlying resource
            alive across the wire.
        outstanding_count: Running tally of introductions we have sent (initial +1 when we
            first export the grant, plus +1 per outbound ``Exported…Expr`` referring to
            this ID). Export entries created to mirror an incoming ``PushMessage``'s
            allocations (token/future/grant) also start at +1, since the push itself
            introduced the ID once on the importer side. Decremented by the refcount
            carried in each incoming :class:`DiscardMessage`; when it reaches zero we
            discard ``backing_lease`` and remove the entry.
        broken_callback_deregister: The deregister handle returned by
            ``backing_lease.register_broken_callback(...)`` when the entry was created. Lets
            us detach the callback cleanly when the entry is torn down (on inbound
            :class:`DiscardMessage` draining ``outstanding_count`` to zero, or on abort),
            so a late backing-lease break doesn't try to send a :class:`BreakMessage` for a
            grant whose export slot has already been removed. ``None`` only transiently,
            while the entry is under construction.
    """

    export_id: int
    token_export_id: int
    backing_lease: Lease[Any]
    outstanding_count: int
    broken_callback_deregister: Callable[[], None] | None = None


@dataclasses.dataclass
class HandleExportEntry:
    """Export-table entry for a locally-owned :class:`IRHandle`.

    Attributes:
        export_id: The export ID of this handle.
        token_export_id: The export ID of the handle's lifetime token.
        local_handle: The concrete local handle object. Held by strong reference.
        outstanding_count: See :class:`_GrantExportEntry`.
    """

    export_id: int
    token_export_id: int
    local_handle: IRHandle[Any]
    outstanding_count: int


@dataclasses.dataclass
class FutureExportEntry:
    """Export-table entry for a locally-owned :class:`IRFuture`.

    .. rubric:: Load-bearing invariant

    The lowering layer maintains the following equivalence at every quiescent point on
    the export side:

        ``resolve_obligation_count > 0 ∧ ¬resolved_sent ⟺ resolve_task_cancel_scope is not None``

    i.e. whenever we owe a :class:`ResolveMessage` and have not yet emitted one, exactly
    one resolver task is alive and its cancel scope is stored here. This is what lets
    the importer side suppress redundant :class:`PullMessage` emission when an inbound
    ``AwaitFutureExpr`` has already pre-paid the obligation
    (:attr:`FutureImportEntry.resolve_obligation_refcount`); the importer relies on the
    exporter actually being mid-delivery whenever the obligation is non-zero.

    Mechanically enforced by
    :meth:`LeasedCapRPCConnection._increment_resolve_obligation_count` (spawns a resolver
    on a 0 → positive crossing) and
    :meth:`LeasedCapRPCConnection._decrement_resolve_obligation_count` (cancels the
    resolver on a positive → 0 crossing); these are the *only* mutators of
    ``resolve_obligation_count``, with the sole exception of
    :meth:`LeasedCapRPCConnection._clear_resolve_obligation_on_send` which zeroes the
    count from inside the resolver itself at the moment of Resolve/Reject emission and
    therefore deliberately bypasses the cancel path.

    Attributes:
        export_id: The export ID of this future.
        token_export_id: The export ID of the future's lifetime token.
        grant_export_id: The export ID of the grant whose backing lease the resolver task
            should clone when it waits on ``local_future`` and when it holds a "survival
            lease" across the resolver's lifetime. ``None`` until a grant has been paired
            with this future export. Populated immediately by :func:`_on_push` (which
            registers the future and its paired grant together) and by the devaluator's
            ``UnwrapForPipelining`` walk (which pairs a freshly-devaluated lease with the
            future it accompanies). Must be non-``None`` before any 0 → positive crossing
            of ``resolve_obligation_count`` (i.e. before any inbound
            :class:`PullMessage` or outbound ``AwaitFutureExpr`` referencing this
            export) — the resolver-spawn path asserts this.
        local_future: The concrete local future object. Held by strong reference.
        outstanding_count: See :class:`_GrantExportEntry`.
        resolve_obligation_count: Running tally of "please resolve" obligations we owe to
            the remote, following the symmetric rule to
            :attr:`FutureImportEntry.resolve_obligation_refcount`: +1 per incoming
            :class:`PullMessage`, +1 per outbound ``AwaitFutureExpr`` the devaluator emits
            referencing this export (each such expression implicitly pulls on the exporter).
            Decremented by the refcount in incoming :class:`UnpullMessage`s; zeroed when we
            emit our :class:`ResolveMessage` / :class:`RejectMessage` (a single resolution
            discharges all outstanding obligations). Once zeroed by our own resolution,
            late inbound :class:`PullMessage` / ``AwaitFutureExpr`` references are ignored
            — the resolve is already on the wire and the peer's view will catch up.
            Named "obligation" rather than "pull" because outbound ``AwaitFutureExpr``
            contributes even without a matching inbound :class:`PullMessage`.
        resolve_task_cancel_scope: The cancel scope for the background resolver task we
            spawn to await ``local_future`` and deliver the resolution. ``None`` iff no
            resolver task is currently running for this export. Per the invariant above,
            non-``None`` exactly when ``resolve_obligation_count > 0 ∧ not
            resolved_sent``. Set by
            :meth:`LeasedCapRPCConnection._ensure_resolver_running_for` on spawn; cleared
            either by the resolver itself just before emitting Resolve/Reject (via
            :meth:`_clear_resolve_obligation_on_send`) or by
            :meth:`_decrement_resolve_obligation_count` on a positive → 0 crossing,
            which also cancels the scope.
        resolved_sent: True once we have enqueued a :class:`ResolveMessage` or
            :class:`RejectMessage` for this export. Late inbound :class:`PullMessage`
            / ``AwaitFutureExpr`` references arriving after this point are silently
            ignored — the peer's view will catch up once our in-flight message lands.
    """

    export_id: int
    token_export_id: int
    local_future: IRFuture[Any, Any]
    outstanding_count: int
    resolve_obligation_count: int
    resolve_task_cancel_scope: anyio.CancelScope | None
    grant_export_id: int | None = None
    resolved_sent: bool = False


type ImportEntry = TokenImportEntry | GrantImportEntry | HandleImportEntry | FutureImportEntry
type ExportEntry = TokenExportEntry | GrantExportEntry | HandleExportEntry | FutureExportEntry
