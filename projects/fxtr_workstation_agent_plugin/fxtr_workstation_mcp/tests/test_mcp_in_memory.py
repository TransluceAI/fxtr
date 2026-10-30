"""End-to-end test driving the MCP server in-process over real
JSON-RPC framing.

Uses :func:`mcp.shared.memory.create_client_server_memory_streams`
to wire the server's typed :class:`SessionMessage` streams up to
a client-side pair, then runs the package's :data:`FastMCP`
instance on the server end. Exercises:

- The actual FastMCP tool-dispatch machinery (``tools/call``
  JSON-RPC routed through to the registered tools, not just
  invoked as Python functions).
- Real ``SessionMessage`` serialization / deserialization at the
  transport layer.
- The ``ServerSession._write_stream.send(...)`` notification
  path that ``notifications/claude/channel`` rides on.
- The real lifespan + ``AppContext`` + workstation supervisor.
- The ``basic_test_demo`` sample workdir + scenarios.

vs. earlier ``test_mcp_subprocess.py``: that variant spawned a
real subprocess via ``mcp.client.stdio`` and ``uv run
fxtr-workstation-mcp``. It tested the same things this file does
plus the literal ``console_scripts`` entrypoint + ``uv``
launcher + the actual stdio framing implementation, but the
subprocess-teardown story (orphaned children when the test got
cancelled mid-cleanup) was a constant maintenance hazard.
``test_plugin_manifest_schema.py`` covers the launcher config
shape, and the entrypoint is exercised by manual operation; the
remaining unique coverage from the subprocess variant (FastMCP
dispatch + real JSON-RPC framing) is preserved here without the
subprocess.

Why a raw JSON-RPC layer instead of ``ClientSession``: the typed
``ServerNotification`` union doesn't include
``notifications/claude/channel`` (it's an Anthropic-custom
extension), so ``ClientSession`` would silently drop those
frames at validation time. We use the raw memory streams
directly and route messages by JSON-RPC shape ourselves.
"""

from __future__ import annotations

import contextlib
import shutil
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import anyio
import pytest
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.shared.memory import create_client_server_memory_streams
from mcp.shared.message import SessionMessage
from mcp.types import (
    JSONRPCError,
    JSONRPCMessage,
    JSONRPCNotification,
    JSONRPCResponse,
)
from fxtr_workstation import WorkstationServer, expose_entity, expose_service
from fxtr_workstation.external_services import ExternalServiceConfig
from fxtr_workstation.dynamic_dispatch import mount_nix_configurable_dispatch
from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope
from fxtr_sandboxing.spawner import create_sandbox_environment
from fxtr_workdir_harness import prepare_workdir_repo

# Paths -----------------------------------------------------------------

_TESTS = Path(__file__).resolve().parent
_MCP_PKG = _TESTS.parent
_EXAMPLES = _MCP_PKG.parent / "examples" / "basic_test_demo"
_AGENT_WORKDIR = _EXAMPLES / "agent_workdir"

# Import the demo workstation pieces so we can boot an equivalent
# server in-process. ``run_demo_workstation`` adds the demo_pkg's
# src/ to sys.path on import (the same src-layout subdir the
# MCP's sandbox bridge will add when run_script runs), which is
# needed for the Note type registration to be consistent across
# this test process and the MCP's sandbox.
sys.path.insert(0, str(_EXAMPLES))
from run_demo_workstation import (  # noqa: E402
    DELAYED_ECHO_NAME,
    DEMO_NOTE_NAME,
    DelayedEchoImpl,
    _callback_tester_worker,
)
from demo_pkg import SANDBOX_SPAWNER_NAME, Note  # noqa: E402

from ._peer import connect_peer_rpc  # noqa: E402

# The agent_workdir's demo_data package — deterministic type id (from
# define_type), so the in-repo source + the prepared-copy flake agree on the
# preset key. Used by the nix set_preset E2E to build a resolvable type ref.
sys.path.insert(0, str(_AGENT_WORKDIR / "packages" / "demo_data" / "src"))
from demo_data import DemoDataEntity  # noqa: E402

# External name the dispatcher is registered under (the agent looks it up).
RENDERER_DISPATCHER_NAME = "renderer-dispatcher"
# FullPanel dispatch-key UUID the demo preset routes (see the flake +
# renderers/demo_data/index.tsx).
_FULL_PANEL = "21b2f3a8-7d54-4a1e-9c63-6a4b8e0f1234"

