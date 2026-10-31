"""Tests for the remote worker session's two ends, proxy and adapter, over an in-process channel
against a mocked host session: both store modes, refusals delivered as the attempt's failure, the
protocol handshake, disconnects on both sides, and the per-attempt scopes.

What the two ends do with ordinary traffic — registration, the message stream, emits and their
tokens, request routing, completions, id checking — is exercised end to end by the
reference suite's remote Postgres entry, which runs every semantics case over this wire."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from unittest.mock import create_autospec

import anyio
import pytest
from frozendict import frozendict
from remote_testing import memory_channel_pair

from fxtr.core import codec
from fxtr.core.entity_data import (
    EntityID,
    UnknownReferenceError,
)
from fxtr.entity_defns.array import Array, ArrayType
from fxtr.runner.experimental.multiplexed import (
    Assigned,
    AttemptCancelled,
    HostDisconnectedError,
    Shutdown,
    StepSpec,
    WorkerMessage,
    WorkerSession,
    WorkflowSpec,
)
from fxtr.runner.experimental.remote import wire
from fxtr.runner.experimental.remote.proxy import RemoteWorkerSession
from fxtr.runner.experimental.remote.serve import serve_session
from fxtr.runner.experimental.remote.wire import (
    PROTOCOL_VERSION,
    ProtocolError,
    decode_frame,
    encode_frame,
)
from fxtr.runner.session import (
    AttemptClosedError,
    AttemptError,
    AttemptID,
    NodeToken,
    NodeTokenError,
    StepAssignment,
    WorkflowAssignment,
)
from fxtr.storage.memory_storage import MemoryStorage

pytestmark = pytest.mark.anyio

WF = AttemptID("wf-1")
ST = AttemptID("st-1")
REFS = ArrayType.of(("[k: int]", EntityID))


def cid(n: int) -> bytes:
    return b"\x01\x71\x12\x20" + n.to_bytes(32, "big")


def T(n: int) -> NodeToken:
    return NodeToken(n)


def content_id(data: Any) -> EntityID:
    return EntityID(codec.compute_cid(codec.encode(data)))


class Host:
    """A mocked host session behind a real in-memory store, whose message stream the test
    feeds."""

    def __init__(self) -> None:
        self.session = create_autospec(WorkerSession, instance=True)
        self.storage = MemoryStorage()
        self._send, receive = anyio.create_memory_object_stream[WorkerMessage](10)
        self._receive = receive

        async def stream() -> AsyncIterator[WorkerMessage]:
            async with self._receive:
                async for message in self._receive:
                    yield message

        self.session.messages.side_effect = stream
        self.session.store_raw_data.side_effect = lambda attempt_id, data: (
            self.storage.store_raw_data_sync(data)
        )

        async def load(attempt_id: AttemptID, entity_id: EntityID) -> Any:
            return await self.storage.load_raw_data(entity_id)

        self.session.load_raw_data.side_effect = load

    async def send(self, message: WorkerMessage) -> None:
        await self._send.send(message)
        await anyio.wait_all_tasks_blocked()

    async def shutdown(self) -> None:
        await self._send.send(Shutdown("bye"))
        await self._send.aclose()

    async def vanish(self) -> None:
        """End the message stream without a Shutdown, as a session closed from outside would."""
        await self._send.aclose()


def workflow_assignment(attempt_id: AttemptID = WF) -> WorkflowAssignment:
    return WorkflowAssignment(
        attempt_id=attempt_id,
        workflow_name="wf",
        pathname="root/wf",
        cache_key_prefix="cache/wf",
        inputs=frozendict({"x": (T(0), ArrayType.of(int))}),
    )


def step_assignment(*inputs: EntityID, attempt_id: AttemptID = ST) -> StepAssignment:
    return StepAssignment(
        attempt_id=attempt_id,
        step_name="s",
        cache_key="cache/s",
        inputs=frozendict({f"in{i}": ref.bind_type(Array) for i, ref in enumerate(inputs)}),
    )


@asynccontextmanager
async def connected(
    host: Host, **proxy_kwargs: Any
) -> AsyncIterator[tuple[RemoteWorkerSession, AsyncIterator[WorkerMessage]]]:
    """The host served over a channel to a proxy; yields the proxy and its message stream."""
    host_end, worker_end = memory_channel_pair()
    async with anyio.create_task_group() as tg:
        tg.start_soon(serve_session, host_end, host.session)
        async with RemoteWorkerSession(worker_end, **proxy_kwargs) as remote:
            messages = remote.messages()
            await remote.register([StepSpec("s"), WorkflowSpec("wf")], max_concurrency=2)
            yield remote, messages


async def settle() -> None:
    await anyio.wait_all_tasks_blocked()


# ---- stores ----


async def test_pipelined_stores_return_at_once_and_are_checked_on_the_host() -> None:
    host = Host()
    ref = await host.storage.store_raw_data({"$type": "t"})
    released = anyio.Event()

    async def slow_store(attempt_id: AttemptID, data: Any) -> EntityID:
        await released.wait()
        return host.storage.store_raw_data_sync(data)

    host.session.store_raw_data.side_effect = slow_store
    async with connected(host) as (remote, messages):
        await host.send(Assigned(step_assignment(ref)))
        assigned = await anext(messages)
        assert isinstance(assigned, Assigned) and isinstance(assigned.assignment, StepAssignment)
        received = assigned.assignment.inputs["in0"].entity_id
        assert received == ref
        stored = await remote.store_raw_data(ST, {"$type": "u", "ref": received})
        assert stored == content_id({"$type": "u", "ref": ref})
        assert host.session.store_raw_data.await_count == 0  # the worker did not wait
        assert remote.is_known(ST, stored)
        # A load of the stored id waits for the pending store on the host.
        loaded: list[Any] = []

        async def load() -> None:
            loaded.append(await remote.load_raw_data(ST, stored))

        async with anyio.create_task_group() as tg:
            tg.start_soon(load)
            await settle()
            assert loaded == []
            released.set()
        assert loaded == [{"$type": "u", "ref": ref}]
        (_, seen) = host.session.store_raw_data.await_args.args
        assert seen == {"$type": "u", "ref": ref}
        await host.shutdown()


async def test_storing_a_pending_value_again_joins_the_first_store() -> None:
    host = Host()
    released = anyio.Event()

    async def slow_store(attempt_id: AttemptID, data: Any) -> EntityID:
        await released.wait()
        return host.storage.store_raw_data_sync(data)

    host.session.store_raw_data.side_effect = slow_store
    async with connected(host) as (remote, messages):
        await host.send(Assigned(workflow_assignment()))
        await anext(messages)
        first = await remote.store_raw_data(WF, {"$type": "u", "n": 1})
        second = await remote.store_raw_data(WF, {"$type": "u", "n": 1})
        assert first == second
        loaded: list[Any] = []

        async def load() -> None:
            loaded.append(await remote.load_raw_data(WF, second))

        async with anyio.create_task_group() as tg:
            tg.start_soon(load)
            await settle()
            assert loaded == []
            released.set()
        assert loaded == [{"$type": "u", "n": 1}]
        assert host.session.store_raw_data.await_count == 2
        await host.shutdown()


async def test_an_id_from_another_attempt_is_known_where_its_cid_is() -> None:
    host = Host()
    shared = await host.storage.store_raw_data({"$type": "t"})
    other = await host.storage.store_raw_data({"$type": "t", "n": 2})
    first, second = AttemptID("st-1"), AttemptID("st-2")
    async with connected(host) as (remote, messages):
        await host.send(Assigned(step_assignment(shared, other, attempt_id=first)))
        assigned = await anext(messages)
        assert isinstance(assigned, Assigned) and isinstance(assigned.assignment, StepAssignment)
        from_first = assigned.assignment.inputs["in0"].entity_id
        not_in_second = assigned.assignment.inputs["in1"].entity_id
        await host.send(Assigned(step_assignment(shared, attempt_id=second)))
        await anext(messages)
        # The id the first attempt received names content the second attempt was given too.
        assert remote.is_known(second, from_first)
        assert await remote.load_raw_data(second, from_first) == {"$type": "t"}
        assert not remote.is_known(second, not_in_second)
        with pytest.raises(UnknownReferenceError, match="neither received nor stored"):
            await remote.load_raw_data(second, not_in_second)
        await host.shutdown()


async def test_host_computed_stores_wait_for_the_hosts_id() -> None:
    host = Host()
    async with connected(host, pipeline_stores=False) as (remote, messages):
        await host.send(Assigned(workflow_assignment()))
        await anext(messages)
        stored = await remote.store_raw_data(WF, {"$type": "u", "n": 1})
        assert stored == content_id({"$type": "u", "n": 1})
        host.session.store_raw_data.assert_awaited_once_with(WF, {"$type": "u", "n": 1})
        assert remote.is_known(WF, stored)
        assert await remote.load_raw_data(WF, stored) == {"$type": "u", "n": 1}
        await host.shutdown()


# ---- arrays ----


async def test_ids_in_an_array_are_known_by_their_cid() -> None:
    host = Host()
    ticket = await host.storage.store_raw_data({"$type": "t"})
    async with connected(host) as (remote, messages):
        await host.send(Assigned(step_assignment(ticket)))
        assigned = await anext(messages)
        assert isinstance(assigned, Assigned) and isinstance(assigned.assignment, StepAssignment)
        received = assigned.assignment.inputs["in0"].entity_id
        stored = await remote.store_raw_data(ST, {"$type": "u"})
        result = Array.from_items([((1,), received), ((2,), stored)], REFS)
        held = cast("list[EntityID]", list(result.raw_values()))
        assert await remote.load_raw_data(ST, held[0]) == {"$type": "t"}
        with pytest.raises(UnknownReferenceError, match="neither received nor stored"):
            await remote.load_raw_data(ST, EntityID(cid(9)))
        await remote.complete_step(ST, result)
        (_, completed) = host.session.complete_step.await_args.args
        assert completed == Array.from_items(
            [((1,), ticket), ((2,), content_id({"$type": "u"}))], REFS
        )
        await host.shutdown()


# ---- refusals and the handshake ----


async def test_a_refused_emit_fails_the_attempt_and_cancels_the_run() -> None:
    host = Host()
    host.session.emit_combine.side_effect = NodeTokenError("token 5 is not bound")
    async with connected(host) as (remote, messages):
        await host.send(Assigned(workflow_assignment()))
        await anext(messages)
        # Returns; the refusal arrives as a failure.
        remote.emit_combine(WF, T(1), {"a": T(5)}, "exact")
        cancelled = await anext(messages)
        assert isinstance(cancelled, AttemptCancelled) and cancelled.attempt_id == WF
        assert cancelled.reason == "NodeTokenError: token 5 is not bound"
        (attempt_id, error) = host.session.fail.await_args.args
        assert attempt_id == WF and error.type_name == "NodeTokenError"
        with pytest.raises(AttemptClosedError):
            remote.emit_combine(WF, T(2), {"a": T(0)}, "exact")
        # The attempt's scope is gone on the host too: a late report is refused, not served.
        await remote.fail(WF, AttemptError("X", "late", ""))  # a report: no exception
        assert host.session.fail.await_count == 1
        await host.shutdown()


async def test_hello_with_an_unknown_version_ends_the_connection() -> None:
    host = Host()
    host_end, worker_end = memory_channel_pair()
    async with anyio.create_task_group() as tg:
        tg.start_soon(serve_session, host_end, host.session)
        await worker_end.send(encode_frame(wire.Hello(PROTOCOL_VERSION + 1)))
        with pytest.raises((anyio.EndOfStream, anyio.ClosedResourceError)):
            await worker_end.receive()
        await worker_end.aclose()
    host.session.messages.assert_not_called()


# ---- disconnects ----


async def test_a_host_that_vanishes_ends_the_messages_and_fails_waiting_requests() -> None:
    host = Host()

    async def never(attempt_id: AttemptID, node: NodeToken) -> Array[Any]:
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    host.session.observe.side_effect = never
    async with connected(host) as (remote, messages):
        await host.send(Assigned(workflow_assignment()))
        await anext(messages)
        errors: list[BaseException] = []

        async def observe() -> None:
            try:
                await remote.observe(WF, T(0))
            except HostDisconnectedError as exc:
                errors.append(exc)

        async with anyio.create_task_group() as tg:
            tg.start_soon(observe)
            await settle()
            await host.vanish()
        assert len(errors) == 1
        with pytest.raises(StopAsyncIteration):
            await anext(messages)  # no Shutdown: the worker treats this as a disconnect
        with pytest.raises(HostDisconnectedError):
            await remote.drain()
        remote.log_message(WF, "dropped")  # one-way calls and reports are dropped silently
        await remote.abandon(WF, "worker stopped")


async def test_the_worker_closing_its_end_ends_the_serving() -> None:
    host = Host()
    host_end, worker_end = memory_channel_pair()
    served = anyio.Event()

    async def serve() -> None:
        await serve_session(host_end, host.session)
        served.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(serve)
        async with RemoteWorkerSession(worker_end) as remote:
            await remote.register([], max_concurrency=1)
        # Exiting the proxy closed the channel; the adapter returns without a Shutdown.
        with anyio.fail_after(2):
            await served.wait()


async def test_a_cancelled_attempt_is_closed_on_the_proxy() -> None:
    host = Host()
    async with connected(host) as (remote, messages):
        await host.send(Assigned(workflow_assignment()))
        await anext(messages)
        await host.send(AttemptCancelled(WF, "superseded"))
        assert await anext(messages) == AttemptCancelled(WF, "superseded")
        with pytest.raises(AttemptClosedError):
            remote.emit_combine(WF, T(1), {"a": T(0)}, "exact")
        with pytest.raises(AttemptClosedError):
            remote.is_known(WF, EntityID(cid(1)))
        await host.shutdown()


async def test_frames_for_unassigned_attempts_are_refused_by_the_adapter() -> None:
    """Below the proxy: a raw worker sending frames for an attempt the host never assigned is
    refused per frame, and the host's session is never called."""
    host = Host()
    host_end, worker_end = memory_channel_pair()
    async with anyio.create_task_group() as tg:
        tg.start_soon(serve_session, host_end, host.session)
        await worker_end.send(encode_frame(wire.Hello(PROTOCOL_VERSION)))
        await worker_end.send(encode_frame(wire.Observe(7, AttemptID("nope"), T(0))))
        refused = decode_frame(await worker_end.receive())
        assert isinstance(refused, wire.Refused) and refused.request_id == 7
        assert isinstance(refused.error.exception(), ProtocolError)
        await worker_end.send(encode_frame(wire.LogMessage(AttemptID("nope"), "x")))
        cancelled = decode_frame(await worker_end.receive())
        assert cancelled == AttemptCancelled(AttemptID("nope"), "not assigned to this worker")
        host.session.fail.assert_not_awaited()
        host.session.log_message.assert_not_called()
        await worker_end.aclose()
    # serve_session returned once the worker closed the channel (the task group finished).
