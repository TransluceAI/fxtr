"""Shared host-side launcher for the pyodide-in-deno sandbox spike.

``open_deno_sandbox`` is the spike's stand-in for a future
``_spawn_runner_nix_pyodide`` spawner arm, mirroring
``fxtr_sandboxing.direct_launcher.open_sandbox``'s body over a subprocess:

    UDS listener → `deno run main.ts` (locked-down permissions)
      → accept_or_exit → SocketJSONStream → wrap_json_stream
      → open_rpc_scope → bridge guest ref
      → _SpawnConnectWrapperImpl (sentinel bundle)
      → yields a ServiceRef[SandboxConnectProto[...]]

With ``transport_server=`` it also opens the multiplexed transport UDS and
serves it via ``transport_accept_loop`` — the same token-redemption side
channel the container runtime uses.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope, ServiceRef
from fxtr2.rpc.json_serdes import wrap_json_stream
from fxtr2.rpc.scope_bridge import open_rpc_scope
from fxtr2.services.service_cap_common import FxtrServiceData, UnknownService
from fxtr_sandbox_types import (
    DirectLauncherSentinel,
    SandboxConnectProto,
    SandboxGuestImplProto,
)
from fxtr_sandbox_types.socket_json_stream import SocketJSONStream
from fxtr_sandboxing._container_common import (
    accept_or_exit,
    teardown_container_proc,
    transport_accept_loop,
)
from fxtr_sandboxing.spawner import _SpawnConnectWrapperImpl
from fxtr_sandboxing.transport_server import TransportConnectionServer

SPIKE_DIR = Path(__file__).parent.resolve()
SITE_PACKAGES = SPIKE_DIR / "guest_site_packages_gitignore"


def _deno_cache_dir() -> str:
    out = subprocess.run(
        ["deno", "info", "--json"], check=True, capture_output=True, text=True
    ).stdout
    return json.loads(out)["denoDir"]


def _deno_cmd(sock_path: str, entry_point: str, transport_sock_path: str | None) -> list[str]:
    deno = shutil.which("deno")
    assert deno is not None, "deno not found on PATH"
    sock_paths = [sock_path] + ([transport_sock_path] if transport_sock_path else [])
    allow_read = ",".join([_deno_cache_dir(), str(SPIKE_DIR), str(SITE_PACKAGES), *sock_paths])
    allow_net = ",".join(f"unix:{p}" for p in sock_paths)
    return [
        deno,
        "run",
        "--no-prompt",
        f"--allow-read={allow_read}",
        f"--allow-write={','.join(sock_paths)}",
        f"--allow-net={allow_net}",
        str(SPIKE_DIR / "main.ts"),
        sock_path,
        entry_point,
        str(SITE_PACKAGES),
        *([transport_sock_path] if transport_sock_path else []),
    ]


@contextlib.asynccontextmanager
async def open_deno_sandbox(
    entry_point: str,
    *,
    transport_server: TransportConnectionServer | None = None,
) -> AsyncIterator[ServiceRef[SandboxConnectProto[FxtrServiceData, UnknownService]]]:
    """Spawn one pyodide-in-deno sandbox; yield its connect ref.

    Same caller shape as ``SandboxSpawnerProto.spawn`` /
    ``DirectSandboxEnvironment.open_sandbox``: ``replace_service_type`` to a
    concrete ``SandboxConnectProto[Cfg, Proto]``, ``resolve()``,
    ``connect(cfg)``. Teardown at CM exit: the call scope drains, the RPC
    scope drains (guest sees EOF and exits), then the
    graceful/terminate/kill ladder reaps the deno process.
    """
    assert (
        SITE_PACKAGES.is_dir()
    ), f"{SITE_PACKAGES} missing — run ./build_guest_site_packages.sh first"
    # Short tmpdir: macOS caps UDS paths at 104 bytes (SUN_LEN).
    spawn_dir = Path(tempfile.mkdtemp(prefix="fxtr-deno-"))
    sock_path = str(spawn_dir / "task.sock")
    transport_sock_path = str(spawn_dir / "transport.sock") if transport_server else None
    t0 = time.monotonic()

    try:
        listener = await anyio.create_unix_listener(sock_path)
        async with anyio.create_task_group() as tg:
            if transport_server is not None:
                assert transport_sock_path is not None
                transport_listener = await anyio.create_unix_listener(transport_sock_path)
                tg.start_soon(transport_accept_loop, transport_listener, transport_server)

            proc = await anyio.open_process(
                _deno_cmd(sock_path, entry_point, transport_sock_path),
                stdout=None,  # inherit for debugging
                stderr=None,
            )
            try:
                stream = await accept_or_exit(listener, proc)
                print(
                    f"[host] guest connected in {time.monotonic() - t0:.2f}s",
                    file=sys.stderr,
                )
                rpc_message_stream = wrap_json_stream(SocketJSONStream(stream))
                async with open_rpc_scope(
                    rpc_message_stream,
                    remote_service_type=SandboxGuestImplProto[FxtrServiceData, UnknownService],
                    name="deno_sandbox_rpc",
                ) as rpc_session:
                    raw_guest_ref = rpc_session.remote_service
                    async with FxtrScope(
                        name="deno_sandbox_call", include=[GLOBAL_SCOPE]
                    ) as call_scope:
                        await raw_guest_ref.bridge_from_scope(
                            rpc_session.scope,
                            into_scope=call_scope,
                            keepalive_via="independent_lease",
                        )
                        sentinel_ref = DirectLauncherSentinel().store(call_scope)
                        bundle_unk_ref: EntityRef[UnknownEntity] = EntityRef(
                            entity_id=sentinel_ref.entity_id, target_type=UnknownEntity
                        )
                        wrapper = _SpawnConnectWrapperImpl(
                            raw_guest_ref=raw_guest_ref, bundle=bundle_unk_ref
                        )
                        wrapper_ref = await call_scope.mount(wrapper)
                        yield wrapper_ref.replace_service_type(
                            SandboxConnectProto[FxtrServiceData, UnknownService]
                        )
            finally:
                await teardown_container_proc(proc)
                await listener.aclose()
                tg.cancel_scope.cancel()  # stop the transport accept loop
        print(
            f"[host] sandbox torn down, total {time.monotonic() - t0:.2f}s",
            file=sys.stderr,
        )
    finally:
        shutil.rmtree(spawn_dir, ignore_errors=True)
