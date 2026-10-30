"""The monitoring introspection surface (``notes/monitoring_ui.md``):
``summarize`` rollups + verdicts, ``inspect_generation`` (per-edge
current-match flags, failures, unanswered requests, checkpoint entries),
``list_observers`` (reverse edges with status/currency), ``delete_cell``
(the settle-event semantics + the pre-delete grounding mark),
``list_bindings`` + handle-cell boundness, and the ``config_id`` exposure
on ``list_keys``.

Reuses ``test_user_surface.py``'s harness (``_local_runner`` /
``open_local``, ``_bind`` + ``EchoTask`` for real runs) and
``test_executor_steps.py``'s step idiom for the checkpoint half of the
generation report.
"""

import uuid
from pathlib import Path

import pytest

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope
from fxtr2.data.system_entities import define_type

from fxtr_experiment import encoding as enc
from fxtr_experiment_task import model as m
from fxtr_experiment_task.errors import CellFaultError, ViewAxisError
from fxtr_experiment.executor import Executor
from fxtr_experiment.state_store import WorkflowSession
from fxtr_experiment.bindings import BindingHost
from fxtr_experiment.user_api import CellLocation, UserCapabilityProto
from fxtr_experiment.user_service import UserCapabilityImpl, Workflow, open_local
from fxtr_experiment_task.view_runtime import step_fn

from .test_user_surface import (
    FN_AXES,
    X_AXES,
    EchoTask,
    _bind,
    _local_runner,
    _paths,
)

_TEST_NS = uuid.UUID("6f2b8d1a-3c5e-4f70-8b92-1d4a6c8e0f25")


