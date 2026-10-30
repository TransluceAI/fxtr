"""User-surface tests: the walking-skeleton slice entirely through the
public surface (`user_service.open_local` → shared views over the user
capability → bind → open_task → demand), target lifecycle across sessions
(persistence, dangling statuses, the per-slice uniqueness conflict),
inspect-with-verdict on stale results, clear → fresh generation, presence
queries, and the sub-workflow test (a task exercising the idempotent
user-calculus subset inside a received subtree)."""

import math
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

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
    GridNotSourcedError,
    TargetConflictError,
    ViewAxisError,
    WriteConflictError,
)
from fxtr_experiment.executor import Executor
from fxtr_experiment.state_store import WorkflowSession
from fxtr_experiment.bindings import BindingHost, wrap_singleton
from fxtr_experiment_task.task_adapter import TaskFnAdapter
from fxtr_experiment.user_api import UserCapabilityProto
from fxtr_experiment.user_service import UserCapabilityImpl, Workflow, open_local
from fxtr_experiment_task.views import (
    DataView,
    HandleView,
    SubtreeView,
    TaskView,
    int_coord,
    str_coord,
)

from .test_ops_engine import _some_entity

FN_AXES = (m.AxisSpec("fn", "str"),)
X_AXES = (m.AxisSpec("x", "str"),)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _paths(tmp_path: Path) -> tuple[str, str]:
    return str(tmp_path / "state.sqlite"), str(tmp_path / "entities.sqlite")


def _fn_key(name: str) -> FxtrID:
    """A distinct content-addressed handle entity per fn name."""
    return (
        enc.SliceTemplateEntity(template=m.SliceTemplate(path=(), target_axes=(), output=name))
        .store()
        .entity_id
    )


async def _bind(wf: Workflow, host: FxtrScope, fns, name: str, task: object) -> FxtrID:
    """Write fns[name] and bind ``task`` to it (all through the surface)."""
    key = _fn_key(name)
    await wf.write_handle(fns, key, fn=name)
    ref = await host.mount(TaskFnAdapter(task))  # type: ignore[arg-type]
    connector = await wrap_singleton(host, ref)
    await wf.bind_handle(key, connector)
    return key


class EchoTask:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, ctx, /, *, inp):
        self.calls += 1
        return await inp.read(x="a")


@pytest.mark.anyio
async def test_full_slice_end_to_end(tmp_path: Path) -> None:
    """The overview's slice, public surface only: open a Workflow, create a
    handle grid + data grid, write an input, bind a task fn, open a task
    grid wired to the input, demand it through a view read — then a target
    survives a close/reopen and run_targets serves it."""
    state, ents = _paths(tmp_path)
    task = EchoTask()
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            v1 = _some_entity().store().entity_id
            await inputs.write(v1, x="a")
            await _bind(wf, host, fns, "echo", task)
            t = await wf.root.open_task(
                "t",
                axes=(),
                task_fn=fns.at(fn="echo"),
                args={"inp": inputs.read_only()},
            )
            # User demands require an attached target (grounding's root facts).
            await wf.add_target(t)
            assert await t.output_entity().read() == v1
            assert task.calls == 2  # the one completed run: main + safety replay
            assert await t.output_entity().read() == v1  # served, no re-run
            assert task.calls == 2

            # Presence queries see what exists — kinds, axes, cells, status.
            keys = {k.key: k for k in await wf.list_keys(wf.root)}
            assert set(keys) == {"fns", "inputs", "t"}
            assert keys["inputs"].kind == "data" and keys["inputs"].axes == X_AXES
            (cell,) = await wf.list_cells(inputs)
            assert cell.coords == ("a",) and cell.status is None
            (tcell,) = await wf.list_cells(t)
            assert tcell.status == "complete" and tcell.currency == "fresh"

            await wf.add_target(t)
            (info,) = await wf.targets()
            assert info.status == "attached"

        # Reopen: the target is durable; grids re-attach; bindings are
        # process state and must be re-registered.
        async with open_local(state, ents) as wf2:
            (info,) = await wf2.targets()
            assert info.status == "attached"
            fns2 = await wf2.root.open_handle("fns", FN_AXES)  # attach (read)
            key = await fns2.read_key(fn="echo")
            ref = await host.mount(TaskFnAdapter(task))  # type: ignore[arg-type]
            connector = await wrap_singleton(host, ref)
            await wf2.bind_handle(key, connector)
            assert await wf2.run_targets() == ()  # nothing dangling
            # Complete + fresh across the reopen: served, not re-run.
            assert task.calls == 2


