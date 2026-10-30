"""Exception types for the LeasedCapIR RPC connection layer.

These are the only exceptions raised by ``LeasedCapRPCConnection`` that are part of
its public API. All three inherit from :class:`fxtr2.leases.lease_types.LeasingError`
so that code already catching lease-level failures surfaces RPC failures uniformly.
"""

from __future__ import annotations

from fxtr2.leases.lease_types import LeasingError


class RPCConnectionError(LeasingError):
    """Base class for errors surfaced by :class:`LeasedCapRPCConnection`.

    Inherits from :class:`LeasingError` so that code catching lease-level failures surfaces RPC
    failures uniformly. Specific subclasses distinguish handshake failure from mid-session abort.
    """


class HandshakeError(RPCConnectionError):
    """Raised when the connection's initial handshake fails.

    Surfaced in either of two situations:
    - The peer's very first message is not a :class:`HelloMessage` (the connection aborts
      immediately and this error is raised on the setup path).
    - The peer sends a second :class:`HelloMessage` at any later point during the session,
      which indicates the remote has desynced or tried to re-initialize. The connection
      aborts and every imported grant is revoked with this as the reason.
    """


class RPCConnectionAborted(RPCConnectionError):
    """Raised locally when the connection has been aborted.

    Aborts happen when either side sends an :class:`AbortMessage`, when the transport breaks,
    when the reader or writer task encounters an unrecoverable internal error, or when the
    handshake fails. After abort, all imported grants are revoked and all imported tokens are
    closed; any further attempt to use an imported value surfaces as a broken lease or a
    closed token.
    """
