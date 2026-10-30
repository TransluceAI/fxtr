"""Supervisor state-machine integration tests (stage 7 post-3).

The supervisor's lifecycle collapsed to one enum + two events
in stage7post3. These tests cover:

- The initial PAUSED state with ``pause_reason = "(not started)"``
  before any tool call.
- The ``connect`` tool's wake-and-wait-ready flow.
- Wire-error free-retry behavior, including budget consumption,
  refresh on successful ``wake_and_wait_ready`` return, and
  fallback to PAUSED once exhausted.
- Non-wire crashes pausing immediately with a formatted reason.
- ``SupervisorNotReady`` surfacing after one in-call retry attempt.
- End-to-end recovery via ``close_session(force=True)`` after a
  poisoned-session pause.

Forcing a wire drop: we reach into the connection's ``_ws`` field
and call ``ws.close()``. The reader's ``ws.receive_text()`` raises,
the supervisor catches the resulting ``ConnectionLost``, and either
runs a free retry or pauses (depending on the budget).
"""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path
from typing import Any

import anyio
import pytest
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr_workstation import WorkstationServer, expose_entity

from fxtr_workstation_mcp.app_context import AppContext
from fxtr_workstation_mcp.config import CONFIG_FILENAME
from fxtr_workstation_mcp.server import (
    close_session,
    connect,
    disconnect,
    open_session,
    run_script,
    set_working_dir,
)
from fxtr_workstation_mcp.sandbox_env import SandboxEnvSupervisor
from fxtr_workstation_mcp.server import make_app_ctx
from fxtr_workstation_mcp.ws_connection import (
    MAX_FREE_RETRY_BUDGET,
    ScriptOutcome,
    SupervisorNotReady,
    SupervisorState,
    WorkstationConnection,
)

from ._peer import connect_peer_rpc

pytestmark = pytest.mark.anyio


_TEST_NS = uuid.UUID("99999999-aaaa-bbbb-cccc-dddddddddddd")
_TEST_NOTE_TYPE_ID = system_entities.define_type(_TEST_NS, "ws_mcp_test_note_3b")


@dataclasses.dataclass(frozen=True)
class _TestNote(DataclassEntity, fxtr_type=_TEST_NOTE_TYPE_ID):
    body: str


# --- Fake MCP-context helpers ----------------------------------------


class _FakeRequestContext:
    def __init__(self, app_ctx: AppContext) -> None:
        self.lifespan_context = app_ctx


class _FakeContext:
    def __init__(self, app_ctx: AppContext) -> None:
        self.request_context = _FakeRequestContext(app_ctx)
        # session is the ServerSession FastMCP would normally
        # provide; we don't have one in unit tests, but the
        # server's _app_ctx helper assigns it to
        # app_ctx.latest_session so it has to exist. None
        # means channel notifications drop silently — fine for
        # tests that don't assert on them.
        self.session = None


async def _swd(ctx: _FakeContext, p: str) -> str:
    return await set_working_dir(p, ctx)  # type: ignore[arg-type]


async def _conn(ctx: _FakeContext) -> str:
    return await connect(ctx)  # type: ignore[arg-type]


async def _open(ctx: _FakeContext, md: dict[str, Any] | None = None) -> str:
    return await open_session(ctx, md)  # type: ignore[arg-type]


async def _close(ctx: _FakeContext) -> str:
    return await close_session(ctx)  # type: ignore[arg-type]


async def _run(ctx: _FakeContext, src: str) -> str:
    return await run_script(src, ctx)  # type: ignore[arg-type]


async def _disconnect(ctx: _FakeContext) -> str:
    return await disconnect(ctx)  # type: ignore[arg-type]


async def _force_ws_drop(connection: WorkstationConnection) -> None:
    """Force-close the supervisor's ``/client`` websocket so the
    reader sees an EOF and the supervisor catches the resulting
    ``ConnectionLost``."""
    ws = connection._ws  # noqa: SLF001 — test peeks at private state
    assert ws is not None, "connection isn't currently up"
    await ws.close()


