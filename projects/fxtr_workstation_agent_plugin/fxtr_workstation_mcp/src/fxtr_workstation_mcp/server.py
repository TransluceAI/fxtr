"""FastMCP server entry for ``fxtr_workstation_mcp``.

Tool surface (stages 1 + 2):

- ``set_working_dir(path)`` — point at a directory containing
  ``.fxtr_workstation.toml`` and ``preamble.py``. Required before
  any other tool.
- ``connect()`` — start the workstation connection supervisor; opens
  the ``/rpc`` and ``/client`` connections and (if the toml has
  ``active_session`` set) subscribes to that session.
- ``open_session(metadata=None)`` — submit an open-session job;
  persist the new UUID to the toml.
- ``close_session()`` — submit a close-session job; clear the toml.
- ``run_script(script)`` — execute Python in a fresh sandbox.
  Sandbox builtins (stage 2): ``lookup_entity``, ``get_service``,
  ``get_id_for_service``, ``ServiceRefStub``.

Tools are thin: they translate the MCP-shaped call into the
appropriate supervisor submit method, mutate the toml as needed,
and turn errors into stringified tool results.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import logging
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import anyio
from fxtr_nix_runner import resolve_backend
from mcp.server.fastmcp import Context, FastMCP

from collections.abc import Mapping

from .app_context import AppContext
from .channel import CHANNEL_CAPABILITY, make_channel_notifier
from .config import (
    CONFIG_FILENAME,
    ConfigError,
    WorkstationConfig,
    read_active_session,
    write_active_session,
)
from .exception_log import ExceptionLog
from .outgoing_buffer import OutgoingBuffer
from .sandbox_env import SandboxEnvSupervisor
from .sandbox_factory import build_runner_volumes
from .ws_connection import (
    ConnectionLost,
    NoActiveSession,
    SessionAlreadyActive,
    SupervisorNotReady,
    WorkstationConnection,
    force_close_session_one_shot,
)

_log = logging.getLogger(__name__)

# Wheel cache lives under the package so it survives across MCP server
# runs (helpful for fast subsequent ``run_script`` startup) and across
# pytest invocations (the bootstrap is the dominant cost in stage-2
# integration tests).
_PKG_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CACHE_DIR = _PKG_PROJECT_ROOT / ".cache" / "wheel_cache"

# --- Verbose I/O logging (debug aid) -----------------------------------
#
# When ``FXTR_WORKSTATION_MCP_VERBOSE`` is set to a truthy value, every
# tool call (inputs) and every tool result + every debounced outgoing
# channel frame is written to stderr inside a fenced block. The MCP's
# stderr is captured by Claude Code into its debug log, so this gives
# a faithful replay of what the agent sent us and what we sent back.
#
# Off by default — no overhead, no log noise.

_VERBOSE_IO = os.environ.get("FXTR_WORKSTATION_MCP_VERBOSE", "").strip().lower() not in (
    "",
    "0",
    "false",
    "no",
)


def _verbose_log(label: str, body: str) -> None:
    """Write ``body`` to stderr inside a labeled fenced block. No-op
    when verbose mode is off."""
    if not _VERBOSE_IO:
        return
    out = sys.stderr
    out.write(f"\n=== {label} ===\n")
    out.write(body)
    if not body.endswith("\n"):
        out.write("\n")
    out.write(f"=== end {label} ===\n")
    out.flush()


def _verbose_log_call(tool_name: str, **inputs: Any) -> None:
    """Log a tool call's inputs. Multi-line values (like
    ``run_script``'s ``script``) are emitted with newlines
    preserved; everything else is ``repr()``'d."""
    if not _VERBOSE_IO:
        return
    lines: list[str] = []
    for k, v in inputs.items():
        if isinstance(v, str) and "\n" in v:
            lines.append(f"{k}:")
            lines.append(v.rstrip("\n"))
        else:
            lines.append(f"{k}={v!r}")
    _verbose_log(f"[{tool_name}] call", "\n".join(lines))


def _verbose_log_result(tool_name: str, result: str) -> None:
    """Log a tool's return string (including any drained
    notifications appended by ``_combine``)."""
    if not _VERBOSE_IO:
        return
    _verbose_log(f"[{tool_name}] result", result)


_TOOL_RESULT = TypeVar("_TOOL_RESULT")


