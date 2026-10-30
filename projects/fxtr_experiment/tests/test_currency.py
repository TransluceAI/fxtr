"""Currency unit tests over manually-constructed records: the tri-state
verdicts, settled-vs-unsettled subjects, the well-founded cycle behavior
(constructions the executor cannot legitimately produce), and the stamp
lifecycle — verification stamps settled generations, mutations clear
stamps per the trigger table (idempotent writes and record traffic mark
nothing), and stamps survive a session reopen."""

from pathlib import Path
from typing import AsyncIterator

import pytest
from frozendict import frozendict

from fxtr2.data.entity_core import FxtrID

from fxtr_experiment import encoding as enc
from fxtr_experiment_task import model as m
from fxtr_experiment.ops_engine import new_generation_id
from fxtr_experiment.state_store import WorkflowSession
from fxtr_experiment.storage import OBS_KIND_CELL_VALUE

from .test_ops_engine import _some_entity

A = enc.encode_key_path(["a"])
B = enc.encode_key_path(["b"])
INPUTS = enc.encode_key_path(["inputs"])
CELL = enc.encode_coords(())


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def s(tmp_path: Path) -> AsyncIterator[WorkflowSession]:
    async with WorkflowSession.open(
        str(tmp_path / "state.sqlite"), str(tmp_path / "entities.sqlite")
    ) as session:
        cfg = enc.GridConfigEntity(config=m.DataGridConfig(axes=(m.AxisSpec("x", "str"),)))
        await session.create_grid(INPUTS, "data", cfg)
        task_cfg = enc.GridConfigEntity(
            config=m.TaskGridConfig(
                axes=(),
                rule=m.TaskRule(
                    task_fn=m.SliceTemplate(path=(), target_axes=()), inputs=frozendict()
                ),
                outputs=m.DEFAULT_TASK_OUTPUTS,
            )
        )
        await session.create_grid(A, "task", task_cfg)
        await session.create_grid(B, "task", task_cfg)
        yield session


async def _generation(s: WorkflowSession, path: str, status: str = "complete") -> int:
    fn = _some_entity().store().entity_id
    exec_id = await s.insert_generation(new_generation_id(), path, CELL, fn, fn)
    await s.set_status(exec_id, status)
    return exec_id


async def _observe(
    s: WorkflowSession, exec_id: int, path: str, coords: str, output: str, fp: FxtrID
) -> None:
    version = await s.intern_version(path, coords, output, fp)
    await s.record_observation(exec_id, version, OBS_KIND_CELL_VALUE)


@pytest.mark.anyio
async def test_no_generation_is_none(s: WorkflowSession) -> None:
    assert await s.cell_currency(A, CELL) is None


@pytest.mark.anyio
async def test_fresh_and_stale_on_data_cell(s: WorkflowSession) -> None:
    v1 = await s.write_cell(INPUTS, '["a"]', _some_entity())
    exec_a = await _generation(s, A)
    await _observe(s, exec_a, INPUTS, '["a"]', "", v1)
    report = await s.cell_currency(A, CELL)
    assert report is not None and report.currency == "fresh" and report.reason is None
    # A settled user change is definite: stale, not unsettled.
    other = enc.SliceTemplateEntity(template=m.SliceTemplate(path=(), target_axes=()))
    await s.write_cell(INPUTS, '["a"]', other)
    report = await s.cell_currency(A, CELL)
    assert report is not None and report.currency == "stale"
    assert "different value" in (report.reason or "")


@pytest.mark.anyio
async def test_unsettled_support_without_settled_result(s: WorkflowSession) -> None:
    """B observed A's output; A's generation is now interrupted (no settled
    result) — B is unsettled, not stale."""
    fp = _some_entity().store().entity_id
    await _generation(s, A, status="interrupted")
    exec_b = await _generation(s, B)
    await _observe(s, exec_b, A, CELL, "result", fp)
    report = await s.cell_currency(B, CELL)
    assert report is not None and report.currency == "unsettled"
    assert "no settled result (interrupted)" in (report.reason or "")


@pytest.mark.anyio
async def test_settled_different_output_is_stale(s: WorkflowSession) -> None:
    """A's rerun settled on a different output value: B resolves to stale."""
    fp = _some_entity().store().entity_id
    exec_a = await _generation(s, A)
    other = enc.SliceTemplateEntity(template=m.SliceTemplate(path=(), target_axes=()))
    await s.record_result(exec_a, "result", other)
    exec_b = await _generation(s, B)
    await _observe(s, exec_b, A, CELL, "result", fp)
    report = await s.cell_currency(B, CELL)
    assert report is not None and report.currency == "stale"
    assert "settled output" in (report.reason or "")


