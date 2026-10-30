"""Lease-based capability-passing intermediate representation for fxtr.

LeasedCapIR defines an abstract system of "leased capabilities", which decompose access to remote
(or local) objects into a combination of
- a _handle_, which identifies which object is being accessed, and
- a _lease_, which determines the scope of access to the object.

LeasedCapIR is an _intermediate representation_, rather than a protocol. In particular, it operates
on a level of abstraction between the application logic and the transport layer. The application
logic defines the semantics of the leased capabilities, and the transport layer is responsible for
encoding and decoding the leased capabilities into a format that can be sent over the wire. The
data model of LeasedCapIR specifically includes primitive leaf values, containers, RPC handles
and leases, and a special "tag" type that can be used to attach semantics to data, inspired by CBOR
tags.

LeasedCapIR is designed to admit a similar wire protocol as [Cap'n'Web][], and has similar
primitives for promise pipelining, but does not include hard-coded JavaScript-specific features,
instead relying on "tag" types to encode domain-specific semantics. See fxtr.rpc for concrete
wire protocol implementations.

[Cap'n'Web]: https://github.com/cloudflare/capnweb/blob/main/protocol.md

The IR contains the following types:
- Primitive values: transport-dependent, but usually at least numbers (including infinite), strings,
    booleans, and null/None, and sometimes also bytes.
- Lists/arrays: sequences of values
- Mappings/objects/dicts: key-value pairs with string keys
- Tags: containers that wrap another IR value with a tag object, which is an application-defined
    marker telling the application how to interpret the contained value. Can be used to e.g. attach
    type information or decoding instructions to contained values (e.g. dates, streams, etc.).
- Handles: objects that support async attribute access and/or method calls, when granted via a
    valid lease.
- Leases: objects that grant and mediate access to handles for a limited period of time.
- Lifetime tokens: unique symbols that coordinate lifetimes of handles and leases.
- Futures: a container for an "owned" IRValue tree which may not be available immediately
    due to an aysnc computation or remote execution, and which may fail. If it resolves to an
    IRValue tree, it retains ownership of that tree, in the sense that it keeps its own copy of
    all handles, futures, and containers inside itself, and discards them when it itself is
    discarded. Futures are a somewhat low-level primitive intended for use in pipelining.
- Exceptions: objects that indicate errors at runtime. These are normal values and can be sent over
    the transport like any other value. However, they can also be raised as exceptions inside a
    computation. This exception will be surfaced to the application on the `request` or `wait`
    operation for the value, instead of returning a concrete result. Note that
    exception types and data may not be round-trip convertable across transports: when sending an
    exception over a transport, you may receive a different exception on the other end.

The design goal is for the system to feel like a "serialization format" for functions and stateful
objects, rather than as a client/server API. Just as you can take Python data, serialize it
to JSON, send the JSON over a unidirectional channel possibly over many network hops, and then
deserialize the JSON into a TypeScript object (for example) on the other end, you should also be
able to take an _implementation of an interface_ in Python, "serialize" it to the IR, send the IR
over a *bidirectional* channel possibly over many network hops, and then deserialize the IR into an
_handle that implements the interface_ in TypeScript (for example) on the other end. The code that
performs the serialization/deserialization needs to know about the application logic, but does NOT
need to know about or be coupled to the details of the transport layer, and we can use the same
serialization format for both local and remote operations.

Ownership rules: A goal of LeasedCapIR is to make local and remote operations behave identically in
terms of ownership and lifetime management. To this end, the system enforces that IRValue trees are
_copied_ or _transferred_ whenever they cross any RPC object method boundary, which avoids
accidental "aliasing" of mutable containers and simplifies lifetime management. This specifically
affects mutable containers as well as lease objects. In particular:
- When dispatching a call to a RPCHandle:
    - All lease arguments are transferred at the point of dispatch (along with target lease if
        necessary), decoupling the lifetime of any leases inside the argument from their original
        source. The function is always called with an argument whose lifetime is independent of the
        argument at dispatch time, and the caller is free to immediately discard or mutate the
        argument they used for dispatch.
    - Mutable collections in arguments are copied at the point of dispatch, so that modifications
        of them do not affect the original argument.
    - The return value must be copied at the point of return, decoupling the lifetime of any leases
        inside the return value from the function itself. There is no way for the function to
        mutate or discard the returned value after it returns to the caller.
        - (This is technically stricter than Cap'n'Web's implementation for local targets, which in
            some cases delays the deep copy until the value is requested or sent over the wire.)
    - The dispatch mechanism is responsible for ensuring that leases in the argument and return
        value do not leak. (Recommended approach, based on Cap'n'Web: discard all leases inside
        the function argument and return value after the function returns.)
- The internal value of an IRFuture is owned by the future value. Discarding the lease on a future
    value implicitly releases all leases inside it. Conversely, `wait`ing a future
    value returns a _copy_ of the resolved value, so that the caller cannot mutate the original
    version.
    - (This is also stricter than Cap'n'Web's ownership rules, because pulling a Cap'n'Web promise
        does not produce a copy but instead directly returns the resolved value, and also in some
        cases doing method-chaining on a promise that was created _locally_ may temporarily expose
        mutable state between original return value, pulled result, and chained ops.)

Note that in LeasedCapIR, handles are stable and never need to be cloned, and can always be compared
by equality; there will only ever be one handle per object. If multiple independent code paths need
access to the same handle, they should generally pass a single handle along with copies of its
_lease_. This is a distinction from Cap'n'Web, where each RPC handle is self-authorizing and thus
has an independent lifetime, requiring it to be cloned to be passed to independent code paths.

Parts of LeasedCapIR were loosely inspired by the GhostCell API for Rust, and more broadly by
the use of branded types for lifetime management in functional programming languages.
https://plv.mpi-sws.org/rustbelt/ghostcell/
"""

