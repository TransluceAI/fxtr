"""Lease-based lifetime management for fxtr.

Defines the base types for fxtr's lease-based capability lifetimes. In fxtr, any resource (such as
a capability to load some data, or a capability to perform some computation) has a lifetime, which
tracks the period of time for which the resource is supposed to be available. These leases are used
to ensure that resources can safely be used for a period of time and then cleaned up safely once
no longer in use, including cases where resources are only accessed via a remote channel.

A holder of a lease is allowed to use the relevant resources for the duration of the lease, and can
also choose to discard the lease to relinquish access. Once all leases to a resource have been
discarded, the resource can be safely reclaimed. Additionally, the _grantor_ of the lease is allowed
to "break" the lease to invalidate it. This revokes all access to the resource and will usually
raise an exception for anyone using the resource. Leases should usually not be broken, but can break
if the grantor hits an unrecoverable error, or if the transport used to connect to the resource is
unexpectedly shut down.

Separating leases from actual resources makes it possible to build more complicated ownership
patterns to coordinate across transport boundaries. As one example, we can construct a "conditional
grant" which grants access to the ability to "upgrade" one lease to another lease, where this
conversion ability is itself mediated by a lease. This can be used for safely exchanging
capabilities bidirectionally with complex cross-boundary dependencies, without having to manage and
release the lifetime of each resource independently. In particular, if party A sends party B a
capability X (e.g. the ability to load some data), party B can _augment_ this capability into a new
capability Y (e.g. the ability to summarize that data) and send this back to A as a conditional
grant, even without holding a persistent lease on the original capability X. Party A is then free to
independently decide when to shut down its own loading-data capability and simultaneously discard
the conditional grant. (Without conditional grants, A would have had to discard its lease on Y,
then _wait_ for B to discard its lease on X, to be sure that this lease was not being used for
any other purposes.) This conditional grant concept ends up also being useful for safe promise
pipelining: callers can reason about their ownership of returned values, and preemptively transfer
the rights to them to others, without having to persistently reserve resources they are not
actually planning to use themselves.

The concept of leases is heavily inspired by the programming patterns of structured concurrency,
wherein the control flow of a program is structured to match the lifetime of asynchronous tasks.
Fxtr's scope system builds on the lease primitive to provide a similar API for working with
external resources and tunneling capabilities over RPC calls: both the grantor and the holder of
a lease can do so inside a defined block of code, and the system will block at the end of the
grantor's scope until all lease-holders exit their own blocks, or break the lease if the grantor
exits the scope due to an exception. Critically, this pattern does _not_ depend on establishing a
specific tunnel between the grantor and the lease-holder, and has the same shape regardless of
whether resources are local or remote.
"""

from __future__ import annotations

import abc
import contextlib
import logging
from typing import Callable, Generator, Hashable, Self

import anyio

logger = logging.getLogger(__name__)


class _PhantomLifetimeBrand:
    """Phantom type for lifetime brands; never instantiated."""

    def __init__(self):
        raise NotImplementedError("Cannot instantiate a lifetime brand directly!")


type LifetimeBrand = _PhantomLifetimeBrand
"""Opaque type for lifetime brands, used for typechecking safety."""

type FreshLifetimeSymbol = str | int
"""Type bound for Literal[{some name}] expressions used as fresh lifetime symbols."""


class _PhantomFreshLifetimeBrand[S: FreshLifetimeSymbol](_PhantomLifetimeBrand):
    """Phantom type for fresh lifetime brands; never instantiated."""

    def _phantom_prove_invariant(self, _: S) -> S:
        """Convince the typechecker that S is invariant."""
        raise NotImplementedError("_phantom_prove_invariant is just for typechecking safety!")


