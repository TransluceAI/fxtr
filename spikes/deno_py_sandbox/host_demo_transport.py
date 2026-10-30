"""Transport side-channel demo for the pyodide-in-deno sandbox spike.

Exercises the token-keyed transport surface end-to-end (the pyodide analog
of ``tests/test_transport_raw_e2e_wasm.py``, without the env/spawner):

    guest user code (transport_task.raw_echo)
      → authorizer.get_transport_token() over the main RPC channel
      → connect_transport(token) → PyodideServiceContext.open_transport_stream
      → deno bridge dials the host's multiplexed transport UDS
      → routing header {token} → consume_token in TransportConnectionServer
      → host echo handler ↔ guest byte round trip

Run from the fxtr_sandboxing package env:

    cd projects/fxtr_sandboxing/fxtr_sandboxing
    uv run python ../../../spikes/deno_py_sandbox/host_demo_transport.py
"""

from __future__ import annotations

import sys
import time
from typing import Protocol

import anyio
import anyio.abc
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef, get_active_scope
from fxtr2.services.typed_service import service_protocol
from fxtr_sandbox_types import SandboxConnectProto
from fxtr_sandbox_types.protocols import NetworkHandleAuthorizerProto
from fxtr_sandboxing.network_authorizer import NetworkHandleAuthorizerImpl
from fxtr_sandboxing.transport_server import TransportConnectionServer

from host_common import open_deno_sandbox


@service_protocol
class TransporterProto(Protocol):
    """Mirror of ``transport_task.TransporterProto`` (raw_echo only —
    http_get needs the [http] extra, which the spike guest doesn't install)."""

    async def raw_echo(self, msg: str) -> str: ...


def _make_echo_handler(tg: anyio.abc.TaskGroup):
    """Sync transport handler that echoes bytes back (same shape as the
    raw-transport E2E test's handler)."""

    async def _echo(stream: anyio.abc.ByteStream) -> None:
        async with stream:
            try:
                while True:
                    chunk = await stream.receive(65536)
                    if not chunk:
                        break
                    await stream.send(chunk)
            except anyio.EndOfStream:
                pass

    def handler(stream: anyio.abc.ByteStream) -> None:
        tg.start_soon(_echo, stream)

    return handler


async def main() -> None:
    t0 = time.monotonic()
    async with FxtrScope():
        transport_server = TransportConnectionServer()
        async with anyio.create_task_group() as echo_tg:
            authorizer_ref = await get_active_scope().mount(
                NetworkHandleAuthorizerImpl(_make_echo_handler(echo_tg), transport_server)
            )

            async with open_deno_sandbox(
                "transport_task:run", transport_server=transport_server
            ) as sandbox_ref:
                typed = sandbox_ref.replace_service_type(
                    SandboxConnectProto[ServiceRef[NetworkHandleAuthorizerProto], TransporterProto]
                )
                sandbox = await typed.resolve()
                user_ref = (await sandbox.connect(authorizer_ref)).replace_service_type(
                    TransporterProto
                )
                user = await user_ref.resolve()
                t_connect = time.monotonic()
                print(f"[host] connected service in {t_connect - t0:.2f}s", file=sys.stderr)

                got = await user.raw_echo("hello, transport!")
                print(f"[host] raw_echo -> {got!r}", file=sys.stderr)
                assert got == "hello, transport!", got

                got2 = await user.raw_echo("second mint + redeem")
                assert got2 == "second mint + redeem", got2
                print(
                    f"[host] transport round-trips OK "
                    f"({time.monotonic() - t_connect:.3f}s for 2 echoes)",
                    file=sys.stderr,
                )

            echo_tg.cancel_scope.cancel()


if __name__ == "__main__":
    anyio.run(main)
