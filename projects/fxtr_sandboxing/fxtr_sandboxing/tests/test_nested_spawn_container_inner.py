"""End-to-end nested-spawn test: WASM outer spawning a nix-container inner.

Mirrors :mod:`tests.test_nested_spawn_wasm_inner` but the inner bundle is
a ``NixContainerServiceDefn`` (the `container_transporter` workdir fixture,
built via ``prepare_workdir_repo`` like the other nix-container tests)
rather than a WASM bundle. The outer is the same WASM
``examples/nested_relay_task``; what's exercised here is:

- An ``EntityRef[NixContainerServiceDefn]`` survives the host→outer
  bridge and is usable as an argument to ``spawner.spawn`` from inside
  the outer.
- The host's ``SandboxSpawnerImpl.spawn`` correctly dispatches to
  ``_spawn_runner_nix_container`` when called from a sandbox (not just
  the host directly).
- The capability ref (``ServiceRef[NetworkHandleAuthorizerProto]``)
  bridges across two sandbox boundaries (host → WASM outer → Linux
  container) and the container can mint+redeem tokens against the
  same env-owned ``TransportConnectionServer``.

Sweeps over all supported container runtimes via the `container_runtime`
fixture (see `conftest.py`); see `test_nix_container_spawner.py` for the
slow-build + host-env caveats (same setup).
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
from fxtr_sandbox_types import NixContainerServiceDefn
from fxtr_sandbox_types.protocols import (
    NetworkHandleAuthorizerProto,
    SandboxConnectProto,
    SandboxSpawnerProto,
)
from fxtr_sandboxing.deprecated.prepare import prepare
from fxtr_sandboxing.proxy import TransportRoute
from fxtr_sandboxing.spawner import create_sandbox_environment
from fxtr_workdir_harness import prepare_workdir_repo

pytestmark = [pytest.mark.anyio, pytest.mark.nix_runtime]

EXAMPLES_DIR = Path(__file__).parent.parent.parent / "examples"
_TRANSPORTER_TEMPLATE = Path(__file__).parent / "_workdir_templates" / "container_transporter"
CACHE_DIR = Path(__file__).parent.parent / "tmp_files_gitignore" / "wheel_cache"


@service_protocol
class NestedRelayProto(Protocol):
    """Mirror of ``nested_relay_task.NestedRelayProto``."""

    async def relay_http_get(self, paths: list[str]) -> list[str]: ...


@dataclass(frozen=True)
class NestedRelayConfig:
    """Mirror of ``nested_relay_task.NestedRelayConfig``."""

    spawner_ref: ServiceRef[SandboxSpawnerProto]
    inner_bundle_ref: EntityRef[Any]
    authorizer_ref: ServiceRef[NetworkHandleAuthorizerProto]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _start_upstream(
    handler,
) -> tuple[aiohttp.web.AppRunner, str]:
    app = aiohttp.web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sock = site._server.sockets[0]  # type: ignore[union-attr]
    port = sock.getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


@pytest.mark.deadline(seconds=900)
async def test_nested_spawn_relay_http_get_container(
    tmp_path: Path, container_runtime: str
) -> None:
    """Outer (WASM) relays http_get to an inner (nix container); results come back wrapped."""

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
            prepared = await prepare_workdir_repo(_TRANSPORTER_TEMPLATE, tmp_path / "wd")
            outer_bundle = await prepare(EXAMPLES_DIR / "nested_relay_task")
            inner_defn = NixContainerServiceDefn(
                git_commit=prepared.head_commit,
                flake_attr="fxtr.services.transporter",
            ).store()
            async with (
                create_sandbox_environment(cache_dir=CACHE_DIR, git_repo_path=prepared.path) as env,
                FxtrScope(include=[env.host_scope]),
            ):
                authorizer_ref = await env.add_transport(route)

                config = NestedRelayConfig(
                    spawner_ref=env.spawner_ref,
                    inner_bundle_ref=inner_defn,
                    authorizer_ref=authorizer_ref,
                )

                spawner = await env.spawner_ref.resolve()
                untyped_sandbox_ref = await spawner.spawn(outer_bundle)
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
