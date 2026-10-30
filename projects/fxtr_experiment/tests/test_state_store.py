"""WorkflowSession tests: two-file bootstrap, liveness ordering, lifecycle."""

from pathlib import Path
from typing import AsyncIterator

import pytest

from fxtr_experiment import encoding as e
from fxtr_experiment_task import model as m
from fxtr_experiment_task.errors import EncodingError, GridNotSourcedError
from fxtr_experiment.state_store import StateDbError, WorkflowSession
from fxtr_experiment.storage import (
    FAILURE_ERROR,
    FAILURE_FAULT,
    OBS_KIND_CELL_VALUE,
)


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
TASK_PATH = e.encode_key_path(["grades"])
CKPT_PATH = e.encode_key_path(["grades", "calls"])


async def _seed_task_grid(s: WorkflowSession) -> int:
    await s.create_grid(TASK_PATH, "task", e.GridConfigEntity(config=DATA_CFG))
    await s.create_grid(CKPT_PATH, "checkpoint", e.GridConfigEntity(config=DATA_CFG))
    return await s.insert_generation(
        "g1",
        TASK_PATH,
        e.encode_coords(("s1",)),
        e.store_config(DATA_CFG),
        e.store_config(DATA_CFG),
    )


@pytest.mark.anyio
async def test_grid_roundtrip_and_rmdir(session: WorkflowSession) -> None:
    await _seed_task_grid(session)
    kind, config_id = await session.get_grid(TASK_PATH)  # type: ignore[misc]
    assert kind == "task"
    assert config_id == e.store_config(DATA_CFG)
    assert await session.list_child_grids(TASK_PATH) == ((CKPT_PATH, "checkpoint"),)
    with pytest.raises(StateDbError):  # non-empty
        await session.delete_grid(TASK_PATH)
    await session.delete_grid(CKPT_PATH)
    with pytest.raises(StateDbError):  # generation still running
        await session.delete_grid(TASK_PATH)
    exec_row = await session.get_generation(TASK_PATH, e.encode_coords(("s1",)))
    assert exec_row is not None
    await session.set_status(exec_row[0], "complete")
    await session.delete_grid(TASK_PATH)
    assert await session.get_grid(TASK_PATH) is None


@pytest.mark.anyio
async def test_recursive_delete_removes_subtree(session: WorkflowSession) -> None:
    """Recursive delete takes a task grid AND its read-only internals (the
    deadlock rmdir can't): non-recursive refuses on the child grid, and
    both variants refuse while a generation in the set is running."""
    await _seed_task_grid(session)  # TASK_PATH (task) + CKPT_PATH (child) + running gen
    with pytest.raises(StateDbError):  # rmdir refuses — the child grid
        await session.delete_grid(TASK_PATH)
    with pytest.raises(StateDbError):  # recursive still refuses — running gen in the set
        await session.delete_grid(TASK_PATH, recursive=True)
    exec_row = await session.get_generation(TASK_PATH, e.encode_coords(("s1",)))
    assert exec_row is not None
    await session.set_status(exec_row[0], "complete")
    await session.delete_grid(TASK_PATH, recursive=True)
    assert await session.get_grid(TASK_PATH) is None
    assert await session.get_grid(CKPT_PATH) is None  # the child went too


@pytest.mark.anyio
async def test_recursive_delete_is_deep(session: WorkflowSession) -> None:
    """The subtree walk (over parent_path) reaches every level."""
    root = e.encode_key_path(["t"])
    child = e.encode_key_path(["t", "a"])
    grandchild = e.encode_key_path(["t", "a", "b"])
    for p in (root, child, grandchild):
        await session.create_grid(p, "subtree", e.GridConfigEntity(config=DATA_CFG))
    await session.delete_grid(root, recursive=True)
    for p in (root, child, grandchild):
        assert await session.get_grid(p) is None


@pytest.mark.anyio
async def test_recursive_delete_reports_external_referrers_only(session: WorkflowSession) -> None:
    """The consequence report names referrers OUTSIDE the deleted set (their
    grid_dep vanishes) and excludes referrers inside it (they are gone too)."""
    root = e.encode_key_path(["t"])
    child = e.encode_key_path(["t", "a"])
    ext = e.encode_key_path(["ext"])
    await session.create_grid(root, "subtree", e.GridConfigEntity(config=DATA_CFG))
    # A child inside the subtree whose rule references the root (in-set).
    await session.create_grid(child, "task", e.GridConfigEntity(config=DATA_CFG), rule_refs=(root,))
    # An external task whose rule references a grid inside the subtree.
    await session.create_grid(ext, "task", e.GridConfigEntity(config=DATA_CFG), rule_refs=(child,))
    referrers = await session.delete_grid(root, recursive=True)
    assert referrers == (ext,)  # ext reported; the in-set child is not