def _value(tag: str) -> FxtrID:
    """A distinct content-addressed entity id per tag."""
    return (
        enc.GridConfigEntity(config=m.DataGridConfig(axes=(m.AxisSpec(tag, "str"),)))
        .store()
        .entity_id
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# -- 1. summarize: rollups, verdicts, axes/config exposure, repeatability -------


@pytest.mark.anyio
async def test_summarize_rollups_and_verdicts(tmp_path: Path) -> None:
    """One completed-then-staled pipeline: the summary carries per-grid
    kinds/axes/config ids, population and status counts, the stale tally
    (force-evaluated), sourcing statuses, and target counts — and a repeat
    call serves identically (the stamps are the memo)."""
    state, ents = _paths(tmp_path)
    task = EchoTask()
    async with FxtrScope(name="test-host") as host:
        async with _local_runner(state, ents) as (wf, session):
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            await inputs.write(_value("v1"), x="a")
            await inputs.write(_value("w"), x="b")
            await _bind(wf, host, fns, "echo", task)
            t = await wf.root.open_task(
                "t", axes=(), task_fn=fns.at(fn="echo"), args={"inp": inputs.read_only()}
            )
            await wf.add_target(t)
            await t.output_entity().read()  # complete + fresh
            await wf.overwrite(inputs, _value("v2"), x="a")  # → t stale

            summary = await wf.summarize()
            rows = {g.path: g for g in summary.grids}
            assert set(rows) == {
                enc.encode_key_path(["fns"]),
                enc.encode_key_path(["inputs"]),
                enc.encode_key_path(["t"]),
            }

            inp_row = rows[enc.encode_key_path(["inputs"])]
            assert inp_row.kind == "data" and inp_row.axes == X_AXES
            assert inp_row.populated == 2
            assert inp_row.sourcing == "attached"
            assert inp_row.grounding == "grounded"
            grid = await session.get_grid(enc.encode_key_path(["inputs"]))
            assert grid is not None and inp_row.config_id == grid[1]

            t_row = rows[enc.encode_key_path(["t"])]
            assert t_row.kind == "task" and t_row.axes == ()
            assert t_row.populated == 1 and t_row.n_complete == 1
            assert t_row.n_running == 0 and t_row.n_errored == 0
            assert t_row.n_stale == 1 and t_row.n_unsettled == 0
            assert t_row.targets_attached == 1 and t_row.targets_dangling == 0

            fns_row = rows[enc.encode_key_path(["fns"])]
            assert fns_row.kind == "handle" and fns_row.populated == 1

            # Repeatable: the second sweep serves the same snapshot from
            # the stamps written by the first.
            assert (await wf.summarize()).grids == summary.grids


@pytest.mark.anyio
async def test_summarize_unsourced_grid_and_target_drift(tmp_path: Path) -> None:
    """The summary's intent columns: a revoked grid reads sourcing=None
    (and its facts go ungrounded); a target pinned before a
    delete-and-recreate config drift counts as dangling on the new row."""
    state, ents = _paths(tmp_path)
    async with open_local(state, ents) as wf:
        d = await wf.root.open_data("d", X_AXES)
        await d.write(_value("v"), x="a")
        await wf.add_target(d, x="a")
        await wf.revoke_grid_sourcing(wf.root, "d", force=True)  # sweeps the target too
        d_path = enc.encode_key_path(["d"])
        (row,) = (await wf.summarize()).grids
        assert row.path == d_path
        assert row.sourcing is None and row.grounding == "orphaned"
        assert row.targets_attached == 0 and row.targets_dangling == 0

        # Target drift: re-source + target, then delete and recreate with
        # different axes — the record survives, pinned to the old config.
        await wf.source_grid(wf.root, "d")
        await wf.add_target(d, x="a")
        assert await wf.delete_cell(d, x="a") is True  # rmdir needs empty
        await wf.delete_grid(wf.root, "d")
        await wf.root.open_data("d", (m.AxisSpec("y", "str"),))
        (row2,) = (await wf.summarize()).grids
        assert row2.targets_attached == 0 and row2.targets_dangling == 1


# -- 2. inspect_generation: edges, failures, unanswered requests, steps ---------


@pytest.mark.anyio
async def test_generation_report_per_edge_current_match(tmp_path: Path) -> None:
    """The 'which input moved' view: after a clean run every edge reads
    ``match``; an input overwrite flips exactly that edge to ``changed``;
    deleting the cell flips it to ``missing`` — while the config edges
    stay ``match`` throughout."""
    state, ents = _paths(tmp_path)
    task = EchoTask()
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            v1 = _value("v1")
            await inputs.write(v1, x="a")
            await _bind(wf, host, fns, "echo", task)
            t = await wf.root.open_task(
                "t", axes=(), task_fn=fns.at(fn="echo"), args={"inp": inputs.read_only()}
            )
            await wf.add_target(t)
            await t.output_entity().read()

            inp_path = enc.encode_key_path(["inputs"])
            acoords = enc.encode_coords(("a",))

            def edge_map(report):
                return {(o.subject_path, o.subject_coords, o.kind): o for o in report.observations}

            report = await wf.inspect_generation(t)
            assert report.generation is not None and report.status == "complete"
            assert report.failures == () and report.unanswered_requests == ()
            assert report.checkpoints == ()
            edges = edge_map(report)
            # Holding ≠ observing: the rule-input grant records nothing —
            # the record is exactly the input read plus the prologue's
            # task-fn handle read (both cell_value edges).
            fns_path = enc.encode_key_path(["fns"])
            assert set(edges) == {
                (inp_path, acoords, "cell_value"),
                (fns_path, enc.encode_coords(("echo",)), "cell_value"),
            }
            read_edge = edges[(inp_path, acoords, "cell_value")]
            assert read_edge.current == "match" and read_edge.fingerprint == v1
            assert all(o.current == "match" for o in report.observations)

            await wf.overwrite(inputs, _value("v2"), x="a")
            edges = edge_map(await wf.inspect_generation(t))
            assert edges[(inp_path, acoords, "cell_value")].current == "changed"
            assert edges[(fns_path, enc.encode_coords(("echo",)), "cell_value")].current == "match"

            await wf.delete_cell(inputs, x="a")
            edges = edge_map(await wf.inspect_generation(t))
            assert edges[(inp_path, acoords, "cell_value")].current == "missing"


@pytest.mark.anyio
async def test_generation_report_failures_and_unanswered_requests(tmp_path: Path) -> None:
    """An interrupted generation's report names what went wrong and what
    it is blocked on: the fault-kind failure row, and the committed
    request edge no observation ever resolved."""
    state, ents = _paths(tmp_path)
    task = EchoTask()
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)  # cell never written
            await _bind(wf, host, fns, "echo", task)
            t = await wf.root.open_task(
                "t", axes=(), task_fn=fns.at(fn="echo"), args={"inp": inputs.read_only()}
            )
            await wf.add_target(t)
            with pytest.raises(CellFaultError):
                await t.output_entity().read()

            report = await wf.inspect_generation(t)
            assert report.status == "interrupted"
            assert any(f.kind == "fault" for f in report.failures)
            assert all(f.ts for f in report.failures)
            blocked = {(r.grid_path, r.coords, r.kind) for r in report.unanswered_requests}
            assert (
                enc.encode_key_path(["inputs"]),
                enc.encode_coords(("a",)),
                "data",
            ) in blocked

            # The empty form: a task cell with no generation at all.
            t2 = await wf.root.open_task(
                "t2", axes=(), task_fn=fns.at(fn="echo"), args={"inp": inputs.read_only()}
            )
            empty = await wf.inspect_generation(t2)
            assert empty.generation is None and empty.observations == ()


