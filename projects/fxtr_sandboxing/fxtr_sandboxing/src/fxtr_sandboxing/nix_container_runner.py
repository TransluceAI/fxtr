"""Spawner-internal **runner** for ``NixContainerServiceDefn`` images.

Owned by :mod:`fxtr_sandboxing.spawner` — callers don't drive these
directly. Delivery (loading a built image into the runtime's image store)
is the nix-runner backend's job (``fxtr_nix_runner.Handle.deliver_image``,
which returns the ``image_ref`` + ``--platform``); this module only
**runs** an already-delivered image. Two pieces:

1. :func:`_run_nix_container_internal` — the run CM:
   no entity loads, no source mounts, no env vars, **no trailing command**
   (the entry point is baked into the image's ``Cmd``). Opens the
   per-spawn task + transport UDSes, runs the container with the
   backend-supplied ``--platform`` (runtimes resolve a loaded image index
   against the *host* platform and won't fall back to the sole variant, so
   the flag is mandatory for emulated arches), accepts the guest's
   task-socket connection, and yields it. When podman is **remote**
   (machine VM), the host sockets can't be reached through ``-v``
   socket-file mounts, so the run rides a per-spawn sshd-sidecar pod from
   :mod:`fxtr_sandboxing._podman` instead; the guest contract is unchanged
   (same socket paths, now on a shared volume).

2. Image-store cache management: :func:`clear_image_cache` /
   :func:`fxtr_sandboxing._podman.clear_spawn_debris`, exposed as
   ``python -m fxtr_sandboxing.nix_container_runner
   {clear-images,clear-spawn-debris}``.

The UDS / accept-race / teardown scaffolding comes from
:mod:`fxtr_sandboxing._container_common`.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import AsyncExitStack, asynccontextmanager
from typing import AsyncIterator

import anyio
import anyio.abc
from fxtr_nix_runner import ContainerRuntime, _delivery
from fxtr_nix_runner.perf import perf_span, proc_label

from ._container_common import (
    accept_or_exit,
    get_container_runtime,
    teardown_container_proc,
    transport_accept_loop,
)
from ._podman import (
    ROLE_LABEL_KEY,
    SIDECAR_IMAGE_REPO,
    clear_spawn_debris,
    open_sidecar_tunnel,
    podman_is_remote,
)
from .transport_server import TransportConnectionServer


async def _run_checked(cmd: list[str], *, what: str) -> str:
    """Run a host command; return stdout, or raise ``RuntimeError``."""
    async with perf_span(proc_label(cmd)):
        result = await anyio.run_process(cmd, check=False)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace")
        raise RuntimeError(f"{what} failed (exit {result.returncode}):\n{stderr}")
    return result.stdout.decode("utf-8", "replace")


@asynccontextmanager
async def _run_nix_container_internal(
    image_ref: str,
    platform: str,
    *,
    transport_server: TransportConnectionServer | None = None,
) -> AsyncIterator[anyio.abc.SocketStream]:
    """Run an already-delivered nix container image and yield its task-socket
    connection.

    Spawner-internal, **run-only**: delivery (loading the image into the
    runtime's store) is the nix-runner backend's job
    (``Handle.deliver_image``), which returns the ``image_ref`` + ``platform``
    passed here. The yielded stream is the byte channel between the host and
    the in-container ``fxtr_service_runner`` shim (which the image's baked
    ``Cmd`` launches); the spawner wraps it in ``SocketJSONStream`` to layer
    JSON framing + the fxtr2 RPC stack.
    """
    runtime = get_container_runtime()

    # Tmpdir for the per-spawn UDS sockets. World-readable mode so the
    # container can connect even if it runs as a different uid.
    socket_dir = tempfile.mkdtemp(prefix="fxtr_socket_")
    socket_path = os.path.join(socket_dir, "task.sock")
    task_listener = await anyio.create_unix_listener(socket_path, mode=0o777)

    transport_listener: anyio.abc.Listener[anyio.abc.SocketStream] | None = None
    transport_socket_path: str | None = None
    if transport_server is not None:
        transport_socket_path = os.path.join(socket_dir, "transport.sock")
        transport_listener = await anyio.create_unix_listener(transport_socket_path, mode=0o777)

    container_proc: anyio.abc.Process | None = None
    socket_stream: anyio.abc.SocketStream | None = None
    task_listener_closed = False
    # Holds the sidecar tunnel (when used). Closed manually in the
    # finally block *after* the container teardown ladder: pod rm -f
    # SIGKILLs pod members, so the tunnel must outlive the guest's
    # graceful EOF-driven exit.
    tunnel_stack = AsyncExitStack()
    try:
        # No trailing command on either branch: the image's Cmd is
        # the service runner.
        cmd = [runtime.cmd, "run", "--rm", "--platform", platform]
        async with perf_span("container.runtime_detect"):
            is_remote = runtime.type == "podman" and await podman_is_remote(runtime.cmd)
        if is_remote:
            # Remote podman (machine VM): host sockets aren't
            # reachable via -v socket-file mounts — ride the
            # sshd-sidecar pod. Network config (none) belongs to
            # the pod, and the guest finds the forwarded sockets
            # on the shared volume at the same /run/fxtr paths.
            socket_names = {"task.sock": socket_path}
            if transport_socket_path is not None:
                socket_names["transport.sock"] = transport_socket_path
            async with perf_span("container.sidecar_tunnel"):
                extra_args = await tunnel_stack.enter_async_context(
                    open_sidecar_tunnel(runtime.cmd, socket_names)
                )
            cmd.extend(extra_args)
        else:
            # Local kernel (native-Linux podman or the escape-hatch
            # runtimes): mount the socket files individually
            # (sockets inside virtiofs *directory* mounts return
            # ENOTSUP on connect under some runtimes).
            cmd.extend(["--network", "none"])
            cmd.extend(["-v", f"{socket_path}:/run/fxtr/task.sock"])
            if transport_socket_path is not None:
                cmd.extend(["-v", f"{transport_socket_path}:/run/fxtr/transport.sock"])
        cmd.append(image_ref)

        # Inherit host stdout/stderr so guest debug output flows naturally.
        # Time from launch to the guest connecting on the task socket — this
        # span covers container boot + the in-container fxtr_service_runner
        # Python/fxtr2 import, so a big number here points at guest startup,
        # not host orchestration.
        async with perf_span("container.run_and_accept"):
            container_proc = await anyio.open_process(cmd, stdin=None, stdout=None, stderr=None)

            # Race the container connecting against an early exit (bad image,
            # runtime mis-config) so we fail fast instead of hanging on accept.
            socket_stream = await accept_or_exit(task_listener, container_proc)
        # One connection per spawn — drop the listener fd now.
        await task_listener.aclose()
        task_listener_closed = True

        async with anyio.create_task_group() as tg:
            if transport_listener is not None:
                assert transport_server is not None  # narrowing for pyright
                tg.start_soon(transport_accept_loop, transport_listener, transport_server)

            yield socket_stream

            tg.cancel_scope.cancel()
    finally:
        async with perf_span("container.teardown"):
            if socket_stream is not None:
                await socket_stream.aclose()
            if not task_listener_closed:
                await task_listener.aclose()
            if container_proc is not None:
                await teardown_container_proc(container_proc)
            if transport_listener is not None:
                await transport_listener.aclose()
            # Only now is it safe to kill the tunnel + pod rm -f + volume rm.
            await tunnel_stack.aclose()

        import shutil

        shutil.rmtree(socket_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Cache management CLI
# ---------------------------------------------------------------------------


async def clear_image_cache(runtime: ContainerRuntime | None = None) -> list[str]:
    """Remove all delivered service images + sidecar images.

    Name-based pruning of the ``fxtr-nix-service`` and
    ``fxtr-sshd-sidecar`` repositories in the runtime's image store
    (the delivery cache), plus — under podman — a sweep of **dangling**
    fxtr-built images by the baked ``fxtr-role`` label: concurrent
    first-builds of the sidecar race on the final ``-t`` retag, and
    every loser keeps existing as an untagged ``<none>`` image that
    name-based listing can't see (the label, living in image content,
    survives untagging). A subsequent spawn re-delivers/re-builds.
    Returns the removed image ids for reporting.
    """
    runtime = runtime if runtime is not None else get_container_runtime()
    is_podman = runtime.type == "podman"
    repos = [f"localhost/{_delivery.IMAGE_REPO}" if is_podman else _delivery.IMAGE_REPO]
    if is_podman:
        repos.append(SIDECAR_IMAGE_REPO)
    removed: list[str] = []

    async def _rmi_listed(list_cmd: list[str]) -> None:
        out = await anyio.run_process(list_cmd, check=False)
        image_ids = dict.fromkeys(out.stdout.decode().split())  # dedupe, keep order
        for image_id in image_ids:
            await _run_checked([runtime.cmd, "rmi", "-f", image_id], what=f"rmi {image_id}")
            removed.append(image_id)

    for repo in repos:
        await _rmi_listed([runtime.cmd, "images", repo, "-q"])
    if is_podman:
        await _rmi_listed(
            [
                runtime.cmd,
                "images",
                "--filter",
                f"label={ROLE_LABEL_KEY}",
                "--filter",
                "dangling=true",
                "-q",
            ]
        )
    return removed


def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m fxtr_sandboxing.nix_container_runner",
        description="Cache management for delivered nix container images.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("clear-images", help="remove all fxtr-nix-service + sidecar images")
    sub.add_parser("clear-spawn-debris", help="remove orphaned per-spawn pods/volumes")
    args = parser.parse_args()

    async def _main() -> None:
        runtime = get_container_runtime()
        if args.command == "clear-images":
            removed = await clear_image_cache(runtime)
        else:
            removed = await clear_spawn_debris(runtime.cmd)
        for item in removed:
            print(item)
        print(f"removed {len(removed)} object(s)")

    anyio.run(_main)


if __name__ == "__main__":
    _cli()
