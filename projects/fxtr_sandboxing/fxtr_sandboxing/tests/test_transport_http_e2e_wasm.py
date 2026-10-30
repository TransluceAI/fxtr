"""End-to-end HTTP-transport tests through the eryx WASM sandbox.

Spins up a tiny aiohttp upstream, registers a ``TransportRoute`` for
it via ``env.add_transport``, and bundles ``examples/transport_task``
to make HTTP requests through the authorizer. Validates the full
``get_client(authorizer_ref)`` path:

    sandbox: get_client(ref) → httpx.AsyncClient
       → AsyncClient.get(path)
       → httpcore pool → _AuthorizingBackend.connect_tcp
       → child FxtrScope: resolve(ref) → get_transport_token() → token
       → connect_transport(token)
       → eryx _transport_connect callback → consume_token
       → host-side launcher → _make_http_route_handler's start_soon
       → proxy_connection (h11 ↔ aiohttp upstream)
       → response back through the same chain
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import aiohttp
import aiohttp.web
import anyio
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef
from fxtr2.services.typed_service import service_protocol
from fxtr_sandbox_types.protocols import NetworkHandleAuthorizerProto, SandboxConnectProto
from fxtr_sandboxing.deprecated.prepare import prepare
from fxtr_sandboxing.proxy import TransportRoute
from fxtr_sandboxing.spawner import create_sandbox_environment

EXAMPLES_DIR = Path(__file__).parent.parent.parent / "examples"
CACHE_DIR = Path(__file__).parent.parent / "tmp_files_gitignore" / "wheel_cache"


@service_protocol
class TransporterProto(Protocol):
    """Mirror of ``transport_task.TransporterProto``."""

    async def raw_echo(self, msg: str) -> str: ...
    async def http_get(self, path: str) -> str: ...
    async def redeem_explicit_token(self, token: str) -> str: ...


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


async def test_http_get_round_trip() -> None:
    """Sandbox calls http_get('/hello'); response body comes back."""

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
            bundle = await prepare(EXAMPLES_DIR / "transport_task")
            async with (
                create_sandbox_environment(cache_dir=CACHE_DIR) as env,
                FxtrScope(include=[env.host_scope]),
            ):
                authorizer_ref = await env.add_transport(route)

                spawner = await env.spawner_ref.resolve()
                sandbox_ref = await spawner.spawn(bundle)
                sandbox_ref = sandbox_ref.replace_service_type(
                    SandboxConnectProto[ServiceRef[NetworkHandleAuthorizerProto], TransporterProto]
                )
                sandbox = await sandbox_ref.resolve()

                user_ref_any = await sandbox.connect(authorizer_ref)
                user_ref = user_ref_any.replace_service_type(TransporterProto)
                user = await user_ref.resolve()

                got = await user.http_get("/hello")
                assert got == "upstream got /hello", f"got {got!r}"

                got2 = await user.http_get("/world")
                assert got2 == "upstream got /world", f"got {got2!r}"
    finally:
        await runner.cleanup()


async def test_http_header_injection() -> None:
    """Verify the route's injected headers actually reach the upstream."""

    captured_headers: dict[str, str] = {}

    async def handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
        captured_headers.update({k: v for k, v in request.headers.items()})
        return aiohttp.web.Response(text="ok")

    runner, base_url = await _start_upstream(handler)
    try:
        route = TransportRoute(
            upstream_base_url=base_url,
            injected_headers={"X-Secret": "shhh"},
            allowed_path_prefixes=None,
        )

        async with FxtrScope():
            bundle = await prepare(EXAMPLES_DIR / "transport_task")
            async with (
                create_sandbox_environment(cache_dir=CACHE_DIR) as env,
                FxtrScope(include=[env.host_scope]),
            ):
                authorizer_ref = await env.add_transport(route)
                spawner = await env.spawner_ref.resolve()
                sandbox_ref = await spawner.spawn(bundle)
                sandbox_ref = sandbox_ref.replace_service_type(
                    SandboxConnectProto[ServiceRef[NetworkHandleAuthorizerProto], TransporterProto]
                )
                sandbox = await sandbox_ref.resolve()

                user_ref_any = await sandbox.connect(authorizer_ref)
                user_ref = user_ref_any.replace_service_type(TransporterProto)
                user = await user_ref.resolve()

                await user.http_get("/")
                assert (
                    captured_headers.get("X-Secret") == "shhh"
                ), f"injected header missing; saw {captured_headers!r}"
    finally:
        await runner.cleanup()


async def test_http_path_filtering_denied() -> None:
    """A path outside the allowlist is rejected with 403.

    Exercises the half-close+drain path in ``_send_error`` — without
    that, a hard close-after-error races the client's outstanding
    writes and surfaces as ``BrokenPipeError`` instead of a clean 403.
    """

    async def handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(text="should not reach")

    runner, base_url = await _start_upstream(handler)
    try:
        route = TransportRoute(
            upstream_base_url=base_url,
            injected_headers={},
            allowed_path_prefixes=["/allowed"],
        )

        async with FxtrScope():
            bundle = await prepare(EXAMPLES_DIR / "transport_task")
            async with (
                create_sandbox_environment(cache_dir=CACHE_DIR) as env,
                FxtrScope(include=[env.host_scope]),
            ):
                authorizer_ref = await env.add_transport(route)
                spawner = await env.spawner_ref.resolve()
                sandbox_ref = await spawner.spawn(bundle)
                sandbox_ref = sandbox_ref.replace_service_type(
                    SandboxConnectProto[ServiceRef[NetworkHandleAuthorizerProto], TransporterProto]
                )
                sandbox = await sandbox_ref.resolve()

                user_ref_any = await sandbox.connect(authorizer_ref)
                user_ref = user_ref_any.replace_service_type(TransporterProto)
                user = await user_ref.resolve()

                # /denied is outside the allowlist → proxy returns 403.
                # http_get's raise_for_status() turns 4xx into an
                # ``HTTPStatusError``, which the RPC layer surfaces as
                # an IRException with the original message in str().
                with anyio.fail_after(5.0):
                    try:
                        await user.http_get("/denied")
                    except Exception as e:
                        assert "403" in str(e), f"expected 403 in error, got {e!r}"
                    else:
                        raise AssertionError("expected an HTTP error for denied path")
    finally:
        await runner.cleanup()
