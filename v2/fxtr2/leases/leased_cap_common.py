"""Common utilities and base classes for implementing LeasedCapIR types.

Contains utilities for implementing subtypes of the LeasedCapIR system: the ownership-transfer
helpers (`copy_ir_value`, `discard_leases_in`), the local Cap-IR result-box machinery
(`LocalPendingResultBox`), the path-walked future view (`IRFutureLocalImpl`), the standard
spawner (`spawn_ir_future_in_task_group`), and the abstract `IRHandleLocalImplBase` that gives
subclasses a working `request` / `dispatch` once they implement `_run_request_async`.

Cap-bridge taxonomy (the at-rest lease-minting machinery a holder stores to produce fresh
leases) lives in `lease_bridges.py`.
"""

from __future__ import annotations

import abc
import dataclasses
import logging
import traceback
import weakref
from typing import Any, Awaitable, Callable, Literal, Mapping, Sequence, cast

import anyio
import anyio.abc

from .lease_types import FreshLifetime, Grant, Lease, LeaseBrokenError, LifetimeBrand, LifetimeToken
from .leased_cap_ir import (
    _PRE_WRAP_TRACEBACK_HEADER,
    Call,
    GetItem,
    IRCallArg,
    IRException,
    IRFuture,
    IRHandle,
    IRStatic,
    IRTagged,
    IRValue,
    Request,
    UnwrapForPipelining,
    WaitAndIgnoreFutureThen,
)
from .local_lease import LocalLifetimeBrand, LocalLifetimeToken, UnconditionalLocalLifetime

logger = logging.getLogger(__name__)

type UnwrappedCallArg = (
    IRValue
    | Lease[Any]
    | IRTagged[IRStatic, UnwrappedCallArg]
    | list[UnwrappedCallArg]
    | dict[str, UnwrappedCallArg]
)
"""Version of IRCallArg that assumes futures have already been unwrapped."""


def _clone_ir_exception(value: IRException[Any], new_data: Any) -> IRException[Any]:
    """Build a fresh :class:`IRException` copy of ``value``, swapping in the caller-
    supplied ``new_data`` for the data field and aliasing Python exception-context
    attributes (``__traceback__`` / ``__cause__`` / ``__context__`` / ``__notes__``)
    to the originals.

    The caller supplies the rebuilt ``new_data`` because the data field's recursive
    traversal differs by call site: :func:`copy_ir_value` recurses synchronously with
    ``take`` semantics, while :meth:`IRHandleLocalImplBase._unwrap_futures_in_value`
    recurses asynchronously to resolve any embedded ``UnwrapForPipelining`` along the
    way. Everything else (message, category, ``traceback_info``, Python attrs) is the
    same across both call sites, so it lives here.

    Use ``.message`` (raw) not ``str(value)`` — the latter is already category-prefixed,
    and reconstructing with a new category would double-prefix on subsequent copies.
    ``traceback_info`` is a frozen tuple of strings, shared by reference.

    Aliasing of ``__traceback__`` etc. is intentional. When Python re-raises
    ``new_exc``, it appends frames to ``new_exc.__traceback__`` (a new list), leaving
    the stored ``value.__traceback__`` untouched. ``__cause__`` / ``__context__`` /
    ``__notes__`` aren't mutated by raise, so aliasing is safe.

    NOTE: these attributes are local-only — neither the Cap-IR wire shape nor the
    JSON codec carries them. Cross-process ``IRException`` flows surface only
    message / category / data / ``traceback_info`` on the far side; ``traceback_info``
    is the wire-portable record of the cross-process traceback chain (see
    ``fxtr2/rpc/json_serdes.py``).
    """
    new_exc: IRException[Any] = IRException(
        message=value.message,
        category=value.category,
        data=new_data,
        traceback_info=value.traceback_info,
    )
    new_exc.__traceback__ = value.__traceback__
    new_exc.__cause__ = value.__cause__
    new_exc.__context__ = value.__context__
    existing_notes = getattr(value, "__notes__", None)
    if existing_notes:
        new_exc.__notes__ = list(existing_notes)
    return new_exc


def copy_ir_value(value: IRCallArg, *, take: bool) -> IRCallArg:
    """Deep-copy an IRValue / IRCallArg tree, cloning (or taking) every contained lease.

    Implements the Cap-IR ownership rule: mutable containers (lists, dicts, `IRTagged`,
    `IRException`, `UnwrapForPipelining`) are reconstructed so mutations on the result do
    not affect the original; leases are cloned — or transferred via `.take()` when
    `take=True` — so the returned tree owns an independent set of lease rights; handles,
    futures, tokens, and primitives are shared by reference (they are already stable).

    Typical call sites:
    - ``take=False`` for the *caller's* copy at dispatch time (caller retains its originals).
    - ``take=True`` for moving owned values between boxes, e.g. when handing a callee's owned
      result off to a pending-result box (the callee's copy is consumed).

    Args:
        value: The IR value or call argument to copy. May contain leases at any depth and
            `UnwrapForPipelining` pairs (the latter only valid inside call arguments).
        take: If True, transfer ownership of leases encountered in `value` by calling
            `.take()` on them (the caller must not use them after). If False, clone leases
            so the caller retains its originals.

    Returns:
        A copy of `value` with the same structure and content. Leases in the returned tree
        are fresh, independently-discardable copies.

    Raises:
        TypeError: If `value` (or any sub-value) is not a recognized IR value type.

    Note: if copying raises partway through (e.g. an unexpected type nested inside), any
    leases already cloned/taken before the failure are leaked. For `take=True` that means
    the corresponding originals have been consumed. This is acceptable in practice because
    the `LocalLifetimeLease` weakref finalizer will clean up any genuinely-leaked lease,
    emitting a warning; the cost is a warning entry in the log.
    """
    # Primitives (including bytes/str) — immutable, share by reference.
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return value
    # Stable reference-identity values — share.
    if isinstance(value, (IRHandle, IRFuture, LifetimeToken)):
        return value
    # Leases — clone or take.
    if isinstance(value, Lease):
        return value.take() if take else value.clone()
    # IRException — rebuild; data may itself be a mutable IR tree. `IRException`'s `D` type
    # parameter is unconstrained (doesn't have to be `IRCallArg`-shaped), so recursing on
    # `value.data` and passing it into a new `IRException` requires a cast to satisfy
    # `IRCallArg`'s narrower grammar (which only allows `IRException[IRStatic]`).
    if isinstance(value, IRException):
        new_data = None if value.data is None else copy_ir_value(value.data, take=take)
        return cast(IRCallArg, _clone_ir_exception(value, new_data))
    # IRTagged — copy tag and value (tag is "static" but may be mutable)
    if isinstance(value, IRTagged):
        return IRTagged(
            tag=cast(IRStatic, copy_ir_value(value.tag, take=take)),
            value=copy_ir_value(value.value, take=take),
        )
    # UnwrapForPipelining — share the future, clone/take the lease.
    if isinstance(value, UnwrapForPipelining):
        new_lease = value.lease.take() if take else value.lease.clone()
        return UnwrapForPipelining(value.future, new_lease)
    # Containers. Mappings before Sequences; bytes/str already handled above.
    if isinstance(value, Mapping):
        return {k: copy_ir_value(v, take=take) for k, v in value.items()}
    if isinstance(value, Sequence):
        return [copy_ir_value(v, take=take) for v in value]
    # Pyright correctly notes this is unreachable for well-typed callers — `IRCallArg` is
    # exhausted above. Keep the runtime check as a safety net for callers who bypass typing.
    raise TypeError(  # pyright: ignore[reportUnreachable]
        f"copy_ir_value: unsupported value of type {type(value).__name__}: {value!r}"
    )


