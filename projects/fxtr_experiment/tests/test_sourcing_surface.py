"""The chunk-4 grounding user surface end-to-end through the wire wrapper.

Exercises the editable-root-facts API of ``notes/grounding.md`` ("maintaining
the root facts") as a user drives it: sourcing-record CRUD and the maintained
grid-before-cell invariant (with the force cascade), dangling statuses,
adoption of task-created grids (``source_grid`` / ``source_cell`` / the
``adopt=`` write), the idempotent-write re-sourcing rule, the
user-demand-requires-a-target law and its auto-target sugar, the
never-user-sourceable internals refusal, ``delete_grid``'s consequence
report, and the verdict surfacing on ``inspect`` / ``list_keys`` /
``list_targets``.

Reuses ``test_user_surface.py``'s harness (``_local_runner`` yields the
session alongside the ``Workflow`` for the white-box facets, ``_bind`` /
``EchoTask`` / ``MakesDataGrid`` for real runs) and the ``IRException``
transit convention (non-``WorkflowError`` categories — ``StateDbError`` —
surface untyped, as ``test_task_service.py`` documents).
"""

from pathlib import Path

import pytest

from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.leases.leased_cap_ir import IRException

from fxtr_experiment import encoding as enc
from fxtr_experiment_task import model as m
from fxtr_experiment_task.errors import (
    GridNotSourcedError,
    NotTargetedError,
    UngroundedSubjectError,
    ViewAxisError,
)
from fxtr_experiment.user_service import Workflow, open_local

