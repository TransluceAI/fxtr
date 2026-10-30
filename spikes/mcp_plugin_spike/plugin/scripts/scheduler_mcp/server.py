"""FastMCP server for the mcp_plugin_spike Claude Code plugin.

Two layers of functionality:

1. **Channel-based message scheduling.** The original spike: a
   ``schedule_message(delay_seconds, message)`` tool plus the
   ``notifications/claude/channel`` JSON-RPC notification path that
   delivers the message back to the agent after the delay. See the
   ``ChannelFastMCP`` / ``_send_channel_notification`` /
   ``_deliver_after`` helpers below — unchanged from the original
   spike.

2. **Sandboxed script execution.** ``set_working_dir(path)`` +
   ``run_script(script)``. The lifespan owns:
   - a long-lived ``eryx.SandboxFactory`` built from
     ``mcp-sandbox-runtime``'s dep closure (paid once at startup,
     ~3s);
   - a ``DirectSandboxEnvironment`` over that factory;
   - a mountable ``FxtrScope`` hosting the ``McpHostImpl``
     (host_info bridged into each sandbox via ``McpRunnerConfig``).
   Each ``run_script`` call opens a fresh sandbox via
   ``env.open_sandbox(entry_point="_fxtr_mcp_runner.entry:run",
   volumes=[...])``, calls ``runner.run()`` to exec the user's
   preamble + script, then replays any scheduled messages collected
   inside the sandbox via the same channel mechanism layer (1) uses.

Three things differ from a vanilla FastMCP server (see original
spike notes plus the additions above):

1. ``ChannelFastMCP`` overrides ``run_stdio_async`` to declare the
   ``experimental.claude/channel`` capability.
2. ``lifespan`` opens the long-lived task group, the FxtrScope,
   mounts ``McpHostImpl``, bootstraps the factory, opens the env.
3. ``_send_channel_notification`` writes a raw notification through
   the session's write stream (the typed ``ServerNotification`` union
   doesn't include ``notifications/claude/channel``).
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import eryx
from _fxtr_mcp_runner.protocols import McpHostProto, McpRunnerConfig, McpRunnerProto
from anyio.abc import TaskGroup
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope, ServiceRef
from fxtr_sandbox_types.protocols import SandboxConnectProto
from fxtr_sandboxing.direct_launcher import (
    DirectSandboxEnvironment,
    create_direct_sandbox_environment,
)
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification

from .host_impl import McpHostImpl
from .sandbox_factory import bootstrap_factory, build_runner_volumes

log = logging.getLogger(__name__)

# plugin/scripts/scheduler_mcp/server.py → plugin/scripts/
PLUGIN_SCRIPTS_DIR = Path(__file__).parent.parent.resolve()
# spikes/mcp_plugin_spike/sandbox_runtime/
SANDBOX_RUNTIME_DIR = (PLUGIN_SCRIPTS_DIR / "../../sandbox_runtime").resolve()
# Wheel cache lives under the plugin's scripts dir so it survives across
# server restarts. Created if missing.
CACHE_DIR = (PLUGIN_SCRIPTS_DIR / ".cache" / "wheel_cache").resolve()


class ChannelFastMCP(FastMCP):
    async def run_stdio_async(self) -> None:
        async with stdio_server() as (read_stream, write_stream):
            await self._mcp_server.run(
                read_stream,
                write_stream,
                self._mcp_server.create_initialization_options(
                    experimental_capabilities={"claude/channel": {}}
                ),
            )


@dataclass
class AppContext:
    """Server-wide state. Owned by ``lifespan``; tools read it via
    ``ctx.request_context.lifespan_context``.

    Scope discipline (per ``v2/notes/scope_discipline.md``): ``host_ref``
    is loadable only in ``host_scope``, so the two are stored as a pair.
    Tools that interact with the bridged host capability — currently
    just ``run_script`` — must explicitly enter ``host_scope`` (via
    ``with host_scope.as_active_scope():``) before opening a sandbox so the
    sandbox's per-call FxtrScope inherits ``host_scope`` as a parent
    and can encode ``host_ref`` across the wire.
    """

    task_group: TaskGroup
    env: DirectSandboxEnvironment
    # `host_ref` is mounted on `host_scope` (mountable). The pair is
    # passed/stored together because the ref isn't loadable anywhere else.
    host_scope: FxtrScope
    host_ref: ServiceRef[McpHostProto]
    # Mutable: set by `set_working_dir`. None until the agent calls it.
    working_dir: Path | None = None


@contextlib.asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncIterator[AppContext]:
    """Server-wide setup. Runs once at MCP startup, torn down at shutdown.

    Resource ordering (outer-to-inner):
      task_group   — long-lived deliveries outlive their originating tool
      host_scope   — `isolated=True, include=[GLOBAL_SCOPE]` per scope
                     discipline. Owns the host service mount; serves as
                     the parent for sandbox per-call scopes opened by
                     `run_script` so `host_ref` is encodable across the
                     RPC bridge.
      McpHostImpl  — mounted on host_scope; ref bridged into every
                     sandbox via `McpRunnerConfig.host_ref`.
      factory      — built from sandbox_runtime's dep closure (~3s,
                     once). `bootstrap_factory` calls `prepare()` which
                     requires an active scope to store the bundle entity
                     into; we enter `host_scope.as_active_scope()` for that
                     call. The bundle entity stays in `host_scope` (its
                     contents have already been materialized to disk by
                     then; we never touch it again).
      direct env   — wraps the factory; each `open_sandbox` is a fresh
                     spawn whose call_scope inherits whatever was active.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    async with anyio.create_task_group() as tg:
        try:
            async with FxtrScope(
                isolated=True,
                include=[GLOBAL_SCOPE],
                mountable=True,
                name="mcp_lifespan",
            ) as host_scope:
                # McpHostImpl mount → host_ref loadable in host_scope.
                host_ref = host_scope.mount(
                    McpHostImpl(f"mcp_plugin_spike, scripts_dir={PLUGIN_SCRIPTS_DIR}")
                )
                log.info("Bootstrapping eryx SandboxFactory (this takes ~3s)...")
                # `bootstrap_factory` -> `prepare()` -> `bundle.store()`
                # uses the active scope. Enter host_scope explicitly
                # (per scope discipline's "user-of-fxtr code expects an
                # active scope" carve-out for `as_active`).
                with host_scope.as_active_scope():
                    factory: eryx.SandboxFactory = await bootstrap_factory(
                        sandbox_runtime_dir=SANDBOX_RUNTIME_DIR,
                        cache_dir=CACHE_DIR,
                    )
                log.info("Factory ready. Opening DirectSandboxEnvironment.")
                async with create_direct_sandbox_environment(factory=factory) as env:
                    yield AppContext(
                        task_group=tg,
                        env=env,
                        host_scope=host_scope,
                        host_ref=host_ref,
                    )
        finally:
            tg.cancel_scope.cancel()


