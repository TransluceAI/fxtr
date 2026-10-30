"""Tests for the stage-10c additions to
:mod:`fxtr_workstation.dynamic_dispatch.basic_dynamic_dispatch`:
the hub-backed subscription surface, ``trigger_reload`` /
``recompile_changed_and_reload``, and ``mount_basic_dynamic_dispatch``.

These cover the impl's hot-reload surface in isolation — no
workstation, no frontend. The end-to-end flow (browser session →
inbox ``subscribe_to`` invoke → reload notification → cache wipe)
is covered against a separate harness backend at the frontend
layer.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope, get_active_scope
from fxtr2.leases.leased_cap_ir import IRException
from fxtr2.services.service_dialect import FxtrServiceData
from fxtr2.services.subscription_types import Subscriber, UnsubscribeHandle
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method
from fxtr_workstation.dynamic_dispatch import (
    BasicDynamicDispatchImpl,
    WorkstationDispatcherProto,
    mount_basic_dynamic_dispatch,
)
from fxtr_workstation.dynamic_dispatch.basic_dynamic_dispatch.compile import npx_available

DEADLOCK_TIMEOUT = 30.0
FIXTURES_DIR = Path(__file__).parent / "_fixtures_basic_dynamic_dispatch"


needs_npx = pytest.mark.skipif(
    not npx_available(),
    reason="npx not on PATH — install Node.js to exercise esbuild compile path.",
)


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    with anyio.fail_after(timeout):
        while not predicate():
            await anyio.sleep(0.01)


# ---------- Recording subscriber impl --------------------------------


class _RecordingSubscriberImpl(TypedServiceImpl[Subscriber[FxtrServiceData]]):
    """Test-only :class:`Subscriber` impl that records every
    :meth:`on_event` call and signals a fresh :class:`anyio.Event`
    per notification so tests can ``await`` arrival without polling.

    Set ``raise_on_event=True`` to test the crash-backstop path
    (the dispatcher drops a subscription whose ``on_event`` raises).
    """

    def __init__(self, *, raise_on_event: bool = False) -> None:
        super().__init__()
        self.notifications: list[Any] = []
        # Fired + replaced on every on_event so tests can capture-
        # then-await without racing.
        self._signal = anyio.Event()
        self._raise_on_event = raise_on_event

    def _target_type_annotation(self) -> Any:
        return Subscriber[FxtrServiceData]

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("test-recording-subscriber"))

    @service_method
    async def on_event(self, payload: FxtrServiceData) -> None:
        if self._raise_on_event:
            raise RuntimeError("subscriber boom")
        self.notifications.append(payload)
        signal = self._signal
        self._signal = anyio.Event()
        signal.set()

    async def wait_for_count(self, target: int) -> None:
        """Wait until ``len(notifications) >= target``. Each new
        notification fires + replaces ``self._signal``; we capture the
        current event before each await so a notification landing
        between iterations isn't lost.
        """
        while len(self.notifications) < target:
            event = self._signal
            await event.wait()


# ---------- trigger_reload + subscription machinery -----------------


async def test_trigger_reload_no_subscribers_is_noop() -> None:
    """``trigger_reload`` with zero subscribers is a sync no-op — no
    errors, no deliveries."""
    async with FxtrScope(
        isolated=True,
        include=[GLOBAL_SCOPE],
        name="dispatch",
    ) as dispatch_scope:
        async with anyio.create_task_group() as impl_tg:
            impl = BasicDynamicDispatchImpl(
                scope=dispatch_scope,
                renderer_dirs=[],
                impl_tg=impl_tg,
            )
            await impl.compile_and_register()
            impl.trigger_reload()  # must not raise
            assert impl._hub.live_subscription_count == 0
            impl_tg.cancel_scope.cancel()


async def test_subscribe_and_trigger_delivers_notification() -> None:
    """End-to-end: mount the impl as a service, resolve the proxy,
    call :meth:`subscribe` through it, and verify that
    :meth:`trigger_reload` causes the subscriber's :meth:`on_event`
    to fire exactly once per trigger.

    Uses the framework dispatch path (proxy call → typed-service
    framework) rather than calling the method directly on the impl
    — same wire shape a real frontend would exercise.
    """
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with FxtrScope(
            isolated=True,
            include=[GLOBAL_SCOPE],
            name="test",
        ) as scope:
            async with anyio.create_task_group() as impl_tg:
                impl = BasicDynamicDispatchImpl(
                    scope=scope,
                    renderer_dirs=[],
                    impl_tg=impl_tg,
                )
                await impl.compile_and_register()
                dispatcher_ref = (await scope.mount(impl)).replace_service_type(
                    WorkstationDispatcherProto
                )
                sub_impl = _RecordingSubscriberImpl()
                sub_ref = await scope.mount(sub_impl)

                dispatcher = await dispatcher_ref.resolve(scope)
                handle_ref = await dispatcher.subscribe(sub_ref)
                assert handle_ref.target_type is UnsubscribeHandle
                assert impl._hub.live_subscription_count == 1

                impl.trigger_reload()
                await sub_impl.wait_for_count(1)
                assert len(sub_impl.notifications) == 1

                # Second trigger should fire a second notification.
                impl.trigger_reload()
                await sub_impl.wait_for_count(2)
                assert len(sub_impl.notifications) == 2

                impl_tg.cancel_scope.cancel()


async def test_duplicate_subscribe_creates_independent_subscription() -> None:
    """Subscriptions are not deduplicated: calling :meth:`subscribe`
    twice with the same underlying subscriber ref creates two
    independent subscriptions — the subscriber receives every
    broadcast once per subscription. (The workstation inbox's
    ``subscribe_to`` dedups at its own level; the dispatcher doesn't.)
    """
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with FxtrScope(
            isolated=True,
            include=[GLOBAL_SCOPE],
            name="test",
        ) as scope:
            async with anyio.create_task_group() as impl_tg:
                impl = BasicDynamicDispatchImpl(
                    scope=scope,
                    renderer_dirs=[],
                    impl_tg=impl_tg,
                )
                await impl.compile_and_register()
                dispatcher_ref = (await scope.mount(impl)).replace_service_type(
                    WorkstationDispatcherProto
                )
                sub_impl = _RecordingSubscriberImpl()
                sub_ref = await scope.mount(sub_impl)

                dispatcher = await dispatcher_ref.resolve(scope)
                await dispatcher.subscribe(sub_ref)
                await dispatcher.subscribe(sub_ref)
                assert impl._hub.live_subscription_count == 2

                impl.trigger_reload()
                await sub_impl.wait_for_count(2)
                assert len(sub_impl.notifications) == 2

                impl_tg.cancel_scope.cancel()


async def test_unsubscribe_stops_notifications() -> None:
    """Invoking the returned :class:`UnsubscribeHandle` ends the
    subscription: the hub drops it and later triggers deliver
    nothing."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with FxtrScope(
            isolated=True,
            include=[GLOBAL_SCOPE],
            name="test",
        ) as scope:
            async with anyio.create_task_group() as impl_tg:
                impl = BasicDynamicDispatchImpl(
                    scope=scope,
                    renderer_dirs=[],
                    impl_tg=impl_tg,
                )
                await impl.compile_and_register()
                dispatcher_ref = (await scope.mount(impl)).replace_service_type(
                    WorkstationDispatcherProto
                )
                sub_impl = _RecordingSubscriberImpl()
                sub_ref = await scope.mount(sub_impl)

                dispatcher = await dispatcher_ref.resolve(scope)
                handle_ref = await dispatcher.subscribe(sub_ref)

                impl.trigger_reload()
                await sub_impl.wait_for_count(1)

                handle = await handle_ref.resolve(scope)
                await handle.unsubscribe()
                await _wait_until(lambda: impl._hub.live_subscription_count == 0)

                impl.trigger_reload()
                await anyio.sleep(0.05)
                assert len(sub_impl.notifications) == 1

                impl_tg.cancel_scope.cancel()