def logged_tool(
    name: str,
) -> Callable[
    [Callable[..., Awaitable[_TOOL_RESULT]]],
    Callable[..., Awaitable[_TOOL_RESULT]],
]:
    """Wrap an MCP tool body with the verbose-IO logging helpers.

    Apply inside ``@mcp.tool()`` so FastMCP wraps the logged
    version. The decorator preserves the wrapped function's
    signature via :func:`functools.wraps` (FastMCP uses
    ``inspect.signature`` and ``typing.get_type_hints`` for tool
    schema generation — both follow ``__wrapped__`` /
    ``__annotations__`` which ``functools.wraps`` populates).

    Behavior:

    - **Entry** — bind ``(*args, **kwargs)`` against the wrapped
      function's signature, drop ``ctx`` (it's the FastMCP
      :class:`Context` object, not interesting to log), pass the
      rest to :func:`_verbose_log_call`.
    - **Success** — log the result via
      :func:`_verbose_log_result`.
    - **BaseException** — log a ``[{name}] raised`` block with
      ``f"{type(exc).__name__}: {exc!r}"`` (no traceback —
      type+repr is what the manual interrupt test needs), then
      re-raise. Covers both regular exceptions and cancellation /
      ``SystemExit``.

    No-op fast path: when ``_VERBOSE_IO`` is off, the wrapper
    skips the sig-bind entirely and just awaits ``fn`` — so the
    off-path is genuinely free.
    """

    def decorator(
        fn: Callable[..., Awaitable[_TOOL_RESULT]],
    ) -> Callable[..., Awaitable[_TOOL_RESULT]]:
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> _TOOL_RESULT:
            if not _VERBOSE_IO:
                return await fn(*args, **kwargs)
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            loggable = {k: v for k, v in bound.arguments.items() if k != "ctx"}
            _verbose_log_call(name, **loggable)
            try:
                result = await fn(*args, **kwargs)
            except BaseException as exc:
                _verbose_log(f"[{name}] raised", f"{type(exc).__name__}: {exc!r}")
                raise
            # ``_verbose_log_result`` types ``result`` as ``str``
            # (every tool callable here returns ``str``). The
            # ``cast`` keeps the decorator generic for future
            # non-str return types without complicating the call
            # site.
            _verbose_log_result(name, result)  # type: ignore[arg-type]
            return result

        return wrapper

    return decorator


@contextlib.asynccontextmanager
async def make_app_ctx(
    *,
    sandbox_env: SandboxEnvSupervisor | None = None,
) -> AsyncIterator[AppContext]:
    """Construct an :class:`AppContext` for one MCP session-lifetime.

    Owns the lifespan task group; if a :class:`WorkstationConnection`
    was started by ``connect``, this is also where its supervisor
    task lives. On exit we signal shutdown and let the TG await the
    supervisor's drain.

    Also constructs the lifespan-scoped :class:`OutgoingBuffer`
    (in ``app_ctx.buffer``) before yielding, so every tool
    callable can rely on its presence. The buffer's
    ``notify_channel`` is the existing raw channel notifier
    bound to whichever session was most recently captured.

    ``sandbox_env`` (test-only knob):
        Inject a pre-built :class:`SandboxEnvSupervisor` so callers
        can share one bootstrapped eryx factory across many
        :class:`AppContext` lifetimes. When set, the context exit
        does **not** call ``shutdown()`` on the supplied env — the
        supplier owns its lifetime. When ``None`` (the production
        default), ``run_script`` lazy-bootstraps a fresh supervisor
        on first use and the lifespan exit shuts it down.
    """
    async with anyio.create_task_group() as tg:
        app_ctx = AppContext(task_group=tg)
        raw_channel = make_channel_notifier(lambda: app_ctx.latest_session)
        if _VERBOSE_IO:
            # Wrap the raw channel sender so every debounced flush
            # frame is mirrored to stderr just before it goes out
            # over the agent's stdio. Inline drains (events folded
            # into tool result strings) are caught by
            # ``logged_tool``'s result-log step instead.
            async def _logged_channel(content: str, attrs: Mapping[str, str]) -> None:
                _verbose_log(
                    "outgoing channel frame",
                    f"attrs={dict(attrs)!r}\ncontent:\n{content}",
                )
                await raw_channel(content, attrs)

            buffer_notify = _logged_channel
        else:
            buffer_notify = raw_channel
        app_ctx.buffer = OutgoingBuffer(
            notify_channel=buffer_notify,
            task_group=tg,
        )
        app_ctx.exception_log = ExceptionLog()
        if sandbox_env is not None:
            app_ctx.sandbox_env = sandbox_env
        # Track whether the caller supplied the env; the exit path
        # only shuts down envs we built ourselves.
        env_was_injected = sandbox_env is not None
        try:
            yield app_ctx
        finally:
            if app_ctx.sandbox_env is not None and not env_was_injected:
                await app_ctx.sandbox_env.shutdown()
            if app_ctx.connection is not None:
                await app_ctx.connection.shutdown()
            tg.cancel_scope.cancel()


@contextlib.asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncIterator[AppContext]:
    """FastMCP lifespan hook: thin delegate to :func:`make_app_ctx`
    with no pre-built sandbox env (production path).
    """
    async with make_app_ctx() as app_ctx:
        yield app_ctx


