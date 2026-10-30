"""End-to-end test for the sandbox-side script-error exit path
(stage5post1 issue #4).

When the user's script raises an ``Exception``, the sandbox's
``run_preamble_and_script`` catches it inside the sandbox boundary,
formats the traceback, and returns ``RunResult.error`` to the host.
The host routes through ``_format_script_error`` so the agent sees a
rollback notice after stdout/stderr/traceback fenced blocks.

``run_script`` delivers the formatted body inside a
``<script_result>...</script_result>`` tool drain envelope.

- The agent sees a clean Python traceback (filename
  ``<mcp_script>``, honest line numbers), not the host-side
  ``ExceptionGroup`` / ``IRException`` repr the agent used to see.
- The workstation session state stays untouched — partial queued
  ops (e.g. an ``export_entity`` call that ran *before* the raise)
  are not applied. Script semantics are atomic.
- The supervisor cycle does not swap — a script error is a
  tool-level failure, not a connection event.
- A follow-up ``run_script`` succeeds on the same cycle.

Slow — pays the eryx factory bootstrap cost on first
``run_script``.
"""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path
from typing import Any

import pytest
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr_workstation import WorkstationServer, expose_entity
from fxtr_workstation.client_protocol import (
    AckResponse,
    SessionSnapshotEvent,
    StateUpdateEvent,
    SubscribeSessionRequest,
)

from fxtr_workstation_mcp.app_context import AppContext
from fxtr_workstation_mcp.config import CONFIG_FILENAME
from fxtr_workstation_mcp.server import (
    close_session,
    connect,
    open_session,
    run_script,
    set_working_dir,
)
from fxtr_workstation_mcp.sandbox_env import SandboxEnvSupervisor
from fxtr_workstation_mcp.server import make_app_ctx

from ._peer import connect_peer_client, connect_peer_rpc

pytestmark = pytest.mark.anyio


_TEST_NS = uuid.UUID("77777777-aaaa-bbbb-cccc-dddddddddddd")
_TEST_NOTE_TYPE_ID = system_entities.define_type(_TEST_NS, "ws_mcp_test_note_5p1_4")


@dataclasses.dataclass(frozen=True)
class _TestNote(DataclassEntity, fxtr_type=_TEST_NOTE_TYPE_ID):
    title: str
    body: str


# --- Fake MCP-context helpers (same shape as other integration tests) ---


class _FakeRequestContext:
    def __init__(self, app_ctx: AppContext) -> None:
        self.lifespan_context = app_ctx


class _FakeContext:
    def __init__(self, app_ctx: AppContext) -> None:
        self.request_context = _FakeRequestContext(app_ctx)
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


async def _read_session_state(client_url: str, session_id: str) -> StateUpdateEvent:
    async with connect_peer_client(client_url) as peer:
        sub_id = 1
        await peer.send(SubscribeSessionRequest(request_id=sub_id, session_id=session_id))
        ack = await peer.receive()
        assert isinstance(ack, AckResponse), ack
        snapshot = await peer.receive()
        assert isinstance(snapshot, SessionSnapshotEvent), snapshot
        return snapshot.last_state


# --- The test --------------------------------------------------------