def discard_leases_in(value: Any) -> None:
    """Recursively walk an IRValue / IRCallArg / Request-shaped tree and `.discard()`
    every `Lease`.

    Handles `Request` variants (``Call``, ``GetItem``, ``WaitAndIgnoreFutureThen``) too,
    so callers holding a partially-taken request tree can clean up on a failure path
    without type-dispatching at the call site.

    Used on cleanup paths — e.g. when a pending box's token ends before the result could
    be delivered, when `set_result`/`set_exception` is called on a box whose token has
    already ended, or when a pipelined work function fails before consuming all the
    leases it took ownership of.

    Unsupported types are logged and skipped (rather than raising) because this runs on
    cleanup paths where the best behavior is to release what we can and move on.
    """
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return
    if isinstance(value, Lease):
        value.discard()
        return
    if isinstance(value, (IRHandle, IRFuture, LifetimeToken)):
        return
    if isinstance(value, IRException):
        if value.data is not None:
            discard_leases_in(value.data)
        return
    if isinstance(value, IRTagged):
        discard_leases_in(value.value)
        return
    if isinstance(value, UnwrapForPipelining):
        value.lease.discard()
        return
    if isinstance(value, Call):
        discard_leases_in(value.arg)
        return
    if isinstance(value, GetItem):
        return  # no leases to discard
    if isinstance(value, WaitAndIgnoreFutureThen):
        discard_leases_in(value.wait_for)
        discard_leases_in(value.then)
        return
    if isinstance(value, Mapping):
        for v in value.values():
            discard_leases_in(v)
        return
    if isinstance(value, Sequence):
        for v in value:
            discard_leases_in(v)
        return
    # Unsupported. Skip — cleanup paths shouldn't throw.
    logger.warning(
        f"discard_leases_in: unsupported value of type {type(value).__name__}: {value!r}",
        stacklevel=2,
    )


def wrap_exception(exc: BaseException, *, category: str | None = None) -> IRException[Any]:
    """Convert a non-IRException Python exception into an IRException, capturing its
    formatted traceback as the initial ``traceback_info`` content.

    No-op if ``exc`` is already an IRException — it passes through unchanged. Encode-time
    enrichment (in :func:`fxtr2.rpc.json_serdes.encode_static`) captures any local Python
    ``__traceback__`` / ``__notes__`` on already-IRException values automatically when they
    cross the wire, so wrap-side logic doesn't need to do anything for them.

    Use this anywhere we catch a non-IR exception and convert it into an IRException to
    surface to a caller — typically inside ``except Exception as exc:`` blocks just before
    setting the result on a pending box, emitting a ``RejectMessage``, or otherwise
    propagating to remote code. Re-raise the wrapped exception with
    ``raise wrap_exception(exc) from None`` to suppress Python's default
    ``__cause__`` / ``__context__`` chain rendering — the chain is already preserved
    verbatim in ``traceback_info``, and surfacing it again via Python's chain mechanism
    would duplicate content at every encode hop.
    """
    if isinstance(exc, IRException):
        return exc
    formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    # Maintain the ``traceback_info`` invariant: always self-labeled as "earlier"
    # content with its body ``>``-quoted one level deeper. When the resulting
    # IRException is later rendered (str, format_exception, or wire-encoded),
    # the original Python exception's trace is unambiguously a previous version,
    # not a continuation of the IRException's own frames.
    pre_wrapped_info = f"{_PRE_WRAP_TRACEBACK_HEADER}\n{_quote_traceback_info(formatted)}"
    return IRException(
        message=str(exc),
        category=category if category is not None else type(exc).__name__,
        traceback_info=pre_wrapped_info,
    )


def _quote_traceback_info(traceback_info: str) -> str:
    """Add a ``"> "`` prefix to every line of an existing ``traceback_info`` content.

    Used by the wire codec (:func:`fxtr2.rpc.json_serdes.encode_static`) when prepending
    a fresh hop to an existing chain: the older content is line-quoted so it visually
    sits one nesting level deeper than the new entry. Empty lines become bare ``">"``
    (no trailing space). Repeated quoting compounds — a string already containing
    ``"> "`` lines gets each line prefixed again to produce ``"> > "`` — which is how
    the progressively-deeper nesting indicator emerges naturally over multiple hops.
    """
    return "\n".join(f"> {line}" if line else ">" for line in traceback_info.splitlines())


def _copy_request(req: Request, *, take: bool) -> Request:
    """Deep-copy a `Request`, cloning (or taking) leases inside `Call.arg` / `wait_for`.

    Symmetric with `copy_ir_value` but over the `Request` sum (which is not itself part of
    `IRCallArg`). Used at dispatch time so the caller can discard or mutate the original
    request without affecting the in-flight execution.
    """
    if isinstance(req, Call):
        return Call(method=req.method, arg=copy_ir_value(req.arg, take=take))
    if isinstance(req, GetItem):
        # Immutable, carries no leases.
        return req
    if isinstance(req, WaitAndIgnoreFutureThen):
        # `wait_for` is an `UnwrapForPipelining` or `Sequence[UnwrapForPipelining]`; both
        # shapes are handled by `copy_ir_value`. The static return type is `IRCallArg`, so
        # we cast it back to the narrower wait_for type.
        new_wait_for = cast(
            "UnwrapForPipelining[Any, Any] | Sequence[UnwrapForPipelining[Any, Any]]",
            copy_ir_value(cast(IRCallArg, req.wait_for), take=take),
        )
        return WaitAndIgnoreFutureThen(
            wait_for=new_wait_for, then=_copy_request(req.then, take=take)
        )
    raise TypeError(  # pyright: ignore[reportUnreachable]
        f"_copy_request: unknown request type {type(req).__name__}"
    )