mcp = FastMCP(
    name="workstation_mcp",
    instructions=(
        "Drive a fxtr workstation session. Workflow: "
        "`set_working_dir(<path>)` → `connect()` → "
        "`open_session()` → `run_script(...)`. "
        "Asynchronous events (slot promotions, callbacks firing) "
        'appear as `<event slot_key="..." kind="...">summary</event>` '
        "blocks: inside a tool's response when an event lands during "
        "that tool's execution, or in a "
        '`<channel source="workstation_mcp" kind="events">…</channel>` '
        "frame when you're idle. Read them and call "
        "`retrieve_result(slot_key)` or "
        "`retrieve_callback_notification(slot_key, notif_index)` "
        "from a subsequent `run_script` to fetch payloads. Use "
        "`wait_for_slot_notification` to block inline on a specific "
        "slot's next event, or pass `wait_seconds=N` to `run_script` "
        "to wait inline for the script's own scheduled invokes."
    ),
    lifespan=lifespan,
)


# Bind the ``claude/channel`` capability into the lowlevel server's
# ``create_initialization_options`` so every transport
# (``run_stdio_async`` / ``run_sse_async`` / ``run_streamable_http_async``)
# picks it up without per-transport overrides. Without this declaration,
# Claude Code drops ``notifications/claude/channel`` frames silently.
_orig_create_init_opts = mcp._mcp_server.create_initialization_options


def _create_init_opts_with_channel(
    notification_options: Any = None,
    experimental_capabilities: dict[str, dict[str, Any]] | None = None,
) -> Any:
    merged = {**CHANNEL_CAPABILITY, **(experimental_capabilities or {})}
    return _orig_create_init_opts(notification_options, merged)


mcp._mcp_server.create_initialization_options = _create_init_opts_with_channel  # type: ignore[assignment]


# --- Helpers -----------------------------------------------------


def _app_ctx(ctx: Context) -> AppContext:
    """Fish AppContext out of a FastMCP request context.

    Also captures ``ctx.session`` into ``app_ctx.latest_session``
    so the workstation connection's asynchronous channel
    notifications (fired outside any tool-call request context)
    can push frames through the client's transport. The session
    object is tied to the client connection's lifetime, not the
    request context's, so reusing the captured reference from
    a later background event is safe as long as the client
    hasn't disconnected.
    """
    app_ctx = ctx.request_context.lifespan_context
    if not isinstance(app_ctx, AppContext):  # pragma: no cover — defensive
        raise RuntimeError("lifespan_context is not an AppContext")
    app_ctx.latest_session = ctx.session
    return app_ctx


async def _ensure_connected(app_ctx: AppContext) -> None:
    """Wake the supervisor and wait until it's ready.

    Assumes ``set_working_dir`` has already been called (which is
    where the :class:`WorkstationConnection` is constructed and
    its supervisor task started in the paused state). Raises a
    plain :class:`RuntimeError` if it wasn't, and propagates
    :class:`SupervisorNotReady` from
    :meth:`WorkstationConnection.wake_and_wait_ready` if the
    supervisor pauses again after one retry attempt.
    """
    if app_ctx.config is None or app_ctx.connection is None:
        raise RuntimeError(
            "Working dir is not set. Call `set_working_dir(<path>)` "
            f"first; the directory must contain {CONFIG_FILENAME}."
        )
    await app_ctx.connection.wake_and_wait_ready()


def _combine(primary: str, drained: str) -> str:
    """Join a tool's primary result string with any buffered
    notification text that was drained on tool exit. The drained
    text is appended *after* the primary result so the chronology
    reads "I did this, and here are events that happened during
    or right after." Empty drained → primary unchanged. Empty
    primary (rare; some error paths) → just the drained text."""
    if not drained:
        return primary
    if not primary:
        return drained
    return f"{primary}\n{drained}"


async def _ensure_sandbox_env(app_ctx: AppContext) -> SandboxEnvSupervisor:
    """Lazy-start the sandbox env supervisor on first use, then wait
    for the env to be ready. Returns the supervisor (so callers can
    ``await sandbox_env.get_env()``)."""
    if app_ctx.sandbox_env is None:
        DEFAULT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        app_ctx.sandbox_env = SandboxEnvSupervisor(cache_dir=DEFAULT_CACHE_DIR)
        app_ctx.sandbox_env.start(app_ctx.task_group)
    return app_ctx.sandbox_env


# --- Tools -------------------------------------------------------