pytestmark = pytest.mark.anyio


# --- Raw JSON-RPC client over typed SessionMessage streams ------------


class _MemoryClient:
    """Minimal JSON-RPC 2.0 client over the SessionMessage streams
    produced by :func:`create_client_server_memory_streams`. Sends
    typed JSON-RPC requests/notifications, collects responses by
    request id, routes incoming notifications onto a memory stream
    the test can pull from.

    The wire format is the typed :class:`SessionMessage` /
    :class:`JSONRPCMessage` pair the upstream library uses, so we
    exercise the same encode + dispatch the real stdio transport
    does — minus the byte-level framing, which is upstream
    concern.
    """

    def __init__(
        self,
        read_stream: MemoryObjectReceiveStream[SessionMessage | Exception],
        write_stream: MemoryObjectSendStream[SessionMessage],
    ) -> None:
        self._read = read_stream
        self._write = write_stream
        self._next_id = 0
        self._response_events: dict[int, anyio.Event] = {}
        self._responses: dict[int, dict[str, Any]] = {}
        self._notifs_send, self._notifs_recv = anyio.create_memory_object_stream[dict[str, Any]](
            max_buffer_size=128
        )
        # Tool results accumulate here so :meth:`wait_for_slot_event`
        # can search them for ``<event …>`` blocks that landed inside
        # tool drains (rather than as separate channel frames).
        self.tool_outputs: list[str] = []

    async def _send(self, payload: dict[str, Any]) -> None:
        msg = JSONRPCMessage.model_validate(payload)
        await self._write.send(SessionMessage(message=msg))

    async def request(self, method: str, params: Any = None) -> dict[str, Any]:
        self._next_id += 1
        rid = self._next_id
        event = anyio.Event()
        self._response_events[rid] = event
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        await self._send(payload)
        await event.wait()
        return self._responses.pop(rid)

    async def notify(self, method: str, params: Any = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._send(payload)

    async def reader_loop(self) -> None:
        """Drain ``read_stream`` and dispatch. Runs until the stream
        ends or the task is cancelled."""
        async for msg in self._read:
            if isinstance(msg, Exception):
                # Transport error; surface to any waiting requesters.
                for ev in self._response_events.values():
                    ev.set()
                return
            root = msg.message.root
            if isinstance(root, JSONRPCResponse):
                rid = int(root.id)
                self._responses[rid] = dict(root.result or {})
                self._response_events.pop(rid, anyio.Event()).set()
            elif isinstance(root, JSONRPCError):
                rid = int(root.id)
                self._responses[rid] = {"_error": root.error.model_dump()}
                self._response_events.pop(rid, anyio.Event()).set()
            elif isinstance(root, JSONRPCNotification):
                params = root.params or {}
                await self._notifs_send.send({"method": root.method, "params": dict(params)})
            else:
                # JSONRPCRequest from the server isn't expected for our
                # tool surface; drop.
                continue

    async def wait_for_notification(
        self,
        method: str,
        *,
        meta_match: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """Wait for the next notification with ``method`` (and,
        optionally, ``meta`` fields matching ``meta_match``). Any
        notifications that don't match are dropped."""
        with anyio.fail_after(timeout):
            while True:
                n = await self._notifs_recv.receive()
                if n["method"] != method:
                    continue
                if meta_match is not None:
                    meta = n["params"].get("meta", {})
                    if not all(meta.get(k) == v for k, v in meta_match.items()):
                        continue
                return n

    async def initialize(self) -> dict[str, Any]:
        """MCP initialize handshake. Sends ``initialize`` request +
        ``notifications/initialized`` notification."""
        result = await self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {
                    "name": "test_mcp_in_memory",
                    "version": "0.0",
                },
            },
        )
        await self.notify("notifications/initialized")
        return result

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """Send ``tools/call``, return the first text content of the
        response (or a stringified error if the call failed). The
        result text is also appended to :attr:`tool_outputs` so
        :meth:`wait_for_slot_event` can search prior tool drains
        for ``<event …>`` blocks delivered inline."""
        result = await self.request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
        )
        if "_error" in result:
            text = f"_ERROR_: {result['_error']}"
            self.tool_outputs.append(text)
            return text
        content = result.get("content") or []
        if not content:
            self.tool_outputs.append("")
            return ""
        first = content[0]
        if isinstance(first, dict):
            text = str(first.get("text", ""))
        else:
            text = str(first)
        self.tool_outputs.append(text)
        return text

    async def wait_for_slot_event(
        self,
        slot_key: str,
        *,
        kind: str,
        timeout: float = 10.0,
    ) -> dict[str, str]:
        """Wait for an ``<event slot_key="X" kind="Y">…</event>``
        block to be visible to the agent — either already in a
        previously-collected tool output, or in a future
        ``notifications/claude/channel`` frame.

        Returns the parsed event's attribute dict (with the
        ``content`` body added under ``"_content"`` for
        assertions). The buffer abstraction means the notification
        can arrive in either path depending on whether a tool
        was active when it landed."""
        from ._events import parse_events

        # Check already-collected tool outputs first — the event may
        # have landed inline in a prior tool's drain.
        for s in self.tool_outputs:
            for ev in parse_events(s):
                if ev.attrs.get("slot_key") == slot_key and ev.attrs.get("kind") == kind:
                    return {**ev.attrs, "_content": ev.content}
        # Otherwise wait for a debounced channel frame.
        with anyio.fail_after(timeout):
            while True:
                n = await self._notifs_recv.receive()
                if n["method"] != "notifications/claude/channel":
                    continue
                for ev in parse_events(n["params"].get("content", "")):
                    if ev.attrs.get("slot_key") == slot_key and ev.attrs.get("kind") == kind:
                        return {**ev.attrs, "_content": ev.content}