mcp = ChannelFastMCP(
    name="scheduler",
    instructions=(
        "Two facilities: (1) `schedule_message(delay_seconds, message)` "
        "delivers a self-directed follow-up after a delay via the "
        '<channel source="scheduler"> mechanism. (2) `set_working_dir(path)` '
        "+ `run_script(script)` execute Python in a sandboxed eryx WASM "
        "process. The working directory must contain `preamble.py` (run "
        "before each script) and may have a `packages/` subdir of importable "
        "Python packages. Inside the script: `from _fxtr_mcp_builtin import "
        "declare_value, schedule_message, host_info` (declare_value collects "
        "JSON values returned to the agent; schedule_message queues a "
        "follow-up like the top-level tool; host_info() round-trips to the "
        "host)."
    ),
    lifespan=lifespan,
)


# ---------- channel notification plumbing (unchanged from original spike) ----------


async def _send_channel_notification(
    session: Any, content: str, meta: dict[str, str] | None = None
) -> None:
    notification = JSONRPCNotification(
        jsonrpc="2.0",
        method="notifications/claude/channel",
        params={"content": content, "meta": meta or {}},
    )
    await session._write_stream.send(SessionMessage(message=JSONRPCMessage(notification)))


async def _deliver_after(session: Any, delay_seconds: float, message: str) -> None:
    try:
        await anyio.sleep(delay_seconds)
        await _send_channel_notification(session, message, {"delay_seconds": str(delay_seconds)})
    except Exception:
        log.exception("failed to deliver scheduled channel message")


# ---------- tools ----------