@mcp.tool()
@logged_tool("set_working_dir")
async def set_working_dir(path: str, ctx: Context) -> str:
    """Set the working directory. Required before any other tool.

    The directory must contain ``.fxtr_workstation.toml`` (at
    minimum a ``workstation_url`` entry) and ``preamble.py`` (may
    be empty; re-exec'd before each ``run_script``). Optional
    ``packages/`` subdir: its subdirectories become importable via
    ``sys.path``.

    Returns a one-line confirmation with workstation URL, active
    session UUID (if any), and detected packages.
    """
    app_ctx = _app_ctx(ctx)
    if app_ctx.connection is not None:
        return (
            "Error: cannot change working_dir after connecting. Restart "
            "the MCP server to point at a different working dir."
        )
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        return f"Error: {p} is not a directory."
    if not (p / "preamble.py").is_file():
        return f"Error: {p}/preamble.py does not exist."
    try:
        config = WorkstationConfig.from_working_dir(p)
    except ConfigError as e:
        return f"Error: {e}"
    app_ctx.working_dir = p
    app_ctx.config = config

    # Construct + start the workstation connection eagerly. The
    # supervisor begins in :attr:`SupervisorState.PAUSED` with
    # reason "(not started)"; it holds no resources until a tool
    # calls :meth:`wake_and_wait_ready` (typically via the
    # ``connect`` tool, or implicitly via any tool that needs the
    # connection). The buffer adapter is the supervisor's hook for
    # routing slot promotions / supervisor_paused banners through
    # the lifespan-scoped :class:`OutgoingBuffer`.
    assert app_ctx.buffer is not None  # set in lifespan body
    buffer = app_ctx.buffer

    async def _notify_via_buffer(content: str, attrs: Mapping[str, str]) -> None:
        buffer.add(content, attrs)

    connection = WorkstationConnection(
        rpc_url=f"{config.workstation_url}/rpc",
        client_url=f"{config.workstation_url}/client",
        working_dir=p,
        notify_channel=_notify_via_buffer,
        agent_mcp_store=config.agent_mcp_store,
        exception_log=app_ctx.exception_log,
    )
    connection.start(app_ctx.task_group)
    app_ctx.connection = connection

    # Live read for the display message — ``WorkstationConfig`` no
    # longer carries ``active_session`` (single source of truth lives
    # in the toml + the connection's per-cycle refresh).
    sess = read_active_session(p) or "(none)"
    pkgs_dir = p / "packages"
    if pkgs_dir.is_dir():
        pkg_names = sorted(
            d.name for d in pkgs_dir.iterdir() if d.is_dir() and not d.name.startswith(".")
        )
        pkgs_note = f" packages={pkg_names or '(empty)'}"
    else:
        pkgs_note = " packages=(none)"
    return (
        f"Working dir set to {p}. workstation_url={config.workstation_url}; "
        f"active_session={sess};{pkgs_note}"
    )


@mcp.tool()
@logged_tool("nix_lock")
async def nix_lock(ctx: Context) -> str:
    """Create or refresh the working dir's ``flake.lock`` via the configured
    nix backend (host nix, or the in-podman builder — no host nix needed).

    Run this after adding or changing a flake input: the frozen build paths
    (spawning a nix service, setting a UI preset) resolve a committed
    ``flake.lock``, so a stale or missing lock makes them fail. Writes
    ``flake.lock`` into the working dir (commit it afterward). Requires
    ``set_working_dir`` first, and the flake's inputs must be reachable
    (network, or local paths the backend is configured to mount via
    ``mountable_paths``).
    """
    app_ctx = _app_ctx(ctx)
    if app_ctx.working_dir is None:
        return "Error: working dir is not set. Call set_working_dir(<path>) first."
    try:
        async with resolve_backend().builder_for(app_ctx.working_dir) as handle:
            await handle.lock()
    except Exception as e:
        return f"Error: nix lock failed: {e}"
    return f"Locked {app_ctx.working_dir}/flake.lock."


@mcp.tool()
@logged_tool("connect")
async def connect(ctx: Context) -> str:
    """Open the workstation connections. Idempotent.

    If the toml has ``active_session`` set, subscribes to it on
    startup. Errors if ``set_working_dir`` hasn't been called or
    the workstation is unreachable / session UUID is stale.
    """
    app_ctx = _app_ctx(ctx)
    assert app_ctx.buffer is not None
    with app_ctx.buffer.tool_window() as drain:
        try:
            await _ensure_connected(app_ctx)
            cfg = app_ctx.config
            connection = app_ctx.connection
            assert cfg is not None and connection is not None
            # Read from the connection rather than ``cfg.active_session``:
            # the connection re-reads the toml at the top of every
            # cycle (see ``_refresh_config_from_disk``), so its
            # ``active_session_id`` is the post-refresh truth — the
            # cached ``cfg`` may be stale from ``set_working_dir``.
            sess = connection.active_session_id or "none"
            primary = f"connected to {cfg.workstation_url}; session={sess}"
        except SupervisorNotReady as e:
            primary = f"Error: {e}"
        except Exception as e:
            primary = f"Error: {e}"
        # Trail-delay before draining: subscribe-time replay of
        # subsequent_events lands as queued _EventJob's and needs a
        # beat to drain into the response.
        await anyio.sleep(0.1)
        return _combine(primary, drain())