# --- Helpers -----------------------------------------------------------


@contextlib.asynccontextmanager
async def _demo_workdir(ws_url: str, tmp_path: Path) -> AsyncGenerator[Path]:
    """Copy ``basic_test_demo/agent_workdir`` to a temp directory
    and write the toml pointing at ``ws_url``. We don't mutate the
    repo copy."""
    dst = tmp_path / "agent_workdir"
    shutil.copytree(_AGENT_WORKDIR, dst)
    (dst / ".fxtr_workstation.toml").write_text(
        f'workstation_url = "{ws_url}"\n' + "agent_mcp_store = false\n"
    )
    yield dst


@contextlib.asynccontextmanager
async def _running_workstation(
    tmp_path: Path, *, with_callback_tester: bool = False
) -> AsyncGenerator[Path]:
    """Boot a :class:`WorkstationServer` + register the demo
    services and entity (same shape as ``run_demo_workstation.py``).
    Optionally also brings up the stage-6 ``callback-tester``
    service. Yields the prepared agent workdir."""
    async with WorkstationServer() as ws:
        async with connect_peer_rpc(ws.rpc_url) as peer:
            echo_ref = await peer.scope.mount(DelayedEchoImpl())
            note_ref = Note(
                title="demo-note",
                body="Registered by test_mcp_in_memory.",
            ).store(peer.scope)

            @contextlib.asynccontextmanager
            async def _maybe_callback_tester() -> AsyncGenerator[None]:
                if not with_callback_tester:
                    yield
                    return
                ready = anyio.Event()
                async with _callback_tester_worker(peer, ready):
                    await ready.wait()
                    yield

            async with _maybe_callback_tester():
                async with (
                    expose_service(peer.remote_service, DELAYED_ECHO_NAME, echo_ref, peer.scope),
                    expose_entity(
                        peer.remote_service,
                        DEMO_NOTE_NAME,
                        note_ref.entity_id,
                        peer.scope,
                    ),
                ):
                    async with _demo_workdir(ws.bound_url, tmp_path) as workdir:
                        yield workdir