@pytest.mark.anyio
async def test_recursive_delete_grid_over_the_wire(tmp_path: Path) -> None:
    """The ``recursive`` flag through the capability: a subtree with a
    child grid can't be deleted rmdir-style, but recursive delete takes
    the whole subtree (the mechanism a run task grid needs for its
    read-only internals)."""
    state, ents = _paths(tmp_path)
    async with open_local(state, ents) as wf:
        sub = await wf.root.open_subtree("sub", axes=())
        leaf = await sub.open_data("leaf", X_AXES)
        await leaf.write(_some_entity().store().entity_id, x="a")
        # Recursive delete over the wire removes the subtree and its child.
        await wf.delete(wf.root, "sub", recursive=True)
        paths = {g.path for g in (await wf.summarize()).grids}
        assert enc.encode_key_path(["sub"]) not in paths
        assert enc.encode_key_path(["sub", "leaf"]) not in paths


@pytest.mark.anyio
async def test_target_dangling_statuses_and_uniqueness(tmp_path: Path) -> None:
    """Dangling lifecycle: delete → dangling_missing; recreate with a
    different config → dangling_mismatched + add_target conflicts on the
    same slice identity; remove (matching slice identity across drift) +
    re-add → attached."""
    state, ents = _paths(tmp_path)
    async with open_local(state, ents) as wf:
        aux = await wf.root.open_data("aux", (m.AxisSpec("k", "str"),))
        v = _some_entity().store().entity_id
        await aux.write(v, k="z")
        await wf.add_target(aux, k="z")
        (info,) = await wf.targets()
        assert info.status == "attached"
        # Locations are absolute and part of the record's identity, so
        # they are served for dangling records too (below).
        assert info.grid_path == enc.encode_key_path(["aux"])
        assert info.coords == enc.encode_coords(("z",))

        await wf.delete(wf.root, "aux")
        (info,) = await wf.targets()
        assert info.status == "dangling_missing"
        assert info.grid_path == enc.encode_key_path(["aux"])
        assert await wf.run_targets() == (info,)  # reported, never demanded

        # Recreate at the same key with a different canonical config (axis
        # names are identity-bearing): the old record dangles as
        # mismatched, and the slice identity is already taken.
        aux2 = await wf.root.open_data("aux", (m.AxisSpec("j", "str"),))
        (info,) = await wf.targets()
        assert info.status == "dangling_mismatched"
        with pytest.raises(TargetConflictError):
            await wf.add_target(aux2, j="z")  # same slice identity, new config

        await wf.remove_target(aux2, j="z")  # matches across config drift
        assert await wf.targets() == ()
        await aux2.write(v, j="z")
        await wf.add_target(aux2, j="z")
        (info,) = await wf.targets()
        assert info.status == "attached"
        # demand_target serves the data cell's value.
        assert dict(await wf.capability.demand_target(info.target)) == {"": v}


@pytest.mark.anyio
async def test_inspect_stale_then_clear_recomputes(tmp_path: Path) -> None:
    """The stale-introspection channel: after an overwrite, reads fault but
    ``inspect`` serves the old results labeled stale (one snapshot);
    ``clear`` + re-demand converges on the new value."""
    state, ents = _paths(tmp_path)
    task = EchoTask()
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            v1 = _some_entity().store().entity_id
            await inputs.write(v1, x="a")
            await _bind(wf, host, fns, "echo", task)
            t = await wf.root.open_task(
                "t", axes=(), task_fn=fns.at(fn="echo"), args={"inp": inputs.read_only()}
            )
            await wf.add_target(t)  # user demands require an attached target
            assert await t.output_entity().read() == v1

            v2 = (
                enc.SliceTemplateEntity(
                    template=m.SliceTemplate(path=(), target_axes=(), output="v2")
                )
                .store()
                .entity_id
            )
            # A plain write conflicts; overwrite is the privileged replace.
            with pytest.raises(WriteConflictError):
                await inputs.write(v2, x="a")
            await wf.overwrite(inputs, v2, x="a")

            # Reads fault stale; inspect serves the old value, labeled.
            with pytest.raises(CellFaultError, match="stale"):
                await t.output_entity().read()
            report = await wf.inspect(t)
            assert report.status == "complete" and report.currency == "stale"
            assert report.reason is not None and "different value" in report.reason
            assert report.results is not None and dict(report.results) == {"result": v1}
            # Data cells inspect too: current value, no verdict machinery.
            dreport = await wf.inspect(inputs, x="a")
            assert dreport.value == v2 and dreport.currency is None

            await wf.clear(t)
            assert await wf.inspect(t) == type(report)()  # empty report
            assert await t.output_entity().read() == v2
            report = await wf.inspect(t)
            assert report.currency == "fresh" and report.reason is None


