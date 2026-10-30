"""Low-level launcher for an eryx sandbox.

Two entry points:

- :func:`run` — the bundle-driven path. Takes a :class:`TaskBundle`,
  per-spawn extracts its source + wheel packages into a tempdir, builds
  an :class:`eryx.SandboxFactory` over that tempdir (paying the ~3 s
  factory-build cost on every spawn), and delegates to
  :func:`run_in_factory`. Used by :class:`SandboxSpawnerImpl`.

- :func:`run_in_factory` — the lower-level path. Takes a pre-built
  :class:`eryx.SandboxFactory` (lifetime owned by the caller), an
  ``entry_point``, and optional per-spawn ``volumes`` /
  ``sys_path_additions``. The factory cost is paid once at construction
  time so per-spawn cost is just ``factory.create_sandbox`` (~10–20 ms).
  Used by :mod:`fxtr_sandboxing.direct_launcher` for the warm-spawn case.

Both yield a :class:`TaskSession` whose ``send`` / ``receive`` plumb
JSON-typed values through the eryx callback bridge. The sandbox executes
``fxtr_sandbox_rpc_adapter.main(ctx, entry_point=...)``; the RPC bridge
lives in the adapter. Higher layers
(:mod:`fxtr_sandboxing.spawner`, :mod:`fxtr_sandboxing.direct_launcher`)
wrap the session with :class:`JSONObjectStream` + ``open_rpc_scope``.

Network transports (stage 3) are wired through here as four byte-level
eryx callbacks (`_transport_connect`, `_transport_send`,
`_transport_recv`, `_transport_close`) plus a small async launcher
that wraps each new socketpair end as an `anyio.abc.ByteStream` and
hands it to the handler bound to the redeemed token. The token table
+ handler mapping live in the env-level
:class:`TransportConnectionServer`; this module just wires the
callbacks to it.
"""

from __future__ import annotations

import base64
import logging
import os
import pathlib
import queue
import shutil
import socket
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import anyio
import anyio.abc
import anyio.from_thread
import anyio.to_thread
import eryx
from fxtr_sandbox_types import TaskBundle

from .transport_server import TransportConnectionServer, TransportHandler

logger = logging.getLogger(__name__)

_ERYX_SANDBOX_RESOURCE_LIMITS = eryx.ResourceLimits(
    execution_timeout_ms=None,
    callback_timeout_ms=None,
    max_callback_invocations=None,
    max_memory_bytes=1024**3,
)

#: How long teardown waits for ``sandbox.execute`` to return after the
#: shutdown sentinel is sent, before abandoning its daemon thread with a
#: warning. The guest's session loop normally exits promptly on the
#: sentinel; only a wedged guest (e.g. blocked forever in a callback
#: whose host side was cancelled) exceeds this.
_SANDBOX_EXIT_GRACE_S = 5.0

# Path to the internal files that get injected into the sandbox
_INTERNAL_DIR = Path(__file__).parent

# JSON-compatible types that can be sent/received through the sandbox messaging API.
type JSONTypes = None | bool | int | float | str | list[JSONTypes] | dict[str, JSONTypes]


class _ShutdownSentinel:
    """Sentinel value used to unblock queues during shutdown."""


_SHUTDOWN = _ShutdownSentinel()

# Shim for eryx's limited socket module. anyio needs AddressFamily, SocketKind,
# and AF_UNSPEC to import; eryx has the raw constants but not the IntEnum wrappers.
_SOCKET_SHIM = """\
import socket as _socket
import enum as _enum
if not hasattr(_socket, 'AF_UNSPEC'):
    _socket.AF_UNSPEC = 0
if not hasattr(_socket, 'AddressFamily'):
    class AddressFamily(_enum.IntEnum):
        AF_UNSPEC = 0
        AF_INET = _socket.AF_INET
        AF_INET6 = _socket.AF_INET6
        AF_UNIX = _socket.AF_UNIX
    _socket.AddressFamily = AddressFamily
if not hasattr(_socket, 'SocketKind'):
    class SocketKind(_enum.IntEnum):
        SOCK_STREAM = _socket.SOCK_STREAM
        SOCK_DGRAM = _socket.SOCK_DGRAM
    _socket.SocketKind = SocketKind
del _enum
"""