from .test_ops_engine import _some_entity
from .test_user_surface import (
    FN_AXES,
    X_AXES,
    EchoTask,
    MakesDataGrid,
    _bind,
    _fn_key,
    _local_runner,
    _paths,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _by_subject(infos):
    """Sourcing/target infos keyed by (grid_path, coords)."""
    return {(s.grid_path, s.coords): s for s in infos}


# -- 1. Sourcing CRUD round-trip + the maintained invariant's force cascade -----


@pytest.mark.anyio
async def test_sourcing_crud_round_trip(tmp_path: Path) -> None:
    """A user-created grid + write auto-appear as attached records; a cell
    record revokes and re-adopts via ``source_cell``; and a grid revoke
    with live cell-level intent (a target) refuses (``StateDbError``, wire-
    wrapped) unless ``force`` sweeps the target + cell records together."""
    state, ents = _paths(tmp_path)
    grid = enc.encode_key_path(["d"])
    acoords = enc.encode_coords(("a",))
    async with open_local(state, ents) as wf:
        d = await wf.root.open_data("d", X_AXES)
        v = _some_entity().store().entity_id
        await d.write(v, x="a")  # auto-sources the grid + the cell

        by = _by_subject(await wf.sourcing())
        assert by[(grid, "")].status == "attached"
        assert by[(grid, acoords)].status == "attached"
        assert by[(grid, acoords)].fingerprint == v

        # Revoke the cell record: the value stays, the claim goes — the
        # now-orphaned value is inspectable (the labeled escape) but a
        # gated read refuses it under the strict default.
        assert await wf.revoke_cell_sourcing(d, x="a") is True
        assert (grid, acoords) not in _by_subject(await wf.sourcing())
        report = await wf.inspect(d, x="a")
        assert report.value == v and report.grounding == "orphaned"
        with pytest.raises(UngroundedSubjectError, match="orphaned"):
            await d.read(x="a")

        # source_cell re-adopts the current value → attached again (and
        # the gated read serves again).
        await wf.source_cell(d, x="a")
        assert _by_subject(await wf.sourcing())[(grid, acoords)].status == "attached"
        assert await d.read(x="a") == v

        # Live cell-level intent (a target) blocks the grid revoke; the
        # invariant is *maintained*, not just checked at add time.
        await wf.add_target(d, x="a")
        with pytest.raises(IRException) as ei:
            await wf.revoke_grid_sourcing(wf.root, "d")
        assert ei.value.category == "StateDbError"

        # force sweeps the target + cell records in the same transaction.
        assert await wf.revoke_grid_sourcing(wf.root, "d", force=True) is True
        assert await wf.sourcing() == ()
        assert await wf.targets() == ()


# -- 2. Dangling statuses (missing on delete; mismatched via a bogus pin) -------


@pytest.mark.anyio
async def test_sourcing_dangling_statuses(tmp_path: Path) -> None:
    """A record's fingerprint pin never silently rebinds: a value overwritten
    out from under it reads ``dangling_mismatched``; deleting the grid reads
    ``dangling_missing`` (both the grid and cell records)."""
    state, ents = _paths(tmp_path)
    grid = enc.encode_key_path(["d"])
    acoords = enc.encode_coords(("a",))
    async with _local_runner(state, ents) as (wf, session):
        d = await wf.root.open_data("d", X_AXES)
        v = _some_entity().store().entity_id
        await d.write(v, x="a")

        # Pin the cell record to a fingerprint the live value doesn't match
        # (a stand-in for config/value drift under a stale record).
        bogus = _fn_key("bogus-dangling")  # a distinct, content-addressed value
        await session.persist(bogus)  # loadable for list_sourcing's re-attach
        assert bogus != v
        await session.upsert_user_sourcing(grid, acoords, bogus)
        assert _by_subject(await wf.sourcing())[(grid, acoords)].status == "dangling_mismatched"

        # Delete the grid: the value is gone, the records dangle missing.
        await wf.delete_grid(wf.root, "d")
        by = _by_subject(await wf.sourcing())
        assert by[(grid, "")].status == "dangling_missing"
        assert by[(grid, acoords)].status == "dangling_missing"


# -- 3. adopt= write into a task-created grid records BOTH grid + cell ----------


async def _run_maker(wf, session, host):
    """Drive ``MakesDataGrid`` once: it creates ``sub/task_data`` and writes
    a ``cell`` value — a task-created grid, no user sourcing record."""
    fns = await wf.open_handle(wf.root, "fns", FN_AXES)
    inputs = await wf.root.open_data("inputs", X_AXES)
    v1 = _some_entity().store().entity_id
    await inputs.write(v1, x="a")
    await _bind(wf, host, fns, "maker", MakesDataGrid())
    sub = await wf.root.open_subtree("sub", axes=())
    t = await wf.root.open_task(
        "t", axes=(), task_fn=fns.at(fn="maker"), args={"sub": sub, "seed": inputs.read_only()}
    )
    await wf.add_target(t)
    assert await t.output_entity().read() == v1
    return sub, v1


@pytest.mark.anyio
async def test_adopt_write_into_task_grid_records_both(tmp_path: Path) -> None:
    """A user write into a task-created grid faults ``GridNotSourcedError``
    without ``adopt``; with ``adopt=True`` it sources the grid (pinned to its
    current config) first, then upserts the cell — both records land."""
    state, ents = _paths(tmp_path)
    data_path = enc.encode_key_path(["sub", "task_data"])
    ccoords = enc.encode_coords(("cell",))
    async with FxtrScope(name="test-host") as host:
        async with _local_runner(state, ents) as (wf, session):
            sub, _v1 = await _run_maker(wf, session, host)
            assert await session.get_user_sourcing(data_path, "") is None  # task-created

            td = await sub.open_data("task_data", (m.AxisSpec("k", "str"),))
            v2 = _fn_key("v2")  # a distinct value
            # No sourcing record and no adopt → refuse before the write.
            with pytest.raises(GridNotSourcedError):
                await wf.overwrite(td, v2, k="cell")

            # adopt=True (capability flag, beside overwrite): sources the grid
            # at its current config AND records the cell.
            sel = Workflow._sel(td, {"k": "cell"})
            await wf.capability.write_cell_entity(sel, v2, overwrite=True, adopt=True)

            grid_row = await session.get_grid(data_path)
            assert grid_row is not None
            assert await session.get_user_sourcing(data_path, "") == grid_row[1]
            assert await session.get_user_sourcing(data_path, ccoords) == v2
            by = _by_subject(await wf.sourcing())
            assert by[(data_path, "")].status == "attached"
            assert by[(data_path, ccoords)].status == "attached"


@pytest.mark.anyio
async def test_source_grid_then_source_cell_adopts_task_written(tmp_path: Path) -> None:
    """Adoption via the explicit verbs: ``source_grid`` promotes a task-
    created (non-internals) grid to directly-intended, unlocking
    ``source_cell`` on the value the *task* wrote — both come back attached,
    the cell pinned to the task-written fingerprint."""
    state, ents = _paths(tmp_path)
    data_path = enc.encode_key_path(["sub", "task_data"])
    ccoords = enc.encode_coords(("cell",))
    async with FxtrScope(name="test-host") as host:
        async with _local_runner(state, ents) as (wf, session):
            sub, v1 = await _run_maker(wf, session, host)
            assert await session.get_user_sourcing(data_path, "") is None

            await wf.source_grid(sub, "task_data")  # adoption (current config)
            td = await sub.open_data("task_data", (m.AxisSpec("k", "str"),))
            await wf.source_cell(td, k="cell")  # adopt the task-written value

            by = _by_subject(await wf.sourcing())
            assert by[(data_path, "")].status == "attached"
            assert by[(data_path, ccoords)].status == "attached"
            assert by[(data_path, ccoords)].fingerprint == v1


# -- 4. Idempotent same-value writes are sourcing (writing claims the value) ----


@pytest.mark.anyio
async def test_idempotent_write_re_sources(tmp_path: Path) -> None:
    """Writing is sourcing whether or not the bytes changed: after revoking a
    cell record, re-writing the *same* value re-installs it."""
    state, ents = _paths(tmp_path)
    grid = enc.encode_key_path(["d"])
    acoords = enc.encode_coords(("a",))
    async with open_local(state, ents) as wf:
        d = await wf.root.open_data("d", X_AXES)
        v = _some_entity().store().entity_id
        await d.write(v, x="a")
        assert await wf.revoke_cell_sourcing(d, x="a") is True
        assert (grid, acoords) not in _by_subject(await wf.sourcing())

        await d.write(v, x="a")  # identical value: a no-op write, but re-sources
        assert _by_subject(await wf.sourcing())[(grid, acoords)].status == "attached"


# -- 5. User demands require an attached target (and the wrapper auto-adds) ------


@pytest.mark.anyio
async def test_not_targeted_then_auto_target_demand(tmp_path: Path) -> None:
    """A task-output read requires an attached target at the capability
    level: the raw wire read faults ``NotTargetedError`` (transit-wrapped)
    and ``demand(target=False)`` faults typed — neither mutating intent —
    while the default ``demand`` auto-adds the target and serves the value."""
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
            out = t.output_entity()

            # Capability-level read with no target: the typed error crosses
            # the wire as an IRException with its class name as category.
            with pytest.raises(IRException) as ei:
                await wf.capability.read_cell_entity(Workflow._sel(out))
            assert ei.value.category == "NotTargetedError"

            # The wrapper's target=False opt-out faults typed, mutating nothing.
            with pytest.raises(NotTargetedError):
                await wf.demand(out, target=False)
            assert await wf.targets() == ()

            # Default demand auto-adds the target AND serves the value.
            assert await wf.demand(out) == v1
            (info,) = await wf.targets()
            assert info.status == "attached"


# -- 6. Task internals are never user-sourceable --------------------------------


class WritesInternals:
    """Writes one cell into its own per-cell internals scratch grid."""

    async def __call__(self, ctx, /, *, inp):
        v = await inp.read(x="a")
        scratch = await ctx.own_internals().open_data("scratch", (m.AxisSpec("s", "str"),))
        await scratch.write(v, s="kept")
        return v


@pytest.mark.anyio
async def test_source_grid_refuses_internals(tmp_path: Path) -> None:
    """An internals child is browsable but never adoptable: ``source_grid``
    on it faults ``ViewAxisError`` — a task's private state is never
    user-ownable (adopting it would plant intent inside internals)."""
    state, ents = _paths(tmp_path)
    task = WritesInternals()
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
            await wf.add_target(t)
            assert await t.output_entity().read() == v1

            ints = await wf.internals(t)
            assert "scratch" in {k.key for k in await wf.list_keys(ints)}
            with pytest.raises(ViewAxisError, match="never user-sourceable"):
                await wf.source_grid(ints, "scratch")


# -- 7. delete_grid's consequence report names rule referrers -------------------


@pytest.mark.anyio
async def test_delete_grid_reports_broken_referrers(tmp_path: Path) -> None:
    """Deleting a grid a task rule references names that task grid in the
    report (the reverse rule-reference index, enumerated pre-delete)."""
    state, ents = _paths(tmp_path)
    async with open_local(state, ents) as wf:
        fns = await wf.open_handle(wf.root, "fns", FN_AXES)
        await wf.write_handle(fns, _fn_key("up"), fn="up")
        inputs = await wf.root.open_data("inputs", X_AXES)
        await inputs.write(_some_entity().store().entity_id, x="a")
        # Opening the task records its rule's reference to ``inputs`` (no run
        # needed — the reverse index is populated at grid creation).
        await wf.root.open_task(
            "up", axes=(), task_fn=fns.at(fn="up"), args={"inp": inputs.read_only()}
        )

        report = await wf.delete_grid(wf.root, "inputs")
        assert enc.encode_key_path(["up"]) in report.broken_referrers


# -- 8. Verdict surfacing on inspect / list_keys / list_targets -----------------


@pytest.mark.anyio
async def test_verdicts_surface_after_targeted_run(tmp_path: Path) -> None:
    """After a targeted pipeline run the introspection surfaces carry both
    axes: ``inspect`` on the task cell reads grounded + wanted alongside
    fresh + results (one snapshot); ``list_keys`` carries per-grid grounding;
    ``list_targets`` carries the target's grid_grounding."""
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
            await wf.add_target(t)
            assert await t.output_entity().read() == v1

            # inspect: currency + grounding + wanted, one coherent snapshot.
            report = await wf.inspect(t)
            assert report.status == "complete" and report.currency == "fresh"
            assert report.grounding == "grounded" and report.grounding_reason is None
            assert report.wanted is True
            assert report.results is not None and dict(report.results) == {"result": v1}

            # list_keys carries per-grid grounding.
            keys = {k.key: k for k in await wf.list_keys(wf.root)}
            assert keys["t"].grounding == "grounded"
            assert keys["inputs"].grounding == "grounded"

            # list_targets carries the wanted grid's grounding (attached only).
            (info,) = await wf.targets()
            assert info.status == "attached" and info.grid_grounding == "grounded"
