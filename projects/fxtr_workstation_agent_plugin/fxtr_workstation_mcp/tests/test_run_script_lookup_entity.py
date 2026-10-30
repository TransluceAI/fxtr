"""End-to-end stage-2 integration test.

Boots a real :class:`WorkstationServer` with a separate ``/rpc``
peer that registers an entity. The MCP runs a sandboxed script that
calls ``lookup_entity(short_id)`` over the host bridge and asserts
the result is a real :class:`FxtrID` matching the peer's
registration.

This covers:

- ``set_working_dir`` validating ``preamble.py``.
- ``run_script`` lifespan: sandbox env supervisor lazy-bootstrap +
  worker spawn + script exec + clean shutdown.
- ``McpHostImpl.lookup_entity`` over fxtr-RPC: sandbox call →
  host method → workstation ``get_entity`` → result FxtrID
  bridged back across both connections into the sandbox.

Heavy: factory bootstrap is paid once (on first ``run_script``).
Marked slow.
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

from ._peer import connect_peer_rpc

pytestmark = pytest.mark.anyio


# A tiny entity defined here just so the peer has something to
# register. The sandbox doesn't need this Python class — it just
# receives the FxtrID and asserts the type.
_TEST_NS = uuid.UUID("88888888-aaaa-bbbb-cccc-dddddddddddd")
_TEST_NOTE_TYPE_ID = system_entities.define_type(_TEST_NS, "ws_mcp_test_note")


@dataclasses.dataclass(frozen=True)
class _TestNote(DataclassEntity, fxtr_type=_TEST_NOTE_TYPE_ID):
    title: str
    body: str


# --- Fake MCP-context helpers (mirror stage 1 tests) -----------------


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


# --- The test --------------------------------------------------------


@pytest.mark.slow
async def test_lookup_entity_e2e(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """Agent script looks up a peer-registered entity by short_id and
    receives the matching FxtrID."""
    async with WorkstationServer() as ws:
        # Background peer: open a /rpc connection, store + expose a
        # _TestNote, hold the registration alive throughout the
        # sandboxed script.
        async with connect_peer_rpc(ws.rpc_url) as peer_session:
            note_id: FxtrID = (
                _TestNote(title="hello", body="world").store(scope=peer_session.scope).entity_id
            )
            async with expose_entity(
                peer_session.remote_service,
                external_name="test-bg-note",
                entity=note_id,
                scope=peer_session.scope,
            ) as short_id:
                # Set up the MCP working dir.
                (tmp_path / CONFIG_FILENAME).write_text(
                    f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
                )
                (tmp_path / "preamble.py").write_text("# stage-2 e2e\n")

                async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
                    ctx = _FakeContext(app_ctx)
                    assert "Working dir set" in (await _swd(ctx, str(tmp_path)))
                    assert (await _conn(ctx)).startswith("connected to")
                    r = await _open(ctx, {"phase": "stage-2"})
                    assert r.startswith("opened session ")

                    # Templated script that asserts the lookup
                    # returns the expected FxtrID. Failure raises
                    # AssertionError inside the sandbox; run_script
                    # surfaces it as an "Error during sandbox
                    # execution" string.
                    expected_full_id = str(note_id)
                    script = (
                        "from _fxtr_mcp_builtin import lookup_entity\n"
                        "from fxtr2.data.entity_core import FxtrID\n"
                        f"fxtr_id = await lookup_entity({short_id!r})\n"
                        "assert isinstance(fxtr_id, FxtrID), "
                        "f'expected FxtrID, got {type(fxtr_id).__name__}'\n"
                        f"assert str(fxtr_id) == {expected_full_id!r}, "
                        "f'id mismatch: {fxtr_id}'\n"
                    )
                    r = await _run(ctx, script)
                    assert "Script ran" in r, r

                    assert (await _close(ctx)).startswith("closed session ")
