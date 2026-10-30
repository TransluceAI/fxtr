"""Flip-time record persistence (phase-3 chunk 2): the standing invariant
``record_id`` non-NULL ⇔ terminal — terminal-with-NULL is
unrepresentable — maintained by ``set_status``'s choreography, the
startup crashed-run flip's identical gapless choreography, and running
flips clearing the pointer. `assert_record_invariant` is the whole-db
biconditional probe (content included: every pointed-at record equals a
fresh assembly), reusable by other suites.
"""

from pathlib import Path

import pytest

from fxtr2.data.entity_ref_scope import FxtrScope

from fxtr_experiment.state_store import WorkflowSession
from fxtr_experiment.user_service import open_local

from .test_generation_record import T_PATH, _seed_rich_generation
from .test_lint import _echo_pipeline
from .test_user_surface import _paths


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def assert_record_invariant(session: WorkflowSession) -> None:
    """The whole-db probe: record_id non-NULL ⇔ terminal, and every
    pointed-at record equals a fresh assembly from the rows."""
    for grid_path, coords, status, record_id in await session.list_generation_rows():
        if status == "running":
            assert record_id is None, f"running {grid_path}{coords} holds a record"
            continue
        assert record_id is not None, f"terminal {grid_path}{coords} has no record"
        row = await session.get_generation(grid_path, coords)
        assert row is not None
        loaded = await session.load_generation_record(record_id)
        assert loaded.record == await session.build_generation_record(row[0])


@pytest.mark.anyio
async def test_terminal_flip_stores_the_record(tmp_path: Path) -> None:
    """A terminal set_status assembles + persists + points, atomically
    with the status update; the stored content equals a fresh assembly."""
    state, ents = _paths(tmp_path)
    async with WorkflowSession.open(state, ents) as session:
        exec_id, _ = await _seed_rich_generation(session)
        await session.set_status(exec_id, "errored")
        ((_, _, status, record_id),) = [
            r for r in await session.list_generation_rows() if r[0] == T_PATH
        ]
        assert status == "errored" and record_id is not None
        loaded = await session.load_generation_record(record_id)
        assert loaded.record.status == "errored"
        assert [f.kind for f in loaded.record.failures] == ["fault", "error"]
        await assert_record_invariant(session)


@pytest.mark.anyio
async def test_startup_flip_records_crashed_runs(tmp_path: Path) -> None:
    """A generation left running by a crash is flipped to interrupted at
    the next open — record stored, RunnerCrash failure included, in the
    same gapless choreography as any terminal flip."""
    state, ents = _paths(tmp_path)
    async with WorkflowSession.open(state, ents) as session:
        await _seed_rich_generation(session)  # left running: the "crash"

    async with WorkflowSession.open(state, ents) as reopened:
        ((_, _, status, record_id),) = [
            r for r in await reopened.list_generation_rows() if r[0] == T_PATH
        ]
        assert status == "interrupted" and record_id is not None
        loaded = await reopened.load_generation_record(record_id)
        assert loaded.record.status == "interrupted"
        assert loaded.record.failures[-1].error_class == "RunnerCrash"
        await assert_record_invariant(reopened)


@pytest.mark.anyio
async def test_retry_reflip_restores_and_repoints(tmp_path: Path) -> None:
    """A running flip clears the pointer; the re-flip stores a *new*
    record (same generation uuid inside) and repoints; the superseded
    record entity stays loadable (version-log food)."""
    state, ents = _paths(tmp_path)
    async with WorkflowSession.open(state, ents) as session:
        exec_id, _ = await _seed_rich_generation(session)
        await session.set_status(exec_id, "errored")
        (row,) = [r for r in await session.list_generation_rows() if r[0] == T_PATH]
        rid1 = row[3]
        assert rid1 is not None

        await session.set_status(exec_id, "running")  # the retry begins
        (row,) = [r for r in await session.list_generation_rows() if r[0] == T_PATH]
        assert row[3] is None  # no at-rest record while in flight
        await assert_record_invariant(session)

        await session.set_status(exec_id, "interrupted")  # the retry's outcome
        (row,) = [r for r in await session.list_generation_rows() if r[0] == T_PATH]
        rid2 = row[3]
        assert rid2 is not None and rid2 != rid1  # status differs → new record
        old = await session.load_generation_record(rid1)  # superseded, retained
        new = await session.load_generation_record(rid2)
        assert old.record.generation == new.record.generation == "g1"
        await assert_record_invariant(session)


@pytest.mark.anyio
async def test_executor_runs_keep_the_invariant(tmp_path: Path) -> None:
    """The real execution paths (demand → run → safety pass → terminal
    flip; clear → re-demand) maintain the invariant with no special
    wiring — set_status is the one choke point."""
    state, ents = _paths(tmp_path)
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            await _echo_pipeline(wf, host)
    # Probe at the session level (open_local owns its session; the bare
    # reopen also exercises the startup flip finding nothing to do).
    async with WorkflowSession.open(state, ents) as session:
        rows = await session.list_generation_rows()
        assert [r[2] for r in rows] == ["complete"]
        await assert_record_invariant(session)
