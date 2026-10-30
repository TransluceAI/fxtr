"""Integration test for the four stage-1 tools.

Boots a real :class:`WorkstationServer` in-process, then drives the
MCP's lifespan + tool callables directly (without going through the
stdio transport) to exercise:

- ``set_working_dir`` precondition
- ``connect`` opening rpc + client
- ``open_session`` creating a session and persisting to the toml
- ``close_session`` closing the session and clearing the toml
- error paths (open when active, close when none)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fxtr_workstation import WorkstationServer

from fxtr_workstation_mcp.app_context import AppContext
from fxtr_workstation_mcp.config import CONFIG_FILENAME
from fxtr_workstation_mcp.server import (
    close_session,
    connect,
    open_session,
    set_working_dir,
)
from fxtr_workstation_mcp.sandbox_env import SandboxEnvSupervisor
from fxtr_workstation_mcp.server import make_app_ctx

pytestmark = pytest.mark.anyio


class _FakeRequestContext:
    def __init__(self, app_ctx: AppContext) -> None:
        self.lifespan_context = app_ctx


class _FakeContext:
    """Minimal stand-in for ``mcp.server.fastmcp.Context``.

    FastMCP's tool dispatch wraps user functions so that the ``ctx``
    argument is supplied automatically. Outside of MCP we call the
    underlying functions directly with our own fake.
    """

    def __init__(self, app_ctx: AppContext) -> None:
        self.request_context = _FakeRequestContext(app_ctx)
        # ServerSession stand-in; None means channel notifications
        # drop silently — fine for tests that don't assert on them.
        self.session = None


# --- Tool callables (FastMCP's @tool() decorator returns the wrapped
# --- function unchanged; we can call them directly with our fake ctx).


async def _call_set_working_dir(ctx: _FakeContext, path: str) -> str:
    return await set_working_dir(path, ctx)  # type: ignore[arg-type]


async def _call_connect(ctx: _FakeContext) -> str:
    return await connect(ctx)  # type: ignore[arg-type]


async def _call_open_session(ctx: _FakeContext, metadata: dict[str, Any] | None = None) -> str:
    return await open_session(ctx, metadata)  # type: ignore[arg-type]


async def _call_close_session(ctx: _FakeContext, *, force: bool = False) -> str:
    return await close_session(ctx, force=force)  # type: ignore[arg-type]


# --- Tests --------------------------------------------------------


async def test_connect_errors_without_working_dir(
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> None:
    async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
        ctx = _FakeContext(app_ctx)
        result = await _call_connect(ctx)
        assert "Error" in result and "set_working_dir" in result


async def test_full_session_lifecycle(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """End-to-end: set_working_dir → connect → open_session → close_session.

    Verifies the workstation actually sees the session and drops it
    on close, and that the toml is updated correspondingly.
    """
    async with WorkstationServer() as ws:
        # Write the toml.
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# empty preamble\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)

            r = await _call_set_working_dir(ctx, str(tmp_path))
            assert "Working dir set" in r and "active_session=(none)" in r

            r = await _call_connect(ctx)
            assert r.startswith("connected to") and "session=none" in r

            # Idempotent re-connect.
            r = await _call_connect(ctx)
            assert r.startswith("connected to")

            # No session yet → close fails.
            r = await _call_close_session(ctx)
            assert r.startswith("Error") and "no active session" in r

            # Open a session.
            r = await _call_open_session(ctx, {"k": "v"})
            assert r.startswith("opened session ")
            session_id = r.split()[2]

            # Workstation sees it.
            assert session_id in {str(s) for s in ws.state.sessions.keys()}

            # Toml has the active_session line.
            toml_after_open = (tmp_path / CONFIG_FILENAME).read_text()
            assert f'active_session = "{session_id}"' in toml_after_open

            # Open again → error (active_session set).
            r = await _call_open_session(ctx, {})
            assert r.startswith("Error") and "already active" in r

            # Close it.
            r = await _call_close_session(ctx)
            assert r.startswith("closed session ") and session_id in r

            # Workstation no longer has the session.
            import uuid as _uuid

            assert _uuid.UUID(session_id) not in ws.state.sessions

            # Toml's active_session is gone.
            toml_after_close = (tmp_path / CONFIG_FILENAME).read_text()
            assert "active_session" not in toml_after_close

            # Close again → error.
            r = await _call_close_session(ctx)
            assert r.startswith("Error") and "no active session" in r


async def test_resubscribe_to_existing_session(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """If the toml already has an ``active_session``, ``connect``
    resubscribes to it on first call. Verified by then closing it
    successfully (which requires a live subscription)."""
    async with WorkstationServer() as ws:
        # First session: open + leave the workstation with the session
        # alive but disconnect our MCP.
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# empty preamble\n")
        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            await _call_set_working_dir(ctx, str(tmp_path))
            await _call_connect(ctx)
            r = await _call_open_session(ctx, {"phase": 1})
            session_id = r.split()[2]
        # AppContext exit closed our /client connection; the workstation
        # session is still alive (Closes only happen via explicit
        # CloseOp).
        import uuid as _uuid

        assert _uuid.UUID(session_id) in ws.state.sessions

        # Reopen from a fresh AppContext — connect should resubscribe.
        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            await _call_set_working_dir(ctx, str(tmp_path))
            await _call_connect(ctx)
            r = await _call_close_session(ctx)
            assert r.startswith("closed session ") and session_id in r
        assert _uuid.UUID(session_id) not in ws.state.sessions


# --- stage7post1: force-close recovery -------------------------------


async def test_force_close_session_closes_workstation_side(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """``close_session(force=True)`` opens a one-shot websocket,
    subscribes, sends CloseOp without processing events, and
    closes — bypassing the supervisor's worker queue. Verifies
    the workstation actually drops the session and the toml is
    cleared."""
    async with WorkstationServer() as ws:
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# stage7post1 force-close\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            await _call_set_working_dir(ctx, str(tmp_path))
            await _call_connect(ctx)
            r = await _call_open_session(ctx, {"phase": "force-close"})
            session_id = r.split()[2]

            import uuid as _uuid

            assert _uuid.UUID(session_id) in ws.state.sessions

            # Force-close.
            r = await _call_close_session(ctx, force=True)
            assert "force-closed session" in r and session_id in r, r

            # Workstation dropped the session.
            assert _uuid.UUID(session_id) not in ws.state.sessions

            # Toml's active_session is gone.
            toml_after = (tmp_path / CONFIG_FILENAME).read_text()
            assert "active_session" not in toml_after

            # The connection's _active_session_id was cleared too.
            assert app_ctx.connection is not None
            # The supervisor may not have noticed yet (its
            # subscription will get session_gone on its next op),
            # but our local _active_session_id was cleared.
            assert app_ctx.connection.active_session_id is None


async def test_force_close_session_no_active_session(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """``close_session(force=True)`` with no ``active_session`` in
    the toml returns an error string."""
    async with WorkstationServer() as ws:
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# stage7post1\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            await _call_set_working_dir(ctx, str(tmp_path))
            # Don't open a session.
            r = await _call_close_session(ctx, force=True)
            assert "Error" in r and "no active_session" in r


async def test_force_close_session_recovers_supervisor_to_clean_state(
    tmp_path: Path,
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> None:
    """After force-close, the agent can call ``connect`` again
    and ``open_session`` to start fresh. Verifies the supervisor's
    state transition from "subscribed to old session" through
    "session externally closed; subscription dies on next op" to
    "no active session, ready for a new open"."""
    async with WorkstationServer() as ws:
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# stage7post1\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            await _call_set_working_dir(ctx, str(tmp_path))
            await _call_connect(ctx)
            r = await _call_open_session(ctx, {"phase": 1})
            old_session_id = r.split()[2]

            # Force-close.
            r = await _call_close_session(ctx, force=True)
            assert "force-closed session" in r

            # connect() should converge to clean state. The
            # supervisor may need a moment to notice session_gone
            # on its existing subscription and reconnect with no
            # active session.
            r = await _call_connect(ctx)
            assert r.startswith("connected to") and "session=none" in r, r

            # Open a fresh session.
            r = await _call_open_session(ctx, {"phase": 2})
            new_session_id = r.split()[2]
            assert new_session_id != old_session_id

            # Both old and new survive distinct in workstation state.
            import uuid as _uuid

            assert _uuid.UUID(new_session_id) in ws.state.sessions
            assert _uuid.UUID(old_session_id) not in ws.state.sessions

            # Normal close.
            r = await _call_close_session(ctx)
            assert r.startswith("closed session ") and new_session_id in r


async def test_force_close_session_without_connect(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """``close_session(force=True)`` works without ever calling
    ``connect`` — it doesn't go through ``_ensure_connected``.
    Requires only ``set_working_dir`` so the toml is parsed."""
    async with WorkstationServer() as ws:
        # Pre-create a session via a separate "MCP context", then
        # close it from a fresh context that never calls connect.
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# stage7post1\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            await _call_set_working_dir(ctx, str(tmp_path))
            await _call_connect(ctx)
            r = await _call_open_session(ctx, {"phase": "force-close-no-connect"})
            session_id = r.split()[2]

        import uuid as _uuid

        assert _uuid.UUID(session_id) in ws.state.sessions

        # Fresh context — no connect. Force-close should still work.
        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            await _call_set_working_dir(ctx, str(tmp_path))
            r = await _call_close_session(ctx, force=True)
            assert "force-closed session" in r and session_id in r, r

        assert _uuid.UUID(session_id) not in ws.state.sessions
