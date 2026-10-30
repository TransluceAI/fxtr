"""Standalone demo workstation for ``basic_test_demo``.

Boots a :class:`WorkstationServer` in-process, registers a
``delayed-echo`` service and a ``demo-note`` entity, prints the
bound URL + short_ids, and then parks until Ctrl-C.

Run via:

    uv run python projects/fxtr_workstation_agent_plugin/examples/basic_test_demo/run_demo_workstation.py

Once it's running, point the ``fxtr_workstation_mcp`` plugin at
``agent_workdir/`` (after editing ``.fxtr_workstation.toml`` to
match the printed URL).
"""

from __future__ import annotations

import argparse
import contextlib
import os
import pathlib
import signal
import sys
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import anyio
import anyio.abc
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef, get_active_scope
from fxtr2.rpc.scope_bridge import RPCSession
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method
from fxtr_sandboxing.proxy import TransportRoute
from fxtr_sandboxing.spawner import create_sandbox_environment
from fxtr_workstation import WorkstationServer, expose_entity, expose_service
from fxtr_workstation.external_services import ExternalServiceConfig
from fxtr_workstation.peer_rpc import open_workstation_rpc
from fxtr_workstation.protocols import WorkstationServiceProto


# Share the Note type + typed Protocols with the agent's demo_pkg so
# entity round-trips and typed-service dispatch work end-to-end. The
# agent_workdir's packages live in src-layout (the convention the
# MCP's sandbox bridge expects), so we add the package's src/
# directly here too.
_HERE = pathlib.Path(__file__).resolve().parent
_DEMO_PKG_SRC = _HERE / "agent_workdir" / "packages" / "demo_pkg" / "src"
sys.path.insert(0, str(_DEMO_PKG_SRC))

from demo_pkg import (  # noqa: E402
    ANTHROPIC_AUTHORIZER_NAME,
    CALLBACK_TESTER_NAME,
    DELAYED_ECHO_NAME,
    DEMO_NOTE_NAME,
    INTERNET_AUTHORIZER_NAME,
    CallbackTesterProto,
    DelayedEchoProto,
    Note,
    WorkstationSessionEventInbox,
    OPENAI_AUTHORIZER_NAME,
    SANDBOX_SPAWNER_NAME,
)

# Wheel cache for the sandbox spawner. Survives across demo runs
# so re-launches after the first don't re-download wheels.
_SANDBOX_CACHE_DIR = _HERE / ".cache" / "sandbox_wheels"


# --- Delayed-echo service -------------------------------------------


_DEMO_NS = uuid.UUID("9b5a7c12-3e8b-4d27-92f1-0a7b3c8e1d22")
DELAYED_ECHO_TYPE_ID: FxtrID = system_entities.define_type(_DEMO_NS, "delayed_echo")


class DelayedEchoImpl(TypedServiceImpl[DelayedEchoProto]):
    """Trivial impl: sleep ``delay`` seconds, return ``value``."""

    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return DelayedEchoProto

    def _service_type_id(self) -> FxtrID:
        return DELAYED_ECHO_TYPE_ID

    @service_method
    async def echo(self, value: Any, delay: float) -> Any:
        if delay > 0:
            await anyio.sleep(delay)
        return value


# --- Callback-tester service (stage 6) ------------------------------

CALLBACK_TESTER_TYPE_ID: FxtrID = system_entities.define_type(_DEMO_NS, "callback_tester")