@pytest.mark.slow
async def test_script_error_does_not_apply_mutation(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """A script that queues an export then raises should:

    1. Return a clean ``Error: script raised`` tool result with a
       fenced traceback and any captured stdout.
    2. Leave the workstation session state empty — the partial
       export is dropped at the sandbox boundary.
    3. Not trigger a supervisor reconnect (state stays READY).
    4. Allow a follow-up ``run_script`` to succeed on the same
       cycle.
    """
    async with WorkstationServer() as ws:
        async with connect_peer_rpc(ws.rpc_url) as peer_session:
            note_id: FxtrID = (
                _TestNote(title="hi", body="bye").store(scope=peer_session.scope).entity_id
            )
            async with expose_entity(
                peer_session.remote_service,
                external_name="bg",
                entity=note_id,
                scope=peer_session.scope,
            ) as bg_short_id:
                (tmp_path / CONFIG_FILENAME).write_text(
                    f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
                )
                (tmp_path / "preamble.py").write_text("# stage5post1 script-error\n")

                async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
                    ctx = _FakeContext(app_ctx)
                    assert "Working dir set" in (await _swd(ctx, str(tmp_path)))
                    assert (await _conn(ctx)).startswith("connected to")
                    open_msg = await _open(ctx, {"phase": "script-error"})
                    session_id = open_msg.removeprefix("opened session ").strip()

                    connection = app_ctx.connection
                    assert connection is not None
                    assert connection.is_ready

                    # Script that:
                    # - prints to stdout (captures pre-raise output)
                    # - queues an export_entity op
                    # - then raises a ValueError
                    # The raise must drop the export at the sandbox
                    # boundary; the host must not _apply_script_mutation.
                    script_with_error = (
                        "from _fxtr_mcp_builtin import export_entity, lookup_entity\n"
                        'print("about to export")\n'
                        f"e = await lookup_entity({bg_short_id!r})\n"
                        'await export_entity("partial", e)\n'
                        'print("about to raise")\n'
                        'raise ValueError("intentional error")\n'
                    )
                    result = await _run(ctx, script_with_error)

                    # Assertion 1: tool result shape (rollback last;
                    # streams + traceback inside ``<script_result>``).
                    assert "<script_result>" in result and "</script_result>" in result, result
                    assert "Error: script raised." in result, result
                    assert "*rolled back*" in result, result
                    assert "--- stdout ---" in result, result
                    assert "about to export" in result, result
                    assert "about to raise" in result, result
                    assert "--- end stdout ---" in result, result
                    assert "--- traceback ---" in result, result
                    assert "<mcp_script>" in result, result  # filename in trace
                    assert 'raise ValueError("intentional error")' in result, (
                        "expected the offending source line to be "
                        f"rendered via linecache; got: {result}"
                    )
                    assert "ValueError: intentional error" in result, result
                    assert "--- end traceback ---" in result, result
                    assert result.index("*rolled back*") > result.index("--- end traceback ---"), (
                        "expected rollback notice after traceback; "
                        f"got roll={result.index('*rolled back*')} "
                        f"trace_end={result.index('--- end traceback ---')} "
                        f"body={result!r}"
                    )

                    # Assertion 2: workstation session state untouched.
                    # Pre-any-mutation, the workstation hasn't even
                    # initialized ``data_by_key`` in the session
                    # state dict — it's just ``{}``. We accept
                    # either missing or empty.
                    state = await _read_session_state(ws.client_url, session_id)
                    # open_session ran the agent_mcp bootstrap, so
                    # ``workstation_inbox`` is in data_by_key. Filter
                    # it out — the test cares about whether the
                    # script's export_entity op was applied.
                    data_by_key = dict(state.state.get("data_by_key") or {})
                    data_by_key.pop("workstation_inbox", None)
                    assert data_by_key == {}, (
                        "the workstation must not have applied the "
                        "export_entity op that preceded the raise; "
                        f"got data_by_key={data_by_key!r}"
                    )
                    assert state.tracked_entities == [], (
                        "no entities should be tracked since no "
                        "mutation was committed; "
                        f"got tracked_entities={state.tracked_entities!r}"
                    )

                    # Assertion 3: supervisor cycle did NOT swap.
                    # Script errors are tool-level failures, not
                    # connection events — state stays READY.
                    assert connection.is_ready, (
                        "the supervisor should still be in READY; a "
                        "script-raised Exception is not a connection-"
                        "level event"
                    )
                    assert connection.active_session_id == session_id

                    # Assertion 4: follow-up call succeeds.
                    r2 = await _run(ctx, "# trivial post-error noop\n")
                    assert "Script ran" in r2, r2

                    assert (await _close(ctx)).startswith("closed session ")