class OuterWorkflowTask:
    """Runs a whole inner pipeline inside its received subtree: creates a
    data grid, copies its seed input there, opens an inner task wired to
    it (fn resolved through the *granted* handle view — tasks never mint
    authority), and returns the inner result."""

    async def __call__(self, ctx, /, *, sub, fns, seed):
        v = await seed.read(x="a")
        data = await sub.open_data("inner_inputs", (m.AxisSpec("k", "str"),))
        await data.write(v, k="seed")
        inner = await sub.open_task(
            "inner", axes=(), task_fn=fns.at(fn="inner"), args={"inp": data.read_only()}
        )
        return await inner.output_entity().read()


class InnerEchoTask:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, ctx, /, *, inp):
        self.calls += 1
        return await inp.read(k="seed")


@pytest.mark.anyio
async def test_sub_workflow_task_builds_and_demands_inside_subtree(tmp_path: Path) -> None:
    """Tasks can exercise the idempotent user-calculus subset themselves:
    the outer task builds a data grid + inner task inside its received
    subtree and demands the inner result transitively (the demand hook),
    all surviving the safety-pass replay."""
    state, ents = _paths(tmp_path)
    outer, inner = OuterWorkflowTask(), InnerEchoTask()
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            v1 = _some_entity().store().entity_id
            await inputs.write(v1, x="a")
            await _bind(wf, host, fns, "outer", outer)
            await _bind(wf, host, fns, "inner", inner)
            sub = await wf.root.open_subtree("sub", axes=())
            t = await wf.root.open_task(
                "outer_t",
                axes=(),
                task_fn=fns.at(fn="outer"),
                args={"sub": sub, "fns": fns.read_only(), "seed": inputs.read_only()},
            )
            await wf.add_target(t)  # the top-level driver; inner reads cascade
            assert await t.output_entity().read() == v1
            assert inner.calls == 2  # inner's one completed run: main + safety
            # The inner pipeline is real, visible durable state.
            keys = {k.key for k in await wf.list_keys(sub)}
            assert keys == {"inner_inputs", "inner"}


@pytest.mark.anyio
async def test_task_internals_ops_fail_intentionally_at_the_top_level(tmp_path: Path) -> None:
    """The user view-backend adapter gives the deliberately-absent
    task-internals ops intentional errors (not raw unknown-method
    failures): checkpoints and steps belong to task internals."""
    state, ents = _paths(tmp_path)
    async with open_local(state, ents) as wf:
        with pytest.raises(ViewAxisError, match="task internals"):
            await wf.root.open_checkpoint("c", axes=())


