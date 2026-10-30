"""End-to-end stage-3a integration test for the export pipeline.

Boots a real :class:`WorkstationServer` plus a peer ``/rpc``
connection that pre-registers an entity. The MCP runs a sandboxed
script that calls ``await export_entity("foo", await
lookup_entity(...))``. We verify via a separate ``/client``
subscriber that the session state has been mutated as expected,
that the workstation pin keeps the entity alive after the peer
drops its registration, and that a follow-up ``forget`` removes
both the data_by_key key and the tracker.

Slow — pays the eryx factory bootstrap cost on first ``run_script``.
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


_TEST_NS = uuid.UUID("88888888-aaaa-bbbb-cccc-dddddddddddd")
_TEST_NOTE_TYPE_ID = system_entities.define_type(_TEST_NS, "ws_mcp_test_note_3a")


@dataclasses.dataclass(frozen=True)
class _TestNote(DataclassEntity, fxtr_type=_TEST_NOTE_TYPE_ID):
    title: str
    body: str


# --- Fake MCP-context helpers (mirror stage 1/2 tests) ---------------


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


# --- /client subscriber helper ---------------------------------------


async def _read_session_state(client_url: str, session_id: str) -> StateUpdateEvent:
    """Open a fresh ``/client`` connection, subscribe to the session,
    and return the latest :class:`StateUpdateEvent`. Useful for
    asserting on session state from outside the MCP."""
    async with connect_peer_client(client_url) as peer:
        sub_id = 1
        await peer.send(SubscribeSessionRequest(request_id=sub_id, session_id=session_id))
        ack = await peer.receive()
        assert isinstance(ack, AckResponse), ack
        snapshot = await peer.receive()
        assert isinstance(snapshot, SessionSnapshotEvent), snapshot
        # 3-post-2: snapshot.last_state is authoritative — state_updates
        # are no longer carried inside the event buckets, so we don't
        # need to walk the lists here. The buckets carry only
        # NonStateSessionEvents now, which the MCP doesn't issue, so
        # they should be empty for this peer's view.
        return snapshot.last_state


async def _peer_entity_alive(rpc_url: str, short_id: str) -> bool:
    """Whether ``short_id`` still resolves on the workstation."""
    async with connect_peer_rpc(rpc_url) as peer_session:
        ws = await peer_session.remote_service.resolve(peer_session.scope)
        try:
            await ws.get_entity(short_id)
            return True
        except Exception:
            return False


# --- The test --------------------------------------------------------


@pytest.mark.slow
async def test_export_pipeline_e2e(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """Agent script exports a peer-registered entity; session state
    reflects the export; entity stays alive across peer drop; a
    follow-up ``forget`` releases it."""
    async with WorkstationServer() as ws:
        # Pre-register a background entity through a peer /rpc.
        async with connect_peer_rpc(ws.rpc_url) as peer_session:
            note_id: FxtrID = (
                _TestNote(title="hello", body="world").store(scope=peer_session.scope).entity_id
            )

            # Set up the MCP working dir.
            (tmp_path / CONFIG_FILENAME).write_text(
                f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
            )
            (tmp_path / "preamble.py").write_text("# stage-3a e2e\n")

            workstation_short_id: str
            session_id: str

            # Phase 1: with the peer's registration alive, export
            # the entity via the MCP and verify the session state.
            async with expose_entity(
                peer_session.remote_service,
                external_name="bg-entity",
                entity=note_id,
                scope=peer_session.scope,
            ) as bg_short_id:
                workstation_short_id = bg_short_id

                async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
                    ctx = _FakeContext(app_ctx)
                    assert "Working dir set" in (await _swd(ctx, str(tmp_path)))
                    assert (await _conn(ctx)).startswith("connected to")
                    open_msg = await _open(ctx, {"phase": "stage-3a"})
                    assert open_msg.startswith("opened session ")
                    session_id = open_msg.removeprefix("opened session ").strip()

                    script_export = (
                        "from _fxtr_mcp_builtin import (\n"
                        "    export_entity, lookup_entity\n"
                        ")\n"
                        f"e = await lookup_entity({bg_short_id!r})\n"
                        'await export_entity("foo", e)\n'
                    )
                    r = await _run(ctx, script_export)
                    assert "Script ran. Committed:" in r, r

                    state_after_export = await _read_session_state(ws.client_url, session_id)
                    # open_session ran the agent_mcp bootstrap, which
                    # added the reserved ``workstation_inbox`` callback
                    # slot to data_by_key. Filter it out — the test
                    # cares about the export_entity outcome, not
                    # MCP-internal slots.
                    data_by_key = dict(state_after_export.state.get("data_by_key") or {})
                    data_by_key.pop("workstation_inbox", None)
                    assert data_by_key == {"foo": ["exported_entity", workstation_short_id]}
                    assert state_after_export.tracked_entities == [workstation_short_id]
                    assert state_after_export.tracked_services == []

                # MCP lifespan exits here, but the workstation
                # session persists (active_session is in the toml).

            # Peer's expose_entity exits → registration released. The
            # session pin should keep the entity alive.
            assert await _peer_entity_alive(ws.rpc_url, workstation_short_id), (
                f"entity {workstation_short_id} should still be resolvable " "via the session's pin"
            )

            # Phase 2: re-attach the MCP to the same session and
            # forget("foo"). The MCP reads the toml's active_session
            # and resubscribes; mirror loads from the snapshot.
            async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx2:
                ctx2 = _FakeContext(app_ctx2)
                assert "Working dir set" in (await _swd(ctx2, str(tmp_path)))
                assert (await _conn(ctx2)).startswith("connected to")

                script_forget = "from _fxtr_mcp_builtin import forget\n" 'forget("foo")\n'
                r2 = await _run(ctx2, script_forget)
                assert "Script ran. Committed:" in r2, r2

                state_after_forget = await _read_session_state(ws.client_url, session_id)
                # Phase 2 starts with active_session set in the toml,
                # so the bootstrap added the reserved ``workstation_inbox``
                # callback to ``data_by_key``. Filter it out for this
                # assertion — the test cares about the forget("foo")
                # outcome, not about MCP-internal slots.
                data_by_key = dict(state_after_forget.state.get("data_by_key") or {})
                data_by_key.pop("workstation_inbox", None)
                assert data_by_key == {}
                assert state_after_forget.tracked_entities == []

                assert (await _close(ctx2)).startswith("closed session ")