async def test_raising_subscriber_dropped_without_disturbing_sibling() -> None:
    """A subscriber whose :meth:`on_event` raises is dropped (crash
    backstop); a healthy sibling keeps receiving."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with FxtrScope(
            isolated=True,
            include=[GLOBAL_SCOPE],
            name="test",
        ) as scope:
            async with anyio.create_task_group() as impl_tg:
                impl = BasicDynamicDispatchImpl(
                    scope=scope,
                    renderer_dirs=[],
                    impl_tg=impl_tg,
                )
                await impl.compile_and_register()
                dispatcher_ref = (await scope.mount(impl)).replace_service_type(
                    WorkstationDispatcherProto
                )
                bad = _RecordingSubscriberImpl(raise_on_event=True)
                good = _RecordingSubscriberImpl()

                dispatcher = await dispatcher_ref.resolve(scope)
                await dispatcher.subscribe(await scope.mount(bad))
                await dispatcher.subscribe(await scope.mount(good))
                assert impl._hub.live_subscription_count == 2

                impl.trigger_reload()
                await good.wait_for_count(1)
                # The raiser's subscription tears down...
                await _wait_until(lambda: impl._hub.live_subscription_count == 1)
                # ...and the sibling keeps receiving.
                impl.trigger_reload()
                await good.wait_for_count(2)
                assert bad.notifications == []

                impl_tg.cancel_scope.cancel()


async def test_multiple_subscribers_each_get_notified() -> None:
    """Two distinct subscribers both receive a notification on each
    :meth:`trigger_reload` — broadcast is per-subscription."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        async with FxtrScope(
            isolated=True,
            include=[GLOBAL_SCOPE],
            name="test",
        ) as scope:
            async with anyio.create_task_group() as impl_tg:
                impl = BasicDynamicDispatchImpl(
                    scope=scope,
                    renderer_dirs=[],
                    impl_tg=impl_tg,
                )
                await impl.compile_and_register()
                dispatcher_ref = (await scope.mount(impl)).replace_service_type(
                    WorkstationDispatcherProto
                )
                sub_a = _RecordingSubscriberImpl()
                sub_b = _RecordingSubscriberImpl()
                ref_a = await scope.mount(sub_a)
                ref_b = await scope.mount(sub_b)

                dispatcher = await dispatcher_ref.resolve(scope)
                await dispatcher.subscribe(ref_a)
                await dispatcher.subscribe(ref_b)
                assert impl._hub.live_subscription_count == 2

                impl.trigger_reload()
                await sub_a.wait_for_count(1)
                await sub_b.wait_for_count(1)

                impl_tg.cancel_scope.cancel()


