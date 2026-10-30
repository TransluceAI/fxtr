"""End-to-end stage-6 integration tests for workstation-managed
callbacks.

A peer registers a :class:`_CallbackTesterImpl` service on the
workstation. The MCP runs scripts that:

1. ``create_callback("cb")`` to open a callback slot.
2. ``schedule_invoke(... register_callback(cb) ...)`` to stash
   the callback ref on the demo service (uses
   ``bridge_service_from_scope`` to ferry the ref into a
   long-lived impl scope).
3. ``schedule_invoke(... fire_notify(payload, delay) ...)`` to
   fire-and-forget a delayed callback ``notify`` from a
   background task on the demo service.

Verifies:

- ``CallbackEvent`` arrives on the wire and lands in the local
  mirror's ``callback_notifications`` field.
- ``notifications/claude/channel`` frame fires with
  ``kind="callback"`` and a numeric ``notif_index`` attr.
- ``retrieve_callback_notification(slot_key, idx)`` returns the
  decoded payload.
- ``forget(slot_key)`` for a ``callback_open`` slot auto-emits
  ``CloseCallbackOp``; subsequent ``notify`` calls silently no-op
  and produce no further ``CallbackEvent``.
- ``forget_callback_notification(slot_key, idx)`` drops one
  stored payload.

Heavy (slow) — factory bootstrap + workstation server boot.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, Protocol

import anyio
import anyio.abc
import pytest
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef, get_active_scope
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.subscription_types import EventSource, Subscriber, UnsubscribeHandle
from fxtr2.services.subscription_util import SubscriberHub
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method
from fxtr_workstation import WorkstationServer, expose_service
from fxtr_workstation.protocols import WorkstationSessionEventInbox

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

from ._events import ParsedEvent, parse_events_from_sources
from ._peer import connect_peer_rpc

pytestmark = pytest.mark.anyio


# --- Callback-tester service (peer-registered) ----------------------

_CB_NS = uuid.UUID("11223344-5566-7788-99aa-bbccddeeff00")
_CB_TYPE_ID = system_entities.define_type(_CB_NS, "stage6_callback_tester")


@service_protocol
class _CallbackTesterProto(Protocol):
    async def register_callback_and_fire_notify(
        self, cb: ServiceRef[WorkstationSessionEventInbox], payload: Any, delay: float
    ) -> None: ...


class _CallbackTesterImpl(TypedServiceImpl[_CallbackTesterProto]):
    """Test-local callback driver.

    Single combined method: bridge ``cb`` into the impl-owned
    scope (idempotent across calls — multiple invocations of the
    same callback ref skip the duplicate-bridge error) and spawn
    a delayed-notify background task. Combining register + fire
    into one call sidesteps the same-mutation invoke-ordering
    race: the workstation's invoke_worker spawns each
    :class:`InvokeOp` as a concurrent task, so two separate
    ``register_callback`` + ``fire_notify`` invokes in one
    mutation aren't guaranteed to run in order.
    """

    def __init__(
        self,
        impl_scope: FxtrScope,
        task_group: anyio.abc.TaskGroup,
    ) -> None:
        self._impl_scope = impl_scope
        self._task_group = task_group
        self._bridged_keys: set[tuple[Any, FxtrID]] = set()

    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return _CallbackTesterProto

    def _service_type_id(self) -> FxtrID:
        return _CB_TYPE_ID

    @service_method
    async def register_callback_and_fire_notify(
        self, cb: ServiceRef[WorkstationSessionEventInbox], payload: Any, delay: float
    ) -> None:
        key = (cb.handle, cb.service_type_id)
        if key not in self._bridged_keys:
            call_scope = get_active_scope()
            await self._impl_scope.bridge_service_from_scope(
                cb, call_scope, keepalive_via="independent_lease"
            )
            self._bridged_keys.add(key)
        # The notify task runs *after* this dispatch returns, using the mount
        # scope from a background task. Base-floor authorization (design §2)
        # makes that work: while ``_impl_scope`` is open its base authorizes any
        # context, so the spawned task needs no re-authorization.
        self._task_group.start_soon(
            _delayed_notify,
            cb,
            self._impl_scope,
            payload,
            delay,
        )


async def _delayed_notify(
    cb: ServiceRef[WorkstationSessionEventInbox],
    impl_scope: FxtrScope,
    payload: Any,
    delay: float,
) -> None:
    try:
        if delay > 0:
            await anyio.sleep(delay)
        proxy = await cb.resolve(impl_scope)
        await proxy.notify(payload)
    except Exception:
        # Closed-callback path: notify may raise after CloseCallbackOp
        # committed if the bridge is no longer reachable. Swallow.
        pass


@contextlib.asynccontextmanager
async def _callback_tester_running(peer: Any, external_name: str) -> AsyncGenerator[str]:
    """Expose a fresh :class:`_CallbackTesterImpl` under
    ``external_name`` for the duration of the CM. Yields the
    workstation-allocated service short_id.

    Owns an impl_scope (child of ``peer.scope``) and a task group
    where ``fire_notify`` spawns delayed-notify children. Drains
    in nested order: cancel task group → release expose_service →
    drain impl_scope; matches the shutdown ordering the demo
    workstation uses.
    """
    async with anyio.create_task_group() as task_group:
        async with FxtrScope(
            isolated=True,
            include=[peer.scope],
            name="callback_tester_impl_test",
        ) as impl_scope:
            impl_ref = await impl_scope.mount(_CallbackTesterImpl(impl_scope, task_group))
            async with expose_service(
                peer.remote_service,
                external_name,
                impl_ref,
                impl_scope,
            ) as svc_short_id:
                yield svc_short_id


# --- Channel-capturing fake MCP context ------------------------------


class _ChannelFrame:
    """One captured ``notifications/claude/channel`` frame."""

    __slots__ = ("content", "attrs")

    def __init__(self, content: str, attrs: dict[str, str]) -> None:
        self.content = content
        self.attrs = attrs


class _FakeWriteStream:
    """Captures notifications/claude/channel frames for assertion."""

    def __init__(self) -> None:
        self.frames: list[_ChannelFrame] = []

    async def send(self, message: Any) -> None:
        # The MCP sends a SessionMessage(message=JSONRPCMessage(notification)).
        # JSONRPCMessage wraps the actual notification; the channel.py
        # helper produces params={"content": ..., "meta": {...}}.
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
    """Test-local FastMCP context shim.

    Captures every tool wrapper's return value in
    :attr:`tool_outputs` so event-stream helpers can inspect both
    channel frames (events that landed while no tool was active)
    and inline tool drains (events that landed during a tool's
    :meth:`OutgoingBuffer.tool_window`)."""

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


# --- Helpers --------------------------------------------------------


def _all_events(ctx: _FakeContext) -> list[ParsedEvent]:
    """Aggregate every ``<event …>`` block the agent could have
    seen so far — across channel frames and tool outputs."""
    return parse_events_from_sources(
        channel_frame_contents=[f.content for f in ctx.session._write_stream.frames],
        tool_outputs=ctx.tool_outputs,
    )


async def _wait_for_callback_event(
    ctx: _FakeContext, slot_key: str, timeout_s: float = 2.0
) -> ParsedEvent:
    """Poll all event sources (channel frames + tool outputs)
    until one with ``kind="callback"`` and ``slot_key`` is
    present, or raise."""
    deadline = anyio.current_time() + timeout_s
    while anyio.current_time() < deadline:
        for ev in _all_events(ctx):
            if ev.attrs.get("kind") == "callback" and ev.attrs.get("slot_key") == slot_key:
                return ev
        await anyio.sleep(0.05)
    raise AssertionError(
        f"timed out waiting for callback event for slot_key={slot_key!r}; "
        f"saw events: {[(e.attrs, e.content) for e in _all_events(ctx)]}"
    )


def _callback_events_for(ctx: _FakeContext, slot_key: str) -> list[ParsedEvent]:
    return [
        e
        for e in _all_events(ctx)
        if e.attrs.get("kind") == "callback" and e.attrs.get("slot_key") == slot_key
    ]


# --- The tests ------------------------------------------------------


@pytest.mark.slow
async def test_same_script_callback_create_and_fire(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """Same-script: ``create_callback`` + ``schedule_invoke``s in one
    mutation. The channel frame for the callback arrives after the
    fire_notify invoke commits + the delayed-notify task fires."""
    async with WorkstationServer() as ws:
        async with connect_peer_rpc(ws.rpc_url) as peer:
            async with _callback_tester_running(peer, "test-cb-tester") as svc_short_id:
                (tmp_path / CONFIG_FILENAME).write_text(
                    f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
                )
                (tmp_path / "preamble.py").write_text("# stage-6 e2e\n")
                async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
                    ctx = _FakeContext(app_ctx)
                    await _swd(ctx, str(tmp_path))
                    await _conn(ctx)
                    await _open(ctx, {"phase": "stage-6-same-script"})

                    script = (
                        "from _fxtr_mcp_builtin import create_callback, get_service,"
                        f" schedule_invoke\nsvc = get_service({svc_short_id!r})\ncb ="
                        ' create_callback("cb")\nawait schedule_invoke("fire", svc, lambda s:'
                        ' s.register_callback_and_fire_notify(cb, "hello", 0.1))\n'
                    )
                    r = await _run(ctx, script)
                    assert "Error" not in r, r

                    # Wait for the callback frame.
                    frame = await _wait_for_callback_event(ctx, "cb")
                    assert frame.attrs["kind"] == "callback"
                    assert frame.attrs["slot_key"] == "cb"
                    notif_idx_str = frame.attrs["notif_index"]
                    assert notif_idx_str.isdigit(), f"non-int notif_index: {notif_idx_str!r}"
                    # Payload summary contains the sent string.
                    assert "hello" in frame.content

                    # Retrieve the stored payload.
                    retrieve_script = (
                        "from _fxtr_mcp_builtin import "
                        "callback_notification_keys, retrieve_callback_notification\n"
                        'keys = await callback_notification_keys("cb")\n'
                        'assert keys, f"no keys: {keys!r}"\n'
                        'val = await retrieve_callback_notification("cb", keys[-1])\n'
                        'assert val == "hello", f"got {val!r}"\n'
                    )
                    r2 = await _run(ctx, retrieve_script)
                    assert "Error" not in r2, r2


@pytest.mark.slow
async def test_cross_script_callback(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """Cross-script: ``create_callback`` in script 1 commits the
    callback slot; script 2 retrieves the stub via
    ``retrieve_result`` and uses it as an invoke arg."""
    async with WorkstationServer() as ws:
        async with connect_peer_rpc(ws.rpc_url) as peer:
            async with _callback_tester_running(peer, "test-cb-tester-2") as svc_short_id:
                (tmp_path / CONFIG_FILENAME).write_text(
                    f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
                )
                (tmp_path / "preamble.py").write_text("# stage-6 cross-script\n")
                async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
                    ctx = _FakeContext(app_ctx)
                    await _swd(ctx, str(tmp_path))
                    await _conn(ctx)
                    await _open(ctx, {"phase": "stage-6-cross-script"})

                    # Script 1: just create the callback.
                    r1 = await _run(
                        ctx,
                        'from _fxtr_mcp_builtin import create_callback\ncreate_callback("cb")\n',
                    )
                    assert "Error" not in r1, r1

                    # Script 2: retrieve the stub, then register + fire
                    # via the combined method.
                    script2 = (
                        "from _fxtr_mcp_builtin import get_service, retrieve_result,"
                        f" schedule_invoke\nsvc = get_service({svc_short_id!r})\ncb = await"
                        ' retrieve_result("cb")\nawait schedule_invoke("fire", svc, lambda s:'
                        ' s.register_callback_and_fire_notify(cb, "world", 0.1))\n'
                    )
                    r2 = await _run(ctx, script2)
                    assert "Error" not in r2, r2

                    frame = await _wait_for_callback_event(ctx, "cb")
                    assert "world" in frame.content


@pytest.mark.slow
async def test_forget_callback_stops_future_notifications(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """After ``forget(slot_key)`` commits, the workstation's
    ``WorkstationInboxImpl._closed`` flag flips, and the
    delayed-notify task (still in flight) gets a silent no-op from
    its ``notify`` call — no :class:`CallbackEvent` is emitted, so
    no further channel frame for that slot arrives.

    Sequence:
    1. Create callback at "cb", register, fire_notify with
       delay=0.05 (arrives quickly).
    2. Verify a callback frame arrives.
    3. fire_notify with delay=1.5 (long).
    4. forget("cb") — commits CloseCallbackOp immediately.
    5. Wait past 1.5s; verify no second channel frame arrives.
    """
    async with WorkstationServer() as ws:
        async with connect_peer_rpc(ws.rpc_url) as peer:
            async with _callback_tester_running(peer, "test-cb-tester-3") as svc_short_id:
                (tmp_path / CONFIG_FILENAME).write_text(
                    f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
                )
                (tmp_path / "preamble.py").write_text("# stage-6 forget\n")
                async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
                    ctx = _FakeContext(app_ctx)
                    await _swd(ctx, str(tmp_path))
                    await _conn(ctx)
                    await _open(ctx, {"phase": "stage-6-forget"})

                    # Step 1: create + (register + fast notify).
                    r1 = await _run(
                        ctx,
                        "from _fxtr_mcp_builtin import create_callback, get_service,"
                        f" schedule_invoke\nsvc = get_service({svc_short_id!r})\ncb ="
                        ' create_callback("cb")\nawait schedule_invoke("fire1", svc, lambda s:'
                        ' s.register_callback_and_fire_notify(cb, "first", 0.05))\n',
                    )
                    assert "Error" not in r1, r1
                    first = await _wait_for_callback_event(ctx, "cb")
                    assert "first" in first.content

                    # Step 3: schedule a slow notify (1.5s delay).
                    # The agent reuses the stub via retrieve_result so
                    # the impl's same-cb-key bridge is a no-op the
                    # second time around.
                    r2 = await _run(
                        ctx,
                        "from _fxtr_mcp_builtin import get_service, retrieve_result,"
                        f" schedule_invoke\nsvc = get_service({svc_short_id!r})\ncb = await"
                        ' retrieve_result("cb")\nawait schedule_invoke("fire2", svc, lambda s:'
                        ' s.register_callback_and_fire_notify(cb, "second", 1.5))\n',
                    )
                    assert "Error" not in r2, r2

                    # Step 4: forget the callback. This commits
                    # CloseCallbackOp immediately, flipping
                    # WorkstationCallbackImpl._closed before the
                    # delayed-notify task wakes up.
                    r3 = await _run(ctx, 'from _fxtr_mcp_builtin import forget\nforget("cb")\n')
                    assert "Error" not in r3, r3

                    # Step 5: wait past the slow-notify delay.
                    await anyio.sleep(1.8)

                    # Only the first frame should have arrived for
                    # slot "cb". (The "second" payload never lands
                    # because notify silently no-ops once closed.)
                    cb_events = _callback_events_for(ctx, "cb")
                    assert len(cb_events) == 1, (
                        "expected exactly 1 callback event for 'cb' after "
                        f"forget; got {len(cb_events)}: "
                        f"{[(e.attrs, e.content) for e in cb_events]}"
                    )


@pytest.mark.slow
async def test_forget_callback_notification(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """``forget_callback_notification(slot_key, idx)`` drops the
    stored payload; subsequent ``retrieve_callback_notification``
    raises and ``callback_notification_keys`` no longer
    includes the index."""
    async with WorkstationServer() as ws:
        async with connect_peer_rpc(ws.rpc_url) as peer:
            async with _callback_tester_running(peer, "test-cb-tester-4") as svc_short_id:
                (tmp_path / CONFIG_FILENAME).write_text(
                    f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
                )
                (tmp_path / "preamble.py").write_text("# stage-6 forget-notif\n")
                async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
                    ctx = _FakeContext(app_ctx)
                    await _swd(ctx, str(tmp_path))
                    await _conn(ctx)
                    await _open(ctx, {"phase": "stage-6-forget-notif"})

                    # Create + (register + fire) one notification.
                    r1 = await _run(
                        ctx,
                        "from _fxtr_mcp_builtin import create_callback, get_service,"
                        f" schedule_invoke\nsvc = get_service({svc_short_id!r})\ncb ="
                        ' create_callback("cb")\nawait schedule_invoke("fire", svc, lambda s:'
                        ' s.register_callback_and_fire_notify(cb, "payload", 0.05))\n',
                    )
                    assert "Error" not in r1, r1
                    frame = await _wait_for_callback_event(ctx, "cb")
                    notif_idx = int(frame.attrs["notif_index"])

                    # Drop the notification.
                    r2 = await _run(
                        ctx,
                        "from _fxtr_mcp_builtin import "
                        "callback_notification_keys, forget_callback_notification, "
                        "retrieve_callback_notification\n"
                        f"forget_callback_notification('cb', {notif_idx})\n",
                    )
                    assert "Error" not in r2, r2

                    # Verify it's gone.
                    r3 = await _run(
                        ctx,
                        "from _fxtr_mcp_builtin import "
                        "callback_notification_keys, retrieve_callback_notification\n"
                        'keys = await callback_notification_keys("cb")\n'
                        f"assert {notif_idx} not in keys, f'still present: {{keys!r}}'\n"
                        "try:\n"
                        f"    await retrieve_callback_notification('cb', {notif_idx})\n"
                        "except KeyError:\n"
                        "    pass\n"
                        "else:\n"
                        "    raise AssertionError('expected KeyError')\n",
                    )
                    assert "Error" not in r3, r3


# --- subscribe_to via callback_invoke (ws 6.1 chunk 4) ----------------

_FEED_NS = uuid.UUID("22334455-6677-8899-aabb-ccddeeff0011")
_FEED_TYPE_ID = system_entities.define_type(_FEED_NS, "stage6_feed_source")


@service_protocol
class _FeedSourceProto(EventSource[FxtrServiceData], Protocol):
    """Test event source — a genuine ``EventSource`` subclass;
    ``subscribe`` is inherited."""


class _FeedSourceImpl(TypedServiceImpl[_FeedSourceProto]):
    """Hub-backed event source the test publishes through."""

    def __init__(self, hub: SubscriberHub[FxtrServiceData]) -> None:
        self.hub = hub

    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return _FeedSourceProto

    def _service_type_id(self) -> FxtrID:
        return _FEED_TYPE_ID

    @service_method
    async def subscribe(
        self, subscriber: ServiceRef[Subscriber[FxtrServiceData]]
    ) -> ServiceRef[UnsubscribeHandle]:
        return await self.hub.subscribe(subscriber, get_active_scope())


@contextlib.asynccontextmanager
async def _feed_source_running(
    peer: Any, external_name: str
) -> AsyncGenerator[tuple[str, SubscriberHub[FxtrServiceData]]]:
    """Expose a fresh :class:`_FeedSourceImpl`; yield
    ``(short_id, hub)`` so the test can publish directly."""
    async with FxtrScope(
        isolated=True,
        include=[peer.scope],
        name="feed_source_impl_test",
    ) as impl_scope:
        async with anyio.create_task_group() as task_group:
            try:
                hub = SubscriberHub[FxtrServiceData](task_group=task_group)
                impl_ref = await impl_scope.mount(_FeedSourceImpl(hub))
                async with expose_service(
                    peer.remote_service,
                    external_name,
                    impl_ref,
                    impl_scope,
                ) as svc_short_id:
                    yield svc_short_id, hub
            finally:
                task_group.cancel_scope.cancel()


@pytest.mark.slow
async def test_same_script_create_and_subscribe_to(
    tmp_path: Path, sandbox_env_supervisor: SandboxEnvSupervisor
) -> None:
    """The agent creates an inbox and ``subscribe_to``s an event
    source **in one script** — ``schedule_invoke`` targeting the
    owned-callback stub rides a ``CallbackInvokeOp`` (same-mutation
    create+subscribe). Events the source publishes afterwards land
    as ``CallbackEvent``s on the inbox's slot and surface as
    ``kind="callback"`` channel frames."""
    async with WorkstationServer() as ws:
        async with connect_peer_rpc(ws.rpc_url) as peer:
            async with _feed_source_running(peer, "test-feed-source") as (
                src_short_id,
                hub,
            ):
                (tmp_path / CONFIG_FILENAME).write_text(
                    f'workstation_url = "{ws.bound_url}"\n' + "agent_mcp_store = false\n"
                )
                (tmp_path / "preamble.py").write_text("# stage-6 subscribe_to\n")
                async with make_app_ctx(sandbox_env=sandbox_env_supervisor) as app_ctx:
                    ctx = _FakeContext(app_ctx)
                    await _swd(ctx, str(tmp_path))
                    await _conn(ctx)
                    await _open(ctx, {"phase": "stage-6-subscribe-to"})

                    script = (
                        "from _fxtr_mcp_builtin import create_callback, get_service,"
                        f" schedule_invoke\nsrc = get_service({src_short_id!r})\nfeed ="
                        ' create_callback("feed")\nawait schedule_invoke("sub", feed, lambda'
                        " i: i.subscribe_to(src))\n"
                    )
                    r = await _run(ctx, script)
                    assert "Error" not in r, r

                    # The subscription is established once the invoke
                    # commits + dispatches.
                    with anyio.fail_after(5.0):
                        while hub.live_subscription_count == 0:
                            await anyio.sleep(0.05)

                    hub.publish("feed-payload")
                    frame = await _wait_for_callback_event(ctx, "feed")
                    assert "feed-payload" in frame.content

                    # forget() closes the inbox → the workstation
                    # unsubscribes from the source.
                    r2 = await _run(ctx, 'from _fxtr_mcp_builtin import forget\nforget("feed")\n')
                    assert "Error" not in r2, r2
                    with anyio.fail_after(5.0):
                        while hub.live_subscription_count > 0:
                            await anyio.sleep(0.05)
