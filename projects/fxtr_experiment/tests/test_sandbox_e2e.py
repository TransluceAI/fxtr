"""Sandboxed bindings end to end, over real sandboxes (gated).

The real-launcher complement of ``test_sandbox_serving.py``'s fake-based
funnel coverage: `open_local(sandbox_repo=…)` → `bind_sandbox` → demand,
with the guest actually built + spawned through `fxtr_sandboxing` from a
workdir fixture repo (``_workdir_templates/``; the `fxtr_sandboxing`
greeter conventions — ``prepare_workdir_repo`` pins this repo's library
code into the fixture).

- **wasm / step-connect** (``binding_echo``): a host task's step connects
  the sandbox-bound key; the guest echoes the request's key + config and
  calls a wired dependency ref back on the host — the whole
  `BindingConnectRequest` crosses the sandbox boundary. Also proves
  release-and-forget: `shutdown_sandbox` while a guest call is in flight
  lets the call finish (the run's lease keeps the process alive).
- **container / task fn** (``binding_taskfn``): the task-fn key itself is
  sandbox-bound; the guest mounts a real
  `fxtr_experiment.task_adapter.TaskFnAdapter` and the executor's runs
  (run + safety pass) drive it over the sandbox RPC boundary.

Gated by the ``nix_runtime`` marker (skips without a configured backend;
see ``conftest.py``). The nix builds are slow on a cold cache — these are
not part of the fast core suite.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Protocol

import anyio
import pytest

from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.debug.scope_report import fail_after_and_report_hung_scopes
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method
from fxtr_workdir_harness import prepare_workdir_repo

from fxtr_experiment_task import model as m
from fxtr_experiment.bindings import wrap_singleton
from fxtr_experiment.user_service import open_local
from fxtr_experiment_task.view_runtime import step_fn

from .test_ops_engine import _some_entity
from .test_user_surface import FN_AXES, X_AXES, _bind, _fn_key

pytestmark = [pytest.mark.anyio, pytest.mark.nix_runtime]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


_TEMPLATES = Path(__file__).parent / "_workdir_templates"

#: Hang guard (a deadlock in the serving/scope plumbing otherwise parks
#: the test forever); generous because a cold-cache nix build of a
#: fixture closure is legitimately slow. Env-overridable for debug
#: iterations against a warm cache.
_E2E_TIMEOUT = int(os.environ.get("FXTR_E2E_TIMEOUT", "1200"))
_NS = uuid.UUID("9d4f7a20-6e1b-4c58-92a7-08c3e5b6d1f4")

# Wire-compatible dup of the fixture's guest-side protocol (same method
# names; the fixture module documents the pairing).
BINDING_ECHO_NS = uuid.UUID("5b8e2f61-4c9a-4d17-8e35-a02d6b9c41f7")


@service_protocol
class BindingEchoProto(Protocol):
    async def describe(self) -> str: ...
    async def ping_dep(self, name: str) -> str: ...


@service_protocol
class HostPingProto(Protocol):
    async def ping(self) -> str: ...


class HostPing(TypedServiceImpl[HostPingProto]):
    """The dependency service (host-side, ephemerally bound): answers the
    guest's calls; optionally blocks each call between two events (the
    in-flight-survives-shutdown probe)."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.entered: anyio.Event | None = None
        self.release: anyio.Event | None = None

    def _target_type_annotation(self) -> Any:
        return HostPingProto

    def _service_type_id(self) -> FxtrID:
        return system_entities.define_type(_NS, "host_ping")

    @service_method
    async def ping(self) -> str:
        self.calls += 1
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        return "pong"


def _paths(tmp_path: Path) -> tuple[str, str]:
    return str(tmp_path / "state.sqlite"), str(tmp_path / "entities.sqlite")


@pytest.mark.parametrize(
    "flake_attr",
    [
        pytest.param("fxtr.services.binding_echo", id="wasm"),
        pytest.param(
            "fxtr.services.binding_echo_pyodide",
            id="pyodide",
            marks=pytest.mark.skipif(
                shutil.which("deno") is None, reason="pyodide runtime needs deno on PATH"
            ),
        ),
    ],
)
async def test_wasm_binding_serves_step_connect(tmp_path: Path, flake_attr: str) -> None:
    """The step-connect path over a real wasm-family guest (eryx and
    pyodide — one fixture package built as both kinds): request contents
    (key + config + dep refs) cross the sandbox boundary, and shutdown
    is release-and-forget for an in-flight guest call."""
    async with fail_after_and_report_hung_scopes(_E2E_TIMEOUT):
        await _step_connect_body(tmp_path, flake_attr)