@pytest.mark.anyio
async def test_rpc_smoke_user_capability_over_a_real_wire(tmp_path: Path) -> None:
    """The stretch proof: nothing cursed is needed for the wire to just
    work. The server side assembles the runner (open_local's internals)
    and exposes the capability over a real RPC transport; the client side
    resolves the peer's ref, wraps it in :class:`Workflow`, and runs a
    small create → write → read → introspect flow, unable to tell it isn't
    in-process."""
    state, ents = _paths(tmp_path)
    a_to_b_send, a_to_b_recv = anyio.create_memory_object_stream[RPCMessage](math.inf)
    b_to_a_send, b_to_a_recv = anyio.create_memory_object_stream[RPCMessage](math.inf)
    server_transport = StapledObjectStream(a_to_b_send, b_to_a_recv)
    client_transport = StapledObjectStream(b_to_a_send, a_to_b_recv)
    client_done = anyio.Event()

    async def server() -> None:
        async with WorkflowSession.open(state, ents) as session:
            async with FxtrScope(
                name="wf3-runner", isolated=True, include=[session.entity_scope, GLOBAL_SCOPE]
            ) as runner_scope:
                bindings = BindingHost(runner_scope, session)
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
                    data = await wf.root.open_data("d", X_AXES)
                    v = _some_entity().store().entity_id  # a client-side value
                    await data.write(v, x="a")
                    assert await data.read(x="a") == v  # round-trips the wire
                    (cell,) = await wf.list_cells(data)
                    assert cell.coords == ("a",)
                    report = await wf.inspect(data, x="a")
                    assert report.value == v
                    # The chunk-3 frame ops over the same wire: IPC bytes
                    # cross the real transport both ways; the write is
                    # validated + canonicalized server-side (canonical row
                    # order comes back, not the written order).
                    from fxtr_experiment_task import tables

                    frame_schema = (m.ColumnSpec("k", "str"), m.ColumnSpec("n", "int"))
                    frames = await wf.root.open_dataframe("fr", X_AXES, frame_schema)
                    await frames.write_table(
                        tables.make_frame(frame_schema, [("b", 2), ("a", 1)]), x="a"
                    )
                    assert (await frames.read_table(x="a")).rows() == [("a", 1), ("b", 2)]
                    # ... and the chunk-4 gather (key table out, stitched
                    # stream back, local join) over the same wire.
                    drive = tables.make_frame((m.ColumnSpec("x", "str"),), [("a",)])
                    joined = await frames.gather(drive, on={"x": "x"})
                    assert joined.rows() == [("a", "a", 1), ("a", "b", 2)]
                    # The chunk-4 sourcing surface over the same wire — the
                    # create auto-sourced "d"; source_grid re-adopts; and
                    # revoke exercises a keyword arg (force=) on a new proto
                    # method (kwargs must survive the real transport).
                    d_path = enc.encode_key_path(["d"])
                    assert (d_path, "") in {(s.grid_path, s.coords) for s in await wf.sourcing()}
                    await wf.source_grid(wf.root, "d")  # idempotent re-adopt
                    assert await wf.revoke_grid_sourcing(wf.root, "d", force=True) is True
                    assert not any(s.grid_path == d_path for s in await wf.sourcing())
                    # The chunk-5 linter over the same wire: the revocation
                    # just orphaned "d" and its cell, and the nested report
                    # dataclasses survive the transport.
                    lint = await wf.lint()
                    assert lint.sound and not lint.clean
                    assert {(u.kind, u.grid_path) for u in lint.ungrounded} == {
                        ("grid", d_path),
                        ("data_cell", d_path),
                    }
                    # open_task wiring crosses the real wire too — including
                    # the token-less WiredCoord variants beside a WiredArg
                    # (the args union decode) — and needs no run: it only
                    # canonicalizes and stores the config.
                    fns = await wf.open_handle(wf.root, "fns", FN_AXES)
                    await wf.write_handle(fns, _fn_key("f"), fn="f")
                    t = await wf.root.open_task(
                        "t",
                        axes=(m.AxisSpec("i", "int"),),
                        task_fn=fns.at(fn="f"),
                        args={
                            "idx": int_coord("i"),  # consumed
                            "j": int_coord("j"),  # kept
                            "c": str_coord("z").at(z="k"),  # literal
                            "d": data.read_only(),  # a WiredArg beside them
                        },
                    )
                    assert t.axes == (m.AxisSpec("i", "int"),)
                    # The monitoring introspection over the same wire: the
                    # summary's nested rows (axes, config ids, counts)
                    # survive the transport; inspect_generation's empty
                    # form decodes; observers/bindings are empty tuples;
                    # delete_cell round-trips a bool.
                    rows = {g.path: g for g in (await wf.summarize()).grids}
                    assert rows[d_path].kind == "data" and rows[d_path].axes == X_AXES
                    assert rows[d_path].populated == 1
                    assert rows[d_path].sourcing is None  # revoked above
                    assert rows[d_path].config_id is not None
                    assert rows[enc.encode_key_path(["t"])].populated == 0
                    empty = await wf.inspect_generation(t, i=1)
                    assert empty.generation is None and empty.observations == ()
                    assert await wf.observers(data, x="a") == ()
                    assert await wf.bindings() == ()
                    assert await wf.delete_cell(data, x="a") is True
                    assert (await wf.inspect(data, x="a")).value is None
                    # The watch stream over the same wire: a
                    # client-mounted subscriber attaches through the
                    # standard EventSource-shaped contract, gets the
                    # snapshot batch, and a subsequent mutation arrives
                    # as a typed diff.
                    from fxtr_experiment.watch_api import WorkflowWatchEvent

                    from .test_watches import WatchRecorder

                    watch_sub = WatchRecorder(WorkflowWatchEvent)
                    watch_ref = await session.scope.mount(watch_sub)
                    unsub_ref = await wf.watch_workflow(watch_ref)
                    await watch_sub.wait_for(lambda es: bool([e for e in es if e.kind == "batch"]))
                    await wf.source_grid(wf.root, "d")  # re-adopt: intent
                    await watch_sub.wait_for(
                        lambda es: any(
                            any(r.grid_path == d_path for r in p.records)
                            for p in watch_sub.patches("sourcing", skip_batches=1)
                        )
                    )
                    # End the subscription BEFORE this scope drains: a live
                    # subscription cross-pins the two sides (the server's
                    # sub_scope holds our subscriber's bridge; we hold its
                    # unsubscribe handle), and a graceful drain parks on
                    # that cycle forever — the same hazard the workstation
                    # inbox parker handles with unsubscribe-then-force-close
                    # (see fxtr_workstation/callback_impl.py's module doc).
                    unsub = await unsub_ref.resolve(session.scope)
                    await unsub.unsubscribe()
                finally:
                    # Unblock the server BEFORE this side's rpc-scope exit:
                    # each side's connection drain waits for the peer to
                    # release its grant exports at the peer's own exit, so
                    # both sides must drain concurrently (see
                    # v2/notes/scope_service_lifetimes.md) — a server gated
                    # on the client's *completed* exit is a cross-wait cycle.
                    client_done.set()
        finally:
            client_done.set()  # belt-and-braces for pre-connection failures

    with anyio.fail_after(30):
        async with anyio.create_task_group() as tg:
            tg.start_soon(server)
            tg.start_soon(client)


