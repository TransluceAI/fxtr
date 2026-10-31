"""The worker side of the wire: a :class:`WorkerSession
<fxtr.runner.experimental.multiplexed.WorkerSession>` over a channel.

A :class:`RemoteWorkerSession` is used as an async context manager around a worker's run: entering
it sends :class:`~.wire.Hello` and starts two tasks, a sender draining an unbounded queue of
outbound frames to the channel (so the synchronous ``emit_<kind>`` methods, ``log_message``, and a
pipelined store queue a frame and return) and a receiver routing the host's frames: a
:class:`~.wire.Reply` or :class:`~.wire.Refused` to the request waiting on its id, and the host's
messages onto the stream :meth:`messages` yields. Exiting flushes what the worker queued, closes
this end of the channel, and stops the tasks.

**Known ids.** Per attempt the proxy keeps a :class:`~.known_ids.WorkerKnownIds`, created when
the attempt is assigned (noting a step assignment's inputs) and dropped when its run ends: after
the completion, ``fail``, or ``abandon`` reply, or on ``AttemptCancelled``. Every id in a reply is
noted under the request's attempt; ``is_known`` is answered from the table with no message, and
a value or array referencing an id the table does not hold is refused before it crosses. A call
naming an attempt the proxy has no table for (cancelled, finished, or never assigned) raises
:class:`~fxtr.runner.session.AttemptClosedError` at once, as an in-process session would.

**Stores.** ``store_raw_data`` first validates the value as an engine would (a tagged mapping, every
reference an id this attempt received or stored). Then, *pipelined* (the default), it encodes the
value, takes the cid of the bytes, queues a one-way :class:`~.wire.Store`, and returns the id at
once; or, *host-computed* (``pipeline_stores=False``), it sends a :class:`~.wire.StoreRequest` and
returns the id the host replies with.

**Disconnect.** When the channel ends, :meth:`messages` ends (the worker then raises
:class:`~fxtr.runner.experimental.multiplexed.HostDisconnectedError` if no ``Shutdown`` came), every
waiting request raises it too, and later one-way calls and the reports ``fail`` and ``abandon`` are
dropped: there is no host to tell. A refusal of a one-way call cannot reach the body (the call has
returned); it arrives as the attempt's failure, an ``AttemptCancelled`` from the host.
"""

from __future__ import annotations

import dataclasses
import itertools
import math
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Collection, Mapping
from contextlib import asynccontextmanager
from typing import Any, Self

import anyio
import anyio.abc
from anyio import AsyncContextManagerMixin
from anyio.abc import ObjectStream
from frozendict import frozendict

from fxtr.core import codec
from fxtr.core.entity_data import (
    EntityID,
    FxtrDataValue,
    StorableFxtrDataValue,
    UnknownReferenceError,
    get_entity_type_tag,
)
from fxtr.entity_defns.array import Array, ArrayType, CombineJoin, Join, KeyValue
from fxtr.entity_defns.converter import DEFAULT_CONVERTER
from fxtr.runner.experimental.multiplexed import (
    Assigned,
    AttemptCancelled,
    FunctionSpec,
    HostDisconnectedError,
    Shutdown,
    WorkerMessage,
)
from fxtr.runner.experimental.remote import wire
from fxtr.runner.experimental.remote.known_ids import WorkerKnownIds
from fxtr.runner.experimental.remote.wire import (
    PROTOCOL_VERSION,
    IdResult,
    NoResult,
    ProtocolError,
    ReplyResult,
    ValueResult,
    WireError,
    WireValue,
    cid_of,
    decode_array,
    decode_frame,
    decode_value,
    encode_frame,
    encode_value,
)
from fxtr.runner.session import (
    AttemptClosedError,
    AttemptError,
    AttemptID,
    NodeToken,
    StepAssignment,
)

_CLOSED = (anyio.EndOfStream, anyio.ClosedResourceError, anyio.BrokenResourceError)
"""How anyio reports a closed stream, at either end."""


@dataclasses.dataclass(eq=False)
class _Pending:
    """A request awaiting its reply."""

    attempt_id: AttemptID | None
    done: anyio.Event = dataclasses.field(default_factory=anyio.Event)
    result: ReplyResult | None = None
    error: BaseException | None = None


