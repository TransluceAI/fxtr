"""Stage 10 / chunk D — supervisor bootstrap integration tests.

Real :class:`WorkstationServer` + real MCP supervisor cycle. Verifies
the per-session ``agent_mcp:{session_id}`` lifecycle subtask:

- A session pre-created on the workstation (via /client) and threaded
  into the MCP via ``active_session`` in the toml triggers the
  bootstrap on the first supervisor cycle. After ``connect``
  returns:
    - ``workstation_inbox`` exists in ``data_by_key`` as
      ``["callback_open", N]``.
    - ``agent_mcp:{session_id}`` is registered on the workstation
      and reachable via ``lookup_short_id_by_external_name`` from a
      separate /rpc peer.
    - ``connection._agent_mcp_impl`` is non-None.
- With ``agent_mcp_store = false`` in the toml, no persistence ref
  is held and ``initial_head`` on the impl is ``None``.
- With ``agent_mcp_store = "<configured cap name>"`` and the named
  persistence cap mounted on the workstation, the bootstrap looks
  up the cap and holds the ref. Initial chain head is still ``None``
  because no log entries have been written.
- On lifespan exit (``shutdown``), the supervisor cycle ends:
  ``agent_mcp:{session_id}`` is no longer registered on the
  workstation.
- A reserved slot_key (``workstation_inbox``) used by an agent
  script is rejected as a tool error before the mutation is sent.

The tests build on the test_supervisor_state_machine.py harness:
session-scoped :func:`sandbox_env_supervisor` fixture +
:func:`make_app_ctx` + :class:`_FakeContext`.
"""

from __future__ import annotations

import pathlib
from pathlib import Path

import anyio
import pytest
from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr_workstation import (
    PersistenceNamespaceConfig,
    WorkstationServer,
)
from fxtr_workstation.client_protocol import (
    CreateSessionRequest,
    SessionCreatedResponse,
)
from fxtr_workstation.protocols import WorkstationServiceProto

from fxtr_workstation_mcp.app_context import AppContext
from fxtr_workstation_mcp.config import CONFIG_FILENAME
from fxtr_workstation_mcp.sandbox_env import SandboxEnvSupervisor
from fxtr_workstation_mcp.server import (
    connect,
    make_app_ctx,
    run_script,
    set_working_dir,
)
from fxtr_workstation_mcp.ws_connection import SupervisorState

from ._peer import connect_peer_client, connect_peer_rpc

pytestmark = pytest.mark.anyio


# --- Test helpers (mirror test_supervisor_state_machine.py) ----------


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


async def _run(ctx: _FakeContext, script: str) -> str:
    return await run_script(script, ctx)  # type: ignore[arg-type]


async def _precreate_session(client_url: str) -> str:
    """Create a session via the workstation's ``/client`` endpoint
    and return its UUID string."""
    async with connect_peer_client(client_url) as client:
        await client.send(CreateSessionRequest(request_id=1, metadata={}))
        frame = await client.receive()
        assert isinstance(frame, SessionCreatedResponse)
        return frame.session_id


async def _lookup_external_name(rpc_url: str, external_name: str) -> str:
    """Resolve ``external_name`` on the workstation from a separate
    /rpc peer. Returns the short_id."""
    async with connect_peer_rpc(rpc_url) as peer:
        ws_ref = peer.remote_service.replace_service_type(WorkstationServiceProto)
        proxy = await ws_ref.resolve(peer.scope)
        return await proxy.lookup_short_id_by_external_name(external_name)


# --- Tests -----------------------------------------------------------