class IRHandleLocalImplBase[B: LocalLifetimeBrand](IRHandle[B]):
    """Helper base class that implements the common parts of an IRHandle implementation.

    Assumed to have at least the following attributes:
        _token: The local lifetime token associated with this handle.
        _task_group: A task group, which _must_ outlive the token (in the sense that the token
            must be revoked before the task group is exited). The task group is used to spawn
            background dispatch tasks, and is typically shared across all handles that share the
            same `_token` (the common pattern is "one task group per token, multiple handles per
            (token, task group) pair"). See `open_lifetime_task_group` in `lease_util` for a
            context manager that co-constructs both and wires task-group errors through to
            `revoke()` on the token.

    The public `request` / `dispatch` methods are implemented here. Subclasses only supply
    `_run_request_async` (see below).
    """

    _token: LocalLifetimeToken[B]
    _task_group: anyio.abc.TaskGroup

    @property
    def token(self) -> LifetimeToken[B]:
        return self._token

    @abc.abstractmethod
    async def _run_request_async(
        self,
        lease: Lease[B],
        request: Call[UnwrappedCallArg] | GetItem,
    ) -> IRValue | UnwrapForPipelining[IRValue, Any]:
        """Subclass-supplied implementation: run a resolved request and return the result.

        The request passed in is always a `Call[UnwrappedCallArg]` (any `UnwrapForPipelining`
        values inside `arg` have already been awaited and substituted) or a `GetItem`.
        `WaitAndIgnoreFutureThen` is handled at the `request`/`dispatch` layer and never
        reaches this method.

        Ownership (standard transfer convention): `lease` and any leases inside `request`
        are transferred to this method. Each input lease should be used in *exactly one* of
        these ways:

        - discarded (explicitly, or by `.take()`'ing it and then discarding the new owner),
        - forwarded to another callee (e.g. handed to `handle.dispatch`), or
        - embedded in the returned result (in which case the runner's `set_result` /
          `resolve_from` takes ownership from it via the return-path copy).

        Note that if a lease is embedded in the result, the return-path `.take()` discards the
        reference the implementation held — an implementation cannot both store an input lease
        AND put it in the returned result. If you need to keep a copy and also return the lease,
        clone it: `stored = lease.clone()` before placing the original in the result.

        Leases that go out of scope without being discarded (not used in any of the three ways
        above) are considered "leaked" and will usually be cleaned up by a weakref finalizer, which
        logs a warning.

        Return value: either a plain `IRValue` (delivered to the result box via
        `set_result`) or an `UnwrapForPipelining(future, lease)` to *forward* resolution to
        another future (delivered via `box.resolve_from`). The latter lets a local handle
        delegate to another future without an extra re-box — the same promise-pipelining
        optimization as `pipeline_dispatch`.
        """
        raise NotImplementedError("Subclasses must implement this method")

    async def request(
        self,
        lease: Lease[B] | UnwrapForPipelining[Lease[B], Any],
        request: Request,
    ) -> IRValue:
        """Dispatch the request and wait for its result.

        Implemented as `dispatch` + `wait` + discard-of-result-lease, which means that
        cancelling the wait does **not** cancel the underlying task — the dispatched work
        runs to completion regardless of whether the caller bails on the wait. This matches
        the intended "wait is a read, not a control op" semantics.
        """
        future, result_lease = self.dispatch(lease, request)
        try:
            return await future.wait(result_lease.clone())
        finally:
            result_lease.discard()

    def dispatch[
        RB: FreshLifetime[Any]
    ](
        self,
        lease: Lease[B] | UnwrapForPipelining[Lease[B], Any],
        request: Request,
    ) -> tuple[
        IRFuture[Any, RB], Lease[RB]
    ]:
        """Dispatch the request without waiting for its result.

        Ownership (synchronous at entry): `lease` and every lease inside `request` are
        transferred in. For an `UnwrapForPipelining` outer lease, the inner `lease.lease`
        slot is taken at entry; the inner future is awaited inside the dispatch task,
        and the resolved `Lease[B]` is passed to `_run_request_async`. The method
        returns `(future, result_lease)` under a fresh result lifetime — discarding
        `result_lease` (and any clones) closes that lifetime and lets the in-flight
        task know its result can be dropped.
        """
        # Take ownership of inputs up-front so the caller can drop originals safely.
        # Pipelined-lease form: take the inner lease and rewrap; the future is shared
        # by reference (mirrors `IRFutureLocalImpl.pipeline_dispatch`'s inner-lease
        # take pattern).
        taken_lease: Lease[B] | UnwrapForPipelining[Lease[B], Any]
        if isinstance(lease, UnwrapForPipelining):
            taken_lease = UnwrapForPipelining(lease.future, lease.lease.take())
        else:
            taken_lease = lease.take()
        try:
            taken_request = _copy_request(request, take=True)
        except BaseException:
            if isinstance(taken_lease, UnwrapForPipelining):
                taken_lease.lease.discard()
            else:
                taken_lease.discard()
            raise

        async def work() -> IRValue | UnwrapForPipelining[IRValue, Any]:
            # Pre-`_run_request_async` cleanup: if either the request unwrap or the
            # outer-lease unwrap raises, the runner still owns `taken_lease` and
            # any unconsumed leases in `taken_request`. `_run_request_async` would
            # normally take ownership but never gets called. Discard explicitly so
            # rejection paths don't leak. (`discard_leases_in` is idempotent on
            # already-discarded leases; a partial `_unwrap_request_futures` walk is
            # safe to clean up this way.)
            try:
                unwrapped = await self._unwrap_request_futures(taken_request)
                concrete_lease = await _resolve_pipelined_lease(taken_lease)
            except BaseException:
                if isinstance(taken_lease, UnwrapForPipelining):
                    taken_lease.lease.discard()
                else:
                    taken_lease.discard()
                discard_leases_in(taken_request)
                raise
            return await self._run_request_async(concrete_lease, unwrapped)

        return spawn_ir_future_in_task_group(
            name=f"{type(self).__name__}.dispatch",
            work=work,
            task_group=self._task_group,
        )

    async def _unwrap_request_futures(self, request: Request) -> Call[UnwrappedCallArg] | GetItem:
        """Resolve any `UnwrapForPipelining` and `WaitAndIgnoreFutureThen` in `request`.

        Returns a `Call[UnwrappedCallArg] | GetItem` — the shape `_run_request_async`
        expects. Each `UnwrapForPipelining` inside a `Call.arg` is awaited, its lease
        consumed by the wait, and its slot is replaced with the resolved value. A
        `WaitAndIgnoreFutureThen` awaits its `wait_for` futures (discarding their results'
        leases) and then recursively unwraps its `then` request.
        """
        if isinstance(request, GetItem):
            return request
        if isinstance(request, Call):
            unwrapped_arg = await self._unwrap_futures_in_value(request.arg)
            return Call[UnwrappedCallArg](method=request.method, arg=unwrapped_arg)
        if isinstance(request, WaitAndIgnoreFutureThen):
            wait_for = request.wait_for
            unwraps: list[UnwrapForPipelining[Any, Any]] = (
                [wait_for] if isinstance(wait_for, UnwrapForPipelining) else list(wait_for)
            )
            for u in unwraps:
                result = await u.future.wait(u.lease)
                # Result is discarded per WaitAndIgnoreFutureThen semantics — release any
                # leases it happens to contain.
                discard_leases_in(result)
            return await self._unwrap_request_futures(request.then)
        raise TypeError(  # pyright: ignore[reportUnreachable]
            f"_unwrap_request_futures: unknown request type {type(request).__name__}"
        )

    async def _unwrap_futures_in_value(self, value: IRCallArg) -> UnwrappedCallArg:
        """Recursively replace `UnwrapForPipelining` in an IRCallArg tree with resolved
        values. Other value kinds pass through unchanged (containers are reconstructed).
        """
        if isinstance(value, UnwrapForPipelining):
            # `wait` takes ownership of the lease, and its result is a plain `IRValue`
            # (no further UnwrapForPipelining nested inside), so we don't need to recurse.
            resolved = await value.future.wait(value.lease)
            return cast(UnwrappedCallArg, resolved)
        if value is None or isinstance(value, (bool, int, float, str, bytes)):
            return value
        if isinstance(value, (IRHandle, IRFuture, LifetimeToken, Lease)):
            return value
        if isinstance(value, IRException):
            new_data = (
                None
                if value.data is None
                else await self._unwrap_futures_in_value(cast(IRCallArg, value.data))
            )
            return cast(UnwrappedCallArg, _clone_ir_exception(value, new_data))
        if isinstance(value, IRTagged):
            return IRTagged(
                tag=value.tag,
                value=await self._unwrap_futures_in_value(cast(IRCallArg, value.value)),
            )
        if isinstance(value, Mapping):
            return {k: await self._unwrap_futures_in_value(v) for k, v in value.items()}
        if isinstance(value, Sequence):
            return [await self._unwrap_futures_in_value(v) for v in value]
        raise TypeError(  # pyright: ignore[reportUnreachable]
            f"_unwrap_futures_in_value: unsupported value of type {type(value).__name__}: {value!r}"
        )