@pytest.mark.anyio
async def test_attach_reconstructs_views_after_reopen(tmp_path: Path) -> None:
    """attach: after a reopen, everything ``list_keys`` shows is reachable
    from stored configs alone — no re-supplied axes/schemas, no rule
    reconstruction for the task grid; a missing key faults."""
    state, ents = _paths(tmp_path)
    task = EchoTask()
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            v1 = _some_entity().store().entity_id
            await inputs.write(v1, x="a")
            await _bind(wf, host, fns, "echo", task)
            t = await wf.root.open_task(
                "t", axes=(), task_fn=fns.at(fn="echo"), args={"inp": inputs.read_only()}
            )
            await wf.add_target(t)  # durable — the target survives the reopen
            assert await t.output_entity().read() == v1

        async with open_local(state, ents) as wf2:
            keys = {k.key for k in await wf2.list_keys(wf2.root)}
            assert keys == {"fns", "inputs", "t"}
            inputs2 = await wf2.attach(wf2.root, "inputs")
            assert isinstance(inputs2, DataView)
            assert await inputs2.read(x="a") == v1
            fns2 = await wf2.attach(wf2.root, "fns")
            assert isinstance(fns2, HandleView)
            key = await fns2.read_key(fn="echo")
            ref = await host.mount(TaskFnAdapter(task))  # type: ignore[arg-type]
            connector = await wrap_singleton(host, ref)
            await wf2.bind_handle(key, connector)
            t2 = await wf2.attach(wf2.root, "t")
            assert isinstance(t2, TaskView)
            assert await t2.output_entity().read() == v1  # complete + fresh: served
            assert task.calls == 2  # never re-ran across the reopen
            # attach asserts nothing but requires existence.
            with pytest.raises(CellFaultError, match="no grid"):
                await wf2.attach(wf2.root, "ghost")
            # attach follows the chain: under the write root the attached
            # grid carries write authority, like the open.
            inputs3 = await wf2.attach(wf2.root, "inputs")
            assert isinstance(inputs3, DataView)
            await inputs3.write(v1, x="a")  # idempotent same-value write


