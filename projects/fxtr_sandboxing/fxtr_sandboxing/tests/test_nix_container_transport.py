"""Transport E2E over the `NixContainerServiceDefn` spawn path.

The git-ref container sibling of `tests/test_transport_e2e_container.py`:
`prepare_workdir_repo` builds a workdir from the `container_transporter`
fixture, spawns it through the nix container arm, threads a
`ServiceRef[NetworkHandleAuthorizerProto]` in as the connect config, and
exercises both transport surfaces (raw byte echo via `connect_transport`,
HTTP via `get_client`) through the per-spawn transport UDS.

Sweeps over all supported container runtimes via the `container_runtime`
fixture (see `conftest.py`); see `test_nix_container_spawner.py` for the
slow-build + host-env caveats (same setup).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import aiohttp
import aiohttp.web
import anyio
import anyio.abc
import pytest
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef
from fxtr2.services.typed_service import service_protocol
from fxtr_sandbox_types import NixContainerServiceDefn
from fxtr_sandbox_types.protocols import NetworkHandleAuthorizerProto, SandboxConnectProto
from fxtr_sandboxing.proxy import TransportRoute
from fxtr_sandboxing.spawner import create_sandbox_environment
from fxtr_workdir_harness import prepare_workdir_repo

pytestmark = [pytest.mark.anyio, pytest.mark.nix_runtime]

_TRANSPORTER_TEMPLATE = Path(__file__).parent / "_workdir_templates" / "container_transporter"
_CACHE_DIR = Path(__file__).parent.parent / "tmp_files_gitignore" / "wheel_cache"


@service_protocol
class TransporterProto(Protocol):
    """Mirror of the fixture's ``TransporterProto``."""

    async def raw_echo(self, msg: str) -> str: ...
    async def http_get(self, path: str) -> str: ...
    async def redeem_explicit_token(self, token: str) -> str: ...


def _make_echo_handler(test_tg: anyio.abc.TaskGroup):
    """Sync transport handler that echoes bytes back (see
    `test_transport_raw_e2e_wasm.py` for the convention)."""

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


async def _start_upstream() -> tuple[aiohttp.web.AppRunner, str]:
    """Single-route aiohttp test server bound to ``127.0.0.1``."""

    async def handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(text=f"upstream got {request.path}")

    app = aiohttp.web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sock = site._server.sockets[0]  # type: ignore[union-attr]
    port = sock.getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.deadline(seconds=900)
async def test_nix_container_transport_round_trips(tmp_path: Path, container_runtime: str) -> None:
    """raw_echo + http_get through the nix-built container's transport UDS."""
    runner, base_url = await _start_upstream()
    try:
        route = TransportRoute(
            upstream_base_url=base_url,
            injected_headers={},
            allowed_path_prefixes=None,
        )

        async with FxtrScope():
            prepared = await prepare_workdir_repo(_TRANSPORTER_TEMPLATE, tmp_path / "wd")

            async with anyio.create_task_group() as test_tg:
                async with (
                    create_sandbox_environment(
                        cache_dir=_CACHE_DIR, git_repo_path=prepared.path
                    ) as env,
                    FxtrScope(include=[env.host_scope]),
                ):
                    http_authorizer = await env.add_transport(route)
                    echo_authorizer = await env.add_raw_transport(_make_echo_handler(test_tg))

                    spawner = await env.spawner_ref.resolve()
                    defn_ref = NixContainerServiceDefn(
                        git_commit=prepared.head_commit,
                        flake_attr="fxtr.services.transporter",
                    ).store()
                    sandbox_ref = await spawner.spawn(defn_ref)
                    sandbox_ref = sandbox_ref.replace_service_type(
                        SandboxConnectProto[
                            ServiceRef[NetworkHandleAuthorizerProto], TransporterProto
                        ]
                    )
                    sandbox = await sandbox_ref.resolve()

                    # HTTP surface.
                    http_user_ref = (await sandbox.connect(http_authorizer)).replace_service_type(
                        TransporterProto
                    )
                    http_user = await http_user_ref.resolve()
                    assert await http_user.http_get("/hello") == "upstream got /hello"

                    # Raw byte-echo surface (fresh connect with the echo
                    # authorizer as config).
                    echo_user_ref = (await sandbox.connect(echo_authorizer)).replace_service_type(
                        TransporterProto
                    )
                    echo_user = await echo_user_ref.resolve()
                    assert await echo_user.raw_echo("ping!") == "ping!"

                test_tg.cancel_scope.cancel()
    finally:
        await runner.cleanup()
