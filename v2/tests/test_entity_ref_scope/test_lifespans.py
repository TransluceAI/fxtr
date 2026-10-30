"""Tests for stage-12 chunk-2 lifespan machinery: ``attach_lifespan_to_scope``,
the closing event, and structured teardown (aclose waits for lifespans).

See ``plans/phase3/stage12_better_scope/new_scope_design.md`` §4.6.
"""

from __future__ import annotations

import contextlib

import anyio
import pytest

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope

pytestmark = pytest.mark.anyio

_BUILTIN = FxtrID.builtin("lifespan_test_builtin")


class TestAttachLifespan:
    async def test_setup_authorized_teardown_unauthorized_and_returns_value(self):
        """Setup runs authorized + active; teardown runs unauthorized; the
        attach call returns the CM's yielded value."""
        seen: dict[str, object] = {}

        @contextlib.asynccontextmanager
        async def ls(scope: FxtrScope):
            seen["setup_auth"] = scope.is_authorized()
            seen["setup_active"] = FxtrScope.get_active() is scope
            yield "STATE"
            seen["teardown_auth"] = scope.is_authorized()

        async with FxtrScope(isolated=True) as scope:
            value = await scope.attach_lifespan_to_scope(ls(scope))
            assert value == "STATE"
            assert seen["setup_auth"] is True
            assert seen["setup_active"] is True
        # After scope exit the lifespan has torn down, unauthorized.
        assert seen["teardown_auth"] is False

    async def test_lifespanless_scope_raises(self):
        scope = FxtrScope(isolated=True).lowlevel_start_lifespanless()
        try:

            @contextlib.asynccontextmanager
            async def ls(_s):
                yield None

            with pytest.raises(RuntimeError, match="no task group"):
                await scope.attach_lifespan_to_scope(ls(scope))
        finally:
            scope.force_close()

    async def test_setup_can_store_into_active_scope(self):
        """Setup runs with the scope active + authorized, so scope-relative
        APIs target it."""
        tracked: dict[str, FxtrID] = {}

        @contextlib.asynccontextmanager
        async def ls(scope: FxtrScope):
            tracked["id"] = scope.track_builtin(_BUILTIN)  # authorized + active
            yield None

        async with FxtrScope(isolated=True) as scope:
            await scope.attach_lifespan_to_scope(ls(scope))
            assert scope.is_tracked(tracked["id"])


class TestStructuredTeardown:
    async def test_teardown_completes_before_scope_exit(self):
        order: list[str] = []

        @contextlib.asynccontextmanager
        async def ls(_s):
            yield None
            order.append("teardown")

        async with FxtrScope(isolated=True) as scope:
            await scope.attach_lifespan_to_scope(ls(scope))
            order.append("body")
        order.append("exited")
        assert order == ["body", "teardown", "exited"]

    async def test_explicit_aclose_waits_for_teardown(self):
        order: list[str] = []

        @contextlib.asynccontextmanager
        async def ls(_s):
            yield None
            await anyio.sleep(0.05)
            order.append("teardown")

        scope = FxtrScope(isolated=True)
        async with scope.open_with_explicit_leases(parent_leases={}):
            await scope.attach_lifespan_to_scope(ls(scope))
            await scope.aclose()
            order.append("after-aclose")
        assert order == ["teardown", "after-aclose"]

    async def test_concurrent_teardown_not_lifo(self):
        """Multiple lifespans tear down concurrently, not in LIFO order: the
        first-attached but slowest-teardown finishes last."""
        order: list[str] = []

        def make(name: str, delay: float):
            @contextlib.asynccontextmanager
            async def ls(_s):
                yield None
                await anyio.sleep(delay)
                order.append(name)

            return ls

        async with FxtrScope(isolated=True) as scope:
            await scope.attach_lifespan_to_scope(make("slow", 0.06)(scope))  # attached first
            await scope.attach_lifespan_to_scope(make("fast", 0.0)(scope))
        # Concurrent: fast finishes before slow despite being attached later.
        assert order == ["fast", "slow"]


class TestNoDeadlockDrain:
    async def test_well_behaved_lifespan_drains_without_timeout(self):
        """A correctly-written lifespan (holds no standing Lease[main] past its
        yield) lets the scope drain promptly. Bounded with ``move_on_after`` so
        a regression that reintroduces a standing lease in the parked lifespan
        fails (times out) instead of hanging forever."""

        @contextlib.asynccontextmanager
        async def ls(_s):
            yield None

        cancelled = False
        with anyio.move_on_after(5.0) as cs:
            async with FxtrScope(isolated=True) as scope:
                await scope.attach_lifespan_to_scope(ls(scope))
        cancelled = cs.cancelled_caught
        assert not cancelled, "scope drain hung — a parked lifespan pinned the token"

    async def test_standing_lease_in_lifespan_would_hang(self):
        """Demonstrates the regression the bound above guards against: a lifespan
        that retains a standing ``Lease[main]`` past its yield pins the token, so
        drain hangs and the ``move_on_after`` bound fires."""

        @contextlib.asynccontextmanager
        async def bad_ls(scope: FxtrScope):
            held = scope.clone_authorization()  # standing Lease[main] — BUG
            try:
                yield None
            finally:
                held.discard()  # released only when teardown runs (after drain)

        with anyio.move_on_after(0.3) as cs:
            async with FxtrScope(isolated=True) as scope:
                await scope.attach_lifespan_to_scope(bad_ls(scope))
        assert cs.cancelled_caught, "expected the standing-lease bug to hang the drain"


class TestExceptionPath:
    async def test_teardown_runs_on_exception(self):
        """An exception in the scope body force-closes; attached lifespans still
        tear down (synchronous teardown completes even under cancellation)."""
        torn: list[bool] = []

        @contextlib.asynccontextmanager
        async def ls(_s):
            yield None
            torn.append(True)  # no await — completes even under cancellation

        with pytest.raises(ValueError, match="boom"):
            async with FxtrScope(isolated=True) as scope:
                await scope.attach_lifespan_to_scope(ls(scope))
                raise ValueError("boom")
        assert torn == [True]
