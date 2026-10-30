"""End-to-end stage-4 integration test for invoke.

A peer registers an :class:`EchoImpl` service on the workstation.
The MCP runs:

1. A first script that ``schedule_invoke``s ``echo("hello")``.
2. A second script that ``retrieve_result``s the slot — expecting
   ``"echo:hello"``.

Covers:

- ``schedule_invoke``: arg walk, mutation-builder slot allocation,
  ``InvokeOp`` on the wire, mutation commit.
- Workstation: ``invoke_worker`` dispatches the method, broadcasts
  ``InvokeResultEvent``.
- MCP: ``_apply_state_effect`` for ``InvokeResultEvent`` promotes
  the slot in ``mirror.data_by_key`` (``["invoke_result",
  payload]``) and records the unflushed entry.
- The second script's mutation flushes the unflushed entry
  (workstation absorbs the promotion).
- ``retrieve_result``: ``read_state_slot`` host RPC reads the
  mirror; sandbox-side decode returns the raw payload.

Heavy: factory bootstrap is paid on first ``run_script``.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Protocol

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


# --- Tiny echo service (peer-registered) ----------------------------


_ECHO_NS = uuid.UUID("aabbccdd-1111-2222-3333-444455556666")
_ECHO_TYPE_ID = system_entities.define_type(_ECHO_NS, "stage4_echo")


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


# --- Fake MCP-context helpers ----------------------------------------


class _FakeWriteStream:
    """Records messages instead of forwarding them to a transport."""

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> None:
        self.sent.append(message)


class _FakeSession:
    """Stub for the ``ServerSession`` FastMCP would normally hand
    a tool callable. The channel notifier (see ``channel.py``)
    asserts a captured session exists when an invoke-result event
    fires, so tests that exercise schedule_invoke must supply
    something with the shape it consumes (``_write_stream.send``).
    Captured frames live on ``write_stream.sent`` for test
    inspection.
    """

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


# --- The test --------------------------------------------------------


@pytest.mark.slow
async def test_schedule_invoke_and_retrieve_result(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """Schedule an echo invoke from a sandboxed script; retrieve the
    result in a follow-up script."""
    import anyio

    async with WorkstationServer() as ws:
        # Peer registers EchoImpl as a workstation-tracked service.
        async with connect_peer_rpc(ws.rpc_url) as peer:
            echo_ref = await peer.scope.mount(_EchoImpl())
            async with expose_service(
                peer.remote_service,
                "test-echo",
                echo_ref,
                peer.scope,
            ) as svc_short_id:
                (tmp_path / CONFIG_FILENAME).write_text(
                    f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
                )
                (tmp_path / "preamble.py").write_text("# stage-4 e2e\n")

                async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
                    ctx = _FakeContext(app_ctx)
                    await _swd(ctx, str(tmp_path))
                    await _conn(ctx)
                    await _open(ctx, {"phase": "stage-4"})

                    # Script 1: schedule the invoke.
                    schedule_script = (
                        "from _fxtr_mcp_builtin import "
                        "schedule_invoke, get_service\n"
                        f"svc = get_service({svc_short_id!r})\n"
                        "await schedule_invoke("
                        '"r", svc, lambda s: s.echo("hello")'
                        ")\n"
                    )
                    r1 = await _run(ctx, schedule_script)
                    assert "Error" not in r1, r1

                    # The result event lands asynchronously. Poll
                    # the local mirror until the slot is promoted
                    # past invoke_pending — the test harness has a
                    # global SIGALRM timeout to bound the wait.
                    assert app_ctx.connection is not None
                    conn = app_ctx.connection
                    for _ in range(50):
                        v = conn._mirror.data_by_key.get("r")
                        if v is not None and v[0] != "invoke_pending":
                            break
                        await anyio.sleep(0.05)
                    else:
                        raise AssertionError(
                            f"timed out waiting for slot 'r' to promote; "
                            f"current value: {conn._mirror.data_by_key.get('r')!r}"
                        )
                    assert conn._mirror.data_by_key["r"] == [
                        "invoke_result",
                        "echo:hello",
                    ]

                    # Script 2: retrieve the result. Also flushes the
                    # unflushed entry via the pre-flush patch.
                    retrieve_script = (
                        "from _fxtr_mcp_builtin import retrieve_result\n"
                        'result = await retrieve_result("r")\n'
                        'assert result == "echo:hello", f"got {result!r}"\n'
                    )
                    r2 = await _run(ctx, retrieve_script)
                    assert "Error" not in r2, r2

                    # After flush: data_by_key still has the promoted
                    # value (it survives the flush — workstation
                    # absorbed it). _pending_changes is now empty
                    # since we sent the replace patch.
                    assert conn._pending_changes == []


@pytest.mark.slow
async def test_schedule_invoke_error(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """A service method that raises produces an
    :class:`InvokeErrorEvent`; ``retrieve_result`` raises
    ``RuntimeError``."""
    import anyio

    _raise_ns = uuid.UUID("ddeeffaa-9999-8888-7777-666655554444")
    _raise_type_id = system_entities.define_type(_raise_ns, "stage4_raise")

    @service_protocol
    class _RaiseProto(Protocol):
        async def boom(self, msg: str) -> str: ...

    class _RaiseImpl(TypedServiceImpl[_RaiseProto]):
        def _target_type_annotation(self):  # type: ignore[no-untyped-def]
            return _RaiseProto

        def _service_type_id(self) -> FxtrID:
            return _raise_type_id

        @service_method
        async def boom(self, msg: str) -> str:
            raise ValueError(f"intentional: {msg}")

    async with WorkstationServer() as ws:
        async with connect_peer_rpc(ws.rpc_url) as peer:
            ref = await peer.scope.mount(_RaiseImpl())
            async with expose_service(
                peer.remote_service,
                "test-raise",
                ref,
                peer.scope,
            ) as svc_short_id:
                (tmp_path / CONFIG_FILENAME).write_text(
                    f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
                )
                (tmp_path / "preamble.py").write_text("# stage-4 error e2e\n")

                async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
                    ctx = _FakeContext(app_ctx)
                    await _swd(ctx, str(tmp_path))
                    await _conn(ctx)
                    await _open(ctx, {})

                    schedule_script = (
                        "from _fxtr_mcp_builtin import "
                        "schedule_invoke, get_service\n"
                        f"svc = get_service({svc_short_id!r})\n"
                        "await schedule_invoke("
                        '"r", svc, lambda s: s.boom("zap")'
                        ")\n"
                    )
                    assert "Error" not in await _run(ctx, schedule_script)

                    assert app_ctx.connection is not None
                    conn = app_ctx.connection
                    for _ in range(50):
                        v = conn._mirror.data_by_key.get("r")
                        if v is not None and v[0] != "invoke_pending":
                            break
                        await anyio.sleep(0.05)
                    else:
                        raise AssertionError("timed out waiting for slot")
                    assert conn._mirror.data_by_key["r"][0] == "invoke_error"

                    # retrieve_result raises RuntimeError sandbox-side.
                    retrieve_script = (
                        "from _fxtr_mcp_builtin import retrieve_result\n"
                        "raised = False\n"
                        "try:\n"
                        '    await retrieve_result("r")\n'
                        "except RuntimeError as e:\n"
                        "    raised = True\n"
                        '    assert "intentional: zap" in str(e), str(e)\n'
                        'assert raised, "expected RuntimeError"\n'
                    )
                    assert "Error" not in await _run(ctx, retrieve_script)
