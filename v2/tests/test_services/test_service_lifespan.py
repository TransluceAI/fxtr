"""Service mount-lifespan + dispatch-accessor coverage (design §11).

Exercises the chunk-3 service surface: ``_fxtr_mount_lifespan`` (setup
authorized + with the mount scope active; teardown after drain, unauthorized),
the per-call ``_fxtr_get_mount_scope`` / ``_fxtr_get_lifespan_state`` accessors
(reading the framework ``_CURRENT_DISPATCH_HANDLE`` contextvar), multi-mount
isolation, the active-scope asymmetry (call scope active during a call, not the
mount scope), and the structural teardown-after-in-flight-calls ordering.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import anyio
import pytest

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope
from fxtr2.services.untyped_service import UntypedServiceImpl, service_method

pytestmark = pytest.mark.anyio


class _LifespanService(UntypedServiceImpl):
    """Records lifespan observations and exposes the dispatch accessors.

    Per the recommended convention it stashes nothing per-mount on ``self``
    beyond observation flags — the per-mount state is the value yielded by
    ``_fxtr_mount_lifespan`` (a fresh ``{"calls": []}`` dict each mount), reached
    inside a call via ``_fxtr_get_lifespan_state()``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setup_count = 0
        self.teardown_count = 0
        self.setup_authorized: bool | None = None
        self.setup_active_is_mount: bool | None = None
        self.teardown_authorized: bool | None = None

    @contextlib.asynccontextmanager
    async def _fxtr_mount_lifespan(self, scope: FxtrScope) -> AsyncIterator[dict[str, Any]]:
        self.setup_count += 1
        self.setup_authorized = scope.is_authorized()
        self.setup_active_is_mount = FxtrScope.get_active() is scope
        state: dict[str, Any] = {"calls": []}
        yield state
        self.teardown_count += 1
        # After drain, the mount scope's base auth is gone → unauthorized.
        self.teardown_authorized = scope.is_authorized()

    @service_method
    async def lifespan_state_calls(self) -> int:
        state = self._fxtr_get_lifespan_state()
        state["calls"].append(1)
        return len(state["calls"])

    @service_method
    async def mount_scope_authorized(self) -> bool:
        return self._fxtr_get_mount_scope().is_authorized()

    @service_method
    async def active_is_mount_scope(self) -> bool:
        # During a call the active scope is the per-call child, NOT the mount
        # scope (§5.3 active-scope asymmetry).
        return FxtrScope.get_active() is self._fxtr_get_mount_scope()

    @service_method
    async def store_in_mount(self, builtin: str) -> str:
        # Track a builtin into the mount scope; it must persist across calls
        # because the mount scope outlives the per-call scope.
        fid = self._fxtr_get_mount_scope().track_builtin(FxtrID.builtin(builtin))
        return str(fid)

    @service_method
    async def is_tracked_in_mount(self, builtin: str) -> bool:
        return self._fxtr_get_mount_scope().is_tracked(FxtrID.builtin(builtin))


class _NoLifespanService(UntypedServiceImpl):
    """No ``_fxtr_mount_lifespan`` override — uses the no-op default."""

    @service_method
    async def echo(self, value: Any) -> Any:
        return value


@contextlib.asynccontextmanager
async def _mounted(impl: UntypedServiceImpl) -> AsyncIterator[tuple[Any, FxtrScope]]:
    async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as scope:
        ref = await scope.mount(impl)
        proxy = await ref.resolve(scope)
        yield proxy, scope


class TestLifespanHook:
    async def test_setup_authorized_and_active_teardown_unauthorized(self) -> None:
        impl = _LifespanService()
        async with _mounted(impl) as (proxy, _scope):
            assert impl.setup_count == 1
            assert impl.setup_authorized is True
            assert impl.setup_active_is_mount is True
            assert impl.teardown_count == 0
            await proxy.lifespan_state_calls()
        # Scope exited → drained → teardown ran, unauthorized.
        assert impl.teardown_count == 1
        assert impl.teardown_authorized is False

    async def test_lifespan_state_retrieval(self) -> None:
        impl = _LifespanService()
        async with _mounted(impl) as (proxy, _scope):
            # Same per-mount state object across calls → the list accumulates.
            assert await proxy.lifespan_state_calls() == 1
            assert await proxy.lifespan_state_calls() == 2
            assert await proxy.lifespan_state_calls() == 3

    async def test_no_lifespan_impl_mounts_and_dispatches(self) -> None:
        async with _mounted(_NoLifespanService()) as (proxy, _scope):
            assert await proxy.echo("hi") == "hi"