type FreshLifetime[S: FreshLifetimeSymbol] = _PhantomFreshLifetimeBrand[S]
"""A fresh lifetime brand. Used like FreshLifetime[Literal["foo"]] to designate a local lifetime.

FreshLifetime is intended to be used in type annotations as the lifetime of a new token or lease
returned by a function. This makes sure that the new lifetime is branded as distinct from other
lifetimes in scope, and prevents accidental use of incorrect lifetimes. (This is in turn used to
emulate existential types in Python on a best-effort basis, which doesn't otherwise support them.)

Intended usage:
- When accepting an argument that is an unknown lifetime, the caller should annotate the argument
    lifetime as a generic like [..., B: LifetimeBrand], enabling the caller to pass any existing
    lifetime they already have.
- When returning a fresh lifetime, the caller should annotate the result lifetime as a generic
    like [RS: FreshLifetimeSymbol, ...], enabling the caller to specify the specific lifetime name
    that they want to use to refer to the result.
- At each call site where you introduce a new lifetime that does _not_ escape the caller's own
    scope, the _caller_ should annotate the result with FreshLifetime[Literal["some_unique_name"]].
    This ensures that the lifetime is treated as distinct from other lifetimes in that scope.
- By convention, output lifetimes should appear before any lifetimes that could be inferred from
    the argument types.

Example:

```python
def foo[
    RS: FreshLifetimeSymbol,  # <- output lifetime symbol
    B1: LifetimeBrand,        # <- input lifetime brand 1
    B2: LifetimeBrand,        # <- input lifetime brand 2
](
    bar: LifetimeLease[B1],  # <- input lease with arbitrary lifetime brand 1
    baz: LifetimeLease[B2],  # <- input lease with arbitrary lifetime brand 2
) -> tuple[
    Foo[FreshLifetime[RS]],  # <- uses our output lifetime symbol
    LifetimeLease[FreshLifetime[RS]],  # <- uses our output lifetime symbol
]:
    # Create a temporary lifetime token
    fresh1 = UnconditionalLocalLifetime[Literal["fresh1"]]()
    fresh1_lease: LifetimeLease[FreshLifetime[Literal["fresh1"]]] = fresh1.root_lease
    # ... do something with fresh1_lease ...
    # Create an output lifetime token
    fresh2 = UnconditionalLocalLifetime[RS]()
    foo = ...
    return foo, fresh2.root_lease

```
"""


class LeasingError(Exception):
    """Base class for leasing errors."""

    ...


class LeaseBrokenError(LeasingError):
    """Exception raised when attempting to use a resource via a lease that has been broken.

    A lease can become broken if the source of the corresponding capability encounters a
    non-recoverable error, or if the underlying transport stops working or is shut down
    unexpectedly. Leases may also break if explicitly revoked by an authority.
    """


class LeaseDiscardedError(LeasingError):
    """Exception raised when attempting to use a resource via a lease that has been discarded.

    The holder of a lease can discard it to allow backing resources to be safely reclaimed. If a
    caller tries to use a discarded lease, it will raise this exception.
    """


class LeaseTransferError(LeasingError):
    """Exception raised when attempting to use a transfer lease that has already been transferred."""


