"""Leases to local resources.

Defines concrete types for leases to local resources. These are used within a single program to
express the lifetime of resources owned by this program.

For maximum flexibility, local leases are agnostic to the particular resource being leased out, and
the system treats them as opaque objects. It is up to library code to define the semantics of what
resources are available under the lease. In particular, when library code attaches a lifetime token
(or conditional grant) to a local resource, the library commits to:
- Keeping all resources gated by the token alive as long as they could possibly be accessed via a
    lease,
- Closing the token (and thus breaking any outstanding leases) as soon as possible if the resources
    unexpectedly become no longer available,
- As a best practice, failing fast if someone tries to use the resource with an invalid lease, to
    avoid bugs and unexpected behavior.

What it means to keep a resource alive as long as it could possibly accessed means different things
for conditional and unconditional leases.
- For an unconditional lease, the resource must be kept alive as long as the lease is active.
- For a conditional lease, the resource must be kept alive as long as the conditional lease is
    BOTH active AND satisfiable. This means that, if any of the tokens required for the conditional
    grant become closed (expectedly or unexpectedly), the resource owner is free to close and
    reclaim the desired resource; no lease can possibly be used to satisfy the conditions of the
    lease anymore. (Note that if the conditional grant was used to acquire an _unconditional_ lease
    via exchanging a grant on the now-closed requirement token, that unconditional lease must have
    already broken by virtue of the upstream lease being broken, so we do not have to worry about
    this case here.)
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import traceback as _traceback
import weakref
from collections.abc import Collection, Mapping
from typing import Any, AsyncGenerator, Callable, Generator, Protocol, Self

import anyio

from fxtr2.leases.lease_types import (
    FreshLifetime,
    Grant,
    Lease,
    LeaseBrokenError,
    LeaseDiscardedError,
    LifetimeBrand,
    LifetimeToken,
    _PhantomLifetimeBrand,
)

logger = logging.getLogger(__name__)

type LocalLifetimeBrand = FreshLifetime[Any]
"""A brand for local lifetime tokens and leases."""


LEASE_LEAK_TRACK_ALLOCATIONS: bool = False
"""If True, capture a traceback at lease-registration time so leak warnings can point at the
allocation site. Free to toggle off in hot paths if the stack-walk overhead matters."""


_LEASE_LEAK_COUNT: int = 0
"""Process-wide counter of leases garbage-collected without being explicitly discarded.

