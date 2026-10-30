"""Single-process spike of the eventual SandboxSpawnerService flow.

See ``plans/phase3/stage8c_user_caps/spawner_spike/SPAWNER_SPIKE_DESIGN.md`` for design notes.

The "guest process" is just an `async` task on a memory-stream pair —
no actual sandbox isolation. We're validating the API/lifetime shape:

- An RPC-aware "entrypoint" function takes ``(config: FxtrID, serve)``
  and calls ``await serve(my_ref)`` once. ``serve`` returns when the
  host caller releases the bridged ref; the entrypoint then runs its
  cleanup and returns.

- A guest-side adapter (``guest_runtime``) owns one entrypoint and
  serves it over RPC behind a ``GuestMainImpl.connect(config)`` method.

- A host-side ``SandboxSpawnerImpl.spawn(name, config)`` looks the
  named entrypoint up in its dict, spawns a fresh guest task on its
  outer task group, opens an RPC connection, calls ``connect``, and
  threads the resulting ``ServiceRef`` back through ``bridge_from_scope``.

- Both ``connect`` and ``spawn`` use the same idiom: the foreground
  service method does ``call_scope._task_group.start(_runner, ...)``;
  the runner calls ``task_status.started((ref, source_scope))`` and
  parks on an ``anyio.Event``; the foreground bridges the ref into
  the call scope and sets the event. Lifetimes ride on the bridge
  clones — no manual lease wrangling.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, Protocol

import anyio
import anyio.abc
from anyio.streams.stapled import StapledObjectStream

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef, get_active_scope
from fxtr2.rpc.lowering_types import RPCMessage
from fxtr2.rpc.scope_bridge import open_rpc_scope
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import UntypedServiceImpl, service_method

####################################################################################################
# Protocols
####################################################################################################


# Type of the user-written entrypoint. The user calls `await serve(my_ref)`
# exactly once after mounting whatever they want to expose; serve() returns
# when the host caller releases the bridged ref, after which the entrypoint
# runs its own cleanup and returns.
EntrypointFn = Callable[
    [FxtrID, Callable[[ServiceRef[Any]], Awaitable[None]]],
    Awaitable[None],
]


@service_protocol
class EchoConfigProto(Protocol):
    """Demo protocol the user-served service implements.

    Used in the spike to verify config FxtrID round-trips and method
    calls work end-to-end.
    """

    async def echo(self, msg: str) -> str: ...

    async def get_config(self) -> FxtrID: ...


@service_protocol
class SandboxSpawnerProto(Protocol):
    """Host-side capability for spawning new sandboxes.

    ``spawn`` looks up the named entrypoint, kicks off a fresh guest
    task, opens an RPC connection, calls ``connect(config)`` on its
    main service, and returns the resulting ServiceRef.
    """

    async def spawn(self, name: str, config: FxtrID) -> ServiceRef[Any]: ...


####################################################################################################
# Bridge-back helper
####################################################################################################


async def _bridge_back_and_signal[
    T
](user_ref: ServiceRef[T], source_scope: FxtrScope, teardown_event: anyio.Event,) -> ServiceRef[T]:
    """Common foreground-side dance for both `connect` and `spawn`.

    Bridges ``user_ref`` from ``source_scope`` into the active scope,
    then fires ``teardown_event`` so the background runner can start
    its (drain-blocked) shutdown. The returned ref is what the service
    method returns — the framework auto-chain-bridges it to the caller.
    """
    call_scope = get_active_scope()
    helper = await user_ref.bridge_from_scope(source_scope, into_scope=call_scope)
    teardown_event.set()
    return helper.ref


####################################################################################################
# Guest side: entrypoint runner + GuestMainImpl + guest_runtime
####################################################################################################


class _ServeAlreadyCalledError(RuntimeError):
    pass


async def _entrypoint_runner(
    entrypoint: EntrypointFn,
    config: FxtrID,
    source_scope: FxtrScope,
    teardown_event: anyio.Event,
    *,
    task_status: anyio.abc.TaskStatus[tuple[ServiceRef[Any], FxtrScope]],
) -> None:
    """Run ``entrypoint(config, serve)`` inside an isolated user_scope.

    `serve(my_ref)`:
    - Reports `(my_ref, user_scope)` back to the foreground via
      `task_status.started`.
    - Awaits `teardown_event`, which the foreground sets right after
      it has bridged my_ref into call_scope.
    - Returns. The user's entrypoint then runs its own cleanup and
      returns. user_scope's CM exits and starts to drain — drain
      blocks on the bridge clone the foreground installed in
      call_scope, so user_scope stays alive as long as the host
      caller holds the chain-bridged ref.

    Calling `serve` a second time raises `_ServeAlreadyCalledError`.

    The runner copies ``config`` from ``source_scope`` (the connect
    call_scope) into the freshly-opened user_scope. Without this, an
    impl mounted on user_scope that *returns* ``config`` from a later
    method dispatch would crash: each method call dispatches against a
    fresh sub-scope derived from user_scope, and the encoder needs the
    returned FxtrID to be tracked there.
    """
    served = False

    async with FxtrScope(isolated=True, mountable=True, name="user_scope") as user_scope:
        # Track ``config`` in user_scope so it's reachable from any
        # dispatch scope derived from user_scope.
        await user_scope.copy_from_scope(config, source_scope)

        async def serve(my_ref: ServiceRef[Any]) -> None:
            nonlocal served
            if served:
                raise _ServeAlreadyCalledError(
                    "serve() can only be called once per entrypoint invocation"
                )
            served = True
            task_status.started((my_ref, user_scope))
            await teardown_event.wait()

        with user_scope.as_active_scope():
            await entrypoint(config, serve)


class GuestMainImpl(UntypedServiceImpl):
    """Internal guest-side main service exposed over RPC.

    One method, ``connect(config)``, runs the bridge-back idiom: starts
    the entrypoint on the call_scope's task group, waits for serve() to
    fire `task_status.started`, bridges the user's ref into call_scope,
    fires the teardown event, and returns. May be called multiple times
    on the same connection; each call gets its own entrypoint invocation
    and its own user_scope.
    """

    def __init__(self, entrypoint: EntrypointFn) -> None:
        self._entrypoint = entrypoint

    @service_method
    async def connect(self, config: FxtrID) -> ServiceRef[Any]:
        call_scope = get_active_scope()
        assert (
            call_scope._task_group is not None
        ), "connect requires a mountable call_scope (open task group)"
        teardown_event = anyio.Event()
        user_ref, user_scope = await call_scope._task_group.start(
            _entrypoint_runner,
            self._entrypoint,
            config,
            call_scope,
            teardown_event,
        )
        return await _bridge_back_and_signal(user_ref, user_scope, teardown_event)


async def guest_runtime(
    transport: anyio.abc.ObjectStream[RPCMessage],
    entrypoint: EntrypointFn,
    *,
    name: str = "guest",
) -> None:
    """Stand-in for ``fxtr_sandbox_rpc_adapter.main`` in the spike.

    Mounts a ``GuestMainImpl(entrypoint)`` on a fresh isolated host
    scope and serves it over ``open_rpc_scope`` until the peer
    disconnects. The CM body is `pass` — the conn drain is the
    "served until the host disconnects" mechanism.
    """
    async with FxtrScope(isolated=True, mountable=True, name=f"{name}_host") as guest_host:
        # Explicit activation: this task was spawned via `start_soon` from
        # the host side, which inherited the host's active-scope contextvar.
        # The host's scope is meaningless to us; we want guest_host to be
        # the active scope so `open_rpc_scope`'s eager precondition lift
        # finds `main_ref` on it.
        with guest_host.as_active_scope():
            main_ref = guest_host.mount(GuestMainImpl(entrypoint))
            async with open_rpc_scope(transport, main_ref, name=f"{name}_rpc"):
                pass


####################################################################################################
# Host side: SandboxSpawnerImpl + create_spawner
####################################################################################################


def _make_transport_pair() -> tuple[
    anyio.abc.ObjectStream[RPCMessage],
    anyio.abc.ObjectStream[RPCMessage],
]:
    """Stapled bidirectional in-memory RPCMessage transport pair."""
    a_to_b_send, a_to_b_recv = anyio.create_memory_object_stream[RPCMessage](math.inf)
    b_to_a_send, b_to_a_recv = anyio.create_memory_object_stream[RPCMessage](math.inf)
    host_end = StapledObjectStream(a_to_b_send, b_to_a_recv)
    guest_end = StapledObjectStream(b_to_a_send, a_to_b_recv)
    return host_end, guest_end


async def _spawn_runner(
    entrypoint: EntrypointFn,
    config: FxtrID,
    teardown_event: anyio.Event,
    guest_tg: anyio.abc.TaskGroup,
    *,
    task_status: anyio.abc.TaskStatus[tuple[ServiceRef[Any], FxtrScope]],
) -> None:
    """Background runner for ``SandboxSpawnerImpl.spawn``.

    Creates an in-memory transport pair, launches `guest_runtime` on
    the spawner's outer task group, opens the host-side
    `open_rpc_scope`, calls `their_main.connect(config)`, reports
    `(user_ref, session.scope)` back to the foreground, and parks on
    the teardown event.

    The session.scope's drain (on this CM exit, after the event fires)
    blocks on the bridge clone the foreground installs in call_scope —
    so the conn stays up until the host caller releases the chain-
    bridged ref. Once that happens, conn drains, transport closes,
    guest sees EOF, guest_runtime returns.
    """
    host_end, guest_end = _make_transport_pair()
    guest_tg.start_soon(guest_runtime, guest_end, entrypoint)

    async with open_rpc_scope(host_end, name="host_rpc") as session:
        guest_main = await session.remote_service.resolve(session.scope)
        # We pinned `their_main` as ServiceRef[UnknownService] (no main
        # service on this side — see SandboxSpawnerImpl.spawn). Use the
        # untyped proxy to call connect.
        user_ref: ServiceRef[Any] = await guest_main.connect(config)
        task_status.started((user_ref, session.scope))
        await teardown_event.wait()


class SandboxSpawnerImpl(TypedServiceImpl[SandboxSpawnerProto]):
    """Host-side spawner. Picks an entrypoint by name, spawns a guest,
    and threads the user ref back via the bridge-back idiom."""

    def __init__(
        self,
        entrypoints: Mapping[str, EntrypointFn],
        guest_tg: anyio.abc.TaskGroup,
    ) -> None:
        self._entrypoints = dict(entrypoints)
        self._guest_tg = guest_tg

    def _target_type_annotation(self) -> Any:
        return SandboxSpawnerProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("sandbox-spawner-spike"))

    @service_method
    async def spawn(self, name: str, config: FxtrID) -> ServiceRef[Any]:
        entrypoint = self._entrypoints.get(name)
        if entrypoint is None:
            raise KeyError(f"Unknown entrypoint {name!r}; available: {sorted(self._entrypoints)!r}")
        call_scope = get_active_scope()
        assert (
            call_scope._task_group is not None
        ), "spawn requires a mountable call_scope (open task group)"
        teardown_event = anyio.Event()
        user_ref, session_scope = await call_scope._task_group.start(
            _spawn_runner,
            entrypoint,
            config,
            teardown_event,
            self._guest_tg,
        )
        return await _bridge_back_and_signal(user_ref, session_scope, teardown_event)


@contextlib.asynccontextmanager
async def create_spawner(
    entrypoints: Mapping[str, EntrypointFn],
) -> AsyncIterator[ServiceRef[SandboxSpawnerProto]]:
    """Set up a SandboxSpawnerImpl and yield a ServiceRef to it.

    The spawner's outer task group sits *outside* the host scope so
    guest tasks outlive the spawner mount. Inside the body, host_scope
    is the active scope (since `isolated=False`), so callers can
    `await spawner_ref.resolve()` against the active scope and pick up
    the local mount.

    On CM exit:
    1. host_scope drains (waits for any chain-bridges to call_scopes
       to release, which propagates teardown into all in-flight guests);
    2. guest_tg exits (waits for guest_runtime tasks to return).
    """
    async with anyio.create_task_group() as guest_tg:
        async with FxtrScope(mountable=True, name="spawner_host") as host_scope:
            impl = SandboxSpawnerImpl(entrypoints=entrypoints, guest_tg=guest_tg)
            spawner_ref = host_scope.mount(impl)
            yield spawner_ref


####################################################################################################
# Demo entrypoint + main()
####################################################################################################


class _EchoConfigImpl(TypedServiceImpl[EchoConfigProto]):
    def __init__(self, config: FxtrID) -> None:
        self._config = config

    def _target_type_annotation(self) -> Any:
        return EchoConfigProto

    def _service_type_id(self) -> FxtrID:
        return get_active_scope().track_builtin(FxtrID.builtin("echo-config-spike"))

    @service_method
    async def echo(self, msg: str) -> str:
        return msg

    @service_method
    async def get_config(self) -> FxtrID:
        return self._config


async def echo_entrypoint(
    config: FxtrID,
    serve: Callable[[ServiceRef[Any]], Awaitable[None]],
) -> None:
    """Reference user-written entrypoint.

    Mounts an EchoConfigImpl on the active (user) scope, hands the ref
    to `serve`, and returns when serve does. Any cleanup the user wants
    to do post-serve goes after the `await serve(...)` line.
    """
    my_ref = get_active_scope().mount(_EchoConfigImpl(config))
    await serve(my_ref)
    # post-serve cleanup would go here. For the spike there's nothing
    # to clean up — the scope teardown handles everything.


async def _exercise_one_session(
    spawner: SandboxSpawnerProto,
    config: FxtrID,
    *,
    expected_msg: str,
) -> None:
    """Spawn a guest under a fresh sub-scope, exercise it, exit.

    Wrapping each session in its own sub-scope is what makes
    teardown-between-sessions observable: when the sub-scope exits, the
    chain-bridge in it is the only thing pinning the entire stack
    (call_scope → session_scope → guest's connect call_scope →
    user_scope), so the whole tower folds up.
    """
    async with FxtrScope(mountable=True, name="session"):
        # The session scope is now the active scope (isolated=False default).
        user_ref_any = await spawner.spawn("echo", config)
        # Retype to the protocol we know the guest serves.
        user_ref = user_ref_any.replace_service_type(EchoConfigProto)
        user = await user_ref.resolve()
        echoed = await user.echo(expected_msg)
        assert echoed == expected_msg, f"echo mismatch: {echoed!r} != {expected_msg!r}"
        got_config = await user.get_config()
        assert got_config == config, f"config mismatch: {got_config!r} != {config!r}"
        print(f"  session ok: echo={echoed!r}, config={got_config}")
    # session_scope has now drained — by the time control returns here,
    # the chain bridge to spawn's call_scope has released, the entire
    # guest stack should be tearing down.


async def main() -> None:
    entrypoints: dict[str, EntrypointFn] = {"echo": echo_entrypoint}

    async with FxtrScope(mountable=True, name="root") as root:
        # Two distinct configs across two back-to-back sessions, to
        # confirm the second spawn works after the first has fully
        # torn down.
        config_a = root.track_builtin(FxtrID.builtin("spike-config-A"))
        config_b = root.track_builtin(FxtrID.builtin("spike-config-B"))

        async with create_spawner(entrypoints) as spawner_ref:
            spawner = await spawner_ref.resolve()
            print("session 1:")
            await _exercise_one_session(spawner, config_a, expected_msg="hello")
            print("session 2:")
            await _exercise_one_session(spawner, config_b, expected_msg="world")
        print("create_spawner exited cleanly")


if __name__ == "__main__":
    anyio.run(main)