async def _step_connect_body(tmp_path: Path, flake_attr: str) -> None:
    prepared = await prepare_workdir_repo(_TEMPLATES / "binding_echo", tmp_path / "wd")
    state, ents = _paths(tmp_path)
    probed: dict[str, str] = {}

    @step_fn(type_id=system_entities.define_type(_NS, "probe_step"))
    async def probe(ctx, *, key):
        svc = await ctx.connect(key)
        echo = await svc.replace_service_type(BindingEchoProto).resolve()
        probed["describe"] = await echo.describe()
        probed["ping"] = await echo.ping_dep("pinger")
        return "probed"

    class ProbeTask:
        async def __call__(self, ctx, /, *, inp, fns):
            value = await inp.read(x="a")
            key = await fns.read_key(fn="svc")
            ckpt = await ctx.own_internals().open_checkpoint("c", axes=())
            assert await ckpt.run_step(probe, args={"key": key}, coords={}) == "probed"
            return value

    ping = HostPing()
    async with FxtrScope(name="e2e-host") as host:
        async with open_local(
            state,
            ents,
            sandbox_repo=prepared.path,
            sandbox_cache_dir=tmp_path / "sandbox_cache",
        ) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            v1 = _some_entity().store().entity_id
            await inputs.write(v1, x="a")

            # The dependency: a host ping service, ephemerally bound, its
            # key pinned by the sandboxed binding.
            dep_key = _fn_key("pinger")
            await wf.write_handle(fns, dep_key, fn="pinger")
            ping_ref = await host.mount(ping)
            await wf.bind_handle(dep_key, await wrap_singleton(host, ping_ref))

            # The sandboxed binding under test.
            svc_key = _fn_key("svc")
            await wf.write_handle(fns, svc_key, fn="svc")
            await wf.bind_sandbox(
                svc_key,
                m.SandboxDefn(git_commit=prepared.head_commit, flake_attr=flake_attr),
                deps={"pinger": fns.at(fn="pinger")},
                config={"who": "e2e"},
            )

            # The driving task (host-side, ephemeral).
            await _bind(wf, host, fns, "probe_task", ProbeTask())
            t = await wf.root.open_task(
                "t",
                axes=(),
                task_fn=fns.at(fn="probe_task"),
                args={"inp": inputs.read_only(), "fns": fns.read_only()},
            )
            await wf.add_target(t)
            assert await t.output_entity().read() == v1

            expected_config = json.dumps({"who": "e2e"}, sort_keys=True, separators=(",", ":"))
            assert probed["describe"] == (f"key={svc_key.id_string()} config={expected_config}")
            assert probed["ping"] == "pong" and ping.calls == 1

            # Release-and-forget: a second run's guest call blocks on the
            # host dep; shutdown mid-flight must not cut it.
            entered = anyio.Event()
            release = anyio.Event()
            ping.entered = entered
            ping.release = release
            t2 = await wf.root.open_task(
                "t2",
                axes=(),
                task_fn=fns.at(fn="probe_task"),
                args={"inp": inputs.read_only(), "fns": fns.read_only()},
            )
            await wf.add_target(t2)

            async with anyio.create_task_group() as tg:

                async def demand() -> None:
                    assert await t2.output_entity().read() == v1

                tg.start_soon(demand)
                await entered.wait()  # the guest call is in flight
                await wf.shutdown_sandbox(svc_key)
                release.set()
            assert probed["ping"] == "pong"

            # The cache was dropped: the next connect relaunches fresh.
            (info,) = [b for b in await wf.bindings() if b.key == svc_key]
            assert info.state == "sandbox_launchable"


async def test_container_task_fn_serves_through_executor(tmp_path: Path) -> None:
    """The executor path over a real container guest: the task-fn key is
    sandbox-bound, the guest mounts a real TaskFnAdapter, and both runs
    (main + safety pass) drive it across the sandbox RPC boundary."""
    async with fail_after_and_report_hung_scopes(_E2E_TIMEOUT):
        await _container_task_fn_body(tmp_path)


async def _container_task_fn_body(tmp_path: Path) -> None:
    prepared = await prepare_workdir_repo(_TEMPLATES / "binding_taskfn", tmp_path / "wd")
    state, ents = _paths(tmp_path)
    async with open_local(
        state,
        ents,
        sandbox_repo=prepared.path,
        sandbox_cache_dir=tmp_path / "sandbox_cache",
    ) as wf:
        fns = await wf.open_handle(wf.root, "fns", FN_AXES)
        inputs = await wf.root.open_data("inputs", X_AXES)
        v1 = _some_entity().store().entity_id
        await inputs.write(v1, x="a")

        fn_key = _fn_key("echo")
        await wf.write_handle(fns, fn_key, fn="echo")
        await wf.bind_sandbox(
            fn_key,
            m.SandboxDefn(
                git_commit=prepared.head_commit,
                flake_attr="fxtr.services.binding_taskfn",
            ),
        )

        t = await wf.root.open_task(
            "t",
            axes=(),
            task_fn=fns.at(fn="echo"),
            args={"inp": inputs.read_only()},
        )
        await wf.add_target(t)
        assert await t.output_entity().read() == v1

        (info,) = [b for b in await wf.bindings() if b.key == fn_key]
        assert info.state == "sandbox_running"

        # Complete + fresh: a later read serves without another run.
        assert await t.output_entity().read() == v1
