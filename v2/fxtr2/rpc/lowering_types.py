"""LeasedCapIR lowering protocol version 1

This module describes a protocol for implementing a channel between two stateful LeasedCapIR
objects in terms of a bidirectional stream of discrete messages, where each message is a
serializable value. The protocol does not define how these messages are serialized or framed on the
wire; this is the responsibility of the transport layer.

The particular lowering scheme implemented here is heavily based on the [Cap'n'Web][] JSON message
protocol. However, there are some substantial differences. Most notably:
- LeasedCapIR uses a lease-based ownership model, so many of the lowering messages are specific to
    leases.
- LeasedCapIR explicitly treats IRFutures as first-class values, since it is designed as an
    intermediate representation for a higher-level user-facing representation. Thus, some of
    Cap'n'Web's features for automatically resolving futures
- Cap'n'Web includes additional JavaScript and web-platform-specific features that we choose not to
    support over this protocol.

[Cap'n'Web]: https://github.com/cloudflare/capnweb/blob/main/protocol.md

## Imports and Exports

Each side of a session maintains two tables: imports and exports. One side's exports correspond to
the other side's imports. Imports and exports are assigned sequential numeric IDs. However, in
some cases an ID needs to be chosen by the importing side, and in some cases by the exporting
side. In order to avoid conflicts:

* IDs 0, 1, and 2 are reserved for the "main" interface:
    * ID 0 is the lifetime token for the main interface.
    * ID 1 is an IRHandle for the main interface.
    * ID 2 is a grant giving access to token 0 (and thus, to the main interface).
* When the importing side chooses the ID, it chooses the next positive ID (starting from 3 and
  going up).
* When the exporting side chooses the ID, it chooses the next negative ID (starting from -1 and
  going down).

By convention, when describing the meaning of any RPC message, we always take the perspective of
the sender. So, if a message contains an "import ID", it is an import from the perspective of the
sender, and an export from the perspective of the recipient.

Note that IDs are never reused.

## Push and Pull

An RPC call follows this sequence:

* The client sends the server a `push` message, containing an expression to evaluate.
    * The `push` message implicitly allocates the next three positive IDs in the client's import
        table. The first corresponds to a fresh token representing the lifetime of the call result,
        the second corresponds to an IRFuture representing the result of the call, and the third
        corresponds to a lease granting access to the result of the call.
    * The expression expresses the call to make.
    * Upon receipt, the server evaluates the expression and delivers the call to the appropriate
      remote handle.
* The client may subsequently send the server a `pull` message, specifying the import IDs just
  created by the `push`. This expresses that the client is interested in receiving the result of the
  call as a `resolve` message.
* The client may subsequently refer to the import IDs in pipelined requests.
* When the server is done executing the call, it sends a `resolve` message, specifying the export
  ID of the IRFuture and an expression for its result.
* Upon receiving the resolution, the client no longer needs the import table entry, so sends a
  `discard` message for the ID of the lease.
    * Once the lease is discarded, the server can freely discard its copy of the result value.

Some notes:

* The client does not need to send a `pull` message if it doesn't care to receive the results. In
  practice, if the application never calls `wait()` on the IRFuture, it is never pulled. The
  IRFuture can still be used in pipelining without pulling.
* Technically, the pushed expression can contain any number of calls, including none. A client
  could, for example, push a large data structure containing no calls, and then subsequently make
  multiple calls that use this data structure via pipelining, to avoid having to send the same
  data multiple times.
* If the call throws an exception, the server will send a `reject` message instead of `resolve`.
* `resolve` and `reject` are the same messages used to resolve exported futures, that is, a
  future that was introduced when it was sent as part of some other RPC message. Thus, IRFutures
  created by push expressions and IRFutures directly sent to the server work the same.
* Almost always, the three results of the `push` message will be used together to pull or unwrap
  the IRFuture and gain access to the result. However, we separate them into three distinct IDs
  so that the individual pieces can be referred to independently, via the same expression language
  that is used to refer to them when sent separately.

## Leases, Grants, and Lifetimes

LeasedCapIR uses a lease-based ownership model both internally within one application and across
RPC boundaries, and applications are free to send tokens and leases directly across RPC boundaries.
This is designed to work transparently, so that applications have the same behavior across an
RPC connection as they do locally. However, there is an important difference between local and
RPC operations: for RPC operations, the connection (and lowering layer) operates as a regrantor of
the lease. This means:
- For any lease received through the connection, the connection manager is responsible for managing
    the corresponding grants of those leases, and will revoke the grant and break any extant leases
    if the connection closes unexpectedly.
- For any lease sent through the connection, the connection manager will hold onto _one_ copy of
    that lease and use it to mediate access to the resource from the remote side.
- Revoked grants will _eventually_ flow through the system as broken leases, but due to network
    delays the remote side may have already dispatched operations using a lease that later turns out
    to be broken; in this case any IRFutures gated by these leases may fail with an exception.

Note that leases themselves are used to manage ownership within a single process or side of a
connection, and any lease sent through the connection is considered consumed by the sending side
and received as a fresh lease by the recipient. Because of this, when communicating over a transport
the two sides communicate by assigning unique IDs to _grants_ rather than leases.

## Import/Export Table Types and Bounce-Back Identity

Evaluated expressions for the transport match the set of IRValue types. Such values are "temporary"
and are always sent from one party to the other.

The import and export tables, on the other hand, contain values that both sides can reference over
time. They store the following types of values and assign them integer IDs:
- Tokens: Unique lifetime tokens. These are held on the exporting side, and deduplicated by
    identity.
- Grants: Objects that grant access to some resource. These are stored as a single lease on the
    exporting side, but conceptually can be used to back multiple leases on the receiving side.
    They are assigned an ID the first time the are exported, and deduplicated if another lease
    backed by the same export-side grant is sent through the connection again. New grants are also
    created dynamically each time a new expression is "push"ed. Every grant has an associated
    subject lifetime token; whenever a grant is introduced for the first time, the message carrying
    that introduction also fixes the subject token's ID (either by including it explicitly —
    ``ExportedLeaseExpr.token_export_id`` — or implicitly via adjacent allocations, as in a
    ``PushMessage``).
- IRHandles: Objects that can be dispatched to. These are held by the exporting side until their
    lifetime ends, and deduplicated by identity.
- IRFutures: Objects that represent the result of a computation. These are also held by the
    exporting side until their lifetime ends, and deduplicated by identity. New IRFutures can also
    be created dynamically each time a new expression is "push"ed.

Note that the type of value stored in the table is implicit, and inferred from the messages and
expressions that reference it.

When sending a table-backed value back and forth over a single transport, both sides are required
to preserve object identity relationships. In particular, this means:
- When sending a value back over the transport that was originally received from the transport,
    the value _must_ be referred to by its original import ID (not a fresh export ID)
- When sending a value over the transport that has already been exported, the value _must_ be
    referred to by the same export ID (not a fresh export ID), _even_ if the other side has
    discarded their reference to it in the meantime.
- Wrapped values must respect these equality relationships also; in particular, when sending leases
    over the channel, the lease objects will be cloned and produce fresh objectes, but the _grant_
    must have a stable identity, which compares equal if and only if the two grants refer to the
    same import ID on the same side of the same connection.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Mapping, Sequence

from fxtr2.leases.lease_types import Lease, LifetimeToken
from fxtr2.leases.leased_cap_ir import (
    Call,
    GetItem,
    IRException,
    IRFuture,
    IRHandle,
    IRStatic,
    IRTagged,
    IRValue,
)

####################################################################################################
# Top-level RPC Messages
####################################################################################################

type RPCMessage = (
    HelloMessage
    | PushMessage
    | PullMessage
    | ResolveMessage
    | RejectMessage
    | WatchMessage
    | ClosedMessage
    | BreakMessage
    | DiscardMessage
    | UnpullMessage
    | AbortMessage
)
"""Top-level messages that can be sent over the transport."""


@dataclasses.dataclass(frozen=True)
class HelloMessage:
    """Connection-initiation handshake.

    Both sides of a connection MUST send a ``HelloMessage`` as the very first message they write
    to the transport, and MUST receive a ``HelloMessage`` as the very first message they read from
    the transport, before treating the connection as usable. A peer that reads any other message
    first, or that reads a second ``HelloMessage`` at any point after the first one, MUST
    immediately abort the connection (see ``AbortMessage``), since this indicates that the remote
    side has desynced or is trying to re-initialize an already-live session.

    ``HelloMessage`` carries no fields in this version of the protocol. The serialized form of the
    message is reserved for negotiating wire-format version information in the future; protocol
    implementations that operate directly on ``RPCMessage`` objects do not need to look at its
    contents.

    Sending / receiving a ``HelloMessage`` does not itself introduce any imports, exports, or
    backing resources. The IDs ``0``, ``1``, ``2`` reserved for the main interface are considered
    implicitly introduced by the act of opening a connection, not by the ``HelloMessage``.
    """


@dataclasses.dataclass(frozen=True)
class PushMessage:
    """Asks the recipient to evaluate the given expression.

    This message implicitly allocates the next three sequential import IDs for the sender (in the
    positive direction):
    - the first corresponds to a fresh connection-owned token representing the lifetime of the call
        result,
    - the second corresponds to an IRFuture representing the result of the call,
    - the third corresponds to a grant giving access to the result of the call, backed by the
        recipient's protocol implementation.

    The sender can immediately use the newly allocated import IDs to pull the result of the
    call, or use them in promise pipelining. The recipient will eventually evaluate the expression,
    delivering any calls therein to the appropriate remote handle, and then populate its own export
    table to match the sender's import table.

    Attributes:
        expression: The expression to evaluate.
    """

    expression: Expression[Any]


@dataclasses.dataclass(frozen=True)
class PullMessage:
    """Signals that the sender would like to receive a ``resolve`` message for the resolution of the
    given import, which must refer to an IRFuture. Any import that refers to an IRFuture may be
    pulled. Note however that the remote side is free to preemptively resolve a future without
    waiting for a pull.

    ``grant_import_id`` refers to a grant that gives access to the future. If not provided, it is
    assumed to be equal to ``future_import_id + 1`` (the common case for push expressions).

    Note: ``AwaitFutureExpr`` occurrences the sender includes in its own outbound expressions do
    not act as implicit pulls — the sender uses them as pipelining hints, not as pull requests.
    (Conversely, an ``AwaitFutureExpr`` that the sender *receives* referencing one of its own
    imports does impose a pull obligation on the exporter, because evaluating the expression
    requires the sender to fetch the value.)

    Note that it is possible to pull a future and then immediately discard the grant; this does not
    block the delivery of the results but it does prevent the sender from referring to the future
    again, and allows the recipient to free the associated resources as soon as the result is
    delivered. (The result can also be kept remotely after being discarded, and referenced via
    unwrapping.)

    Suggestion: When serializing PullMessage, the grant_import_id may be elided if it is equal to
    ``future_import_id + 1`` (the common case for push expressions).

    Attributes:
        future_import_id: The import ID of the IRFuture to pull.
        grant_import_id: The import ID of the grant that gives access to the future.
    """

    future_import_id: int
    grant_import_id: int


@dataclasses.dataclass(frozen=True)
class ResolveMessage:
    """Instructs the recipient to evaluate the given expression and then use it as the resolution of
    the given IRFuture export.

    Attributes:
        future_export_id: The export ID of the IRFuture to resolve.
        expression: The expression whose result resolves the future. As a special case, if the
            expression is an `AwaitFutureExpr`, the recipient is encouraged to forward the result of
            the export future to this future rather than directly awaiting it. A recipient that
            directly awaits is still correct, just potentially slower — multi-hop pipelining
            chains (A→B→C) then round-trip intermediate values that could have been forwarded.
            (``LeasedCapRPCConnection`` always forwards in this case, via
            ``LocalPendingResultBox.resolve_from``.)
    """

    future_export_id: int
    expression: Expression[Any]


@dataclasses.dataclass(frozen=True)
class RejectMessage:
    """Instructs the recipient to evaluate the given expression and then use it to reject the given
    IRFuture export.

    The expression must evaluate to a static :class:`IRStatic` value — concretely, a tree
    containing only primitives, ``IRTagged``, ``IRException``, and homogeneous containers of
    the same. In particular it may not contain ``Imported…Expr`` / ``Exported…Expr`` /
    ``AwaitFutureExpr`` / ``HandleRequestExpr`` / ``HandleDispatchExpr`` / ``WalkFutureExpr``
    nodes — rejection payloads are not permitted to carry stubs or trigger async work. The
    value it evaluates to is typically an :class:`IRException`. If the expression cannot be
    statically evaluated, or does not evaluate to an :class:`IRException`, the recipient
    should synthesize a replacement :class:`IRException` describing the malformed payload
    and use that as the rejection.

    Attributes:
        future_export_id: The export ID of the IRFuture to reject.
        expression: The static expression whose result rejects the future.
    """

    future_export_id: int
    expression: Expression[IRException[Any]]


@dataclasses.dataclass(frozen=True)
class WatchMessage:
    """Signals that the sender would like to receive a ``closed`` message when the lifetime token at
    the given import ID is closed.

    Attributes:
        token_import_id: The import ID of the lifetime token to watch.
    """

    token_import_id: int


@dataclasses.dataclass(frozen=True)
class ClosedMessage:
    """Signals that the lifetime token at the given export ID has been closed.

    Attributes:
        token_export_id: The export ID of the closed lifetime token.
    """

    token_export_id: int


@dataclasses.dataclass(frozen=True)
class BreakMessage:
    """Notifies the recipient that any leases referring to the grant at the given export ID have been
    broken unexpectedly. The reason string is a human-readable description of the reason for the
    grant being broken.

    This message MUST be sent when the backing lease for an exported grant breaks if the recipient
    either has outstanding leases to the grant, OR has any pending pulls that used this grant that
    they are still waiting for (e.g. that have not resolved and have not been unpulled), so that the
    recipient can clean up its state. The message may be sent even if the recipient does not have
    any remaining references to the grant, in which case the recipient should simply ignore it.

    Attributes:
        grant_export_id: The export ID of the broken grant.
        reason: A human-readable description of the reason for the break.
    """

    grant_export_id: int
    reason: str


@dataclasses.dataclass(frozen=True)
class DiscardMessage:
    """Instructs the recipient to discard the value (usually a grant) at the given import ID.

    ``refcount`` is the total number of times this import ID has been "introduced" to the sender.
    From the sender's (importer's) perspective, exactly two events increment the refcount for an
    imported ID:

    1. Allocating the import: for a ``PushMessage``, the three implicitly-allocated import IDs
       (token, future, grant) each start at refcount 1 as soon as the push is enqueued. For the
       reserved main-interface IDs (0/1/2), the refcount starts at 1 as soon as the connection
       is opened (no message carries the introduction).
    2. Receiving any ``Exported…Expr`` from the exporter that references this ID — each such
       occurrence inside an incoming message counts as exactly one introduction, regardless of
       how many other references appear in the same message.

    Critically, ``Imported…Expr`` occurrences inside messages the sender writes back out do **not**
    increment the refcount. When the sender sends back a value it originally received from the
    exporter, that reference does not place any new obligation on the exporter to keep the backing
    resource alive (the exporter is free to do as it likes with values it receives back). Only
    exporter-originated introductions need to be tracked.

    The refcount must be sent on discard to avoid a race condition if the exporter has recently
    introduced the same ID again: the exporter remembers how many times it has introduced this ID,
    decrementing by the refcount of any discard messages received, and only actually discards the
    backing data when this counter reaches zero.

    Discarding is primarily important for grants, since it allows discarding the lease backing the
    grant and freeing the associated resources. Implementors of this protocol are required to
    immediately discard the lease backing any grant that has been discarded by this message and has
    not been reintroduced.

    IRHandles and IRFutures can technically be discarded as well to free the associated resources
    even if the lifetime of the object has not ended (e.g. if the recipient has garbage-collected
    all copies of the handle or future and cannot access it anymore). Implementing support for
    discarding of IRHandles and IRFutures is optional, and implementors are free to ignore messages
    for these types. Implementors are also free to preemptively discard IRHandles and IRFutures as
    soon as their lifetime ends, even if no discard message has been sent. (The key difference
    between leases and handles/futures here is that discarding of leases sends an explicit signal to
    the exporter by calling the ``.discard()`` method on the backing lease, whereas discarding a
    handle or future just drops the reference to the object as an optimization.)

    Attributes:
        import_id: The import ID of the value to discard.
        refcount: The total number of times this import ID has been introduced.
    """

    import_id: int
    refcount: int


@dataclasses.dataclass(frozen=True)
class UnpullMessage:
    """Instructs the recipient that the result of pulling a previous IRFuture import is no longer
    needed and does not need to be sent.

    ``refcount`` is the total number of pending obligations the exporter has to deliver a
    resolution for this future, from the sender's (importer's) perspective. Exactly two events
    contribute to this count:

    1. The sender has sent a ``PullMessage`` for this future that has not yet been answered by a
       ``ResolveMessage`` / ``RejectMessage`` / matching ``UnpullMessage``.
    2. The sender has **received** an ``AwaitFutureExpr`` from the exporter referencing this
       future — the only way for the sender to evaluate such an expression is to pull the
       future, so each received occurrence obligates the exporter to deliver a resolution.
       (Specifically, they have received an ``AwaitFutureExpr`` that contains either an
       ``ExportedIRFutureExpr`` or a ``WalkFutureExpr`` directly wrapping an
       ``ExportedIRFutureExpr``.)

    Importantly, ``AwaitFutureExpr`` occurrences the sender writes *out* do **not** contribute
    to this count. An outbound ``AwaitFutureExpr`` is a suggestion to the exporter about how to
    evaluate the surrounding expression (commonly as part of promise pipelining), not a pull
    request — the exporter is free to forward the future or otherwise optimize and is not
    obligated to actually deliver a resolution in response to it.

    The refcount must be sent on unpull to avoid a race condition: the exporter remembers how
    many outstanding resolution obligations it has for this future, and ignores ``UnpullMessage``s
    against newer obligations already represented by more recent messages.

    Unpull messages can be used to indicate that a pending ``wait`` operation has been cancelled and
    the result no longer needs to be sent. This does NOT cancel the computation itself, it only
    cancels the request for the result.

    Messages against a future the exporter has *already resolved* (i.e. the exporter has already
    emitted a :class:`ResolveMessage` or :class:`RejectMessage` for it) are silently ignored —
    the in-flight resolve will reach the importer and discharge everything. Symmetrically, on
    the importer side: once a :class:`ResolveMessage` / :class:`RejectMessage` is received, the
    outstanding obligation refcount is zeroed and the importer stops sending unpulls for that
    future.

    Implementing support for this message is optional; it can safely be ignored.

    Attributes:
        future_import_id: The import ID of the IRFuture to unpull.
        refcount: The total number of times this future has been pulled or unwrapped.
    """

    future_import_id: int
    refcount: int


@dataclasses.dataclass(frozen=True)
class AbortMessage:
    """Indicates that the sender has experienced an error causing it to terminate the session. The
    expression evaluates to the error which caused the abort. No further messages will be received
    and no further messages should be sent.

    The expression must evaluate to a static :class:`IRStatic` value (same restriction as
    :class:`RejectMessage`): no stubs, no async work. If the expression cannot be statically
    evaluated, the recipient should synthesize a replacement :class:`IRException` describing
    the malformed payload and use that as the abort reason.

    Upon sending or receiving this message, both sides should immediately:
    - revoke all grants given to the program that were created from imports on this connection
    - close all tokens given to the program that were created from imports on this connection

    Attributes:
        expression: The static expression evaluating to the error that caused the abort.
    """

    expression: Expression[IRException[Any]]


####################################################################################################
# Expressions
####################################################################################################


class Expression[T: IRValue]:
    """Base class for expressions that can be sent over the transport."""


# ---------------------------------------------------------------------------------------------------
# Basics
# ---------------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class StaticExpr[T: IRStatic](Expression[T]):
    """Evaluates to a static value."""

    value: T


@dataclasses.dataclass(frozen=True)
class ListExpr[T: IRValue](Expression[Sequence[T]]):
    """Evaluates to a list value."""

    elements: tuple[Expression[T], ...]


@dataclasses.dataclass(frozen=True)
class DictExpr[T: IRValue](Expression[Mapping[str, T]]):
    """Evaluates to a dictionary value."""

    items: dict[str, Expression[T]]


@dataclasses.dataclass(frozen=True)
class IRTaggedExpr[T: IRStatic, V: IRValue](Expression[IRTagged[T, V]]):
    """Evaluates to a tagged value.

    Attributes:
        tag: The tag to apply to the value. Should always be static.
        value: The value to tag. Should always be a value.
    """

    tag: T
    value: Expression[V]


# ---------------------------------------------------------------------------------------------------
# Imports and exports
# ---------------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ImportedLifetimeTokenExpr(Expression[LifetimeToken[Any]]):
    """Evaluates to the lifetime token imported by the sender at the given import ID.

    To evaluate the expression, the recipient should use the corresponding token in their export
    table.

    Attributes:
        import_id: The import ID of the lifetime token.
    """

    import_id: int


@dataclasses.dataclass(frozen=True)
class ExportedLifetimeTokenExpr(Expression[LifetimeToken[Any]]):
    """Evaluates to the lifetime token exported by the sender at the given export ID.

    To evaluate the expression, the recipient should construct (or look up) a token proxy that
    refers to the corresponding token in their import table. The recipient must ensure that any
    tokens referring to the same import ID compare equal (bounce-back identity).

    Attributes:
        token_export_id: The export ID of the lifetime token.
    """

    token_export_id: int


@dataclasses.dataclass(frozen=True)
class ImportedLeaseExpr(Expression[Lease[Any]]):
    """Evaluates to a *new* lease backed by the grant imported by the sender at the given import ID.

    To evaluate the expression, the recipient should clone the backing lease for this grant. Every
    instance of this expression evaluates to a fresh lease.

    (Note: ImportedLeaseExpr should only be sent if the sender-side program has presented a valid
    lease to the token re-granted by the sender's connection manager.)

    Attributes:
        grant_import_id: The import ID of the grant that backs the lease.
    """

    grant_import_id: int


@dataclasses.dataclass(frozen=True)
class ExportedLeaseExpr(Expression[Lease[Any]]):
    """Evaluates to a *new* lease backed by a grant exported by the sender at the given export ID.

    To evaluate the expression, the recipient should grant a new lease to this resource (where the
    grant is owned by the recipient's connection manager). Every instance of this expression
    evaluates to a fresh lease.

    ``token_export_id`` pins the subject token of the grant at first introduction — symmetric with
    ``ExportedIRFutureExpr`` / ``ExportedIRHandleExpr``, which also carry the subject token ID of
    the value they introduce. A grant and its subject token share identity across the wire: if the
    recipient has not previously seen this grant export ID, the recipient allocates a fresh import
    entry (introducing ``token_export_id`` as a new token import, if it wasn't already known), and
    records that ID as the grant's ``token_import_id``. If the recipient has already seen the
    grant, the accompanying ``token_export_id`` must match the previously-recorded value — a
    mismatch is a protocol violation and must abort the connection.

    Attributes:
        grant_export_id: The export ID of the grant that backs the lease.
        token_export_id: The export ID of the grant's subject lifetime token. Must match the
            subject token on any subsequent reintroduction of the same grant.
    """

    grant_export_id: int
    token_export_id: int


@dataclasses.dataclass(frozen=True)
class ImportedIRHandleExpr(Expression[IRHandle[Any]]):
    """Evaluates to the IRHandle imported by the sender at the given import ID.

    To evaluate the expression, the recipient should use the corresponding handle in their export
    table.

    Attributes:
        import_id: The import ID of the IRHandle.
    """

    import_id: int


@dataclasses.dataclass(frozen=True)
class ExportedIRHandleExpr(Expression[IRHandle[Any]]):
    """Evaluates to the IRHandle exported by the sender at the given export ID.

    To evaluate the expression, the recipient should construct a new IRHandle object that refers to
    the corresponding handle in their import table. It should ensure that any IRHandles referring
    to the same import ID compare equal.

    Attributes:
        handle_export_id: The export ID of the IRHandle.
        token_export_id: The export ID of the token that tracks the handle's lifetime.
    """

    handle_export_id: int
    token_export_id: int


@dataclasses.dataclass(frozen=True)
class ImportedIRFutureExpr(Expression[IRFuture[Any, Any]]):
    """Evaluates to the IRFuture imported by the sender at the given import ID.

    To evaluate the expression, the recipient should use the corresponding future in their export
    table.

    Attributes:
        import_id: The import ID of the IRFuture.
    """

    import_id: int


@dataclasses.dataclass(frozen=True)
class ExportedIRFutureExpr(Expression[IRFuture[Any, Any]]):
    """Evaluates to the IRFuture exported by the sender at the given export ID.

    To evaluate the expression, the recipient should construct a new IRFuture object that refers to
    the corresponding handle in their import table. It should ensure that any IRFutures referring
    to the same import ID compare equal.

    Attributes:
        future_export_id: The export ID of the IRFuture.
        token_export_id: The export ID of the token that tracks the future's lifetime.
    """

    future_export_id: int
    token_export_id: int


# ---------------------------------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class AwaitFutureExpr[T: IRValue](Expression[T]):
    """Evaluates to the result of waiting for the given future.

    Note: AwaitFutureExpr is used to represent UnwrapForPipelining values.

    Attributes:
        future: The future to unwrap.
        lease: The lease that grants access to the future.
    """

    future: Expression[IRFuture[T, Any]]
    lease: Expression[Lease[Any]]


@dataclasses.dataclass(frozen=True)
class WalkFutureExpr[T: IRValue](Expression[IRFuture[Any, Any]]):
    """Evaluates to the result of walking a path in a future.

    Attributes:
        future: The future to walk.
        attr_path: The path to walk the future on.
    """

    future: Expression[IRFuture[T, Any]]
    attr_path: Sequence[str | int]


@dataclasses.dataclass(frozen=True)
class HandleRequestExpr(Expression[Any]):
    """Evaluates to the result of making a request on the given handle and waiting for it.

    For the purpose of evaluating the expression, HandleRequestExpr waits for the result of any
    request. However, as an optimization, if a "push" message contains _only_ a HandleRequestExpr,
    the recipient is encouraged to use `dispatch` to get a future and then forward the result of
    the top-level push future to the resulting call future. A recipient that directly awaits the
    dispatch result and then fills the top-level push future is still correct, just potentially
    slower. (``LeasedCapRPCConnection`` always forwards in this case, via
    ``LocalPendingResultBox.resolve_from``.)

    If you want to directly construct a future from a request _without_ unwrapping it, you can use
    HandleDispatchExpr instead.

    Note: HandleRequestExpr can be combined with AwaitFutureExpr as the `handle` to pipeline
    requests on futures; this can be used to implement `pipeline_dispatch` for remote IRFutures.

    Attributes:
        handle: The handle to make a request on.
        lease: The lease to grant access to the handle.
        request: The request to make on the handle.
        wait_for: A sequence of futures to wait for before the request should start.
    """

    handle: Expression[IRHandle[Any]]
    lease: Expression[Lease[Any]]
    request: Call[Expression[Any]] | GetItem
    wait_for: Sequence[AwaitFutureExpr[Any]] = ()


@dataclasses.dataclass(frozen=True)
class HandleDispatchExpr(Expression[Any]):
    """Evaluates to the result of dispatching a request on the given handle.

    Specifically, evaluates to a two-element list [dispatch_future, dispatch_lease].

    Note: HandleDispatchExpr can be combined with AwaitFutureExpr as the `handle` to pipeline
    requests on futures; this can be used to implement `pipeline_dispatch` for remote IRFutures.

    Attributes:
        handle: The handle to make a request on.
        lease: The lease to grant access to the handle.
        request: The request to make on the handle.
        wait_for: A sequence of futures to wait for before the request should start.
    """

    handle: Expression[IRHandle[Any]]
    lease: Expression[Lease[Any]]
    request: Call[Expression[Any]] | GetItem
    wait_for: Sequence[AwaitFutureExpr[Any]] = ()