async def test_bootstrap_creates_workstation_inbox_and_registers_service(
    tmp_path: Path,
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> None:
    """Initial-session path: pre-create a session, point the toml at
    it, ``connect`` runs the bootstrap → ``workstation_inbox`` is
    created and ``agent_mcp:{session_id}`` is registered."""
    async with WorkstationServer() as ws:
        session_id = await _precreate_session(ws.client_url)
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n'
            f'active_session = "{session_id}"\n'
            "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# chunk D bootstrap test\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            assert "Working dir set" in (await _swd(ctx, str(tmp_path)))
            r = await _conn(ctx)
            assert r.startswith("connected to"), r

            connection = app_ctx.connection
            assert connection is not None
            assert connection.state == SupervisorState.READY

            # workstation_inbox is in data_by_key as callback_open.
            inbox_value = connection.peek_slot_key("workstation_inbox")
            assert inbox_value is not None
            assert isinstance(inbox_value, list)
            assert len(inbox_value) == 2
            assert inbox_value[0] == "callback_open"
            assert isinstance(inbox_value[1], int)

            # Impl is registered.
            assert connection._agent_mcp_impl is not None  # type: ignore[private-usage]
            # No persistence (agent_mcp_store = false).
            assert connection._agent_mcp_persistence_ref is None  # type: ignore[private-usage]

            # agent_mcp:{session_id} is visible to other /rpc peers.
            agent_mcp_short_id = await _lookup_external_name(ws.rpc_url, f"agent_mcp:{session_id}")
            assert agent_mcp_short_id.startswith("s")


async def test_bootstrap_with_persistence_cap_holds_ref(
    tmp_path: pathlib.Path,
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> None:
    """With ``agent_mcp_store = "<name>"`` and the named persistence
    cap mounted on the workstation, the bootstrap looks up the cap.
    The impl is constructed with ``initial_head=None`` (no log
    entries exist yet)."""
    async with SQLiteFxtrNamespace(str(tmp_path / "store.sqlite"), "ns") as ns:
        cap_cfg = PersistenceNamespaceConfig(
            external_name="agent_mcp_store",
            namespace=ns,
        )
        async with WorkstationServer(persistence_namespaces=[cap_cfg]) as ws:
            session_id = await _precreate_session(ws.client_url)
            (tmp_path / CONFIG_FILENAME).write_text(
                f'workstation_url = "{ws.bound_url}"\n'
                f'active_session = "{session_id}"\n'
                'agent_mcp_store = "agent_mcp_store"\n'
            )
            (tmp_path / "preamble.py").write_text("# chunk D persistence test\n")

            async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
                ctx = _FakeContext(app_ctx)
                assert "Working dir set" in (await _swd(ctx, str(tmp_path)))
                assert (await _conn(ctx)).startswith("connected to")

                connection = app_ctx.connection
                assert connection is not None
                assert connection.state == SupervisorState.READY

                # Persistence ref held.
                assert (
                    connection._agent_mcp_persistence_ref is not None  # type: ignore[private-usage]
                )
                # Initial head is None — no log entries have been
                # written yet.
                impl = connection._agent_mcp_impl  # type: ignore[private-usage]
                assert impl is not None
                assert impl.chain_head is None


async def test_bootstrap_fails_when_persistence_cap_missing(
    tmp_path: Path,
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> None:
    """``agent_mcp_store`` configured to a name that isn't registered
    on the workstation: the bootstrap fails, the supervisor pauses
    with a meaningful reason, and ``connect`` returns an error tool
    string."""
    async with WorkstationServer() as ws:
        session_id = await _precreate_session(ws.client_url)
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n'
            f'active_session = "{session_id}"\n'
            'agent_mcp_store = "no_such_cap_registered"\n'
        )
        (tmp_path / "preamble.py").write_text("# chunk D missing-cap test\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            assert "Working dir set" in (await _swd(ctx, str(tmp_path)))
            r = await _conn(ctx)
            assert "Error" in r
            assert "no_such_cap_registered" in r


async def test_reserved_slot_key_rejected_in_script(
    tmp_path: Path,
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> None:
    """Scripts that try to write to ``workstation_inbox`` get a tool
    error before the mutation is built. The agent retries with a
    different slot_key."""
    async with WorkstationServer() as ws:
        session_id = await _precreate_session(ws.client_url)
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n'
            f'active_session = "{session_id}"\n'
            "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# chunk D reserved-key test\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            assert "Working dir set" in (await _swd(ctx, str(tmp_path)))
            assert (await _conn(ctx)).startswith("connected to")

            # Try to create_callback on the reserved slot — should
            # fail with the reserved-slot tool error.
            script = (
                "from _fxtr_mcp_builtin import create_callback\n"
                'create_callback("workstation_inbox")\n'
            )
            out = await _run(ctx, script)
            assert "Error" in out
            assert "workstation_inbox" in out
            assert "reserved" in out


async def test_open_session_runs_bootstrap_inline(
    tmp_path: Path,
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> None:
    """The agent's standard workflow uses ``open_session`` (not the
    toml-driven ``active_session`` path). Verify that
    ``open_session`` triggers the bootstrap inline: after the tool
    returns, ``workstation_inbox`` is in data_by_key and
    ``agent_mcp:{session_id}`` is registered on the workstation.
    """
    async with WorkstationServer() as ws:
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# chunk D open_session bootstrap test\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            assert "Working dir set" in (await _swd(ctx, str(tmp_path)))
            assert (await _conn(ctx)).startswith("connected to")

            from fxtr_workstation_mcp.server import open_session

            open_result = await open_session(ctx, {})  # type: ignore[arg-type]
            assert open_result.startswith("opened session "), open_result
            session_id = open_result.removeprefix("opened session ").strip()

            connection = app_ctx.connection
            assert connection is not None

            # workstation_inbox is in data_by_key.
            inbox_value = connection.peek_slot_key("workstation_inbox")
            assert inbox_value is not None
            assert inbox_value[0] == "callback_open"

            # Impl is registered.
            assert connection._agent_mcp_impl is not None  # type: ignore[private-usage]

            # agent_mcp:{session_id} is reachable from a peer.
            agent_mcp_short_id = await _lookup_external_name(ws.rpc_url, f"agent_mcp:{session_id}")
            assert agent_mcp_short_id.startswith("s")


async def test_close_session_tears_down_agent_mcp_subtask(
    tmp_path: Path,
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> None:
    """``close_session`` cancels the agent_mcp subtask before sending
    ``CloseOp``, so the registration drops on the workstation
    around the time the session itself goes away."""
    async with WorkstationServer() as ws:
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# chunk D close_session teardown test\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            assert "Working dir set" in (await _swd(ctx, str(tmp_path)))
            assert (await _conn(ctx)).startswith("connected to")

            from fxtr_workstation_mcp.server import close_session, open_session

            r = await open_session(ctx, {})  # type: ignore[arg-type]
            session_id = r.removeprefix("opened session ").strip()
            connection = app_ctx.connection
            assert connection is not None
            assert connection._agent_mcp_impl is not None  # type: ignore[private-usage]

            # Close → bootstrap subtask should be cancelled, impl cleared.
            assert (await close_session(ctx)).startswith("closed session ")  # type: ignore[arg-type]
            assert connection._agent_mcp_impl is None  # type: ignore[private-usage]

            # Registration should be gone from the workstation.
            with pytest.raises(Exception):  # noqa: BLE001
                await _lookup_external_name(ws.rpc_url, f"agent_mcp:{session_id}")


async def test_agent_mcp_registration_dropped_on_shutdown(
    tmp_path: Path,
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> None:
    """When the lifespan exits, the supervisor cycle ends, and the
    ``agent_mcp:{session_id}`` registration is dropped on the
    workstation — a subsequent ``lookup_short_id_by_external_name``
    from a fresh peer raises."""
    async with WorkstationServer() as ws:
        session_id = await _precreate_session(ws.client_url)
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n'
            f'active_session = "{session_id}"\n'
            "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# chunk D shutdown test\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            assert "Working dir set" in (await _swd(ctx, str(tmp_path)))
            assert (await _conn(ctx)).startswith("connected to")
            # Confirm it's registered while the lifespan is alive.
            await _lookup_external_name(ws.rpc_url, f"agent_mcp:{session_id}")

        # Lifespan exited — the supervisor torn down its cycle, which
        # released the agent_mcp registration. A new peer's lookup
        # should fail.
        with pytest.raises(Exception):  # noqa: BLE001 — workstation surfaces RPC error
            with anyio.fail_after(5.0):
                await _lookup_external_name(ws.rpc_url, f"agent_mcp:{session_id}")