# ---------- mtime-based recompile ----------------------------------


def _stage_renderer(parent: Path, type_id: str, body: str = "") -> Path:
    """Copy fixture renderer into ``parent/<type_id>/index.tsx``,
    optionally appending ``body`` (so callers can produce a distinct
    on-disk file even though the compiled result is shape-identical).
    Returns the parent dir (suitable for ``renderer_dirs=[parent]``).
    """
    subdir = parent / type_id
    subdir.mkdir(parents=True, exist_ok=True)
    text = (FIXTURES_DIR / "renderers_a" / "type_id_aaa" / "index.tsx").read_text()
    if body:
        text += f"\n// {body}\n"
    (subdir / "index.tsx").write_text(text)
    return parent


def _bump_mtime(path: Path) -> None:
    """Advance ``path``'s mtime by 1 second so the mtime snapshot
    detects a change without us having to wait for the real wall
    clock."""
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 1.0))


async def test_recompile_changed_noop_when_nothing_changed(tmp_path: Path) -> None:
    """``recompile_changed_and_reload`` returns ``False`` when no
    subdir's ``index.tsx`` mtime has advanced. No reload broadcast
    fires."""
    if not npx_available():
        pytest.skip("npx not on PATH")
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        _stage_renderer(tmp_path / "r", "type_id_x")
        async with mount_basic_dynamic_dispatch(
            renderer_dirs=[tmp_path / "r"],
        ) as mounted:
            assert "type_id_x" in mounted.impl._table
            # No mtime changes — should be a clean no-op.
            changed = await mounted.impl.recompile_changed_and_reload()
            assert changed is False