async def _resolve_pipelined_lease[
    B: LifetimeBrand
](lease: Lease[B] | UnwrapForPipelining[Lease[B], Any],) -> Lease[B]:
    """Resolve an outer dispatch-lease to a concrete `Lease[B]`.

    For a plain `Lease[B]`, returns it unchanged. For an
    :class:`UnwrapForPipelining[Lease[B], Any]`, awaits the inner future (the
    `lease.lease` slot is consumed by the wait per the `IRFuture.wait` contract)
    and returns the resolved lease. If the resolved value isn't actually a
    :class:`Lease`, raises :class:`IRException` with category
    ``"PipelineTypeError"``; any leases buried inside the resolved (deep-
    copied, owned) value are discarded before the raise.

    Used by :meth:`IRHandleLocalImplBase.dispatch`'s spawned runner to
    materialise the outer lease lazily, avoiding a synchronous round-trip
    on the dispatch path when the caller hands us a future-of-lease (e.g.
    a Cap-IR converter producing a fresh lease for a derived lifetime).
    """
    if not isinstance(lease, UnwrapForPipelining):
        return lease
    resolved = await lease.future.wait(lease.lease)
    if not isinstance(resolved, Lease):
        # Resolved value isn't a Lease; we own a deep copy of it that may
        # contain leases buried inside (handles, nested leases) — release
        # them before raising.
        discard_leases_in(resolved)
        raise IRException(
            message=(
                "IRHandle.dispatch: outer lease UnwrapForPipelining resolved to "
                f"{type(resolved).__name__}, not Lease"
            ),
            category="PipelineTypeError",
        )
    return cast(Lease[B], resolved)


_BoxState = Literal["pending", "resolved", "rejected", "forwarded", "discarded"]


@dataclasses.dataclass
class _BoxStorage:
    """Lease-owning state for a :class:`PendingResultBox`.

    Kept off the box so a :func:`weakref.finalize` on the box can access it
    (via the finalize's captured ``args``) to clean up any unconsumed leases
    if the box is garbage-collected before the token has ended. This sidesteps
    the otherwise-mutual pin between the box and its subject token's
    ``end_callbacks`` list — see :class:`LocalPendingResultBox` docstring.

    All fields are mutated in place by the box's setter methods; the storage
    object's identity is stable for the box's lifetime, so the finalize's
    closure sees whatever the most recent state transition left behind.
    """

    state: _BoxState = "pending"
    value: Any = None
    exception: IRException[Any] | None = None
    forward_future: IRFuture[Any, Any] | None = None
    forward_lease: Lease[Any] | None = None

    def discard_leases_and_mark_discarded(self) -> None:
        """Idempotent lease cleanup.

        Called from :meth:`PendingResultBox.discard` (explicit-owner path),
        :meth:`LocalPendingResultBox._on_token_ended` (token-end path), and
        :func:`_finalize_box_storage` (box-GC path). After this returns,
        ``state`` is ``"discarded"`` and every lease-bearing field has been
        released.
        """
        if self.state == "resolved":
            discard_leases_in(self.value)
            self.value = None
        elif self.state == "rejected":
            assert self.exception is not None
            discard_leases_in(self.exception)
            self.exception = None
        elif self.state == "forwarded":
            assert self.forward_lease is not None
            self.forward_lease.discard()
            self.forward_lease = None
            self.forward_future = None
        self.state = "discarded"


def _finalize_box_storage(
    storage: _BoxStorage,
    deregister_token_subscription: Callable[[], None],
) -> None:
    """:func:`weakref.finalize` callback for GC'd :class:`LocalPendingResultBox`.

    Scrubs the weak token-end subscription (so the token's ``end_callbacks``
    list doesn't accumulate dead closures) and discards any leases still held
    in ``storage`` — the cleanup ``_on_token_ended`` would have done, run at
    GC time instead.
    """
    deregister_token_subscription()
    storage.discard_leases_and_mark_discarded()


