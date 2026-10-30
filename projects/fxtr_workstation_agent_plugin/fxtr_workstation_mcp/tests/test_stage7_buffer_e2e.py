"""End-to-end tests for stage-7 buffer wiring.

Exercises behaviors that need a real workstation + the
:class:`OutgoingBuffer` wired into the lifespan / supervisor:

- Self-commit notification appears in a ``run_script`` drain.
- ``wait_for_slot_notification`` returns inline when the slot
  resolves during the wait.
- ``wait_for_slot_notification`` times out cleanly.
- ``wait_for_slot_notification`` errors when the slot is already
  terminal (a foreign mutator can't promote it to anything new
  in the future) or unknown.

Event injection uses the callback-tester service so we don't
need a second mutator session — the same path stage-6's tests
use.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import anyio
import pytest
from fxtr_workstation import WorkstationServer

from fxtr_workstation_mcp.app_context import AppContext
from fxtr_workstation_mcp.config import CONFIG_FILENAME
from fxtr_workstation_mcp.server import (
    connect,
    open_session,
    run_script,
    set_working_dir,
    wait_for_slot_notification,
)
from fxtr_workstation_mcp.sandbox_env import SandboxEnvSupervisor
from fxtr_workstation_mcp.server import make_app_ctx

from ._events import parse_events
from ._peer import connect_peer_rpc
from .test_callback_e2e import _callback_tester_running

pytestmark = pytest.mark.anyio


# --- Test context shim (same shape as test_callback_e2e._FakeContext) ----


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
        self.tool_outputs: list[str] = []


async def _swd(ctx: _FakeContext, p: str) -> str:
    r = await set_working_dir(p, ctx)  # type: ignore[arg-type]
    ctx.tool_outputs.append(r)
    return r


async def _conn(ctx: _FakeContext) -> str:
    r = await connect(ctx)  # type: ignore[arg-type]
    ctx.tool_outputs.append(r)
    return r


async def _open(ctx: _FakeContext, md: dict[str, Any] | None = None) -> str:
    r = await open_session(ctx, md)  # type: ignore[arg-type]
    ctx.tool_outputs.append(r)
    return r


async def _run(ctx: _FakeContext, src: str) -> str:
    r = await run_script(src, ctx)  # type: ignore[arg-type]
    ctx.tool_outputs.append(r)
    return r


async def _wait_slot(
    ctx: _FakeContext,
    slot_key: str,
    timeout_s: float,
    after_notif_index: int | None = None,
) -> str:
    r = await wait_for_slot_notification(  # type: ignore[arg-type]
        slot_key, timeout_s, ctx, after_notif_index=after_notif_index
    )
    ctx.tool_outputs.append(r)
    return r


@contextlib.asynccontextmanager
async def _bootstrap_session(
    tmp_path: Path, ws: Any, sandbox_env_supervisor: SandboxEnvSupervisor
) -> AsyncGenerator[_FakeContext]:
    """Boot a fresh agent_workdir against ``ws``, run lifespan,
    set_working_dir → connect → open_session. Yields the
    ``_FakeContext`` ready for run_script / wait_for_slot."""
    (tmp_path / CONFIG_FILENAME).write_text(
        f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
    )
    (tmp_path / "preamble.py").write_text("# stage-7 e2e\n")
    async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
        ctx = _FakeContext(app_ctx)
        await _swd(ctx, str(tmp_path))
        await _conn(ctx)
        await _open(ctx, {"phase": "stage-7"})
        yield ctx


# --- The tests ------------------------------------------------------


@pytest.mark.slow
async def test_run_script_self_commit_event_appears(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """Any ``run_script`` that issues a mutation emits a
    ``kind="self_commit"`` event from ``_AckCommitJob`` which lands
    in that script's tool drain (the AckCommitJob runs before the
    trail-delay window closes).

    Uses ``create_callback`` as a minimal mutation that doesn't
    require any external workstation service registration."""
    async with WorkstationServer() as ws:
        async with _bootstrap_session(tmp_path, ws, sandbox_env_supervisor) as ctx:
            r = await _run(
                ctx,
                "from _fxtr_mcp_builtin import create_callback\ncreate_callback('cb')\n",
            )
            assert "Error" not in r, r
            events = parse_events(r)
            self_commits = [e for e in events if e.attrs.get("kind") == "self_commit"]
            assert len(self_commits) == 1, (
                f"expected exactly one self_commit event in run_script drain; "
                f"got events: {[(e.attrs, e.content) for e in events]}"
            )
            assert self_commits[0].attrs.get("seq", "").isdigit()


@pytest.mark.slow
async def test_wait_for_slot_notification_resolves_inline(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """``schedule_invoke`` with a long-ish delay so the result
    event lands *after* ``run_script`` exits its trail-delay.
    Then ``wait_for_slot_notification`` blocks until the result
    arrives and returns it as a drained ``<event>`` block."""
    async with WorkstationServer() as ws:
        async with connect_peer_rpc(ws.rpc_url) as peer:
            async with _callback_tester_running(peer, "t7-tester-a") as svc_short_id:
                async with _bootstrap_session(tmp_path, ws, sandbox_env_supervisor) as ctx:
                    # Use the callback-tester service to fire a callback
                    # 0.5s after we schedule it. The result happens well
                    # after run_script's 0.1s trail-delay window — so
                    # wait_for_slot_notification will block until the
                    # CallbackEvent arrives.
                    r1 = await _run(
                        ctx,
                        "from _fxtr_mcp_builtin import create_callback, get_service, "
                        f"schedule_invoke\nsvc = get_service({svc_short_id!r})\n"
                        "cb = create_callback('w7')\n"
                        "await schedule_invoke('fire-w7', svc, lambda s: "
                        "s.register_callback_and_fire_notify(cb, 'late', 0.5))\n",
                    )
                    assert "Error" not in r1, r1

                    # The callback hasn't fired yet (delay=0.5s). The
                    # tool will block until it arrives. Allow up to 3s.
                    r2 = await _wait_slot(ctx, "w7", 3.0)
                    events = parse_events(r2)
                    cb_events = [e for e in events if e.attrs.get("kind") == "callback"]
                    assert len(cb_events) == 1, (
                        f"expected one callback event in wait drain; "
                        f"got: {[(e.attrs, e.content) for e in events]}"
                    )
                    assert cb_events[0].attrs["slot_key"] == "w7"
                    assert "late" in cb_events[0].content


@pytest.mark.slow
async def test_wait_for_slot_notification_times_out(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """A pending slot that won't be promoted within the timeout
    window surfaces a clear timeout message."""
    async with WorkstationServer() as ws:
        async with connect_peer_rpc(ws.rpc_url) as peer:
            async with _callback_tester_running(peer, "t7-tester-b") as svc_short_id:
                async with _bootstrap_session(tmp_path, ws, sandbox_env_supervisor) as ctx:
                    # Schedule a callback that fires much later than
                    # the wait timeout.
                    r1 = await _run(
                        ctx,
                        "from _fxtr_mcp_builtin import create_callback, get_service, "
                        f"schedule_invoke\nsvc = get_service({svc_short_id!r})\n"
                        "cb = create_callback('w7t')\n"
                        "await schedule_invoke('fire-w7t', svc, lambda s: "
                        "s.register_callback_and_fire_notify(cb, 'very-late', 5.0))\n",
                    )
                    assert "Error" not in r1, r1

                    r2 = await _wait_slot(ctx, "w7t", 0.5)
                    assert "Timed out" in r2, r2
                    assert "w7t" in r2


@pytest.mark.slow
async def test_wait_for_slot_notification_unknown_slot(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """Slot that's not in the session's ``data_by_key`` —
    immediate error."""
    async with WorkstationServer() as ws:
        async with _bootstrap_session(tmp_path, ws, sandbox_env_supervisor) as ctx:
            r = await _wait_slot(ctx, "never-existed", 0.5)
            assert "Error" in r, r
            assert "never-existed" in r
            assert "not present" in r


@pytest.mark.slow
async def test_wait_for_slot_notification_already_terminal(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """Slot that's already in a terminal state (``invoke_result``
    here) — returns success immediately (stage7post1 race fix).
    Pre-stage7post1 this errored; now we treat it as "you missed
    the notification but the result is ready" and let the agent
    continue.

    We use the callback-tester service's
    ``register_callback_and_fire_notify`` with ``delay=0`` which
    returns ``None`` immediately, so the invoke slot lands as
    ``["invoke_result", null]`` (terminal) during the trail-delay
    window."""
    async with WorkstationServer() as ws:
        async with connect_peer_rpc(ws.rpc_url) as peer:
            async with _callback_tester_running(peer, "t7-tester-d") as svc_short_id:
                async with _bootstrap_session(tmp_path, ws, sandbox_env_supervisor) as ctx:
                    r1 = await _run(
                        ctx,
                        "from _fxtr_mcp_builtin import create_callback, get_service, "
                        f"schedule_invoke\nsvc = get_service({svc_short_id!r})\n"
                        "cb = create_callback('term_cb')\n"
                        # delay=0 → invoke returns immediately;
                        # slot 'done' becomes ["invoke_result", null].
                        "await schedule_invoke('done', svc, lambda s: "
                        "s.register_callback_and_fire_notify(cb, 'x', 0.0))\n",
                    )
                    assert "Error" not in r1, r1
                    # Give the InvokeResultEvent time to be processed.
                    await anyio.sleep(0.3)
                    # wait_for_slot_notification on 'done' should now
                    # return success (not error) — the slot is
                    # already resolved.
                    r2 = await _wait_slot(ctx, "done", 0.5)
                    assert "Error" not in r2, r2
                    assert "already resolved" in r2
                    assert "retrieve_result" in r2


@pytest.mark.slow
async def test_wait_for_slot_notification_terminal_with_after_notif_index(
    tmp_path: Path,
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> None:
    """Terminal slot + after_notif_index — returns success with a
    note that after_notif_index is callback-only. We don't error
    because the agent's primary intent (wait for this slot's
    resolution) is met."""
    async with WorkstationServer() as ws:
        async with connect_peer_rpc(ws.rpc_url) as peer:
            async with _callback_tester_running(peer, "t7-tester-da") as svc_short_id:
                async with _bootstrap_session(tmp_path, ws, sandbox_env_supervisor) as ctx:
                    r1 = await _run(
                        ctx,
                        "from _fxtr_mcp_builtin import create_callback, get_service, "
                        f"schedule_invoke\nsvc = get_service({svc_short_id!r})\n"
                        "cb = create_callback('term_cb2')\n"
                        "await schedule_invoke('done2', svc, lambda s: "
                        "s.register_callback_and_fire_notify(cb, 'x', 0.0))\n",
                    )
                    assert "Error" not in r1, r1
                    await anyio.sleep(0.3)
                    r2 = await _wait_slot(ctx, "done2", 0.5, after_notif_index=99)
                    assert "Error" not in r2, r2
                    assert "already resolved" in r2
                    assert "after_notif_index" in r2
                    assert "callback" in r2.lower()


@pytest.mark.slow
async def test_wait_for_callback_already_in_inbox_returns_immediately(
    tmp_path: Path,
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> None:
    """Callback notification lands while the agent is composing
    the wait_for call. The slot is still ``callback_open`` (callbacks
    don't auto-promote), but the inbox now has an entry. Without
    after_notif_index, the tool returns immediately with a hint.
    """
    async with WorkstationServer() as ws:
        async with connect_peer_rpc(ws.rpc_url) as peer:
            async with _callback_tester_running(peer, "t7-tester-cb") as svc_short_id:
                async with _bootstrap_session(tmp_path, ws, sandbox_env_supervisor) as ctx:
                    r1 = await _run(
                        ctx,
                        "from _fxtr_mcp_builtin import create_callback, get_service, "
                        f"schedule_invoke\nsvc = get_service({svc_short_id!r})\n"
                        "cb = create_callback('race_cb')\n"
                        "await schedule_invoke('rfire', svc, lambda s: "
                        "s.register_callback_and_fire_notify(cb, 'race-payload', 0.0))\n",
                    )
                    assert "Error" not in r1, r1
                    # Let the callback fire + notification land.
                    await anyio.sleep(0.5)
                    # Now wait — should return immediately with hint.
                    r2 = await _wait_slot(ctx, "race_cb", 0.5)
                    assert "Error" not in r2, r2
                    assert "already in the inbox" in r2
                    assert "retrieve_callback_notification" in r2
                    assert "after_notif_index" in r2


@pytest.mark.slow
async def test_wait_for_callback_after_notif_index_returns_immediately_if_newer(
    tmp_path: Path,
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> None:
    """Agent passes after_notif_index pointing at a known seq; a
    newer notification is already in the inbox → return immediately
    with a hint at the latest seq.

    Fires two notifications by invoking
    ``register_callback_and_fire_notify`` twice — the bridge step
    is idempotent so the second call just fires another notify."""
    async with WorkstationServer() as ws:
        async with connect_peer_rpc(ws.rpc_url) as peer:
            async with _callback_tester_running(peer, "t7-tester-cba") as svc_short_id:
                async with _bootstrap_session(tmp_path, ws, sandbox_env_supervisor) as ctx:
                    r1 = await _run(
                        ctx,
                        "from _fxtr_mcp_builtin import create_callback, get_service, "
                        f"schedule_invoke\nsvc = get_service({svc_short_id!r})\n"
                        "cb = create_callback('multi_cb')\n"
                        "await schedule_invoke('fire1', svc, lambda s: "
                        "s.register_callback_and_fire_notify(cb, 'first', 0.0))\n",
                    )
                    assert "Error" not in r1, r1
                    await anyio.sleep(0.5)
                    r2 = await _run(
                        ctx,
                        "from _fxtr_mcp_builtin import get_service, schedule_invoke, "
                        f"retrieve_result\nsvc = get_service({svc_short_id!r})\n"
                        # Re-fetch the same callback ref via the
                        # already-committed slot.
                        "cb = await retrieve_result('multi_cb')\n"
                        "await schedule_invoke('fire2', svc, lambda s: "
                        "s.register_callback_and_fire_notify(cb, 'second', 0.0))\n",
                    )
                    assert "Error" not in r2, r2
                    await anyio.sleep(0.5)

                    # Find the seqs in the inbox via run_script.
                    r3 = await _run(
                        ctx,
                        "from _fxtr_mcp_builtin import callback_notification_keys\n"
                        "keys = await callback_notification_keys('multi_cb')\n"
                        "print('SEQS:', keys)\n",
                    )
                    seqs_line = [line for line in r3.splitlines() if line.startswith("SEQS:")]
                    assert seqs_line, r3
                    import ast as _ast

                    seqs = _ast.literal_eval(seqs_line[0].removeprefix("SEQS:").strip())
                    assert len(seqs) >= 2, f"expected >=2 notifs, got {seqs}; r3={r3}"
                    first_seq = seqs[0]
                    latest_seq = seqs[-1]

                    # Pass after_notif_index=first_seq; newer is in inbox.
                    r4 = await _wait_slot(ctx, "multi_cb", 0.5, after_notif_index=first_seq)
                    assert "Error" not in r4, r4
                    assert "newer than" in r4
                    assert f"after_notif_index={first_seq}" in r4
                    assert f"latest seq={latest_seq}" in r4


@pytest.mark.slow
async def test_wait_for_callback_after_notif_index_not_in_inbox_errors(
    tmp_path: Path,
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> None:
    """Agent passes after_notif_index that doesn't match any
    delivered seq — error."""
    async with WorkstationServer() as ws:
        async with connect_peer_rpc(ws.rpc_url) as peer:
            async with _callback_tester_running(peer, "t7-tester-cbe") as svc_short_id:
                async with _bootstrap_session(tmp_path, ws, sandbox_env_supervisor) as ctx:
                    r1 = await _run(
                        ctx,
                        "from _fxtr_mcp_builtin import create_callback, get_service, "
                        f"schedule_invoke\nsvc = get_service({svc_short_id!r})\n"
                        "cb = create_callback('err_cb')\n"
                        "await schedule_invoke('fire_e', svc, lambda s: "
                        "s.register_callback_and_fire_notify(cb, 'p', 0.0))\n",
                    )
                    assert "Error" not in r1, r1
                    await anyio.sleep(0.3)
                    r2 = await _wait_slot(ctx, "err_cb", 0.5, after_notif_index=999999)
                    assert "Error" in r2, r2
                    assert "after_notif_index=999999" in r2
                    assert "not found" in r2


async def test_wait_for_invoke_with_after_notif_index_errors_unit(
    tmp_path: Path,
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> None:
    """``after_notif_index`` on a non-callback pending slot is a
    misuse → error.

    Unit-style: drive the inner ``_wait_for_slot_notification_inner``
    helper directly with a fake connection so we don't have to
    arrange a real long-running invoke (every test service in this
    repo returns immediately, which would race the slot into a
    terminal state). Validates only the validation branch."""
    from fxtr_workstation_mcp.server import _wait_for_slot_notification_inner

    class _FakeConn:
        active_session_id = "fake"

        def peek_slot_key(self, _key: str) -> list[Any] | None:
            return ["invoke_pending", 7]

    class _FakeAppCtx:
        connection = _FakeConn()
        config = object()  # truthy

    class _FakeBuf:
        def watch_slot(self, _key: str) -> Any:
            raise AssertionError("should not park")

        def unwatch_slot(self, *args: Any) -> None:
            pass

    r = await _wait_for_slot_notification_inner(
        app_ctx=_FakeAppCtx(),  # type: ignore[arg-type]
        buffer=_FakeBuf(),  # type: ignore[arg-type]
        drain=lambda: "",
        slot_key="ie_inv",
        timeout_seconds=0.5,
        after_notif_index=1,
    )
    assert "Error" in r, r
    assert "after_notif_index is only valid for callback_open" in r


@pytest.mark.slow
async def test_idle_event_arrives_as_debounced_channel_frame(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """Schedule a callback to fire well *after* run_script's
    trail-delay closes; verify the event arrives as a single
    debounced channel frame with ``meta={"kind": "events"}``
    containing one ``<event kind="callback" …>`` block."""
    async with WorkstationServer() as ws:
        async with connect_peer_rpc(ws.rpc_url) as peer:
            async with _callback_tester_running(peer, "t7-tester-c") as svc_short_id:
                async with _bootstrap_session(tmp_path, ws, sandbox_env_supervisor) as ctx:
                    r1 = await _run(
                        ctx,
                        "from _fxtr_mcp_builtin import create_callback, get_service, "
                        f"schedule_invoke\nsvc = get_service({svc_short_id!r})\n"
                        "cb = create_callback('idle_cb')\n"
                        "await schedule_invoke('fire-idle', svc, lambda s: "
                        "s.register_callback_and_fire_notify(cb, 'idle-payload', 0.5))\n",
                    )
                    assert "Error" not in r1, r1
                    # Sleep past the callback's delay + the buffer's
                    # 0.1s debounce. The event lands while we're idle
                    # (no tool window open) so the buffer should
                    # debounce + flush as a channel frame.
                    await anyio.sleep(1.0)

                    frames = ctx.session._write_stream.frames
                    # At least one channel frame with kind=events.
                    events_frames = [f for f in frames if f.attrs.get("kind") == "events"]
                    assert events_frames, (
                        f"expected at least one kind=events channel frame; "
                        f"saw {[(f.attrs, f.content) for f in frames]}"
                    )
                    # That frame should contain a callback event for
                    # 'idle_cb' with the expected payload.
                    callback_hits = [
                        e
                        for frame in events_frames
                        for e in parse_events(frame.content)
                        if (
                            e.attrs.get("slot_key") == "idle_cb"
                            and e.attrs.get("kind") == "callback"
                        )
                    ]
                    assert callback_hits, (
                        f"no callback event for 'idle_cb' in channel frames; "
                        f"saw frames: {[(f.attrs, f.content) for f in frames]}"
                    )
                    assert "idle-payload" in callback_hits[0].content