@pytest.mark.anyio
async def test_matching_but_stale_support_is_unsettled(s: WorkflowSession) -> None:
    """A's output still matches what B observed, but A itself is stale (its
    input changed and it hasn't re-run): B is unsettled — whether it is
    condemned depends on how A's rerun resolves."""
    v1 = await s.write_cell(INPUTS, '["a"]', _some_entity())
    exec_a = await _generation(s, A)
    await _observe(s, exec_a, INPUTS, '["a"]', "", v1)
    result = await s.record_result(exec_a, "result", _some_entity())
    exec_b = await _generation(s, B)
    await _observe(s, exec_b, A, CELL, "result", result)
    other = enc.SliceTemplateEntity(template=m.SliceTemplate(path=(), target_axes=()))
    await s.write_cell(INPUTS, '["a"]', other)  # A stale; A's output unchanged
    a_report = await s.cell_currency(A, CELL)
    b_report = await s.cell_currency(B, CELL)
    assert a_report is not None and a_report.currency == "stale"
    assert b_report is not None and b_report.currency == "unsettled"
    assert "is stale" in (b_report.reason or "")


@pytest.mark.anyio
async def test_observation_cycle_parks_at_unsettled(s: WorkflowSession) -> None:
    """A cross-generation observation cycle with matching values proves
    nothing: freshness needs well-founded support, so both cells park at
    unsettled (never fresh, never stale)."""
    result_a = _some_entity().store().entity_id
    other = enc.SliceTemplateEntity(template=m.SliceTemplate(path=(), target_axes=(), output="b"))
    exec_a = await _generation(s, A)
    exec_b = await _generation(s, B)
    result_b = await s.record_result(exec_b, "result", other)
    await s.record_result(exec_a, "result", result_a)
    await _observe(s, exec_a, B, CELL, "result", result_b)  # A observed B
    await _observe(s, exec_b, A, CELL, "result", result_a)  # B observed A
    for path in (A, B):
        report = await s.cell_currency(path, CELL)
        assert report is not None and report.currency == "unsettled"
        assert "cycle" in (report.reason or "")


async def _stamp(s: WorkflowSession, path: str) -> tuple[str | None, str | None] | None:
    """A cell's raw stamp columns (verdict, reason), or None for no row."""

    def q(conn):
        return conn.execute(
            "SELECT currency_verdict, currency_reason FROM task_execs"
            " WHERE grid_path = ? AND coords = ?",
            (path, CELL),
        ).fetchone()

    return await s._run(q)


@pytest.mark.anyio
async def test_stamp_lifecycle(s: WorkflowSession) -> None:
    """Verification stamps settled generations; the trigger table clears:
    an idempotent write and record traffic mark nothing, a real change
    clears the stamp, and reverting the value resurrects freshness
    through the ordinary mark → re-verify cycle."""
    first = _some_entity()
    v1 = await s.write_cell(INPUTS, '["a"]', first)
    exec_a = await _generation(s, A)
    await _observe(s, exec_a, INPUTS, '["a"]', "", v1)
    assert await _stamp(s, A) == (None, None)  # settled but never verified
    report = await s.cell_currency(A, CELL)
    assert report is not None and report.currency == "fresh"
    assert await _stamp(s, A) == ("fresh", None)

    # Idempotent same-value write: no change, no mark.
    await s.write_cell(INPUTS, '["a"]', first)
    assert await _stamp(s, A) == ("fresh", None)

    # A real change clears the stamp in the same transaction ...
    other = enc.SliceTemplateEntity(template=m.SliceTemplate(path=(), target_axes=()))
    await s.write_cell(INPUTS, '["a"]', other)
    assert await _stamp(s, A) == (None, None)
    # ... and verification stamps the stale verdict too.
    report = await s.cell_currency(A, CELL)
    assert report is not None and report.currency == "stale"
    verdict, reason = (await _stamp(s, A)) or (None, None)
    assert verdict == "stale" and "different value" in (reason or "")

    # Resurrection: revert the value; mark → re-verify → fresh again.
    await s.write_cell(INPUTS, '["a"]', first)
    assert await _stamp(s, A) == (None, None)
    report = await s.cell_currency(A, CELL)
    assert report is not None and report.currency == "fresh"
    assert await _stamp(s, A) == ("fresh", None)


@pytest.mark.anyio
async def test_record_traffic_marks_nothing(s: WorkflowSession) -> None:
    """A running generation's record inserts clear no stamps: nothing
    cached can depend on an in-flight record (stamps only exist on
    settled generations, and this one's own verdict is gated on its
    status flip)."""
    v1 = await s.write_cell(INPUTS, '["a"]', _some_entity())
    exec_a = await _generation(s, A)
    await _observe(s, exec_a, INPUTS, '["a"]', "", v1)
    report = await s.cell_currency(A, CELL)
    assert report is not None and report.currency == "fresh"
    exec_b = await _generation(s, B, status="running")
    version = await s.intern_version(INPUTS, '["a"]', "", v1)
    await s.record_observation(exec_b, version, OBS_KIND_CELL_VALUE)
    await s.record_result(exec_b, "result", _some_entity())
    assert await _stamp(s, A) == ("fresh", None)  # untouched by record traffic