@pytest.mark.anyio
async def test_generation_report_checkpoints(tmp_path: Path) -> None:
    """The report's step half — the sanctioned browse of a task's recorded
    checkpoint history: one entry per completed step invocation, carrying
    the recorded step identity and the payload/result ids."""
    state, ents = _paths(tmp_path)
    step_type = define_type(_TEST_NS, "noted_step")

    @step_fn(type_id=step_type)
    async def noted(ctx, *, i):
        return f"done-{i}"

    class TwoSteps:
        async def __call__(self, ctx, /, *, inp):
            value = await inp.read(x="a")
            ckpt = await ctx.own_internals().open_checkpoint(
                "steps", axes=(m.AxisSpec("i", "str"),)
            )
            for i in ("1", "2"):
                await ckpt.run_step(noted, args={"i": i}, coords={"i": i})
            return value

    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            await inputs.write(_value("v"), x="a")
            await _bind(wf, host, fns, "steps", TwoSteps())
            t = await wf.root.open_task(
                "t", axes=(), task_fn=fns.at(fn="steps"), args={"inp": inputs.read_only()}
            )
            await wf.add_target(t)
            await t.output_entity().read()

            report = await wf.inspect_generation(t)
            assert report.status == "complete"
            ckpt_path = enc.encode_key_path(["t", "steps"])
            entries = {c.coords: c for c in report.checkpoints}
            assert set(entries) == {enc.encode_coords(("1",)), enc.encode_coords(("2",))}
            for entry in entries.values():
                assert entry.grid_path == ckpt_path
                assert entry.step_type_id == step_type
                assert entry.args_id is not None and entry.value_id is not None


# -- 3. list_observers: reverse edges with status/currency ----------------------


@pytest.mark.anyio
async def test_list_observers_data_cell_and_task_output(tmp_path: Path) -> None:
    """Reverse edges both ways: a data cell's observers name the task that
    read it; a task cell's observers (narrowed by output) name the
    downstream reader — each with status + currency, flipping to stale
    together after an overwrite."""
    state, ents = _paths(tmp_path)
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            await inputs.write(_value("v1"), x="a")
            await _bind(wf, host, fns, "echo", EchoTask())

            class ReadsOutput:
                async def __call__(self, ctx, /, *, inp):
                    return await inp.read()

            await _bind(wf, host, fns, "reader", ReadsOutput())
            t = await wf.root.open_task(
                "t", axes=(), task_fn=fns.at(fn="echo"), args={"inp": inputs.read_only()}
            )
            t2 = await wf.root.open_task(
                "t2", axes=(), task_fn=fns.at(fn="reader"), args={"inp": t.output()}
            )
            await wf.add_target(t2)
            await t2.output_entity().read()  # runs t then t2

            t_path = enc.encode_key_path(["t"])
            t2_path = enc.encode_key_path(["t2"])

            (obs,) = await wf.observers(inputs, x="a")
            assert (obs.grid_path, obs.coords) == (t_path, enc.encode_coords(()))
            assert obs.status == "complete" and obs.currency == "fresh"

            (obs2,) = await wf.observers(t, output="result")
            assert obs2.grid_path == t2_path
            assert obs2.status == "complete" and obs2.currency == "fresh"

            # A subject nobody observed lists empty.
            assert await wf.observers(inputs, x="b") == ()

            await wf.overwrite(inputs, _value("v2"), x="a")
            (obs,) = await wf.observers(inputs, x="a")
            assert obs.currency == "stale"
            (obs2,) = await wf.observers(t, output="result")
            assert obs2.currency == "unsettled"  # support went stale, not settled


# -- 4. delete_cell: the settle event ------------------------------------------