# Shim for eryx's event loop. Patches _EryxLoop with methods that anyio needs
# for task groups, sleep, and timers. Requires _host_sleep and _host_cleanup_timers
# callbacks to be registered on the host side.
_LOOP_SHIM = """\
import asyncio as _asyncio
import contextvars as _contextvars
import time as _time

_loop = _asyncio.get_event_loop()

# anyio calls loop.get_task_factory() — eryx raises NotImplementedError.
# Returning None makes anyio fall back to loop.create_task().
_loop.get_task_factory = lambda: None

# anyio calls loop.call_soon() without a context kwarg in some paths.
# Eryx requires context to be non-None. Patch to use the current context.
_orig_call_soon = _loop.call_soon
def _patched_call_soon(callback, *args, context=None):
    if context is None:
        context = _contextvars.copy_context()
    return _orig_call_soon(callback, *args, context=context)
_loop.call_soon = _patched_call_soon

# anyio.sleep() needs loop.call_later(), which needs loop.time().
# Provide a monotonic clock and real call_later/call_at via _host_sleep.
_start_time = _time.monotonic()
_loop.time = lambda: _time.monotonic() - _start_time

_timer_tasks = set()

def _call_later(delay, callback, *args, context=None):
    if delay <= 0:
        return _patched_call_soon(callback, *args, context=context)
    if context is None:
        context = _contextvars.copy_context()
    handle = _asyncio.Handle(callback, args, _loop, context)
    async def _delayed():
        try:
            completed = await _host_sleep(seconds=delay)
        except _asyncio.CancelledError:
            return
        if handle._cancelled:
            return
        if not completed:
            return
        handle._run()
    task = _loop.create_task(_delayed())
    _timer_tasks.add(task)
    task.add_done_callback(_timer_tasks.discard)
    return handle
_loop.call_later = _call_later

def _call_at(when, callback, *args, context=None):
    delay = max(0, when - _loop.time())
    return _call_later(delay, callback, *args, context=context)
_loop.call_at = _call_at

async def _cleanup_timers():
    await _host_cleanup_timers()
    for task in list(_timer_tasks):
        try:
            await task
        except (_asyncio.CancelledError, Exception):
            pass


import _eryx, _eryx_async

def resume(event0: int, event1: int, event2: int) -> int:
    state = _eryx.context_get_()
    _eryx.context_set_(None)

    # Handle subtask completion
    if event0 == _eryx_async._EVENT_SUBTASK and event2 == _eryx_async._STATUS_RETURNED:
        _eryx.waitable_join_(event1, 0)
        _eryx.subtask_drop_(event1)
        fut = state.futures.pop(event1)
        # BUG FIX: don't set result on cancelled futures
        if fut.cancelled():
            print("WARNING: setting result on cancelled future; ignoring")
        else:
            fut.set_result(event2)
    elif event0 == _eryx_async._EVENT_NONE:
        pass  # Just poll again
    # Other events (streams, futures) would go here if we supported them

    return _eryx_async._poll(state)

_eryx_async.resume = resume
"""


class TaskSession:
    """Internal handle for the queue-based JSON message bridge to the sandbox.

    Not part of the public API — the spawner wraps this with `JSONObjectStream`
    and the fxtr RPC stack. Don't use it directly.
    """

    def __init__(
        self,
        h2s_queue: queue.Queue[JSONTypes | _ShutdownSentinel],
        s2h_queue: queue.Queue[JSONTypes | _ShutdownSentinel],
    ):
        self._h2s = h2s_queue
        self._s2h = s2h_queue

    async def send(self, data: JSONTypes) -> None:
        await anyio.to_thread.run_sync(self._h2s.put, data)

    async def receive(self) -> JSONTypes:
        msg = await anyio.to_thread.run_sync(self._s2h.get, abandon_on_cancel=True)
        if isinstance(msg, _ShutdownSentinel):
            raise RuntimeError("Sandbox has shut down")
        return msg


def _check_and_maybe_repair_eryx_embedded_dir() -> None:
    """Make sure that the $TMPDIR/eryx-embedded directory is in a valid state.

    Eryx's SandboxFactory errors out with a preinit error if the temporary eryx-embedded
    directory exists but is in an invalid state. This can happen if temporary files are purged to
    save space. In particular, if file contents are deleted but directory metadata is not, eryx
    will assume the directory is valid and will try to reuse it, leading to an error.

    This function checks if the directory exists but is invalid, and if so, clears it so that
    eryx can recreate it from scratch.
    """
    tmpdir = os.environ.get("TMPDIR")
    if not tmpdir:
        logger.warning("TMPDIR is not set, skipping check and repair of eryx-embedded directory")
        return

    extracted_stdlib_path = pathlib.Path(os.environ["TMPDIR"]) / "eryx-embedded" / "python-stdlib"
    if extracted_stdlib_path.is_dir():
        canary_file = extracted_stdlib_path / "encodings" / "__init__.py"
        if canary_file.exists():
            logger.info("Eryx-embedded directory appears valid, skipping repair")
            return
        else:
            logger.warning("Eryx-embedded directory appears invalid, clearing it")
            clear_path = (
                pathlib.Path(os.environ["TMPDIR"])
                / "eryx-embedded"
                / f"python-stdlib-old-{uuid.uuid4()}"
            )
            extracted_stdlib_path.rename(clear_path)
            shutil.rmtree(clear_path)


