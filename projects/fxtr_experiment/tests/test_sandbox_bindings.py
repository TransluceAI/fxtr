"""Chunk-2 sandboxed-binding tests: the durable record + capability surface,
entirely through the public surface (`open_local` → the user capability),
plus a wire-transit check. Serving is inert this chunk, so a sandbox-bound
key still faults not-ready.
"""

import math
from pathlib import Path

import anyio
import pytest
from anyio.streams.stapled import StapledObjectStream

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope
from fxtr2.rpc.lowering_types import RPCMessage
from fxtr2.rpc.scope_bridge import open_rpc_scope

from fxtr_experiment import encoding as enc
from fxtr_experiment_task import model as m
from fxtr_experiment_task.errors import (
    CellFaultError,
    HandleAlreadyBoundError,
    SandboxDependencyError,
    UnknownBindingError,
)
from fxtr_experiment.executor import Executor
from fxtr_experiment.state_store import WorkflowSession
from fxtr_experiment.bindings import BindingHost, wrap_singleton
from fxtr_experiment_task.task_adapter import TaskFnAdapter
from fxtr_experiment.user_api import UserCapabilityProto
from fxtr_experiment.user_service import UserCapabilityImpl, Workflow, open_local

from .test_ops_engine import _some_entity

FN_AXES = (m.AxisSpec("fn", "str"),)
DEP_AXES = (m.AxisSpec("d", "str"),)
DEFN = m.SandboxDefn(git_commit="c0ffee", flake_attr="svc", flake_path=None)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _paths(tmp_path: Path) -> tuple[str, str]:
    return str(tmp_path / "state.sqlite"), str(tmp_path / "entities.sqlite")


def _key(tag: str) -> FxtrID:
    """A distinct content-addressed handle entity id per tag."""
    return (
        enc.SliceTemplateEntity(template=m.SliceTemplate(path=(), target_axes=(), output=tag))
        .store()
        .entity_id
    )


async def _handle_grid_with(wf: Workflow, name: str, cells: dict[str, FxtrID]):
    """Create a handle grid ``name`` and populate ``coord -> key`` cells."""
    grid = await wf.open_handle(wf.root, name, DEP_AXES)
    for coord, key in cells.items():
        await wf.write_handle(grid, key, d=coord)
    return grid


def _find(bindings, key: FxtrID):
    """The BindingInfo for ``key`` (or None)."""
    return next((b for b in bindings if b.key == key), None)


def _sandbox(bindings, key: FxtrID):
    """The BindingInfo for ``key``, asserted to be a sandbox row (deps /
    config live on BindingInfo itself; the sandbox detail is `.sandbox`)."""
    info = _find(bindings, key)
    assert info is not None and info.sandbox is not None
    return info


@pytest.mark.anyio
async def test_bind_sandbox_records_pins_and_survives_reopen(tmp_path: Path) -> None:
    """bind_sandbox pins each dep cell's current key, round-trips the defn +
    config, and — being durable — survives a close/reopen with the key and
    pins still loadable (the liveness invariant)."""
    state, ents = _paths(tmp_path)
    ka, kb, svc = _key("a"), _key("b"), _key("svc")
    config = {"z": 1, "a": [1, 2], "nested": {"k": True}}
    async with open_local(state, ents) as wf:
        deps = await _handle_grid_with(wf, "deps", {"a": ka, "b": kb})
        # deps map is unordered — pass "b" first to prove name-sorting.
        await wf.bind_sandbox(
            svc, DEFN, deps={"b": deps.at(d="b"), "a": deps.at(d="a")}, config=config
        )
        (info,) = [b for b in await wf.bindings() if b.key == svc]
        assert info.state == "sandbox_repo_missing"  # no repo configured
        assert info.sandbox is not None
        assert info.sandbox.defn == DEFN
        assert info.config == config
        # Deps are name-sorted; pins are the cells' current keys; attached.
        assert [d.name for d in info.deps] == ["a", "b"]
        assert [(d.pinned_key_id, d.pin_status) for d in info.deps] == [
            (ka, "attached"),
            (kb, "attached"),
        ]

    # Durable: the record (and its loadable key/pins) survive a reopen.
    async with open_local(state, ents) as wf2:
        (info,) = [b for b in await wf2.bindings() if b.key == svc]
        assert info.state == "sandbox_repo_missing"
        assert info.sandbox is not None and info.config == config
        assert [d.pinned_key_id for d in info.deps] == [ka, kb]