from __future__ import annotations

import abc
import dataclasses
from typing import Any, Mapping, Sequence

from fxtr2.leases.lease_types import FreshLifetime, Lease, LifetimeBrand, LifetimeToken

type IRPrimitive = None | int | float | str | bool | bytes

type IRValue = (
    # Primitive values
    IRPrimitive
    # Containers
    | Sequence[IRValue]
    | Mapping[str, IRValue]
    | IRTagged[IRStatic, IRValue]
    | IRException[IRStatic]
    # Lifetime management
    | LifetimeToken[Any]
    | Lease[Any]
    # Handles and futures
    | IRHandle[Any]
    | IRFuture[IRValue, Any]
)
"""Allowed types in the capability-passing IR.

A design goal of the system is that transports and conversion logic should be transparent and
automatic: it should be possible to send or receive _any_ value that conforms to the IRValue
type over a transport that handles IRValue, or mix-and-match values over multiple transports, or
combine local and remote values, without needing to think about the fact that some of the values
are remote. It should also be possible to encode application-specific data via IRTagged values,
similar to CBOR tags.
"""

type IRStatic = (
    IRPrimitive
    | Sequence[IRStatic]
    | Mapping[str, IRStatic]
    | IRTagged[IRStatic, IRStatic]
    | IRException[IRStatic]
)
"""Static / serializable values in the capability-passing IR for a given leaf type.

Static values do not contain any runtime state or capabilities.
"""


@dataclasses.dataclass(frozen=True)
class IRTagged[T, V]:
    """A tagged value that wraps another IR value with a tag object.

    Tagged values can be used to attach application-defined semantics to other IR values, e.g. by
    identifying them as having a specific format or instructing the application on the expected
    schema. Applications should use the `tag` field to determine how to interpret the `value` field.
    When tags nest, outer tags should specify how to interpret inner tags (e.g. it is allowed for
    an outer tag to indicate that it is "quoting" the inner tag, or to indicate that the inner tag
    should be handled as normal). Usually, tags should be handled by the application via some sort
    of recursive decoding or encoding logic.

    Tagged values may also be used to convert between IRValue types with different leaf types.
    Conversion logic can walk the IR and apply encoding or decoding logic based on the tag as an
    escape mechanism. This requires both sides of the transport to use the same conversion tag
    scheme.

    RPC tags are inspired by the tag mechanism in CBOR. Applications are allowed to use the CBOR
    tag numbers to send CBOR-encodable values, and CBOR transports may use CBOR tags to mark RPC
    tag values.

    When used with an RPC transport, usually a subtype of IRTagged[IRStatic, IRValue].

    Args:
        tag: The tag object to attach to the value. Usually a primitive value for the transport,
            generally either a string or an integer.
        value: The value to wrap with the tag.
    """

    tag: T
    value: V


