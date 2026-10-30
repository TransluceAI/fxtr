"""The generation-record entity (phase-3 chunk 1): assembly from a
generation's rows, the canonically-ordered entity form (equal fact sets ⇒
equal fingerprints; failures keep insertion order), strong-ref closure
pinning across a session reopen, and the at-rest-only contract. The
step-type-id half of the extraction contract (checkpoint rows persist
their type entity) is asserted here too; the live-engine path is covered
by the existing step suites running against the changed `run_step`.
"""

from pathlib import Path
from typing import AsyncIterator

import pytest
from frozendict import frozendict

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope

from fxtr_experiment import encoding as e
from fxtr_experiment_task import model as m
from fxtr_experiment.state_store import StateDbError, WorkflowSession
from fxtr_experiment.storage import (
    FAILURE_ERROR,
    FAILURE_FAULT,
    OBS_KIND_CELL_VALUE,
    OBS_KIND_GRID_CONFIG,
    OBS_KIND_WROTE_CELL,
    REQ_KIND_DATA,
    REQ_KIND_TASK_OUTPUT,
)

from .test_ops_engine import _some_entity


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[WorkflowSession]:
    async with WorkflowSession.open(
        str(tmp_path / "state.sqlite"), str(tmp_path / "entities.sqlite")
    ) as s:
        yield s


DATA_CFG = m.DataGridConfig(axes=(m.AxisSpec("x", "str"),))
T_PATH = e.encode_key_path(["t"])
CKPT_PATH = e.encode_key_path(["t", "calls"])
INPUTS_PATH = e.encode_key_path(["inputs"])
SIDE_PATH = e.encode_key_path(["side"])
CELL = e.encode_coords(("s1",))


async def _seed_rich_generation(s: WorkflowSession) -> "tuple[int, dict[str, FxtrID]]":
    """One generation exercising every record field; returns (exec_id,
    the ids the assertions compare against)."""
    await s.create_grid(T_PATH, "task", e.GridConfigEntity(config=DATA_CFG))
    await s.create_grid(CKPT_PATH, "checkpoint", e.GridConfigEntity(config=DATA_CFG))
    fn_id = e.store_config(m.HandleGridConfig(axes=()))  # any pinned id works as the fn
    await s.persist(fn_id)
    exec_id = await s.insert_generation("g1", T_PATH, CELL, fn_id, fn_id)
    v1 = _some_entity().store().entity_id
    await s.persist(v1)
    # Requests (commitment) + their resolutions (observations).
    await s.record_request_edge(exec_id, INPUTS_PATH, e.encode_coords(("a",)), REQ_KIND_DATA)
    await s.record_request_edge(exec_id, e.encode_key_path(["up"]), "[]", REQ_KIND_TASK_OUTPUT)
    ver = await s.intern_version(INPUTS_PATH, e.encode_coords(("a",)), "", v1)
    await s.record_observation(exec_id, ver, OBS_KIND_CELL_VALUE)
    wrote = await s.intern_version(SIDE_PATH, e.encode_coords(("k",)), "", v1)
    await s.record_observation(exec_id, wrote, OBS_KIND_WROTE_CELL)
    side_cfg = e.store_config(DATA_CFG)
    await s.persist(side_cfg)
    cfg_ver = await s.intern_version(SIDE_PATH, "", "", side_cfg)
    await s.record_observation(exec_id, cfg_ver, OBS_KIND_GRID_CONFIG)
    await s.record_grid_assertion(exec_id, SIDE_PATH, side_cfg)
    # Failures in a known insertion order.
    await s.record_failure(exec_id, FAILURE_FAULT, "CellFaultError", "no value at x")
    await s.record_failure(exec_id, FAILURE_ERROR, "WriteConflictError", "cell taken")
    # A result and a checkpoint entry.
    result_id = await s.record_result(exec_id, "result", _some_entity())
    step_type = e.store_config(m.SubtreeGridConfig(axes=()))  # a stand-in type id
    await s.put_checkpoint(
        CKPT_PATH, e.encode_coords(("s1", 0)), exec_id, step_type, _some_entity(), _some_entity()
    )
    return exec_id, {
        "fn_id": fn_id,
        "v1": v1,
        "side_cfg": side_cfg,
        "result_id": result_id,
        "step_type": step_type,
    }


@pytest.mark.anyio
async def test_assemble_roundtrip_and_load(session: WorkflowSession) -> None:
    """build_generation_record assembles every field in canonical order;
    the stored entity loads back equal (decode re-canonicalizes)."""
    exec_id, ids = await _seed_rich_generation(session)
    await session.set_status(exec_id, "errored")
    rec = await session.build_generation_record(exec_id)

    assert rec.generation == "g1" and rec.status == "errored"
    assert rec.current_fn == ids["fn_id"] and rec.executed_fn == ids["fn_id"]
    assert dict(rec.results) == {"result": ids["result_id"]}
    # Insertion order, not sorted: the fault came first.
    assert [f.kind for f in rec.failures] == ["fault", "error"]
    assert rec.failures[0].error_class == "CellFaultError" and rec.failures[0].ts
    # The op record, sorted by location; kinds are name literals.
    assert [(o.kind, o.grid_path, o.coords) for o in rec.observations] == [
        ("cell_value", INPUTS_PATH, e.encode_coords(("a",))),
        ("grid_config", SIDE_PATH, ""),
        ("wrote_cell", SIDE_PATH, e.encode_coords(("k",))),
    ]
    assert rec.observations[0].fingerprint == ids["v1"]
    assert rec.assertions == (m.AssertionFact(grid_path=SIDE_PATH, config_id=ids["side_cfg"]),)
    assert [(r.kind, r.grid_path) for r in rec.request_edges] == [
        ("data", INPUTS_PATH),
        ("task_output", e.encode_key_path(["up"])),
    ]
    ((ckpt),) = rec.checkpoints
    assert (ckpt.grid_path, ckpt.step_type_id) == (CKPT_PATH, ids["step_type"])

    record_id = await session.persist(e.GenerationRecordEntity(record=rec))
    loaded = await session.load_generation_record(record_id)
    assert loaded.record == rec