class CallbackTesterImpl(TypedServiceImpl[CallbackTesterProto]):
    """Stage-6 demo impl.

    Carries the impl-owned :class:`FxtrScope` (where stashed
    callback refs are bridged) and the impl-owned task group
    where delayed-notify tasks run. Both are constructed in
    :func:`_callback_tester_worker` and live for the worker's
    lifetime, which is independent of the workstation's
    teardown — that's the load-bearing structural choice that
    lets the demo service drain cleanly before the workstation
    has to.
    """

    def __init__(
        self,
        impl_scope: FxtrScope,
        task_group: anyio.abc.TaskGroup,
    ) -> None:
        self._impl_scope = impl_scope
        self._task_group = task_group
        self._stored_cb: ServiceRef[WorkstationSessionEventInbox] | None = None

    def _target_type_annotation(self):  # type: ignore[no-untyped-def]
        return CallbackTesterProto

    def _service_type_id(self) -> FxtrID:
        return CALLBACK_TESTER_TYPE_ID

    @service_method
    async def register_callback(self, cb: ServiceRef[WorkstationSessionEventInbox]) -> None:
        """Bridge ``cb`` from the dispatch call_scope into the
        impl-owned scope so it survives past the per-call drain,
        then stash it. The bridge from impl_scope holds a clone
        of the lease that pins the source call_scope — that
        pin is released when impl_scope drains at demo shutdown,
        before the workstation tears down (see
        ``_callback_tester_worker``).
        """
        call_scope = get_active_scope()
        await self._impl_scope.bridge_service_from_scope(
            cb, call_scope, keepalive_via="independent_lease"
        )
        self._stored_cb = cb

    @service_method
    async def fire_notify(self, payload: Any, delay: float) -> None:
        """Fire-and-forget: schedule a background task that
        sleeps ``delay`` and then calls ``self._stored_cb.notify``.
        Returns immediately so the invoke's result event arrives
        independently of the callback's :class:`CallbackEvent`
        (the latter lands after ``delay`` seconds).

        The spawned task runs on ``self._task_group``, which is
        a child of the worker's task group rather than of any
        per-call call_scope — so it outlives the dispatch. The
        delayed-notify task resolves the bridged ref against
        ``self._impl_scope`` (where it lives) since there's no
        active scope on the background task.
        """
        if self._stored_cb is None:
            raise RuntimeError("fire_notify called before register_callback; nothing to notify")
        cb = self._stored_cb
        # The notify task outlives this dispatch and runs from a background
        # task. Base-floor authorization (design §2) covers this: while
        # ``_impl_scope`` is open its base authorizes any context, so the
        # spawned task needs no re-authorization.
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
    """Sleep, then call ``cb.notify(payload)``. ``cb`` is bridged
    into ``impl_scope`` so we resolve against it explicitly
    (background tasks have no active scope); the mount scope's live
    base authorizes us with no re-authorization needed.

    A closed callback (CloseCallbackOp ran on the workstation
    side) silently drops the notify — no :class:`CallbackEvent` is
    emitted; the test asserts the absence of the channel frame. We
    swallow exceptions so a closed-after-spawn callback doesn't
    blow up the demo task group.
    """
    try:
        await anyio.sleep(delay)
        proxy = await cb.resolve(impl_scope)
        await proxy.notify(payload)
    except Exception as e:
        # Swallow — the agent's view is "callback either fired or
        # was closed before we got a chance"; we don't need to
        # surface the demo service's exception here.
        print(f"[demo] _delayed_notify swallowed: {type(e).__name__}: {e}", flush=True)


@contextlib.asynccontextmanager
async def _callback_tester_worker(
    peer: RPCSession[WorkstationServiceProto],
    ready: anyio.Event,
) -> AsyncGenerator[None]:
    """Run the :class:`CallbackTesterImpl` in its own structured-
    concurrency scope.

    Owns:
    - an isolated mountable :class:`FxtrScope` (child of
      ``peer.scope`` so the impl can resolve workstation refs
      through it), where ``register_callback`` bridges the
      received callback ref.
    - an :func:`anyio.create_task_group` where
      ``fire_notify`` spawns ``_delayed_notify`` children.

    Sets ``ready`` once the service is registered. The async-CM
    drains (in order) the task group + the
    :func:`expose_service` registration + the impl_scope. Both
    happen before the surrounding workstation server's drain
    starts, so any bridge pin from a workstation-side
    invoke_worker call_scope is released before workstation
    teardown begins.
    """
    async with anyio.create_task_group() as task_group:
        async with FxtrScope(
            isolated=True,
            include=[peer.scope],
            name="callback_tester_impl",
        ) as impl_scope:
            impl_ref = await impl_scope.mount(CallbackTesterImpl(impl_scope, task_group))
            async with expose_service(
                peer.remote_service,
                CALLBACK_TESTER_NAME,
                impl_ref,
                impl_scope,
            ):
                ready.set()
                try:
                    yield
                finally:
                    # Cancel pending delayed-notify tasks before
                    # expose_service releases — once the
                    # registration is gone, the workstation may
                    # have already dropped its session pin on
                    # the cb, so we don't want a notify call
                    # racing the teardown.
                    task_group.cancel_scope.cancel()


# --- Main -------------------------------------------------------------


def _build_llm_authorizers(
    sandbox_env: Any,
) -> list[tuple[str, Any]]:
    """Bridge in host-specific authorizers for any LLM API whose key
    is set in the environment.

    Returns a list of ``(external_name, authorizer_ref)`` pairs the
    caller threads into ``ExternalServiceConfig``. Each authorizer
    pins the upstream URL + injects the corresponding API key as a
    header — sandboxed code can call the API through the authorizer
    without ever seeing the key.

    Prints a one-line note per registered authorizer; emits a
    warning to stderr if neither key is set (so the user knows the
    flag was a no-op).
    """
    registrations: list[tuple[str, Any]] = []

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_key:
        ref = sandbox_env.add_transport(
            TransportRoute(
                upstream_base_url="https://api.anthropic.com",
                injected_headers={
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01",
                },
                allowed_path_prefixes=None,
            )
        )
        registrations.append((ANTHROPIC_AUTHORIZER_NAME, ref))

    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        ref = sandbox_env.add_transport(
            TransportRoute(
                upstream_base_url="https://api.openai.com",
                injected_headers={"Authorization": f"Bearer {openai_key}"},
                allowed_path_prefixes=None,
            )
        )
        registrations.append((OPENAI_AUTHORIZER_NAME, ref))

    if not registrations:
        print(
            "warning: --allow-llm-apis passed but neither ANTHROPIC_API_KEY "
            "nor OPENAI_API_KEY is set; no LLM authorizers bridged in.",
            file=sys.stderr,
            flush=True,
        )

    return registrations


