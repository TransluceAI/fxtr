"""Poisoning + request-obligation tests: failures are persisted at raise
time on the raising side (never classified off the wire), catching or
ignoring a result cannot evade them, and a committed op resolves — and
records — even when its requester is cancelled."""

from pathlib import Path
from typing import AsyncIterator

import anyio
import pytest

from fxtr2.data.entity_ref_scope import FxtrScope

from fxtr_experiment import encoding as enc
from fxtr_experiment_task import model as m
from fxtr_experiment_task.errors import CellFaultError, WriteConflictError
from fxtr_experiment.state_store import WorkflowSession
from fxtr_experiment.storage import FAILURE_ERROR, FAILURE_FAULT
from fxtr_experiment_task.task_adapter import TaskFnAdapter
from fxtr_experiment_task.view_runtime import build_view
from fxtr_experiment_task.view_spec import LeafViewSpec, SubtreeViewSpec, ViewSpec
from fxtr_experiment_task.views import DataView

from .boundary import run_task_over_engine
from .test_ops_engine import Harness, _some_entity


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[Harness]:
    async with WorkflowSession.open(
        str(tmp_path / "state.sqlite"), str(tmp_path / "entities.sqlite")
    ) as session:
        harness = Harness(session)
        await harness.seed()
        yield harness


INPUTS_PATH = enc.encode_key_path(["inputs"])


def _data_spec(mode: str = "read") -> ViewSpec:
    """A view of the whole 1-axis ``inputs`` data grid."""
    return LeafViewSpec(
        kind="data",
        path=(m.SubgridStepTemplate(cell=(), key="inputs"),),
        target_axes=(m.KeepAxis(),),
        axes=(m.AxisSpec("x", "str"),),
        mode=mode,  # type: ignore[arg-type]
    )


def _internals_spec() -> ViewSpec:
    """The zero-axis driver cell's internals view."""
    return SubtreeViewSpec(
        path=(m.SubgridStepTemplate(cell=(), key="driver"),),
        target_axes=(),
        axes=(),
        mode="write",
        internals=True,
    )


async def _seed_inputs(h: Harness) -> None:
    await h.session.create_grid(
        INPUTS_PATH,
        "data",
        enc.GridConfigEntity(config=m.DataGridConfig(axes=(m.AxisSpec("x", "str"),))),
    )


@pytest.mark.anyio
async def test_fault_is_persisted_at_raise_time(h: Harness) -> None:
    """An engine fault writes its failure row when it raises — before any
    executor sees anything — classified from the typed exception."""
    await _seed_inputs(h)
    engine = await h.engine()
    view = build_view(engine, h.registry.mint(_data_spec()))
    assert isinstance(view, DataView)
    with pytest.raises(CellFaultError):
        await view.read(x="nope")
    (row,) = await h.session.load_failures(h.exec_id)
    assert row[0] == FAILURE_FAULT and row[1] == "CellFaultError"
    assert engine.run_failures == [row]  # the in-memory mirror matches


@pytest.mark.anyio
async def test_integrity_violation_is_error_class(h: Harness) -> None:
    """Non-fault workflow errors classify FAILURE_ERROR (the task's own
    fault) — here a write conflict with a live different value."""
    await _seed_inputs(h)
    await h.session.write_cell(INPUTS_PATH, '["a"]', _some_entity())
    other = enc.GridConfigEntity(config=m.DataGridConfig(axes=(m.AxisSpec("y", "int"),)))
    engine = await h.engine()
    view = build_view(engine, h.registry.mint(_data_spec("write")))
    assert isinstance(view, DataView)
    with pytest.raises(WriteConflictError):
        await view.write(other.store().entity_id, x="a")
    (row,) = await h.session.load_failures(h.exec_id)
    assert row[0] == FAILURE_ERROR and row[1] == "WriteConflictError"


