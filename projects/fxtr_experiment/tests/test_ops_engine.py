"""Ops-engine tests: runtime views over the engine over a real session.

The harness plays one generation of a zero-axis "driver" task cell (no
executor yet — chunk 6); every scenario drives the engine through actual
view objects, so specs, wire values, record-check, and storage are all the
real thing.
"""

from pathlib import Path
from typing import AsyncIterator

import pytest
from frozendict import frozendict

from fxtr_experiment import encoding as enc
from fxtr_experiment_task import model as m
from fxtr_experiment_task.errors import (
    CellFaultError,
    GridConfigMismatchError,
    NondeterminismError,
    ReadWriteExclusionError,
    ViewAxisError,
    WriteConflictError,
)
from fxtr_experiment.ops_engine import OpsEngine, new_generation_id
from fxtr_experiment.spec_registry import SpecRegistry
from fxtr_experiment.state_store import WorkflowSession
from fxtr_experiment_task.view_runtime import build_view
from fxtr_experiment_task.view_spec import SubtreeViewSpec
from fxtr_experiment_task.views import SubtreeView
from fxtr2.data.entity_core import FxtrID

DRIVER_PATH = enc.encode_key_path(["driver"])
ROOT_SPEC = SubtreeViewSpec(path=(), target_axes=(), axes=(), mode="write")


def _some_entity() -> enc.GridConfigEntity:
    return enc.GridConfigEntity(config=m.DataGridConfig(axes=()))


def _driver_config() -> m.TaskGridConfig:
    return m.TaskGridConfig(
        axes=(),
        rule=m.TaskRule(
            task_fn=m.SliceTemplate(
                path=(m.SubgridStepTemplate(cell=(), key="fns"),),
                target_axes=(m.LiteralCoord("driver"),),
            ),
            inputs=frozendict(),
        ),
        outputs=m.DEFAULT_TASK_OUTPUTS,
    )


class Harness:
    """One session + one driver generation + a fresh engine/registry."""

    def __init__(self, session: WorkflowSession) -> None:
        self.session = session
        self.generation = new_generation_id()
        self.exec_id: int = -1

    async def seed(self) -> None:
        await self.session.create_grid(
            DRIVER_PATH, "task", enc.GridConfigEntity(config=_driver_config())
        )
        await self.seed_regen()

    async def seed_regen(self) -> None:
        """(Re)insert the driver generation row (grid already exists)."""
        fn_id = enc.store_config(m.DataGridConfig(axes=()))
        self.exec_id = await self.session.insert_generation(
            self.generation, DRIVER_PATH, "[]", fn_id, fn_id
        )

    async def seed_fns(self) -> FxtrID:
        """A user-created handle grid `fns` with one cell 'child'."""
        await self.session.create_grid(
            enc.encode_key_path(["fns"]),
            "handle",
            enc.GridConfigEntity(config=m.HandleGridConfig(axes=(m.AxisSpec("fn", "str"),))),
        )
        key = _some_entity().store().entity_id
        await self.session.write_cell(enc.encode_key_path(["fns"]), '["child"]', key)
        return key

    async def status_only_hook(self, grid_path: str, coords: str) -> dict[str, FxtrID]:
        """The harness's trivial demand hook: serve a complete cell's
        results, fault otherwise. Deliberately weaker than the executor's
        real hook (no currency check, no run orchestration, and a
        check-then-read window) — fine for driving the engine directly.
        """
        row = await self.session.get_generation(grid_path, coords)
        if row is None or row[2] != "complete":
            status = "absent" if row is None else row[2]
            raise CellFaultError(f"task cell {grid_path}{coords} is {status}")
        return await self.session.get_results(row[0])

    async def engine(self, demand_hook=None, step_host=None, read_gate=None) -> OpsEngine:
        """A fresh engine (and registry) for the driver generation —
        constructing again is exactly resume. ``demand_hook`` defaults to
        :meth:`status_only_hook` (the hook is required by the engine);
        ``step_host`` / ``read_gate`` stay None unless a test exercises
        run_step misses / gated reads."""
        self.registry = SpecRegistry()
        return await OpsEngine.load(
            self.session,
            self.registry,
            exec_id=self.exec_id,
            generation=self.generation,
            holder_coords=(),
            demand_hook=demand_hook or self.status_only_hook,
            read_gate=read_gate,
            step_host=step_host,
        )

    def root(self, engine: OpsEngine) -> SubtreeView:
        return build_view(engine, self.registry.mint(ROOT_SPEC))  # type: ignore[return-value]


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