@pytest.mark.anyio
async def test_bind_sandbox_no_deps_no_config(tmp_path: Path) -> None:
    """A binding with no dependencies and no config is fine."""
    state, ents = _paths(tmp_path)
    async with open_local(state, ents) as wf:
        await wf.bind_sandbox(_key("svc"), DEFN)
        (info,) = await wf.bindings()
        assert info.state == "sandbox_repo_missing"
        assert info.sandbox is not None
        assert info.deps == () and info.config is None


@pytest.mark.anyio
async def test_dependency_cell_must_be_a_populated_handle_cell(tmp_path: Path) -> None:
    """Empty cell / missing grid / non-handle grid each fault at bind."""
    state, ents = _paths(tmp_path)
    async with open_local(state, ents) as wf:
        deps = await wf.open_handle(wf.root, "deps", DEP_AXES)  # no cells yet
        data = await wf.root.open_data("data", DEP_AXES)
        await data.write(_some_entity().store().entity_id, d="a")

        with pytest.raises(SandboxDependencyError):  # empty handle cell
            await wf.bind_sandbox(_key("s1"), DEFN, deps={"x": deps.at(d="a")})
        with pytest.raises(SandboxDependencyError):  # a data grid, not handle
            await wf.bind_sandbox(_key("s2"), DEFN, deps={"x": data.at(d="a")})
        assert await wf.bindings() == ()  # nothing partially bound


@pytest.mark.anyio
async def test_cross_class_exclusivity_both_directions(tmp_path: Path) -> None:
    """A key is bound in at most one class; the second registration is
    refused whichever order it comes in."""
    state, ents = _paths(tmp_path)
    k1, k2 = _key("k1"), _key("k2")
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            connector = await wrap_singleton(host, await host.mount(TaskFnAdapter(_Noop())))

            # ephemeral then sandbox
            await wf.bind_handle(k1, connector)
            with pytest.raises(HandleAlreadyBoundError):
                await wf.bind_sandbox(k1, DEFN)

            # sandbox then ephemeral
            await wf.bind_sandbox(k2, DEFN)
            with pytest.raises(HandleAlreadyBoundError):
                await wf.bind_handle(k2, connector)

            # sandbox then sandbox (the same key again)
            with pytest.raises(HandleAlreadyBoundError):
                await wf.bind_sandbox(k2, DEFN)


@pytest.mark.anyio
async def test_remove_sandbox_binding(tmp_path: Path) -> None:
    """Removal drops the record; removing an unbound key errors."""
    state, ents = _paths(tmp_path)
    svc = _key("svc")
    async with open_local(state, ents) as wf:
        await wf.bind_sandbox(svc, DEFN)
        assert _find(await wf.bindings(), svc) is not None
        await wf.remove_sandbox_binding(svc)
        assert _find(await wf.bindings(), svc) is None
        with pytest.raises(UnknownBindingError):
            await wf.remove_sandbox_binding(svc)  # already gone
        with pytest.raises(UnknownBindingError):
            await wf.remove_sandbox_binding(_key("never"))


@pytest.mark.anyio
async def test_pin_status_reflects_live_cell(tmp_path: Path) -> None:
    """A dep's pin_status tracks the live cell: attached → mismatched (the
    cell now holds a different key) → missing (the cell was deleted)."""
    state, ents = _paths(tmp_path)
    ka, kb, svc = _key("a"), _key("b"), _key("svc")
    async with open_local(state, ents) as wf:
        deps = await _handle_grid_with(wf, "deps", {"a": ka})
        await wf.bind_sandbox(svc, DEFN, deps={"dep": deps.at(d="a")})

        (dep,) = _sandbox(await wf.bindings(), svc).deps
        assert dep.pin_status == "attached" and dep.pinned_key_id == ka

        await wf.overwrite(deps, kb, d="a")  # rewire the dep cell
        (dep,) = _sandbox(await wf.bindings(), svc).deps
        assert dep.pin_status == "mismatched" and dep.pinned_key_id == ka  # pin unchanged

        await wf.delete_cell(deps, d="a")
        (dep,) = _sandbox(await wf.bindings(), svc).deps
        assert dep.pin_status == "missing"