class RemoteWorkerSession(AsyncContextManagerMixin):
    """A ``WorkerSession`` over a channel to a host (see the module docstring).

    Args:
        channel: One end of an ordered, reliable bytes-message channel whose other end a host is
            serving (:func:`~.serve.serve_session`).
        pipeline_stores: Whether ``store_raw_data`` returns as soon as its frame is queued, the
            proxy hashing the value's bytes, or waits for the host to
            store and reply. Which is faster depends on where the worker runs; the default is a
            judgement, not a measurement.
        close_timeout: Seconds to wait, on exit, for queued frames to be sent before the channel
            is closed anyway.
    """

    def __init__(
        self,
        channel: ObjectStream[bytes],
        *,
        pipeline_stores: bool = True,
        close_timeout: float = 5.0,
    ) -> None:
        self._channel = channel
        self._pipeline_stores = pipeline_stores
        self._close_timeout = close_timeout
        self._outbound_send, self._outbound_receive = anyio.create_memory_object_stream[bytes](
            math.inf
        )
        self._messages_send, self._messages_receive = anyio.create_memory_object_stream[
            WorkerMessage
        ](math.inf)
        self._requests: dict[int, _Pending] = {}
        self._request_ids = itertools.count(1)
        self._attempts: dict[AttemptID, WorkerKnownIds] = {}
        self._disconnected = False
        self._sent_all = anyio.Event()

    # ---- lifecycle ----

    @asynccontextmanager
    async def __asynccontextmanager__(self) -> AsyncGenerator[Self]:
        async with anyio.create_task_group() as tg:
            self._queue(wire.Hello(PROTOCOL_VERSION))
            tg.start_soon(self._send_frames)
            tg.start_soon(self._receive_frames)
            try:
                yield self
            finally:
                # The worker is done: let the sender flush what was queued, then close our end,
                # which ends the receiver (and tells the host we are gone).
                self._outbound_send.close()
                with anyio.CancelScope(shield=True), anyio.move_on_after(self._close_timeout):
                    await self._sent_all.wait()
                with anyio.CancelScope(shield=True):
                    await self._channel.aclose()
                self._mark_disconnected()
                tg.cancel_scope.cancel()

    async def _send_frames(self) -> None:
        try:
            async with self._outbound_receive:
                async for data in self._outbound_receive:
                    await self._channel.send(data)
        except _CLOSED:
            self._mark_disconnected()
        finally:
            self._sent_all.set()

    async def _receive_frames(self) -> None:
        try:
            while True:
                try:
                    data = await self._channel.receive()
                except _CLOSED:
                    return
                try:
                    self._handle(decode_frame(data))
                except (WireError, ProtocolError):
                    return  # the host broke the protocol: as good as disconnected
        finally:
            self._mark_disconnected()

    def _mark_disconnected(self) -> None:
        if self._disconnected:
            return
        self._disconnected = True
        for pending in self._requests.values():
            pending.error = HostDisconnectedError("The connection to the host was lost")
            pending.done.set()
        self._requests.clear()
        self._messages_send.close()

    def _handle(self, frame: wire.AnyFrame) -> None:
        """Route one frame from the host."""
        match frame:
            case wire.Reply(request_id=request_id, result=result):
                pending = self._requests.pop(request_id, None)
                if pending is not None:
                    self._note_result(pending.attempt_id, result)
                    pending.result = result
                    pending.done.set()
            case wire.Refused(request_id=request_id, error=refusal):
                pending = self._requests.pop(request_id, None)
                if pending is not None:
                    try:
                        pending.error = refusal.exception()
                    except WireError as exc:
                        pending.error = exc
                    pending.done.set()
            case Assigned(assignment=assignment):
                known = WorkerKnownIds()
                if isinstance(assignment, StepAssignment):
                    for ref in assignment.inputs.values():
                        known.note(ref.entity_id)
                self._attempts[assignment.attempt_id] = known
                self._messages_send.send_nowait(frame)
            case AttemptCancelled(attempt_id=attempt_id):
                self._attempts.pop(attempt_id, None)
                self._messages_send.send_nowait(frame)
            case Shutdown():
                self._messages_send.send_nowait(frame)
            case _:
                raise ProtocolError(f"The host sent a worker frame: {type(frame).__name__}")

    def _note_result(self, attempt_id: AttemptID | None, result: ReplyResult) -> None:
        known = self._attempts.get(attempt_id) if attempt_id is not None else None
        if known is None:
            return
        match result:
            case ValueResult(value=value):
                known.note_value(value)
            case IdResult(id=entity_id):
                known.note(entity_id)
            case _:
                pass

    # ---- sending ----

    def _queue(self, frame: wire.AnyFrame) -> None:
        """Queue a frame for the sender; dropped once disconnected.

        Raises:
            WireError: If the frame holds something that cannot cross.
        """
        data = encode_frame(frame)
        if self._disconnected:
            return
        try:
            self._outbound_send.send_nowait(data)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            pass  # exited: nothing more goes out

    async def _request(
        self, attempt_id: AttemptID | None, build: Callable[[int], wire.Request]
    ) -> ReplyResult:
        """Send a request and wait for its reply.

        Raises:
            HostDisconnectedError: If the host is gone, before or while waiting.
            RunnerError: What the host refused the request with.
        """
        if self._disconnected:
            raise HostDisconnectedError("The connection to the host was lost")
        request_id = next(self._request_ids)
        pending = _Pending(attempt_id)
        self._requests[request_id] = pending
        try:
            self._queue(build(request_id))
            await pending.done.wait()
        finally:
            self._requests.pop(request_id, None)  # a reply after a cancellation is dropped
        if pending.error is not None:
            raise pending.error
        assert pending.result is not None
        return pending.result

    def _known(self, attempt_id: AttemptID) -> WorkerKnownIds:
        known = self._attempts.get(attempt_id)
        if known is None:
            raise AttemptClosedError(
                f"Attempt {attempt_id} is not assigned to this worker (finished, cancelled, or"
                " never assigned)"
            )
        return known

    def _run_ended(self, attempt_id: AttemptID) -> None:
        self._attempts.pop(attempt_id, None)

    # ---- WorkerSession: registration and messages ----

    async def register(self, functions: Collection[FunctionSpec], *, max_concurrency: int) -> None:
        await self._request(None, lambda rid: wire.Register(rid, tuple(functions), max_concurrency))

    async def drain(self) -> None:
        await self._request(None, lambda rid: wire.Drain(rid))

    async def messages(self) -> AsyncIterator[WorkerMessage]:
        async with self._messages_receive:
            async for message in self._messages_receive:
                yield message

    # ---- WorkerSession: workflow attempts ----

    def emit_source_array(
        self, attempt_id: AttemptID, token: NodeToken, name: str, array: Array[Any]
    ) -> None:
        value = self._encode_array(self._known(attempt_id), array)
        self._queue(wire.EmitSourceArray(attempt_id, token, name, value))

    def emit_combine(
        self,
        attempt_id: AttemptID,
        token: NodeToken,
        inputs: Mapping[str, NodeToken],
        join: CombineJoin,
    ) -> None:
        self._known(attempt_id)
        self._queue(wire.EmitCombine(attempt_id, token, frozendict(inputs), join))

    def emit_stack(
        self,
        attempt_id: AttemptID,
        token: NodeToken,
        inputs: Mapping[KeyValue, NodeToken],
        dim: str,
    ) -> None:
        self._known(attempt_id)
        self._queue(wire.EmitStack(attempt_id, token, tuple(inputs.items()), dim))

    def emit_run_step(
        self,
        attempt_id: AttemptID,
        token: NodeToken,
        *,
        name: str,
        step_name: str,
        inputs: Mapping[str, NodeToken],
        map_over: tuple[str, ...],
        join: Join,
        replicas: Mapping[str, tuple[KeyValue, ...]],
        cache_key_prefix: str,
        type: ArrayType,
    ) -> None:
        self._known(attempt_id)
        self._queue(
            wire.EmitRunStep(
                attempt_id,
                token,
                name,
                step_name,
                frozendict(inputs),
                tuple(map_over),
                join,
                frozendict({k: tuple(v) for k, v in replicas.items()}),
                cache_key_prefix,
                type,
            )
        )

    def emit_run_child_workflow(
        self,
        attempt_id: AttemptID,
        token: NodeToken,
        *,
        name: str,
        workflow_name: str,
        inputs: Mapping[str, NodeToken],
        map_over: tuple[str, ...],
        join: Join,
        replicas: Mapping[str, tuple[KeyValue, ...]],
        cache_key_prefix: str,
        type: ArrayType,
    ) -> None:
        self._known(attempt_id)
        self._queue(
            wire.EmitRunChildWorkflow(
                attempt_id,
                token,
                name,
                workflow_name,
                frozendict(inputs),
                tuple(map_over),
                join,
                frozendict({k: tuple(v) for k, v in replicas.items()}),
                cache_key_prefix,
                type,
            )
        )

    async def observe(self, attempt_id: AttemptID, node: NodeToken) -> Array[Any]:
        self._known(attempt_id)
        result = await self._request(attempt_id, lambda rid: wire.Observe(rid, attempt_id, node))
        return decode_array(_expect(result, ValueResult).value)

    async def complete_workflow(self, attempt_id: AttemptID, result: NodeToken) -> None:
        self._known(attempt_id)
        try:
            await self._request(
                attempt_id, lambda rid: wire.CompleteWorkflow(rid, attempt_id, result)
            )
        finally:
            self._run_ended(attempt_id)

    # ---- WorkerSession: step attempts ----

    async def complete_step(self, attempt_id: AttemptID, result: Array[Any]) -> None:
        value = self._encode_array(self._known(attempt_id), result)
        try:
            await self._request(attempt_id, lambda rid: wire.CompleteStep(rid, attempt_id, value))
        finally:
            self._run_ended(attempt_id)

    # ---- WorkerSession: either ----

    async def fail(self, attempt_id: AttemptID, error: AttemptError) -> None:
        try:
            if attempt_id in self._attempts and not self._disconnected:
                await self._request(attempt_id, lambda rid: wire.Fail(rid, attempt_id, error))
        except HostDisconnectedError:
            pass  # a report with no one to report to
        finally:
            self._run_ended(attempt_id)

    async def abandon(self, attempt_id: AttemptID, reason: str) -> None:
        try:
            if attempt_id in self._attempts and not self._disconnected:
                await self._request(attempt_id, lambda rid: wire.Abandon(rid, attempt_id, reason))
        except HostDisconnectedError:
            pass
        finally:
            self._run_ended(attempt_id)

    def log_message(self, attempt_id: AttemptID, message: str) -> None:
        self._known(attempt_id)
        self._queue(wire.LogMessage(attempt_id, message))

    async def store_raw_data(self, attempt_id: AttemptID, data: StorableFxtrDataValue) -> EntityID:
        known = self._known(attempt_id)
        get_entity_type_tag(data)
        _require_known(known, data, "Cannot store a value referencing")
        value = encode_value(data)
        if self._pipeline_stores:
            entity_id = known.stored(cid_of(value))
            self._queue(wire.Store(attempt_id, entity_id, value))
            return entity_id
        result = await self._request(
            attempt_id, lambda rid: wire.StoreRequest(rid, attempt_id, value)
        )
        return _expect(result, IdResult).id

    async def load_raw_data(self, attempt_id: AttemptID, entity_id: EntityID) -> FxtrDataValue:
        _require_known(self._known(attempt_id), entity_id, "Cannot load")
        result = await self._request(
            attempt_id, lambda rid: wire.LoadRawData(rid, attempt_id, entity_id)
        )
        return decode_value(_expect(result, ValueResult).value)

    def is_known(self, attempt_id: AttemptID, entity_id: EntityID) -> bool:
        return self._known(attempt_id).is_known(entity_id)

    def _encode_array(self, known: WorkerKnownIds, array: Array[Any]) -> WireValue:
        """``array`` for the wire.

        Raises:
            UnknownReferenceError: If it references an id this attempt did not receive or store.
        """
        data = DEFAULT_CONVERTER.unstructure_entity(array)
        _require_known(known, data, "Cannot send an array referencing")
        return encode_value(data)


def _require_known(known: WorkerKnownIds, value: StorableFxtrDataValue, action: str) -> None:
    """Check that every reference in ``value`` is one this attempt received or stored.

    Raises:
        UnknownReferenceError: Naming ``action`` and the first unknown reference.
    """
    for ref in codec.iter_entity_ids(value):
        if not known.is_known(ref):
            raise UnknownReferenceError(
                f"{action} {ref!r}: this attempt neither received nor stored it"
            )


def _expect[R: (ValueResult, IdResult, NoResult)](result: ReplyResult, kind: type[R]) -> R:
    if isinstance(result, kind):
        return result
    raise ProtocolError(f"The host replied with {type(result).__name__}, not {kind.__name__}")


__all__ = ["RemoteWorkerSession"]