@pytest.mark.anyio
async def test_read_coherence_and_resume(h: Harness) -> None:
    """First read pins the value: later reads (same run OR a resumed run)
    serve the record even after the live cell changes; a new generation
    sees the new value."""
    v1, v2 = _some_entity().store().entity_id, None
    await h.session.create_grid(
        enc.encode_key_path(["inputs"]),
        "data",
        enc.GridConfigEntity(config=m.DataGridConfig(axes=(m.AxisSpec("x", "str"),))),
    )
    await h.session.write_cell(enc.encode_key_path(["inputs"]), '["a"]', v1)

    engine = await h.engine()
    inputs = await h.root(engine).open_data("inputs", axes=(m.AxisSpec("x", "str"),))
    assert await inputs.read(x="a") == v1

    # External overwrite (a privileged user edit — the real write path,
    # which pins: served ids must satisfy the liveness invariant).
    v2 = await h.session.write_cell(
        enc.encode_key_path(["inputs"]),
        '["a"]',
        enc.SliceTemplateEntity(template=m.SliceTemplate(path=(), target_axes=())),
    )
    assert await inputs.read(x="a") == v1  # same-run hit
    resumed = await h.engine()
    inputs2 = await h.root(resumed).open_data("inputs", axes=(m.AxisSpec("x", "str"),))
    assert await inputs2.read(x="a") == v1  # resume replays the record

    # A NEW generation observes the new value.
    await h.session.delete_generation(h.exec_id)
    h.generation = new_generation_id()
    await h.seed_regen()
    fresh = await h.engine()
    inputs3 = await h.root(fresh).open_data("inputs", axes=(m.AxisSpec("x", "str"),))
    assert await inputs3.read(x="a") == v2


@pytest.mark.anyio
async def test_write_semantics(h: Harness) -> None:
    engine = await h.engine()
    out = await h.root(engine).open_data("out", axes=(m.AxisSpec("x", "str"),))
    v = _some_entity().store().entity_id
    await out.write(v, x="a")
    await out.write(v, x="a")  # idempotent rewrite: no-op hit
    with pytest.raises(NondeterminismError):  # different value vs own record
        await out.write(
            enc.SliceTemplateEntity(template=m.SliceTemplate(path=(), target_axes=()))
            .store()
            .entity_id,
            x="a",
        )
    # Pre-existing DIFFERENT live value at a cell we never touched:
    other = enc.SliceTemplateEntity(template=m.SliceTemplate(path=(), target_axes=(), output="x"))
    await h.session.write_cell(enc.encode_key_path(["out"]), '["b"]', other)
    with pytest.raises(WriteConflictError):
        await out.write(v, x="b")
    # Writing through a read-mode view is refused.
    with pytest.raises(ViewAxisError):
        await out.read_only().write(v, x="c")


@pytest.mark.anyio
async def test_read_write_exclusion(h: Harness) -> None:
    engine = await h.engine()
    root = h.root(engine)
    grid = await root.open_data("g", axes=(m.AxisSpec("x", "str"),))
    await grid.write(_some_entity().store().entity_id, x="a")
    with pytest.raises(ReadWriteExclusionError):
        await grid.read_only().read(x="a")  # read after write, same grid
    # And the reverse, on a separate grid:
    await h.session.create_grid(
        enc.encode_key_path(["g2"]),
        "data",
        enc.GridConfigEntity(config=m.DataGridConfig(axes=(m.AxisSpec("x", "str"),))),
    )
    await h.session.write_cell(enc.encode_key_path(["g2"]), '["a"]', _some_entity())
    g2 = await root.open_data("g2", axes=(m.AxisSpec("x", "str"),))
    await g2.read_only().read(x="a")
    with pytest.raises(ReadWriteExclusionError):
        await g2.write(_some_entity().store().entity_id, x="b")


@pytest.mark.anyio
async def test_open_semantics(h: Harness) -> None:
    engine = await h.engine()
    root = h.root(engine)
    await root.open_data("d", axes=(m.AxisSpec("x", "str"),))
    await root.open_data("d", axes=(m.AxisSpec("x", "str"),))  # idempotent
    with pytest.raises(NondeterminismError):  # own record, different config
        await root.open_data("d", axes=(m.AxisSpec("y", "str"),))
    # A different generation hitting a live mismatch gets the config error.
    await h.session.delete_generation(h.exec_id)
    h.generation = new_generation_id()
    await h.seed_regen()
    fresh = await h.engine()
    with pytest.raises(GridConfigMismatchError):
        await h.root(fresh).open_data("d", axes=(m.AxisSpec("y", "str"),))
    # Handle grids: attach-only for tasks.
    with pytest.raises(CellFaultError):
        await h.root(fresh).open_handle("judges", axes=(m.AxisSpec("j", "str"),))
    await h.session.create_grid(
        enc.encode_key_path(["judges"]),
        "handle",
        enc.GridConfigEntity(config=m.HandleGridConfig(axes=(m.AxisSpec("j", "str"),))),
    )
    key = _some_entity().store().entity_id
    await h.session.write_cell(enc.encode_key_path(["judges"]), '["j1"]', key)
    judges = await h.root(fresh).open_handle("judges", axes=(m.AxisSpec("j", "str"),))
    assert await judges.read_key(j="j1") == key
    # Checkpoint grids only open inside internals.
    with pytest.raises(GridConfigMismatchError):
        await h.root(fresh).open_checkpoint("cp", axes=())