@pytest.mark.anyio
async def test_fingerprint_order_insensitive_except_failures(session: WorkflowSession) -> None:
    """Equal fact sets yield equal entity ids regardless of construction
    order (the entity form canonicalizes); failures are the exception —
    insertion order is meaningful and identity-bearing."""
    a = _some_entity().store().entity_id
    b = e.store_config(DATA_CFG)
    obs = (
        m.ObservationFact("cell_value", INPUTS_PATH, e.encode_coords(("a",)), "", a),
        m.ObservationFact("grid_config", SIDE_PATH, "", "", b),
    )
    reqs = (
        m.RequestFact("data", INPUTS_PATH, e.encode_coords(("a",))),
        m.RequestFact("task_output", T_PATH, "[]"),
    )
    fails = (
        m.FailureFact("fault", "CellFaultError", "x", "t1"),
        m.FailureFact("error", "RuntimeError", "y", "t2"),
    )

    def rec(observations, request_edges, failures) -> m.GenerationRecord:
        return m.GenerationRecord(
            generation="g1",
            status="interrupted",
            current_fn=a,
            executed_fn=a,
            results=frozendict(),
            failures=failures,
            observations=observations,
            assertions=(),
            request_edges=request_edges,
            checkpoints=(),
        )

    id_fwd = e.store_generation_record(rec(obs, reqs, fails))
    id_rev = e.store_generation_record(rec(obs[::-1], reqs[::-1], fails))
    assert id_fwd == id_rev
    id_swapped_failures = e.store_generation_record(rec(obs, reqs, fails[::-1]))
    assert id_swapped_failures != id_fwd


@pytest.mark.anyio
async def test_running_generation_has_no_record(session: WorkflowSession) -> None:
    """Records describe at-rest generations only."""
    exec_id, _ = await _seed_rich_generation(session)  # inserted running
    with pytest.raises(StateDbError, match="at-rest"):
        await session.build_generation_record(exec_id)


@pytest.mark.anyio
async def test_strong_refs_pin_closure_across_reopen(tmp_path: Path) -> None:
    """Persisting a record pins the closure of ids it embeds: a child
    stored only in a transient scope survives the reopen *through the
    record* (never individually pinned), and the checkpoint row's type
    entity is individually pinned by put_checkpoint (the extraction
    contract's fix)."""
    state, ents = str(tmp_path / "state.sqlite"), str(tmp_path / "entities.sqlite")
    child_cfg = m.DataGridConfig(axes=(m.AxisSpec("z", "int"),))
    async with WorkflowSession.open(state, ents) as s:
        async with FxtrScope(name="transient") as scope:
            child_id = e.GridConfigEntity(config=child_cfg).store(scope=scope).entity_id
            fn_id = e.store_config(m.HandleGridConfig(axes=()), scope=scope)
            # The fn id gets *recorded* below (insert_generation), so the
            # liveness invariant requires its persist — and the reopen's
            # startup flip + record repair over "g2" enforces exactly that.
            await s.persist(fn_id, from_scope=scope)
            rec = m.GenerationRecord(
                generation="g1",
                status="complete",
                current_fn=fn_id,
                executed_fn=fn_id,
                results=frozendict({"out": child_id}),
                failures=(),
                observations=(),
                assertions=(),
                request_edges=(),
                checkpoints=(),
            )
            record_id = await s.persist(e.GenerationRecordEntity(record=rec), from_scope=scope)
        # A checkpoint row's type entity is pinned individually.
        await s.create_grid(T_PATH, "task", e.GridConfigEntity(config=DATA_CFG))
        await s.create_grid(CKPT_PATH, "checkpoint", e.GridConfigEntity(config=DATA_CFG))
        exec_id = await s.insert_generation("g2", T_PATH, CELL, fn_id, fn_id)
        step_type = e.store_config(m.SubtreeGridConfig(axes=()))
        await s.put_checkpoint(
            CKPT_PATH,
            e.encode_coords(("s1", 0)),
            exec_id,
            step_type,
            _some_entity(),
            _some_entity(),
        )

    async with WorkflowSession.open(state, ents) as s2:
        # The embedded child was never individually pinned (checked before
        # anything attaches closures to the entity scope)...
        with pytest.raises(KeyError):
            await s2.ensure_loadable(child_id)
        # ...the record itself is pinned, loadable, equal...
        loaded = await s2.load_generation_record(record_id)
        assert loaded.record.results["out"] == child_id
        # ...and the child rides the record's strong-ref closure: loading
        # the record made it visible below the entity scope.
        async with FxtrScope(name="reader", isolated=True, include=[s2.entity_scope]) as reader:
            child = await EntityRef(child_id, e.GridConfigEntity).load(reader)
            assert child.config == child_cfg
        # The type entity is individually pinned (put_checkpoint persists it).
        assert await s2.ensure_loadable(step_type) == step_type