class PendingResultBox[T: IRValue]:
    """State container for a pending dispatch result.

    The box is a plain helper (not an `IRFuture`). All future views that share the same
    underlying computation hold a reference to a single ``PendingResultBox`` and differ
    only in their ``attr_path`` into the resolved tree.

    The base class is *token-less*: its lifecycle is managed entirely by its owner via
    explicit :meth:`discard`. The owner is responsible for calling ``discard`` when the
    box's underlying lifetime ends (so its stored value's leases are released and any
    parked waiter wakes with :class:`LeaseBrokenError`). Subclass
    :class:`LocalPendingResultBox` re-introduces an automatic token-end subscription for
    the local-future case where the box's owner *is* the lifetime owner; the RPC
    lowering layer's :class:`FutureImportEntry`, in contrast, owns the explicit-discard
    policy itself so a peer-driven token-end doesn't kill an in-flight delivery (see
    ``plans/extras/extra5_future_box_race_fix.md``).

    State machine (five states, stored in ``_storage.state``):
    - ``"pending"`` — no result delivered yet.
    - ``"resolved"`` — `set_result` was called with an `IRValue`. The value is stored in
        ``_storage.value`` and the box owns every lease inside it.
    - ``"rejected"`` — `set_exception` was called with an `IRException`. The exception is
        stored in ``_storage.exception``. A returned `IRException` is a valid resolved
        value (goes through `set_result`) and is distinct from a rejection.
    - ``"forwarded"`` — `resolve_from(future, lease)` was called to delegate resolution
        to another `IRFuture`. Subsequent `await_resolution` calls delegate to that future
        (no eager fetch-and-re-box), which is how we avoid an extra network round-trip
        when a local pipelined call is forwarding to a remote future. The box owns a
        lease (``_storage.forward_lease``) on the forwarded future's lifetime and shares
        (borrows) the future itself.
    - ``"discarded"`` — the box has been explicitly torn down (or, for
        :class:`LocalPendingResultBox`, its observed token has ended). Any stored
        value/exception has been freed and their leases discarded. ``await_resolution``
        raises ``LeaseBrokenError``; ``set_result`` / ``set_exception`` / ``resolve_from``
        gracefully release the inputs' leases and return, so a deliver task that wins
        the race against a discard never leaks.

    Ownership: `set_result` and `set_exception` *take* their inputs via `copy_ir_value(...,
    take=True)`. Callers must not use the passed value afterward. The take-and-canonicalize
    step ensures every stored container is a plain `list` / `dict` that `_descend_and_copy`
    can walk structurally (avoiding edge cases where a user class implements both
    `IRHandle` and `Sequence`, for instance).

    The box does not validate `lease.grant` on `await_resolution` — grant validation is the
    responsibility of the concrete `IRFuture` that owns the box (e.g. `IRFutureLocalImpl` checks
    grant equality against its single expected grant; a remote-side future proxy would instead
    check that `lease.grant.subject` is the future's token). Keeping the check out of the box
    lets remote proxies reuse the same state container with different validation semantics.
    """

    def __init__(self) -> None:
        # Lease-bearing state lives on a separate container so a
        # :func:`weakref.finalize` (installed by :class:`LocalPendingResultBox`)
        # can reach it for cleanup if the box is garbage-collected before its
        # token has ended. See the module-level :class:`_BoxStorage` docstring.
        self._storage = _BoxStorage()
        self._resolved_event = anyio.Event()

    @property
    def state(self) -> _BoxState:
        """The box's current state: one of
        ``"pending" | "resolved" | "rejected" | "forwarded" | "discarded"``.

        Exposed so callers outside this module (e.g. the RPC lowering's double-resolve
        protocol check) can observe non-pending states without peeking at the
        underscore-prefixed field.
        """
        return self._storage.state

    def is_resolved(self) -> bool:
        """True if the box has committed to a resolution — ``"resolved"``, ``"rejected"``,
        or ``"forwarded"`` (delegated to another future). ``"pending"`` and ``"discarded"``
        return False."""
        return self._storage.state in ("resolved", "rejected", "forwarded")

    def discard(self) -> None:
        """Explicitly tear down the box.

        Idempotent. Releases any stored value/exception/forward-lease, transitions to
        ``"discarded"``, and wakes any parked waiter (which then raises
        :class:`LeaseBrokenError` from :meth:`await_resolution`).

        This is the public escape hatch the RPC lowering layer's
        :class:`FutureImportEntry` uses to drive box teardown when (a) the remote
        token has ended and (b) any in-flight delivery has finished and waiters
        have drained. For :class:`LocalPendingResultBox`, the same teardown is also
        triggered automatically by the observed token's end-callback.
        """
        self._storage.discard_leases_and_mark_discarded()
        self._resolved_event.set()

    def set_result(self, value: T) -> None:
        """Deliver a successful result, taking ownership of every lease in `value`.

        `value` is nominally `T` (the caller's static type), but treated at runtime as an
        `IRValue`. If the box has already been ``discard``-ed, the value's leases are
        released and the call is a graceful no-op (the standard "delivery raced a
        discard" path). If the box has already been resolved/rejected/forwarded, raises
        :class:`RuntimeError`.
        """
        s = self._storage
        if s.state == "discarded":
            # Box already torn down; caller transferred ownership, so clean up their leases.
            discard_leases_in(value)
            return
        if s.state != "pending":
            raise RuntimeError(f"PendingResultBox already {s.state}; cannot set_result again")
        # `take=True` consumes `value`'s leases and gives us fresh independently-owned
        # copies stored in plain list/dict containers — see class docstring.
        s.value = cast(T, copy_ir_value(value, take=True))
        s.state = "resolved"
        self._resolved_event.set()

    def set_exception(self, exc: IRException[Any]) -> None:
        """Reject the box with an exception. Same take/idempotency/race rules as `set_result`."""
        s = self._storage
        if s.state == "discarded":
            discard_leases_in(exc)
            return
        if s.state != "pending":
            raise RuntimeError(f"PendingResultBox already {s.state}; cannot set_exception again")
        fresh = copy_ir_value(exc, take=True)
        assert isinstance(fresh, IRException)
        s.exception = fresh
        s.state = "rejected"
        self._resolved_event.set()

    def resolve_from[B2: LifetimeBrand](self, future: IRFuture[T, B2], lease: Lease[B2]) -> None:
        """Delegate this box's resolution to another `IRFuture`.

        After this call, any `await_resolution` on the box forwards to
        ``future.walk(attr_path).wait(lease.clone())`` — no eager fetch-and-re-box. This
        is the promise-pipelining optimization: when a local pipelined call produces a
        future that's already out on the network, we just forward our box to it rather
        than eagerly pulling the value through our process.

        Ownership: `lease` is transferred in (`.take()`'d). The caller may keep using
        `future` elsewhere — we only hold a borrowed reference to it. If the box has
        already been ``discard``-ed, the lease is discarded and the call is a graceful
        no-op.

        Raises:
            RuntimeError: if the box is already ``"resolved"``, ``"rejected"``, or
                ``"forwarded"`` (a box can only be committed once).
            RuntimeError: if `future` is the box itself (a self-forwarding loop).
        """
        s = self._storage
        if s.state == "discarded":
            lease.discard()
            return
        if s.state != "pending":
            raise RuntimeError(f"PendingResultBox already {s.state}; cannot resolve_from")
        if future is self:
            # Immediate self-loop. We can't detect deeper cycles cheaply, but this one
            # is worth catching.
            lease.discard()
            raise RuntimeError("PendingResultBox.resolve_from: cannot forward a box to itself.")
        s.forward_future = future
        s.forward_lease = lease.take()
        s.state = "forwarded"
        self._resolved_event.set()

    async def await_resolution(
        self,
        attr_path: Sequence[str | int],
        *,
        register_break: Callable[[Callable[[str], None]], Callable[[], None]],
    ) -> T:
        """Wait for the box to resolve, descend `attr_path`, and return a deep copy.

        Return type is annotated as `T`, matching the box's type parameter — this is the
        caller's static claim about the resolved value's shape and is not checked at
        runtime. For `attr_path=()` the returned value is exactly `T`; for a non-empty
        path the sub-value is a projection of `T` whose specific type isn't carried in the
        generic parameters.

        Break detection: `register_break` is called once on entry to install a
        cancellation-registering callback; its returned deregister handle is invoked
        before return. When the callback fires with a reason string, the wait aborts
        with `LeaseBrokenError`. Typical callers pass either
        ``lease.register_broken_callback`` (for a lease-backed wait) or a registrar
        that subscribes to a connection-level break channel (for a remote wait that
        has already discarded its user-visible lease). The box's token-end path still
        transitions to ``"discarded"`` via its own observer, so no separate token-end
        listening is needed here.

        If the box is in the ``"rejected"`` state, raises a fresh copy of the stored
        exception regardless of `attr_path` (the whole computation failed; descending into
        it doesn't apply). If the box is in the ``"discarded"`` state (token ended before
        any resolution was delivered), raises `LeaseBrokenError`.
        """
        if not self._resolved_event.is_set():
            await self._wait_until_resolved(register_break)

        s = self._storage
        if s.state == "discarded":
            raise LeaseBrokenError("result lifetime ended before a resolution could be delivered")
        if s.state == "rejected":
            exc = s.exception
            assert exc is not None
            # Always raise a *fresh* copy — Python mutates `__traceback__` on
            # exceptions when they're raised, so sharing the stored instance
            # across multiple awaits would contaminate that attribute.
            # ``copy_ir_value`` aliases ``__traceback__`` / ``__cause__`` /
            # ``__context__`` / ``__notes__`` from the stored value onto the
            # fresh copy so debug tracebacks survive the indirection; Python
            # then appends new frames to the fresh copy's ``__traceback__``
            # without mutating the stored one.
            fresh = copy_ir_value(exc, take=False)
            assert isinstance(fresh, IRException)
            raise fresh
        if s.state == "forwarded":
            # Delegate to the forwarded future. Wrapped in a cancel scope listening
            # to the caller's break channel so we abort the delegation promptly if
            # the caller's lifetime-interest goes (preserves the "break →
            # LeaseBrokenError ASAP" behavior that the non-forwarded path has via
            # `_wait_until_resolved`).
            return await self._await_forwarded(attr_path, register_break=register_break)
        assert s.state == "resolved"
        return cast(T, _descend_and_copy(s.value, attr_path))

    async def await_or_forward(
        self,
        attr_path: Sequence[str | int],
        *,
        register_break: Callable[[Callable[[str], None]], Callable[[], None]],
    ) -> IRValue | UnwrapForPipelining[IRValue, Any]:
        """Variant of `await_resolution` that exposes the forwarded state to the caller.

        Identical to `await_resolution` in every state except ``"forwarded"``: instead
        of recursively awaiting through the forward target, returns
        ``UnwrapForPipelining(forward_future.walk(attr_path), forward_lease.clone())``
        so the caller can decide how to follow the chain. Useful for the RPC lowering
        layer's pull path, which wants to detect "forwarded to a peer-side future" and
        emit a forwarding `ResolveMessage` rather than eagerly fetching the value.

        Return type is widened to `IRValue` (rather than `T`) because `attr_path` can
        descend into an arbitrary sub-shape of the resolved tree — the box's type
        parameter describes the whole value, not the walked-to leaf. Concrete
        `IRFuture` wrappers (e.g. :class:`IRFutureLocalImpl.wait_or_forward`) that
        bake in a specific `attr_path` at construction time can safely re-narrow to
        `T`, because in that setting `T` *is* the walked-to shape.

        Break detection: same contract as `await_resolution` — `register_break`
        installs a break-callback that aborts the wait with `LeaseBrokenError`. The
        returned `UnwrapForPipelining` carries a *clone* of the box's
        `_forward_lease` (the box retains its own copy), so the caller owns the
        returned lease and must eventually discard it.

        Raises the same `IRException` / `LeaseBrokenError` as `await_resolution` on the
        resolved/rejected/discarded/broken-lease paths.
        """
        if not self._resolved_event.is_set():
            await self._wait_until_resolved(register_break)

        s = self._storage
        if s.state == "discarded":
            raise LeaseBrokenError("result lifetime ended before a resolution could be delivered")
        if s.state == "rejected":
            exc = s.exception
            assert exc is not None
            fresh = copy_ir_value(exc, take=False)
            assert isinstance(fresh, IRException)
            raise fresh
        if s.state == "forwarded":
            assert s.forward_future is not None
            assert s.forward_lease is not None
            return UnwrapForPipelining(
                future=s.forward_future.walk(attr_path),
                lease=s.forward_lease.clone(),
            )
        assert s.state == "resolved"
        return _descend_and_copy(s.value, attr_path)

    async def _await_forwarded(
        self,
        attr_path: Sequence[str | int],
        *,
        register_break: Callable[[Callable[[str], None]], Callable[[], None]],
    ) -> T:
        """Delegate to the forwarded future, cancelling if the caller's break fires."""
        s = self._storage
        assert s.forward_future is not None
        assert s.forward_lease is not None
        broken_reason: str | None = None
        with anyio.CancelScope() as scope:

            def on_broken(reason: str) -> None:
                nonlocal broken_reason
                broken_reason = reason
                scope.cancel()

            dereg = register_break(on_broken)
            try:
                # `wait` takes ownership of our cloned forward lease and discards it on
                # exit (success, exception, or cancel), so we don't need to clean it up
                # here.
                return cast(
                    T,
                    await s.forward_future.walk(attr_path).wait(s.forward_lease.clone()),
                )
            finally:
                dereg()
        # Only reachable via scope.cancel() — the caller's break fired mid-delegation.
        raise LeaseBrokenError(f"broken during forwarded await_resolution: {broken_reason}")

    async def _wait_until_resolved(
        self,
        register_break: Callable[[Callable[[str], None]], Callable[[], None]],
    ) -> None:
        """Block until the box resolves, cancelling if the supplied break fires."""
        broken_reason: str | None = None
        with anyio.CancelScope() as scope:

            def on_broken(reason: str) -> None:
                nonlocal broken_reason
                broken_reason = reason
                scope.cancel()

            dereg = register_break(on_broken)
            try:
                await self._resolved_event.wait()
            finally:
                dereg()
        if scope.cancel_called:
            raise LeaseBrokenError(
                f"broken while waiting on LocalPendingResultBox: {broken_reason}"
            )


