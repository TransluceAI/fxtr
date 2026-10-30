"""Reproducer for a suspected lifetime bug.

Combination not exercised elsewhere in the v2 test suite:

- Service is declared with ``@service_protocol`` + ``TypedServiceImpl``
  (the typed-service framework path).
- The service is mounted as the main service of an ``open_rpc_scope``
  bootstrap (RPC, not in-process).
- A method takes a ``ServiceRef`` arg AND returns a chain-bridged
  ``ServiceRef`` output.

The RPC tests in ``test_scope_bridge.py`` use ``UntypedServiceImpl``
and pass. The typed-service tests in
``test_services/test_typed_service.py`` exercise the same in-process
shape and pass. This file checks the typed-RPC combination.

Discovered while building the workstation server in
``projects/fxtr_workstation``: the workstation's ``register_service``
method (typed protocol, takes a ``ServiceRef``, returns a
chain-bridged ``ReleaseCallback`` ref) sees its ``call_scope._token``
end immediately on dispatch return — before the chain-bridged output
reaches the caller — even though the cb impl's handle.token is
supposed to pin the call scope until the caller drops the ref. The
caller surfaces ``LeaseBrokenError: result lifetime ended before a
resolution could be delivered`` and the connection aborts.

Removing the ``ServiceRef`` arg from the method's signature makes
the bug go away (witness pattern works as expected).
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import anyio
import anyio.abc
import pytest
from anyio.streams.stapled import StapledObjectStream

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef, get_active_scope
from fxtr2.rpc.json_serdes import JSONTypes, wrap_json_stream
from fxtr2.rpc.lowering_types import RPCMessage
from fxtr2.rpc.scope_bridge import open_rpc_scope
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method


# Minimal copy of fxtr_sandbox_types.json_object_stream.JSONObjectStream
# (not in v2's deps). Same in-band ``["!", "eof"]`` sentinel for EOF.
class _JSONObjectStream(anyio.abc.ObjectStream[JSONTypes]):
    def __init__(
        self,
        sender: Callable[[JSONTypes], Awaitable[None]],
        receiver: Callable[[], Awaitable[JSONTypes]],
    ) -> None:
        self._send = sender
        self._receive = receiver
        self._closed = False
        self._eof = False

    async def send(self, item: JSONTypes) -> None:
        if self._closed:
            raise anyio.ClosedResourceError
        if isinstance(item, list) and len(item) > 0 and item[0] == "!":
            await self._send(["!", "escape", item])
        else:
            await self._send(item)

    async def receive(self) -> JSONTypes:
        if self._closed:
            raise anyio.ClosedResourceError
        if self._eof:
            raise anyio.EndOfStream
        raw = await self._receive()
        if isinstance(raw, list) and len(raw) > 0 and raw[0] == "!":
            if len(raw) == 2 and raw[1] == "eof":
                self._eof = True
                raise anyio.EndOfStream
            if len(raw) == 3 and raw[1] == "escape":
                return raw[2]
            raise ValueError(f"invalid control frame: {raw!r}")
        return raw

    def __aiter__(self) -> "_JSONObjectStream":
        return self

    async def __anext__(self) -> JSONTypes:
        try:
            return await self.receive()
        except (anyio.EndOfStream, anyio.ClosedResourceError):
            raise StopAsyncIteration

    async def aclose(self) -> None:
        self._closed = True

    async def send_eof(self) -> None:
        if self._closed:
            raise anyio.ClosedResourceError
        await self._send(["!", "eof"])


_ECHO_TYPE_ID = FxtrID.builtin("test-typed-rpc/echo")
_WITNESS_TYPE_ID = FxtrID.builtin("test-typed-rpc/witness")
_WRAPPER_TYPE_ID = FxtrID.builtin("test-typed-rpc/wrapper")
_WITNESS_ONLY_TYPE_ID = FxtrID.builtin("test-typed-rpc/witness-only")

pytestmark = pytest.mark.anyio


_DEADLOCK_TIMEOUT: float = 5.0


def _make_transport_pair() -> (
    tuple[anyio.abc.ObjectStream[RPCMessage], anyio.abc.ObjectStream[RPCMessage]]
):
    a_to_b_send, a_to_b_recv = anyio.create_memory_object_stream[RPCMessage](math.inf)
    b_to_a_send, b_to_a_recv = anyio.create_memory_object_stream[RPCMessage](math.inf)
    a_transport = StapledObjectStream(a_to_b_send, b_to_a_recv)
    b_transport = StapledObjectStream(b_to_a_send, a_to_b_recv)
    return a_transport, b_transport


def _make_json_transport_pair() -> (
    tuple[anyio.abc.ObjectStream[RPCMessage], anyio.abc.ObjectStream[RPCMessage]]
):
    """Mirror of the workstation `/rpc` transport stack: JSONObjectStream
    (in-band EOF sentinel) + wrap_json_stream layered over an in-memory
    JSON pipe."""
    a_to_b_send, a_to_b_recv = anyio.create_memory_object_stream[JSONTypes](math.inf)
    b_to_a_send, b_to_a_recv = anyio.create_memory_object_stream[JSONTypes](math.inf)

    a_json = _JSONObjectStream(sender=a_to_b_send.send, receiver=b_to_a_recv.receive)
    b_json = _JSONObjectStream(sender=b_to_a_send.send, receiver=a_to_b_recv.receive)
    return wrap_json_stream(a_json), wrap_json_stream(b_json)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ----------------------------------------------------------------------
# Typed protocols
# ----------------------------------------------------------------------


@service_protocol
class EchoProto(Protocol):
    async def echo(self, value: Any) -> Any: ...


@service_protocol
class WitnessProto(Protocol):
    async def ping(self) -> str: ...


@service_protocol
class WrapperProto(Protocol):
    async def wrap(self, inner_ref: ServiceRef[Any]) -> tuple[str, ServiceRef[WitnessProto]]: ...


@service_protocol
class WitnessOnlyProto(Protocol):
    """Same shape as WrapperProto.wrap but with NO ServiceRef arg —
    used to verify the witness pattern works when input doesn't carry
    a ServiceRef."""

    async def witness(self) -> ServiceRef[WitnessProto]: ...


# ----------------------------------------------------------------------
# Impls
# ----------------------------------------------------------------------


class EchoImpl(TypedServiceImpl[EchoProto]):
    def _target_type_annotation(self) -> Any:
        return EchoProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(_ECHO_TYPE_ID)

    @service_method
    async def echo(self, value: Any) -> Any:
        return value


class WitnessImpl(TypedServiceImpl[WitnessProto]):
    def _target_type_annotation(self) -> Any:
        return WitnessProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(_WITNESS_TYPE_ID)

    @service_method
    async def ping(self) -> str:
        return "pong"


class WrapperImpl(TypedServiceImpl[WrapperProto]):
    """Records on_end firing time on its dispatch scope token."""

    def __init__(self) -> None:
        self.end_callbacks_fired: list[float] = []
        self.dispatch_returns: list[float] = []

    def _target_type_annotation(self) -> Any:
        return WrapperProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(_WRAPPER_TYPE_ID)

    @service_method
    async def wrap(self, inner_ref: ServiceRef[Any]) -> tuple[str, ServiceRef[WitnessProto]]:
        del inner_ref
        scope = get_active_scope()

        recorder = self.end_callbacks_fired
        scope._token.register_end_callback(lambda: recorder.append(anyio.current_time()))

        out = await scope.mount(WitnessImpl())
        self.dispatch_returns.append(anyio.current_time())
        return ("hello", out)


class WitnessOnlyImpl(TypedServiceImpl[WitnessOnlyProto]):
    def _target_type_annotation(self) -> Any:
        return WitnessOnlyProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(_WITNESS_ONLY_TYPE_ID)

    @service_method
    async def witness(self) -> ServiceRef[WitnessProto]:
        scope = get_active_scope()
        return await scope.mount(WitnessImpl())


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


async def test_typed_service_witness_no_input_serviceref() -> None:
    """Control: typed service over RPC, method returns chain-bridged ref,
    NO ServiceRef input arg. Expected to pass."""
    a_transport, b_transport = _make_transport_pair()

    with anyio.fail_after(_DEADLOCK_TIMEOUT):
        async with anyio.create_task_group() as tg:

            async def side_a() -> None:
                async with FxtrScope(isolated=False, name="A") as host:
                    my_main = await host.mount(EchoImpl())
                    async with open_rpc_scope(a_transport, my_main, name="A_rpc") as session:
                        their_proxy = session.remote_service.replace_service_type(WitnessOnlyProto)
                        proxy = await their_proxy.resolve(session.scope)
                        wit_ref = await proxy.witness()
                        wit_typed = wit_ref.replace_service_type(WitnessProto)
                        wit_proxy = await wit_typed.resolve(session.scope)
                        assert await wit_proxy.ping() == "pong"

            async def side_b() -> None:
                async with FxtrScope(isolated=False, name="B") as host:
                    my_main = await host.mount(WitnessOnlyImpl())
                    async with open_rpc_scope(b_transport, my_main, name="B_rpc"):
                        pass

            tg.start_soon(side_a)
            tg.start_soon(side_b)


async def test_typed_service_witness_with_input_serviceref_json_transport() -> None:
    """Same as above but layered over JSONObjectStream + wrap_json_stream
    (the workstation /rpc transport stack). Probes whether the in-band
    EOF sentinel + JSON encoding step changes lifetime semantics."""
    a_transport, b_transport = _make_json_transport_pair()
    wrapper_impl = WrapperImpl()

    with anyio.fail_after(_DEADLOCK_TIMEOUT):
        async with anyio.create_task_group() as tg:

            async def side_a() -> None:
                # Match the workstation test client (connect_rpc): no
                # outer host; rely on open_rpc_scope's own NoOpService
                # mount via ``our_main_service=None``. The inner ref
                # is mounted on session.scope (the user_scope), which
                # is what tests do via ``await session_a.scope.mount(...)``.
                async with open_rpc_scope(
                    a_transport,
                    our_main_service=None,
                    name="A_rpc",
                ) as session:
                    my_inner = await session.scope.mount(EchoImpl())
                    their_proxy = session.remote_service.replace_service_type(WrapperProto)
                    proxy = await their_proxy.resolve(session.scope)
                    s, wit_ref = await proxy.wrap(my_inner)
                    assert s == "hello"
                    await anyio.sleep(0.1)
                    assert wrapper_impl.end_callbacks_fired == [], (
                        f"on_end fired prematurely while caller "
                        f"still holds wit_ref: {wrapper_impl.end_callbacks_fired}"
                    )
                    wit_typed = wit_ref.replace_service_type(WitnessProto)
                    wit_proxy = await wit_typed.resolve(session.scope)
                    assert await wit_proxy.ping() == "pong"

            async def side_b() -> None:
                # Match the workstation /rpc nesting:
                #   root_scope -> record_scope -> user_scope (via
                #   open_rpc_scope) -> call_scope (per-dispatch).
                async with FxtrScope(isolated=True, name="B_root") as root_scope:
                    async with FxtrScope(
                        isolated=True,
                        include=[root_scope],
                        name="B_record",
                    ) as record_scope:
                        my_main = await record_scope.mount(wrapper_impl)
                        async with open_rpc_scope(
                            b_transport,
                            my_main,
                            isolated=True,
                            include=[record_scope],
                            name="B_rpc",
                        ):
                            pass

            tg.start_soon(side_a)
            tg.start_soon(side_b)