@dataclasses.dataclass(frozen=True)
class UnwrapForPipelining[T: IRValue, B: LifetimeBrand]:
    """Indicates a future that should be sent and unwrapped before the call is actuall invoked.

    UnwrapForPipelining bundles together a future with its lease, which enables the recipient to
    retrieve the resolved value when needed. Implementations are required to treat an
    UnwrapForPipelining as equivalent to the resolved value of the future, except for the purpose
    of "unobservable" optimizations or logging outputs that do not affect the observable behavior
    of the call or its result. Usually, an implementation of IRHandle should simply unwrap the
    future before passing it to the underlying business logic.

    UnwrapForPipelining is a frozen dataclass with two fields (`future` and `lease`). Construct it
    either as ``UnwrapForPipelining(future, lease)`` or by spreading a dispatch result:
    ``UnwrapForPipelining(*handle.dispatch(lease, request))``. The `.pair` property is retained
    for callers that want the tuple form.

    Use sites that accept ``UnwrapForPipelining`` directly (i.e. without going through call-arg
    substitution) include:

    - call args of `IRHandle.dispatch` / `IRHandle.request` — the implementation's
      `_unwrap_request_futures` resolves them at the dispatch boundary;
    - the outer `lease` of `IRHandle.dispatch` / `IRHandle.request` — the implementation
      resolves the wrapper inside the dispatch task before invoking the underlying call;
    - the `inner_handle_lease` of `IRFuture.pipeline_dispatch` — same pattern, applied
      to the lease used for the inner pipelined call.
    """

    future: IRFuture[T, B]
    lease: Lease[B]

    @property
    def pair(self) -> tuple[IRFuture[T, B], Lease[B]]:
        """The (future, lease) tuple. Equivalent to ``(self.future, self.lease)``."""
        return (self.future, self.lease)

    def dispatch[
        RB: FreshLifetime[Any], IB: LifetimeBrand
    ](
        self: UnwrapForPipelining[IRHandle[IB], B],
        lease: Lease[IB] | UnwrapForPipelining[Lease[IB], Any],
        request: Request,
    ) -> tuple[IRFuture[Any, RB], Lease[RB]]:
        """Dispatch a request without waiting for the result.

        This is a shorthand for invoking `pipeline_dispatch` with the attached future and lease.
        """
        return self.future.pipeline_dispatch(self.lease, lease, request)


type IRCallArg = (
    IRValue
    | Lease[Any]
    | IRTagged[IRStatic, IRCallArg]
    | Sequence[IRCallArg]
    | Mapping[str, IRCallArg]
    | UnwrapForPipelining[IRValue, Any]
)
"""An argument to a call. Extends IRValue with the ability to unwrap futures.

Future-unwrapping is a convenience, which allows the caller to immediately dispatch a call on a
handle before waiting for the argument to complete.
"""


@dataclasses.dataclass(frozen=True)
class Call[Arg]:
    """A request to call a method on an object."""

    method: str | int | Sequence[str | int]
    arg: Arg


@dataclasses.dataclass(frozen=True)
class GetItem:
    """A request to look up an item or attribute on an object."""

    attr: str | int | Sequence[str | int]


@dataclasses.dataclass(frozen=True)
class WaitAndIgnoreFutureThen:
    """A request to wait for a future to resolve, discard the result, then dispatch a sub-request.

    The behavior of dispatching with `WaitAndIgnoreFutureThen(future, request)` must always be
    equivalent to the behavior of waiting for the future to resolve and then dispatching `request`.
    """

    wait_for: UnwrapForPipelining[Any, Any] | Sequence[UnwrapForPipelining[Any, Any]]
    then: Request


type Request = Call[IRCallArg] | GetItem | WaitAndIgnoreFutureThen