@pytest.mark.anyio
async def test_internals_browsing_is_read_only(tmp_path: Path) -> None:
    """attach_internals: the top level can browse a task cell's private
    key-space — data grids, nested subtrees, checkpoint grids all listed;
    values readable through read-mode attaches — but never touch it:
    writes and write-mode attaches refuse, checkpoint grids list but
    don't attach."""
    from fxtr_experiment_task.view_runtime import step_fn
    from fxtr2.data.system_entities import define_type
    import uuid as _uuid

    @step_fn(type_id=define_type(_uuid.UUID("5d7e2b1a-9c3f-4a08-8b61-2f4e0c9d7a35"), "noted"))
    async def noted(ctx, *, tag):
        return f"noted-{tag}"

    class BuildsInternals:
        async def __call__(self, ctx, /, *, inp):
            value = await inp.read(x="a")
            internals = ctx.own_internals()
            scratch = await internals.open_data("scratch", (m.AxisSpec("s", "str"),))
            await scratch.write(value, s="kept")
            nested = await internals.open_subtree("nested", axes=())
            deep = await nested.open_data("deep", (m.AxisSpec("d", "str"),))
            await deep.write(value, d="down")
            ckpt = await internals.open_checkpoint("steps", axes=())
            await ckpt.run_step(noted, args={"tag": "x"}, coords={})
            return value

    state, ents = _paths(tmp_path)
    task = BuildsInternals()
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            v1 = _some_entity().store().entity_id
            await inputs.write(v1, x="a")
            await _bind(wf, host, fns, "builder", task)
            t = await wf.root.open_task(
                "t", axes=(), task_fn=fns.at(fn="builder"), args={"inp": inputs.read_only()}
            )
            await wf.add_target(t)  # user demands require an attached target
            assert await t.output_entity().read() == v1

            ints = await wf.internals(t)
            keys = {k.key: k.kind for k in await wf.list_keys(ints)}
            assert keys == {"scratch": "data", "nested": "subtree", "steps": "checkpoint"}

            scratch = await wf.attach(ints, "scratch")
            assert isinstance(scratch, DataView)
            assert await scratch.read(s="kept") == v1
            (cell,) = await wf.list_cells(scratch)
            assert cell.coords == ("kept",)
            report = await wf.inspect(scratch, s="kept")
            assert report.value == v1

            nested = await wf.attach(ints, "nested")
            assert isinstance(nested, SubtreeView)
            deep = await wf.attach(nested, "deep")  # read-mode propagates
            assert isinstance(deep, DataView)
            assert await deep.read(d="down") == v1

            # Introspection, not intervention.
            with pytest.raises(ViewAxisError, match="not writable|read-only"):
                await scratch.write(v1, s="kept")
            with pytest.raises(ViewAxisError, match="not attachable"):
                await wf.attach(ints, "steps")
            # The mode ceiling holds for opens too — a zero-axis internals
            # root is an *unsliced* read grant, so an opened child comes back
            # read-only and writes through it fault.
            scratch_ro = await ints.open_data("scratch", (m.AxisSpec("s", "str"),))
            with pytest.raises(ViewAxisError, match="not writable"):
                await scratch_ro.write(v1, s="kept")
            # open_subtree inherits the read mode (browse, not authority).
            nested2 = await ints.open_subtree("nested", axes=())
            deep2 = await nested2.open_data("deep", (m.AxisSpec("d", "str"),))
            assert await deep2.read(d="down") == v1
            with pytest.raises(ViewAxisError, match="not writable"):
                await deep2.write(v1, d="down")


@pytest.mark.anyio
async def test_grid_level_internals_and_the_walk(tmp_path: Path) -> None:
    """The grid-level browse pair: ``attach_internals`` with open
    positions is a read-sliced window spanning every cell's internals
    (kept task axes ride into the grant), and the root-frame walk —
    the grant-minting path behind the ``watch_cells`` meta — crosses
    the task boundary into internals transparently, read-mode forced
    from the boundary down (exercised here through a live grid watch:
    the served grant addresses the internals grid across every cell)."""
    from fxtr_experiment_task.errors import typed_wire_call
    from fxtr_experiment_task.runner_api import ViewSel

    class CellInternalsTask:
        async def __call__(self, ctx, /, *, inp, xdx):
            del xdx  # consumes the x axis (each run sees one cell)
            value = await inp.read()  # this cell's input (x consumed)
            internals = ctx.own_internals()
            scratch = await internals.open_data("scratch", (m.AxisSpec("s", "str"),))
            await scratch.write(value, s="kept")
            return value

    state, ents = _paths(tmp_path)
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            v1 = _some_entity().store().entity_id
            await inputs.write(v1, x="a")
            await inputs.write(v1, x="b")
            await _bind(wf, host, fns, "builder", CellInternalsTask())
            t = await wf.root.open_task(
                "t",
                axes=X_AXES,
                task_fn=fns.at(fn="builder"),
                args={"inp": inputs.read_only(), "xdx": str_coord("x")},
            )
            for x in ("a", "b"):
                await wf.add_target(t, x=x)
                assert await t.output_entity().read(x=x) == v1

            # Open positions stay kept: one window across both cells'
            # internals, cells keyed by (task axis, own axis).
            ints_all = await wf.internals(t)  # x left open
            scratch_all = await wf.attach(ints_all, "scratch")
            assert {c.coords for c in await wf.list_cells(scratch_all)} == {
                ("a", "kept"),
                ("b", "kept"),
            }
            # The bound form (one cell's key-space) is unchanged.
            ints_a = await wf.internals(t, x="a")
            scratch_a = await wf.attach(ints_a, "scratch")
            assert {c.coords for c in await wf.list_cells(scratch_a)} == {("kept",)}

            # The walk through a live grid watch: the ``watch_cells``
            # meta for the internals grid carries a grant minted root →
            # task → internals — kept task axes ride in, the boundary
            # forces read mode — and the served token addresses the
            # window across every cell (the grants-ride-the-payload
            # contract, `notes/event_system.md`).
            from fxtr_experiment.watch_api import CellsWatchEvent

            from .test_watches import WatchRecorder

            scratch_path = enc.encode_key_path(["t", "scratch"])
            sub = WatchRecorder(CellsWatchEvent)
            await wf.watch_cells(await host.mount(sub), path=scratch_path)
            await sub.wait_for(lambda es: bool([e for e in es if e.kind == "batch"]))
            meta = next(p for p in sub.batches()[0].patches if p.kind == "meta")
            assert [a.name for a in meta.grant.axes] == ["x", "s"]
            assert meta.grant.mode == "read"
            sel = ViewSel(token=meta.grant.token)
            cells = await typed_wire_call(wf.capability.list_cells(sel))
            assert {c.coords for c in cells} == {("a", "kept"), ("b", "kept")}
            (snap_rows,) = [p.cells for p in sub.batches()[0].patches if p.kind == "cells_upsert"]
            assert {c.coords for c in snap_rows} == {("a", "kept"), ("b", "kept")}


