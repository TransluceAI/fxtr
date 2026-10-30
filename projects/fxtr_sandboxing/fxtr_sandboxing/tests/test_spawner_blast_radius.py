"""Regression test for the spawner's blast radius (stage-13
teardown-crash issue 2 — see
``v2/plans/phase3/stage13_output_channels/teardown_crash_investigation.md``).

One spawned sandbox's RPC connection aborting must terminate *that
spawn*, not collapse the whole sandbox environment. Before the fix
(``_contain_failures_after_started`` in ``spawner.py``), an
:class:`RPCConnectionAborted` escaped ``_spawn_runner_*``'s
``open_rpc_scope`` into ``create_sandbox_environment``'s task group
and took every sibling spawn (and, in the demo server, the whole
process lifespan) down with it.

Construction: **only the sandbox itself is mocked** — ``_run_wasm``
(the spawner's single contact with eryx) is replaced with an
in-memory pipe whose far end runs a *real in-process guest*: the
adapter's own ``_GuestMainImpl`` over real ``JSONObjectStream``
framing and a real ``open_rpc_scope``, mirroring
``fxtr_sandbox_rpc_adapter._main.main``. Everything the test
exercises host-side — ``spawn()``, the runner, the wire frames, the
RPC handshake/drain, ``connect`` with provenance, the environment
task group — is unmodified production code.

The abort is then injected at the one site that *means* "the guest
aborted": the guest connection's own ``_abort(reason)`` — the
primitive every guest-side fatal path funnels through (the demo
crash's phase-1 violation calls exactly it). It emits a genuine
``AbortMessage`` through the guest's writer; the host reader's
``_on_abort`` raise and everything after it is the real failure
path. Independent of the phase-1 break/push race (issue 1 in the same
doc — no protocol violation is involved here).
"""

from __future__ import annotations

import dataclasses
import math
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, AsyncIterator, Protocol

import anyio
import fxtr2.rpc.scope_bridge as scope_bridge_module
import fxtr_sandboxing.spawner as spawner_module
import pytest
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity, EntityRef
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope, ServiceRef, get_active_scope
from fxtr2.leases.leased_cap_ir import IRException
from fxtr2.rpc.json_serdes import wrap_json_stream
from fxtr2.rpc.lowering import LeasedCapRPCConnection
from fxtr2.rpc.scope_bridge import open_rpc_scope
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method
from fxtr_sandbox_rpc_adapter._guest_main import _GuestMainImpl
from fxtr_sandbox_types.json_object_stream import JSONObjectStream
from fxtr_sandbox_types.protocols import SandboxConnectProto
from fxtr_sandboxing.deprecated.prepare import prepare
from fxtr_sandboxing.spawner import create_sandbox_environment

EXAMPLES_DIR = Path(__file__).parent.parent.parent / "examples"
CACHE_DIR = Path(__file__).parent.parent / "tmp_files_gitignore" / "wheel_cache"

# Wire-compatible mirror of `rpc_task.GreetingConfig` (same UUID +
# define_type names). The greeter itself runs IN-PROCESS here (the
# guest is mocked), so unlike test_spawner.py we also implement it.
RPC_TASK_NS = uuid.UUID("3a6b1d05-9e7f-4a82-b1c3-eef9a52d0c8b")

GREETER_TYPE_ID = system_entities.define_type(RPC_TASK_NS, "greeter")


@dataclasses.dataclass(frozen=True)
class GreetingConfig(
    DataclassEntity,
    fxtr_type=system_entities.define_type(RPC_TASK_NS, "greeting_config"),
):
    greeting: str
    punctuation: str


@service_protocol
class GreeterProto(Protocol):
    async def greet(self, name: str) -> str: ...


class _GreeterImpl(TypedServiceImpl[GreeterProto]):
    def __init__(self, cfg: GreetingConfig) -> None:
        super().__init__()
        self._cfg = cfg

    def _target_type_annotation(self) -> Any:
        return GreeterProto

    def _service_type_id(self) -> FxtrID:
        return GREETER_TYPE_ID

    @service_method
    async def greet(self, name: str) -> str:
        return f"{self._cfg.greeting}, {name}{self._cfg.punctuation}"


async def _guest_entrypoint(config: EntityRef[GreetingConfig]) -> ServiceRef[GreeterProto]:
    """The mock guest's ``fxtr.service`` entry point (rpc_task's shape)."""
    cfg = await config.load()
    return await get_active_scope().mount(_GreeterImpl(cfg))


#: Task-local marker: set inside each mock-guest task so the recording
#: connection subclass can tell guest-side connections apart from the
#: host-side ones the spawner's runners create.
_IN_MOCK_GUEST: ContextVar[bool] = ContextVar("_IN_MOCK_GUEST", default=False)