class IRHandle[B: LifetimeBrand](abc.ABC):
    """A handle to a remote (or local) object that supports IR calls and attribute access."""

    @property
    @abc.abstractmethod
    def token(self) -> LifetimeToken[B]:
        """The lifetime token corresponding to the lifetime of the handle."""
        ...

    @abc.abstractmethod
    async def request(
        self,
        lease: Lease[B] | UnwrapForPipelining[Lease[B], Any],
        request: Request,
    ) -> IRValue:
        """Dispatch a request and wait for the result.

        This is a blocking operation that dispatches a request and waits for the result. Because it
        waits for the result to complete, the result cannot be pipelined or accessed in any way
        until the operation completes.

        Ownership: implementations of `request` take ownership of `lease` (by calling
        `lease.take()` on entry) and of any leases inside `request`, discarding them before
        returning. Callers must not use `lease` or any lease inside `request` after calling this
        method — pass `lease.clone()` or prepare clones inside the request if you need to retain
        access. If `lease` is an `UnwrapForPipelining[Lease[B], Any]`, the implementation
        takes ownership of the inner `lease.lease` slot and resolves the inner future inside
        the dispatch task before invoking the underlying call; the future itself is not
        owned (it's a shared reference).

        Args:
            lease: A lease that grants access to this object, or an
                `UnwrapForPipelining[Lease[B], Any]` whose resolved value is such a lease.
                In either case, the lease will be transferred and kept alive for the duration
                of the call, and will be discarded when the call completes.
            request: A request to either call a method or look up an attribute on the object. As
                with `dispatch`, the argument will be transferred at the time of dispatch and can be
                be discarded or mutated without affecting the dispatched execution result.

        Returns:
            The result of the call. May instead raise an exception if the call fails.
        """
        ...

    @abc.abstractmethod
    def dispatch[
        RB: FreshLifetime[Any]
    ](
        self,
        lease: Lease[B] | UnwrapForPipelining[Lease[B], Any],
        request: Request,
    ) -> tuple[
        IRFuture[Any, RB], Lease[RB]
    ]:
        """Dispatch a request without waiting for the result.

        Invoking `dispatch` immediately dispatches the request, but does not block waiting for the
        result. Note that all leases passed to dispatch (including the root lease as well as leases
        inside arguments) will be transferred and kept alive for the duration of the call, and all
        mutable objects in the request will be copied. As soon as this method returns, it is safe
        to discard the original objects without affecting the execution result. (Note that the
        callee may also clone any leases passed in as arguments and retain them for itself.)

        The root lease, and any leases inside arguments, will be discarded when the call completes.
        The result of the call (and any leases inside it) will be encapsulated into a new lifetime
        and will remain available via the returned lease until released. (Note that the return
        value may include new handles with the same lifetime as `self`. If so, you will need to
        firt unwrap the future using the returned lease, then use a fresh copy of the input lease
        to actually invoke the handle.)

        Args:
            lease: A lease that grants access to this object, or an
                `UnwrapForPipelining[Lease[B], Any]` whose resolved value is such a lease.
                In either case, the lease will be transferred and kept alive for the duration
                of the call, and will be discarded when the call completes. Pipelined-lease
                callers don't need to wait for the lease to be available before invoking
                `dispatch` — the implementation resolves the wrapper inside the dispatch
                task before invoking the underlying call.
            request: A request to either call a method or look up an attribute on the object. If
                `Call`, the argument is an argument to call the object with; if it contains
                UnwrapFuture values, the implementation should wait for them to resolve and treat
                them as equivalent to the resolved values; this means callers can pipeline calls by
                passing IRFuture values from previous calls to new calls. The argument will be
                transferred at the time of dispatch; once `dispatch` returns the caller is free to
                discard or mutate the argument without affecting the dispatched execution result.
                If `GetItem`, the argument is an attribute path to look up.

        Returns:
            A future representing the result of the call, along with a lease that ensures that the
            result will remain available until retrieved or discarded. If you would like to pipeline
            a request on a future, consider wrapping in `UnwrapForPipelining`.

        Ownership: same as `request` — implementations take ownership of `lease` and any leases
        inside `request` (calling `.take()` on entry) and discard them before the dispatch returns.
        For an `UnwrapForPipelining` outer lease, ownership of the inner `lease.lease` slot is
        taken at entry and the inner future's resolution-lease is consumed when the future is
        awaited inside the dispatch task; the future itself is a shared reference, not owned.
        Callers should pass clones of any leases they wish to retain.

        Note on call ordering:
            The ordering of concurrent calls dispatched to the same handle depends on the
            transport. Callers should not assume that calls arrive in the order they were
            dispatched unless the transport explicitly guarantees this. To enforce sequential
            execution, either wait for each call to complete before dispatching the next, or
            use WaitAndIgnoreFutureThen to create an explicit data dependency.
        """
        ...


