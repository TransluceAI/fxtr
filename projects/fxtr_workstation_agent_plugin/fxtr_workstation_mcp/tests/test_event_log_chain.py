"""Stage 10 / chunk E — log writer + chain extension integration tests.

Real :class:`WorkstationServer` + real MCP supervisor cycle, focusing
on the per-cycle log-intent stream and ``_log_writer_loop`` wired
inside ``_agent_mcp_session_lifecycle``:

- After a script runs, ``connection._agent_mcp_impl.chain_head``
  advances to a :class:`WsAgentLogEventChain` whose ``event`` is a
  :class:`ScriptExecutionLogEvent` with the script's source and
  ``committed_seq`` populated.
- After a ``schedule_invoke`` resolves, the chain grows by one more
  node whose ``event`` is an :class:`InvokeResultLogEvent` carrying
  the resolved payload.
- ``present_to_user(payload)`` appears as a
  :class:`PresentToUserOpRecord` entry on the committed script's
  ``ScriptExecutionLogEvent.ops`` list, with placeholder
  substitution applied to entity refs in the payload.
- ``close_session`` drains the writer before tearing down the
  agent_mcp subtask; no log intents from pre-close work get lost,
  no errors raised.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Protocol

import anyio
import pytest
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method
from fxtr_workstation import (
    PersistenceNamespaceConfig,
    WorkstationServer,
    expose_service,
)
from fxtr_workstation.client_protocol import (
    CreateSessionRequest,
    SessionCreatedResponse,
)

from fxtr_workstation_mcp.agent_log_types import (
    OP_KIND_PRESENT_TO_USER,
    OP_KIND_SCHEDULE_INVOKE,
    CallbackNotificationLogEvent,
    InvokeResultLogEvent,
    ScriptExecutionLogEvent,
    WsAgentLogEventChain,
)
from fxtr_workstation_mcp.app_context import AppContext
from fxtr_workstation_mcp.config import CONFIG_FILENAME
from fxtr_workstation_mcp.sandbox_env import SandboxEnvSupervisor
from fxtr_workstation_mcp.server import (
    close_session,
    connect,
    make_app_ctx,
    open_session,
    run_script,
    set_working_dir,
)
from fxtr_workstation_mcp.ws_connection import WorkstationConnection

from ._peer import connect_peer_client, connect_peer_rpc

pytestmark = pytest.mark.anyio


# --- Echo service for schedule_invoke tests --------------------------


_ECHO_NS = uuid.UUID("aabbccdd-1111-2222-3333-444466667777")
_ECHO_TYPE_ID = system_entities.define_type(_ECHO_NS, "stage10_chunk_e_echo")


@service_protocol
class _EchoProto(Protocol):
    async def echo(self, msg: str) -> str: ...


class _EchoImpl(TypedServiceImpl[_EchoProto]):
    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return _EchoProto

    def _service_type_id(self) -> FxtrID:
        return _ECHO_TYPE_ID

    @service_method
    async def echo(self, msg: str) -> str:
        return f"echo:{msg}"


# --- FastMCP context shim (same shape as test_invoke_e2e) ------------


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


async def _run(ctx: _FakeContext, src: str) -> str:
    return await run_script(src, ctx)  # type: ignore[arg-type]


async def _close(ctx: _FakeContext) -> str:
    return await close_session(ctx)  # type: ignore[arg-type]


# --- Chain-walking helpers -------------------------------------------


async def _wait_for_chain_head_change(
    connection: WorkstationConnection,
    *,
    previous_head_id: FxtrID | None,
    poll_seconds: float = 0.05,
    max_polls: int = 100,
) -> None:
    """Poll ``connection._agent_mcp_impl.chain_head`` until it points at
    a *different* head than ``previous_head_id`` (or appears for the
    first time, when ``previous_head_id is None``)."""
    for _ in range(max_polls):
        impl = connection._agent_mcp_impl  # type: ignore[private-usage]
        if impl is not None:
            current = impl.chain_head
            current_id = current.entity_id if current is not None else None
            if current_id != previous_head_id:
                return
        await anyio.sleep(poll_seconds)
    raise AssertionError(
        f"timed out waiting for chain head to advance past "
        f"{previous_head_id!r}; current head: "
        f"{connection._agent_mcp_impl.chain_head if connection._agent_mcp_impl else None}"  # type: ignore[private-usage]
    )


async def _wait_for_chain_length(
    connection: WorkstationConnection,
    *,
    at_least: int,
    poll_seconds: float = 0.05,
    max_polls: int = 100,
) -> list[WsAgentLogEventChain]:
    """Poll until the chain has at least ``at_least`` entries. Returns
    the walked chain."""
    for _ in range(max_polls):
        impl = connection._agent_mcp_impl  # type: ignore[private-usage]
        if impl is not None and impl.chain_head is not None:
            chain = await _walk_chain(connection)
            if len(chain) >= at_least:
                return chain
        await anyio.sleep(poll_seconds)
    impl = connection._agent_mcp_impl  # type: ignore[private-usage]
    actual = (
        len(await _walk_chain(connection))
        if impl is not None and impl.chain_head is not None
        else 0
    )
    raise AssertionError(
        f"timed out waiting for chain to reach length {at_least}; " f"current length: {actual}"
    )


async def _walk_chain(
    connection: WorkstationConnection,
) -> list[WsAgentLogEventChain]:
    """Walk back from the current chain head via ``prev`` links until
    ``None``. Returns the list ordered oldest → newest.

    Loads each chain node via the impl_scope; assumes the impl is
    still running."""
    impl = connection._agent_mcp_impl  # type: ignore[private-usage]
    assert impl is not None, "agent_mcp impl is not running"
    scope: FxtrScope = impl._scope  # type: ignore[private-usage]

    out: list[WsAgentLogEventChain] = []
    head_ref = impl.chain_head
    with scope.as_active_scope():
        while head_ref is not None:
            node = await head_ref.load()
            out.append(node)
            head_ref = node.prev
    out.reverse()
    return out


# --- Tests ------------------------------------------------------------


async def test_chain_grows_on_script_execution(
    tmp_path: Path,
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> None:
    """A trivial no-op script extends the chain by one node whose
    event is a :class:`ScriptExecutionLogEvent`."""
    async with WorkstationServer() as ws:
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# chunk E script-execution test\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            await _swd(ctx, str(tmp_path))
            await _conn(ctx)
            await _open(ctx, {"chunk": "E"})

            connection = app_ctx.connection
            assert connection is not None

            # Capture the head id right after bootstrap. The bootstrap's
            # workstation_inbox commit does not itself produce a log
            # entry (it's pushed by ``_apply_script_mutation``, which
            # the bootstrap path doesn't go through), so chain_head
            # starts as None.
            impl = connection._agent_mcp_impl  # type: ignore[private-usage]
            assert impl is not None
            assert impl.chain_head is None

            script_src = "# no-op\n"
            r = await _run(ctx, script_src)
            assert "Error" not in r, r

            await _wait_for_chain_head_change(connection, previous_head_id=None)

            chain = await _walk_chain(connection)
            assert len(chain) == 1
            node = chain[0]
            assert node.prev is None

            impl_scope: FxtrScope = impl._scope  # type: ignore[private-usage]
            with impl_scope.as_active_scope():
                event = await node.event.load()
            assert isinstance(event, ScriptExecutionLogEvent)
            assert event.script_src == script_src
            assert event.error is None
            assert event.rejected is None
            # No queued ops → empty ops tuple. ``committed_seq`` is
            # None because the build returned None (no-op).
            assert event.ops == ()
            assert event.committed_seq is None


async def test_chain_grows_on_invoke_result(
    tmp_path: Path,
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> None:
    """A ``schedule_invoke`` produces *two* chain nodes — one for the
    script execution, one for the :class:`InvokeResultLogEvent` when
    the workstation broadcasts the result."""
    async with WorkstationServer() as ws:
        async with connect_peer_rpc(ws.rpc_url) as peer:
            echo_ref = await peer.scope.mount(_EchoImpl())
            async with expose_service(
                peer.remote_service,
                "test-chunk-e-echo",
                echo_ref,
                peer.scope,
            ) as svc_short_id:
                (tmp_path / CONFIG_FILENAME).write_text(
                    f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
                )
                (tmp_path / "preamble.py").write_text("# chunk E invoke-result test\n")

                async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
                    ctx = _FakeContext(app_ctx)
                    await _swd(ctx, str(tmp_path))
                    await _conn(ctx)
                    await _open(ctx, {"chunk": "E"})

                    connection = app_ctx.connection
                    assert connection is not None

                    schedule_script = (
                        "from _fxtr_mcp_builtin import "
                        "schedule_invoke, get_service\n"
                        f"svc = get_service({svc_short_id!r})\n"
                        "await schedule_invoke("
                        '"r", svc, lambda s: s.echo("hi")'
                        ")\n"
                    )
                    r = await _run(ctx, schedule_script)
                    assert "Error" not in r, r

                    # The script execution + invoke result both
                    # produce chain extensions. Wait until both have
                    # landed.
                    chain = await _wait_for_chain_length(connection, at_least=2)
                    assert len(chain) == 2

                    impl = connection._agent_mcp_impl  # type: ignore[private-usage]
                    assert impl is not None
                    impl_scope: FxtrScope = impl._scope  # type: ignore[private-usage]

                    with impl_scope.as_active_scope():
                        first_event = await chain[0].event.load()
                        second_event = await chain[1].event.load()

                    assert isinstance(first_event, ScriptExecutionLogEvent)
                    assert first_event.error is None
                    assert first_event.rejected is None
                    # The script committed → ``ops`` has the
                    # ``schedule_invoke`` op recorded.
                    assert any(
                        op.kind == OP_KIND_SCHEDULE_INVOKE for op in first_event.ops
                    ), first_event.ops

                    assert isinstance(second_event, InvokeResultLogEvent)
                    assert second_event.slot_key == "r"
                    assert second_event.payload == "echo:hi"


async def test_present_to_user_logs_op_record(
    tmp_path: Path,
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> None:
    """``present_to_user`` is MCP-side only: the chain head's
    ``ScriptExecutionLogEvent.ops`` carries the
    :class:`PresentToUserOpRecord` with the walked payload."""
    async with WorkstationServer() as ws:
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# chunk E present_to_user test\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            await _swd(ctx, str(tmp_path))
            await _conn(ctx)
            await _open(ctx, {"chunk": "E"})

            connection = app_ctx.connection
            assert connection is not None

            # Use a non-trivial payload — a dict with a string + an
            # int. (No entity refs here; that's covered by the
            # log-builder unit tests.)
            script_src = (
                "from _fxtr_mcp_builtin import present_to_user\n"
                'await present_to_user({"greeting": "hello", "n": 42})\n'
            )
            r = await _run(ctx, script_src)
            assert "Error" not in r, r

            await _wait_for_chain_head_change(connection, previous_head_id=None)

            chain = await _walk_chain(connection)
            assert len(chain) == 1

            impl = connection._agent_mcp_impl  # type: ignore[private-usage]
            assert impl is not None
            impl_scope: FxtrScope = impl._scope  # type: ignore[private-usage]
            with impl_scope.as_active_scope():
                event = await chain[0].event.load()

            assert isinstance(event, ScriptExecutionLogEvent)
            assert event.error is None
            assert event.rejected is None
            # ``present_to_user`` is the only op; ``ops`` has length 1.
            assert len(event.ops) == 1
            op = event.ops[0]
            assert op.kind == OP_KIND_PRESENT_TO_USER
            # The walker turns the dict into a frozendict; assert on
            # the key/value pairs.
            assert op.payload is not None
            assert op.payload["greeting"] == "hello"
            assert op.payload["n"] == 42


async def test_close_session_drains_log_intents(
    tmp_path: Path,
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> None:
    """``close_session`` pushes a ``_LogIntent_Shutdown`` sentinel,
    awaits the writer's drain, then cancels the agent_mcp subtask.
    Verify the close path returns cleanly with no errors propagated
    from the writer."""
    async with WorkstationServer() as ws:
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# chunk E close drain test\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            await _swd(ctx, str(tmp_path))
            await _conn(ctx)
            await _open(ctx, {"chunk": "E"})

            connection = app_ctx.connection
            assert connection is not None

            # Run a couple of scripts so the chain has entries.
            await _run(ctx, "# script 1\n")
            await _run(ctx, "# script 2\n")

            # Confirm chain extended (poll briefly).
            for _ in range(40):
                impl = connection._agent_mcp_impl  # type: ignore[private-usage]
                if impl is not None and impl.chain_head is not None:
                    chain = await _walk_chain(connection)
                    if len(chain) >= 2:
                        break
                await anyio.sleep(0.05)
            else:
                raise AssertionError("chain did not extend to expected length")

            # Close cleanly — the drain happens before the subtask
            # cancels. If the drain hangs or the writer crashes, this
            # call hangs (caught by SIGALRM timeout).
            r = await _close(ctx)
            assert r.startswith("closed session "), r

            # Agent_mcp impl has been torn down.
            assert connection._agent_mcp_impl is None  # type: ignore[private-usage]
            # Writer-done event was cleared on subtask exit.
            assert connection._log_writer_done is None  # type: ignore[private-usage]


async def test_user_message_via_send_lands_as_callback_log_event(
    tmp_path: Path,
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> None:
    """``AgentMcpRunningSessionProto.send`` fires a workstation
    ``CallbackEvent`` on ``workstation_inbox``; the MCP's log writer
    records a :class:`CallbackNotificationLogEvent(slot_key=
    "workstation_inbox", ...)``."""
    from fxtr_workstation_mcp.agent_mcp_proto import AgentMcpRunningSessionProto

    async with WorkstationServer() as ws:
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# chunk E user-message test\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            await _swd(ctx, str(tmp_path))
            await _conn(ctx)
            await _open(ctx, {"chunk": "E"})

            connection = app_ctx.connection
            assert connection is not None
            session_id = connection.active_session_id
            assert session_id is not None

            # Use a separate /rpc peer to play the role of the
            # frontend: look up ``agent_mcp:{session_id}`` and call
            # ``send`` on it.
            from fxtr_workstation.protocols import WorkstationServiceProto

            async with connect_peer_rpc(ws.rpc_url) as peer:
                ws_ref = peer.remote_service.replace_service_type(WorkstationServiceProto)
                ws_proxy = await ws_ref.resolve(peer.scope)
                agent_mcp_short_id = await ws_proxy.lookup_short_id_by_external_name(
                    f"agent_mcp:{session_id}"
                )
                agent_mcp_ref = (
                    await ws_proxy.get_service(agent_mcp_short_id)
                ).replace_service_type(AgentMcpRunningSessionProto)
                agent_mcp_proxy = await agent_mcp_ref.resolve(peer.scope)

                # Send a payload. The workstation routes the
                # callback through the inbox, fires a CallbackEvent
                # on the MCP's subscription, the log writer extends
                # the chain.
                await agent_mcp_proxy.send({"from": "user", "msg": "hi"})

            # The chain head changes asynchronously after send returns.
            await _wait_for_chain_head_change(connection, previous_head_id=None)

            chain = await _walk_chain(connection)
            # Exactly one entry — the CallbackNotificationLogEvent
            # for the inbox.
            assert len(chain) == 1

            impl = connection._agent_mcp_impl  # type: ignore[private-usage]
            assert impl is not None
            impl_scope: FxtrScope = impl._scope  # type: ignore[private-usage]
            with impl_scope.as_active_scope():
                event = await chain[0].event.load()

            assert isinstance(event, CallbackNotificationLogEvent)
            assert event.slot_key == "workstation_inbox"
            # The payload was wire-form-walked into a typed cbor; the
            # dict survives as a frozendict.
            assert event.payload is not None
            assert event.payload["from"] == "user"
            assert event.payload["msg"] == "hi"


# --- Reconnect / persistence-load tests -------------------------------


async def _precreate_session(client_url: str) -> str:
    """Create a session via the workstation's ``/client`` endpoint
    so the toml can name it as ``active_session``. Returns the
    session UUID."""
    async with connect_peer_client(client_url) as client:
        await client.send(CreateSessionRequest(request_id=1, metadata={}))
        frame = await client.receive()
        assert isinstance(frame, SessionCreatedResponse)
        return frame.session_id


async def test_reconnect_loads_chain_head_from_persistence(
    tmp_path: Path,
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> None:
    """First cycle: extend the chain + pin to the persistence cap.
    Second cycle (fresh ``WorkstationConnection`` on the same
    workstation): the bootstrap loads the prior chain head from the
    cap so ``impl.chain_head`` is non-``None`` at startup."""
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
            (tmp_path / "preamble.py").write_text("# reconnect test\n")

            # Cycle 1: run a script so the chain has a real head;
            # capture the head id.
            captured_head_id: FxtrID
            async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
                ctx = _FakeContext(app_ctx)
                await _swd(ctx, str(tmp_path))
                await _conn(ctx)

                connection = app_ctx.connection
                assert connection is not None

                r = await _run(ctx, "# script 1\n")
                assert "Error" not in r, r

                await _wait_for_chain_head_change(connection, previous_head_id=None)
                impl = connection._agent_mcp_impl  # type: ignore[private-usage]
                assert impl is not None
                assert impl.chain_head is not None
                captured_head_id = impl.chain_head.entity_id

            # Cycle 2: re-enter the lifespan. The supervisor opens a
            # fresh ``WorkstationConnection``, runs the bootstrap
            # against the same workstation, and the persistence cap
            # still holds the pinned head from cycle 1.
            async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx2:
                ctx2 = _FakeContext(app_ctx2)
                await _swd(ctx2, str(tmp_path))
                await _conn(ctx2)

                connection2 = app_ctx2.connection
                assert connection2 is not None

                impl2 = connection2._agent_mcp_impl  # type: ignore[private-usage]
                assert impl2 is not None
                assert impl2.chain_head is not None
                assert impl2.chain_head.entity_id == captured_head_id


async def test_disabled_persistence_chain_does_not_survive_reconnect(
    tmp_path: Path,
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> None:
    """With ``agent_mcp_store = false`` the chain extends in memory
    but no pin happens; a reconnect (new cycle) starts with an empty
    chain head."""
    async with WorkstationServer() as ws:
        session_id = await _precreate_session(ws.client_url)
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n'
            f'active_session = "{session_id}"\n'
            "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# disabled-persistence test\n")

        # Cycle 1: chain extends in memory.
        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            await _swd(ctx, str(tmp_path))
            await _conn(ctx)
            connection = app_ctx.connection
            assert connection is not None
            await _run(ctx, "# script 1\n")
            await _wait_for_chain_head_change(connection, previous_head_id=None)
            impl = connection._agent_mcp_impl  # type: ignore[private-usage]
            assert impl is not None
            assert impl.chain_head is not None
            # No persistence ref held.
            assert connection._agent_mcp_persistence_ref is None  # type: ignore[private-usage]

        # Cycle 2: chain starts empty — nothing was pinned.
        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx2:
            ctx2 = _FakeContext(app_ctx2)
            await _swd(ctx2, str(tmp_path))
            await _conn(ctx2)
            connection2 = app_ctx2.connection
            assert connection2 is not None
            impl2 = connection2._agent_mcp_impl  # type: ignore[private-usage]
            assert impl2 is not None
            assert impl2.chain_head is None


async def test_chain_pinned_after_each_extension(
    tmp_path: Path,
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> None:
    """Each chain extension calls ``persistence.pin(session_id,
    head_id, overwrite=True)``. After running multiple scripts the
    cap holds the *latest* head — verified by reading via a separate
    /rpc peer."""
    from fxtr_workstation.protocols import PersistenceProto, WorkstationServiceProto

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
            (tmp_path / "preamble.py").write_text("# pin-each-extension test\n")

            async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
                ctx = _FakeContext(app_ctx)
                await _swd(ctx, str(tmp_path))
                await _conn(ctx)
                connection = app_ctx.connection
                assert connection is not None

                # Two scripts → two extensions.
                await _run(ctx, "# script 1\n")
                await _wait_for_chain_head_change(connection, previous_head_id=None)
                impl = connection._agent_mcp_impl  # type: ignore[private-usage]
                assert impl is not None
                first_head_id = impl.chain_head.entity_id  # type: ignore[union-attr]

                await _run(ctx, "# script 2\n")
                await _wait_for_chain_head_change(connection, previous_head_id=first_head_id)
                second_head_id = impl.chain_head.entity_id  # type: ignore[union-attr]
                assert second_head_id != first_head_id

                # Read the cap via a separate /rpc peer — should
                # report the *latest* head.
                async with connect_peer_rpc(ws.rpc_url) as peer:
                    ws_ref = peer.remote_service.replace_service_type(WorkstationServiceProto)
                    ws_proxy = await ws_ref.resolve(peer.scope)
                    cap_short_id = await ws_proxy.lookup_short_id_by_external_name(
                        "agent_mcp_store"
                    )
                    cap_untyped = await ws_proxy.get_service(cap_short_id)
                    cap_ref = cap_untyped.replace_service_type(PersistenceProto)
                    cap_proxy = await cap_ref.resolve(peer.scope)
                    pinned = await cap_proxy.try_retrieve(session_id)
                    assert pinned == second_head_id


async def test_present_to_user_entity_loadable_same_cycle(
    tmp_path: Path,
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> None:
    """Smaller-scope sibling of
    :func:`test_reconnect_loads_present_to_user_entity_data` that
    only exercises the same-cycle path: after the chain extends,
    the ``present_to_user`` op's :class:`RawWeakFxtrID` payload
    must resolve and the referenced entity must be loadable
    (which proves my fix's host_scope lease is keeping the entity
    alive on the workstation past the per-script
    ``AsyncExitStack`` exit).

    Doesn't pin / reload — that's the reconnect test's job.
    """
    async with WorkstationServer() as ws:
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# present_to_user entity-loadable test\n")

        script_src = (
            "import dataclasses, uuid\n"
            "from fxtr2.conversion.converters.mashumaro_entities import "
            "DataclassEntity\n"
            "from fxtr2.data import system_entities\n"
            "from _fxtr_mcp_builtin import present_to_user\n"
            "\n"
            "_NS = uuid.UUID('c0ffee01-2222-3333-4444-555566667777')\n"
            "\n"
            "@dataclasses.dataclass(frozen=True)\n"
            "class _Memo(\n"
            "    DataclassEntity,\n"
            "    fxtr_type=system_entities.define_type(_NS, 'memo'),\n"
            "):\n"
            "    title: str\n"
            "    body: str\n"
            "\n"
            "memo = _Memo(title='same-cycle', body='loadable in this cycle')"
            ".store()\n"
            "await present_to_user(memo)\n"
        )

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            await _swd(ctx, str(tmp_path))
            await _conn(ctx)
            await _open(ctx, {"chunk": "F"})
            connection = app_ctx.connection
            assert connection is not None
            r = await _run(ctx, script_src)
            assert "Error" not in r, r
            await _wait_for_chain_head_change(connection, previous_head_id=None)

            chain = await _walk_chain(connection)
            assert len(chain) == 1

            impl = connection._agent_mcp_impl  # type: ignore[private-usage]
            assert impl is not None
            impl_scope: FxtrScope = impl._scope  # type: ignore[private-usage]
            with impl_scope.as_active_scope():
                event = await chain[0].event.load()

            assert isinstance(event, ScriptExecutionLogEvent)
            assert event.error is None
            assert event.rejected is None
            assert len(event.ops) == 1
            op = event.ops[0]
            assert op.kind == OP_KIND_PRESENT_TO_USER

            from fxtr2.conversion.converters.mashumaro_entities import EntityRef
            from fxtr2.conversion.converters.unknown_entity import UnknownEntity
            from fxtr2.data.entity_core import RawWeakFxtrID

            assert isinstance(op.payload, RawWeakFxtrID), (
                f"expected RawWeakFxtrID at present_to_user payload, "
                f"got {type(op.payload).__name__}: {op.payload!r}"
            )
            memo_fxtr_id = op.payload.inner_id

            memo_ref: EntityRef[UnknownEntity] = EntityRef(
                entity_id=memo_fxtr_id, target_type=UnknownEntity
            )
            with impl_scope.as_active_scope():
                memo_data = await memo_ref.load()
            assert memo_data.raw_cbor["title"] == "same-cycle"
            assert memo_data.raw_cbor["body"] == "loadable in this cycle"


async def test_reconnect_loads_present_to_user_entity_data(
    tmp_path: Path,
    sandbox_env_supervisor: SandboxEnvSupervisor,
) -> None:
    """Regression test for the entity-lifetime bug fixed by
    ``_prefill_entity_short_ids``: when a script ``present_to_user``s
    an entity, the chain head must pin in a way that *transitively
    persists the entity's data* — so that on a fresh cycle (with the
    workstation server still running, but the MCP supervisor torn
    down and restarted), the agent can walk the chain back to the
    ``PresentToUserOpRecord`` AND load the referenced entity's
    fields.

    The earlier bug had two symptoms in this path:

    1. The walker's ``workstation.get_entity(short_id)`` raised
       "unknown or inactive entity short id" because the per-script
       ``expose_entity`` registration had already released.
    2. Even if (1) is patched (which the prefill cache does), the
       transitively-referenced entity's data has to be reachable
       from the scope ``persistence.pin`` is dispatched on, so the
       persistence cap walks into it. This test catches a
       regression on either front.
    """
    # Use ``preserve_provenance`` to exercise the full round-trip
    # including provenance preservation — matches the
    # ``run_demo_workstation.py`` cap configuration. (Until the
    # scope-level bare-mode provenance leak in
    # ``entity_ref_scope.store_cbor`` was fixed, this mode hit a
    # downstream ``_encode_fxtr_id received an ID with a
    # provenance parent`` assertion in the cap-IR-arena layer.)
    async with SQLiteFxtrNamespace(str(tmp_path / "store.sqlite"), "ns") as ns:
        cap_cfg = PersistenceNamespaceConfig(
            external_name="agent_mcp_store",
            namespace=ns,
            mode="preserve_provenance",
        )
        async with WorkstationServer(persistence_namespaces=[cap_cfg]) as ws:
            session_id = await _precreate_session(ws.client_url)
            (tmp_path / CONFIG_FILENAME).write_text(
                f'workstation_url = "{ws.bound_url}"\n'
                f'active_session = "{session_id}"\n'
                'agent_mcp_store = "agent_mcp_store"\n'
            )
            (tmp_path / "preamble.py").write_text("# reconnect-after-present_to_user test\n")

            # Cycle 1: the script defines a small ``DataclassEntity``
            # type in-line, stores an instance, and present_to_user's
            # it. The class definition is sandbox-local; what matters
            # for round-trip is the deterministic ``fxtr_type`` (the
            # namespace UUID + label).
            script_src = (
                "import dataclasses, uuid\n"
                "from fxtr2.conversion.converters.mashumaro_entities import "
                "DataclassEntity\n"
                "from fxtr2.data import system_entities\n"
                "from _fxtr_mcp_builtin import present_to_user\n"
                "\n"
                "_NS = uuid.UUID('c0ffee01-2222-3333-4444-555566667777')\n"
                "\n"
                "@dataclasses.dataclass(frozen=True)\n"
                "class _Memo(\n"
                "    DataclassEntity,\n"
                "    fxtr_type=system_entities.define_type(_NS, 'memo'),\n"
                "):\n"
                "    title: str\n"
                "    body: str\n"
                "\n"
                "memo = _Memo(title='persisted', body='survive a reconnect')"
                ".store()\n"
                "await present_to_user(memo)\n"
            )

            async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
                ctx = _FakeContext(app_ctx)
                await _swd(ctx, str(tmp_path))
                await _conn(ctx)
                connection = app_ctx.connection
                assert connection is not None
                r = await _run(ctx, script_src)
                assert "Error" not in r, r
                await _wait_for_chain_head_change(connection, previous_head_id=None)
                impl = connection._agent_mcp_impl  # type: ignore[private-usage]
                assert impl is not None
                assert impl.chain_head is not None

            # Cycle 2: fresh MCP supervisor cycle on the same
            # workstation. The persistence cap was pinned in cycle
            # 1; reloading from it must give us back a chain head
            # whose ``present_to_user`` op's payload is loadable.
            async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx2:
                ctx2 = _FakeContext(app_ctx2)
                await _swd(ctx2, str(tmp_path))
                await _conn(ctx2)
                connection2 = app_ctx2.connection
                assert connection2 is not None

                impl2 = connection2._agent_mcp_impl  # type: ignore[private-usage]
                assert impl2 is not None
                assert impl2.chain_head is not None

                # Walk the chain — there should be exactly one
                # entry, the ScriptExecutionLogEvent.
                chain = await _walk_chain(connection2)
                assert len(chain) == 1

                impl_scope2: FxtrScope = impl2._scope  # type: ignore[private-usage]
                with impl_scope2.as_active_scope():
                    event = await chain[0].event.load()

                assert isinstance(event, ScriptExecutionLogEvent)
                assert event.error is None
                assert event.rejected is None
                # The present_to_user op is the only one.
                assert len(event.ops) == 1
                op = event.ops[0]
                assert op.kind == OP_KIND_PRESENT_TO_USER

                # The op's payload is a ``RawWeakFxtrID`` wrapping
                # the memo's FxtrID — the substitution walker
                # decoded the wire ``["entity", short_id]`` leaf
                # into a typed cbor leaf.
                from fxtr2.conversion.converters.mashumaro_entities import (
                    EntityRef,
                )
                from fxtr2.conversion.converters.unknown_entity import (
                    UnknownEntity,
                )
                from fxtr2.data.entity_core import RawWeakFxtrID

                assert isinstance(op.payload, RawWeakFxtrID), (
                    f"expected RawWeakFxtrID at present_to_user payload, "
                    f"got {type(op.payload).__name__}: {op.payload!r}"
                )
                memo_fxtr_id = op.payload.inner_id

                # The big assertion: the memo's data must still be
                # loadable on this fresh cycle. We don't have the
                # ``_Memo`` Python class here — it was defined
                # inside the sandbox script — so load via
                # :class:`UnknownEntity`, which gives us a typed
                # view onto the cbor.
                memo_ref: EntityRef[UnknownEntity] = EntityRef(
                    entity_id=memo_fxtr_id, target_type=UnknownEntity
                )
                with impl_scope2.as_active_scope():
                    memo_data = await memo_ref.load()

                # ``UnknownEntity.raw_cbor`` is the frozen decoded
                # cbor dict; assert the fields we stored survived.
                assert memo_data.raw_cbor["title"] == "persisted"
                assert memo_data.raw_cbor["body"] == "survive a reconnect"