class LocalPendingResultBox[T: IRValue, B: LifetimeBrand](PendingResultBox[T]):
    """Subclass of :class:`PendingResultBox` for the local-future case.

    Adds a *weakly-held* subscription to the supplied token's end-callback list so the
    box auto-:meth:`discard`-s when the token ends — the appropriate teardown trigger
    when the box's owner *is* the lifetime owner (e.g. a dispatcher running in the same
    process as the future's holder). Used by :class:`IRFutureLocalImpl` and
    :func:`spawn_ir_future_in_task_group`.

    Lifetime ownership is external: the creator constructs a :class:`LifetimeToken` +
    a :class:`Grant` for it, passes the token in here, and keeps the lifetime alive.
    The box merely *observes* the token (``register_end_callback``) so it can clean up
    on token-end. Validation of incoming ``await_resolution`` leases against an expected
    grant is the responsibility of the surrounding :class:`IRFutureLocalImpl`.

    The token-end subscription is structured so the token's ``end_callbacks`` list does
    not pin the box: the registered ``on_end`` only weakly references the box, and a
    :func:`weakref.finalize` runs :func:`_finalize_box_storage` if the box is GC'd
    before the token ends. That finalizer scrubs the registration (so dead closures
    don't accumulate in the token's list) and discards any still-stored leases.

    The remote-future case (``RemoteIRFuture`` / ``FutureImportEntry``) does *not* use
    this subclass: the peer's token can end while we are still locally producing a
    result, so coupling box-discard to token-end would surface :class:`LeaseBrokenError`
    to a waiter that should have received the value. The lowering layer uses the
    plain :class:`PendingResultBox` and drives ``discard()`` itself once delivery and
    waiter-drain are both complete.
    """

    def __init__(self, token: LifetimeToken[B]) -> None:
        super().__init__()
        self._token = token
        # Weak-ref-based subscription to token-end so the token's
        # ``end_callbacks`` list does not pin the box. ``on_end`` only
        # weakly references ``self``; a :func:`weakref.finalize` runs
        # :func:`_finalize_box_storage` on GC to (a) remove our
        # ``on_end`` entry from the token's callback list and (b)
        # discard any leases still in ``_storage``.
        weak_self: weakref.ref[LocalPendingResultBox[T, B]] = weakref.ref(self)

        def on_end() -> None:
            box = weak_self()
            if box is not None:
                box.discard()

        dereg = token.register_end_callback(on_end)
        weakref.finalize(self, _finalize_box_storage, self._storage, dereg)

    @property
    def token(self) -> LifetimeToken[B]:
        return self._token