class TestDispatchAccessors:
    async def test_mount_scope_is_authorized_during_call(self) -> None:
        async with _mounted(_LifespanService()) as (proxy, _scope):
            assert await proxy.mount_scope_authorized() is True

    async def test_mount_scope_store_persists_across_calls(self) -> None:
        async with _mounted(_LifespanService()) as (proxy, _scope):
            assert await proxy.is_tracked_in_mount("svc-lifespan-persist") is False
            await proxy.store_in_mount("svc-lifespan-persist")
            # A later call sees the mount-scope tracking from the earlier call.
            assert await proxy.is_tracked_in_mount("svc-lifespan-persist") is True

    async def test_active_scope_is_call_scope_not_mount(self) -> None:
        async with _mounted(_LifespanService()) as (proxy, _scope):
            assert await proxy.active_is_mount_scope() is False

    async def test_accessors_raise_outside_dispatch(self) -> None:
        impl = _LifespanService()
        with pytest.raises(RuntimeError, match="outside a service dispatch"):
            impl._fxtr_get_mount_scope()
        with pytest.raises(RuntimeError, match="outside a service dispatch"):
            impl._fxtr_get_lifespan_state()


class TestMultiMount:
    async def test_same_impl_two_scopes_independent_state(self) -> None:
        """Mounting one impl instance into two scopes yields independent
        per-mount lifespan state (two handles, two yielded dicts)."""
        impl = _LifespanService()
        async with _mounted(impl) as (proxy_a, _a):
            async with _mounted(impl) as (proxy_b, _b):
                assert impl.setup_count == 2
                # Each mount has its own ``{"calls": []}`` state.
                assert await proxy_a.lifespan_state_calls() == 1
                assert await proxy_a.lifespan_state_calls() == 2
                assert await proxy_b.lifespan_state_calls() == 1  # independent
                assert await proxy_a.lifespan_state_calls() == 3
        assert impl.teardown_count == 2


class TestTeardownOrdering:
    async def test_teardown_waits_for_in_flight_call(self) -> None:
        """Teardown runs only after in-flight calls complete: the mount-scope
        drain waits for the call's ``Lease[mount.main]`` (carried by the per-call
        child) to release. Bounded so a regression hangs loudly instead of
        forever."""
        started = anyio.Event()
        release = anyio.Event()

        class _BlockingService(UntypedServiceImpl):
            def __init__(self) -> None:
                super().__init__()
                self.teardown_ran = False

            @contextlib.asynccontextmanager
            async def _fxtr_mount_lifespan(self, scope: FxtrScope) -> AsyncIterator[None]:
                yield None
                self.teardown_ran = True

            @service_method
            async def block(self) -> int:
                started.set()
                await release.wait()
                return 1

        impl = _BlockingService()

        with anyio.fail_after(5):
            async with anyio.create_task_group() as tg:

                async def releaser() -> None:
                    await started.wait()
                    # The call is in flight and the scope is mid-drain (close
                    # requested below); teardown must NOT have run yet.
                    await anyio.sleep(0.1)
                    assert impl.teardown_ran is False
                    release.set()

                tg.start_soon(releaser)

                async with FxtrScope(include=[GLOBAL_SCOPE], isolated=True) as scope:
                    ref = await scope.mount(impl)
                    proxy = await ref.resolve(scope)
                    tg.start_soon(_call_blocking, proxy)
                    await started.wait()
                    # Fall out of the `async with` → aclose blocks on drain until
                    # the in-flight call releases, then teardown runs.
        assert impl.teardown_ran is True


async def _call_blocking(proxy: Any) -> None:
    await proxy.block()
