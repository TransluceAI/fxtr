"""Transport E2E over the `NixPyodideServiceDefn` spawn path.

The pyodide sibling of `tests/test_nix_wasm_transport.py`: spawns the
`pyodide_transporter` fixture and exercises both transport surfaces —
raw byte echo via `connect_transport` (bridge-dialed transport UDS +
routing-header handshake) and HTTP via `get_client` (httpx/httpcore under
pyodide, riding the same authorizing backend). The http_get leg doubles as
the `[http]`-extra-under-pyodide verification from the 2p5 plan: httpx is
pure-python and TLS terminates host-side, so import-compat with pyodide's
stubbed `ssl` is the only requirement.

Skips when `deno` is not on PATH. See `test_nix_spawner.py` for the
slow-build caveats (same harness + nix setup).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol

import aiohttp
import aiohttp.web
import anyio
import anyio.abc
import pytest
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef
from fxtr2.services.typed_service import service_protocol
from fxtr_sandbox_types import NixPyodideServiceDefn
from fxtr_sandbox_types.protocols import NetworkHandleAuthorizerProto, SandboxConnectProto
from fxtr_sandboxing.proxy import TransportRoute
from fxtr_sandboxing.spawner import create_sandbox_environment
from fxtr_workdir_harness import prepare_workdir_repo

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.nix_runtime,
    pytest.mark.skipif(
        shutil.which("deno") is None,
        reason="pyodide backend needs deno on PATH",
    ),
]

_TRANSPORTER_TEMPLATE = Path(__file__).parent / "_workdir_templates" / "pyodide_transporter"
_CACHE_DIR = Path(__file__).parent.parent / "tmp_files_gitignore" / "wheel_cache"


@service_protocol
class TransporterProto(Protocol):
    """Mirror of the fixture's ``TransporterProto``."""

    async def raw_echo(self, msg: str) -> str: ...
    async def http_get(self, path: str) -> str: ...
    async def redeem_explicit_token(self, token: str) -> str: ...


def _make_echo_handler(test_tg: anyio.abc.TaskGroup):
    """Sync transport handler that echoes bytes back."""

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


@pytest.mark.deadline(seconds=600)
async def test_nix_pyodide_transport_round_trips(tmp_path: Path) -> None:
    """raw_echo + http_get through the git-ref-spawned pyodide sandbox."""
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
                    defn_ref = NixPyodideServiceDefn(
                        git_commit=prepared.head_commit,
                        flake_attr="fxtr.services.pyodide_transporter",
                    ).store()
                    sandbox_ref = await spawner.spawn(defn_ref)
                    # spawn() above includes the slow nix eval/build (SIGALRM
                    # territory); the live interaction below gets a tight
                    # cancellable window with clean async-CM unwind.
                    with anyio.fail_after(120):
                        sandbox_ref = sandbox_ref.replace_service_type(
                            SandboxConnectProto[
                                ServiceRef[NetworkHandleAuthorizerProto], TransporterProto
                            ]
                        )
                        sandbox = await sandbox_ref.resolve()

                        # Raw byte-echo surface.
                        echo_user_ref = (
                            await sandbox.connect(echo_authorizer)
                        ).replace_service_type(TransporterProto)
                        echo_user = await echo_user_ref.resolve()
                        assert await echo_user.raw_echo("ping!") == "ping!"

                        # HTTP surface ([http] extra under pyodide).
                        http_user_ref = (
                            await sandbox.connect(http_authorizer)
                        ).replace_service_type(TransporterProto)
                        http_user = await http_user_ref.resolve()
                        assert await http_user.http_get("/hello") == "upstream got /hello"

                test_tg.cancel_scope.cancel()
    finally:
        await runner.cleanup()