@contextlib.asynccontextmanager
async def _initialized_mcp(workdir: Path) -> AsyncGenerator[_MemoryClient]:
    """Run the package's FastMCP server in-process over a pair of
    in-memory ``SessionMessage`` streams, drive the initialize
    handshake, point the server at ``workdir`` + connect +
    open_session. Yields the raw client positioned for tool calls.

    The server task + the client reader loop both run on the same
    task group; on exit, cancelling that group tears down both
    cleanly (no subprocess to reap, so no SIGTERM dance).
    """
    # Late import so importing the test module doesn't construct
    # the global FastMCP instance unnecessarily.
    from fxtr_workstation_mcp.server import mcp as fxtr_mcp_app

    async with create_client_server_memory_streams() as (
        client_streams,
        server_streams,
    ):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        async with anyio.create_task_group() as tg:
            server_obj = fxtr_mcp_app._mcp_server  # noqa: SLF001
            init_opts = server_obj.create_initialization_options()

            async def _run_server() -> None:
                await server_obj.run(server_read, server_write, init_opts)

            tg.start_soon(_run_server)

            client = _MemoryClient(client_read, client_write)
            tg.start_soon(client.reader_loop)
            try:
                await client.initialize()
                assert "_ERROR_" not in (
                    text := await client.call_tool("set_working_dir", {"path": str(workdir)})
                ), text
                assert "Error" not in (text := await client.call_tool("connect")), text
                assert "Error" not in (text := await client.call_tool("open_session")), text
                yield client
            finally:
                tg.cancel_scope.cancel()


# --- Scenario tests ---------------------------------------------------


async def _run_scenario_pair(
    client: _MemoryClient,
    *,
    schedule_script: str,
    slot_key: str,
    retrieve_script: str,
    notification_timeout: float = 15.0,
) -> None:
    """Helper: run a ``schedule_*`` script, wait for the slot's
    channel notification, then run a ``retrieve_*`` script. Each
    script asserts sandbox-side; the helper just confirms no
    error propagated through the tool result."""
    text = await client.call_tool("run_script", {"script": schedule_script})
    assert "Error" not in text, text

    # The notification may have already landed inside ``text`` (if
    # the invoke completed during the tool's trail-delay window) or
    # may arrive shortly after as a debounced channel frame.
    # ``wait_for_slot_event`` looks in both places.
    ev = await client.wait_for_slot_event(
        slot_key,
        kind="invoke_result",
        timeout=notification_timeout,
    )
    assert ev["_content"], ev

    text = await client.call_tool("run_script", {"script": retrieve_script})
    assert "Error" not in text, text


@pytest.mark.slow
@pytest.mark.deadline(seconds=300)
async def test_in_memory_e2e_all_scenarios(tmp_path: Path) -> None:
    """Drive every scenario in ``demo_pkg.scenarios`` against the
    in-memory MCP server.

    Runs everything in a single session so the heavy parts
    (sandbox bootstrap, workstation startup) are paid once.

    Covers, in order:

    1. ``basic_echo``: schedule_invoke + channel notification +
       retrieve_result on a scalar.
    2. ``entity_arg_echo``: same, but with a freshly-stored
       ``Note`` passed as an entity arg + loaded back from the
       result.
    3. ``fetch_demo_note``: name-based entity lookup of the
       stable ``demo-note`` registration (no scheduling).
    """
    with anyio.fail_after(90.0):
        async with _running_workstation(tmp_path) as workdir:
            async with _initialized_mcp(workdir) as client:
                # 1. Basic echo: scalar round-trip.
                await _run_scenario_pair(
                    client,
                    schedule_script="await scenarios.schedule_basic_echo()",
                    slot_key="basic_echo",
                    retrieve_script=(
                        "result = await scenarios.retrieve_basic_echo()\n"
                        "assert result == 'hello', f'got {result!r}'\n"
                    ),
                )

                # 2. Entity arg echo: pass a fresh Note in, load
                #    it back, verify the fields survived.
                await _run_scenario_pair(
                    client,
                    schedule_script=(
                        "await scenarios.schedule_entity_arg_echo(\n"
                        "    title='passed-in', body='round-trip'\n"
                        ")\n"
                    ),
                    slot_key="entity_arg_echo",
                    retrieve_script=(
                        "note = await scenarios.retrieve_entity_arg_echo()\n"
                        "assert note.title == 'passed-in', note\n"
                        "assert note.body == 'round-trip', note\n"
                    ),
                )

                # 3. Fetch demo-note: pure lookup_entity_by_name
                #    path, no scheduling. Mirrors the body the
                #    in-process workstation registered above in
                #    ``_running_workstation``.
                text = await client.call_tool(
                    "run_script",
                    {
                        "script": (
                            "note = await scenarios.fetch_demo_note()\n"
                            "assert note.title == 'demo-note', note\n"
                            "assert 'test_mcp_in_memory' in note.body, note\n"
                        ),
                    },
                )
                assert "Error" not in text, text