@pytest.mark.anyio
async def test_delete_cell_semantics(tmp_path: Path) -> None:
    """A delete settles the subject on 'missing': the reader flips stale
    (currency) and loses its grounding chain (the pre-delete mark is what
    reaches the reader — a stale stamp would still read grounded); the
    sourcing record dangles; the second delete is a no-op; and task cells
    refuse."""
    state, ents = _paths(tmp_path)
    task = EchoTask()
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            await inputs.write(_value("v1"), x="a")
            await _bind(wf, host, fns, "echo", task)
            t = await wf.root.open_task(
                "t", axes=(), task_fn=fns.at(fn="echo"), args={"inp": inputs.read_only()}
            )
            await wf.add_target(t)
            await t.output_entity().read()
            assert (await wf.inspect(t)).grounding == "grounded"

            assert await wf.delete_cell(inputs, x="a") is True
            assert (await wf.inspect(inputs, x="a")).value is None
            report = await wf.inspect(t)
            assert report.currency == "stale" and report.reason is not None
            # The reader's grounding chain broke with the fact it read.
            assert report.grounding == "unanchored"
            acoords = enc.encode_coords(("a",))
            by = {(s.grid_path, s.coords): s for s in await wf.sourcing()}
            assert by[(enc.encode_key_path(["inputs"]), acoords)].status == "dangling_missing"

            assert await wf.delete_cell(inputs, x="a") is False  # idempotent
            with pytest.raises(ViewAxisError, match="clear_cells"):
                await wf.delete_cell(t)


# -- 5. bindings + boundness ----------------------------------------------------


async def _connector(host: FxtrScope):
    """A minimal connector service around a fresh EchoTask adapter."""
    from fxtr_experiment.bindings import wrap_singleton
    from fxtr_experiment_task.task_adapter import TaskFnAdapter

    ref = await host.mount(TaskFnAdapter(EchoTask()))  # type: ignore[arg-type]
    return await wrap_singleton(host, ref)


def _handle_key(name: str) -> FxtrID:
    """A distinct handle entity id per name."""
    return (
        enc.SliceTemplateEntity(template=m.SliceTemplate(path=(), target_axes=(), output=name))
        .store()
        .entity_id
    )


@pytest.mark.anyio
async def test_list_bindings_and_handle_boundness(tmp_path: Path) -> None:
    """The binding introspection pair: ``bindings()`` lists the live
    bindings, each key joined to the handle cells holding it; ``inspect``
    reports a handle cell's boundness (True / False /
    None-for-non-handles); deleting the handle cell leaves the binding
    live with no holding cells (runner config, not grid state); an
    explicit release drops both readings. Manual runner assembly (the
    ``test_user_surface`` release idiom) so the release capability can be
    resolved in the runner scope."""
    state, ents = _paths(tmp_path)
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
                data = await wf.root.open_data("d", X_AXES)
                await data.write(_value("v"), x="a")
                assert await wf.bindings() == ()

                # A written-but-unbound handle cell reads bound=False;
                # data cells always read bound=None.
                key_solo = _handle_key("solo")
                await wf.write_handle(fns, key_solo, fn="solo")
                assert (await wf.inspect(fns, fn="solo")).bound is False
                assert (await wf.inspect(data, x="a")).bound is None

                release_ref = await wf.bind_handle(key_solo, await _connector(host))
                (binding,) = await wf.bindings()
                assert binding.key == key_solo
                # The grid-side join: the one handle cell holding the key.
                assert binding.cells == (
                    CellLocation(
                        grid_path=enc.encode_key_path(["fns"]),
                        coords=enc.encode_coords(("solo",)),
                    ),
                )
                report = await wf.inspect(fns, fn="solo")
                assert report.value == key_solo and report.bound is True

                # Deleting the cell does NOT release the binding — the
                # binding keys the handle entity, not the grid cell —
                # but the join now finds no holding cells.
                assert await wf.delete_cell(fns, fn="solo") is True
                (binding,) = await wf.bindings()
                assert binding.key == key_solo and binding.cells == ()
                assert (await wf.inspect(fns, fn="solo")).bound is None  # empty

                release = await release_ref.resolve(runner_scope)
                await release.release()
                assert await wf.bindings() == ()


# -- 6. config_id exposure on list_keys ------------------------------------------


@pytest.mark.anyio
async def test_list_keys_config_id(tmp_path: Path) -> None:
    """``list_keys`` serves each child's canonical config entity id — the
    UI's dispatch handle onto stored rules/schemas."""
    state, ents = _paths(tmp_path)
    async with _local_runner(state, ents) as (wf, session):
        await wf.root.open_data("d", X_AXES)
        (info,) = await wf.list_keys(wf.root)
        grid = await session.get_grid(enc.encode_key_path(["d"]))
        assert grid is not None and info.config_id == grid[1]
