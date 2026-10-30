"""Null main interface placeholder used by ``LeasedCapRPCConnection.as_client``."""

from __future__ import annotations

from typing import Any

from fxtr2.leases.lease_types import FreshLifetime, Lease, LifetimeBrand, LifetimeToken
from fxtr2.leases.leased_cap_common import discard_leases_in
from fxtr2.leases.leased_cap_ir import (
    Call,
    IRException,
    IRFuture,
    IRHandle,
    IRValue,
    Request,
    UnwrapForPipelining,
)


class NullMainInterface[B: LifetimeBrand](IRHandle[B]):
    """Placeholder main interface exposed by ``LeasedCapRPCConnection.as_client``.

    A client-only connection has no main interface worth exposing — the client is a pure
    consumer of the server's main interface. But the connection's main-export slots (IDs
    0/1/2) still need a valid handle, token, and lease, so we hand them a non-callable
    placeholder. Any inbound request targeting ``MAIN_HANDLE_ID`` fails with an
    :class:`IRException` (``NullMainInterface``) rather than crashing the connection.

    Attributes:
        _token: The lifetime token this handle is bound to. Typically owned by an
            :class:`UnconditionalLocalLifetime` created inside ``as_client`` solely to back
            the placeholder.
    """

    def __init__(self, token: LifetimeToken[B]) -> None:
        self._token = token

    @property
    def token(self) -> LifetimeToken[B]:
        return self._token

    async def request(
        self,
        lease: Lease[B] | UnwrapForPipelining[Lease[B], Any],
        request: Request,
    ) -> IRValue:
        # Discard every lease we consumed per the transfer convention: the
        # top-level ``lease`` and any leases inside ``request.arg``. Leaving
        # them to the weakref finalizer would work, but emitting the
        # ``DiscardMessage``s promptly lets drain complete without waiting
        # for a GC pass. ``WaitAndIgnoreFutureThen`` and ``GetItem`` carry no
        # leases of their own (the former wraps a ``Request`` but in practice
        # arrives already flattened by ``_dispatch_handle_request``).
        # Pipelined-lease form: discard the inner lease; the future is a
        # shared reference we don't own.
        if isinstance(lease, UnwrapForPipelining):
            lease.lease.discard()
        else:
            lease.discard()
        if isinstance(request, Call):
            discard_leases_in(request.arg)
        raise IRException(
            message="client-only connection has no main interface",
            category="NullMainInterface",
        )

    def dispatch[
        RB: FreshLifetime[Any]
    ](
        self,
        lease: Lease[B] | UnwrapForPipelining[Lease[B], Any],
        request: Request,
    ) -> tuple[
        IRFuture[Any, RB], Lease[RB]
    ]:
        # Same rationale as ``request``: discard all consumed leases before
        # raising so the connection isn't left leaking grant imports that
        # would block drain.
        if isinstance(lease, UnwrapForPipelining):
            lease.lease.discard()
        else:
            lease.discard()
        if isinstance(request, Call):
            discard_leases_in(request.arg)
        raise IRException(
            message="client-only connection has no main interface",
            category="NullMainInterface",
        )