@pytest.mark.anyio
async def test_load_config_roundtrip(session: WorkflowSession) -> None:
    """create_grid pins the config entity; load_config gets the model back
    (via the namespace's own scope — no reliance on the caller's)."""
    await session.create_grid(TASK_PATH, "task", e.GridConfigEntity(config=DATA_CFG))
    row = await session.get_grid(TASK_PATH)
    assert row is not None
    entity = await session.load_config(row[1])
    assert entity.config == DATA_CFG


@pytest.mark.anyio
async def test_persist_then_record_survives_reopen(tmp_path: Path) -> None:
    """write_cell pins the closure before recording; a fresh session can
    resolve the recorded id from the entity store."""
    coords = e.encode_coords(())
    async with WorkflowSession.open(str(tmp_path / "s.sqlite"), str(tmp_path / "e.sqlite")) as s:
        await s.create_grid(
            e.encode_key_path(["inputs"]), "data", e.GridConfigEntity(config=DATA_CFG)
        )
        value_id = await s.write_cell(
            e.encode_key_path(["inputs"]), coords, e.GridConfigEntity(config=DATA_CFG)
        )
    async with WorkflowSession.open(str(tmp_path / "s.sqlite"), str(tmp_path / "e.sqlite")) as s2:
        got = await s2.read_cell(e.encode_key_path(["inputs"]), coords)
        assert got == value_id
        ref = await s2._namespace.retrieve(value_id.id_string())
        assert ref == value_id


@pytest.mark.anyio
async def test_startup_flips_running_to_interrupted(tmp_path: Path) -> None:
    paths = (str(tmp_path / "s.sqlite"), str(tmp_path / "e.sqlite"))
    async with WorkflowSession.open(*paths) as s:
        await _seed_task_grid(s)
    async with WorkflowSession.open(*paths) as s2:
        row = await s2.get_generation(TASK_PATH, e.encode_coords(("s1",)))
        assert row is not None and row[2] == "interrupted"
        # The flip records a fault-class failure (the uniform "why").
        failures = await s2.load_failures(row[0])
        assert len(failures) == 1
        kind, error_class, message = failures[0]
        assert kind == FAILURE_FAULT
        assert error_class == "RunnerCrash"
        assert "crash" in message


@pytest.mark.anyio
async def test_failure_records_roundtrip_in_order(session: WorkflowSession) -> None:
    """Failures are plural per generation and come back in insertion order
    (concurrent branches may each fault/error)."""
    exec_id = await _seed_task_grid(session)
    assert await session.load_failures(exec_id) == ()
    await session.record_failure(exec_id, FAILURE_FAULT, "CellFaultError", "no value at x")
    await session.record_failure(exec_id, FAILURE_ERROR, "WriteConflictError", "cell taken")
    assert await session.load_failures(exec_id) == (
        (FAILURE_FAULT, "CellFaultError", "no value at x"),
        (FAILURE_ERROR, "WriteConflictError", "cell taken"),
    )
    # A new run starts by clearing the last attempt's evidence.
    await session.clear_failures(exec_id)
    assert await session.load_failures(exec_id) == ()


@pytest.mark.anyio
async def test_record_roundtrip_and_turnover(session: WorkflowSession) -> None:
    exec_id = await _seed_task_grid(session)
    fp = e.store_config(DATA_CFG)
    version = await session.intern_version(e.encode_key_path(["inputs"]), "[]", "", fp)
    assert await session.intern_version(e.encode_key_path(["inputs"]), "[]", "", fp) == version
    await session.record_observation(exec_id, version, OBS_KIND_CELL_VALUE)
    # Would-create claims accumulate idempotently (INSERT OR IGNORE) and
    # round-trip as (grid_path, config fingerprint).
    await session.record_grid_assertion(exec_id, e.encode_key_path(["inputs"]), fp)
    await session.record_grid_assertion(exec_id, e.encode_key_path(["inputs"]), fp)  # idempotent
    assert await session.load_assertions(exec_id) == ((e.encode_key_path(["inputs"]), fp),)
    await session.put_checkpoint(
        CKPT_PATH,
        e.encode_coords(("s1", "p1")),
        exec_id,
        fp,
        e.GridConfigEntity(config=DATA_CFG),
        e.GridConfigEntity(config=DATA_CFG),
    )
    record = await session.load_record("g1")
    assert [(r[0], r[4]) for r in record] == [(e.encode_key_path(["inputs"]), OBS_KIND_CELL_VALUE)]
    assert await session.observers_of(e.encode_key_path(["inputs"]), "[]") == (
        (TASK_PATH, e.encode_coords(("s1",))),
    )
    ckpt = await session.get_checkpoint(CKPT_PATH, e.encode_coords(("s1", "p1")))
    assert ckpt is not None and ckpt[0] == exec_id
    cfg_entity_id = e.store_config(DATA_CFG)
    assert await session.load_checkpoints(exec_id) == (
        (CKPT_PATH, e.encode_coords(("s1", "p1")), fp, cfg_entity_id, cfg_entity_id),
    )
    # Turnover: one delete sweeps the record + checkpoint + assertions.
    await session.delete_generation(exec_id)
    assert await session.load_record("g1") == ()
    assert await session.load_assertions(exec_id) == ()
    assert await session.get_checkpoint(CKPT_PATH, e.encode_coords(("s1", "p1"))) is None