Bumped inside :func:`_on_lease_leaked` before the cleanup path runs. Used by the test-suite's
autouse ``_assert_no_lease_leaks`` fixture to detect leaks without having to compete with
tests that intentionally leak for observation. Read via :func:`get_lease_leak_count`.
"""


def get_lease_leak_count() -> int:
    """Return the current value of the process-wide lease-leak counter.

    Each call returns the number of leases that have been GC'd without explicit discard
    since process start. Tests typically capture a snapshot before and after a block and
    assert the delta is zero. Monotonic; no reset.
    """
    return _LEASE_LEAK_COUNT


@dataclasses.dataclass(frozen=True)
class _LeaseCleanupInfo:
    """Base: owner-side info about a registered lease, used for both discard and leak cleanup.

    Concrete subclasses add owner-specific fields. The shared shape is just "enough to log
    a useful leak warning" plus the ``discard_stranded_resources`` hook for the owner-GC'd
    path.

    The weakref finalizer holds a strong reference to the info (via its captured args), so
    any sub-leases referenced by a subclass (e.g. ``_DerivedLeaseCleanupInfo.vias``) stay
    alive until the cleanup path has a chance to discard them — even if the primary lease
    itself is garbage-collected.
    """

    lease_repr: str
    alloc_tb: str | None
    holder_label: str | None = dataclasses.field(default=None, kw_only=True)
    """Optional human-readable statement of who holds this lease and why (e.g.
    ``"independent_lease into <scope>"``), set post-registration via
    ``LocalLifetimeLease.set_debug_label``. Rendered by the scope-drain report
    (``fxtr2/debug/scope_report.py``); the leak-warning finalizer captured the
    pre-label info and does not show it."""

    def discard_stranded_resources(self) -> None:
        """Release resources this info holds that would otherwise leak if the owner is
        GC'd before the lease is cleaned up.

        Default: no-op. Subclasses override when they carry e.g. ``vias`` leases backed
        by separate owners that aren't otherwise reachable.
        """


@dataclasses.dataclass(frozen=True)
class _UnconditionalLeaseCleanupInfo(_LeaseCleanupInfo):
    """Cleanup info for leases owned by an ``UnconditionalLocalLifetime``. No extra state."""


def _capture_alloc_tb() -> str | None:
    if not LEASE_LEAK_TRACK_ALLOCATIONS:
        return None
    # Drop the last frame (this helper itself); keep the rest for the leak warning.
    return "".join(_traceback.format_stack()[:-1])


def _on_lease_leaked(
    owner_ref: weakref.ReferenceType[Any],
    lease_id: int,
    info: _LeaseCleanupInfo,
) -> None:
    """Finalizer callback: called by the GC when a registered lease is collected without
    having been explicitly discarded. Logs a warning and runs the same cleanup path as
    ``_deregister_lease`` would have (via ``_clean_up_lease_data``), so the token still
    closes cleanly if this was the last live lease and any stranded resources carried in
    ``info`` (e.g. a ``via`` lease on the requirement side) are released.
    """
    global _LEASE_LEAK_COUNT
    _LEASE_LEAK_COUNT += 1
    owner = owner_ref()
    alloc_hint = (
        f"\nAllocated at:\n{info.alloc_tb}"
        if info.alloc_tb
        else ("\n(Enable LEASE_LEAK_TRACK_ALLOCATIONS for allocation traceback.)")
    )
    logger.warning(
        "Lease leaked without discard: %s (owner=%s)%s",
        info.lease_repr,
        owner if owner is not None else "<gc'd>",
        alloc_hint,
    )
    if owner is None:
        # Owner is gone, so nothing to update on the owner side. Still release any
        # resources the info carries that aren't otherwise reachable.
        info.discard_stranded_resources()
        return
    owner._clean_up_lease_data(lease_id, info)


class LocalLifetimeToken[RB: LocalLifetimeBrand](LifetimeToken[RB]):
    """A lifetime token for a local, transient resource provided by this running program.

    Concrete token type used by `UnconditionalLocalLifetime` and `DerivedLocalLifetime`. The token
    is transitioned to `ended` exactly once (either by auto-close on last-lease-discard or by an
    explicit `revoke`), at which point registered end-callbacks fire synchronously.

    Not thread-safe: intended to be used within a single event loop. Callers that need cross-
    thread lifetime coordination should serialize through the event loop (e.g. via
    `anyio.from_thread`).

    Satisfies the `LifetimeToken` "only weak references to mutable state" convention in a
    lightweight way: the token holds `_end_callbacks` directly, but the list is drained
    and replaced with an empty list when the token transitions to `ended`, so using the
    token as a dict key (e.g. in the RPC re-export checker) does not pin callback state
    beyond the token's active period.
    """

    # Redeclared on the concrete class for defensive consistency: if a future subclass
    # accidentally defines `__eq__`, Python would set `__hash__ = None` on that subclass
    # unless it also declares `__hash__`. The declaration here at the base concrete class
    # doesn't protect subclasses — they'd still need their own — but it makes the
    # intent explicit at this layer.
    __hash__ = object.__hash__

    def __init__(self, name: str):
        self._name = name
        self._ended = False
        self._end_callbacks: list[Callable[[], None]] = []

    def __repr__(self) -> str:
        status = "ended" if self._ended else "active"
        return f"<LocalLifetimeToken {self._name!r} ({status})>"

    def ended(self) -> bool:
        return self._ended

    def register_end_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        if self._ended:
            callback()
            return _noop_deregister
        self._end_callbacks.append(callback)

        def deregister() -> None:
            try:
                self._end_callbacks.remove(callback)
            except ValueError:
                pass

        return deregister

    def _mark_ended(self) -> None:
        """Transition the token to `ended` and fire end-callbacks. Idempotent."""
        if self._ended:
            return
        self._ended = True
        callbacks = self._end_callbacks
        self._end_callbacks = []
        for cb in callbacks:
            try:
                cb()
            except Exception:
                logger.exception("Error in end callback for %r", self)


def _noop_deregister() -> None:
    pass


class LocalGrant[RB: LocalLifetimeBrand](Grant[RB]):
    """An opaque identity for a grant over a local lifetime token.

    `LocalGrant` is a pure identity object: holding one confers no authority. It exists only as
    something `Lease.grant` can return, so that two leases can be compared for equivalence via
    their `.grant`. Equality is identity-based: each `LocalGrant` instance is its own grant. It
    carries a reference to its subject token only so that `Lease.subject` works.

    The actual revocation authority and lease-tracking state live on whichever object owns the
    grant (`UnconditionalLocalLifetime` or `DerivedLocalLifetime`). Application code should not
    construct `LocalGrant` instances directly.
    """

    def __init__(self, subject: LocalLifetimeToken[RB]):
        self._subject = subject

    def __repr__(self) -> str:
        return f"<LocalGrant subject={self._subject!r}>"

    @property
    def subject(self) -> LifetimeToken[RB]:
        return self._subject

    # `Grant` inherits from `collections.abc.Hashable`, which shadows `object.__hash__` with an
    # abstract method. Re-expose the default identity-based hash so `LocalGrant` is concrete.
    __hash__ = object.__hash__


class _LeaseOwner[RB: LocalLifetimeBrand, I: _LeaseCleanupInfo](Protocol):
    """Protocol for objects that own break-groups of `LocalLifetimeLease`s.

    Implemented by `UnconditionalLocalLifetime` (with ``I = _UnconditionalLeaseCleanupInfo``),
    `DerivedLocalLifetime` (with ``I = _DerivedLeaseCleanupInfo``), and — in the RPC layer
    — `LeasedCapRPCConnection` (with its own connection-specific info shape). Leases
    self-register on construction via `_register_lease` and notify their owner on discard
    via `_deregister_lease`.

    The owner is also responsible for setting up a `weakref.finalize` on each registered lease,
    so that a leaked lease (one that gets garbage-collected without an explicit `discard`) is
    still implicitly cleaned up — the token closes if it was the last holder, any stranded
    resources carried by the info are released, and a warning is logged pointing at the
    allocation site (if `LEASE_LEAK_TRACK_ALLOCATIONS` is enabled).
    """

    def _register_lease(self, lease: LocalLifetimeLease[RB]) -> None: ...
    def _deregister_lease(self, lease: LocalLifetimeLease[RB]) -> None: ...
    def _clean_up_lease_data(self, lease_id: int, info: I) -> None: ...


class LocalLifetimeLease[RB: LocalLifetimeBrand](Lease[RB]):
    """A lease backed by a lifetime owner (`UnconditionalLocalLifetime` or similar).

    Obtained via `UnconditionalLocalLifetime.root_lease` / `DerivedLocalLifetime.exchange_forward`
    or by cloning an existing lease; application code never constructs these directly. The
    lease's `.grant` is the `LocalGrant` it was issued under, and is used only for lease-equality
    comparisons.
    """

    # `Lease` does not declare `__slots__`, so we inherit a `__dict__` and `__weakref__` from
    # it; we don't need to add `__weakref__` here. Our slots are just for the state fields.
    __slots__ = ("_owner", "_grant", "_state", "_broken_reason", "_broken_callbacks")

    _STATE_ACTIVE = "active"
    _STATE_DISCARDED = "discarded"
    _STATE_BROKEN = "broken"

    def __init__(self, owner: _LeaseOwner[RB, Any], grant: Grant[RB]):
        """Construct a lease and register it with its owner.

        The owner's specific cleanup-info type is not surfaced here — `LocalLifetimeLease`
        only calls `_register_lease` / `_deregister_lease` on the owner, which are info-
        type-agnostic. The info type only matters for the weakref-finalizer path, which
        the owner sets up internally.
        """
        self._owner = owner
        self._grant = grant
        self._state = LocalLifetimeLease._STATE_ACTIVE
        self._broken_reason: str | None = None
        self._broken_callbacks: list[Callable[[str], None]] = []
        owner._register_lease(self)

    def __repr__(self) -> str:
        return f"<LocalLifetimeLease grant={self._grant!r} state={self._state}>"

    def set_debug_label(self, label: str) -> None:
        """Attach a human-readable holder label to this lease's tracking info.

        Purely diagnostic: rendered by the scope-drain report
        (``fxtr2/debug/scope_report.py``) when this lease shows up as an
        outstanding hold on a draining scope. No effect on lease semantics;
        no-op if the lease is no longer tracked (already discarded/broken)
        or if the owner does not track labels (the optional hook is
        implemented by the two local lifetime owners; other `_LeaseOwner`
        implementers — e.g. the RPC connection — simply drop it).

        Args:
            label: Human-readable statement of who holds this lease and
                why (e.g. ``"independent_lease into <scope name>"``).
                Later calls overwrite earlier ones; clones are not
                labeled by this call.
        """
        setter = getattr(self._owner, "_set_lease_debug_label", None)
        if setter is not None:
            setter(self, label)

    @property
    def grant(self) -> Grant[RB]:
        return self._grant

    def is_active(self) -> bool:
        return self._state == LocalLifetimeLease._STATE_ACTIVE

    def clone(self) -> Self:
        self.ensure_active()
        # Constructor self-registers with the owner.
        return type(self)(self._owner, self._grant)

    def take(self) -> Self:
        result = self.clone()
        self.discard()
        return result

    def discard(self) -> None:
        if self._state == LocalLifetimeLease._STATE_ACTIVE:
            self._state = LocalLifetimeLease._STATE_DISCARDED
            self._broken_callbacks = []
            self._owner._deregister_lease(self)
        elif self._state == LocalLifetimeLease._STATE_BROKEN:
            # Already broken; clear callbacks (they've fired) and transition out.
            self._state = LocalLifetimeLease._STATE_DISCARDED
            self._broken_callbacks = []
        # DISCARDED: no-op.

    def ensure_active(self) -> None:
        if self._state == LocalLifetimeLease._STATE_ACTIVE:
            return
        if self._state == LocalLifetimeLease._STATE_BROKEN:
            raise LeaseBrokenError(
                f"Lease on {self._grant.subject} is broken: {self._broken_reason}"
            )
        raise LeaseDiscardedError(f"Lease on {self._grant.subject} has been discarded")

    def register_broken_callback(self, callback: Callable[[str], None]) -> Callable[[], None]:
        if self._state == LocalLifetimeLease._STATE_BROKEN:
            callback(self._broken_reason or "")
            return _noop_deregister
        if self._state == LocalLifetimeLease._STATE_DISCARDED:
            # Lease is gone; no further events will fire.
            return _noop_deregister
        self._broken_callbacks.append(callback)

        def deregister() -> None:
            try:
                self._broken_callbacks.remove(callback)
            except ValueError:
                pass

        return deregister

    def _mark_broken(self, reason: str) -> None:
        """Transition this lease to the broken state and fire broken-callbacks."""
        if self._state != LocalLifetimeLease._STATE_ACTIVE:
            return
        self._state = LocalLifetimeLease._STATE_BROKEN
        self._broken_reason = reason
        callbacks = self._broken_callbacks
        self._broken_callbacks = []
        for cb in callbacks:
            try:
                cb(reason)
            except Exception:
                logger.exception("Error in broken callback for lease on %r", self._grant.subject)


class _PreBrokenLeaseOwner:
    """Owner for leases minted by :func:`make_broken_lease`.

    Such leases are born broken: they pin no resource, never join a break-group,
    and need no leak finalizer (a GC'd broken lease releases nothing). All owner
    hooks are therefore no-ops.
    """

    def _register_lease(self, lease: LocalLifetimeLease[Any]) -> None:
        del lease

    def _deregister_lease(self, lease: LocalLifetimeLease[Any]) -> None:
        del lease

    def _clean_up_lease_data(self, lease_id: int, info: _LeaseCleanupInfo) -> None:
        del lease_id, info


_PRE_BROKEN_LEASE_OWNER = _PreBrokenLeaseOwner()


def make_broken_lease[RB: LocalLifetimeBrand](grant: Grant[RB], reason: str) -> Lease[RB]:
    """Mint a fresh lease on ``grant`` that is already broken with ``reason``.

    For responder-side teardown races: a holder's request referencing a grant
    whose subject lifetime has just been revoked locally can be answered with
    the lease it *would* have gotten an instant before the break — same
    ``.grant`` identity, ``is_active()`` False, ``ensure_active()`` raising
    :class:`LeaseBrokenError` naming the real subject and the original break
    reason. Used by the RPC layer's phase-1 substitution (see the
    break-tombstone handling in ``rpc/lowering_evaluate.py``).
    """
    lease = LocalLifetimeLease(_PRE_BROKEN_LEASE_OWNER, grant)
    lease._mark_broken(reason)
    return lease


class UnconditionalLocalLifetime[RB: LocalLifetimeBrand](anyio.AsyncContextManagerMixin):
    """A root object for an unconditional lifetime token and lease.

    UnconditionalLocalLifetime owns exactly one `Grant` — the root grant for its token — and all
    leases derived from this lifetime share that grant (i.e. they all compare equal via `.grant`).

    The lifetime eagerly constructs a single "root" lease at construction time; `root_lease` is a
    stable property that returns that same lease every time. The lifetime token closes
    automatically as soon as the last live lease is discarded — typically the caller either
    discards `root_lease` directly, or calls `root_lease.take()` / `root_lease.clone()` to
    transfer ownership and lets the cloned copy drive the lifetime.

    Two scoped-usage patterns are provided:

    - ``with lt.revoke_after_block() as lt:`` (sync): on block exit, revoke the lifetime
      synchronously. Use when the lifetime is self-contained inside the block and nothing is
      expected to outlive it.
    - ``async with UnconditionalLocalLifetime(...) as lt:`` (async, via
      `anyio.AsyncContextManagerMixin`): on normal block exit, discard the root lease and
      *drain* — wait until all other holders have released their clones before unwinding. On
      any exception (including cancellation) the lifetime is revoked instead for fast close.
      Use when you're handing out leases and want to guarantee holders drain.
    """

    def __init__(self, name: str):
        self._name = name
        self._token: LocalLifetimeToken[RB] = LocalLifetimeToken(name)
        self._grant: LocalGrant[RB] = LocalGrant(self._token)
        # Per-lease tracking, keyed by `id(lease)`. The tuple is `(info, finalizer)`. The
        # `finalizer` is a `weakref.finalize` that fires `_on_lease_leaked` if the lease is
        # garbage-collected without going through `_deregister_lease`. On the normal discard
        # path we detach the finalizer before it fires.
        self._tracking: dict[int, tuple[_UnconditionalLeaseCleanupInfo, weakref.finalize]] = {}
        # Eagerly construct the single root lease; it self-registers via `_register_lease`.
        self._root_lease: LocalLifetimeLease[RB] = LocalLifetimeLease(self, self._grant)

    def __repr__(self) -> str:
        return f"<UnconditionalLocalLifetime {self._name!r}>"

    @property
    def token(self) -> LocalLifetimeToken[RB]:
        """Get the lifetime token for this lifetime.

        Each UnconditionalLocalLifetime owns a globally-unique lifetime token which is distinct from
        all other lifetime tokens.
        """
        return self._token

    @property
    def root_lease(self) -> Lease[RB]:
        """Get the root lease for this lifetime.

        Returns a stable reference to the single root lease (the same object every call — this is
        not a fresh-lease factory). Clone the returned lease to produce independent copies, or
        `take()` it to transfer its ownership. Discarding the root lease while no clones are
        outstanding closes the lifetime token.
        """
        return self._root_lease

    def revoke(self, reason: str) -> None:
        """Revoke the lifetime and break all outstanding leases.

        Synchronously breaks every live lease for this lifetime (firing their broken-callbacks)
        and marks the lifetime token itself as having ended (firing its end-callbacks). Any
        subsequent operations on the broken leases raise `LeaseBrokenError`.

        Idempotent: if the lifetime has already ended (either via explicit revoke or via the last
        lease being discarded), this is a no-op.

        Args:
            reason: Human-readable description of why the lifetime is being revoked.
        """
        if self._token.ended():
            return
        tracking = self._tracking
        self._tracking = {}
        for _info, finalizer in tracking.values():
            peek = finalizer.peek()
            if peek is not None:
                lease = peek[0]
                lease._mark_broken(reason)
            finalizer.detach()
        # No `via` leases for unconditional lifetimes, so no per-lease cleanup beyond the
        # detach above.
        self._token._mark_ended()

    @contextlib.contextmanager
    def revoke_after_block(self) -> Generator[Self, None, None]:
        """Return a sync context manager that revokes this lifetime on block exit.

        ``with lt.revoke_after_block() as lt:`` runs the block with `lt` live, then on any
        exit (normal or exceptional) calls `self.revoke("block exit")` — synchronously
        breaking any outstanding leases. Use when the lifetime is self-contained inside the
        block and nothing is expected to outlive it.
        """
        try:
            yield self
        finally:
            self.root_lease.discard()
            self.revoke("block exit")

    async def aclose(self) -> None:
        """Discard the root lease and wait for the token to drain.

        Discards ``self.root_lease`` (signalling "no more new holders will be added"),
        then waits for the token's end-callback to fire — i.e. for all remaining clones
        to be released. On any cancellation during the wait, the lifetime is revoked for
        a fast close and the cancellation propagates.

        Idempotent: if the token has already ended, returns immediately.

        This is the canonical close path for an unconditional lifetime that was handed
        out to other holders; ``async with`` wraps ``aclose`` in a try/finally.
        """
        if self._token.ended():
            self._root_lease.discard()
            return
        end_event = anyio.Event()
        deregister = self._token.register_end_callback(end_event.set)
        try:
            self._root_lease.discard()
            if self._token.ended():
                return
            try:
                await end_event.wait()
            except BaseException:
                self.revoke("aclose cancelled during drain")
                raise
        finally:
            deregister()

    @contextlib.asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncGenerator[Self, None]:
        """Async-CM form: discard root on normal exit and wait for drain; revoke otherwise.

        On normal block exit, this discards our root lease and then awaits the token
        reaching `ended()` — i.e. waits for all other holders to release their clones.
        On any exception (including cancellation, which arrives as a BaseException
        subclass), the lifetime is revoked instead for fast close.

        Use when you're handing out leases and want to guarantee holders drain before the
        scope unwinds.
        """
        try:
            yield self
        except BaseException as exc:
            self.revoke(f"exception in block: {type(exc).__name__}")
            raise
        await self.aclose()

    # --- Internal protocol called by owned leases. ---

    def _register_lease(self, lease: LocalLifetimeLease[RB]) -> None:
        if self._grant != lease.grant:
            raise RuntimeError(f"Lease {lease!r} does not belong to {self!r}: grant mismatch.")
        if self._token.ended():
            raise LeaseBrokenError(
                f"Cannot issue lease: lifetime {self._name!r} has already ended."
            )
        lease_id = id(lease)
        info = _UnconditionalLeaseCleanupInfo(
            lease_repr=repr(lease),
            alloc_tb=_capture_alloc_tb(),
        )
        finalizer = weakref.finalize(lease, _on_lease_leaked, weakref.ref(self), lease_id, info)
        self._tracking[lease_id] = (info, finalizer)

    def _set_lease_debug_label(self, lease: LocalLifetimeLease[RB], label: str) -> None:
        """Replace the tracked info with a labeled copy (see
        ``LocalLifetimeLease.set_debug_label``). The leak finalizer keeps the
        pre-label info it captured at registration — labels are for the
        scope-drain report, which reads this dict.

        Args:
            lease: The registered lease to label; unknown/deregistered
                leases are a no-op.
            label: The holder label to record on the tracking info.
        """
        entry = self._tracking.get(id(lease))
        if entry is not None:
            info, finalizer = entry
            self._tracking[id(lease)] = (
                dataclasses.replace(info, holder_label=label),
                finalizer,
            )

    def _deregister_lease(self, lease: LocalLifetimeLease[RB]) -> None:
        if self._grant != lease.grant:
            raise RuntimeError(f"Lease {lease!r} does not belong to {self!r}: grant mismatch.")
        lease_id = id(lease)
        entry = self._tracking.get(lease_id)
        if entry is None:
            # Already deregistered (double-discard or race with finalizer). Idempotent no-op.
            return
        info, finalizer = entry
        finalizer.detach()
        self._clean_up_lease_data(lease_id, info)

    def _clean_up_lease_data(self, lease_id: int, info: _UnconditionalLeaseCleanupInfo) -> None:
        """Common owner-side cleanup for both the normal discard path and the leak finalizer.

        Removes the tracking entry (if still present) and closes the token if this was the
        last live holder. No stranded-resource cleanup needed for unconditional lifetimes —
        the info carries no sub-leases (so ``info`` is unused here; the parameter is kept
        to satisfy the ``_LeaseOwner`` protocol shape). Callers on the discard path are
        expected to have already detached the finalizer; leak-path callers come in via
        ``_on_lease_leaked`` after the finalizer has fired.
        """
        del info  # unused: unconditional info carries no sub-leases
        self._tracking.pop(lease_id, None)
        if not self._tracking and not self._token.ended():
            self._token._mark_ended()


# ---------------------------------------------------------------------------
# DerivedLocalLifetime — output token tied to a set of arbitrary input tokens.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _DerivedLeaseCleanupInfo(_LeaseCleanupInfo):
    """Cleanup info for output leases owned by a ``DerivedLocalLifetime``.

    Attributes:
        vias: Map from each input lifetime token to an equivalent lease on that input.
            **The same dict object is shared by reference with the owning
            :attr:`LocalDerivedTokenLease._vias` field** — see that class's docstring
            for the full invariant. Owner-side cleanup iterates this dict, discards
            each lease, and ``.clear()``s; because the dict is shared, the lease's
            ``_vias`` is emptied by the same operation. ``discard_stranded_resources``
            (used when the owner is GC'd before the lease) does the same, independent
            of the owner. Any code that mutates ``vias`` after registration must also
            update the lease's ``_vias`` — today only ``_clean_up_lease_data`` /
            ``_close_output`` / ``discard_stranded_resources`` / ``exchange_reverse``
            touch it, and each either clears through the shared reference or relies
            on the ``ensure_active`` guard at the lease level.
    """

    vias: dict[LifetimeToken[Any], Lease[Any]]

    def discard_stranded_resources(self) -> None:
        for via in self.vias.values():
            via.discard()
        self.vias.clear()


@dataclasses.dataclass(frozen=True)
class _DerivedOutputGrant[RB_Out: LocalLifetimeBrand](Grant[RB_Out]):
    """The grant backing a forward-exchanged output lease on a ``DerivedLocalLifetime``.

    Identity is structural: ``(derived, input_grants)``. Two forward-exchanges whose
    input-lease grants are equal as a ``frozenset`` produce output leases that are
    clones of each other. Two exchanges whose input-grant sets differ produce distinct
    output grants. The frozenset representation means input-order in the caller's
    mapping does not affect equality.
    """

    _derived: DerivedLocalLifetime[RB_Out]
    _input_grants: frozenset[Grant[Any]]

    @property
    def subject(self) -> LifetimeToken[RB_Out]:
        return self._derived._output_token


class LocalDerivedTokenLease[RB_Out: LocalLifetimeBrand](LocalLifetimeLease[RB_Out]):
    """A lease on the output lifetime of a ``DerivedLocalLifetime``.

    Each output lease carries a ``_vias`` dict mapping each input token to an
    equivalent lease on that input. These are the leases that make "output lease
    alive ⇒ every input alive" structurally true, and are what ``exchange_reverse``
    hands back to the caller.

    **Shared-dict invariant:** ``_vias`` is the *same dict object* that the owning
    :class:`DerivedLocalLifetime` stores on ``_DerivedLeaseCleanupInfo.vias``. This
    has three consequences:

    - Owner-side cleanup paths (normal discard via ``_clean_up_lease_data`` and
      leak-finalizer via ``_on_lease_leaked``) iterate the info's ``vias`` and
      ``.clear()`` it; that same iteration empties the lease's ``_vias``.
    - The input-end / revoke cascade (``DerivedLocalLifetime._close_output``)
      similarly discards + clears through the info reference; after the cascade,
      this lease is broken AND its ``_vias`` is ``{}``.
    - Anything that later inspects ``_vias`` on a broken lease — e.g. a stale
      ``exchange_reverse`` call — will consistently see an empty dict (plus the
      ``ensure_active()`` check in ``.take()`` raises ``LeaseBrokenError`` first,
      so stale callers never actually reach the body).

    If you ever need to mutate ``_vias`` out-of-band (adding/removing entries
    after registration), you must also coordinate with the owner's info
    reference, or switch both sides to a snapshot-at-registration model.
    """

    __slots__ = ("_vias",)

    def __init__(
        self,
        owner: _LeaseOwner[RB_Out, Any],
        grant: _DerivedOutputGrant[RB_Out],
        vias: dict[LifetimeToken[Any], Lease[Any]],
    ):
        self._vias = vias
        super().__init__(owner, grant)  # self-registers, captures `vias` into cleanup info

    def clone(self) -> Self:
        self.ensure_active()
        return type(self)(
            self._owner,
            self._grant,  # type: ignore[arg-type]
            {tok: via.clone() for tok, via in self._vias.items()},
        )


class DerivedLocalLifetime[RB_Out: LocalLifetimeBrand]:
    """A lifetime whose output token is tied to a *set* of input tokens.

    Ties a new output lifetime to an arbitrary set of input lifetimes, all treated
    symmetrically. The output is valid only while every input is still alive — so
    this is the primitive for intersecting and coordinating multiple independent
    lifetimes into a single downstream handle. All input tokens contribute equally;
    there is no privileged "key" / "requirement" split in the primitive itself.

    Callers who want the ability to cleanly rescind the output lifetime on demand
    — independent of when the real upstream lifetimes end — follow a "key-gating"
    idiom on top: create a private :class:`UnconditionalLocalLifetime` whose sole
    purpose is to be an input, include its token in ``inputs``, and retain its
    root lease. Dropping the key's root (and any clones) ends the key input,
    which in turn ends the output via the cascade below. See the
    ``LOCAL_CODE_OVERVIEW.md`` common-patterns section for the full composition
    recipe.

    **Cascade rules:**

    +-----------------------------------------+---------------+--------------+-----------+
    | Trigger                                 | Output leases | Output token | Inputs    |
    +=========================================+===============+==============+===========+
    | Any input token ends                    | broken        | ends         | untouched |
    |                                         | ("input X     |              |           |
    |                                         | ended")       |              |           |
    +-----------------------------------------+---------------+--------------+-----------+
    | ``revoke(reason)``                      | broken        | ends         | untouched |
    +-----------------------------------------+---------------+--------------+-----------+
    | All output leases discarded             | —             | stays alive  | untouched |
    | while all inputs still live             |               | (idle);      |           |
    |                                         |               | ``exchange_  |           |
    |                                         |               | forward``    |           |
    |                                         |               | still ok     |           |
    +-----------------------------------------+---------------+--------------+-----------+

    The output token ends **iff** any input ends or ``revoke`` is called. A nice
    consequence: any live output lease implies every one of its ``_vias`` is live,
    so ``exchange_reverse`` can always clone vias without worrying about
    broken-input edge cases.

    **DOA:** If any input is already ended at construction, the output token is
    closed synchronously during ``__init__``. Subsequent ``exchange_forward`` calls
    raise ``LeaseBrokenError``. An empty input set is a degenerate but legal
    configuration; the output only ever ends via ``revoke`` in that case.
    """

    def __init__(self, name: str, inputs: Collection[LifetimeToken[Any]]):
        self._name = name
        # frozenset: tokens compare with __hash__/__eq__ — identity for local tokens today,
        # value-based for remote tokens in the RPC layer.
        self._input_tokens: frozenset[LifetimeToken[Any]] = frozenset(inputs)

        self._output_token: LocalLifetimeToken[RB_Out] = LocalLifetimeToken(f"{name}/output")

        # Per-lease tracking for output leases only. (The Derived does not own any
        # lease on its inputs — vias live inside each output lease.) Tuple is
        # ``(info, finalizer)``; the finalizer fires ``_on_lease_leaked`` on GC
        # without explicit discard.
        self._output_tracking: dict[int, tuple[_DerivedLeaseCleanupInfo, weakref.finalize]] = {}

        # Subscribe to each input. If an input is already ended, its callback fires
        # synchronously here → the output is DOA.
        self._input_dereg: dict[LifetimeToken[Any], Callable[[], None]] = {}
        for token in self._input_tokens:
            # Capture `token` in a default-arg to avoid late-binding in the closure.
            self._input_dereg[token] = token.register_end_callback(
                lambda t=token: self._on_input_ended(t)
            )

    def __repr__(self) -> str:
        return f"<DerivedLocalLifetime {self._name!r}>"

    @property
    def token(self) -> LocalLifetimeToken[RB_Out]:
        """The output lifetime token. Distinct from every input token."""
        return self._output_token

    @property
    def inputs(self) -> frozenset[LifetimeToken[Any]]:
        """The declared input token set, as a frozenset.

        Snapshots the inputs supplied at construction; never mutated.
        Useful for validation in higher-level code that wraps a
        :class:`DerivedLocalLifetime` (e.g. ``CapIRArena`` checks that
        its declared X / K tokens are members of the derived's inputs).
        """
        return self._input_tokens

    def exchange_forward(
        self,
        input_leases: Mapping[LifetimeToken[Any], Lease[Any]],
    ) -> Lease[RB_Out]:
        """Exchange leases for every input into a fresh output lease.

        ``input_leases.keys()`` must equal the Derived's declared input token set
        exactly — missing or extra keys raise ``RuntimeError``. Takes ownership of
        every lease in the mapping (per the lease-transfer convention); on validation
        failure all supplied leases are discarded.

        Grant identity is structural: two exchanges whose input-lease grants are
        equal (as a ``frozenset``) produce output leases with equal grants. Cloning
        an output lease produces further clones with the same grant.

        Raises:
            RuntimeError: If the mapping keys don't match the declared inputs.
            LeaseBrokenError: If the output token has already ended (DOA / post-revoke
                / post-input-end).
            LeaseDiscardedError / LeaseBrokenError: If any supplied lease is inactive
                (surfaced via ``take()``).
        """
        # Take ownership of all inputs up front; the with-stack discards them on any
        # exception before we reach the construction step.
        with contextlib.ExitStack() as stack:
            taken: dict[LifetimeToken[Any], Lease[Any]] = {}
            for token, lease in input_leases.items():
                taken[token] = stack.enter_context(lease.take())

            supplied_keys = frozenset(input_leases.keys())
            if supplied_keys != self._input_tokens:
                missing = self._input_tokens - supplied_keys
                extra = supplied_keys - self._input_tokens
                raise RuntimeError(
                    "exchange_forward input token set mismatch on"
                    f" {self._name!r}: missing={set(missing)!r}, extra={set(extra)!r}"
                )

            if self._output_token.ended():
                raise LeaseBrokenError(f"Output lifetime of {self._name!r} has already ended.")

            grant = _DerivedOutputGrant[RB_Out](
                self,
                frozenset(taken[t].grant for t in taken),
            )
            vias: dict[LifetimeToken[Any], Lease[Any]] = {t: taken[t].clone() for t in taken}
            # Output lease self-registers via _register_lease, which reads `vias`
            # into the cleanup info by reference.
            return LocalDerivedTokenLease[RB_Out](self, grant, vias)
            # Taken input leases are discarded by the ExitStack on exit.

    def exchange_reverse(
        self,
        out_lease: Lease[RB_Out],
    ) -> dict[LifetimeToken[Any], Lease[Any]]:
        """Recover equivalent leases for every input token.

        Consumes ``out_lease`` and returns a fresh dict mapping each declared input
        token to an active lease on it. Because any input ending synchronously breaks
        all output leases (and the ``take()`` at the top of this method will raise
        if ``out_lease`` is broken/discarded), every returned lease is active at
        return time.

        Raises:
            TypeError: If ``out_lease`` is not an output lease of this Derived.
            LeaseDiscardedError / LeaseBrokenError: If ``out_lease`` is inactive.
        """
        with out_lease:
            validated = self._validate_output_lease(out_lease, "exchange_reverse")
            return {tok: via.clone() for tok, via in validated._vias.items()}

    def subclone[
        B: LifetimeBrand
    ](self, out_lease: Lease[RB_Out], target: LifetimeToken[B],) -> Lease[B]:
        """Clone the via for ``target`` from ``out_lease``, leaving ``out_lease`` alive.

        Sibling of :meth:`exchange_reverse` for the common case of "I just want one
        of the input vias, and I'd like to keep the output lease usable afterwards."
        Validates ``out_lease`` the same way; takes a peek at its ``_vias`` and
        returns a fresh clone of the entry for ``target``. Does **not** consume
        ``out_lease`` — clone a usable output lease in, get a usable output lease
        and a usable input-via clone out.

        Use :meth:`subtake` if you'd like to consume ``out_lease`` instead.

        Raises:
            TypeError: If ``out_lease`` is not an output lease of this Derived.
            KeyError: If ``target`` is not in the input set.
            LeaseDiscardedError / LeaseBrokenError: If ``out_lease`` is inactive.
        """
        # ensure_active without consuming: peek at the lease state directly.
        out_lease.ensure_active()
        validated = self._validate_output_lease(out_lease, "subclone")
        return self._clone_via(validated, target, "subclone")

    def subtake[
        B: LifetimeBrand
    ](self, out_lease: Lease[RB_Out], target: LifetimeToken[B],) -> Lease[B]:
        """Consume ``out_lease`` and return the via for ``target``; discard the rest.

        Sibling of :meth:`exchange_reverse` for the common case of "I want one of
        the input vias and I'm fine giving up the output lease for it." Equivalent
        to ``exchange_reverse(out_lease)``, popping ``target`` and discarding every
        other entry. Implemented as "clone every via, return the chosen one,
        discard the rest"; ``out_lease`` itself is consumed by the surrounding
        ``take()``, which clears the carried via dict on cleanup.

        Use :meth:`subclone` if you'd like to keep ``out_lease`` alive.

        Raises:
            TypeError: If ``out_lease`` is not an output lease of this Derived.
            KeyError: If ``target`` is not in the input set.
            LeaseDiscardedError / LeaseBrokenError: If ``out_lease`` is inactive.
        """
        with out_lease:
            validated = self._validate_output_lease(out_lease, "subtake")
            if target not in validated._vias:
                raise KeyError(f"subtake: target {target!r} is not an input of {self._name!r}")
            result: Lease[B] | None = None
            try:
                for tok, via in validated._vias.items():
                    if tok is target:
                        result = via.clone()
            except BaseException:
                if result is not None:
                    result.discard()
                raise
            assert result is not None  # target presence checked above
            return result

    def _validate_output_lease(
        self, out_lease: Lease[RB_Out], op: str
    ) -> LocalDerivedTokenLease[RB_Out]:
        """Shared type check for :meth:`exchange_reverse` / :meth:`subclone` / :meth:`subtake`.

        Returns ``out_lease`` narrowed to :class:`LocalDerivedTokenLease` for
        callers that need to read ``_vias``.
        """
        if not isinstance(out_lease, LocalDerivedTokenLease) or out_lease._owner is not self:
            raise TypeError(
                f"{op} received a lease that does not belong to"
                f" the output lifetime of {self._name!r}."
            )
        return out_lease

    def _clone_via[
        B: LifetimeBrand
    ](self, out_lease: LocalDerivedTokenLease[RB_Out], target: LifetimeToken[B], op: str) -> Lease[
        B
    ]:
        """Clone the via lease for ``target`` from a (still-active) output lease."""
        if target not in out_lease._vias:
            raise KeyError(f"{op}: target {target!r} is not an input of {self._name!r}")
        return out_lease._vias[target].clone()

    def revoke(self, reason: str) -> None:
        """Revoke the output lifetime: break all output leases and end the token.

        Idempotent; a no-op if the output has already ended (DOA, input-end cascade,
        or prior revoke). Does not touch input tokens — they're owned externally.
        """
        self._close_output(reason)

    def _on_input_ended(self, token: LifetimeToken[Any]) -> None:
        """Callback fired when any input token ends. Closes the output synchronously."""
        self._close_output(f"input {token} ended")

    def _close_output(self, reason: str) -> None:
        """Shared implementation of the revoke / input-end cascade.

        Breaks every live output lease (firing broken-callbacks synchronously),
        discards their vias, ends the output token, and detaches from any
        still-live inputs.

        **Shared-dict invariant:** ``info.vias`` is the *same dict object* as the
        lease's ``_vias`` (see :class:`LocalDerivedTokenLease` and
        :class:`_DerivedLeaseCleanupInfo`). ``info.vias.clear()`` below therefore
        also empties the lease's ``_vias``, which matters if the now-broken lease
        is ever queried later (e.g. by ``exchange_reverse``, which checks
        ``_vias`` for surviving entries). After this cascade, every live output
        lease has an empty ``_vias`` dict and a ``broken`` state.
        """
        if self._output_token.ended():
            return
        output_tracking = self._output_tracking
        self._output_tracking = {}
        for info, finalizer in output_tracking.values():
            peek = finalizer.peek()
            if peek is not None:
                lease = peek[0]
                lease._mark_broken(reason)
            finalizer.detach()
            # Discard every surviving via, then clear the dict. Because `info.vias`
            # is shared-by-reference with the lease's `_vias`, both sides are emptied
            # in one step — this keeps any late lookup against `_vias` consistent.
            for via in info.vias.values():
                via.discard()
            info.vias.clear()
        # Detach from any still-live input tokens so we stop getting callbacks.
        for deregister in self._input_dereg.values():
            deregister()
        self._input_dereg = {}
        self._output_token._mark_ended()

    async def aclose(self) -> None:
        """Wait for the output token to end (revoking on cancellation).

        Because the output only ends on input-end or ``revoke``, callers are
        expected to drop whatever input they control **before** calling ``aclose``.
        See ``LOCAL_CODE_OVERVIEW.md`` for the common "key ULL + Derived" composition.

        On cancellation, revokes the Derived for a fast close and re-raises.
        Idempotent on an already-ended output token.
        """
        if self._output_token.ended():
            return
        end_event = anyio.Event()
        deregister = self._output_token.register_end_callback(end_event.set)
        try:
            await end_event.wait()
        except BaseException:
            self.revoke("aclose cancelled during drain")
            raise
        finally:
            deregister()

    @contextlib.contextmanager
    def revoke_after_block(self) -> Generator[Self, None, None]:
        """Sync context manager that revokes this Derived on block exit.

        ``with derived.revoke_after_block() as d:`` runs the block with ``d`` live,
        then on any exit (normal or exceptional) calls ``self.revoke("block exit")``.
        Useful for tests and self-contained blocks; does not participate in any
        input drain.
        """
        try:
            yield self
        finally:
            self.revoke("block exit")

    # --- Internal protocol called by owned output leases. ---

    def _validate_owned_lease(self, lease: LocalLifetimeLease[Any]) -> None:
        """Validate that ``lease`` is an output lease of this Derived; else raise.

        Defensive check used by ``_register_lease`` / ``_deregister_lease`` to catch
        bugs where a lease from a different owner accidentally gets routed here.
        """
        grant = lease.grant
        if not isinstance(grant, _DerivedOutputGrant):
            raise RuntimeError(f"Lease {lease!r} does not belong to {self!r}: unrecognized grant.")
        if grant._derived is not self:
            raise RuntimeError(
                f"Output lease {lease!r} belongs to a different DerivedLocalLifetime"
                f" (not {self!r})."
            )

    def _register_lease(self, lease: LocalLifetimeLease[RB_Out]) -> None:
        self._validate_owned_lease(lease)
        if self._output_token.ended():
            raise LeaseBrokenError(
                f"Cannot issue output lease: {self._name!r} output has already ended."
            )
        assert isinstance(lease, LocalDerivedTokenLease)
        lease_id = id(lease)
        # The `vias` field stores the *same* dict object as `lease._vias`; see
        # `LocalDerivedTokenLease` / `_DerivedLeaseCleanupInfo` docstrings for the
        # invariant. Cleanup paths (discard, leak finalizer, input-end cascade) all
        # operate through this shared reference.
        info = _DerivedLeaseCleanupInfo(
            lease_repr=repr(lease),
            alloc_tb=_capture_alloc_tb(),
            vias=lease._vias,
        )
        finalizer = weakref.finalize(lease, _on_lease_leaked, weakref.ref(self), lease_id, info)
        self._output_tracking[lease_id] = (info, finalizer)

    def _set_lease_debug_label(self, lease: LocalLifetimeLease[RB_Out], label: str) -> None:
        """Counterpart of ``UnconditionalLocalLifetime._set_lease_debug_label``
        for output leases (see ``LocalLifetimeLease.set_debug_label``).

        Args:
            lease: The registered output lease to label; unknown/
                deregistered leases are a no-op.
            label: The holder label to record on the tracking info.
        """
        entry = self._output_tracking.get(id(lease))
        if entry is not None:
            info, finalizer = entry
            self._output_tracking[id(lease)] = (
                dataclasses.replace(info, holder_label=label),
                finalizer,
            )

    def _deregister_lease(self, lease: LocalLifetimeLease[RB_Out]) -> None:
        self._validate_owned_lease(lease)
        lease_id = id(lease)
        entry = self._output_tracking.get(lease_id)
        if entry is None:
            return  # idempotent (double-discard or race with finalizer)
        info, finalizer = entry
        finalizer.detach()
        self._clean_up_lease_data(lease_id, info)

    def _clean_up_lease_data(self, lease_id: int, info: _DerivedLeaseCleanupInfo) -> None:
        """Common owner-side cleanup for both the normal discard path and the leak finalizer.

        Removes the tracking entry (if still present) and discards each stored via.
        The output token is NOT closed here — it only closes when an input ends or
        on explicit ``revoke``. When outputs drain while all inputs are live the
        Derived just becomes idle, ready for more forward exchanges.
        """
        self._output_tracking.pop(lease_id, None)
        for via in info.vias.values():
            via.discard()
        info.vias.clear()


# ---------------------------------------------------------------------------
# STATIC_LIFETIME — a global "never-ending" lifetime for process-lifetime data.
# ---------------------------------------------------------------------------


class _PhantomStaticLifetimeBrand(_PhantomLifetimeBrand):
    pass


type StaticBrand = _PhantomStaticLifetimeBrand
"""Brand for the process-global static lifetime.

Distinct from all fresh session brands; exists so typecheckers can still see that a
`Lease[StaticBrand]` is not a `Lease[B]` for some other `B`, even though operationally the
static lease is never broken and can be freely cloned/discarded.
"""


class _StaticLifetimeToken(LifetimeToken[StaticBrand]):
    """A lifetime token that is always active. Exposed via `STATIC_LIFETIME.token`.

    `ended()` returns False forever; `register_end_callback` silently ignores the callback
    (the callback will never fire) and returns a no-op deregister. There is no way to end
    this token.
    """

    __hash__ = object.__hash__

    def __repr__(self) -> str:
        return "<StaticLifetimeToken>"

    def ended(self) -> bool:
        return False

    def register_end_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        # The token never ends, so the callback will never fire. We drop the reference
        # rather than keeping a list, since nothing can ever cause us to call it.
        del callback
        return _noop_deregister


class _StaticGrant(Grant[StaticBrand]):
    """The singleton grant for `STATIC_LIFETIME`. All static leases share this grant.

    Equality and hashing are identity-based via the `Grant` default; there is only one
    instance so this is correct.
    """

    __hash__ = object.__hash__

    def __init__(self, subject: _StaticLifetimeToken):
        self._subject = subject

    def __repr__(self) -> str:
        return "<StaticGrant>"

    @property
    def subject(self) -> LifetimeToken[StaticBrand]:
        return self._subject


class _StaticLease(Lease[StaticBrand]):
    """A lightweight never-breaking lease for `STATIC_LIFETIME`.

    Instances are created by `STATIC_LIFETIME.lease()`. `clone()` returns a new lease
    object sharing the same process-wide grant; `take()` (from `Lease`) transfers
    holdership by discarding the original and returning a fresh clone. `discard()`
    deactivates this handle only. The grant never revokes, so `register_broken_callback`
    is a no-op that returns a no-op deregister.

    This type intentionally does not register with a lease owner or use weakref
    finalizers — it is a small per-handle shell around the static grant.
    """

    def __init__(self, grant: _StaticGrant):
        self._grant = grant
        self._active = True

    def __repr__(self) -> str:
        return "<StaticLease>"

    @property
    def grant(self) -> Grant[StaticBrand]:
        return self._grant

    def is_active(self) -> bool:
        return self._active

    def clone(self) -> Self:
        return type(self)(self._grant)

    def discard(self) -> None:
        self._active = False

    def ensure_active(self) -> None:
        assert self._active

    def register_broken_callback(self, callback: Callable[[str], None]) -> Callable[[], None]:
        del callback
        return _noop_deregister


class _StaticLifetime:
    """The process-global static lifetime.

    Semantics: "this data has no formal lifetime bound — the Python object that owns it is
    the sole gatekeeper, and its owner must keep it alive as long as it could be reached."
    The static lifetime's token never ends, and the static grant is never revoked (leases
    never *break* in the usual sense — no `register_broken_callback` will fire). Each
    `lease()` / `clone()` is a new lightweight handle; discarding one handle only
    deactivates that object. Cloning is cheap (shared grant).

    Useful for arenas / data sources whose lifetime is not bounded by any revokable
    lease — e.g. `TransferEntitySource`, which holds entity bytes received over the wire in a
    plain Python dict. The owner of such an object uses Python-level ownership (holding
    a reference to the object) to bound the lifetime of the data, and calls `close()`
    explicitly when done.

    Note on `lazily_attach_from_arena`: if a memory store registers a static arena with
    `key_lease=STATIC_LIFETIME.lease()`, the store's break-callback will never fire. If the
    underlying static source is `close()`d manually while the claim is still live,
    subsequent loads will fail at load time rather than up-front. Closing a static-backed
    arena is the caller's explicit choice; only do it when you know the arena is no
    longer needed.
    """

    def __init__(self) -> None:
        self._token: _StaticLifetimeToken = _StaticLifetimeToken()
        self._grant: _StaticGrant = _StaticGrant(self._token)

    def __repr__(self) -> str:
        return "<StaticLifetime>"

    @property
    def token(self) -> LifetimeToken[StaticBrand]:
        return self._token

    @property
    def grant(self) -> Grant[StaticBrand]:
        return self._grant

    def lease(self) -> Lease[StaticBrand]:
        """Creates a new lease for the static lifetime."""
        return _StaticLease(self._grant)


STATIC_LIFETIME: _StaticLifetime = _StaticLifetime()
"""The process-global static lifetime singleton. See `_StaticLifetime` for semantics."""
