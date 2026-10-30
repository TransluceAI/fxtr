"""End-to-end tests for ``fxtr2.rpc.scope_bridge.open_rpc_scope``.

Two-party RPC bridges using a stapled in-process pair of
``anyio.create_memory_object_stream[RPCMessage]`` (raw RPCMessage transport).

Drain-coordination convention
-----------------------------

Each side's ``open_rpc_scope`` body is just the work that side wants to do; on
exit the conn drain inside ``open_rpc_scope`` blocks until the *peer* has
released the corresponding grant exports. So while one side sits in conn
drain, its outer host scope stays alive (the host scope's CM is wrapping the
``open_rpc_scope`` CM) and its services remain dispatchable for the peer's
in-flight calls. Once both sides have entered drain and released their
bridges, both conn drains unblock together — the drain itself is the
synchronization mechanism, no explicit events needed.
"""

from __future__ import annotations

import math
from typing import Any

import anyio
import anyio.abc
import pytest
from anyio.streams.stapled import StapledObjectStream

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef, get_active_scope
from fxtr2.leases.leased_cap_ir import IRException
from fxtr2.rpc.lowering_types import RPCMessage
from fxtr2.rpc.scope_bridge import RPCSession, open_rpc_scope
from fxtr2.services.untyped_service import UntypedServiceImpl, service_method

pytestmark = pytest.mark.anyio


_DEADLOCK_TIMEOUT: float = 5.0


def _make_transport_pair() -> (
    tuple[anyio.abc.ObjectStream[RPCMessage], anyio.abc.ObjectStream[RPCMessage]]
):
    """Stapled bidirectional in-memory ``RPCMessage`` transport."""
    a_to_b_send, a_to_b_recv = anyio.create_memory_object_stream[RPCMessage](math.inf)
    b_to_a_send, b_to_a_recv = anyio.create_memory_object_stream[RPCMessage](math.inf)
    a_transport = StapledObjectStream(a_to_b_send, b_to_a_recv)
    b_transport = StapledObjectStream(b_to_a_send, a_to_b_recv)
    return a_transport, b_transport


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


####################################################################################################
# Sample service impls
####################################################################################################


class _EchoService(UntypedServiceImpl):
    @service_method
    async def echo(self, value: Any) -> Any:
        return value

    @service_method
    async def add(self, a: int, b: int) -> int:
        return a + b


class _SpawnerService(UntypedServiceImpl):
    """Mounts a fresh echo service in the call body and returns the ref."""

    @service_method
    async def echo(self, value: Any) -> Any:
        return value

    @service_method
    async def spawn_echo(self) -> ServiceRef[Any]:
        scope = FxtrScope.get_active()
        return await scope.mount(_EchoService())


_FRESH_TYPE_ID = FxtrID.builtin("test-scope-bridge-fresh-type-id")


class _FreshTypeIdEchoService(UntypedServiceImpl):
    """An echo service whose service_type_id is a fresh builtin.

    The fresh type_id is *not* shared with any peer's bootstrap main
    type_id and is not pulled in via any arg arena, so it must arrive
    on the caller's home scope through the result-arena pass of
    ``_install_returned_resources``.
    """

    @service_method
    async def echo(self, value: Any) -> Any:
        return value

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(_FRESH_TYPE_ID)


class _FreshTypeIdSpawnerService(UntypedServiceImpl):
    """Mounts a ``_FreshTypeIdEchoService`` in the call body and returns the ref.

    Spawner-side type_id is the default ``untyped_service`` (set by
    ``UntypedServiceImpl``); the *returned* ref's type_id is the fresh
    builtin. Exercises the install-order path that the existing
    ``_SpawnerService`` test misses (since that test's returned ref
    shares its type_id with the bootstrap peer-main).
    """

    @service_method
    async def spawn_fresh_echo(self) -> ServiceRef[Any]:
        return await get_active_scope().mount(_FreshTypeIdEchoService())


class _RaisingService(UntypedServiceImpl):
    @service_method
    async def echo(self, value: Any) -> Any:
        return value

    @service_method
    async def boom(self) -> Any:
        raise ValueError("kaboom from peer service")