@pytest.mark.slow
@pytest.mark.deadline(seconds=300)
async def test_in_memory_e2e_callback_scenarios(tmp_path: Path) -> None:
    """Drive the stage-6 cross-script callback flow against the
    in-memory MCP server + the demo's ``callback-tester`` service.

    Sequence:

    1. ``scenarios.cross_script_register_callback`` opens the
       callback in script A.
    2. ``scenarios.cross_script_fire_callback`` in script B
       schedules two invokes — ``register_callback`` then
       ``fire_notify``. These run on separate workstation
       invoke-worker tasks, so the demo service's stash
       ordering is best-effort across the two; we wait
       generously for the resulting callback channel frame.
    3. ``scenarios.retrieve_one_callback_notification`` in
       script C reads the stored payload through the
       ``callback_notifications`` field and verifies it.

    The unit-level ``test_callback_e2e.py`` covers the
    same-script case via a combined
    ``register_callback_and_fire_notify`` method; the demo's
    two-method API is exercised in cross-script form here
    where retries (if needed) are clearer to surface.
    """
    with anyio.fail_after(90.0):
        async with _running_workstation(tmp_path, with_callback_tester=True) as workdir:
            async with _initialized_mcp(workdir) as client:
                # Script A: open the callback.
                text = await client.call_tool(
                    "run_script",
                    {"script": "await scenarios.cross_script_register_callback()\n"},
                )
                assert "Error" not in text, text

                # Script B: register + fire via the retrieved stub.
                text = await client.call_tool(
                    "run_script",
                    {
                        "script": (
                            "await scenarios.cross_script_fire_callback(\n"
                            "    payload='cross-script', delay=0.1\n"
                            ")\n"
                        ),
                    },
                )
                assert "Error" not in text, text

                # Wait for the callback event — possibly inline in
                # one of the prior tool drains, possibly arriving as
                # a debounced channel frame.
                ev = await client.wait_for_slot_event(
                    "demo_callback",
                    kind="callback",
                    timeout=15.0,
                )
                assert "cross-script" in ev["_content"], ev
                assert ev.get("notif_index", "").isdigit(), ev

                # Script C: retrieve the stored payload via the
                # callback_notifications field. Verifies the full
                # store / retrieve loop end-to-end.
                text = await client.call_tool(
                    "run_script",
                    {
                        "script": (
                            "val = await scenarios.retrieve_one_callback_notification()\n"
                            "assert val == 'cross-script', f'got {val!r}'\n"
                        ),
                    },
                )
                assert "Error" not in text, text


# --- Nix git-ref spawn E2E --------------------------------------------


@contextlib.asynccontextmanager
async def _running_workstation_with_nix_spawner(
    tmp_path: Path,
) -> AsyncGenerator[tuple[Path, str]]:
    """Boot a workstation whose ``sandbox-spawner`` external service is a
    ``create_sandbox_environment`` configured with ``git_repo_path`` = a
    freshly-prepared agent-workdir repo (via ``prepare_workdir_repo``).

    Mirrors ``run_demo_workstation.py``'s spawner wiring: the env is opened
    *outside* the ``WorkstationServer`` CM (the ``external_services``
    contract pins ``host_scope`` for the server's lifetime). Yields the
    prepared workdir path + its committed HEAD (the rev the agent bakes into
    its ``NixWasmServiceDefn``).
    """
    cache_dir = tmp_path / "sandbox_cache"
    cache_dir.mkdir()
    prepared = await prepare_workdir_repo(_AGENT_WORKDIR, tmp_path / "agent_workdir")
    async with create_sandbox_environment(cache_dir=cache_dir, git_repo_path=prepared.path) as env:
        external_services = [
            ExternalServiceConfig(
                external_name=SANDBOX_SPAWNER_NAME,
                service=env.spawner_ref,
                scope=env.host_scope,
            ),
        ]
        async with WorkstationServer(external_services=external_services) as ws:
            # The toml is written post-commit (runtime config, not part of
            # the flake the spawner builds from `prepared.head_commit`).
            (prepared.path / ".fxtr_workstation.toml").write_text(
                f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
            )
            yield prepared.path, prepared.head_commit


