"""Runner for ``NixPyodideServiceDefn`` guests: pyodide CPython-wasm inside a
permission-locked deno subprocess on the host (spawner-internal).

The process-model sibling of :mod:`.nix_container_runner`, minus containers:
the host opens a per-spawn task UDS (plus the multiplexed transport UDS when
a :class:`TransportConnectionServer` is configured), launches ``deno run``
on the host-trusted driver (``fxtr_pyodide_runner``'s ``main.ts``) with a
minimal static permission profile, and races accept against early exit. The
driver narrows permissions further at runtime (grant-then-drop) before any
flake-supplied code executes; see the driver's header comment.

Static permission profile (the security boundary — everything the guest can
ever touch):

- ``--allow-read``: the runtime bundle, the site-packages dir, the socket
  paths. Nothing else — no deno cache (pyodide is imported from the bundle
  by file path), no env, no run, no ffi, no write except the sockets.
- ``--allow-write`` / ``--allow-net=unix:...``: the socket paths only
  (deno gates unix-socket connect on all three).

Deno itself comes from the host PATH — an optional host dependency; only
the pyodide kind needs it.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import anyio.abc
from fxtr_nix_runner.perf import perf_span, proc_label

from ._container_common import accept_or_exit, teardown_container_proc, transport_accept_loop
from .transport_server import TransportConnectionServer


def _find_deno() -> str:
    deno = shutil.which("deno")
    if deno is None:
        raise RuntimeError(
            "spawn(NixPyodideServiceDefn): `deno` not found on PATH. The "
            "pyodide sandbox backend runs guests inside a deno subprocess; "
            "install deno (https://docs.deno.com/runtime/getting_started/"
            "installation/) or use a wasm/container-kind service instead."
        )
    return deno


def _deno_cmd(
    *,
    task_socket: str,
    entry_point: str,
    site_packages: Path,
    runtime_bundle: Path,
    transport_socket: str | None,
    v8_flags: str | None,
) -> list[str]:
    import fxtr_pyodide_runner

    sock_paths = [task_socket] + ([transport_socket] if transport_socket else [])
    driver_dir = fxtr_pyodide_runner.driver_main_ts_path().parent
    allow_read = ",".join([str(driver_dir), str(runtime_bundle), str(site_packages), *sock_paths])
    cmd = [
        _find_deno(),
        "run",
        "--no-prompt",
        f"--config={fxtr_pyodide_runner.driver_config_path()}",
        f"--allow-read={allow_read}",
        f"--allow-write={','.join(sock_paths)}",
        f"--allow-net={','.join(f'unix:{p}' for p in sock_paths)}",
    ]
    if v8_flags:
        cmd.append(f"--v8-flags={v8_flags}")
    cmd += [
        str(fxtr_pyodide_runner.driver_main_ts_path()),
        task_socket,
        entry_point,
        str(site_packages),
        str(runtime_bundle),
    ]
    if transport_socket:
        cmd.append(transport_socket)
    return cmd


@contextlib.asynccontextmanager
async def _run_pyodide_internal(
    *,
    entry_point: str,
    site_packages: Path,
    runtime_bundle: Path,
    transport_server: TransportConnectionServer | None = None,
    v8_flags: str | None = None,
) -> AsyncIterator[anyio.abc.SocketStream]:
    """Launch one pyodide guest; yield the accepted task-socket stream.

    Teardown mirrors the container runner: the caller closes / drains the
    RPC connection first (the guest sees EndOfStream and exits; the driver's
    EOF backstop hard-exits a wedged guest), then the graceful → terminate →
    kill ladder reaps the deno process.

    ``v8_flags`` is a best-effort resource knob passed straight to deno's
    ``--v8-flags`` (e.g. ``"--max-old-space-size=512"``). It bounds the V8
    heap; wasm linear memory (the Python heap) is allocated through V8 but
    hard caps are not guaranteed — treat it as a soft limit, eryx-parity.
    """
    # Short tmpdir: macOS caps UDS paths at 104 bytes (SUN_LEN).
    spawn_dir = Path(tempfile.mkdtemp(prefix="fxtr-pyodide-"))
    task_socket = str(spawn_dir / "task.sock")
    transport_socket = str(spawn_dir / "transport.sock") if transport_server else None

    try:
        task_listener = await anyio.create_unix_listener(task_socket)
        transport_listener = None
        if transport_server is not None:
            assert transport_socket is not None
            transport_listener = await anyio.create_unix_listener(transport_socket)

        cmd = _deno_cmd(
            task_socket=task_socket,
            entry_point=entry_point,
            site_packages=site_packages,
            runtime_bundle=runtime_bundle,
            transport_socket=transport_socket,
            v8_flags=v8_flags,
        )

        async with anyio.create_task_group() as tg:
            if transport_listener is not None:
                assert transport_server is not None
                tg.start_soon(transport_accept_loop, transport_listener, transport_server)

            # stdout/stderr inherit: guest prints land on the host's
            # streams (stdout is deliberately left to user code). The span
            # covers deno start + pyodide boot + guest import + dial-back.
            proc = await anyio.open_process(cmd, stdout=None, stderr=None)
            try:
                async with perf_span("pyodide.run_and_accept", proc=proc_label(cmd)):
                    stream = await accept_or_exit(task_listener, proc)
                try:
                    yield stream
                finally:
                    await stream.aclose()
            finally:
                await teardown_container_proc(proc)
                await task_listener.aclose()
                if transport_listener is not None:
                    await transport_listener.aclose()
                tg.cancel_scope.cancel()  # stop the transport accept loop
    finally:
        shutil.rmtree(spawn_dir, ignore_errors=True)