@pytest.mark.anyio
async def test_bind_release_explicit_and_auto_on_scope_close(tmp_path: Path) -> None:
    """Binding-release lifecycle: explicit ``release()`` unbinds
    immediately (rebinding after is fine); dropping the returned ref by
    closing the scope that held it auto-releases (the mount-lifespan
    teardown) — a release capability stranded in a closed scope cannot
    leave the binding stuck. An unbound task fn at demand time is a
    not-ready fault: the cell lands ``interrupted``, never ``errored``."""
    state, ents = _paths(tmp_path)
    task = EchoTask()
    async with FxtrScope(name="test-host") as host:
        async with WorkflowSession.open(state, ents) as session:
            async with FxtrScope(
                name="wf3-runner", isolated=True, include=[session.entity_scope, GLOBAL_SCOPE]
            ) as runner_scope:
                bindings = BindingHost(runner_scope, session)
                executor = Executor(session, bindings, runner_scope)
                cap_ref = await runner_scope.mount(
                    UserCapabilityImpl(session, executor, bindings, runner_scope)
                )
                proxy = await cap_ref.replace_service_type(UserCapabilityProto).resolve(
                    runner_scope
                )
                wf = await Workflow.connect(proxy)
                fns = await wf.open_handle(wf.root, "fns", FN_AXES)
                inputs = await wf.root.open_data("inputs", X_AXES)
                v1 = _some_entity().store().entity_id
                await inputs.write(v1, x="a")
                key = _fn_key("echo")
                await wf.write_handle(fns, key, fn="echo")
                t = await wf.root.open_task(
                    "t", axes=(), task_fn=fns.at(fn="echo"), args={"inp": inputs.read_only()}
                )
                await wf.add_target(t)  # user demands require an attached target
                fn_ref = await host.mount(TaskFnAdapter(task))  # type: ignore[arg-type]
                connector = await wrap_singleton(host, fn_ref)

                # Explicit release: immediate; the unbound demand is a
                # not-ready fault (interrupted, not errored).
                release_ref = await wf.bind_handle(key, connector)
                release = await release_ref.resolve(runner_scope)
                await release.release()
                await release.release()  # idempotent
                with pytest.raises(CellFaultError, match="no binding"):
                    await t.output_entity().read()
                report = await wf.inspect(t)
                assert report.status == "interrupted"
                assert report.reason is not None and "HandleUnboundError" in report.reason

                # Auto-release: bind through a proxy resolved in a
                # sub-scope; closing the sub-scope drops the caller's ref
                # bridge → the release impl's mount drains → unbind.
                async with FxtrScope(name="user-sub", isolated=True, include=[runner_scope]) as sub:
                    proxy2 = await cap_ref.replace_service_type(UserCapabilityProto).resolve(sub)
                    await proxy2.bind_handle(key, connector)
                    assert await t.output_entity().read() == v1  # bound: resumes, completes
                # The binding died with the sub-scope: clear + re-demand
                # needs the fn again and faults unbound.
                await wf.clear(t)
                with pytest.raises(CellFaultError, match="no binding"):
                    await t.output_entity().read()

                # Rebinding after an auto-release is the ordinary
                # promised-stateless flow; the cell recomputes.
                await wf.bind_handle(key, connector)
                assert await t.output_entity().read() == v1


# -- user sourcing through the surface (white-box: the sourcing surface is
# session-level, so these assemble the runner directly to reach it) -------------