@pytest.mark.anyio
async def test_marks_propagate_up_observers(s: WorkflowSession) -> None:
    """Clearing propagates transitively: verifying B stamps its support
    (A) too, and a change under A clears both stamps in one mutation."""
    v1 = await s.write_cell(INPUTS, '["a"]', _some_entity())
    exec_a = await _generation(s, A)
    await _observe(s, exec_a, INPUTS, '["a"]', "", v1)
    result = await s.record_result(exec_a, "result", _some_entity())
    exec_b = await _generation(s, B)
    await _observe(s, exec_b, A, CELL, "result", result)
    report = await s.cell_currency(B, CELL)
    assert report is not None and report.currency == "fresh"
    # Verifying B stamped the whole settled support cone.
    assert await _stamp(s, A) == ("fresh", None)
    assert await _stamp(s, B) == ("fresh", None)
    other = enc.SliceTemplateEntity(template=m.SliceTemplate(path=(), target_axes=()))
    await s.write_cell(INPUTS, '["a"]', other)  # one mutation clears both
    assert await _stamp(s, A) == (None, None)
    assert await _stamp(s, B) == (None, None)


@pytest.mark.anyio
async def test_unsettled_running_stamp_invalidated_by_the_flip(s: WorkflowSession) -> None:
    """B's unsettled-because-running stamp is cleared by A's terminal
    status flip (the completion is the visibility/marking event)."""
    fp = _some_entity().store().entity_id
    exec_a = await _generation(s, A, status="running")
    exec_b = await _generation(s, B)
    await _observe(s, exec_b, A, CELL, "result", fp)
    report = await s.cell_currency(B, CELL)
    assert report is not None and report.currency == "unsettled"
    assert await _stamp(s, B) == ("unsettled", report.reason)
    await s.record_result(
        exec_a, "result", enc.SliceTemplateEntity(template=m.SliceTemplate(path=(), target_axes=()))
    )
    assert await _stamp(s, B) == ("unsettled", report.reason)  # results don't mark
    await s.set_status(exec_a, "complete")  # the flip does
    assert await _stamp(s, B) == (None, None)
    report = await s.cell_currency(B, CELL)
    assert report is not None and report.currency == "stale"  # observed fp differs


@pytest.mark.anyio
async def test_stamps_survive_reopen(tmp_path: Path) -> None:
    """Stamps are durable: a fresh verdict stamped in one session serves
    in the next (crash between a mutation and its marks is impossible —
    they share a transaction — so a reopened db is always consistent)."""
    state, ents = str(tmp_path / "state.sqlite"), str(tmp_path / "entities.sqlite")
    async with WorkflowSession.open(state, ents) as s1:
        cfg = enc.GridConfigEntity(config=m.DataGridConfig(axes=(m.AxisSpec("x", "str"),)))
        await s1.create_grid(INPUTS, "data", cfg)
        task_cfg = enc.GridConfigEntity(
            config=m.TaskGridConfig(
                axes=(),
                rule=m.TaskRule(
                    task_fn=m.SliceTemplate(path=(), target_axes=()), inputs=frozendict()
                ),
                outputs=m.DEFAULT_TASK_OUTPUTS,
            )
        )
        await s1.create_grid(A, "task", task_cfg)
        v1 = await s1.write_cell(INPUTS, '["a"]', _some_entity())
        exec_a = await _generation(s1, A)
        await _observe(s1, exec_a, INPUTS, '["a"]', "", v1)
        report = await s1.cell_currency(A, CELL)
        assert report is not None and report.currency == "fresh"
    async with WorkflowSession.open(state, ents) as s2:
        assert await _stamp(s2, A) == ("fresh", None)
        report = await s2.cell_currency(A, CELL)
        assert report is not None and report.currency == "fresh"


@pytest.mark.anyio
async def test_fresh_report_is_value_pinned(s: WorkflowSession) -> None:
    """A complete cell's report carries the results validated at the same
    snapshot as the verdict — the fact consumers serve from."""
    v1 = await s.write_cell(INPUTS, '["a"]', _some_entity())
    exec_a = await _generation(s, A)
    await _observe(s, exec_a, INPUTS, '["a"]', "", v1)
    result = await s.record_result(exec_a, "result", _some_entity())
    report = await s.cell_currency(A, CELL)
    assert report is not None and report.currency == "fresh"
    assert report.status == "complete"
    assert report.results == {"result": result}