@mcp.tool()
async def schedule_message(
    delay_seconds: float,
    message: str,
    ctx: Context,
) -> str:
    """Schedule `message` to be delivered through the scheduler channel after `delay_seconds`.

    Returns immediately. The message arrives later as a `<channel source="scheduler">` event.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    app_ctx.task_group.start_soon(_deliver_after, ctx.session, delay_seconds, message)
    return f"Scheduled. Delivery in {delay_seconds}s."


@mcp.tool()
async def set_working_dir(path: str, ctx: Context) -> str:
    """Set the working directory for subsequent `run_script` calls.

    The directory must contain `preamble.py` (re-exec'd before each script).
    A `packages/` subdir is optional — if present, its subdirectories are
    importable from the script via `sys.path`.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        return f"Error: {p} is not a directory."
    if not (p / "preamble.py").is_file():
        return f"Error: {p}/preamble.py does not exist."
    app_ctx.working_dir = p
    pkgs_note = ""
    if (p / "packages").is_dir():
        pkg_names = sorted(
            d.name for d in (p / "packages").iterdir() if d.is_dir() and not d.name.startswith(".")
        )
        pkgs_note = f" Found packages/: {pkg_names or '(empty)'}."
    else:
        pkgs_note = " No packages/ subdir."
    return f"Working dir set to {p}.{pkgs_note}"


def _format_run_result(declared: dict[str, Any], scheduled: list[dict[str, Any]]) -> str:
    """Format `runner.run()`'s output as plain readable text for the agent.

    Avoids JSON-in-JSON nesting (the agent reads tool results as text,
    so returning JSON forces a parse step that's wasted effort for
    a phase-1 spike). Each declared value is shown as
    ``key = python_repr(value)``; scheduled messages are summarized
    by count + a one-line preview.
    """
    lines: list[str] = []
    if declared:
        lines.append("Declared values:")
        for k, v in declared.items():
            lines.append(f"  {k} = {v!r}")
    else:
        lines.append("Declared values: (none)")

    if scheduled:
        lines.append(f"Scheduled {len(scheduled)} follow-up message(s):")
        for entry in scheduled:
            delay = entry.get("delay_seconds")
            msg = entry.get("message", "")
            preview = msg if len(msg) <= 60 else msg[:57] + "..."
            lines.append(f"  +{delay}s: {preview!r}")
    else:
        lines.append("Scheduled follow-ups: (none)")

    return "\n".join(lines)


@mcp.tool()
async def run_script(script: str, ctx: Context) -> str:
    """Execute `script` in a fresh sandbox, after re-exec'ing the preamble.

    Returns a plain-text summary of values declared by the script and
    follow-up messages scheduled. Any `schedule_message(...)` calls
    made inside the script are queued for delivery via the same channel
    mechanism as the top-level `schedule_message` tool.

    The script can `from _fxtr_mcp_builtin import declare_value,
    schedule_message, host_info`. Top-level `await` is supported.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context

    if app_ctx.working_dir is None:
        return "Error: working_dir not set. Call set_working_dir(path) first."

    preamble_path = app_ctx.working_dir / "preamble.py"
    preamble_src = preamble_path.read_text()

    packages_dir = app_ctx.working_dir / "packages"
    user_packages_dir = packages_dir if packages_dir.is_dir() else None
    volumes, sys_path_additions = build_runner_volumes(
        plugin_scripts_dir=PLUGIN_SCRIPTS_DIR,
        user_packages_dir=user_packages_dir,
    )

    config = McpRunnerConfig(
        preamble_src=preamble_src,
        script_src=script,
        # `host_ref` is loadable in `app_ctx.host_scope` (mounted there
        # by lifespan). We must enter `host_scope.as_active_scope()` below so
        # `open_sandbox`'s per-call FxtrScope inherits it as a parent —
        # otherwise the connect-side encoding of `host_ref` can't reach
        # the scope where the ref lives.
        host_ref=app_ctx.host_ref,
    )

    try:
        # Explicit active-scope entry (don't rely on contextvars surviving
        # FastMCP's request handling). `open_sandbox` opens a per-call
        # FxtrScope inside that inherits whatever's active here as its
        # parent; that parent must include `host_scope` so the bridged
        # `host_ref` is encodable into the sandbox's connect config.
        with app_ctx.host_scope.as_active_scope():
            async with app_ctx.env.open_sandbox(
                entry_point="_fxtr_mcp_runner.entry:run",
                volumes=volumes,
                sys_path_additions=sys_path_additions,
            ) as sandbox_ref:
                typed = sandbox_ref.replace_service_type(
                    SandboxConnectProto[McpRunnerConfig, McpRunnerProto]
                )
                # `sandbox_ref` is loadable in open_sandbox's per-call
                # scope (now active). `resolve()` without an explicit
                # scope picks up the active one — which is correct here.
                sandbox = await typed.resolve()
                runner_ref = await sandbox.connect(config)
                runner = await runner_ref.resolve()
                result = await runner.run()
    except Exception as exc:
        # Surface sandbox-side errors (validation, script exception, etc.)
        # back to the agent as a tool result rather than a tool error.
        log.exception("run_script failed")
        return f"Error during sandbox execution: {exc!r}"

    declared = result.get("declared", {})
    scheduled = result.get("scheduled", [])

    # Replay the scheduled messages via the same channel-notification
    # task group that schedule_message uses directly.
    for entry in scheduled:
        delay = float(entry["delay_seconds"])
        msg = str(entry["message"])
        app_ctx.task_group.start_soon(_deliver_after, ctx.session, delay, msg)

    return _format_run_result(declared, scheduled)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
