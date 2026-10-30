"""End-to-end raw-transport tests through the eryx WASM sandbox.

Bundles ``examples/transport_task`` and exercises the full path:

    sandbox user code → connect_transport(token)
       → contextvar → EryxServiceContext.open_transport_stream
       → eryx _transport_connect callback
       → consume_token in TransportConnectionServer
       → host-side launcher invokes the registered handler
       → handler echoes bytes over the socketpair-backed ByteStream
       → sandbox user code reads them back

No HTTP involved — the handler is a tiny user-supplied async coroutine
that copies bytes from receive to send until EOF. Validates the token
table, eryx callback shape, ByteStream wrapper, and routing-header
machinery (the latter only via the eryx path; container handshake is a
separate test).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import anyio
import anyio.abc
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef
from fxtr2.services.typed_service import service_protocol
from fxtr_sandbox_types.protocols import NetworkHandleAuthorizerProto, SandboxConnectProto
from fxtr_sandboxing.deprecated.prepare import prepare
from fxtr_sandboxing.spawner import create_sandbox_environment

EXAMPLES_DIR = Path(__file__).parent.parent.parent / "examples"
CACHE_DIR = Path(__file__).parent.parent / "tmp_files_gitignore" / "wheel_cache"


@service_protocol
class TransporterProto(Protocol):
    """Mirror of ``transport_task.TransporterProto``. Same shape, used
    to retype the user ref host-side after ``connect()``."""

    async def raw_echo(self, msg: str) -> str: ...


def _make_echo_handler(test_tg: anyio.abc.TaskGroup):
    """Build a sync transport handler that echoes bytes back.

    The handler ``start_soon``s the actual async work on ``test_tg`` —
    this matches the convention HTTP route handlers use (capture an
    env-level task group at construction time).
    """

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
        test_tg.start_soon(_echo, stream)

    return handler


async def test_raw_transport_echo_round_trip() -> None:
    """Sandbox calls raw_echo(); bytes go through the eryx callback path
    to a host-side echo handler; the sandbox reads them back."""
    async with FxtrScope():
        bundle = await prepare(EXAMPLES_DIR / "transport_task")

        async with (
            create_sandbox_environment(cache_dir=CACHE_DIR) as env,
            FxtrScope(include=[env.host_scope]),
        ):
            async with anyio.create_task_group() as test_tg:
                authorizer_ref = await env.add_raw_transport(_make_echo_handler(test_tg))

                spawner = await env.spawner_ref.resolve()
                sandbox_ref = await spawner.spawn(bundle)
                sandbox_ref = sandbox_ref.replace_service_type(
                    SandboxConnectProto[ServiceRef[NetworkHandleAuthorizerProto], TransporterProto]
                )
                sandbox = await sandbox_ref.resolve()

                # The entry-point's first arg is the authorizer ref
                # itself — see transport_task.run.
                user_ref_any = await sandbox.connect(authorizer_ref)
                user_ref = user_ref_any.replace_service_type(TransporterProto)
                user = await user_ref.resolve()

                got = await user.raw_echo("hello, transport!")
                assert got == "hello, transport!", f"got {got!r}"

                # Cancel the test_tg so any lingering echo task exits
                # before the env CM tries to drain.
                test_tg.cancel_scope.cancel()


async def test_raw_transport_two_round_trips() -> None:
    """Two separate raw_echo calls on the same connect — exercises
    multiple token mints + redemptions on one sandbox session."""
    async with FxtrScope():
        bundle = await prepare(EXAMPLES_DIR / "transport_task")

        async with (
            create_sandbox_environment(cache_dir=CACHE_DIR) as env,
            FxtrScope(include=[env.host_scope]),
        ):
            async with anyio.create_task_group() as test_tg:
                authorizer_ref = await env.add_raw_transport(_make_echo_handler(test_tg))

                spawner = await env.spawner_ref.resolve()
                sandbox_ref = await spawner.spawn(bundle)
                sandbox_ref = sandbox_ref.replace_service_type(
                    SandboxConnectProto[ServiceRef[NetworkHandleAuthorizerProto], TransporterProto]
                )
                sandbox = await sandbox_ref.resolve()

                # The entry-point's first arg is the authorizer ref
                # itself — see transport_task.run.
                user_ref_any = await sandbox.connect(authorizer_ref)
                user_ref = user_ref_any.replace_service_type(TransporterProto)
                user = await user_ref.resolve()

                assert await user.raw_echo("one") == "one"
                assert await user.raw_echo("two") == "two"

                test_tg.cancel_scope.cancel()