class IRFuture[T: IRValue, B: LifetimeBrand]:
    """A reference to an IRValue that will become available in the future.

    IRFutures are returned by the `dispatch` method on an IRHandle, and represent a pointer to the
    result of an operation running asynchronously. Their main purpose is to allow pipelining calls
    across asynchronous operations without waiting for network round-trips.

    Note that if you receive a IRFuture value, this means the computation that produced it has
    already been dispatched; further operations on the IRFuture do not affect the computation
    being performed.

    IRFutures are _owning_ references, in the sense that (a) once they resolve, they are
    immutable and cannot be modified in place by any valid operation (so, in particular, they
    cannot be mutated by the producer or any consumer after they resolve) and (b) they control the
    lifetime of every lease inside them, and ensure they live as long as the future's own token.
    Waiting on an IRFuture returns a _copy_ of the resolved value, which is owned by the
    caller. Pipelining calls on futures will pipeline off of the owned copy of the value once it
    resolves.

    IRFutures may fail. If an IRFuture fails, it and every operation depending on it will also fail.
    Failures will be surfaced when `wait` is called. Note that if a sequence of
    pipelined calls is performed, failure on any of the pipelined calls will only be surfaced when
    awaiting the final result. It is recommended to always wait the final result of a computation to
    make sure it succeeds.

    Type parameter T indicates the type of the final value that will be resolved, if known. Type
    parameter B is the lifetime brand of the future.
    """

    @property
    @abc.abstractmethod
    def token(self) -> LifetimeToken[B]:
        """The lifetime token corresponding to the lifetime of the future."""
        ...

    @abc.abstractmethod
    async def wait(self, lease: Lease[B]) -> T:
        """Wait for the future to resolve and return the resolved value.

        This is a blocking operation that waits for the future to resolve. It requires a lease that
        grants access to the future's lifetime.

        Note that if an IRFuture is never `wait`ed, it may never be sent back over the transport.

        If the lease for the future breaks during the operation, the `wait` operation may fail with
        a LeaseBrokenError. This is distinct from the IRFuture itself being rejected with an
        IRException (which indicates that some error occured _inside_ the future's computation).

        Args:
            lease: A lease that grants access to the future's lifetime. The lease will be
                transferred and kept alive for the duration of the wait, and will be discarded when=
                the wait completes.

        Returns:
            A copy of the resolved value, which is owned by the caller.
        """
        ...

    @abc.abstractmethod
    def walk(self, attr_path: str | int | Sequence[str | int]) -> IRFuture[Any, B]:
        """Walk to a sub-object of the future, treating it as a future with the same lifetime.

        Convenience: a bare ``str`` or ``int`` is treated as a single-step path (so
        ``future.walk("x")`` is equivalent to ``future.walk(("x",))``). A ``str`` is always a
        single step, never a sequence of characters — to walk multiple string steps, pass a
        list or tuple.

        Args:
            attr_path: Either a single attribute/index lookup, or a sequence of them.

        Returns:
            A future representing the result of the walk.
        """
        ...

    @abc.abstractmethod
    def pipeline_dispatch[
        RB: FreshLifetime[Any]
    ](
        self: IRFuture[IRHandle[Any], B],
        future_lease: Lease[B],
        inner_handle_lease: Lease[Any] | UnwrapForPipelining[Lease[Any], Any],
        request: Request,
    ) -> tuple[IRFuture[Any, RB], Lease[RB]]:
        """Pipeline a request on a future that resolves to an IRHandle.

        Invoking `pipeline_dispatch` is equivalent to first waiting for `self` (and possibly
        `inner_handle_lease`) to resolve, asserting that it resolved to an IRHandle, then
        dispatching the request on that handle. Similar to `dispatch`, invoking `pipeline_dispatch`
        immediately dispatches the request, takes ownership of all leases, and copies all mutable
        collections in the request, but does not block waiting for the result.

        Args:
            future_lease: A lease that grants access to this future's lifetime.
            inner_handle_lease: A lease that grants access to the inner handle's lifetime, or an
                future that will be unwrapped to construct the lease.
            request: A request to either call a method or look up an attribute on the object.

        Returns:
            A future representing the result of the call, along with a lease that ensures that the
            result will remain available until retrieved or discarded.

        Note on call ordering:
            The ordering of concurrent calls dispatched to the same handle depends on the
            transport. Callers should not assume that calls arrive in the order they were
            dispatched unless the transport explicitly guarantees this. To enforce sequential
            execution, either wait for each call to complete before dispatching the next, or
            use WaitAndIgnoreFutureThen to create an explicit data dependency.
        """
        ...

    # TODO(daniel): Consider adding Cap'n'Web's "remap" operation, to allow callers to transform the
    # results of an IRFuture. (This could be a sync fn with a fresh lease, and must return an
    # IRCallArg.)