async def test_one_aborting_guest_does_not_kill_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Capture guest-side connections so the test can abort one.
    guest_conns: list[LeasedCapRPCConnection[Any, Any]] = []

    class _RecordingConn(LeasedCapRPCConnection[Any, Any]):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            if _IN_MOCK_GUEST.get():
                guest_conns.append(self)

    monkeypatch.setattr(scope_bridge_module, "LeasedCapRPCConnection", _RecordingConn)

    @asynccontextmanager
    async def fake_run_wasm(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        """Stands in for ``runner.run``: yields a TaskSession-shaped
        object whose far end is a real in-process guest (the adapter's
        main loop, minus eryx)."""
        del args, kwargs
        h2s_send, h2s_recv = anyio.create_memory_object_stream[Any](math.inf)
        s2h_send, s2h_recv = anyio.create_memory_object_stream[Any](math.inf)

        async def guest() -> None:
            _IN_MOCK_GUEST.set(True)
            # Mirror fxtr_sandbox_rpc_adapter._main.main.
            json_stream = JSONObjectStream(s2h_send.send, h2s_recv.receive)
            rpc_message_stream = wrap_json_stream(json_stream)
            try:
                async with FxtrScope(
                    isolated=True, include=[GLOBAL_SCOPE], name="mock_guest_host"
                ) as guest_host:
                    with guest_host.as_active_scope():
                        main_ref = await guest_host.mount(_GuestMainImpl(_guest_entrypoint))
                        async with open_rpc_scope(rpc_message_stream, main_ref, name="guest_rpc"):
                            pass  # served until the host disconnects (or we abort)
            except Exception:
                # A guest-side connection failure (including the abort
                # this test injects) ends the guest. It must not leak
                # into the host-side runner hosting this fake sandbox —
                # in the real world it would have died in another
                # process.
                pass

        class _FakeSession:
            async def send(self, data: Any) -> None:
                await h2s_send.send(data)

            async def receive(self) -> Any:
                return await s2h_recv.receive()

        async with anyio.create_task_group() as tg:
            tg.start_soon(guest)
            yield _FakeSession()
            # No cancel: by the time the runner exits this CM its
            # open_rpc_scope has fully closed (EOF sent), so the guest
            # winds down on its own and the task group joins it.

    monkeypatch.setattr(spawner_module, "_run_wasm", fake_run_wasm)

    async with FxtrScope():
        bundle = await prepare(EXAMPLES_DIR / "rpc_task")
        cfg_ref = GreetingConfig(greeting="hello", punctuation="!").store()

        async with (
            create_sandbox_environment(cache_dir=CACHE_DIR) as env,
            FxtrScope(include=[env.host_scope]),
        ):
            spawner = await env.spawner_ref.resolve()

            # Healthy spawn, exercised before and after the sibling dies.
            healthy_ref = (await spawner.spawn(bundle)).replace_service_type(
                SandboxConnectProto[EntityRef[GreetingConfig], GreeterProto]
            )
            healthy = await healthy_ref.resolve()
            greeter_ref = (await healthy.connect(cfg_ref)).replace_service_type(GreeterProto)
            greeter = await greeter_ref.resolve()
            assert await greeter.greet("world") == "hello, world!"

            # Doomed spawn. Resolve its ref so the spawn stays alive
            # (an unheld spawn tears down cleanly on its own).
            doomed_ref = await spawner.spawn(bundle)
            doomed = await doomed_ref.resolve()
            assert len(guest_conns) == 2

            # "The guest aborted, for whatever reason": the guest-side
            # abort primitive. Emits a real AbortMessage through the
            # guest's writer; the host reader raises from there.
            guest_conns[1]._abort(
                IRException(
                    message="injected: guest aborted the connection",
                    category="ProtocolError",
                )
            )
            await anyio.sleep(0.5)  # let the abort propagate through the spawn runner

            # One guest connection dying must be a per-spawn failure:
            # the environment and the healthy sibling keep working...
            # (Bounded: under the original bug the abort killed the
            # environment task group — including the healthy sibling's
            # runner — so this call never completed.)
            with anyio.fail_after(5):
                assert await greeter.greet("again") == "hello, again!"

            # ...while the doomed spawn's own surface degrades
            # request-scoped: its guest is gone, so calling through the
            # connect wrapper fails with an ordinary exception instead of
            # hanging or escalating.
            with anyio.fail_after(5):
                doomed_connect = await doomed_ref.replace_service_type(
                    SandboxConnectProto[EntityRef[GreetingConfig], GreeterProto]
                ).resolve()
                with pytest.raises(IRException) as excinfo:
                    await doomed_connect.connect(cfg_ref)
                assert excinfo.value.category == "LeaseBrokenError"
                assert "injected: guest aborted the connection" in excinfo.value.message
            del doomed, doomed_ref
        # ...and the environment context exits cleanly (reaching here
        # without create_sandbox_environment re-raising is the point).
