"""End-to-end integration test: two LeasedCapRPCConnection halves bridged by the
JSON wire codec.

Models the simplest bidirectional RPC flow from ``test_lowering.py``, but the transport
is a pair of ``anyio.create_memory_object_stream`` carrying ``JSONTypes`` values,
wrapped with :func:`wrap_json_stream` on each side so the connection still sees
``RPCMessage`` at its edge. This exercises every codec path in a realistic round-trip.
"""

from __future__ import annotations

import math
from typing import Any

import anyio
import anyio.abc
import pytest
from anyio.streams.stapled import StapledObjectStream

from fxtr2.leases.lease_types import Lease
from fxtr2.leases.lease_util import open_lifetime_task_group
from fxtr2.leases.leased_cap_common import IRHandleLocalImplBase
from fxtr2.leases.leased_cap_ir import Call, GetItem, IRValue, UnwrapForPipelining
from fxtr2.rpc.json_serdes import JSONTypes, wrap_json_stream
from fxtr2.rpc.lowering import LeasedCapRPCConnection, RPCMessage

pytestmark = pytest.mark.anyio


_TEST_DEADLOCK_TIMEOUT: float = 5.0


def _make_json_transport_pair() -> tuple[
    anyio.abc.ObjectStream[RPCMessage],
    anyio.abc.ObjectStream[RPCMessage],
]:
    """Build a bidirectional pair of ``JSONTypes``-valued streams stapled together,
    then wrap each side via :func:`wrap_json_stream` so the connection sees
    ``ObjectStream[RPCMessage]``."""
    a_to_b_send, a_to_b_recv = anyio.create_memory_object_stream[JSONTypes](math.inf)
    b_to_a_send, b_to_a_recv = anyio.create_memory_object_stream[JSONTypes](math.inf)
    a_raw = StapledObjectStream(a_to_b_send, b_to_a_recv)
    b_raw = StapledObjectStream(b_to_a_send, a_to_b_recv)
    return wrap_json_stream(a_raw), wrap_json_stream(b_raw)


class _EchoMain(IRHandleLocalImplBase[Any]):
    """Server-side main interface that echoes call args or mirrors GetItem attrs.

    - ``Call(method=m, arg=x)`` → returns ``x`` verbatim.
    - ``GetItem(attr=a)`` → returns ``{"attr": a}``.
    """

    def __init__(self, token: Any, task_group: anyio.abc.TaskGroup) -> None:
        self._token = token
        self._task_group = task_group

    async def _run_request_async(
        self,
        lease: Lease[Any],
        request: Call[Any] | GetItem,
    ) -> IRValue | UnwrapForPipelining[IRValue, Any]:
        lease.discard()
        if isinstance(request, Call):
            return request.arg
        return {"attr": request.attr}


class TestJsonWireRoundTrip:
    """Two connections bridged via wrap_json_stream exchange real RPC calls."""

    async def test_bidirectional_main_interface_calls(self) -> None:
        """Each side exposes an _EchoMain. Each side makes a call on the peer's main
        interface. Both results come back correct.

        This exercises: the JSON codec for Hello + Push + Pull + Resolve + Discard
        messages, expressions with imports / exports / statics, and bidirectional
        leases crossing the wire.
        """
        a_transport, b_transport = _make_json_transport_pair()

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with open_lifetime_task_group("A-main") as (a_lt, a_tg):
                        a_main = _EchoMain(a_lt.token, a_tg)
                        async with LeasedCapRPCConnection(
                            a_transport, a_main, a_lt.root_lease.clone()
                        ) as conn:
                            # A calls B's main interface.
                            result = await conn.their_main_interface.request(
                                conn.their_main_lease.clone(),
                                Call(method="echo", arg={"from": "A", "n": 1}),
                            )
                            assert result == {"from": "A", "n": 1}

                async def side_b() -> None:
                    async with open_lifetime_task_group("B-main") as (b_lt, b_tg):
                        b_main = _EchoMain(b_lt.token, b_tg)
                        async with LeasedCapRPCConnection(
                            b_transport, b_main, b_lt.root_lease.clone()
                        ) as conn:
                            # B calls A's main interface.
                            result = await conn.their_main_interface.request(
                                conn.their_main_lease.clone(),
                                GetItem(attr="hello"),
                            )
                            assert result == {"attr": "hello"}

                # No explicit rendezvous needed: each side runs its calls,
                # exits its CM, and drain inside ``conn.__aexit__`` waits for
                # the peer to release matching grant exports — the drain is
                # the rendezvous. While one side sits in conn drain, the
                # outer ``open_lifetime_task_group`` keeps its main service
                # alive and dispatchable for the peer's still-in-flight call.
                tg.start_soon(side_a)
                tg.start_soon(side_b)