class IRException[D](Exception):
    """An exception that can be sent as an IRValue.

    If application code throws an IRException, it will be propagated to the caller of the function
    even across RPC boundaries along with its data.

    Uncaught exceptions of other types will be wrapped in an IRException before being propagated to
    the caller.

    Args:
        message: The message of the exception. Accessible as `.message` on the instance.
        category: The category of the exception. Intended to be human-readable and not necessarily
            unique.
        data: Any data associated with the exception. May be used to provide additional context.
        traceback_info: A single formatted-traceback string carrying the chain of prior wire hops
            (oldest at the most-deeply-quoted level). At each new hop — wrap-time via
            :func:`wrap_exception`, or wire-encode-time in
            :func:`fxtr2.rpc.json_serdes.encode_static` — the new hop's frames are *prepended*
            to the existing string and the previous content is line-quoted with ``"> "`` plus a
            ``Earlier (pre-encoding) version:`` separator. Repeated hops therefore stack
            progressively-deeper ``>`` prefixes on the older content, making the nesting depth
            visually obvious. ``None`` means "no recorded chain" (the exception was constructed
            without a Python ``__traceback__`` and hasn't crossed a wire boundary yet).

    `str(exc)` formats as ``"[category] message"`` when a category is present, or just
    ``"message"`` otherwise — so callers wrapping a non-IR exception can simply pass the
    plain message and category separately and get a reasonable composed display without
    manually duplicating the category into the message. When ``traceback_info`` is non-None,
    it is appended to the display on a fresh line.
    """

    def __init__(
        self,
        message: str,
        category: str | None = None,
        data: D | None = None,
        traceback_info: str | None = None,
    ):
        super().__init__(message)
        # Keep the raw message accessible for round-trip copies (see `copy_ir_value`). `str(self)`
        # returns the category-prefixed display form; use `.message` to recover the raw text.
        self.message = message
        self.category = category
        self.data = data
        self.traceback_info = traceback_info

    def __str__(self) -> str:
        base = f"[{self.category}] {self.message}" if self.category is not None else self.message
        if self.traceback_info is None:
            return base
        return f"{base}\n{self.traceback_info}"


_PRE_WRAP_TRACEBACK_HEADER = "Earlier (non-IRException) version of this exception:"
"""Header line marking the boundary at which a non-``IRException`` Python exception was
wrapped into an ``IRException`` via :func:`fxtr2.leases.leased_cap_common.wrap_exception`.
Set once, at wrap time, with the original exception's ``traceback.format_exception``
output ``>``-quoted below — so when the wrapped exception is later rendered via
``__str__`` or ``format_exception``, the original is unambiguously a "previous" exception
sitting one quote level deeper, never mistaken for a continuation of the IRException's
own frames."""


_PRE_RPC_TRACEBACK_HEADER = "Earlier (pre-RPC) version of this exception:"
"""Header line marking the boundary at which the IRException crossed an encode/decode
boundary (typically the JSON wire codec; see :func:`fxtr2.rpc.json_serdes.encode_static`).
Each encode-time hop adds another ``Earlier (pre-RPC)`` block above the prior chain,
``>``-quoting the older content one level deeper — so successive hops compound the ``>``
prefix and the deepest-nested trace at the bottom is the original exception."""


# ``traceback_info`` invariant: when non-``None``, it always begins with one of the two
# header lines above and the content below is uniformly ``>``-prefixed. This means
# ``__str__`` (and ``format_exception``'s rendering, which calls ``str(exc)`` for the
# header line) can render ``traceback_info`` verbatim without any extra wrapping —
# the chain is already self-labeled.