@pytest.mark.slow
@pytest.mark.deadline(seconds=300)
async def test_in_memory_e2e_nix_spawn(tmp_path: Path) -> None:
    """Full agent-facing git-ref spawn through the MCP → workstation →
    spawner stack: the agent constructs a ``NixWasmServiceDefn`` in-sandbox,
    spawns ``fxtr.services.hello_service`` (the spawner builds it from the
    workdir's git ref), connects, and calls ``greet()``."""
    async with _running_workstation_with_nix_spawner(tmp_path) as (workdir, _head):
        async with _initialized_mcp(workdir) as client:
            # 1. Construct the defn in-sandbox + spawn. The agent self-serves
            #    the commit via current_commit() (clean committed tree, so it
            #    succeeds). The build offloads to the nix builder, so the
            #    "running" slot may take a while.
            spawn_script = (
                "from _fxtr_mcp_builtin import (\n"
                "    NixWasmServiceDefn,\n"
                "    current_commit,\n"
                "    lookup_service_by_name,\n"
                "    schedule_invoke,\n"
                ")\n"
                'spawner = await lookup_service_by_name("sandbox-spawner")\n'
                "defn = NixWasmServiceDefn(\n"
                "    git_commit=await current_commit(),\n"
                '    flake_attr="fxtr.services.hello_service",\n'
                ").store()\n"
                'await schedule_invoke("running", spawner, lambda s: s.spawn(defn))\n'
            )
            assert "Error" not in (
                text := await client.call_tool("run_script", {"script": spawn_script})
            ), text
            ev = await client.wait_for_slot_event("running", kind="invoke_result", timeout=240.0)
            assert ev["_content"], ev

            # 2. Connect with a HelloConfig to get the typed service.
            connect_script = (
                "from _fxtr_mcp_builtin import retrieve_result, schedule_invoke\n"
                "from hello_service import HelloConfig\n"
                'wrapper = await retrieve_result("running")\n'
                'cfg = HelloConfig(name="world").store()\n'
                'await schedule_invoke("hello_svc", wrapper, lambda s: s.connect(cfg))\n'
            )
            assert "Error" not in (
                text := await client.call_tool("run_script", {"script": connect_script})
            ), text
            ev = await client.wait_for_slot_event("hello_svc", kind="invoke_result", timeout=60.0)
            assert ev["_content"], ev

            # 3. Call greet() — assert the service responded.
            greet_script = (
                "from _fxtr_mcp_builtin import retrieve_result, schedule_invoke\n"
                'svc = await retrieve_result("hello_svc")\n'
                'await schedule_invoke("greeting", svc, lambda s: s.greet())\n'
            )
            assert "Error" not in (
                text := await client.call_tool("run_script", {"script": greet_script})
            ), text
            ev = await client.wait_for_slot_event("greeting", kind="invoke_result", timeout=60.0)
            assert "Hello, world!" in ev["_content"], ev


@contextlib.asynccontextmanager
async def _running_workstation_nixed(
    tmp_path: Path,
) -> AsyncGenerator[tuple[Path, str]]:
    """Boot a bare ``WorkstationServer`` + a freshly-prepared (committed
    git) agent-workdir repo whose flake declares ``fxtr.entities.demo_data``.

    No spawner: the data-path builtins exercised here
    (``retrieve_nix_entity`` / ``current_commit``) are MCP-host-side and
    don't go through a workstation service. Yields the workdir path + its
    committed HEAD.
    """
    prepared = await prepare_workdir_repo(_AGENT_WORKDIR, tmp_path / "agent_workdir")
    async with WorkstationServer() as ws:
        # toml is runtime config, written post-commit (not part of the flake).
        (prepared.path / ".fxtr_workstation.toml").write_text(
            f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
        )
        yield prepared.path, prepared.head_commit