def _descend_and_copy(value: IRValue, attr_path: Sequence[str | int]) -> IRValue:
    """Descend into `value` along `attr_path`, then deep-copy the sub-value for the caller.

    Checks specifically for `dict` and `list` at each step (not the broader `Mapping` /
    `Sequence` protocols) because the box stores values produced by `copy_ir_value`, which
    always emits plain `dict`s and `list`s for container positions. This also sidesteps
    edge cases where a user class happens to satisfy both `IRHandle` and `Sequence`: such
    a user class at a container position should be treated as a leaf handle, not walked.

    Raises `TypeError` if a path step can't be applied to the current node.
    """
    current: Any = value
    for step in attr_path:
        if isinstance(current, dict):
            current = current[step]
        elif isinstance(current, list):
            if not isinstance(step, int):
                raise TypeError(
                    f"LocalPendingResultBox: cannot index list with non-integer step {step!r}"
                )
            current = current[step]
        else:
            raise TypeError(
                "LocalPendingResultBox: cannot walk into value of type"
                f" {type(current).__name__} at step {step!r}"
            )
    # `copy_ir_value` returns `IRCallArg`, which is a superset of `IRValue`; since the box
    # only ever stores `IRValue`, the descended-and-copied sub-value is narrower than the
    # function's declared return type.
    return cast(IRValue, copy_ir_value(current, take=False))


class IRFutureLocalImpl[T: IRValue, B: LifetimeBrand](IRFuture[T, B]):
    """Common implementation of an IRFuture.

    An `IRFutureLocalImpl` is a lightweight view onto a shared `LocalPendingResultBox`: it
    stores a reference to the box plus an `attr_path` (sequence of attribute / index
    lookups) describing which part of the resolved tree it represents. `walk` produces a
    new view with an extended path without touching the box. `wait` delegates to
    `box.await_resolution(attr_path, lease)` which awaits resolution, descends the path,
    and returns a deep copy. `pipeline_dispatch` schedules a continuation on the box that
    runs the downstream call once resolution is available, routing its result into a
    freshly created box under a fresh lifetime.

    The `task_group` passed in at construction is the task group that produced the box's
    result and is the one used for scheduling `pipeline_dispatch` continuations. Walked
    futures share the same task group as their parent.

    `IRFutureLocalImpl` owns grant validation for the box: every `wait` / `pipeline_dispatch`
    input lease must have a grant equal to `self._expected_grant`. The box itself does not
    check grants — keeping validation here lets remote-side proxies reuse
    `LocalPendingResultBox` with different validation semantics (e.g. "any grant with the
    right subject token" for imports).
    """

    def __init__(
        self,
        box: LocalPendingResultBox[T, B],
        attr_path: Sequence[str | int],
        task_group: anyio.abc.TaskGroup,
        expected_grant: Grant[B],
    ):
        """Initialize an IRFuture as a reference to a particular location in an async result.

        Args:
            box: The shared pending-result box. All walked views of this future share the
                same box; they differ only in their `attr_path`.
            attr_path: The walk path into the resolved tree that this view represents.
            task_group: The task group used to schedule `pipeline_dispatch` continuations.
                Must outlive the box's lifetime token.
            expected_grant: The grant that input leases (to `wait` / `pipeline_dispatch`)
                must compare equal to. Stored on the impl rather than the box so that
                remote-side futures can reuse the box with different validation rules.
        """
        self._box = box
        self._attr_path = tuple(attr_path)
        self._task_group = task_group
        self._expected_grant = expected_grant

    @property
    def token(self) -> LifetimeToken[B]:
        return self._box.token

    def _check_grant(self, lease: Lease[B]) -> None:
        """Raise RuntimeError if `lease.grant` does not match the expected grant.

        The expected grant appears on the left of the comparison so its class controls
        the equality semantics (mirrors the convention `LocalPendingResultBox` previously
        used when it owned the check).
        """
        if self._expected_grant != lease.grant:
            raise RuntimeError(
                "IRFutureLocalImpl: lease grant does not match this future's expected grant."
            )

    async def wait(self, lease: Lease[B]) -> T:
        """Await the box's resolution, descend `self._attr_path`, and return a deep copy.

        Ownership: `lease` is transferred in per the `IRFuture` contract and discarded
        before return on every path — including grant-mismatch, where we take ownership
        first and then validate so the lease is still cleaned up when the check raises.
        The lease's broken-callback registrar is passed to the box as its break signal.
        """
        with lease.take() as lease:
            self._check_grant(lease)
            return await self._box.await_resolution(
                self._attr_path, register_break=lease.register_broken_callback
            )

    async def wait_or_forward(self, lease: Lease[B]) -> T | UnwrapForPipelining[T, Any]:
        """Variant of :meth:`wait` that returns forwarded state to the caller.

        Same as ``wait`` except that if the box is in the ``"forwarded"`` state, instead
        of recursively awaiting through the forward target, returns
        ``UnwrapForPipelining(forward_future.walk(self._attr_path), forward_lease.clone())``.
        Lets the caller decide whether to follow the chain locally (by awaiting the
        returned future) or relay it elsewhere (e.g. the RPC lowering layer's pull path
        emits it as ``ResolveMessage(AwaitFutureExpr(...))`` rather than fetching the
        value through this process).

        The narrower ``T | UnwrapForPipelining[T, Any]`` return type is sound here
        (compared to the box's ``await_or_forward`` which widens to ``IRValue``): an
        :class:`IRFutureLocalImpl[T, ...]`'s type parameter describes the
        walked-to sub-value at ``self._attr_path``, so whatever the box resolves to
        at that path is a valid ``T``.

        Ownership: `lease` is transferred in and discarded before return on every
        branch (including grant-mismatch; same pattern as ``wait``). The returned
        ``UnwrapForPipelining``'s lease is a fresh clone the caller owns.
        """
        with lease.take() as lease:
            self._check_grant(lease)
            outcome = await self._box.await_or_forward(
                self._attr_path, register_break=lease.register_broken_callback
            )
        return cast("T | UnwrapForPipelining[T, Any]", outcome)

    def walk(self, attr_path: str | int | Sequence[str | int]) -> IRFuture[Any, B]:
        """Return a new IRFuture rooted at the same box with an extended attr_path.

        Convenience: a bare ``str`` or ``int`` is treated as a single-step path (so
        ``future.walk("x")`` works). A ``str`` is always a single step, never a sequence
        of characters — disambiguated by checking ``isinstance(..., (str, int))`` first.
        """
        if isinstance(attr_path, (str, int)):
            extra: tuple[str | int, ...] = (attr_path,)
        else:
            extra = tuple(attr_path)
        # The walked future may point at a sub-type of `T`; we widen to `Any` since the
        # type parameter can't express that projection generically.
        return IRFutureLocalImpl[Any, B](
            self._box, self._attr_path + extra, self._task_group, self._expected_grant
        )

    def pipeline_dispatch[
        RB: FreshLifetime[Any]
    ](
        self: IRFutureLocalImpl[IRHandle[Any], B],
        future_lease: Lease[B],
        inner_handle_lease: Lease[Any] | UnwrapForPipelining[Lease[Any], Any],
        request: Request,
    ) -> tuple[IRFuture[Any, RB], Lease[RB]]:
        """Schedule a pipelined call on the resolved value of this future.

        Conceptually equivalent to: wait for `self` to resolve (and descend `attr_path`),
        assert it's an `IRHandle`, optionally wait for `inner_handle_lease` if it's an
        `UnwrapForPipelining`, then dispatch `request` on that handle. This method
        returns immediately with a `(future, lease)` pair for the eventual result.

        Ownership (taken synchronously, before we return): `future_lease`,
        `inner_handle_lease` (whether a lease or an `UnwrapForPipelining`), and every
        lease inside `request` are transferred into the scheduled work. The caller must
        not use any of them after this returns.

        The continuation runs inside `self._task_group`, same as any other future produced
        by the dispatcher that owns this group.
        """
        # Take ownership of everything up-front so the caller can drop originals safely,
        # then validate the grant. Doing the take first means a mismatched lease and the
        # accompanying inner/request leases are all discarded on the raise rather than
        # silently leaked — matches the `wait`/`wait_or_forward` pattern.
        taken_future_lease = future_lease.take()
        taken_inner: Lease[Any] | UnwrapForPipelining[Lease[Any], Any]
        if isinstance(inner_handle_lease, UnwrapForPipelining):
            taken_inner = UnwrapForPipelining(
                inner_handle_lease.future, inner_handle_lease.lease.take()
            )
        else:
            taken_inner = inner_handle_lease.take()
        taken_request = _copy_request(request, take=True)
        try:
            self._check_grant(taken_future_lease)
        except BaseException:
            taken_future_lease.discard()
            if isinstance(taken_inner, UnwrapForPipelining):
                taken_inner.lease.discard()
            else:
                taken_inner.discard()
            discard_leases_in(taken_request)
            raise

        # Capture for closure.
        attr_path = self._attr_path
        box = self._box

        async def work() -> IRValue | UnwrapForPipelining[IRValue, Any]:
            # Relies on idempotent discard: downstream consumers take ownership of the
            # leases we pass in and discard them, so the `finally` block's
            # `discard_leases_in` calls are no-ops on successful paths. On any early
            # failure, the same calls release whatever leases the consumer didn't get to.
            try:
                # 1. Resolve `self` to an IRHandle. The future lease's
                #    broken-callback is the break signal; the lease itself stays
                #    alive until the `finally` block below discards it.
                resolved = await box.await_resolution(
                    attr_path, register_break=taken_future_lease.register_broken_callback
                )
                if not isinstance(resolved, IRHandle):
                    raise IRException(
                        message=(
                            f"pipeline_dispatch: future at {attr_path!r} resolved to"
                            f" {type(resolved).__name__}, not IRHandle"
                        ),
                        category="PipelineTypeError",
                    )
                # 2. Get the inner handle's lease, unwrapping if needed.
                inner_lease: Lease[Any]
                if isinstance(taken_inner, UnwrapForPipelining):
                    resolved_lease = await taken_inner.future.wait(taken_inner.lease)
                    if not isinstance(resolved_lease, Lease):
                        raise IRException(
                            message=(
                                "pipeline_dispatch: UnwrapForPipelining resolved to"
                                f" {type(resolved_lease).__name__}, not Lease"
                            ),
                            category="PipelineTypeError",
                        )
                    inner_lease = resolved_lease
                else:
                    inner_lease = taken_inner
                # 3. Fire-and-forget `dispatch` on the handle (rather than `request`),
                # then return an `UnwrapForPipelining` so the runner forwards our box to
                # the dispatched future's eventual resolution — no eager await + re-box.
                # `handle.dispatch` takes the lease and the request and gives us back
                # `(future, result_lease)` tied to a fresh lifetime.
                inner_future, inner_result_lease = resolved.dispatch(inner_lease, taken_request)
                return UnwrapForPipelining(inner_future, inner_result_lease)
            finally:
                discard_leases_in(taken_future_lease)
                discard_leases_in(taken_inner)
                discard_leases_in(taken_request)

        return spawn_ir_future_in_task_group(
            name=f"pipeline_dispatch({attr_path})",
            work=work,
            task_group=self._task_group,
        )