@pytest.mark.anyio
async def test_targets_and_events(session: WorkflowSession) -> None:
    slice_id = e.store_slice(
        m.SliceTemplate(path=(m.SubgridStepTemplate(cell=(), key="t"),), target_axes=())
    )
    cfg_id = e.store_config(DATA_CFG)
    grid_path = e.encode_key_path(["wanted"])
    coords = e.encode_coords(("a",))
    # The grid-before-cell invariant: a target needs the grid-level sourcing row.
    with pytest.raises(GridNotSourcedError):
        await session.add_target(slice_id, cfg_id, grid_path, coords)
    await session.upsert_user_sourcing(grid_path, "", cfg_id)
    await session.add_target(slice_id, cfg_id, grid_path, coords)
    await session.add_target(slice_id, cfg_id, grid_path, coords)  # idempotent
    assert await session.list_targets() == ((slice_id, cfg_id, grid_path, coords),)
    await session.remove_target(slice_id, cfg_id)
    assert await session.list_targets() == ()
    await session.append_event("test", '{"ok":true}')


@pytest.mark.anyio
async def test_step_payload_persist_pins_closures(tmp_path: Path) -> None:
    """Persisting a step payload pins the closures of every id it embeds
    (strong refs + complete subgraph): after a reopen, both the payload
    and the entities it references are loadable from the store."""
    paths = (str(tmp_path / "s.sqlite"), str(tmp_path / "e.sqlite"))
    inner = e.GridConfigEntity(config=DATA_CFG).store()
    async with WorkflowSession.open(*paths) as s:
        payload_id = e.store_step_payload({"cfg": inner.entity_id, "k": 1})
        await s.persist(payload_id)
    async with WorkflowSession.open(*paths) as s2:
        value = await s2.load_step_payload(payload_id)
        assert value == {"cfg": inner.entity_id, "k": 1}
        # The embedded id's closure came along for the ride.
        assert await s2.entity_scope.load_cbor(inner.entity_id) is not None


# -- user sourcing + grid_refs (the editable root facts) ------------------------

# Two distinct configs → two distinct fingerprints for pin-replacement tests.
CFG_OTHER = m.DataGridConfig(axes=())


@pytest.mark.anyio
async def test_sourcing_crud(session: WorkflowSession) -> None:
    """Grid/cell sourcing round-trips; upsert replaces the pin; the
    grid-before-cell invariant gates cell rows; revoke reports presence."""
    grid = e.encode_key_path(["g"])
    fp_a, fp_b = e.store_config(DATA_CFG), e.store_config(CFG_OTHER)
    assert fp_a != fp_b

    await session.upsert_user_sourcing(grid, "", fp_a)
    assert await session.get_user_sourcing(grid, "") == fp_a
    assert await session.list_user_sourcing() == ((grid, "", fp_a),)

    # Upsert again with a different fingerprint replaces the pin (one row).
    await session.upsert_user_sourcing(grid, "", fp_b)
    assert await session.get_user_sourcing(grid, "") == fp_b
    assert await session.list_user_sourcing() == ((grid, "", fp_b),)

    # A cell upsert works with the grid row present.
    cell = e.encode_coords(("a",))
    await session.upsert_user_sourcing(grid, cell, fp_a)
    assert await session.get_user_sourcing(grid, cell) == fp_a

    # A cell upsert on a grid with NO grid-level row is refused.
    with pytest.raises(GridNotSourcedError):
        await session.upsert_user_sourcing(e.encode_key_path(["ungrounded"]), cell, fp_a)

    # revoke reports presence: True for the extant cell row, False the second time.
    assert await session.revoke_user_sourcing(grid, cell) is True
    assert await session.revoke_user_sourcing(grid, cell) is False