class _WrappingService(UntypedServiceImpl):
    """Receives a ServiceRef arg; wraps it in a fresh service that delegates back."""

    @service_method
    async def echo(self, value: Any) -> Any:
        return value

    @service_method
    async def wrap(self, inner_ref: ServiceRef[Any]) -> ServiceRef[Any]:
        scope = FxtrScope.get_active()

        class _Delegator(UntypedServiceImpl):
            @service_method
            async def call_inner(self, msg: Any) -> Any:
                proxy = await inner_ref.resolve()
                return await proxy.echo(msg)

        return await scope.mount(_Delegator())


####################################################################################################
# Tests
####################################################################################################


class TestBasicBootstrap:
    async def test_basic_bootstrap_works(self) -> None:
        """Symmetric two-party setup. Each side mounts an echo service. After
        ``open_rpc_scope``, each side resolves the peer's ref and successfully
        invokes a method.
        """
        a_transport, b_transport = _make_transport_pair()

        with anyio.fail_after(_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with FxtrScope(isolated=False, name="A") as host:
                        my_ref = await host.mount(_EchoService())
                        async with open_rpc_scope(a_transport, my_ref, name="A_rpc") as session:
                            assert isinstance(session, RPCSession)
                            assert isinstance(session.remote_service, ServiceRef)
                            proxy = await session.remote_service.resolve(session.scope)
                            assert await proxy.echo("hello-from-A") == "hello-from-A"
                            assert await proxy.add(2, 3) == 5

                async def side_b() -> None:
                    async with FxtrScope(isolated=False, name="B") as host:
                        my_ref = await host.mount(_EchoService())
                        async with open_rpc_scope(b_transport, my_ref, name="B_rpc") as session:
                            proxy = await session.remote_service.resolve(session.scope)
                            assert await proxy.echo({"x": 1}) == {"x": 1}

                tg.start_soon(side_a)
                tg.start_soon(side_b)


class TestNoOpMainService:
    async def test_none_our_main_service_uses_noop(self) -> None:
        """``open_rpc_scope(transport, our_main_service=None)`` mounts a NoOpService
        on a freshly-opened isolated host scope. The peer can resolve it for
        introspection but every method call raises ``UnknownMethod``.

        Side A passes ``None``; side B exposes a real echo service. A invokes B's
        echo successfully (one-direction RPC); B resolves A's noop service and gets
        ``UnknownMethod`` on any attempted call.
        """
        from fxtr2.data.entity_core import FxtrID

        a_transport, b_transport = _make_transport_pair()

        with anyio.fail_after(_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    # No outer host scope needed at all — open_rpc_scope mounts
                    # NoOpService on its own internal isolated host.
                    async with open_rpc_scope(a_transport, name="A_rpc") as session:
                        proxy = await session.remote_service.resolve(session.scope)
                        assert await proxy.echo("hi") == "hi"

                async def side_b() -> None:
                    async with FxtrScope(isolated=False, name="B") as host:
                        my_ref = await host.mount(_EchoService())
                        async with open_rpc_scope(b_transport, my_ref, name="B_rpc") as session:
                            # A's main is a NoOpService — service_type_id is the
                            # builtin "noop_service" FxtrID, methods all raise.
                            assert session.remote_service.service_type_id == FxtrID.builtin(
                                "noop_service"
                            )
                            proxy = await session.remote_service.resolve(session.scope)
                            with pytest.raises(IRException) as excinfo:
                                await proxy.anything()
                            assert excinfo.value.category == "UnknownMethod"

                tg.start_soon(side_a)
                tg.start_soon(side_b)


class TestSpawnerOverRPC:
    async def test_spawn_returns_fresh_serviceref(self) -> None:
        """Substage-6 ServiceRef-as-return chain-bridge mechanic, layered onto RPC.

        B's main is a spawner; A calls ``spawn_echo`` and receives a fresh
        ``ServiceRef`` pointing at the service B mounted inside the call body. A
        then invokes it through the chain bridge.
        """
        a_transport, b_transport = _make_transport_pair()

        with anyio.fail_after(_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with FxtrScope(isolated=False, name="A") as host:
                        my_main = await host.mount(_EchoService())
                        async with open_rpc_scope(a_transport, my_main, name="A_rpc") as session:
                            their_proxy = await session.remote_service.resolve(session.scope)
                            spawned_ref = await their_proxy.spawn_echo()
                            assert isinstance(spawned_ref, ServiceRef)
                            spawned_proxy = await spawned_ref.resolve(session.scope)
                            assert await spawned_proxy.echo("hello") == "hello"

                async def side_b() -> None:
                    async with FxtrScope(isolated=False, name="B") as host:
                        my_main = await host.mount(_SpawnerService())
                        async with open_rpc_scope(b_transport, my_main, name="B_rpc"):
                            pass

                tg.start_soon(side_a)
                tg.start_soon(side_b)


_STASHED_BUILTIN = FxtrID.builtin("test-scope-bridge-stashed-builtin")


class _StashesAndReturnsId(UntypedServiceImpl):
    """Service whose method returns a FxtrID stashed at construction time.

    The id is tracked on the mount scope (the test sets that up), but
    is *not* an arg of the method being called. The dispatch's
    call_scope is fresh and derived from the mount scope, so the id is
    reachable via parent-walk but not directly tracked in
    ``call_scope._main_arena`` until something lifts it.
    """

    def __init__(self, stashed: FxtrID) -> None:
        self._stashed = stashed

    @service_method
    async def get_stashed(self) -> FxtrID:
        return self._stashed


class TestReturnedIdAutoLiftedFromMountScope:
    """A service method that returns a FxtrID it captured at construction
    time (tracked on the mount scope, not received as a method arg)
    must work without any extra ``track_*`` boilerplate in the impl.

    The dispatch's call_scope is a child of the mount scope, so the id
    is reachable via parent-walk; the framework lifts it into the call
    scope before encoding the return packet.

    Without the auto-lift fix this fails as
    ``IRException: [ValueError] Node ... is not tracked in this arena!``
    raised from ``_build_return_resources``.
    """

    async def test_returned_stashed_id_round_trips(self) -> None:
        a_transport, b_transport = _make_transport_pair()

        with anyio.fail_after(_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with FxtrScope(isolated=False, name="A") as host:
                        my_main = await host.mount(_EchoService())
                        async with open_rpc_scope(a_transport, my_main, name="A_rpc") as session:
                            proxy = await session.remote_service.resolve(session.scope)
                            got = await proxy.get_stashed()
                            assert got == _STASHED_BUILTIN

                async def side_b() -> None:
                    async with FxtrScope(isolated=False, name="B") as host:
                        # Pre-track the stashed id on B's mount scope.
                        host.track_builtin(_STASHED_BUILTIN)
                        my_main = await host.mount(_StashesAndReturnsId(_STASHED_BUILTIN))
                        async with open_rpc_scope(b_transport, my_main, name="B_rpc"):
                            pass

                tg.start_soon(side_a)
                tg.start_soon(side_b)


class TestArbitraryExceptionInDispatchSurfacesAsIRException:
    """An arbitrary ``Exception`` raised anywhere in the dispatch (including
    paths *outside* the user's ``_handle_invoke`` body, e.g. result-encode /
    return-arena build) must reach the caller as a clean ``IRException``
    whose category reflects the original exception type — not as an
    ``IRException: [EndOfStream]`` produced because the dispatch task
    crashed without sending an in-band ``("err", …)`` packet.
    """

    async def test_runtime_error_in_user_method_surfaces_as_irexception(self) -> None:
        a_transport, b_transport = _make_transport_pair()
        captured: list[IRException] = []

        class _PostInvokeFails(UntypedServiceImpl):
            @service_method
            async def echo(self, value: Any) -> Any:
                return value

            @service_method
            async def boom(self) -> Any:
                raise RuntimeError("kaboom from boom()")

        with anyio.fail_after(_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with FxtrScope(isolated=False, name="A") as host:
                        my_main = await host.mount(_EchoService())
                        async with open_rpc_scope(a_transport, my_main, name="A_rpc") as session:
                            proxy = await session.remote_service.resolve(session.scope)
                            try:
                                await proxy.boom()
                            except IRException as exc:
                                captured.append(exc)

                async def side_b() -> None:
                    async with FxtrScope(isolated=False, name="B") as host:
                        my_main = await host.mount(_PostInvokeFails())
                        async with open_rpc_scope(b_transport, my_main, name="B_rpc"):
                            pass

                tg.start_soon(side_a)
                tg.start_soon(side_b)

        assert len(captured) == 1
        exc = captured[0]
        assert exc.category == "RuntimeError", (
            "caller should see category=RuntimeError reflecting the original "
            f"user-method exception, not EndOfStream. got: category={exc.category!r}, "
            f"message={exc.message!r}"
        )
        assert "kaboom from boom()" in (exc.message or "") or any(
            "kaboom from boom()" in s for s in (exc.traceback_info or "").splitlines()
        ), f"expected the original message to survive; got {exc!r}"


class TestSpawnerReturnsFreshTypeId:
    async def test_returned_ref_with_fresh_type_id_installs(self) -> None:
        """Regression: a returned ``ServiceRef`` whose ``service_type_id`` is *not*
        already tracked on the caller's home scope (i.e. not shared with the
        bootstrap peer-main type_id, not pulled in through an arg arena) must
        still be installable.

        The fresh type_id needs to ride the result arena and get bridged in by
        ``_install_returned_resources``'s arena pass before the handle pass
        runs ``attach_bridged_lowlevel_cap`` (which strict-precondition-checks
        ``is_tracked(service_type_id)``).

        Without the install-order fix this manifests as
        ``RPCConnectionAborted: ... must be tracked in <FxtrScope ...> before
        attaching.``
        """
        a_transport, b_transport = _make_transport_pair()

        with anyio.fail_after(_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with FxtrScope(isolated=False, name="A") as host:
                        my_main = await host.mount(_EchoService())
                        async with open_rpc_scope(a_transport, my_main, name="A_rpc") as session:
                            their_proxy = await session.remote_service.resolve(session.scope)
                            spawned_ref = await their_proxy.spawn_fresh_echo()
                            assert isinstance(spawned_ref, ServiceRef)
                            assert spawned_ref.service_type_id == _FRESH_TYPE_ID
                            spawned_proxy = await spawned_ref.resolve(session.scope)
                            assert await spawned_proxy.echo("hello-fresh") == "hello-fresh"

                async def side_b() -> None:
                    async with FxtrScope(isolated=False, name="B") as host:
                        my_main = await host.mount(_FreshTypeIdSpawnerService())
                        async with open_rpc_scope(b_transport, my_main, name="B_rpc"):
                            pass

                tg.start_soon(side_a)
                tg.start_soon(side_b)


class TestSwapServicesViaMethod:
    async def test_pass_serviceref_through_rpc(self) -> None:
        """Substage-6 ServiceRef-as-arg / ServiceRef-as-return mechanics layered onto
        the RPC bridge.

        B's main is a WrappingService. A passes a separate ServiceRef as an arg to
        ``wrap``; B mounts a delegator in the call body and returns the ref. A
        invokes the returned ref, which calls back through to A's original echo.
        End-to-end thread: A → rpc → B (wrap) → rpc-back → A (echo).
        """
        a_transport, b_transport = _make_transport_pair()

        with anyio.fail_after(_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with FxtrScope(isolated=False, name="A") as host:
                        my_main = await host.mount(_EchoService())
                        my_inner_echo = await host.mount(_EchoService())
                        async with open_rpc_scope(a_transport, my_main, name="A_rpc") as session:
                            their_proxy = await session.remote_service.resolve(session.scope)
                            wrapped_ref = await their_proxy.wrap(my_inner_echo)
                            assert isinstance(wrapped_ref, ServiceRef)
                            wrapped_proxy = await wrapped_ref.resolve(session.scope)
                            result = await wrapped_proxy.call_inner("ping")
                            assert result == "ping"

                async def side_b() -> None:
                    async with FxtrScope(isolated=False, name="B") as host:
                        my_main = await host.mount(_WrappingService())
                        async with open_rpc_scope(b_transport, my_main, name="B_rpc"):
                            pass

                tg.start_soon(side_a)
                tg.start_soon(side_b)


class TestCleanDrain:
    async def test_no_lease_leaks_after_exit(self) -> None:
        """After both sides exit ``open_rpc_scope``, the autouse leak fixture sees
        no leaked leases.
        """
        a_transport, b_transport = _make_transport_pair()

        with anyio.fail_after(_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side(transport: anyio.abc.ObjectStream[RPCMessage], label: str) -> None:
                    async with FxtrScope(isolated=False, name=label) as host:
                        my_ref = await host.mount(_EchoService())
                        async with open_rpc_scope(transport, my_ref, name=label) as session:
                            proxy = await session.remote_service.resolve(session.scope)
                            assert await proxy.echo(label) == label

                tg.start_soon(side, a_transport, "A")
                tg.start_soon(side, b_transport, "B")


class TestEagerPrecondition:
    async def test_unreachable_ref_raises_before_opening_transport(self) -> None:
        """Calling ``open_rpc_scope`` with a ServiceRef whose handle isn't reachable
        from the active scope raises ``ValueError`` before opening the transport.
        """
        a_transport, _b_transport = _make_transport_pair()

        async with FxtrScope(isolated=False, name="A_host") as host_a:
            ref_a = await host_a.mount(_EchoService())

        # ref_a's host scope has now exited — ref_a is no longer reachable from any
        # active scope. Attempting to use it as our_main_service should fail.
        async with FxtrScope(isolated=False, name="other"):
            with pytest.raises(ValueError, match="not registered"):
                async with open_rpc_scope(a_transport, ref_a, name="A_rpc"):
                    pass


class TestCancelDuringBootstrap:
    async def test_closed_transport_unblocks_other_side(self) -> None:
        """If one side closes its transport without ever calling ``open_rpc_scope``,
        the other side's bootstrap fails with the connection's standard error path.
        """
        a_transport, b_transport = _make_transport_pair()

        async def side_a() -> None:
            async with FxtrScope(isolated=False, name="A") as host:
                my_ref = await host.mount(_EchoService())
                async with open_rpc_scope(a_transport, my_ref, name="A_rpc"):
                    pass

        # Close B's transport so A can't reach a peer.
        await b_transport.aclose()

        with anyio.fail_after(_DEADLOCK_TIMEOUT):
            with pytest.raises(BaseException):  # noqa: B017,PT011
                await side_a()


class TestExceptionAbortsBothSides:
    async def test_exception_in_body_tears_down_peer_in_sleep(self) -> None:
        """When one side raises in its ``open_rpc_scope`` body, the other side is
        torn down promptly even if it's parked on an infinite sleep while holding
        the peer's main service.

        Verifies: (a) abort cancellation propagates to a body parked on
        ``anyio.sleep_forever``, (b) the original exception is catchable outside
        ``open_rpc_scope`` without corrupting the host scope, (c) the host scope's
        own services are still usable after recovery, and (d) the host scope
        exits cleanly.
        """
        a_transport, b_transport = _make_transport_pair()
        a_made_call = anyio.Event()

        with anyio.fail_after(_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with FxtrScope(isolated=False, name="A") as host:
                        my_main = await host.mount(_EchoService())
                        with pytest.raises(RuntimeError, match="kaboom from A's body"):
                            async with open_rpc_scope(
                                a_transport, my_main, name="A_rpc"
                            ) as session:
                                proxy = await session.remote_service.resolve(session.scope)
                                # First, a successful call to confirm the connection
                                # was healthy before the abort.
                                assert await proxy.echo("setup") == "setup"
                                # Signal B to enter sleep_forever holding the peer ref.
                                a_made_call.set()
                                # Brief yield so B definitely parks on sleep before we
                                # raise. (Not strictly required for correctness; the
                                # abort would still propagate.)
                                await anyio.sleep(0.01)
                                raise RuntimeError("kaboom from A's body")
                        # After ``open_rpc_scope`` exits with the exception, the host
                        # scope should still be fully usable.
                        my_proxy = await my_main.resolve(host)
                        assert await my_proxy.echo("after-recovery") == "after-recovery"

                async def side_b() -> None:
                    async with FxtrScope(isolated=False, name="B") as host:
                        my_main = await host.mount(_SpawnerService())
                        # B's body parks on sleep_forever while holding a resolved peer
                        # ref. The abort cancels us out via the keepalive cancel scope.
                        # The exception that surfaces is whatever the conn's abort path
                        # produces (a LeaseBrokenError-bearing group); we don't pin it
                        # down beyond "something raised".
                        with pytest.raises(BaseException):  # noqa: B017,PT011
                            async with open_rpc_scope(
                                b_transport, my_main, name="B_rpc"
                            ) as session:
                                await a_made_call.wait()
                                _proxy = await session.remote_service.resolve(session.scope)
                                # Hold the proxy reference and sleep forever.
                                await anyio.sleep_forever()
                        my_proxy = await my_main.resolve(host)
                        assert await my_proxy.echo("b-recovery") == "b-recovery"

                tg.start_soon(side_a)
                tg.start_soon(side_b)


class TestLiftOurMainServiceIntoActiveScope:
    async def test_open_rpc_scope_can_lift_services(self) -> None:
        a_transport, b_transport = _make_transport_pair()

        with anyio.fail_after(_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with FxtrScope(name="host_a") as host_a:
                        a_ref = await host_a.mount(_EchoService())
                        async with FxtrScope(name="host_a_child"):
                            async with open_rpc_scope(a_transport, a_ref, name="rpc_a") as session:
                                proxy = await session.remote_service.resolve(session.scope)
                                assert await proxy.echo("ping-from-host_a") == "ping-from-host_a"

                async def side_b() -> None:
                    async with FxtrScope(isolated=False, name="host_b") as host_b:
                        b_ref = await host_b.mount(_EchoService())
                        async with FxtrScope(name="host_b_child"):
                            async with open_rpc_scope(b_transport, b_ref, name="rpc_b") as session:
                                proxy = await session.remote_service.resolve(session.scope)
                                assert await proxy.echo("ping-from-host_b") == "ping-from-host_b"

                tg.start_soon(side_a)
                tg.start_soon(side_b)


class TestTracebackPropagation:
    async def test_python_exception_traceback_crosses_wire(self) -> None:
        """When a peer's service method raises a Python ``ValueError``, the caller
        catches an ``IRException`` whose ``traceback_info`` is non-empty and contains
        the peer-side filename. End-to-end check that Stream 1 + Stream 2 compose.
        """
        a_transport, b_transport = _make_transport_pair()

        captured_traceback: list[str] = []

        with anyio.fail_after(_DEADLOCK_TIMEOUT):
            async with anyio.create_task_group() as tg:

                async def side_a() -> None:
                    async with FxtrScope(isolated=False, name="A") as host:
                        my_ref = await host.mount(_EchoService())
                        async with open_rpc_scope(a_transport, my_ref, name="A_rpc") as session:
                            proxy = await session.remote_service.resolve(session.scope)
                            try:
                                await proxy.boom()
                            except IRException as exc:
                                if exc.traceback_info is not None:
                                    captured_traceback.append(exc.traceback_info)

                async def side_b() -> None:
                    async with FxtrScope(isolated=False, name="B") as host:
                        my_ref = await host.mount(_RaisingService())
                        async with open_rpc_scope(b_transport, my_ref, name="B_rpc"):
                            pass

                tg.start_soon(side_a)
                tg.start_soon(side_b)

        assert captured_traceback, "expected traceback_info to be non-empty"
        joined = "\n".join(captured_traceback)
        assert "kaboom from peer service" in joined
        assert "ValueError" in joined
        assert "test_scope_bridge.py" in joined
