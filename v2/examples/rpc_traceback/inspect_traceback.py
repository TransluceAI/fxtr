"""Inspect what a Python exception looks like after flowing through a JSON-RPC bridge.

Side A and side B are connected via :func:`fxtr2.rpc.scope_bridge.open_rpc_scope`
over a JSON-encoded in-memory transport pair. B's main service has a single
method ``trigger`` that calls down through several inner layers
(``_b_layer_a`` → ``_b_layer_b`` → ``_b_layer_c``) and ultimately raises a
plain Python ``ValueError``. A invokes the method, catches the resulting
:class:`IRException`, and writes three views of it to a file:

1. ``traceback.print_exception(exc)`` — what the user sees if they let the
   exception propagate uncaught (Python's standard exception display).
2. ``str(exc)`` — the ``__str__`` form (no leading "Traceback ..." header,
   but otherwise structurally identical to the printed form).
3. The raw fields (``message`` / ``category`` / ``data`` / ``traceback_info``)
   so you can see how the chain is encoded into the single ``traceback_info``
   string.

Run with ``uv run python examples/rpc_traceback/inspect_traceback.py``. The
output file is written to ``./ir_exception_render.txt`` next to this script.

Useful for verifying that:
- The encode-time + wrap-time machinery preserves the original Python
  exception's traceback across the wire.
- The ``Earlier (non-IRException)`` / ``Earlier (pre-RPC)`` headers + ``>``
  quoting clearly indicate hop boundaries and nesting depth.
- ``raise ... from None`` at the wrap site doesn't double-up the original
  exception's traceback (which would happen with ``raise ... from exc``).
"""

from __future__ import annotations

import math
import os
import traceback
from typing import Any

import anyio
import anyio.abc
from anyio.streams.stapled import StapledObjectStream

from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.leases.leased_cap_ir import IRException
from fxtr2.rpc.json_serdes import JSONTypes, wrap_json_stream
from fxtr2.rpc.lowering_types import RPCMessage
from fxtr2.rpc.scope_bridge import open_rpc_scope
from fxtr2.services.untyped_service import UntypedServiceImpl, service_method


def _make_json_transport_pair() -> (
    tuple[anyio.abc.ObjectStream[RPCMessage], anyio.abc.ObjectStream[RPCMessage]]
):
    a_to_b_send, a_to_b_recv = anyio.create_memory_object_stream[JSONTypes](math.inf)
    b_to_a_send, b_to_a_recv = anyio.create_memory_object_stream[JSONTypes](math.inf)
    a_raw = StapledObjectStream(a_to_b_send, b_to_a_recv)
    b_raw = StapledObjectStream(b_to_a_send, a_to_b_recv)
    return wrap_json_stream(a_raw), wrap_json_stream(b_raw)


class _RaisingService(UntypedServiceImpl):
    @service_method
    async def trigger(self) -> Any:
        return await self._b_layer_a()

    async def _b_layer_a(self) -> Any:
        return await self._b_layer_b()

    async def _b_layer_b(self) -> Any:
        return await self._b_layer_c()

    async def _b_layer_c(self) -> Any:
        raise ValueError("kaboom from deep inside B's service method")


async def main() -> None:
    a_transport, b_transport = _make_json_transport_pair()
    captured: list[IRException[Any]] = []

    with anyio.fail_after(5.0):
        async with anyio.create_task_group() as tg:

            async def side_a() -> None:
                async with open_rpc_scope(a_transport, name="A_rpc") as session:
                    proxy = await session.remote_service.resolve(session.scope)
                    try:
                        await proxy.trigger()
                    except IRException as exc:
                        captured.append(exc)

            async def side_b() -> None:
                async with FxtrScope(isolated=False, mountable=True, name="B") as host:
                    my_main = host.mount(_RaisingService())
                    async with open_rpc_scope(b_transport, my_main, name="B_rpc"):
                        pass

            tg.start_soon(side_a)
            tg.start_soon(side_b)

    assert captured, "expected to catch an IRException"
    exc = captured[0]
    sep = "=" * 78
    parts = [
        sep,
        "traceback.print_exception(exc):  (what the user sees on uncaught raise)",
        sep,
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        sep,
        "str(exc):",
        sep,
        str(exc),
        "",
        sep,
        "Field summary:",
        sep,
        f"  exc.message:  {exc.message!r}",
        f"  exc.category: {exc.category!r}",
        f"  exc.data:     {exc.data!r}",
    ]
    if exc.traceback_info is None:
        parts.append("  exc.traceback_info: None")
    else:
        parts.append(f"  exc.traceback_info: ({len(exc.traceback_info)} chars)")
        parts.append("")
        parts.append(exc.traceback_info)
    output = "\n".join(parts) + "\n"

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ir_exception_render.txt")
    with open(out_path, "w") as f:
        f.write(output)
    print(f"wrote rendered output to {out_path}")
    print(f"(len: {len(output)} chars)")


if __name__ == "__main__":
    anyio.run(main)