@pytest.mark.anyio
async def test_sourcing_invariant_maintained_on_revoke(session: WorkflowSession) -> None:
    """A grid-level revoke refuses while cell rows / targets exist; cascade
    deletes those dependents in the same transaction; a plain cell revoke
    needs no cascade."""
    grid = e.encode_key_path(["g"])
    cell = e.encode_coords(("a",))
    fp = e.store_config(DATA_CFG)
    slice_id = e.store_slice(
        m.SliceTemplate(path=(m.SubgridStepTemplate(cell=(), key="g"),), target_axes=())
    )

    await session.upsert_user_sourcing(grid, "", fp)
    await session.upsert_user_sourcing(grid, cell, fp)
    await session.add_target(slice_id, fp, grid, cell)

    # Revoking the grid row while dependents exist is refused (maintained,
    # not merely checked at add time).
    with pytest.raises(StateDbError):
        await session.revoke_user_sourcing(grid, "")

    # cascade sweeps the dependent cell rows AND targets in one transaction.
    assert await session.revoke_user_sourcing(grid, "", cascade=True) is True
    assert await session.list_user_sourcing() == ()
    assert await session.list_targets() == ()

    # A plain cell-row revoke needs no cascade (nothing hangs beneath a cell).
    await session.upsert_user_sourcing(grid, "", fp)
    await session.upsert_user_sourcing(grid, cell, fp)
    assert await session.revoke_user_sourcing(grid, cell) is True
    assert await session.list_user_sourcing() == ((grid, "", fp),)


@pytest.mark.anyio
async def test_sourcing_survives_grid_deletion(session: WorkflowSession) -> None:
    """Sourcing rows are un-FK'd intent: deleting the grid leaves the record
    listed (a dangling record, derived at read time)."""
    grid = e.encode_key_path(["data"])
    fp = e.store_config(DATA_CFG)
    await session.create_grid(grid, "data", e.GridConfigEntity(config=DATA_CFG))
    await session.upsert_user_sourcing(grid, "", fp)
    await session.delete_grid(grid)
    assert await session.get_grid(grid) is None
    assert await session.list_user_sourcing() == ((grid, "", fp),)


@pytest.mark.anyio
async def test_grid_refs_reverse_index_and_cascade(session: WorkflowSession) -> None:
    """create_grid populates the reverse rule-reference index; the rows
    cascade away with the referrer grid."""
    referrer = e.encode_key_path(["task"])
    p1 = e.encode_key_path(["inputs"])
    p2 = e.encode_key_path(["fns"])
    await session.create_grid(
        referrer, "task", e.GridConfigEntity(config=DATA_CFG), rule_refs=(p1, p2)
    )
    assert await session.grids_referencing(p1) == (referrer,)
    assert await session.grids_referencing(p2) == (referrer,)
    # Deleting the referrer removes its grid_refs rows (FK cascade).
    await session.delete_grid(referrer)
    assert await session.grids_referencing(p1) == ()
    assert await session.grids_referencing(p2) == ()


@pytest.mark.anyio
async def test_create_grid_enforces_key_charset_law(session: WorkflowSession) -> None:
    """Grid keys become directory names in the at-rest export layout
    (notes/at_rest_format.md), so creation is the one enforcement point:
    lowercase [a-z0-9][a-z0-9_-]*, length-capped — no invalid key ever
    reaches the grids table."""
    for ok in ("a", "task_data", "judge-calls", "0x", "a" * e.GRID_KEY_MAX_LEN):
        await session.create_grid(
            e.encode_key_path([ok]), "data", e.GridConfigEntity(config=DATA_CFG)
        )
    for bad in (
        "",
        "Upper",
        "has.dot",
        "has/slash",
        "_leading",
        "-leading",
        "sp ace",
        "unié",
        "a" * (e.GRID_KEY_MAX_LEN + 1),
    ):
        with pytest.raises(EncodingError, match="invalid grid key"):
            await session.create_grid(
                e.encode_key_path([bad]), "data", e.GridConfigEntity(config=DATA_CFG)
            )
    # Every key of the path is checked, not just the leaf.
    with pytest.raises(EncodingError, match="invalid grid key"):
        await session.create_grid(
            e.encode_key_path(["Bad", "leaf"]), "data", e.GridConfigEntity(config=DATA_CFG)
        )
