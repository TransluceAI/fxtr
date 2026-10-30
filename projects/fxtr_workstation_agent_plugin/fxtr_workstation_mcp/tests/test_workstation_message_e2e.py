"""End-to-end tests for the ``workstation_message`` channel
notification (fix 3 of stage3_mcp_backend_tweaks).

When the operator sends a message via
``AgentMcpRunningSessionProto.send(payload)``, the workstation
fires a ``CallbackEvent`` on the reserved ``workstation_inbox``
slot. The MCP's ``_apply_state_effect`` callback branch
special-cases this slot to emit a channel notification with
``kind="workstation_message"`` and the **full** payload as
content (rather than the generic ``kind="callback"`` with a
truncated ``repr`` summary).

For string payloads the content is the raw string verbatim; for
non-string payloads the content is the unabbreviated ``repr``
plus a brief hint pointing the agent at
``retrieve_callback_notification('workstation_inbox', <seq>)``
if they need the typed object.
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
from fxtr_workstation import WorkstationServer
from fxtr_workstation.protocols import WorkstationServiceProto

from fxtr_workstation_mcp.agent_mcp_proto import AgentMcpRunningSessionProto
from fxtr_workstation_mcp.app_context import AppContext
from fxtr_workstation_mcp.config import CONFIG_FILENAME
from fxtr_workstation_mcp.sandbox_env import SandboxEnvSupervisor
from fxtr_workstation_mcp.server import (
    connect,
    make_app_ctx,
    open_session,
    set_working_dir,
)

from ._events import ParsedEvent, parse_events_from_sources
from ._peer import connect_peer_rpc

pytestmark = pytest.mark.anyio


# --- Channel-capturing fake MCP context ------------------------------
#
# Mirrors test_callback_e2e.py's harness. We need ``ctx.session`` to
# be non-None so the channel notifier actually emits frames (it
# asserts the session is present), and we capture every frame
# pushed through it so the test can assert on the rendered shape.


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


def _all_events(ctx: _FakeContext) -> list[ParsedEvent]:
    return parse_events_from_sources(
        channel_frame_contents=[f.content for f in ctx.session._write_stream.frames],
        tool_outputs=ctx.tool_outputs,
    )


async def _wait_for_workstation_message(
    ctx: _FakeContext, *, timeout_s: float = 2.0
) -> ParsedEvent:
    """Poll all event sources until an event with
    ``kind="workstation_message"`` is present, or raise."""
    deadline = anyio.current_time() + timeout_s
    while anyio.current_time() < deadline:
        for ev in _all_events(ctx):
            if ev.attrs.get("kind") == "workstation_message":
                return ev
        await anyio.sleep(0.05)
    raise AssertionError(
        f"timed out waiting for workstation_message event; saw: "
        f"{[(e.attrs, e.content) for e in _all_events(ctx)]}"
    )


async def _send_via_agent_mcp(rpc_url: str, session_id: str, payload: Any) -> None:
    """Open a peer ``/rpc`` connection, resolve
    ``agent_mcp:{session_id}``, and call ``send(payload)``."""
    async with connect_peer_rpc(rpc_url) as peer:
        ws_ref = peer.remote_service.replace_service_type(WorkstationServiceProto)
        ws_proxy = await ws_ref.resolve(peer.scope)
        agent_mcp_short_id = await ws_proxy.lookup_short_id_by_external_name(
            f"agent_mcp:{session_id}"
        )
        agent_mcp_ref = (await ws_proxy.get_service(agent_mcp_short_id)).replace_service_type(
            AgentMcpRunningSessionProto
        )
        agent_mcp_proxy = await agent_mcp_ref.resolve(peer.scope)
        await agent_mcp_proxy.send(payload)


# --- Tests ----------------------------------------------------------


async def test_inbox_string_payload_renders_verbatim(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """String payloads emit ``kind="workstation_message"`` with the
    raw string as the event content — no ``repr`` quoting, no
    truncation, no retrieval hint."""
    async with WorkstationServer() as ws:
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# fix3 workstation_message string-payload test\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            await _swd(ctx, str(tmp_path))
            await _conn(ctx)
            await _open(ctx, {"phase": "fix3-str"})

            connection = app_ctx.connection
            assert connection is not None
            session_id = connection.active_session_id
            assert session_id is not None

            message = (
                "Hello agent! This is a much longer string than the 200-char "
                "summary cap so we can verify the full payload comes through "
                "without any truncation by summarize_invoke_payload. " * 2
            )
            assert len(message) > 200, "test message too short to catch truncation regression"

            await _send_via_agent_mcp(ws.bound_url + "/rpc", session_id, message)

            ev = await _wait_for_workstation_message(ctx)
            assert ev.attrs["kind"] == "workstation_message"
            assert ev.attrs["slot_key"] == "workstation_inbox"
            assert ev.attrs.get("notif_index", "").isdigit()
            # Full content, verbatim.
            assert ev.content == message
            # No ellipsis (no truncation).
            assert "…" not in ev.content
            # No retrieve hint on the string path.
            assert "retrieve_callback_notification" not in ev.content


# A tiny entity defined here just so the peer has something to embed
# in a send payload. The sandbox doesn't need this Python class — the
# wire codec carries the entity as a bare FxtrID and the MCP-side log
# walker only stores a ``WorkstationServicePlaceholder``-style record
# referencing the short_id.
_INBOX_TEST_NS = uuid.UUID("aabbccdd-eeff-1122-3344-556677889900")
_INBOX_TEST_NOTE_TYPE_ID = system_entities.define_type(_INBOX_TEST_NS, "ws_mcp_inbox_test_note")


@dataclasses.dataclass(frozen=True)
class _InboxTestNote(DataclassEntity, fxtr_type=_INBOX_TEST_NOTE_TYPE_ID):
    title: str


async def test_inbox_dict_payload_renders_repr_with_hint(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """Non-string payloads emit the unabbreviated ``repr`` plus a
    hint pointing at ``retrieve_callback_notification`` so the
    agent can fetch the typed object if they need to walk it."""
    async with WorkstationServer() as ws:
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# fix3 workstation_message dict-payload test\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            await _swd(ctx, str(tmp_path))
            await _conn(ctx)
            await _open(ctx, {"phase": "fix3-dict"})

            connection = app_ctx.connection
            assert connection is not None
            session_id = connection.active_session_id
            assert session_id is not None

            payload = {"from": "user", "msg": "hi", "priority": 7}
            await _send_via_agent_mcp(ws.bound_url + "/rpc", session_id, payload)

            ev = await _wait_for_workstation_message(ctx)
            assert ev.attrs["kind"] == "workstation_message"
            assert ev.attrs["slot_key"] == "workstation_inbox"
            notif_idx = ev.attrs.get("notif_index", "")
            assert notif_idx.isdigit()

            # The wire-form payload is a dict (frozendict on the
            # receive side); its repr should appear in the content.
            assert "'from'" in ev.content or '"from"' in ev.content
            assert "'user'" in ev.content or '"user"' in ev.content
            # Retrieval hint with the exact slot_key + seq.
            assert "retrieve_callback_notification" in ev.content
            assert "'workstation_inbox'" in ev.content
            assert notif_idx in ev.content


async def test_inbox_payload_with_entity_ref(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """Payloads carrying an :class:`EntityRef` flow through ``send``
    cleanly.

    Regression test for the scope bug where
    :meth:`AgentMcpRunningSessionImpl.send` resolves the inbox
    callback ref against ``self._scope`` (the impl_scope) rather
    than the active call_scope. The typed-service framework bridges
    each incoming entity ref into the per-call ``call_scope`` (a
    child of the impl_scope), so encoding the payload through a
    proxy whose home scope is ``impl_scope`` cannot reach the
    entity — the wire encode fails with an
    ``IRException(category='Lookup')`` ("entity not found in any
    parent scope") that propagates back through the workstation to
    the peer.

    After the fix, ``send`` captures the active call scope and
    resolves the inbox ref against it; the proxy's home scope is
    the call scope (which the entity is loadable in), so encoding
    succeeds.
    """
    async with WorkstationServer() as ws:
        (tmp_path / CONFIG_FILENAME).write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        (tmp_path / "preamble.py").write_text("# inbox entity-ref regression test\n")

        async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
            ctx = _FakeContext(app_ctx)
            await _swd(ctx, str(tmp_path))
            await _conn(ctx)
            await _open(ctx, {"phase": "inbox-entity-ref"})

            connection = app_ctx.connection
            assert connection is not None
            session_id = connection.active_session_id
            assert session_id is not None

            # Peer opens its own /rpc connection, stores a note in
            # the peer's RPC scope (so the workstation can bridge
            # it across when it receives the send payload), and
            # invokes agent_mcp.send with the entity_id embedded
            # alongside a plain field.
            async with connect_peer_rpc(ws.bound_url + "/rpc") as peer:
                note_id = _InboxTestNote(title="from-operator").store(scope=peer.scope).entity_id
                ws_ref = peer.remote_service.replace_service_type(WorkstationServiceProto)
                ws_proxy = await ws_ref.resolve(peer.scope)
                agent_mcp_short_id = await ws_proxy.lookup_short_id_by_external_name(
                    f"agent_mcp:{session_id}"
                )
                agent_mcp_ref = (
                    await ws_proxy.get_service(agent_mcp_short_id)
                ).replace_service_type(AgentMcpRunningSessionProto)
                agent_mcp_proxy = await agent_mcp_ref.resolve(peer.scope)

                # Before the fix: this raises an IRException because
                # the MCP-side impl.send can't encode the entity_id
                # for the wire to the inbox callback (the entity is
                # only loadable in the framework-bridged call_scope,
                # not in impl_scope where ``send`` resolves the
                # callback ref).
                await agent_mcp_proxy.send({"msg": "look at this", "note": note_id})

            ev = await _wait_for_workstation_message(ctx)
            assert ev.attrs["kind"] == "workstation_message"
            assert ev.attrs["slot_key"] == "workstation_inbox"
            # The payload should round-trip through the MCP's
            # callback-notification rendering. We don't pin the
            # exact entity-ref rendering shape here — just that the
            # event arrives with the test marker in the content so
            # we know the payload reached the channel.
            assert "look at this" in ev.content