async def _wait_for_state(
    connection: WorkstationConnection,
    target: SupervisorState,
    *,
    max_seconds: float = 5.0,
    poll: float = 0.01,
) -> None:
    """Poll until the supervisor reaches ``target``."""
    elapsed = 0.0
    while connection.state != target:
        if elapsed >= max_seconds:
            raise AssertionError(
                f"supervisor did not reach {target} within {max_seconds}s "
                f"(currently {connection.state}, pause_reason={connection.pause_reason!r})"
            )
        await anyio.sleep(poll)
        elapsed += poll


async def _wait_for_next_ready_handshake(
    connection: WorkstationConnection,
    state_changed_before: anyio.Event,
    *,
    max_seconds: float = 5.0,
) -> None:
    """Wait for the supervisor to leave its current cycle and reach
    READY again. Used after a forced wire drop: capturing
    ``connection._state_changed`` before the drop, then awaiting
    that specific event reference, guarantees we see at least one
    state transition (so a state == READY check afterwards isn't
    seeing the pre-drop state)."""
    with anyio.fail_after(max_seconds):
        await state_changed_before.wait()
    await _wait_for_state(connection, SupervisorState.READY, max_seconds=max_seconds)


# --- Initial / wake / shutdown ---------------------------------------


async def test_supervisor_starts_in_paused_state(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """After ``set_working_dir`` (which constructs and ``start()``s
    the connection), the supervisor is immediately in PAUSED with
    reason ``"(not started)"``. No work has been done."""
    async with WorkstationServer() as ws:
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# stage7post3 initial-paused\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            assert "Working dir set" in (await _swd(ctx, str(tmp_path)))

            connection = app_ctx.connection
            assert connection is not None
            assert connection.state == SupervisorState.PAUSED
            assert connection.pause_reason == "(not started)"
            assert not connection.is_ready


async def test_connect_tool_wakes_supervisor(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """``connect`` calls ``wake_and_wait_ready``: the supervisor
    leaves PAUSED, runs one cycle, transitions to READY. The tool
    returns the connected confirmation."""
    async with WorkstationServer() as ws:
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# stage7post3 connect wakes\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            assert "Working dir set" in (await _swd(ctx, str(tmp_path)))

            connection = app_ctx.connection
            assert connection is not None
            assert connection.state == SupervisorState.PAUSED

            r = await _conn(ctx)
            assert r.startswith("connected to"), r
            assert connection.state == SupervisorState.READY


async def test_lifespan_shutdown_transitions_to_exited(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """Lifespan exit signals shutdown; the supervisor reaches
    EXITED. Subsequent ``wake_and_wait_ready`` raises
    :class:`SupervisorNotReady` immediately."""
    async with WorkstationServer() as ws:
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# stage7post3 shutdown→exited\n")

        connection: WorkstationConnection | None = None
        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            assert "Working dir set" in (await _swd(ctx, str(tmp_path)))
            assert (await _conn(ctx)).startswith("connected to")
            connection = app_ctx.connection
            assert connection is not None
            assert connection.state == SupervisorState.READY

        # Lifespan exited → supervisor.shutdown() awaited.
        assert connection.state == SupervisorState.EXITED
        with pytest.raises(SupervisorNotReady):
            await connection.wake_and_wait_ready()


# --- Wire blip free-retry --------------------------------------------


async def test_reconnect_after_forced_disconnect(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """Force a /client disconnect after open_session; verify the
    supervisor free-retries silently (no PAUSED transition), the
    new cycle subscribes to the same session, and a follow-up tool
    call works."""
    async with WorkstationServer() as ws:
        async with connect_peer_rpc(ws.rpc_url) as peer_session:
            note_id: FxtrID = _TestNote(body="hi").store(scope=peer_session.scope).entity_id
            async with expose_entity(
                peer_session.remote_service,
                external_name="bg",
                entity=note_id,
                scope=peer_session.scope,
            ) as bg_short_id:
                (tmp_path / CONFIG_FILENAME).write_text(
                    f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
                )
                (tmp_path / "preamble.py").write_text("# stage7post3 free-retry\n")

                async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
                    ctx = _FakeContext(app_ctx)
                    assert "Working dir set" in (await _swd(ctx, str(tmp_path)))
                    assert (await _conn(ctx)).startswith("connected to")
                    open_msg = await _open(ctx, {"phase": "free-retry"})
                    session_id = open_msg.removeprefix("opened session ").strip()

                    connection = app_ctx.connection
                    assert connection is not None
                    assert connection.state == SupervisorState.READY
                    pre_budget = connection._free_retry_budget  # noqa: SLF001

                    # Force the wire drop. The supervisor should
                    # consume one free retry and bring the new
                    # cycle to READY without ever entering PAUSED.
                    # Capture state_changed BEFORE the drop so we
                    # don't race past the transition.
                    ev = connection._state_changed  # noqa: SLF001
                    await _force_ws_drop(connection)
                    await _wait_for_next_ready_handshake(connection, ev)
                    assert connection.active_session_id == session_id
                    assert connection._free_retry_budget == pre_budget - 1  # noqa: SLF001

                    # Follow-up call works against the same session.
                    script_lookup = (
                        "from _fxtr_mcp_builtin import lookup_entity\n"
                        f"e = await lookup_entity({bg_short_id!r})\n"
                        f"assert str(e) == {str(note_id)!r}\n"
                    )
                    r = await _run(ctx, script_lookup)
                    assert "Script ran" in r, r

                    # The follow-up's wake_and_wait_ready return
                    # refreshed the budget to MAX.
                    assert connection._free_retry_budget == MAX_FREE_RETRY_BUDGET  # noqa: SLF001

                    assert (await _close(ctx)).startswith("closed session ")


async def test_free_retry_budget_refreshes_on_ready_handshake(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """Each successful ``wake_and_wait_ready`` return resets the
    budget. So drop → free-retry → tool-call refreshes → drop →
    free-retry again works for many cycles in a row, with the
    budget staying at MAX-1 immediately after each drop."""
    async with WorkstationServer() as ws:
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# stage7post3 budget-refresh\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            assert "Working dir set" in (await _swd(ctx, str(tmp_path)))
            assert (await _conn(ctx)).startswith("connected to")
            assert (await _open(ctx, {"phase": "budget-refresh"})).startswith("opened session ")

            connection = app_ctx.connection
            assert connection is not None

            # Three consecutive drops, each followed by a tool call
            # that succeeds (and refreshes the budget). The budget
            # should never reach zero.
            for _ in range(3):
                assert connection._free_retry_budget == MAX_FREE_RETRY_BUDGET  # noqa: SLF001
                ev = connection._state_changed  # noqa: SLF001
                await _force_ws_drop(connection)
                await _wait_for_next_ready_handshake(connection, ev)
                assert connection._free_retry_budget == MAX_FREE_RETRY_BUDGET - 1  # noqa: SLF001
                r = await _run(ctx, "# refresh-budget noop\n")
                assert "Script ran" in r, r
                assert connection._free_retry_budget == MAX_FREE_RETRY_BUDGET  # noqa: SLF001

            assert (await _close(ctx)).startswith("closed session ")


# --- Non-wire crash → PAUSED -----------------------------------------


async def test_non_wire_post_ready_crash_pauses_without_free_retry(
    tmp_path: Path,
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> None:
    """A non-wire post-ready crash routes through PAUSED
    immediately — the free-retry path is wire-only. The next tool
    call wakes the supervisor; a fresh cycle runs and the call
    succeeds."""
    async with WorkstationServer() as ws:
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# stage7post3 non-wire pause\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            assert "Working dir set" in (await _swd(ctx, str(tmp_path)))
            assert (await _conn(ctx)).startswith("connected to")
            assert (await _open(ctx, {"phase": "non-wire"})).startswith("opened session ")

            connection = app_ctx.connection
            assert connection is not None
            session_id = connection.active_session_id
            assert session_id is not None
            pre_budget = connection._free_retry_budget  # noqa: SLF001

            real = connection._handle_script_job  # noqa: SLF001
            fired = False

            async def _crashy_handle_script_job(job: Any) -> None:
                nonlocal fired
                if not fired:
                    fired = True
                    exc = RuntimeError("injected: bypassing per-handler catch")
                    job.completion.set_exception(exc)
                    raise exc
                await real(job)

            connection._handle_script_job = _crashy_handle_script_job  # type: ignore[method-assign]  # noqa: SLF001

            r = await _run(ctx, "# this will escape the handler\n")
            assert "injected" in r, r

            await _wait_for_state(connection, SupervisorState.PAUSED)
            assert connection.pause_reason is not None
            assert "injected" in connection.pause_reason
            # Free retry was NOT consumed for a non-wire crash.
            assert connection._free_retry_budget == pre_budget  # noqa: SLF001

            connection._handle_script_job = real  # type: ignore[method-assign]  # noqa: SLF001
            r2 = await _run(ctx, "# post-park noop\n")
            assert "Script ran" in r2, r2
            assert connection.state == SupervisorState.READY
            assert connection.active_session_id == session_id

            assert (await _close(ctx)).startswith("closed session ")


async def test_paused_state_carries_string_reason(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """The ``pause_reason`` string is formatted via
    :func:`_format_pause_reason` and matches expected substrings
    for the underlying exception kind."""
    async with WorkstationServer() as ws:
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# stage7post3 reason-format\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            assert "Working dir set" in (await _swd(ctx, str(tmp_path)))
            assert (await _conn(ctx)).startswith("connected to")
            assert (await _open(ctx, {"phase": "reason-format"})).startswith("opened session ")

            connection = app_ctx.connection
            assert connection is not None

            real = connection._handle_script_job  # noqa: SLF001

            async def _crashy(job: Any) -> None:
                exc = NotImplementedError("specific reason text")
                job.completion.set_exception(exc)
                raise exc

            connection._handle_script_job = _crashy  # type: ignore[method-assign]  # noqa: SLF001

            r = await _run(ctx, "# trigger NotImplementedError\n")
            assert "specific reason text" in r

            await _wait_for_state(connection, SupervisorState.PAUSED)
            reason = connection.pause_reason
            assert reason is not None
            assert reason.startswith("unimplemented handler:"), reason
            assert "specific reason text" in reason

            connection._handle_script_job = real  # type: ignore[method-assign]  # noqa: SLF001
            assert (await _close(ctx)).startswith("closed session ")


# --- wake_and_wait_ready repeats-pause behavior ----------------------


async def test_wake_and_wait_ready_raises_on_repeated_pause(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """If a cycle paused and the retry attempt ALSO pauses,
    :meth:`wake_and_wait_ready` raises :class:`SupervisorNotReady`
    rather than looping forever.

    Setup: point the connection at an unreachable workstation
    URL (port 1 — ECONNREFUSED on localhost). Every cycle's
    ``/rpc`` open fails with ``ConnectionLost``; the cycle never
    reaches READY, so the wire-error free-retry path doesn't
    apply. The first ``wake_and_wait_ready`` call signals retry,
    the supervisor tries again, fails again, transitions to
    PAUSED a second time, and the call raises.

    This is also the canonical shape of "agent calls ``connect``
    against a workstation that isn't running" — the MCP stays
    alive, the tool gets one ``SupervisorNotReady`` per call,
    the supervisor parks waiting for the next attempt.
    """
    (tmp_path / CONFIG_FILENAME).write_text('workstation_url = "ws://127.0.0.1:1"\n')
    (tmp_path / "preamble.py").write_text("# stage7post3 repeat-pause\n")

    async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
        ctx = _FakeContext(app_ctx)
        assert "Working dir set" in (await _swd(ctx, str(tmp_path)))

        connection = app_ctx.connection
        assert connection is not None
        # Supervisor begins in PAUSED with the initial reason.
        assert connection.state == SupervisorState.PAUSED
        assert connection.pause_reason == "(not started)"

        # First call: signal retry → supervisor tries to open
        # /rpc → ConnectionLost (cycle never reached READY) →
        # PAUSED with the wire-error reason → wake_and_wait_ready
        # sees PAUSED again and raises.
        with pytest.raises(SupervisorNotReady) as exc_info:
            await connection.wake_and_wait_ready()
        assert "connection lost:" in str(exc_info.value), exc_info.value

        # The connection stays alive in PAUSED; a second call
        # makes a fresh attempt and pauses the same way. No MCP
        # tear-down between calls.
        assert connection.state == SupervisorState.PAUSED
        with pytest.raises(SupervisorNotReady):
            await connection.wake_and_wait_ready()


# --- script-job per-handler exception (unchanged behavior) ----------


async def test_script_job_exception_does_not_pause_supervisor(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """When the script-job body raises a regular ``Exception``,
    :meth:`_handle_script_job` absorbs it onto the completion
    future and returns; the worker keeps draining and the
    supervisor stays in READY. The agent sees the exception via
    the tool result; no PAUSED transition happens."""
    async with WorkstationServer() as ws:
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# stage7post3 absorb-Exception\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            assert "Working dir set" in (await _swd(ctx, str(tmp_path)))
            assert (await _conn(ctx)).startswith("connected to")
            assert (await _open(ctx, {"phase": "absorb-exception"})).startswith("opened session ")

            connection = app_ctx.connection
            assert connection is not None
            session_id = connection.active_session_id
            assert session_id is not None

            real = connection._do_script_job  # noqa: SLF001
            fired = False

            async def _erroring_do_script_job(job: Any) -> ScriptOutcome:
                nonlocal fired
                if not fired:
                    fired = True
                    raise RuntimeError("injected: simulating a body-level bug")
                return await real(job)

            connection._do_script_job = _erroring_do_script_job  # type: ignore[method-assign]  # noqa: SLF001

            r = await _run(ctx, "# this body raises via the injection\n")
            assert "injected" in r, r

            # Still READY — the per-handler Exception catch
            # absorbed the error.
            assert connection.state == SupervisorState.READY
            assert connection.active_session_id == session_id

            connection._do_script_job = real  # type: ignore[method-assign]  # noqa: SLF001
            r2 = await _run(ctx, "# post-error noop\n")
            assert "Script ran" in r2, r2
            assert connection.state == SupervisorState.READY

            assert (await _close(ctx)).startswith("closed session ")


# --- Force-close recovery from a paused supervisor -------------------


async def test_park_then_force_close_recovers_to_clean_state(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """End-to-end recovery flow: a non-wire crash pauses the
    supervisor; the agent uses ``close_session(force=True)`` to
    clear the poisoned session; then ``connect`` + ``open_session``
    succeed on a fresh session."""
    async with WorkstationServer() as ws:
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# stage7post3 park+force-close\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            assert "Working dir set" in (await _swd(ctx, str(tmp_path)))
            assert (await _conn(ctx)).startswith("connected to")
            r = await _open(ctx, {"phase": "park-then-force"})
            old_session_id = r.removeprefix("opened session ").strip()

            connection = app_ctx.connection
            assert connection is not None

            real = connection._handle_script_job  # noqa: SLF001

            async def _crashy(job: Any) -> None:
                exc = RuntimeError("park-then-force-close repro")
                job.completion.set_exception(exc)
                raise exc

            connection._handle_script_job = _crashy  # type: ignore[method-assign]  # noqa: SLF001
            r = await _run(ctx, "# triggers the crash\n")
            assert "park-then-force-close repro" in r
            connection._handle_script_job = real  # type: ignore[method-assign]  # noqa: SLF001

            await _wait_for_state(connection, SupervisorState.PAUSED)

            # Force-close. The supervisor stays paused during this
            # call; the one-shot websocket bypasses it entirely.
            r = await close_session(ctx, force=True)  # type: ignore[arg-type]
            assert "force-closed session" in r and old_session_id in r, r

            # Workstation dropped the session.
            assert uuid.UUID(old_session_id) not in ws.state.sessions

            # connect() wakes the supervisor; the new cycle has
            # no active session so subscribe is skipped.
            r = await _conn(ctx)
            assert r.startswith("connected to") and "session=none" in r, r
            assert connection.state == SupervisorState.READY

            r = await _open(ctx, {"phase": "after-recovery"})
            new_session_id = r.removeprefix("opened session ").strip()
            assert new_session_id != old_session_id

            assert (await _close(ctx)).startswith("closed session ")


# --- Tool call across wire drop -------------------------------------


@pytest.mark.flaky(reruns=2)
async def test_run_after_disconnect_runs_against_post_retry_cycle(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """A tool call submitted while the supervisor is mid-free-retry
    waits in ``wake_and_wait_ready`` until the new cycle reaches
    READY, then runs there.

    Marked flaky pending investigation. Has been observed to fail
    intermittently under ``pytest -n auto`` with
    ``WorkstationRejected: subscribe_session(...) failed: no
    session with id ...`` during the retry's resubscribe. Always
    passes in isolation. Root cause not yet pinned down — rerun
    on failure rather than block the suite.
    """
    async with WorkstationServer() as ws:
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# stage7post3 queued-call\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            assert "Working dir set" in (await _swd(ctx, str(tmp_path)))
            assert (await _conn(ctx)).startswith("connected to")
            open_msg = await _open(ctx, {"phase": "queued-call"})
            session_id = open_msg.removeprefix("opened session ").strip()

            connection = app_ctx.connection
            assert connection is not None

            # Capture _state_changed *before* dropping so we don't
            # race past the transition. Then drop the wire, wait for
            # the supervisor's reader to detect EOF and the state to
            # leave READY (i.e. enter STARTING under the free-retry
            # path). Only after that do we submit the script — if we
            # raced past this point the still-READY state would let
            # ``wake_and_wait_ready`` return immediately and the
            # worker would start the script in a doomed cycle.
            ev = connection._state_changed  # noqa: SLF001
            await _force_ws_drop(connection)
            with anyio.fail_after(2.0):
                await ev.wait()
            assert connection.state != SupervisorState.READY

            r = await _run(ctx, "# tiny noop\n")
            assert "Script ran" in r, r

            assert connection.state == SupervisorState.READY
            assert connection.active_session_id == session_id

            assert (await _close(ctx)).startswith("closed session ")


# --- Fix 1: toml re-read on reconnect --------------------------------


async def test_cycle_refreshes_active_session_from_toml(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """Manually clearing ``active_session`` in the toml takes effect
    on the next supervisor cycle.

    Scenario (the bug fix 1 targets): the workstation restarted and
    lost the session UUID the toml points at. The supervisor wakes,
    tries to subscribe, gets ``no session with id ...``, pauses.
    Before fix 1 the in-memory ``_active_session_id`` was frozen at
    construction time, so even after the agent edited the toml the
    next wake re-tried the same stale UUID forever. Fix 1 makes the
    supervisor re-read the toml at the top of every cycle.

    Test:
    1. Toml points at a bogus UUID the workstation never created.
       First ``connect`` call wakes the supervisor → cycle refreshes
       config → subscribes → fails → PAUSED.
    2. Rewrite the toml without ``active_session``.
    3. Second ``connect`` call wakes the supervisor again → cycle
       refreshes config → sees no active session → skips subscribe
       → READY.
    """
    bogus_uuid = uuid.uuid4()
    async with WorkstationServer() as ws:
        toml_path = tmp_path / CONFIG_FILENAME
        toml_path.write_text(
            f'workstation_url = "{ws.bound_url}"\n'
            f'active_session = "{bogus_uuid}"\n'
            "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# fix1 toml-refresh test\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            assert "Working dir set" in (await _swd(ctx, str(tmp_path)))

            connection = app_ctx.connection
            assert connection is not None

            # First connect: stale UUID → subscribe fails → PAUSED.
            r1 = await _conn(ctx)
            assert "Error" in r1, r1
            assert str(bogus_uuid) in r1 or "no session with id" in r1, r1
            assert connection.state == SupervisorState.PAUSED
            assert connection.pause_reason is not None
            assert "no session with id" in connection.pause_reason, connection.pause_reason

            # Agent clears active_session from the toml.
            toml_path.write_text(
                f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
            )

            # Second connect: cycle re-reads toml, sees no active
            # session, skips subscribe, transitions to READY.
            r2 = await _conn(ctx)
            assert r2.startswith("connected to"), r2
            assert "session=none" in r2, r2
            assert connection.state == SupervisorState.READY
            assert connection.active_session_id is None


async def test_cycle_pauses_on_invalid_toml(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """A corrupted toml at cycle start raises ``ConfigInvalid`` from
    ``_refresh_config_from_disk``; supervisor pauses with the
    ``ConfigError`` message as ``pause_reason`` rather than
    crashing. Agent fixes the file and retries — no MCP restart."""
    async with WorkstationServer() as ws:
        toml_path = tmp_path / CONFIG_FILENAME
        toml_path.write_text(f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n")
        (tmp_path / "preamble.py").write_text("# fix1 invalid-toml test\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            assert "Working dir set" in (await _swd(ctx, str(tmp_path)))
            connection = app_ctx.connection
            assert connection is not None

            # Corrupt the toml before the first connect call.
            toml_path.write_text("this is not valid toml = = =\n")

            r1 = await _conn(ctx)
            assert "Error" in r1, r1
            assert connection.state == SupervisorState.PAUSED
            assert connection.pause_reason is not None
            assert "workstation rejected" in connection.pause_reason, connection.pause_reason

            # Restore the toml; next connect succeeds.
            toml_path.write_text(
                f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
            )
            r2 = await _conn(ctx)
            assert r2.startswith("connected to"), r2
            assert connection.state == SupervisorState.READY


# --- Fix 5: disconnect() tool ----------------------------------------


# Channel-capturing context to assert on the supervisor_paused
# banner suppression. Same pattern as test_callback_e2e.


class _ChannelFrame:
    __slots__ = ("content", "attrs")

    def __init__(self, content: str, attrs: dict[str, str]) -> None:
        self.content = content
        self.attrs = attrs


class _FakeWriteStream:
    def __init__(self) -> None:
        self.frames: list[_ChannelFrame] = []

    async def send(self, message: Any) -> None:
        notif = message.message.root
        params = notif.params
        self.frames.append(_ChannelFrame(content=params["content"], attrs=params["meta"]))


class _FakeSessionWithStream:
    def __init__(self) -> None:
        self._write_stream = _FakeWriteStream()


class _FakeContextWithStream(_FakeContext):
    def __init__(self, app_ctx: AppContext) -> None:
        super().__init__(app_ctx)
        self.session = _FakeSessionWithStream()


def _supervisor_paused_frames(ctx: _FakeContextWithStream) -> list[_ChannelFrame]:
    return [
        f for f in ctx.session._write_stream.frames if f.attrs.get("kind") == "supervisor_paused"
    ]


async def test_disconnect_from_ready_parks_without_banner(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """READY → ``disconnect()`` → PAUSED with the
    ``"disconnected by request"`` reason; **no**
    ``supervisor_paused`` channel banner is emitted."""
    async with WorkstationServer() as ws:
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# fix5 disconnect from ready\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContextWithStream(app_ctx)
            assert "Working dir set" in (await _swd(ctx, str(tmp_path)))
            assert (await _conn(ctx)).startswith("connected to")

            connection = app_ctx.connection
            assert connection is not None
            assert connection.state == SupervisorState.READY

            r = await _disconnect(ctx)
            assert "Disconnected" in r, r
            assert connection.state == SupervisorState.PAUSED
            assert connection.pause_reason == "disconnected by request"
            assert _supervisor_paused_frames(ctx) == [], (
                "agent-requested disconnect should not emit a "
                f"supervisor_paused banner; saw frames: "
                f"{ctx.session._write_stream.frames}"
            )


async def test_disconnect_from_paused_returns_already(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """Calling ``disconnect()`` while already PAUSED is a no-op
    with the ``"Already disconnected."`` response — no banner,
    no state change."""
    (tmp_path / CONFIG_FILENAME).write_text('workstation_url = "ws://127.0.0.1:1"\n')
    (tmp_path / "preamble.py").write_text("# fix5 already-disconnected\n")

    async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
        ctx = _FakeContextWithStream(app_ctx)
        assert "Working dir set" in (await _swd(ctx, str(tmp_path)))
        connection = app_ctx.connection
        assert connection is not None
        # Supervisor begins PAUSED with reason "(not started)";
        # disconnect from this state should short-circuit.
        assert connection.state == SupervisorState.PAUSED
        r = await _disconnect(ctx)
        assert r == "Already disconnected.", r
        assert connection.state == SupervisorState.PAUSED
        # Pause reason untouched.
        assert connection.pause_reason == "(not started)"


async def test_disconnect_then_reconnect_picks_up_toml_edit(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """Fix 1 + Fix 5 integration: open a session, disconnect,
    edit the toml's ``active_session`` to a fresh UUID, then
    ``connect()`` again — the new cycle re-reads the toml and
    subscribes to whatever's there now (or no session if
    cleared).
    """
    async with WorkstationServer() as ws:
        toml_path = tmp_path / CONFIG_FILENAME
        toml_path.write_text(f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n")
        (tmp_path / "preamble.py").write_text("# fix1+5 toml reload via disconnect\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            await _swd(ctx, str(tmp_path))
            await _conn(ctx)
            open_msg = await _open(ctx, {"phase": "fix5-integration"})
            session_id = open_msg.removeprefix("opened session ").strip()

            connection = app_ctx.connection
            assert connection is not None
            assert connection.state == SupervisorState.READY
            assert connection.active_session_id == session_id

            # The toml is up to date with the session we just
            # opened (open_session wrote it). Verify by editing
            # active_session OUT externally, disconnecting, and
            # asserting the next connect skips subscribe.
            toml_path.write_text(
                f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
            )

            r = await _disconnect(ctx)
            assert "Disconnected" in r, r
            assert connection.state == SupervisorState.PAUSED

            r2 = await _conn(ctx)
            assert r2.startswith("connected to"), r2
            assert "session=none" in r2, r2
            assert connection.state == SupervisorState.READY
            assert connection.active_session_id is None


@pytest.mark.skip(
    reason=(
        "Something in the cancel-mid-script teardown chain hangs the "
        "test-harness shutdown when this test runs: the test body's "
        "assertions all pass (the supervisor reaches PAUSED with the "
        "right reason, the in-flight script's completion future fires), "
        "but pytest never exits after the passed banner. The hang has "
        "not been isolated — plausible culprits include the eryx "
        "sandbox subprocess not terminating after host-side "
        "cancellation, an inner_tg child stuck in an uncancellable "
        "region, the fxtr2 RPC channel cleanup, or the WorkstationServer "
        "fixture's teardown. Disconnect-during-flight is rare in "
        "practice (agents call disconnect() when idle or when the cycle "
        "looks stuck, not while a script is actively running) so we "
        "leave the test in the file as a marker for future investigation."
    )
)
async def test_disconnect_aborts_in_flight_script(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """A ``run_script`` mid-flight when ``disconnect()`` fires
    sees its job aborted by the cycle teardown — same shape as a
    wire blip would produce — and the supervisor lands in
    PAUSED with the disconnect reason."""
    async with WorkstationServer() as ws:
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# fix5 disconnect aborts script\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            await _swd(ctx, str(tmp_path))
            await _conn(ctx)
            await _open(ctx, {"phase": "fix5-abort"})

            connection = app_ctx.connection
            assert connection is not None
            assert connection.state == SupervisorState.READY

            # Long-sleeping script: kick it off in the background,
            # then disconnect from this task while the worker is
            # parked inside the sandbox runner.
            script_result: dict[str, str] = {}

            async def _run_long_script() -> None:
                try:
                    script_result["body"] = await _run(
                        ctx,
                        "import anyio\nawait anyio.sleep(10.0)\n",
                    )
                except BaseException as exc:  # noqa: BLE001 — test bookkeeping
                    script_result["raised"] = repr(exc)

            async with anyio.create_task_group() as tg:
                tg.start_soon(_run_long_script)
                # Give the script time to reach the sandbox-side
                # sleep — connect, queue, runner.run() startup.
                await anyio.sleep(0.5)
                r = await _disconnect(ctx)
                assert "Disconnected" in r, r

            assert connection.state == SupervisorState.PAUSED
            assert connection.pause_reason == "disconnected by request"
            # The in-flight run_script either returned with an
            # error-shaped string or raised — either way the
            # call is no longer hanging.
            assert ("body" in script_result) or ("raised" in script_result), script_result
