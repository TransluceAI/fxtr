"""The host side of the wire: drive any
:class:`~fxtr.runner.experimental.multiplexed.WorkerSession` from the frames a remote worker sends.

:func:`serve_session` reads the worker's :class:`~.wire.Hello`, refusing a protocol version it does
not speak by closing the channel, then runs until the channel ends. It does not own the session's
lifecycle (the caller enters the session, serves it, and exits it when this returns); it owns its
own work per run, in a **per-attempt scope**: opened when an ``Assigned`` goes out, holding the
attempt's :class:`~.known_ids.HostKnownIds` (with its pending worker stores) and every task
started for that attempt, and closed once the run has ended (the completion reply, a ``Fail`` or
``Abandon`` reply, an ``AttemptCancelled``, or a refusal delivered as the attempt's failure) *and*
those tasks have finished, so a store still in flight at completion keeps the scope open a moment
longer and nothing is orphaned.

Two tasks run alongside a sender that drains one outbound queue to the channel. One pumps
``session.messages()`` out, recording a step assignment's input ids as given to the attempt and
closing the channel if the stream ends without a ``Shutdown`` (the worker must learn the host is
gone); after a ``Shutdown`` the channel stays open for the worker's last reports, and the worker
closes it. The other reads frames sequentially and dispatches: checking first, in frame order (that
the frame's ids are ones the attempt was given or stored, awaiting any pending store it refers to);
then a one-way frame calls the session's synchronous method inline, a :class:`~.wire.Store` starts
the host's store as a task in the attempt's scope, and a request starts a task that awaits the
session method and sends the reply. Requests run concurrently: several bodies block in ``observe``
at once.

**Intended use.** A daemon that accepts worker connections wraps each one as an
``ObjectStream[bytes]`` (a websocket's binary frames, a Unix socket with a length prefix), opens a
multiplexed host over its backend (:class:`~fxtr.runner.experimental.multiplexed.MultiplexedHost`,
the ``WorkerSession`` a remote worker is fed from), and serves it over that connection for the
connection's lifetime::

    async def worker_endpoint(websocket):
        await websocket.accept()
        host = MultiplexedHost()
        await serve_session(WebSocketChannel(websocket), host)

One session per connection: the adapter returns when the channel ends, and the caller's disposal
of the session is what abandons whatever the worker was still running. The adapter never sends
concurrently (one sender task drains one queue) and closes the channel itself only for a bad
``Hello`` or a session that ended without a ``Shutdown``. A reconnecting client must start a new
session, not resume one: the known-id tables and pending stores are per connection.

**Errors stay with their frame.** Nothing escapes the read loop. A request that fails, in the
session or in checking, is answered with :class:`~.wire.Refused`. A one-way
frame the session refuses (an emit, a log line, a pipelined store, or a claimed cid that does not
match) has no reply slot, so the refusal is delivered as the attempt's failure: ``session.fail``
(idempotent on a closed attempt) followed by ``AttemptCancelled``; for an attempt with no open
scope the session call is skipped and the cancel still sent. Only two things end the connection: a
frame that cannot be decoded, and a ``Hello`` naming a version the host does not speak.

**Ordering.** One-way calls are applied in frame order, but a request's effect happens when its
task runs, possibly after a later one-way frame was applied. This is safe because the worker never
sends a frame that depends on a request's effect before it has the reply: a host-computed store's
id is only used after its reply, and a body is sequential between awaits (concurrent operations in
a task group are unordered in process too). Pipelined stores are the one dependency that does not
go through a reply; the known-id tables' pending entries cover it.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

import anyio
import anyio.abc
from anyio.abc import ObjectStream
from frozendict import frozendict

from fxtr.runner.experimental.multiplexed import (
    Assigned,
    AttemptCancelled,
    Shutdown,
    WorkerSession,
)
from fxtr.runner.experimental.remote import wire
from fxtr.runner.experimental.remote.known_ids import HostKnownIds
from fxtr.runner.experimental.remote.wire import (
    PROTOCOL_VERSION,
    IdResult,
    NoResult,
    ProtocolError,
    ReplyResult,
    ValueResult,
    WireError,
    decode_frame,
    encode_frame,
    refusal_of,
)
from fxtr.runner.session import (
    AttemptError,
    AttemptID,
    StepAssignment,
)

_CLOSED = (anyio.EndOfStream, anyio.ClosedResourceError, anyio.BrokenResourceError)

type _Call = Callable[[], Awaitable[ReplyResult]]


@dataclasses.dataclass(eq=False)
class _Scope:
    """One attempt's work on the adapter: its known ids and the task group its tasks run in.

    ``ready`` is set once the runner task has opened the group; ``ended`` when the run has ended,
    after which the group waits for its children and the scope is gone.
    """

    attempt_id: AttemptID
    known: HostKnownIds = dataclasses.field(default_factory=HostKnownIds)
    tg: anyio.abc.TaskGroup | None = None
    ready: anyio.Event = dataclasses.field(default_factory=anyio.Event)
    ended: anyio.Event = dataclasses.field(default_factory=anyio.Event)


async def serve_session(channel: ObjectStream[bytes], session: WorkerSession) -> None:
    """Serve ``session`` to the worker at the other end of ``channel`` until the channel ends
    (see the module docstring). Returns normally in every case; the caller closes the session."""
    try:
        data = await channel.receive()
    except _CLOSED:
        return
    try:
        hello = decode_frame(data)
    except WireError:
        await channel.aclose()
        return
    if not isinstance(hello, wire.Hello) or hello.protocol_version != PROTOCOL_VERSION:
        await channel.aclose()
        return
    await _Server(channel, session).run()


class _Server:
    def __init__(self, channel: ObjectStream[bytes], session: WorkerSession) -> None:
        self._channel = channel
        self._session = session
        self._outbound_send, self._outbound_receive = anyio.create_memory_object_stream[bytes](
            math.inf
        )
        self._scopes: dict[AttemptID, _Scope] = {}
        self._tg: anyio.abc.TaskGroup | None = None

    async def run(self) -> None:
        async with anyio.create_task_group() as tg:
            self._tg = tg
            tg.start_soon(self._send_frames)
            tg.start_soon(self._pump_messages)
            await self._read_frames()
            # The worker is gone: nothing more can be answered or delivered.
            tg.cancel_scope.cancel()
        self._outbound_send.close()

    # ---- outbound ----

    def _queue(self, frame: wire.AnyFrame) -> None:
        try:
            self._outbound_send.send_nowait(encode_frame(frame))
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            pass

    async def _send_frames(self) -> None:
        try:
            async with self._outbound_receive:
                async for data in self._outbound_receive:
                    await self._channel.send(data)
        except _CLOSED:
            pass

    async def _pump_messages(self) -> None:
        shutdown = False
        async for message in self._session.messages():
            match message:
                case Assigned(assignment=assignment):
                    scope = self._open_scope(assignment.attempt_id)
                    if isinstance(assignment, StepAssignment):
                        message = Assigned(
                            dataclasses.replace(
                                assignment,
                                inputs=frozendict(
                                    {
                                        name: scope.known.give(ref.entity_id).bind_loader(
                                            ref.loader
                                        )
                                        for name, ref in assignment.inputs.items()
                                    }
                                ),
                            )
                        )
                case AttemptCancelled(attempt_id=attempt_id):
                    self._end_scope(attempt_id)
                case Shutdown():
                    shutdown = True
            self._queue(message)
        if not shutdown:
            # The session ended without a Shutdown (it was closed from outside): the worker must
            # see a disconnect, not a silence.
            await self._channel.aclose()

    # ---- scopes ----

    def _open_scope(self, attempt_id: AttemptID) -> _Scope:
        assert self._tg is not None
        scope = _Scope(attempt_id)
        self._scopes[attempt_id] = scope
        self._tg.start_soon(self._run_scope, scope)
        return scope

    async def _run_scope(self, scope: _Scope) -> None:
        async with anyio.create_task_group() as tg:
            scope.tg = tg
            scope.ready.set()
            await scope.ended.wait()
        # Every task of the attempt has finished; the scope, and its known ids, are gone.

    def _end_scope(self, attempt_id: AttemptID) -> None:
        scope = self._scopes.pop(attempt_id, None)
        if scope is not None:
            scope.ended.set()

    async def _spawn(
        self, scope: _Scope, fn: Callable[..., Coroutine[Any, Any, Any]], *args: Any
    ) -> bool:
        """Start a task in an attempt's scope; false if the scope has ended meanwhile."""
        await scope.ready.wait()
        if scope.ended.is_set():
            return False
        assert scope.tg is not None
        try:
            scope.tg.start_soon(fn, *args)
        except RuntimeError:
            return False  # the group closed between the check and the start
        return True

    # ---- inbound ----

    async def _read_frames(self) -> None:
        while True:
            try:
                data = await self._channel.receive()
            except _CLOSED:
                return
            try:
                frame = decode_frame(data)
            except WireError:
                return  # undecodable: the two ends do not share a protocol
            await self._dispatch(frame)

    async def _dispatch(self, frame: wire.AnyFrame) -> None:
        match frame:
            case wire.Register() | wire.Drain():
                assert self._tg is not None
                self._tg.start_soon(self._answer, frame, None, self._session_call(frame))
            case (
                wire.Observe()
                | wire.CompleteWorkflow()
                | wire.CompleteStep()
                | wire.Fail()
                | wire.Abandon()
                | wire.StoreRequest()
                | wire.LoadRawData()
            ):
                await self._attempt_request(frame)
            case wire.Store():
                await self._store(frame)
            case (
                wire.EmitSourceArray()
                | wire.EmitCombine()
                | wire.EmitStack()
                | wire.EmitRunStep()
                | wire.EmitRunChildWorkflow()
                | wire.LogMessage()
            ):
                await self._one_way(frame)
            case _:
                pass  # a second Hello, or a host frame from the worker: nothing to do with it

    # -- requests --

    def _session_call(self, frame: wire.Register | wire.Drain) -> _Call:
        async def call() -> ReplyResult:
            match frame:
                case wire.Register(functions=functions, max_concurrency=max_concurrency):
                    await self._session.register(functions, max_concurrency=max_concurrency)
                case wire.Drain():
                    await self._session.drain()
            return NoResult()

        return call

    async def _attempt_request(self, frame: wire.Request) -> None:
        attempt_id: AttemptID = frame.attempt_id  # type: ignore[union-attr]
        scope = self._scopes.get(attempt_id)
        if scope is None:
            self._refuse(frame, ProtocolError(f"Attempt {attempt_id} is not assigned here"))
            return
        try:
            call = await self._prepare(frame, scope)
        except Exception as exc:
            self._refuse(frame, exc)
            return
        if not await self._spawn(scope, self._answer, frame, scope, call):
            self._refuse(frame, ProtocolError(f"Attempt {attempt_id} is no longer assigned here"))

    async def _prepare(self, frame: wire.Request, scope: _Scope) -> _Call:
        """Check a request's ids (awaiting pending stores) and return the session call that
        answers it."""
        session, known = self._session, scope.known
        match frame:
            case wire.Observe(attempt_id=aid, node=node):

                async def call() -> ReplyResult:
                    return ValueResult(known.give_array(await session.observe(aid, node)))

            case wire.CompleteWorkflow(attempt_id=aid, result=token):

                async def call() -> ReplyResult:
                    await session.complete_workflow(aid, token)
                    return NoResult()

            case wire.CompleteStep(attempt_id=aid, result=value):
                array = await known.check_array(value)

                async def call() -> ReplyResult:
                    await session.complete_step(aid, array)
                    return NoResult()

            case wire.Fail(attempt_id=aid, error=error):

                async def call() -> ReplyResult:
                    await session.fail(aid, error)
                    return NoResult()

            case wire.Abandon(attempt_id=aid, reason=reason):

                async def call() -> ReplyResult:
                    await session.abandon(aid, reason)
                    return NoResult()

            case wire.StoreRequest(attempt_id=aid, data=value):
                data = await known.check_value(value)

                async def call() -> ReplyResult:
                    return IdResult(known.give(await session.store_raw_data(aid, data)))

            case wire.LoadRawData(attempt_id=aid, entity_id=entity_id):
                await known.check(entity_id)

                async def call() -> ReplyResult:
                    return ValueResult(
                        known.give_value(await session.load_raw_data(aid, entity_id))
                    )

            case _:
                raise ProtocolError(f"{type(frame).__name__} is not an attempt request")
        return call

    async def _answer(self, frame: wire.Request, scope: _Scope | None, call: _Call) -> None:
        try:
            reply: wire.Reply | wire.Refused = wire.Reply(frame.request_id, await call())
        except Exception as exc:
            reply = wire.Refused(frame.request_id, refusal_of(exc))
        self._queue(reply)
        if scope is not None and isinstance(
            frame, wire.CompleteWorkflow | wire.CompleteStep | wire.Fail | wire.Abandon
        ):
            self._end_scope(scope.attempt_id)

    def _refuse(self, frame: wire.Request, exc: BaseException) -> None:
        self._queue(wire.Refused(frame.request_id, refusal_of(exc)))

    # -- one-way frames --

    async def _one_way(self, frame: wire.WorkerFrame) -> None:
        attempt_id: AttemptID = frame.attempt_id  # type: ignore[union-attr]
        scope = self._scopes.get(attempt_id)
        if scope is None:
            self._queue(AttemptCancelled(attempt_id, "not assigned to this worker"))
            return
        session, known = self._session, scope.known
        try:
            match frame:
                case wire.EmitSourceArray(token=token, name=name, array=value):
                    array = await known.check_array(value)
                    session.emit_source_array(attempt_id, token, name, array)
                case wire.EmitCombine(token=token, inputs=inputs, join=join):
                    session.emit_combine(attempt_id, token, inputs, join)
                case wire.EmitStack(token=token, inputs=inputs, dim=dim):
                    session.emit_stack(attempt_id, token, dict(inputs), dim)
                case wire.EmitRunStep():
                    session.emit_run_step(
                        attempt_id,
                        frame.token,
                        name=frame.name,
                        step_name=frame.step_name,
                        inputs=frame.inputs,
                        map_over=frame.map_over,
                        join=frame.join,
                        replicas=frame.replicas,
                        cache_key_prefix=frame.cache_key_prefix,
                        type=frame.type,
                    )
                case wire.EmitRunChildWorkflow():
                    session.emit_run_child_workflow(
                        attempt_id,
                        frame.token,
                        name=frame.name,
                        workflow_name=frame.workflow_name,
                        inputs=frame.inputs,
                        map_over=frame.map_over,
                        join=frame.join,
                        replicas=frame.replicas,
                        cache_key_prefix=frame.cache_key_prefix,
                        type=frame.type,
                    )
                case wire.LogMessage(message=message):
                    session.log_message(attempt_id, message)
                case _:
                    raise ProtocolError(f"{type(frame).__name__} is not a one-way frame")
        except Exception as exc:
            await self._late_error(attempt_id, exc)

    async def _store(self, frame: wire.Store) -> None:
        scope = self._scopes.get(frame.attempt_id)
        if scope is None:
            self._queue(AttemptCancelled(frame.attempt_id, "not assigned to this worker"))
            return
        scope.known.expect(frame.entity_id.cid)
        await self._spawn(scope, self._store_task, scope, frame)

    async def _store_task(self, scope: _Scope, frame: wire.Store) -> None:
        try:
            data = await scope.known.check_value(frame.data)
            stored = await self._session.store_raw_data(frame.attempt_id, data)
            scope.known.complete(frame.entity_id.cid, stored)  # verifies the claimed cid
        except Exception as exc:
            scope.known.fail(frame.entity_id.cid, exc)
            await self._late_error(frame.attempt_id, exc)

    async def _late_error(self, attempt_id: AttemptID, exc: BaseException) -> None:
        """A one-way frame was refused: the attempt fails, the worker's run is cancelled."""
        if attempt_id in self._scopes:
            try:
                await self._session.fail(attempt_id, AttemptError.from_exception(exc))
            except Exception:
                pass  # already closed on the host: nothing to add
        self._queue(AttemptCancelled(attempt_id, f"{type(exc).__name__}: {exc}"))
        self._end_scope(attempt_id)


__all__ = ["serve_session"]