@pytest.mark.slow
@pytest.mark.deadline(seconds=300)
async def test_in_memory_e2e_retrieve_nix_entity(tmp_path: Path) -> None:
    """The DATA-path builtins end-to-end through the MCP: build + retrieve a
    nix entity, read HEAD via ``current_commit``, and the failure / dirty-tree
    / untracked-notice surfaces."""
    async with _running_workstation_nixed(tmp_path) as (workdir, head):
        async with _initialized_mcp(workdir) as client:
            # 1. Build + retrieve fxtr.entities.demo_data, load it, read data.
            retrieve_script = (
                "from _fxtr_mcp_builtin import retrieve_nix_entity\n"
                "from demo_data import DemoDataEntity\n"
                "from fxtr2.data.entity_ref_scope import EntityRef\n"
                "fid = await retrieve_nix_entity('demo_data')\n"
                "ent = await EntityRef(fid, DemoDataEntity).load()\n"
                "print('RETRIEVED_TITLE:', ent.title)\n"
            )
            text = await client.call_tool("run_script", {"script": retrieve_script})
            assert "Error" not in text, text
            assert "RETRIEVED_TITLE: demo data" in text, text

            # 1b. A builtin entity resolves to its primitive FxtrID with no
            #     build (the id_string IS the entity).
            builtin_script = (
                "from _fxtr_mcp_builtin import retrieve_nix_entity\n"
                "fid = await retrieve_nix_entity('basic_type')\n"
                "print('BUILTIN_ID:', fid.id_string())\n"
            )
            text = await client.call_tool("run_script", {"script": builtin_script})
            assert "Error" not in text, text
            assert "BUILTIN_ID: !basic_type" in text, text

            # 2. current_commit() on the clean committed tree returns HEAD.
            cc_script = (
                "from _fxtr_mcp_builtin import current_commit\n"
                "print('COMMIT:', await current_commit())\n"
            )
            text = await client.call_tool("run_script", {"script": cc_script})
            assert "Error" not in text, text
            assert f"COMMIT: {head}" in text, text

            # 3. Retrieving a missing / non-entity attr raises NixEntityBuildFailed.
            bad_script = (
                "from _fxtr_mcp_builtin import retrieve_nix_entity, NixEntityBuildFailed\n"
                "try:\n"
                "    await retrieve_nix_entity('does_not_exist')\n"
                "    print('NO_ERROR')\n"
                "except NixEntityBuildFailed:\n"
                "    print('BUILD_FAILED')\n"
            )
            text = await client.call_tool("run_script", {"script": bad_script})
            assert "BUILD_FAILED" in text, text

            # 4. An untracked file is invisible to nix; retrieve still works and
            #    emits the count-only notice.
            (workdir / "scratch_untracked.txt").write_text("not tracked by git\n")
            text = await client.call_tool("run_script", {"script": retrieve_script})
            assert "Error" not in text, text
            assert "untracked file(s)" in text, text

            # 5. current_commit() now refuses (dirty tree — the untracked file).
            dirty_script = (
                "from _fxtr_mcp_builtin import current_commit, WorkingTreeDirty\n"
                "try:\n"
                "    await current_commit()\n"
                "    print('NO_ERROR')\n"
                "except WorkingTreeDirty:\n"
                "    print('DIRTY')\n"
            )
            text = await client.call_tool("run_script", {"script": dirty_script})
            assert "DIRTY" in text, text


# --- Nix git-ref set_preset E2E ---------------------------------------


@contextlib.asynccontextmanager
async def _running_workstation_with_nix_dispatcher(
    tmp_path: Path,
) -> AsyncGenerator[tuple[Path, str, Any]]:
    """Boot a workstation whose ``renderer-dispatcher`` external service is a
    :class:`NixConfigurableDispatchImpl` configured against a freshly-prepared
    agent-workdir repo — and **no** initial preset, so the agent's
    ``set_preset`` is what fills the table.

    Mirrors ``_running_workstation_with_nix_spawner``: the mount CM is opened
    *outside* the ``WorkstationServer`` CM (the ``external_services`` contract
    pins the dispatcher's scope for the server's lifetime). Yields the
    prepared workdir path, its committed HEAD (the rev the agent pins via
    ``current_commit()``), and the mounted dispatch handle (so the test can
    assert on ``impl.current_ref`` + resolve through the impl's scope — the
    agent side of ``NixDispatcherProto`` is ``set_preset``-only).
    """
    prepared = await prepare_workdir_repo(_AGENT_WORKDIR, tmp_path / "agent_workdir")
    async with mount_nix_configurable_dispatch(repo=prepared.path) as mounted:
        external_services = [
            ExternalServiceConfig(
                external_name=RENDERER_DISPATCHER_NAME,
                service=mounted.service_ref,
                scope=mounted.scope,
            ),
        ]
        async with WorkstationServer(external_services=external_services) as ws:
            # toml is runtime config, written post-commit (not part of the flake).
            (prepared.path / ".fxtr_workstation.toml").write_text(
                f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
            )
            yield prepared.path, prepared.head_commit, mounted