@pytest.mark.anyio
async def test_open_task_and_output_read(h: Harness) -> None:
    engine = await h.engine()
    root = h.root(engine)
    await h.seed_fns()
    fns = await root.open_handle("fns", axes=(m.AxisSpec("fn", "str"),))
    ws = await root.open_subtree("ws", axes=())
    child = await ws.open_task("t", axes=(), task_fn=fns.at(fn="child"), args={})
    # Stored config is canonical + durable:
    row = await h.session.get_grid(enc.encode_key_path(["ws", "t"]))
    assert row is not None and row[0] == "task"
    # Reading the output with no executor faults (absent target, no hook):
    with pytest.raises(CellFaultError):
        await child.output_entity().read()
    # Simulate the executor completing the child, then read through the view:
    child_exec = await h.session.insert_generation(
        new_generation_id(),
        enc.encode_key_path(["ws", "t"]),
        "[]",
        _some_entity().store().entity_id,
        _some_entity().store().entity_id,
    )
    result = await h.session.record_result(child_exec, "result", _some_entity())
    await h.session.set_status(child_exec, "complete")
    assert await child.output_entity().read() == result


@pytest.mark.anyio
async def test_demand_hook_is_consulted(h: Harness) -> None:
    calls: list[tuple[str, str]] = []

    async def hook(grid_path: str, coords: str) -> dict[str, FxtrID]:
        calls.append((grid_path, coords))
        child_exec = await h.session.insert_generation(
            new_generation_id(),
            grid_path,
            coords,
            _some_entity().store().entity_id,
            _some_entity().store().entity_id,
        )
        result = await h.session.record_result(child_exec, "result", _some_entity())
        await h.session.set_status(child_exec, "complete")
        return {"result": result}  # the validated results (per DemandHook)

    engine = await h.engine(demand_hook=hook)
    root = h.root(engine)
    await h.seed_fns()
    fns = await root.open_handle("fns", axes=(m.AxisSpec("fn", "str"),))
    ws = await root.open_subtree("ws", axes=())
    child = await ws.open_task("t", axes=(), task_fn=fns.at(fn="child"), args={})
    value = await child.output_entity().read()
    assert calls == [(enc.encode_key_path(["ws", "t"]), "[]")]
    assert value is not None


@pytest.mark.anyio
async def test_checkpoint_opens_inside_internals(h: Harness) -> None:
    engine = await h.engine()
    internals_spec = SubtreeViewSpec(
        path=(m.SubgridStepTemplate(cell=(), key="driver"),),
        target_axes=(),
        axes=(),
        mode="write",
        internals=True,
    )
    internals = build_view(engine, h.registry.mint(internals_spec))
    assert isinstance(internals, SubtreeView)  # an internals grant is subtree-shaped
    cp = await internals.open_checkpoint("calls", axes=(m.AxisSpec("p", "str"),))
    assert cp is not None
    row = await h.session.get_grid(enc.encode_key_path(["driver", "calls"]))
    assert row is not None and row[0] == "checkpoint"


@pytest.mark.anyio
async def test_write_open_records_grid_assertion(h: Harness) -> None:
    """An open through write authority records a would-create claim
    (grounding's asserted_grid); a same-generation record hit is idempotent
    (exactly one row)."""
    engine = await h.engine()
    root = h.root(engine)  # the root grant is write-mode
    axes = (m.AxisSpec("x", "str"),)
    await root.open_data("g", axes=axes)
    grid_path = enc.encode_key_path(["g"])
    config_id = enc.store_config(m.DataGridConfig(axes=axes))
    assert await h.session.load_assertions(h.exec_id) == ((grid_path, config_id),)
    # The same open again in this generation is a record hit; INSERT OR
    # IGNORE keeps exactly one row.
    await root.open_data("g", axes=axes)
    assert await h.session.load_assertions(h.exec_id) == ((grid_path, config_id),)


@pytest.mark.anyio
async def test_read_and_handle_opens_record_no_assertion(h: Harness) -> None:
    """A handle open is attach-only and never claims; an open through a
    read-mode subtree grant carries no write authority, so it records no
    would-create claim either."""
    engine = await h.engine()
    # Handle open under the write root: attach-only, no claim.
    await h.seed_fns()
    await h.root(engine).open_handle("fns", axes=(m.AxisSpec("fn", "str"),))
    assert await h.session.load_assertions(h.exec_id) == ()

    # Read-grant open: attach an existing grid through a read-mode subtree.
    read_root = build_view(
        engine, h.registry.mint(SubtreeViewSpec(path=(), target_axes=(), axes=(), mode="read"))
    )
    assert isinstance(read_root, SubtreeView)
    axes = (m.AxisSpec("x", "str"),)
    await h.session.create_grid(
        enc.encode_key_path(["ro"]),
        "data",
        enc.GridConfigEntity(config=m.DataGridConfig(axes=axes)),
    )
    await read_root.open_data("ro", axes=axes)
    assert await h.session.load_assertions(h.exec_id) == ()