@mcp.tool()
@logged_tool("open_session")
async def open_session(
    ctx: Context,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Create a new workstation session and bind it to this MCP.

    Writes the new session UUID to the toml's ``active_session``;
    the supervisor has already subscribed by the time this
    returns. Errors if a session is already active — call
    ``close_session`` first (or ``close_session(force=True)`` if
    the supervisor is stuck on a poisoned session).
    """
    app_ctx = _app_ctx(ctx)
    assert app_ctx.buffer is not None
    with app_ctx.buffer.tool_window() as drain:
        try:
            await _ensure_connected(app_ctx)
            cfg = app_ctx.config
            connection = app_ctx.connection
            assert cfg is not None and connection is not None
            # ``connection.active_session_id`` is the post-refresh
            # truth (re-read from the toml at the top of every
            # cycle); reading ``cfg.active_session`` would see the
            # cached-at-set_working_dir value, which is stale after
            # an external toml edit + reconnect.
            existing = connection.active_session_id
            if existing is not None:
                primary = (
                    "Error: a session is already active "
                    f"(active_session={existing}). Call "
                    "close_session first, or manually remove the "
                    f"`active_session` line from {CONFIG_FILENAME} if the "
                    "session is stale."
                )
            else:
                try:
                    session_id = await connection.open_session(metadata or {})
                except SessionAlreadyActive as e:
                    primary = f"Error: {e}"
                except ConnectionLost as e:
                    primary = f"Error: connection lost during open_session: {e}"
                except Exception as e:
                    primary = f"Error: open_session failed: {e}"
                else:
                    assert app_ctx.working_dir is not None
                    write_active_session(app_ctx.working_dir, session_id)
                    primary = f"opened session {session_id}"
        except Exception as e:
            primary = f"Error: {e}"
        return _combine(primary, drain())


@mcp.tool()
@logged_tool("disconnect")
async def disconnect(ctx: Context) -> str:
    """End the current workstation connection cycle without
    closing the session.

    Tears down the active ``/rpc`` + ``/client`` connections and
    parks the supervisor in PAUSED. The next tool call (anything
    that goes through ``_ensure_connected``) automatically wakes
    a fresh cycle that re-reads ``.fxtr_workstation.toml`` — so
    manual toml edits (e.g. swapping ``active_session``) take
    effect on the very next call.

    Use cases:

    - You hand-edited the toml and want the new ``active_session``
      to take effect.
    - Something feels "off" about the cycle (cached proxy, stale
      state) and you want a clean reconnect without restarting
      the MCP.

    Does NOT close the workstation-side session — use
    ``close_session`` (or ``close_session(force=True)``) if you
    want the session gone. In-flight tool calls (e.g. a long
    ``run_script``) will see a ``ConnectionLost``-shaped error
    when this fires; the agent retries them from the next cycle.
    """
    app_ctx = _app_ctx(ctx)
    if app_ctx.connection is None:
        return "Error: not connected (set_working_dir not called)."
    return await app_ctx.connection.disconnect()


@mcp.tool()
@logged_tool("close_session")
async def close_session(ctx: Context, force: bool = False) -> str:
    """Close the active session and clear the toml entry. Errors
    if no session is active.

    ``force=True``: emergency recovery path. Opens a one-shot
    ``/client`` websocket and closes the session without going
    through the supervisor. Use when the supervisor is parked or
    can't drain a normal close (e.g. poisoned ``subsequent_events``
    replay keeps crashing the worker).
    """
    app_ctx = _app_ctx(ctx)
    assert app_ctx.buffer is not None
    with app_ctx.buffer.tool_window() as drain:
        if force:
            primary = await _do_force_close_session(app_ctx)
        else:
            primary = await _do_normal_close_session(app_ctx)
        return _combine(primary, drain())


async def _do_normal_close_session(app_ctx: AppContext) -> str:
    """Standard close path: go through the supervisor's queue."""
    try:
        await _ensure_connected(app_ctx)
        connection = app_ctx.connection
        assert connection is not None and app_ctx.working_dir is not None
        # Live runtime view of the active session: ``WorkstationConfig``
        # no longer carries it; the connection's per-cycle refresh +
        # ``write_active_session`` calls on open/close keep this in
        # sync with the toml.
        sid = connection.active_session_id
        if sid is None:
            return "Error: no active session to close."
        try:
            await connection.close_session()
        except NoActiveSession as e:
            return f"Error: {e}"
        except ConnectionLost as e:
            return f"Error: connection lost during close_session: {e}"
        except Exception as e:
            return f"Error: close_session failed: {e}"
        write_active_session(app_ctx.working_dir, None)
        return f"closed session {sid}"
    except Exception as e:
        return f"Error: {e}"


