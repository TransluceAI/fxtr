"""End-to-end nested-spawn test: WASM outer spawning a WASM inner.

The outer (``examples/nested_relay_task``) holds a
``ServiceRef[SandboxSpawnerProto]`` for the env's spawner, an
``EntityRef`` for the inner bundle, and a
``ServiceRef[NetworkHandleAuthorizerProto]`` for an HTTP route. Per
call to ``relay_http_get(paths)`` it spawns the inner
(``examples/transport_task``), connects with the authorizer, maps
``inner.http_get`` over the paths, wraps each result with
``[outer-wrapped] ``, and returns the list.

What this exercises end-to-end:

- A ``ServiceRef[SandboxSpawnerProto]`` survives a host→sandbox
  bridge and is callable from inside the outer.
- ``spawn(EntityRef[TaskBundle])`` from inside a sandbox lands a
  fresh sandbox under the env (a sibling of the outer at the runtime
  layer), not nested under the outer's process.
- A ``ServiceRef[NetworkHandleAuthorizerProto]`` bridges across two
  sandbox boundaries (host → outer → inner) and is usable from the
  leaf to mint tokens + redeem them on the env-owned side channel.
- Per-call inner spawn/teardown: the outer's dispatch scope drains
  at end of method, the bridges drop, the inner sandbox tears down
  cleanly, and the env stays usable.
- A plain Python ``@dataclass(frozen=True)`` is usable as the entry
  point's config annotation (the outer declares ``NestedRelayConfig``;
  the test mirrors it structurally).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import aiohttp
import aiohttp.web
import pytest
from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope, ServiceRef
from fxtr2.services.typed_service import service_protocol
from fxtr_sandbox_types.protocols import (
    NetworkHandleAuthorizerProto,
    SandboxConnectProto,
    SandboxSpawnerProto,
)
from fxtr_sandboxing.deprecated.prepare import prepare
from fxtr_sandboxing.proxy import TransportRoute
from fxtr_sandboxing.spawner import create_sandbox_environment

pytestmark = pytest.mark.nix_runtime

EXAMPLES_DIR = Path(__file__).parent.parent.parent / "examples"
CACHE_DIR = Path(__file__).parent.parent / "tmp_files_gitignore" / "wheel_cache"


@service_protocol
class NestedRelayProto(Protocol):
    """Mirror of ``nested_relay_task.NestedRelayProto``."""

    async def relay_http_get(self, paths: list[str]) -> list[str]: ...


@dataclass(frozen=True)
class NestedRelayConfig:
    """Mirror of ``nested_relay_task.NestedRelayConfig``.

    Plain Python dataclass — same field names + types as the outer's
    declaration. The service dialect either accepts plain dataclasses
    structurally, or this test will surface the gap and we'll fall
    back to a typed 3-tuple.
    """

    spawner_ref: ServiceRef[SandboxSpawnerProto]
    inner_bundle_ref: EntityRef[Any]
    authorizer_ref: ServiceRef[NetworkHandleAuthorizerProto]


async def _start_upstream(
    handler,
) -> tuple[aiohttp.web.AppRunner, str]:
    """Spin up a single-route aiohttp test server bound to ``127.0.0.1``."""
    app = aiohttp.web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sock = site._server.sockets[0]  # type: ignore[union-attr]
    port = sock.getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


async def test_nested_spawn_relay_http_get() -> None:
    """Outer relays http_get for a list of paths; results come back wrapped."""

    async def handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(text=f"upstream got {request.path}")

    runner, base_url = await _start_upstream(handler)
    try:
        route = TransportRoute(
            upstream_base_url=base_url,
            injected_headers={},
            allowed_path_prefixes=None,
        )

        async with FxtrScope():
            outer_bundle = await prepare(EXAMPLES_DIR / "nested_relay_task")
            inner_bundle = await prepare(EXAMPLES_DIR / "transport_task")
            async with (
                create_sandbox_environment(cache_dir=CACHE_DIR) as env,
                FxtrScope(include=[env.host_scope]),
            ):
                authorizer_ref = await env.add_transport(route)

                config = NestedRelayConfig(
                    spawner_ref=env.spawner_ref,
                    inner_bundle_ref=inner_bundle,
                    authorizer_ref=authorizer_ref,
                )

                spawner = await env.spawner_ref.resolve()
                untyped_sandbox_ref = await spawner.spawn(outer_bundle)

                # Type-narrow the sandbox-connect ref to the specific
                # (config, service) parameterization for the outer. This
                # makes ``connect(config)`` accept the typed dataclass
                # directly (the dialect encodes it for us) and gives back
                # an already-typed ``ServiceRef[NestedRelayProto]`` —
                # no second ``replace_service_type`` on the result needed.
                outer_sandbox_ref = untyped_sandbox_ref.replace_service_type(
                    SandboxConnectProto[NestedRelayConfig, NestedRelayProto]
                )
                outer_sandbox = await outer_sandbox_ref.resolve()

                relay_ref = await outer_sandbox.connect(config)
                relay = await relay_ref.resolve()

                got = await relay.relay_http_get(["/hello", "/world"])
                assert got == [
                    "[outer-wrapped] upstream got /hello",
                    "[outer-wrapped] upstream got /world",
                ], f"got {got!r}"
    finally:
        await runner.cleanup()