@asynccontextmanager
async def _local_runner(state: str, ents: str) -> AsyncIterator[tuple[Workflow, WorkflowSession]]:
    """``open_local``'s assembly, but yielding the session alongside the
    Workflow: the sourcing surface has no wire form yet, so tests that
    inspect/adopt sourcing records reach it at the session level."""
    async with WorkflowSession.open(state, ents) as session:
        async with FxtrScope(
            name="wf3-runner", isolated=True, include=[session.entity_scope, GLOBAL_SCOPE]
        ) as runner_scope:
            bindings = BindingHost(runner_scope, session)
            executor = Executor(session, bindings, runner_scope)
            cap_ref = await runner_scope.mount(
                UserCapabilityImpl(session, executor, bindings, runner_scope)
            )
            proxy = await cap_ref.replace_service_type(UserCapabilityProto).resolve(runner_scope)
            wf = await Workflow.connect(proxy)
            yield wf, session


class MakesDataGrid:
    """Creates a data grid inside its received subtree and writes a cell — a
    task-created grid, which carries NO user sourcing record."""

    async def __call__(self, ctx, /, *, sub, seed):
        v = await seed.read(x="a")
        data = await sub.open_data("task_data", (m.AxisSpec("k", "str"),))
        await data.write(v, k="cell")
        return v


@pytest.mark.anyio
async def test_user_create_and_write_auto_source(tmp_path: Path) -> None:
    """A user create auto-sources the grid; a user write auto-sources the
    cell with the written value's id."""
    state, ents = _paths(tmp_path)
    async with _local_runner(state, ents) as (wf, session):
        data = await wf.root.open_data("d", X_AXES)
        grid = enc.encode_key_path(["d"])
        # The create left a grid-level sourcing row pinning the grid's config.
        row = await session.get_grid(grid)
        assert row is not None
        assert await session.get_user_sourcing(grid, "") == row[1]

        # A user write leaves a cell-level sourcing row pinning the value.
        v = _some_entity().store().entity_id
        await data.write(v, x="a")
        assert await session.get_user_sourcing(grid, enc.encode_coords(("a",))) == v


@pytest.mark.anyio
async def test_user_write_into_task_grid_needs_sourcing_then_adopts(tmp_path: Path) -> None:
    """A user write into a grid the user did NOT create (a task-created grid)
    faults GridNotSourcedError; sourcing the grid (adoption) then lets the
    same write through."""
    state, ents = _paths(tmp_path)
    task = MakesDataGrid()
    async with FxtrScope(name="test-host") as host:
        async with _local_runner(state, ents) as (wf, session):
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            v1 = _some_entity().store().entity_id
            await inputs.write(v1, x="a")
            await _bind(wf, host, fns, "maker", task)
            sub = await wf.root.open_subtree("sub", axes=())
            t = await wf.root.open_task(
                "t",
                axes=(),
                task_fn=fns.at(fn="maker"),
                args={"sub": sub, "seed": inputs.read_only()},
            )
            await wf.add_target(t)  # user demands require an attached target
            assert await t.output_entity().read() == v1

            # The task created sub/task_data — task grid, not user-sourced.
            data_path = enc.encode_key_path(["sub", "task_data"])
            assert await session.get_user_sourcing(data_path, "") is None

            # A user open attaches (no auto-source); a user write refuses,
            # checked BEFORE the write so no orphan value lands.
            td = await sub.open_data("task_data", (m.AxisSpec("k", "str"),))
            v2 = _fn_key("v2")  # a distinct value
            with pytest.raises(GridNotSourcedError):
                await wf.overwrite(td, v2, k="cell")

            # Adoption: source the grid explicitly, then the write succeeds
            # and auto-sources the cell.
            grid_row = await session.get_grid(data_path)
            assert grid_row is not None
            await session.upsert_user_sourcing(data_path, "", grid_row[1])
            await wf.overwrite(td, v2, k="cell")
            assert await session.get_user_sourcing(data_path, enc.encode_coords(("cell",))) == v2


@pytest.mark.anyio
async def test_add_target_into_unsourced_grid_faults(tmp_path: Path) -> None:
    """``add_target`` presupposes the grid's sourcing record (grid-before-
    cell): revoking it makes the same target attempt fault."""
    state, ents = _paths(tmp_path)
    async with _local_runner(state, ents) as (wf, session):
        data = await wf.root.open_data("d", X_AXES)
        v = _some_entity().store().entity_id
        await data.write(v, x="a")  # auto-sources grid + cell
        grid = enc.encode_key_path(["d"])
        # Drop the auto-created grid sourcing (cascade clears the cell row).
        assert await session.revoke_user_sourcing(grid, "", cascade=True) is True
        assert await session.get_user_sourcing(grid, "") is None
        with pytest.raises(GridNotSourcedError):
            await wf.add_target(data, x="a")