async def _do_force_close_session(app_ctx: AppContext) -> str:
    """Force-close path: open a one-shot ``/client`` websocket,
    subscribe + close without going through the supervisor.

    Skips ``_ensure_connected`` because the whole point of
    force-close is to recover when the supervisor is wedged or
    parked. We only need the toml for ``workstation_url`` and
    ``active_session``.

    Re-reads ``active_session`` fresh from disk so manual edits
    between the agent's set_working_dir and the force-close take
    effect. ``workstation_url`` is read from the cached
    :class:`WorkstationConfig` (it doesn't change during a
    process's lifetime).
    """
    cfg = app_ctx.config
    if cfg is None or app_ctx.working_dir is None:
        return "Error: working dir not set. Call `set_working_dir(<path>)` first."
    try:
        session_id = read_active_session(app_ctx.working_dir)
    except ConfigError as e:
        return f"Error: failed to re-read toml: {e}"
    if session_id is None:
        return "Error: no active_session in toml to force-close."
    client_url = f"{cfg.workstation_url}/client"
    try:
        outcome = await force_close_session_one_shot(client_url=client_url, session_id=session_id)
    except Exception as e:
        return f"Error: force_close_session failed: {e}"
    # Clear the toml + tell the supervisor (if it exists) so it
    # converges to a clean no-active-session state.
    write_active_session(app_ctx.working_dir, None)
    if app_ctx.connection is not None:
        app_ctx.connection.notify_session_externally_closed()
    return outcome


@mcp.tool()
@logged_tool("run_script")
async def run_script(
    script: str,
    ctx: Context,
    wait_seconds: float = 0.0,
) -> str:
    """Execute ``script`` in a fresh sandbox.

    The working dir's ``preamble.py`` is re-exec'd before each
    call (its globals are in scope for the script). Top-level
    ``await`` is allowed. Sandbox builtins importable via
    ``from _fxtr_mcp_builtin import …``:

    - Lookups: ``lookup_entity``, ``lookup_entity_by_name``,
      ``lookup_service_by_name``, ``get_service``,
      ``get_id_for_service``.
    - Writes (queued, committed atomically at script end):
      ``export_entity``, ``export_service``, ``forget``.
    - Invokes: ``schedule_invoke``, ``retrieve_result``.
    - Callbacks: ``create_callback``,
      ``callback_notification_keys``,
      ``retrieve_callback_notification``,
      ``forget_callback_notification``.

    Plus the ``ServiceRefStub`` / ``CallbackRefStub`` dataclasses
    returned from those calls.

    Returns a tool-result string: the script's primary output is
    wrapped in ``<script_result>…</script_result>``, followed by any
    buffered ``<event …>`` notifications (stdout/stderr and summary or
    traceback blocks appear inside the script result text). All
    queued ops are sent as one workstation mutation; either every op
    commits or none do.

    ``wait_seconds`` (default ``0.0``): if positive, wait until
    every service invocation completes, or until a timeout.
    """
    app_ctx = _app_ctx(ctx)
    if wait_seconds < 0:
        return f"Error: wait_seconds must be non-negative (got {wait_seconds!r})."
    assert app_ctx.buffer is not None
    buffer = app_ctx.buffer
    with buffer.tool_window() as drain:
        primary, scheduled_invoke_slot_keys = await _run_script_primary(app_ctx, script)
        buffer.add_script_result(primary)
        tail: str | None = None
        if wait_seconds == 0.0:
            # Trail-delay: lets just-in-time invoke results that
            # landed right after our commit make it into the same
            # response, and lets the worker drain any foreign
            # events queued behind our ScriptJob. No wait branch,
            # no timeout tail.
            await anyio.sleep(0.1)
        else:
            tail = await _wait_for_scheduled_invokes(
                connection=app_ctx.connection,
                buffer=buffer,
                slot_keys=scheduled_invoke_slot_keys,
                wait_seconds=wait_seconds,
            )
        result = drain()
        if tail is not None:
            result = _combine(result, tail)
        return result


async def _run_script_primary(app_ctx: AppContext, script: str) -> tuple[str, list[str]]:
    """Compute ``run_script``'s primary result string + the list
    of slot_keys this script scheduled invokes on. The tool body
    passes the string to :meth:`OutgoingBuffer.add_script_result`
    before trailing awaits so cancellation cannot drop it.

    The slot-key list is empty on every error path (no mutation
    was committed). On success it carries the slot_keys for the
    ``["invoke_pending", N]`` entries the just-committed mutation
    placed into ``data_by_key``; the tool's ``wait_seconds``
    branch uses it to know which slots to wait on before
    draining the buffer.
    """
    try:
        await _ensure_connected(app_ctx)
    except Exception as e:
        return f"Error: {e}", []

    cfg = app_ctx.config
    connection = app_ctx.connection
    if cfg is None or connection is None or app_ctx.working_dir is None:
        return "Error: not connected.", []

    preamble_src = (app_ctx.working_dir / "preamble.py").read_text()

    sandbox_env = await _ensure_sandbox_env(app_ctx)
    try:
        env = await sandbox_env.get_env()
    except Exception as e:
        return f"Error: failed to bootstrap sandbox env: {e}", []

    packages_dir = app_ctx.working_dir / "packages"
    user_packages_dir = packages_dir if packages_dir.is_dir() else None
    volumes, sys_path_additions, layout_warnings = build_runner_volumes(
        user_packages_dir=user_packages_dir,
    )

    try:
        outcome = await connection.run_script(
            preamble_src=preamble_src,
            script_src=script,
            env=env,
            volumes=volumes,
            sys_path_additions=sys_path_additions,
            working_dir=app_ctx.working_dir,
        )
    except Exception as e:
        _log.exception("run_script failed")
        return f"Error during sandbox execution: {e!r}", []

    body = outcome.body
    if layout_warnings:
        prefix = "\n".join(f"warning: {w}" for w in layout_warnings) + "\n\n"
        body = prefix + body
    return body, outcome.scheduled_invoke_slot_keys


