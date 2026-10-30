"""End-to-end tests for ``run_script(wait_seconds=...)``.

Covers stage7post2's wait knob: with ``wait_seconds > 0`` the
tool blocks until every invoke this script just scheduled has
promoted to a terminal value (or the timeout elapses), and the
results flow into the same response inline. With ``wait_seconds
== 0.0`` (default) the tool keeps the current 0.1s trail-delay
behavior and never emits a "Timed out…" tail.

Uses a peer-registered ``_DelayedEchoImpl`` whose ``echo(msg,
delay)`` ``await``s for the given number of seconds before
returning ``"echo:<msg>"``. Slow because each test pays one
factory bootstrap.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, Protocol

import anyio
import pytest
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method
from fxtr_workstation import WorkstationServer, expose_service

from fxtr_workstation_mcp.app_context import AppContext
from fxtr_workstation_mcp.config import CONFIG_FILENAME
from fxtr_workstation_mcp.server import (
    connect,
    open_session,
    run_script,
    set_working_dir,
)
from fxtr_workstation_mcp.sandbox_env import SandboxEnvSupervisor
from fxtr_workstation_mcp.server import make_app_ctx

from ._peer import connect_peer_rpc

pytestmark = pytest.mark.anyio


_DELAYED_NS = uuid.UUID("11223344-5566-7788-99aa-bbccddeeff00")
_DELAYED_TYPE_ID = system_entities.define_type(_DELAYED_NS, "stage7post2_delayed_echo")


@service_protocol
class _DelayedEchoProto(Protocol):
    async def echo(self, msg: str, delay: float) -> str: ...


class _DelayedEchoImpl(TypedServiceImpl[_DelayedEchoProto]):
    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return _DelayedEchoProto

    def _service_type_id(self) -> FxtrID:
        return _DELAYED_TYPE_ID

    @service_method
    async def echo(self, msg: str, delay: float) -> str:
        await anyio.sleep(delay)
        return f"echo:{msg}"


# --- MCP-context shims (copied from test_invoke_e2e.py) ---------------


class _FakeWriteStream:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> None:
        self.sent.append(message)


class _FakeSession:
    def __init__(self) -> None:
        self._write_stream = _FakeWriteStream()


class _FakeRequestContext:
    def __init__(self, app_ctx: AppContext) -> None:
        self.lifespan_context = app_ctx


class _FakeContext:
    def __init__(self, app_ctx: AppContext) -> None:
        self.request_context = _FakeRequestContext(app_ctx)
        self.session = _FakeSession()


async def _swd(ctx: _FakeContext, p: str) -> str:
    return await set_working_dir(p, ctx)  # type: ignore[arg-type]


async def _conn(ctx: _FakeContext) -> str:
    return await connect(ctx)  # type: ignore[arg-type]


async def _open(ctx: _FakeContext, md: dict[str, Any] | None = None) -> str:
    return await open_session(ctx, md)  # type: ignore[arg-type]


async def _run(
    ctx: _FakeContext,
    src: str,
    *,
    wait_seconds: float = 0.0,
) -> str:
    return await run_script(src, ctx, wait_seconds=wait_seconds)  # type: ignore[arg-type]


# --- Test helper: stand up a session with the delayed-echo service ----


@contextlib.asynccontextmanager
async def _delayed_echo_session(
    tmp_path: Path,
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> AsyncGenerator[tuple[_FakeContext, AppContext, str]]:
    """Bring up a workstation + peer + delayed-echo service and an
    MCP session pointed at it. Yields ``(ctx, app_ctx,
    svc_short_id)`` — the service's workstation short_id is the
    third element so scripts can ``get_service(svc_short_id)``.
    """
    async with WorkstationServer() as ws:
        async with connect_peer_rpc(ws.rpc_url) as peer:
            echo_ref = await peer.scope.mount(_DelayedEchoImpl())
            async with expose_service(
                peer.remote_service,
                "stage7post2-delayed-echo",
                echo_ref,
                peer.scope,
            ) as svc_short_id:
                (tmp_path / CONFIG_FILENAME).write_text(
                    f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
                )
                (tmp_path / "preamble.py").write_text("# stage7post2 wait-seconds tests\n")

                async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
                    ctx = _FakeContext(app_ctx)
                    await _swd(ctx, str(tmp_path))
                    await _conn(ctx)
                    await _open(ctx, {"phase": "stage7post2-wait"})
                    yield ctx, app_ctx, svc_short_id


def _schedule_invoke_script(svc_short_id: str, slot_key: str, delay: float) -> str:
    """Return a script source that schedules a delayed-echo invoke
    on ``slot_key`` with the given delay."""
    return (
        "from _fxtr_mcp_builtin import schedule_invoke, get_service\n"
        f"svc = get_service({svc_short_id!r})\n"
        "await schedule_invoke("
        f"{slot_key!r}, svc, lambda s: s.echo({slot_key!r}, {delay!r})"
        ")\n"
    )


# --- Tests -------------------------------------------------------------


@pytest.mark.slow
async def test_wait_seconds_blocks_until_invoke_resolves(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """With ``wait_seconds > 0``, ``run_script`` blocks until the
    just-scheduled invoke promotes and the ``<event …>`` inline
    block ends up in the same response — no separate
    ``wait_for_slot_notification`` round-trip needed."""
    async with _delayed_echo_session(tmp_path, sandbox_env_supervisor) as (
        ctx,
        _app_ctx,
        svc_short_id,
    ):
        script = _schedule_invoke_script(svc_short_id, "r", delay=0.2)
        # Wait up to 5s — well above the 0.2s delay.
        r = await _run(ctx, script, wait_seconds=5.0)
        assert "Error" not in r, r
        # The invoke result event was buffered during the wait and
        # drained into the response.
        assert 'kind="invoke_result"' in r, r
        assert 'slot_key="r"' in r, r
        assert "Timed out" not in r, r


@pytest.mark.slow
async def test_wait_seconds_times_out(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """When the budget elapses with slots still pending, the
    response gets a one-line "Timed out…" tail naming them. The
    script body / mutation summary stays intact above the tail.
    """
    async with _delayed_echo_session(tmp_path, sandbox_env_supervisor) as (
        ctx,
        _app_ctx,
        svc_short_id,
    ):
        # Invoke delay (3s) >> wait_seconds (0.2s) → guaranteed timeout.
        script = _schedule_invoke_script(svc_short_id, "r", delay=3.0)
        r = await _run(ctx, script, wait_seconds=0.2)
        assert "Script ran" in r, r
        assert "Timed out" in r, r
        assert "'r'" in r, r


@pytest.mark.slow
async def test_wait_seconds_zero_no_timeout_tail(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """Even when the invoke definitely won't resolve in the 0.1s
    trail-delay window, the default ``wait_seconds=0.0`` path must
    never emit a "Timed out…" tail (that's a wait-branch-only
    artifact)."""
    async with _delayed_echo_session(tmp_path, sandbox_env_supervisor) as (
        ctx,
        _app_ctx,
        svc_short_id,
    ):
        # Long delay; with default wait_seconds=0.0 we don't wait
        # for it.
        script = _schedule_invoke_script(svc_short_id, "r", delay=5.0)
        r = await _run(ctx, script)
        assert "Script ran" in r, r
        assert "Timed out" not in r, r


@pytest.mark.slow
async def test_wait_seconds_negative_errors(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """Negative ``wait_seconds`` is a misuse; the tool returns a
    validation error and does not run the script."""
    async with _delayed_echo_session(tmp_path, sandbox_env_supervisor) as (
        ctx,
        _app_ctx,
        svc_short_id,
    ):
        script = _schedule_invoke_script(svc_short_id, "r", delay=0.1)
        r = await _run(ctx, script, wait_seconds=-1.0)
        assert "Error" in r, r
        assert "wait_seconds" in r, r


@pytest.mark.slow
async def test_wait_seconds_only_waits_for_this_scripts_invokes(
    tmp_path: Path,
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> None:
    """A long invoke scheduled by an earlier script must not block
    a later ``run_script(..., wait_seconds=...)`` whose own invoke
    resolves quickly. Verifies the per-script scoping of the wait
    set.
    """
    async with _delayed_echo_session(tmp_path, sandbox_env_supervisor) as (
        ctx,
        app_ctx,
        svc_short_id,
    ):
        # Script 1: schedule a long invoke; default wait_seconds=0
        # → don't wait for it.
        long_script = _schedule_invoke_script(svc_short_id, "long", delay=10.0)
        r1 = await _run(ctx, long_script)
        assert "Error" not in r1, r1
        # Long invoke is still pending after the 0.1s trail-delay.
        conn = app_ctx.connection
        assert conn is not None
        long_value = conn.peek_slot_key("long")
        assert long_value is not None and long_value[0] == "invoke_pending", long_value

        # Script 2: schedule a short invoke and wait. Should return
        # in ~0.2s, well before the long invoke (10s) resolves.
        short_script = _schedule_invoke_script(svc_short_id, "short", delay=0.2)
        with anyio.fail_after(5.0):  # generous bound, far below 10s
            r2 = await _run(ctx, short_script, wait_seconds=4.0)
        assert "Error" not in r2, r2
        assert 'kind="invoke_result"' in r2, r2
        assert 'slot_key="short"' in r2, r2
        assert "Timed out" not in r2, r2

        # And the long invoke is still pending — wait_seconds=4
        # only waited for "short", not "long".
        long_value_after = conn.peek_slot_key("long")
        assert (
            long_value_after is not None and long_value_after[0] == "invoke_pending"
        ), long_value_after