async def _run(allow_llm_apis: bool) -> None:
    _SANDBOX_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # ``create_sandbox_environment`` is opened *outside* the
    # ``WorkstationServer`` CM as required by the
    # ``external_services`` contract — the workstation pins
    # ``sandbox_env.host_scope`` to its own ``root_scope`` for the
    # duration of the server's lifetime, so the user scope's CM
    # can't drain until the workstation finishes shutting down.
    async with create_sandbox_environment(cache_dir=_SANDBOX_CACHE_DIR) as sandbox_env:
        # Pre-register a wide-open HTTPS passthrough authorizer.
        # Agents can bridge this into a sandboxed service's connect
        # config to give it ``get_client``-shaped internet access.
        # No host whitelist — convenient for demos, but also means
        # any sandboxed code that gets the ref can hit any public
        # host. The agent could be more conservative in production
        # by passing ``allowed_hosts`` or ``blocked_hosts``.
        internet_authorizer_ref = await sandbox_env.add_passthrough_transport()

        # Optional per-API authorizers. Each is a fixed-upstream
        # TransportRoute with the host-side API key injected as a
        # header; the sandboxed code never sees the key, only the
        # ref it can hand to ``get_client``.
        llm_authorizers: list[tuple[str, Any]] = (
            _build_llm_authorizers(sandbox_env) if allow_llm_apis else []
        )

        external_services = [
            ExternalServiceConfig(
                external_name=SANDBOX_SPAWNER_NAME,
                service=sandbox_env.spawner_ref,
                scope=sandbox_env.host_scope,
            ),
            ExternalServiceConfig(
                external_name=INTERNET_AUTHORIZER_NAME,
                service=internet_authorizer_ref,
                scope=sandbox_env.host_scope,
            ),
            *(
                ExternalServiceConfig(
                    external_name=name,
                    service=ref,
                    scope=sandbox_env.host_scope,
                )
                for name, ref in llm_authorizers
            ),
        ]
        async with WorkstationServer(
            port=8642,
            debug_ui_dist_path=WorkstationServer.bundled_debug_ui_dist_path(),
            external_services=external_services,
        ) as ws:
            async with open_workstation_rpc(ws.rpc_url, name="basic_test_demo_peer") as peer:
                echo_ref = await peer.scope.mount(DelayedEchoImpl())
                note_ref = Note(
                    title="demo-note",
                    body="Registered by run_demo_workstation.py at startup.",
                ).store(peer.scope)

                callback_tester_ready = anyio.Event()
                # The callback-tester worker runs as a nested
                # async-CM so its scopes drain *before* expose_service
                # / expose_entity / peer / workstation start to tear
                # down. Without that ordering, a callback ref bridge
                # held by the impl_scope would pin a workstation-side
                # call_scope past workstation drain → deadlock.
                async with _callback_tester_worker(peer, callback_tester_ready):
                    await callback_tester_ready.wait()
                    async with (
                        expose_service(
                            peer.remote_service,
                            DELAYED_ECHO_NAME,
                            echo_ref,
                            peer.scope,
                        ) as svc_short_id,
                        expose_entity(
                            peer.remote_service,
                            DEMO_NOTE_NAME,
                            note_ref.entity_id,
                            peer.scope,
                        ) as note_short_id,
                    ):
                        print()
                        print(f"workstation_url = {ws.bound_url}")
                        print(f"  service {DELAYED_ECHO_NAME!r} short_id = {svc_short_id}")
                        print(f"  entity  {DEMO_NOTE_NAME!r} short_id = {note_short_id}")
                        print(f"  service {CALLBACK_TESTER_NAME!r} registered")
                        print(f"  service {SANDBOX_SPAWNER_NAME!r} registered (external_services)")
                        print(
                            f"  service {INTERNET_AUTHORIZER_NAME!r} "
                            "registered (external_services; HTTPS passthrough)"
                        )
                        for name, _ref in llm_authorizers:
                            print(
                                f"  service {name!r} registered "
                                "(external_services; API key injected on host)"
                            )
                        print()
                        print(f"debug UI:  {ws.debug_http_url}")
                        print()
                        print(
                            "Point the MCP plugin at "
                            "examples/basic_test_demo/agent_workdir/ "
                            "(edit .fxtr_workstation.toml to match the URL above)."
                        )
                        print("Ctrl-C to shut down.")
                        print(flush=True)

                        with anyio.open_signal_receiver(signal.SIGINT) as signals:
                            async for sig in signals:
                                assert sig == signal.SIGINT
                                print("Ctrl+C pressed!")
                                break


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Standalone demo workstation for basic_test_demo.",
    )
    parser.add_argument(
        "--allow-llm-apis",
        action="store_true",
        help=(
            "Bridge in host-side authorizers for api.anthropic.com / "
            "api.openai.com when the corresponding ANTHROPIC_API_KEY / "
            "OPENAI_API_KEY env vars are set. Only the ones whose keys "
            "are present get registered; a warning is printed if you "
            "pass the flag with neither set."
        ),
    )
    args = parser.parse_args()

    anyio.run(_run, args.allow_llm_apis)


if __name__ == "__main__":
    main()