@pytest.mark.slow
@pytest.mark.deadline(seconds=900)
async def test_in_memory_e2e_nix_set_preset(tmp_path: Path) -> None:
    """Full agent-facing ``set_preset`` through the MCP → RPC → workstation
    invoke stack (the analog of ``test_in_memory_e2e_nix_spawn`` for the
    dispatcher; Stage 5's direct test drove ``set_preset`` through an
    in-process proxy, not the agent wire path).

    The agent looks the dispatcher up by name, re-types it to
    ``NixDispatcherProto``, and ``set_preset(current_commit(),
    demo_preset)``; the workstation impl eval's the table, builds the real
    ``esbuildRenderer`` ESM + ``mkEntityFromPython`` typeEntity layer,
    validates the key, mints the ``RendererEntity``, swaps the table, and
    broadcasts a reload. We assert on the impl handle (set after the table
    swaps) + resolve the demo_data type to real ESM bytes.

    Slow + nix-gated: the ``demo_data`` host venv (full fxtr2 via uv2nix)
    dominates a cold run — hence the generous deadline.
    """
    async with _running_workstation_with_nix_dispatcher(tmp_path) as (workdir, head, mounted):
        async with _initialized_mcp(workdir) as client:
            # 1. Push the preset via the real agent path. current_commit() is
            #    pre-awaited (it's async, so it can't be called inside the
            #    synchronously-recorded schedule_invoke lambda).
            set_preset_script = (
                "from _fxtr_mcp_builtin import (\n"
                "    NixDispatcherProto,\n"
                "    current_commit,\n"
                "    lookup_service_by_name,\n"
                "    schedule_invoke,\n"
                ")\n"
                "commit = await current_commit()\n"
                'dispatcher = (await lookup_service_by_name("renderer-dispatcher"))'
                ".replace_service_type(NixDispatcherProto)\n"
                'await schedule_invoke("p", dispatcher, lambda d: d.set_preset(\n'
                '    commit, "fxtr.uiPresets.demo_preset"))\n'
            )
            assert "Error" not in (
                text := await client.call_tool("run_script", {"script": set_preset_script})
            ), text
            # set_preset returns None on success → the slot promotes to
            # invoke_result (not invoke_error). The build offloads to nix, so
            # allow a long window.
            ev = await client.wait_for_slot_event("p", kind="invoke_result", timeout=840.0)
            assert ev is not None, ev

            # 2. current_ref is set only after eval + build + validate + table
            #    swap all succeed — a strong signal the full path completed.
            assert mounted.impl.current_ref == (
                head,
                "fxtr.uiPresets.demo_preset",
                None,
            )

            # 3. Resolve the demo_data type for FullPanel through a proxy into
            #    the impl's scope (the frontend's wire path): a real
            #    RendererEntity built from the esbuildRenderer ESM, loadable to
            #    real ESM bytes. Mirrors test_nix_dispatch_preset.py.
            # mounted.scope transitively includes GLOBAL_SCOPE (the mount
            # helper's default parent), so this reaches both the dispatcher's
            # minted entities and the global type registry.
            async with FxtrScope(isolated=True, include=[mounted.scope], name="test") as scope:
                # Store an instance so the type-descriptor closure is tracked in
                # `scope`, making the type ref wire-encodable as a resolve arg.
                DemoDataEntity(title="probe", body="probe").store(scope)
                type_ref = EntityRef(DemoDataEntity.fxtr_type, DemoDataEntity)
                dispatcher = await mounted.service_ref.resolve(scope)
                hit = await dispatcher.resolve(type_ref, _FULL_PANEL)
                assert hit is not None
                assert hit.subtype_key == "demo_data"
                loaded = await hit.renderer.load(scope)
                assert loaded.module  # non-empty
                assert b"export" in loaded.module  # ESM output