# Re-exported for back-compat: the implementation lives in
# `_bundle_materialize.py` (historically shared with the deleted
# py_container runner). direct_launcher.py and tests still import the
# name from here.
from .deprecated._bundle_materialize import build_site_packages_for_bundle  # noqa: F401, E402


@asynccontextmanager
async def run(
    bundle: TaskBundle,
    cache_dir: Path,
    *,
    transport_server: TransportConnectionServer | None = None,
) -> AsyncIterator[TaskSession]:
    """Launch a TaskBundle inside an eryx sandbox (per-spawn factory build).

    Builds a per-spawn :class:`eryx.SandboxFactory` over a tempdir
    materialized from ``bundle.packages``, then delegates to
    :func:`run_in_factory`. The factory build is the ~3 s ``pre_initialize``
    + ``precompile`` step — every spawn pays it. Direct-launcher callers
    avoid this by calling :func:`run_in_factory` against a long-lived
    factory.

    Args:
        bundle: The task bundle to execute.
        cache_dir: Directory for caching downloaded wheels.
        transport_server: See :func:`run_in_factory`.
    """
    # Caller-owned tempdir for the materialized site-packages. We don't
    # clean up at end of run() — the eryx factory needs the dir alive for
    # its lifetime, which is whatever the inner CM body decides. Matches
    # the prior behavior; a content-addressed cache would replace this.
    import tempfile as _tempfile

    site_packages_path = Path(_tempfile.mkdtemp(prefix="fxtr_site_packages_"))
    await build_site_packages_for_bundle(bundle, cache_dir, site_packages_path)
    _check_and_maybe_repair_eryx_embedded_dir()
    factory = eryx.SandboxFactory(site_packages=str(site_packages_path))
    async with run_in_factory(
        factory,
        entry_point=bundle.entry_point,
        transport_server=transport_server,
    ) as session:
        yield session