async def _wait_for_scheduled_invokes(
    *,
    connection: WorkstationConnection | None,
    buffer: OutgoingBuffer,
    slot_keys: list[str],
    wait_seconds: float,
) -> str | None:
    """Wait for every slot in ``slot_keys`` to promote out of
    ``["invoke_pending", N]`` (i.e. to ``invoke_result`` /
    ``invoke_error``), up to ``wait_seconds`` total.

    Returns ``None`` if every slot completed within budget, or a
    one-line "Timed out…" tail string naming the slots that
    didn't. Called only when ``wait_seconds > 0`` and never
    produces output on the 0.0 path.

    Implementation:

    - Peek the mirror at call time; slots already terminal at
      peek are dropped from the wait set (they may have promoted
      while we were composing the request).
    - For the remaining slots, register an
      :meth:`OutgoingBuffer.watch_slot` event each; the
      ``_apply_state_effect`` per-kind branch for ``InvokeResultEvent`` /
      ``InvokeErrorEvent`` will eventually call
      ``buffer.add(..., attrs={"slot_key": ...})`` and fire the
      matching watchers.
    - Wait for all of them under one ``anyio.move_on_after``
      umbrella. On timeout the unfired watchers stay un-set;
      ``unwatch_slot`` in ``finally`` releases them.

    Sync peek + watcher registration with no awaits between them
    means the worker can't sneak a notification through the gap
    — same race-safe contract as ``wait_for_slot_notification``.
    """
    if connection is None:
        return None
    pending: list[str] = []
    for sk in slot_keys:
        value = connection.peek_slot_key(sk)
        if isinstance(value, list) and len(value) == 2 and value[0] == "invoke_pending":
            pending.append(sk)
    if not pending:
        return None
    watchers: dict[str, anyio.Event] = {sk: buffer.watch_slot(sk) for sk in pending}
    try:
        with anyio.move_on_after(wait_seconds):
            async with anyio.create_task_group() as tg:
                for ev in watchers.values():
                    tg.start_soon(ev.wait)
        still_pending = [sk for sk in pending if not watchers[sk].is_set()]
    finally:
        for sk, ev in watchers.items():
            buffer.unwatch_slot(sk, ev)
    if still_pending:
        return f"Timed out after {wait_seconds}s waiting for invoke slots: {still_pending!r}."
    return None


# Tags in ``mirror.data_by_key`` denoting "future notifications
# expected" for this slot. ``invoke_pending`` — one-shot, promote
# on result/error event. ``callback_open`` — long-lived, fires
# repeatedly via CallbackEvent. Anything else (``invoke_result`` /
# ``invoke_error`` / ``exported_entity`` / ``exported_service`` /
# …) is terminal: no future notification can arrive.
@mcp.tool()
@logged_tool("wait_for_slot_notification")
async def wait_for_slot_notification(
    slot_key: str,
    timeout_seconds: float,
    ctx: Context,
    after_notif_index: int | None = None,
) -> str:
    """Block until ``slot_key`` receives a notification, or
    ``timeout_seconds`` elapses (positive, no default).

    Race-safe short-circuits:

    - Slot already resolved (invoke/load result already landed) →
      success immediately, with a hint to call
      ``retrieve_result(slot_key)``.
    - Callback slot with notification(s) already in the inbox →
      returns immediately with a hint at the latest seq.

    ``after_notif_index`` (callback slots only): pass the most
    recent ``notif_index`` you've seen (from channel attrs
    ``notif_index="..."``) to wait for *anything newer*. Errors
    if that seq isn't in the inbox (so you can't ask to wait
    after a seq you never received) or if passed for a
    non-callback slot.

    On match: returns the drained buffer (the triggering event
    plus anything else buffered during the wait). On timeout:
    drained buffer + ``Timed out after Ns waiting for slot X.``.
    Absent slot errors.
    """
    app_ctx = _app_ctx(ctx)
    assert app_ctx.buffer is not None
    buffer = app_ctx.buffer
    with buffer.tool_window() as drain:
        return await _wait_for_slot_notification_inner(
            app_ctx=app_ctx,
            buffer=buffer,
            drain=drain,
            slot_key=slot_key,
            timeout_seconds=timeout_seconds,
            after_notif_index=after_notif_index,
        )