@needs_npx
async def test_recompile_changed_fires_on_mtime_bump(tmp_path: Path) -> None:
    """Bumping ``<subdir>/index.tsx``'s mtime causes
    ``recompile_changed_and_reload`` to recompile that subdir,
    update ``_table``, and broadcast a reload notification.

    Subscription goes through the resolved dispatcher proxy so the
    framework dispatch path is what installs the listener (same
    wire shape a real frontend would exercise).
    """
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        renderer_parent = _stage_renderer(tmp_path / "r", "type_id_x")
        async with FxtrScope(
            isolated=True,
            include=[GLOBAL_SCOPE],
            name="test",
        ) as scope:
            async with anyio.create_task_group() as impl_tg:
                impl = BasicDynamicDispatchImpl(
                    scope=scope,
                    renderer_dirs=[renderer_parent],
                    impl_tg=impl_tg,
                )
                await impl.compile_and_register()
                dispatcher_ref = (await scope.mount(impl)).replace_service_type(
                    WorkstationDispatcherProto
                )
                sub_impl = _RecordingSubscriberImpl()
                sub_ref = await scope.mount(sub_impl)

                dispatcher = await dispatcher_ref.resolve(scope)
                await dispatcher.subscribe(sub_ref)

                # First call: no changes ⇒ no-op.
                assert await impl.recompile_changed_and_reload() is False
                assert len(sub_impl.notifications) == 0

                # Bump mtime + recompile ⇒ changed, broadcast fires.
                index_tsx = renderer_parent / "type_id_x" / "index.tsx"
                _bump_mtime(index_tsx)
                assert await impl.recompile_changed_and_reload() is True
                await sub_impl.wait_for_count(1)

                impl_tg.cancel_scope.cancel()


@needs_npx
async def test_recompile_changed_drops_vanished_subdir(tmp_path: Path) -> None:
    """Removing a subdir between two scans drops its entry from
    ``_table`` and broadcasts a reload."""
    with anyio.fail_after(DEADLOCK_TIMEOUT):
        renderer_parent = tmp_path / "r"
        _stage_renderer(renderer_parent, "type_id_keep")
        _stage_renderer(renderer_parent, "type_id_drop")

        async with FxtrScope(
            isolated=True,
            include=[GLOBAL_SCOPE],
            name="test",
        ) as scope:
            async with anyio.create_task_group() as impl_tg:
                impl = BasicDynamicDispatchImpl(
                    scope=scope,
                    renderer_dirs=[renderer_parent],
                    impl_tg=impl_tg,
                )
                await impl.compile_and_register()
                dispatcher_ref = (await scope.mount(impl)).replace_service_type(
                    WorkstationDispatcherProto
                )
                sub_impl = _RecordingSubscriberImpl()
                sub_ref = await scope.mount(sub_impl)

                dispatcher = await dispatcher_ref.resolve(scope)
                await dispatcher.subscribe(sub_ref)
                assert set(impl._table.keys()) == {
                    "type_id_keep",
                    "type_id_drop",
                }

                # Remove the second subdir entirely.
                drop_subdir = renderer_parent / "type_id_drop"
                (drop_subdir / "index.tsx").unlink()
                drop_subdir.rmdir()

                assert await impl.recompile_changed_and_reload() is True
                assert set(impl._table.keys()) == {"type_id_keep"}
                await sub_impl.wait_for_count(1)

                impl_tg.cancel_scope.cancel()


async def test_recompile_before_register_raises_irexception() -> None:
    """Calling :meth:`recompile_changed_and_reload` before
    :meth:`compile_and_register` completes is a programming error
    — same category as the resolve guard."""
    async with FxtrScope(
        isolated=True,
        include=[GLOBAL_SCOPE],
        name="dispatch",
    ) as dispatch_scope:
        async with anyio.create_task_group() as impl_tg:
            impl = BasicDynamicDispatchImpl(
                scope=dispatch_scope,
                renderer_dirs=[],
                impl_tg=impl_tg,
            )
            with pytest.raises(IRException) as exc_info:
                await impl.recompile_changed_and_reload()
            assert exc_info.value.category == "DispatcherNotRegistered"
            impl_tg.cancel_scope.cancel()