@pytest.mark.anyio
async def test_repo_configured_state(tmp_path: Path) -> None:
    """With a sandbox repo configured, a sandbox binding is launchable;
    without, it is repo-missing (ephemeral bindings are always external)."""
    state, ents = _paths(tmp_path)
    svc = _key("svc")
    async with open_local(state, ents, sandbox_repo=tmp_path / "repo") as wf:
        await wf.bind_sandbox(svc, DEFN)
        info = _find(await wf.bindings(), svc)
        assert info is not None and info.state == "sandbox_launchable"


@pytest.mark.anyio
async def test_sandbox_bound_key_faults_not_ready(tmp_path: Path) -> None:
    """Chunk-2 records are inert: a task whose fn key is sandbox-bound faults
    not-ready on demand (serving lands in chunk 3)."""
    state, ents = _paths(tmp_path)
    svc = _key("svc")
    async with open_local(state, ents) as wf:
        fns = await wf.open_handle(wf.root, "fns", FN_AXES)
        await wf.write_handle(fns, svc, fn="svc")
        await wf.bind_sandbox(svc, DEFN)  # durable, but nothing serves it yet
        t = await wf.root.open_task("t", axes=(), task_fn=fns.at(fn="svc"), args={})
        await wf.add_target(t)  # demands require a target
        # The prologue spawn of the sandbox-bound fn key finds no server
        # (serving is inert this chunk) → HandleUnbound → interrupted.
        with pytest.raises(CellFaultError):
            await t.output_entity().read()


@pytest.mark.anyio
async def test_bind_sandbox_over_a_real_wire(tmp_path: Path) -> None:
    """The full bind_sandbox surface crosses a real RPC transport: the
    SandboxDefn arg, the ViewSel dep map, the JSON config, and the nested
    SandboxBindingInfo return all survive."""
    state, ents = _paths(tmp_path)
    a_to_b_send, a_to_b_recv = anyio.create_memory_object_stream[RPCMessage](math.inf)
    b_to_a_send, b_to_a_recv = anyio.create_memory_object_stream[RPCMessage](math.inf)
    server_transport = StapledObjectStream(a_to_b_send, b_to_a_recv)
    client_transport = StapledObjectStream(b_to_a_send, a_to_b_recv)
    client_done = anyio.Event()
    ka, svc = _key("a"), _key("svc")
    config = {"limit": 4, "url": "http://x"}

    async def server() -> None:
        async with WorkflowSession.open(state, ents) as session:
            async with FxtrScope(
                name="wf3-runner", isolated=True, include=[session.entity_scope, GLOBAL_SCOPE]
            ) as runner_scope:
                bindings = BindingHost(runner_scope, session, sandbox_repo=tmp_path / "repo")
                executor = Executor(session, bindings, runner_scope)
                cap_ref = await runner_scope.mount(
                    UserCapabilityImpl(session, executor, bindings, runner_scope)
                )
                async with open_rpc_scope(
                    server_transport, cap_ref, include=[runner_scope], name="wf3-server"
                ):
                    await client_done.wait()

    async def client() -> None:
        try:
            async with open_rpc_scope(
                client_transport, remote_service_type=UserCapabilityProto, name="wf3-client"
            ) as session:
                try:
                    proxy = await session.remote_service.resolve(session.scope)
                    wf = await Workflow.connect(proxy)
                    deps = await _handle_grid_with(wf, "deps", {"a": ka})
                    await wf.bind_sandbox(svc, DEFN, deps={"dep": deps.at(d="a")}, config=config)
                    (info,) = [b for b in await wf.bindings() if b.key == svc]
                    assert info.state == "sandbox_launchable"
                    assert info.sandbox is not None
                    assert info.sandbox.defn == DEFN and info.config == config
                    (dep,) = info.deps
                    assert dep.name == "dep" and dep.pinned_key_id == ka
                    assert dep.pin_status == "attached"
                finally:
                    client_done.set()
        finally:
            client_done.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(server)
        tg.start_soon(client)


class _Noop:
    async def __call__(self, ctx, /, **args):  # pragma: no cover - never invoked
        return None