def spawn_ir_future_in_task_group[
    T: IRValue, RB: FreshLifetime[Any]
](
    *,
    name: str,
    work: Callable[[], Awaitable[T | UnwrapForPipelining[T, Any]]],
    task_group: anyio.abc.TaskGroup,
) -> tuple[IRFutureLocalImpl[T, RB], Lease[RB]]:
    """Run `work` inside `task_group` and route its outcome into a fresh result box.

    Creates a fresh `UnconditionalLocalLifetime` for the future, constructs a
    `LocalPendingResultBox` observing that lifetime, schedules a runner in `task_group`
    that executes `work`, and returns the resulting `(future, lease)` pair. The caller
    owns the returned lease; discarding it (and any clones) closes the lifetime, which
    transitions the box to ``"discarded"`` — any result `work` later produces will be
    dropped gracefully.

    Outcome routing:

    - `work` returns a plain `IRValue` → ``box.set_result(value)``.
    - `work` returns an `UnwrapForPipelining(future, lease)` → ``box.resolve_from(
      future, lease)``. This delegates the box's resolution to an already-pending
      `IRFuture` (the promise-pipelining optimization — it avoids eagerly fetching the
      inner value just to re-box it locally).
    - `work` raises `IRException` → ``box.set_exception(exc)`` with the original.
    - `work` raises any other `Exception` → wrapped in a fresh `IRException`
      (``category=type(exc).__name__``) and delivered via ``box.set_exception``. The
      task group does not see this exception (we swallow it to avoid cancelling the
      whole group over an application-level failure).
    - `work` raises a non-`Exception` `BaseException` (e.g. cancellation, SystemExit) →
      the lifetime is revoked and the exception is re-raised into `task_group` so
      structured-concurrency unwinding can proceed.

    Note that the runner keeps executing even if the caller discards the returned lease
    early (fire-and-forget pattern). Its eventual result will be dropped via the
    box's graceful-race handling.

    Args:
        name: Human-readable name for the lifetime (shows up in logs / leak warnings).
        work: Async callable producing the result (or an `UnwrapForPipelining` to
            delegate resolution to another future). Called with no arguments.
        task_group: The task group to schedule the runner in; must outlive the future's
            lifetime.

    Returns:
        `(future, lease)` — `future` is an `IRFutureLocalImpl` rooted at the box's empty
        attr_path; `lease` is the root lease on the future's fresh lifetime, owned by the
        caller.
    """
    lifetime = UnconditionalLocalLifetime[RB](name)
    box = LocalPendingResultBox[T, RB](lifetime.token)

    async def runner() -> None:
        try:
            result = await work()
        except IRException as exc:
            box.set_exception(exc)
        except Exception as exc:
            box.set_exception(wrap_exception(exc))
        except BaseException:
            lifetime.revoke(f"work in {name!r} raised BaseException")
            raise
        else:
            if isinstance(result, UnwrapForPipelining):
                # Delegate: the box's resolution is `result.future`'s resolution.
                # `result.lease` is taken into the box's `_forward_lease`.
                box.resolve_from(result.future, result.lease)
            else:
                box.set_result(result)

    task_group.start_soon(runner)
    future = IRFutureLocalImpl[T, RB](
        box, attr_path=(), task_group=task_group, expected_grant=lifetime.root_lease.grant
    )
    # `take()` hands the caller the root lease and discards our in-flight copy. The
    # lifetime stays open as long as the caller (or any clones) hold leases; when the
    # last is discarded, the token closes and the box transitions to `"discarded"`.
    return future, lifetime.root_lease.take()