async def _wait_for_slot_notification_inner(
    *,
    app_ctx: AppContext,
    buffer: OutgoingBuffer,
    drain: Callable[[], str],
    slot_key: str,
    timeout_seconds: float,
    after_notif_index: int | None,
) -> str:
    """Body of :func:`wait_for_slot_notification`. Returns the
    tool-result string; caller wraps it in the tool window +
    verbose logging."""
    if timeout_seconds <= 0:
        return _combine(
            f"Error: timeout_seconds must be > 0 (got {timeout_seconds!r}).",
            drain(),
        )
    connection = app_ctx.connection
    if connection is None or app_ctx.config is None:
        return _combine("Error: not connected.", drain())
    if connection.active_session_id is None:
        return _combine("Error: no active session.", drain())

    current = connection.peek_slot_key(slot_key)
    if current is None:
        return _combine(
            f"Error: slot {slot_key!r} is not present in session state.",
            drain(),
        )

    tag = current[0] if (isinstance(current, list) and current) else None

    if tag == "callback_open":
        return await _wait_for_callback_slot(
            connection=connection,
            buffer=buffer,
            drain=drain,
            slot_key=slot_key,
            timeout_seconds=timeout_seconds,
            after_notif_index=after_notif_index,
        )

    if tag == "invoke_pending":
        if after_notif_index is not None:
            return _combine(
                "Error: after_notif_index is only valid for callback_open slots; "
                f"slot {slot_key!r} is {current!r}.",
                drain(),
            )
        return await _park_on_slot_watch(
            buffer=buffer,
            drain=drain,
            slot_key=slot_key,
            timeout_seconds=timeout_seconds,
        )

    # Terminal slot (invoke_result / invoke_error / exported_entity /
    # exported_service / ...). The agent expected to wait for
    # resolution; the resolution already happened. Return success.
    # If they passed after_notif_index, it's meaningless here but their
    # intent (wait for this slot) is met, so we don't error — just hint.
    headline = (
        f"Slot {slot_key!r} was already resolved before the wait: {current!r}. "
        f"Retrieve via retrieve_result({slot_key!r})."
    )
    if after_notif_index is not None:
        headline += (
            " (Note: after_notif_index should only be used for callback slots; ignored here.)"
        )
    return _combine(headline, drain())


async def _wait_for_callback_slot(
    *,
    connection: WorkstationConnection,
    buffer: OutgoingBuffer,
    drain: Callable[[], str],
    slot_key: str,
    timeout_seconds: float,
    after_notif_index: int | None,
) -> str:
    """Callback-flow body of :func:`wait_for_slot_notification`.

    Consults ``mirror.callback_notifications[slot_key]`` to decide
    whether to park or short-circuit. Sync between the peek and
    the watcher registration — anyio's cooperative scheduling
    means no notification can land in between.
    """
    inbox = connection.peek_callback_notifications(slot_key) or {}
    inbox_seqs = sorted(int(s) for s in inbox.keys())

    if after_notif_index is not None:
        if after_notif_index not in inbox_seqs:
            return _combine(
                f"Error: after_notif_index={after_notif_index} was not found "
                f"among delivered notifications for slot {slot_key!r} "
                f"(inbox seqs: {inbox_seqs}). after_notif_index must be a "
                "previously-delivered notif_index for this slot; pass the seq "
                "from the most recent callback notification you've seen, or "
                "omit it to wait for any notification.",
                drain(),
            )
        later = [s for s in inbox_seqs if s > after_notif_index]
        if later:
            return _combine(
                f"A callback notification for slot {slot_key!r} newer than "
                f"after_notif_index={after_notif_index} is already in the inbox "
                f"(latest seq={later[-1]}). Retrieve it via "
                f"retrieve_callback_notification({slot_key!r}, {later[-1]}).",
                drain(),
            )
        # after_notif_index is the latest in the inbox → park.
    else:
        if inbox_seqs:
            latest = inbox_seqs[-1]
            return _combine(
                f"A callback notification for slot {slot_key!r} is already in "
                f"the inbox (latest seq={latest}). Retrieve it via "
                f"retrieve_callback_notification({slot_key!r}, {latest}); pass "
                f"after_notif_index={latest} to wait for the *next* one.",
                drain(),
            )
        # Empty inbox → park.

    return await _park_on_slot_watch(
        buffer=buffer,
        drain=drain,
        slot_key=slot_key,
        timeout_seconds=timeout_seconds,
    )


async def _park_on_slot_watch(
    *,
    buffer: OutgoingBuffer,
    drain: Callable[[], str],
    slot_key: str,
    timeout_seconds: float,
) -> str:
    """Register a slot watcher and block on it until it fires or
    the timeout elapses. Returns the drained buffer (plus a timeout
    sentinel if applicable)."""
    ev = buffer.watch_slot(slot_key)
    timed_out = True
    try:
        with anyio.move_on_after(timeout_seconds):
            await ev.wait()
            timed_out = False
    finally:
        buffer.unwatch_slot(slot_key, ev)

    drained = drain()
    if timed_out:
        tail = f"Timed out after {timeout_seconds}s waiting for slot {slot_key!r}."
        return _combine(drained, tail)
    return drained


def main() -> None:
    """Console-script entry point declared in ``pyproject.toml``."""
    mcp.run()


if __name__ == "__main__":
    main()