@pytest.mark.anyio
async def test_internal_error_poisons_too(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-workflow exception inside an op (an engine/storage bug) also
    records an error-class failure at raise time — task code catching its
    wrapped form cannot complete cleanly around an op that failed
    mid-effect."""
    await _seed_inputs(h)
    engine = await h.engine()
    view = build_view(engine, h.registry.mint(_data_spec()))
    assert isinstance(view, DataView)

    async def boom(grid_path: str, coords: str) -> None:
        raise RuntimeError("storage exploded")

    monkeypatch.setattr(h.session, "read_cell", boom)
    with pytest.raises(RuntimeError):
        await view.read(x="a")
    (row,) = await h.session.load_failures(h.exec_id)
    assert row[0] == FAILURE_ERROR and row[1] == "RuntimeError"
    assert engine.run_failures == [row]


@pytest.mark.anyio
async def test_catch_and_return_still_poisons(h: Harness) -> None:
    """The marquee poisoning property, across the full service boundary: a
    task fn that catches the op fault and returns a perfectly valid value
    anyway — the run *returns normally*, but the failure was persisted at
    raise time and the run's failure set is non-empty."""

    class CatchesAndReturns:
        async def __call__(self, ctx, /, *, inp):
            try:
                return await inp.read(x="nope")
            except Exception:
                return _some_entity().store().entity_id  # a valid bare return

    await _seed_inputs(h)
    engine = await h.engine()
    async with FxtrScope(name="runner") as runner_scope:
        fn_ref = await runner_scope.mount(TaskFnAdapter(CatchesAndReturns()))
        result = await run_task_over_engine(
            fn_ref,
            engine,
            runner_scope=runner_scope,
            args={"inp": h.registry.mint(_data_spec())},
            internals=h.registry.mint(_internals_spec()),
            outputs=m.DEFAULT_TASK_OUTPUTS,
        )
    assert set(result) == {"result"}  # the task "succeeded", as far as it knows
    (row,) = await h.session.load_failures(h.exec_id)
    assert row[0] == FAILURE_FAULT and row[1] == "CellFaultError"
    assert engine.run_failures == [row]  # the executor's gate sees the poison


@pytest.mark.anyio
async def test_cancelled_request_aborts_and_poisons(h: Harness) -> None:
    """Request = observation, the cancellation flavor: once an op commits
    (sync, on entry) the requester cancelling it cannot retract it — but
    nothing blocks on the in-flight work either. The op resolves by
    *aborting*: a fault-class RequestAbortedError failure is recorded (so
    the run can only end interrupted) and the cancellation propagates
    immediately, even while the demand hook is deep inside a long upstream
    execution."""
    entered = anyio.Event()

    async def stuck_upstream(grid_path: str, coords: str) -> None:
        entered.set()
        await anyio.sleep(3600)  # an upstream run that never finishes

    engine = await h.engine(demand_hook=stuck_upstream)
    root = h.root(engine)
    assert engine.obligations_outstanding == 0
    await engine.wait_obligations_drained()  # vacuous drain returns at once
    await h.seed_fns()
    fns = await root.open_handle("fns", axes=(m.AxisSpec("fn", "str"),))
    ws = await root.open_subtree("ws", axes=())
    child = await ws.open_task("t", axes=(), task_fn=fns.at(fn="child"), args={})

    async def doomed_demand() -> None:
        await child.output_entity().read()  # blocks in the hook, then is cancelled

    async with anyio.create_task_group() as tg:
        tg.start_soon(doomed_demand)
        await entered.wait()  # the read op is committed and in flight
        tg.cancel_scope.cancel()  # requester gone — must NOT wait the hour
    await engine.wait_obligations_drained()
    (row,) = await h.session.load_failures(h.exec_id)
    assert row[0] == FAILURE_FAULT and row[1] == "RequestAbortedError"
    assert engine.run_failures == [row]
    # The aborted op recorded no observation: a later run re-attempts it.
    record = await h.session.load_record(h.generation)
    assert all(r[0] != enc.encode_key_path(["ws", "t"]) or r[2] == "" for r in record)