class LifetimeToken[B: LifetimeBrand](Hashable, abc.ABC):
    """A lifetime token that can be used to coordinate lifetimes of handles and leases.

    Tokens are identity-hashable by default (``__hash__ = object.__hash__``) so callers
    can use them as dict keys without ceremony — for example, the RPC lowering layer's
    re-export checker keys dicts directly by token identity. Subclasses that introduce a
    value-based ``__eq__`` must also provide a consistent ``__hash__``.

    Convention: tokens should only hold **weak references** to mutable state that outlives
    the token's logical lifetime. Using a token as a dict key pins the token itself but
    should not pin large auxiliary state (e.g. lists of end callbacks are owned by the
    token but drained when the token ends, so they don't accumulate). Concrete
    `LocalLifetimeToken` satisfies this naturally: its ``_end_callbacks`` list is
    cleared when the token transitions to ``ended``. The RPC layer's
    ``RemoteLifetimeToken`` is a stateless frozen dataclass whose mutable state lives on
    the connection's import table entry, which the connection owns independently.
    """

    # Identity-based hash. Explicit so that future subclasses which add `__eq__` don't
    # silently disable hashing (Python sets `__hash__ = None` on any class that defines
    # `__eq__` without an accompanying `__hash__`).
    __hash__ = object.__hash__

    @abc.abstractmethod
    def ended(self) -> bool:
        """Check if the lifetime is already known to have ended."""
        ...

    @abc.abstractmethod
    def register_end_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a callback to be called when the lifetime has ended.

        If the lifetime has already ended at the time of this call, the callback is invoked
        synchronously before this method returns. The returned deregister function is a no-op in
        that case.

        Returns:
            A unregister function that can be used to unregister this callback.
        """
        ...

    def _phantom_brand_is_invariant(self, brand: B) -> B:
        raise NotImplementedError


class Grant[B: LifetimeBrand](Hashable, abc.ABC):
    """A specific grant of access to resources with a given lifetime.

    Identifies a particular grant of access to something, which is given out via one or more leases.
    You must hold a lease to access the granted resource.

    Grants can be safely revoked once there are no outstanding leases that depend on this grant.
    They can also be revoked early, in which case any active leases that are backed by this grant
    will break.

    The main use of a grant object is to check if two leases are equivalent. Two leases are clones
    of each other iff their grants compare equal; implementations must provide `__eq__`
    and `__hash__` consistent with this guarantee. Two patterns are common and both satisfy it:
    (a) pure identity (``LocalGrant``) — ``__hash__ = object.__hash__`` and default ``__eq__``;
    (b) structural value equality (``_AttachedOutputGrant``) — a frozen dataclass whose
    auto-generated ``__eq__`` / ``__hash__`` derive from its fields.

    Convention: grants should only hold **weak references** to mutable state. Using a grant as
    a dict key (e.g. the RPC layer's re-export checker) should not pin heavy auxiliary state.
    The default identity-hash below is provided so that subclasses that don't want
    value-equality can inherit it without having to opt in explicitly; structural-equality
    subclasses (frozen dataclasses) override it implicitly via the dataclass decorator.
    """

    # Default identity-based hash. Overridden by frozen-dataclass subclasses (which supply
    # value-based equality + hash automatically). Making it explicit keeps the base class
    # concrete in that respect rather than abstract-via-Hashable.
    __hash__ = object.__hash__

    @property
    @abc.abstractmethod
    def subject(self) -> LifetimeToken[B]:
        """Get the subject of this grant."""
        ...


class Lease[B: LifetimeBrand](anyio.ContextManagerMixin, abc.ABC):
    """A lease that grants access to a resource for a limited period of time.

    By convention, leases are considered _transferred_ when passed as an argument to a function.
    If you want to give someone access to a fresh lease, you should clone it when passing it as an
    argument. If you take a lease as an argument, you should generally call `.take` on it to ensure
    your copy cannot be mutated by the caller if they forgot to use `.clone`.

    Similarly, if a lease is returned, it is considered transferred to the caller, and should not
    be used by the caller afterward.

    Common pattern: use a with-statement like:

        ```python
        def do_something(lease: Lease[T]):
            with lease.take() as lease:
                # use the lease
                bar(lease.clone())  # <- clone and give a copy to bar
                baz(lease.clone())  # <- clone and give a copy to baz
                ...
            # lease is cleaned up here automatically
        ```

    """

    @property
    @abc.abstractmethod
    def grant(self) -> Grant[B]:
        """Get the grant that backs this lease.

        All clones of a lease share the same grant. The grant can be used as a key to identify the
        equivalence class of leases that are clones of each other.
        """
        ...

    @property
    def subject(self) -> LifetimeToken[B]:
        """Get the subject of this lease."""
        return self.grant.subject

    @abc.abstractmethod
    def is_active(self) -> bool:
        """Check if the lease is still (believed to be) active."""
        ...

    @abc.abstractmethod
    def clone(self) -> Self:
        """Create a fresh lease that is a copy of this lease.

        The new lease will grant access to the same resource (and have an equal .grant attribute),
        but can be discarded independently.
        """
        ...

    @abc.abstractmethod
    def discard(self) -> None:
        """Discard the lease.

        Must be idempotent and safe to call even if the lease is already discarded or broken.
        """
        ...

    @abc.abstractmethod
    def ensure_active(self) -> None:
        """Ensure the lease is still (believed to be) active, or raise a LeasingError.

        Note that in some cases a lease may be reported as active even though any operation with
        the lease will fail with a LeaseBrokenError. This occurs in cases where the grant has
        been revoked remotely but we have not yet received a notification. Conceptually, from the
        perspective of this program, the lease breaks at the moment we learn that it has been
        revoked, by either receiving a notification or trying and failing to use the lease in an
        operation.
        """
        ...

    @abc.abstractmethod
    def register_broken_callback(self, callback: Callable[[str], None]) -> Callable[[], None]:
        """Register a callback to be called when the lease has been broken.

        Note that the only way to subscribe to a grant being revoked is to have an open lease to it.
        If a grant has no outstanding leases, it is considered inaccessible and so its status is
        irrelevant.

        If the lease is already known to be broken at the time of this call, the callback is
        invoked synchronously before this method returns, so that subscribers never miss a break
        event.

        Returns:
            A unregister function that can be used to unregister this callback.
        """
        ...

    def take(self) -> Self:
        """Take ownership of the lease.

        This will discard the lease and return a fresh copy of it.
        """
        result = self.clone()
        self.discard()
        return result

    @contextlib.contextmanager
    def __contextmanager__(self) -> Generator[Self, None, None]:
        """Ensure that the lease is active during a context, and close it afterward.

        Note: This checks that the lease is active before entering the context, and discards the
        lease after the context exits. However, it does not immediately cancel tasks if the lease
        breaks while the context is active, under the assumption that the error would surface at
        the point where the lease is used. If you want to cancel tasks when the lease breaks, use
        the `lease_cancel_scope` helper instead.

        For extra safety, consider using a pattern like:

        ```python
        with lease.take() as lease:  # <- replace the original lease with a fresh copy
            # use the lease
            bar(lease.clone())  # <- clone and give a copy to bar
            baz(lease.clone())  # <- clone and give a copy to baz
            ...
        # lease is cleaned up here automatically
        ```
        """
        self.ensure_active()
        try:
            yield self
        finally:
            self.discard()