@asynccontextmanager
async def run_in_factory(
    factory: eryx.SandboxFactory,
    *,
    entry_point: str,
    volumes: list[tuple[str, str, bool]] = [],
    sys_path_additions: list[str] = [],
    transport_server: TransportConnectionServer | None = None,
) -> AsyncIterator[TaskSession]:
    """Launch a sandbox off a pre-built factory.

    Lower-level than :func:`run` — caller controls factory lifetime
    (built once, used many times) and decides how packages get into the
    sandbox: baked into the snapshot via ``packages=``/``imports=``,
    extracted into a stable site-packages via ``site_packages=``, or
    mounted per-spawn via ``volumes=``. Per-spawn extra mount points
    that should be on ``sys.path`` go through ``sys_path_additions``.

    Args:
        factory: Caller-owned factory. Survives across calls.
        entry_point: ``module:function`` string, same shape as
            ``TaskBundle.entry_point``. Resolved by the adapter.
        volumes: Extra host dirs to mount into the sandbox. Each entry
            is ``(host_path, guest_path, read_only)``: ``host_path`` is
            an absolute path on the host (string — eryx does not accept
            ``Path``); ``guest_path`` is where the dir appears inside
            the sandbox's WASI filesystem (e.g. ``"/local-pkgs"``);
            ``read_only`` is a bool. Forwarded to ``factory.create_sandbox``
            with ``resource_limits`` where ``execution_timeout_ms``,
            ``callback_timeout_ms``, ``max_callback_invocations``, and
            ``max_fuel`` are ``None``, and ``max_memory_bytes`` is 1 GiB
            (``1024 ** 3`` bytes).
        sys_path_additions: Guest paths (typically the ``guest_path``
            of one of ``volumes``) inserted at the front of ``sys.path``
            before the adapter runs. Use this when a volume-mounted dir
            contains importable packages that ``entry_point`` needs to
            reach.
        transport_server: Optional env-level token table. When provided,
            the four ``_transport_*`` callbacks are registered so the
            sandbox can call ``connect_transport(token)`` /
            ``get_client(authorizer_ref)`` to open byte-stream
            connections. When ``None``, transport callbacks aren't
            registered and any sandbox-side attempt to redeem a token
            raises a clear error (the contextvar machinery in the
            adapter delegates to ``ctx.open_transport_stream`` which
            this module only registers when the server is present).
    """
    # Set up bidirectional communication via blocking queues
    h2s_queue: queue.Queue[JSONTypes | _ShutdownSentinel] = queue.Queue()
    s2h_queue: queue.Queue[JSONTypes | _ShutdownSentinel] = queue.Queue()

    registry = eryx.CallbackRegistry()

    @registry.callback(description="Receive a JSON message from the host")
    def _receive_from_host() -> JSONTypes:
        msg = h2s_queue.get()
        if isinstance(msg, _ShutdownSentinel):
            raise RuntimeError("Sandbox shutting down")
        return msg  # type: ignore[return-value]

    @registry.callback(description="Send a JSON message to the host")
    def _send_to_host(data: JSONTypes) -> None:
        s2h_queue.put(data)
        return None

    # Host-side sleep callbacks for the loop shim. These let anyio.sleep() and
    # call_later/call_at work inside the eryx sandbox.
    active_sleep_events: dict[int, threading.Event] = {}

    @registry.callback(
        description="Sleep for specified seconds, returns True if completed naturally"
    )
    def _host_sleep(seconds: float) -> bool:
        event = threading.Event()
        eid = id(event)
        active_sleep_events[eid] = event
        try:
            interrupted = event.wait(timeout=seconds)
            return not interrupted
        finally:
            active_sleep_events.pop(eid, None)

    @registry.callback(description="Cancel all pending sleeps")
    def _host_cleanup_timers() -> None:
        for event in list(active_sleep_events.values()):
            event.set()
        deadline = time.monotonic() + 5.0
        while active_sleep_events and time.monotonic() < deadline:
            time.sleep(0.001)
        return None

    # Network-transport bridge. The sandbox-side talks raw bytes via four
    # blocking eryx callbacks (`_transport_connect`, `_transport_send`,
    # `_transport_recv`, `_transport_close`). On `_transport_connect`, we
    # validate the token against the env's TransportConnectionServer
    # (consume_token is a sync atomic pop, raises BadTransportTokenError
    # on bad / consumed token), create a socketpair, and queue the
    # host-side end + handler for the async launcher to pick up.
    connections: dict[str, socket.socket] = {}
    new_proxy_conns: queue.Queue[tuple[socket.socket, TransportHandler] | _ShutdownSentinel] = (
        queue.Queue()
    )

    if transport_server is not None:
        # Capture into a local for the closures below — sidesteps the
        # narrowing-vs-closure dance.
        _server = transport_server

        @registry.callback(description="Connect to a network transport via token")
        def _transport_connect(token: str) -> str:
            # Raises BadTransportTokenError on bad/consumed token; the
            # exception propagates back into the sandbox via the eryx
            # callback failure path.
            handler = _server.consume_token(token)
            callback_sock, proxy_sock = socket.socketpair()
            conn_id = uuid.uuid4().hex
            connections[conn_id] = callback_sock
            new_proxy_conns.put((proxy_sock, handler))
            return conn_id

        @registry.callback(description="Send bytes over a transport connection")
        def _transport_send(conn_id: str, data_b64: str) -> str:
            sock = connections[conn_id]
            sock.sendall(base64.b64decode(data_b64))
            return ""

        @registry.callback(description="Receive bytes from a transport connection")
        def _transport_recv(conn_id: str, max_bytes: int) -> str:
            sock = connections[conn_id]
            data = sock.recv(max_bytes)
            if not data:
                return ""
            return base64.b64encode(data).decode("ascii")

        @registry.callback(description="Half-close (write side) a transport connection")
        def _transport_send_eof(conn_id: str) -> str:
            sock = connections.get(conn_id)
            if sock is not None:
                # Shut down the write side of the host-side end of the
                # socketpair → the handler's read side sees EOF on its
                # next ``receive``. Idempotent at the socket level.
                try:
                    sock.shutdown(socket.SHUT_WR)
                except OSError:
                    # Already shut down or peer-closed — ignore.
                    pass
            return ""

        @registry.callback(description="Close a transport connection")
        def _transport_close(conn_id: str) -> str:
            sock = connections.pop(conn_id, None)
            if sock is not None:
                sock.close()
            return ""

    def _on_stdout(chunk: str) -> None:
        sys.stdout.write(chunk)

    def _on_stderr(chunk: str) -> None:
        sys.stderr.write(chunk)

    sandbox = factory.create_sandbox(
        callbacks=registry,
        volumes=list(volumes),
        resource_limits=_ERYX_SANDBOX_RESOURCE_LIMITS,
        on_stdout=_on_stdout,
        on_stderr=_on_stderr,
    )

    runner_parts = [_SOCKET_SHIM, _LOOP_SHIM]
    if sys_path_additions:
        runner_parts.append("import sys\n" f"sys.path[:0] = {list(sys_path_additions)!r}\n")
    runner_parts.append(f"_ENTRY_POINT = {entry_point!r}\n")
    runner_parts.append((_INTERNAL_DIR / "_runner.py").read_text())
    runner_code = "\n".join(runner_parts)

    session = TaskSession(h2s_queue, s2h_queue)

    # ``sandbox.execute`` is a blocking FFI call with no host-side way to
    # interrupt it; clean return relies on the guest's session loop
    # reading the in-band ``_SHUTDOWN`` sentinel (see the ``finally``
    # below). It runs on a dedicated *daemon* thread rather than anyio's
    # ``to_thread`` pool: pool workers are non-daemonic, so a cancelled
    # (abandoned) call whose guest never exits — e.g. wedged in a
    # callback whose host side was torn down by the same cancellation —
    # leaves a non-daemon thread that CPython joins forever at
    # interpreter exit (the process "finishes" but never terminates).
    # A daemon thread has identical abandonment semantics minus the exit
    # hang. The result is handed back to the event loop through a
    # BlockingPortal, the supported foreign-thread → loop channel.
    sandbox_thread: threading.Thread | None = None

    async def _run_sandbox() -> None:
        nonlocal sandbox_thread
        outcome: list[eryx.ExecuteResult | BaseException] = []
        finished = anyio.Event()

        async with anyio.from_thread.BlockingPortal() as portal:

            def _execute_in_thread() -> None:
                try:
                    outcome.append(sandbox.execute(runner_code))
                except BaseException as exc:  # noqa: BLE001 — re-raised on the loop side
                    outcome.append(exc)
                finally:
                    try:
                        portal.call(finished.set)
                    except RuntimeError:
                        # Portal already closed: we were cancelled and the
                        # guest exited later — nobody is waiting anymore.
                        pass

            sandbox_thread = threading.Thread(
                target=_execute_in_thread,
                name="eryx-sandbox-execute",
                daemon=True,
            )
            sandbox_thread.start()
            await finished.wait()

        result = outcome[0]
        if isinstance(result, BaseException):
            raise result
        if result.stderr:  # type: ignore[attr-defined]
            print(f"Sandbox stderr: {result.stderr}")  # type: ignore[attr-defined]

    async def _proxy_launcher() -> None:
        """Sync→async bridge for the eryx callback path.

        The eryx ``_transport_connect`` callback runs in a thread and
        can't directly wrap a raw socket as an
        ``anyio.abc.ByteStream`` (that requires ``await``), so it puts
        ``(proxy_sock, handler)`` on a queue and we pick it up here.
        After wrapping the socket we hand the stream to the handler
        synchronously — the handler takes ownership of the stream
        from there (typically ``start_soon``s its own async work on
        whatever task group it captured at construction).
        """
        while True:
            item = await anyio.to_thread.run_sync(new_proxy_conns.get, abandon_on_cancel=True)
            if isinstance(item, _ShutdownSentinel):
                break
            proxy_sock, handler = item  # type: ignore[misc]
            stream = await anyio.abc.UNIXSocketStream.from_socket(proxy_sock)
            handler(stream)

    try:
        async with anyio.create_task_group() as tg:
            if transport_server is not None:
                tg.start_soon(_proxy_launcher)
            tg.start_soon(_run_sandbox)
            yield session
            tg.cancel_scope.cancel()
    finally:
        h2s_queue.put(_SHUTDOWN)
        if sandbox_thread is not None and sandbox_thread.is_alive():
            # Bounded wait for the guest to read the sentinel and exit.
            # Shielded: this finally often runs under cancellation, and
            # the join itself always returns within the grace period.
            thread = sandbox_thread
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(lambda: thread.join(_SANDBOX_EXIT_GRACE_S))
            if thread.is_alive():
                logger.warning(
                    "eryx sandbox.execute did not return within %.1fs of the "
                    "shutdown sentinel; abandoning its daemon thread (guest "
                    "likely wedged, e.g. blocked in a callback whose host side "
                    "was cancelled). The call cannot be interrupted, but as a "
                    "daemon thread it no longer blocks interpreter exit.",
                    _SANDBOX_EXIT_GRACE_S,
                )
        s2h_queue.put(_SHUTDOWN)
        new_proxy_conns.put(_SHUTDOWN)

        for sock in list(connections.values()):
            sock.close()
        connections.clear()
