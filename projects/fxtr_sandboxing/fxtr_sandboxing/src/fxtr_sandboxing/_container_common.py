"""Container-runner scaffolding for :mod:`fxtr_sandboxing.nix_container_runner`.

Container-runtime command discovery, the UDS-accept race against
early-exit, the transport-socket multiplexer accept loop, and the
graceful-terminate-kill teardown ladder. (Historically shared with the
deleted ``container_runner`` / ``py_container_runner`` — hence the
split from the runner module.)
"""

from __future__ import annotations

from typing import Final, cast

import anyio
import anyio.abc
from fxtr_nix_runner import ContainerRuntime, load_config
from fxtr_sandbox_types.transport_routing import read_routing_header, write_routing_response

from .transport_server import BadTransportTokenError, TransportConnectionServer

# How long to wait for the container to exit cleanly after the
# ``open_rpc_scope`` drain triggers EOF. Containers should return
# promptly on EndOfStream — anything longer suggests user code is
# blocking inside the entry point.
GRACEFUL_TEARDOWN_S: Final = 5.0
TERMINATE_TEARDOWN_S: Final = 5.0


def get_container_runtime() -> ContainerRuntime:
    """Return the resolved container runtime (``cmd`` + concrete ``type``).

    Resolved from the unified config (``fxtr_nix_runner.load_config`` —
    ``nix_runtime.toml`` + the ``FXTR_CONTAINER_RUNTIME_CMD`` /
    ``FXTR_CONTAINER_RUNTIME_TYPE`` env). There is **no implicit default**: an
    unconfigured ``container_runtime_cmd`` raises ``ConfigError`` pointing at
    ``fxtr_nix runtime setup``.

    **podman is the recommended runtime.** ``container_runtime_type`` selects
    the behavior family: ``podman`` (tarless socket delivery + socket-file
    mounts) vs ``docker_compatible`` (the docker-CLI subset + tar ``image
    load``; covers **Apple Container** — a supported, swept non-default
    runtime — and the untested ``docker`` escape hatch). The live path uses
    only the docker-CLI-portable subset (``run --rm --network none -v
    --platform``, ``image inspect``).
    """
    return load_config().require_container_runtime()


async def accept_or_exit(
    listener: anyio.abc.SocketListener,
    container_proc: anyio.abc.Process,
) -> anyio.abc.SocketStream:
    """Race ``listener.accept()`` against the container's exit.

    If the container connects, return the SocketStream. If the
    container exits before connecting, raise :class:`RuntimeError`
    with the exit code so callers see a fast, actionable error
    instead of hanging forever on accept().

    Implementation: spawn both tasks under one task group; whichever
    completes first records its result and cancels the group. The
    outer scope reads the result after the group exits.
    """
    # Casts widen the narrowing pyright infers from `= None`; without them
    # pyright doesn't track the closure mutations and the post-task-group
    # `if accepted is not None` branch looks unreachable.
    accepted = cast("anyio.abc.SocketStream | None", None)
    exited_rc = cast("int | None", None)

    async with anyio.create_task_group() as tg:

        async def _accept_task() -> None:
            nonlocal accepted
            accepted = await listener.accept()
            tg.cancel_scope.cancel()

        async def _watch_proc() -> None:
            nonlocal exited_rc
            exited_rc = await container_proc.wait()
            tg.cancel_scope.cancel()

        tg.start_soon(_accept_task)
        tg.start_soon(_watch_proc)

    if accepted is not None:
        return accepted
    raise RuntimeError(
        "Container exited before connecting to the task socket "
        f"(exit code {exited_rc}). Check the container's stdout/stderr "
        "for the underlying error — common causes are an image whose "
        "entry point crashed on startup, a socket mount the guest "
        "can't reach, or a runtime that can't emulate the requested "
        "--platform."
    )


# ---------------------------------------------------------------------------
# Transport multiplexer: token-keyed accept loop
# ---------------------------------------------------------------------------


async def handle_transport_conn(
    conn: anyio.abc.SocketStream,
    transport_server: TransportConnectionServer,
) -> None:
    """Handle one connection on the multiplexed transport socket.

    Reads the routing header (``{"token": "..."}``), validates the
    token against ``transport_server``, writes back the routing-header
    response (``{"ok": true}`` / ``{"ok": false, "error": ...}``), and
    on success hands the connection to the token's handler. The
    handler takes ownership of the stream from there.

    On any failure (malformed header, bad/consumed token), writes the
    error response and closes the connection. We don't use
    ``async with conn`` here because on the success path the handler
    owns the close — but we *do* close on every failure path.
    """
    try:
        try:
            token = await read_routing_header(conn)
        except (anyio.EndOfStream, anyio.ClosedResourceError, ValueError) as e:
            await write_routing_response(conn, ok=False, error=str(e))
            await conn.aclose()
            return

        try:
            handler = transport_server.consume_token(token)
        except BadTransportTokenError as e:
            await write_routing_response(conn, ok=False, error=str(e))
            await conn.aclose()
            return

        await write_routing_response(conn, ok=True)
        handler(conn)
    except BaseException:
        # Defensive: if anything explodes before the handler took
        # ownership, make sure the connection isn't leaked.
        try:
            await conn.aclose()
        except BaseException:  # noqa: BLE001 — double-fault on close is best-effort
            pass
        raise


async def transport_accept_loop(
    listener: anyio.abc.Listener[anyio.abc.SocketStream],
    transport_server: TransportConnectionServer,
) -> None:
    """Accept connections on the transport socket and handle each one."""
    await listener.serve(lambda conn: handle_transport_conn(conn, transport_server))


# ---------------------------------------------------------------------------
# Teardown handshake
# ---------------------------------------------------------------------------


async def teardown_container_proc(container_proc: anyio.abc.Process) -> None:
    """Graceful-terminate-kill ladder for a container subprocess.

    Caller invokes this from the runner's ``finally`` block after the
    task socket has been closed. ``open_rpc_scope``'s normal exit drains
    the conn, so the container's ``fxtr_service_runner`` has already
    seen EndOfStream and is on its way out — the graceful wait usually
    returns immediately.

    Ladder:
      1. ``wait()`` with ``GRACEFUL_TEARDOWN_S`` deadline — happy path.
      2. ``terminate()`` then ``wait()`` with ``TERMINATE_TEARDOWN_S`` deadline.
      3. ``kill()`` then ``wait()`` (unbounded).
    """
    with anyio.move_on_after(GRACEFUL_TEARDOWN_S) as scope:
        await container_proc.wait()
    if not scope.cancelled_caught:
        return
    container_proc.terminate()
    with anyio.move_on_after(TERMINATE_TEARDOWN_S) as scope:
        await container_proc.wait()
    if not scope.cancelled_caught:
        return
    container_proc.kill()
    await container_proc.wait()
