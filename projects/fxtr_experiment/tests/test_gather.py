"""Gather (phase 5 chunk 4): the batched, host-stitched frame read.

Engine semantics over real storage (per-cell record integration, replay
hits, fault-and-resume, read-xor-write, the degenerate shapes), the
executor e2e (the MH-aggregate-shaped scenario over frame outputs, the
concurrent-demand rendezvous proof, fan-in dedup), the local-join error
surface, and the user capability's twin (gated reads, per-cell targets).
"""

from pathlib import Path
from typing import AsyncIterator

import anyio
import polars as pl
import pytest

from fxtr2.data.entity_ref_scope import FxtrScope

from fxtr_experiment import encoding as enc
from fxtr_experiment_task import model as m
from fxtr_experiment_task import tables
from fxtr_experiment_task.errors import (
    GatherFailedError,
    ReadWriteExclusionError,
    UngroundedSubjectError,
    ViewAxisError,
)
from fxtr_experiment.frame_encoding import build_frame_entity
from fxtr_experiment.state_store import WorkflowSession
from fxtr_experiment.user_service import open_local
from fxtr_experiment_task.views import str_coord

from .test_frame_cell_ops import ROWS, SCHEMA, canonical
from .test_ops_engine import Harness
from .test_user_surface import FN_AXES, _bind, _paths

X_AXES = (m.AxisSpec("x", "str"),)
FRAMES_PATH = enc.encode_key_path(["frames"])

SCORES = (m.ColumnSpec("item", "str"), m.ColumnSpec("score", "int"))


def _scores_rows(judge: str, cat: str) -> list[tuple]:
    return [(f"{judge}/{cat}/1", 1), (f"{judge}/{cat}/2", 2)]


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


async def _seed_frames(h: Harness, cells: dict[str, list[tuple]]) -> None:
    """Populate cells of the driver-opened 'frames' grid directly."""
    for x, rows in cells.items():
        await h.session.write_cell(FRAMES_PATH, f'["{x}"]', build_frame_entity(SCHEMA, rows))


def _drive(*xs: str, payload: bool = False) -> pl.DataFrame:
    cols: dict = {"x": list(xs)}
    if payload:
        cols["tag"] = [f"t{i}" for i in range(len(xs))]
    return pl.DataFrame(cols)


# -- engine semantics ------------------------------------------------------------------


@pytest.mark.anyio
async def test_engine_gather_serves_and_replays(h: Harness) -> None:
    """Canonical stitched serving, payload ride-through, empty-cell join
    arity, fan-in dedup, and the pure-hit replay of an identical gather."""
    engine = await h.engine()
    root = h.root(engine)
    d = await root.open_dataframe("frames", axes=X_AXES, schema=SCHEMA)
    await _seed_frames(h, {"a": ROWS, "b": [("z", 9)], "empty": []})
    driving = _drive("b", "a", "empty", "b", payload=True)  # dup key: fan-in
    out = await d.gather(driving, on={"x": "x"})
    assert out.columns == ["x", "tag", "k", "n"]
    # Driving order × canonical order; the empty cell's row vanished
    # (zero output rows, no null-padding); the duplicate key joined twice.
    assert out.rows() == (
        [("b", "t0", "z", 9)] + [("a", "t1", *r) for r in canonical(ROWS)] + [("b", "t3", "z", 9)]
    )
    # One observation per distinct cell (fan-in dedup), not per driving row.
    record = await h.session.load_record(h.generation)
    assert {c for g, c, *_ in record if g == FRAMES_PATH and c} == {'["a"]', '["b"]', '["empty"]'}
    # An identical gather replays as pure hits: no new record rows.
    n_rows = len(record)
    again = await d.gather(driving, on={"x": "x"})
    assert again.rows() == out.rows()
    assert len(await h.session.load_record(h.generation)) == n_rows
    assert not engine.run_failures


@pytest.mark.anyio
async def test_engine_gather_shapes_and_guards(h: Harness) -> None:
    """Empty key table, zero-open-axes cross join, read-xor-write, and a
    fault-then-resume across engines (recorded observations replay)."""
    engine = await h.engine()
    root = h.root(engine)
    d = await root.open_dataframe("frames", axes=X_AXES, schema=SCHEMA)
    d0 = await root.open_dataframe("flat", axes=(), schema=SCHEMA)
    await _seed_frames(h, {"a": ROWS})
    await h.session.write_cell(
        enc.encode_key_path(["flat"]), "[]", build_frame_entity(SCHEMA, ROWS)
    )

    # Empty key table: zero cells, zero observations, empty typed result.
    before = len(await h.session.load_record(h.generation))
    out = await d.gather(_drive(payload=True), on={"x": "x"})
    assert out.height == 0 and out.columns == ["x", "tag", "k", "n"]
    assert len(await h.session.load_record(h.generation)) == before

    # Zero open axes: the degenerate single-cell cross join.
    driving = pl.DataFrame({"tag": ["t0", "t1"]})
    out = await d0.gather(driving, on={})
    assert out.rows() == [("t0", *r) for r in canonical(ROWS)] + [
        ("t1", *r) for r in canonical(ROWS)
    ]

    # A mid-batch fault records the resolved observations; a fresh engine
    # (same generation) replays them as hits and resolves the rest. The
    # escaping signal names the failed cell; the durable row carries the
    # per-cell typed cause.
    with pytest.raises(GatherFailedError, match="missing"):
        await d.gather(_drive("a", "missing"), on={"x": "x"})
    assert any(cls == "CellFaultError" for _k, cls, _m in engine.run_failures)
    await _seed_frames(h, {"missing": [("m", 0)]})
    engine2 = await h.engine()
    root2 = h.root(engine2)
    d2 = await root2.open_dataframe("frames", axes=X_AXES, schema=SCHEMA)
    out = await d2.gather(_drive("a", "missing"), on={"x": "x"})
    assert out.rows() == [("a", *r) for r in canonical(ROWS)] + [("missing", "m", 0)]
    assert not engine2.run_failures

    # Read-xor-write: gathering a grid this generation wrote (fresh
    # generation — the one above holds the read side already).
    await h.session.delete_generation(h.exec_id)
    await h.seed_regen()
    engine3 = await h.engine()
    root3 = h.root(engine3)
    d3 = await root3.open_dataframe("frames", axes=X_AXES, schema=SCHEMA)
    await d3.write_table(tables.make_frame(SCHEMA, ROWS), x="w")
    with pytest.raises(ReadWriteExclusionError):
        await d3.gather(_drive("w"), on={"x": "x"})


@pytest.mark.anyio
async def test_local_join_error_surface(h: Harness) -> None:
    """The view-layer validation: on= coverage, duplicate driving columns,
    null keys, kind mismatches, prefix collisions."""
    engine = await h.engine()
    root = h.root(engine)
    d = await root.open_dataframe("frames", axes=X_AXES, schema=SCHEMA)
    await _seed_frames(h, {"a": ROWS})
    with pytest.raises(ViewAxisError, match="must bind exactly"):
        await d.gather(_drive("a"), on={})
    with pytest.raises(ViewAxisError, match="must bind exactly"):
        await d.gather(_drive("a"), on={"x": "x", "y": "x"})
    with pytest.raises(ViewAxisError, match="no key column"):
        await d.gather(pl.DataFrame({"other": ["a"]}), on={"x": "wrong"})
    with pytest.raises(ViewAxisError, match="null values"):
        await d.gather(pl.DataFrame({"x": ["a", None]}), on={"x": "x"})
    with pytest.raises(ViewAxisError, match="dtype"):
        await d.gather(pl.DataFrame({"x": [1, 2]}), on={"x": "x"})
    # A driving column colliding with an appended schema column.
    clash = pl.DataFrame({"x": ["a"], "k": ["payload"]})
    with pytest.raises(ViewAxisError, match="collides"):
        await d.gather(clash, on={"x": "x"})
    out = await d.gather(clash, on={"x": "x"}, column_prefix="g_")
    assert out.columns == ["x", "k", "g_k", "g_n"]
    # Two axes bound from one driving column are rejected up front.
    d2 = await root.open_dataframe(
        "two", axes=(m.AxisSpec("p", "str"), m.AxisSpec("q", "str")), schema=SCHEMA
    )
    with pytest.raises(ViewAxisError, match="distinct column"):
        await d2.gather(pl.DataFrame({"c": ["a"]}), on={"p": "c", "q": "c"})


@pytest.mark.anyio
async def test_gather_records_every_concurrent_failure(h: Harness) -> None:
    """Per-request failure semantics: cells failing concurrently each
    record their own failure row, classified from their own typed
    exception — and the synthesized GatherFailedError names them all
    while itself never being recorded (the k-parallel-reads semantics)."""
    from fxtr_experiment.storage import FAILURE_ERROR, FAILURE_FAULT

    ready = anyio.Event()
    seen: set[str] = set()

    async def gate(grid_path: str, coords: str):
        # Rendezvous: both cells' resolutions are in flight before either
        # raises, so both failures land before cancellation can.
        seen.add(coords)
        if len(seen) == 2:
            ready.set()
        await ready.wait()
        if coords == '["a"]':
            raise UngroundedSubjectError(f"cell {coords} is orphaned")  # fault-class
        raise ValueError(f"gate exploded on {coords}")  # unexpected: error-class

    engine = await h.engine(read_gate=gate)
    root = h.root(engine)
    d = await root.open_dataframe("frames", axes=X_AXES, schema=SCHEMA)
    await _seed_frames(h, {"a": ROWS, "b": ROWS})
    with pytest.raises(GatherFailedError) as ei:
        await d.gather(_drive("a", "b"), on={"x": "x"})
    # The signal names both cells with their error classes...
    msg = str(ei.value)
    assert "UngroundedSubjectError" in msg and "ValueError" in msg
    assert '["a"]' in msg and '["b"]' in msg
    # ...and the durable record carries one row per failed cell, each
    # classified from its own exception — no row for the signal itself.
    assert {(kind, cls) for kind, cls, _m in engine.run_failures} == {
        (FAILURE_FAULT, "UngroundedSubjectError"),
        (FAILURE_ERROR, "ValueError"),
    }
    assert len(engine.run_failures) == 2


# -- executor e2e: the MH-aggregate shape + concurrency ---------------------------------


class JudgeTask:
    """One frame output per (judge, cat) cell — the gathered upstream."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, ctx, /, *, judge, cat):
        self.calls += 1
        return tables.make_frame(SCORES, _scores_rows(judge.value(), cat.value()))


class AggregateTask:
    """Reads the domain frame, gathers the judges' outputs across it, and
    reduces — the MH-aggregate shape."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, ctx, /, *, dom, scores):
        self.calls += 1
        driving = await dom.read_table()
        joined = await scores.gather(driving, on={"judge": "judge", "cat": "cat"})
        agg = joined.group_by("judge").agg(pl.col("score").sum().alias("total")).sort("judge")
        return agg


DOM_SCHEMA = (m.ColumnSpec("judge", "str"), m.ColumnSpec("cat", "str"))
AGG_SCHEMA = (m.ColumnSpec("judge", "str"), m.ColumnSpec("total", "int"))


@pytest.mark.anyio
async def test_gather_aggregate_end_to_end(tmp_path: Path) -> None:
    """Domain-driven gather forces exactly the addressed judge cells, one
    observation per distinct cell, the aggregate reduces correctly, the
    safety pass replays the gathering task as pure hits, and a re-demand
    serves without re-running anything."""
    state, ents = _paths(tmp_path)
    judge, agg = JudgeTask(), AggregateTask()
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            await _bind(wf, host, fns, "judge", judge)
            await _bind(wf, host, fns, "agg", agg)
            dom = await wf.root.open_dataframe("dom", (), DOM_SCHEMA)
            await dom.write_table(
                tables.make_frame(
                    DOM_SCHEMA,
                    [("j1", "c1"), ("j1", "c2"), ("j2", "c1")],  # 3 of 4 combos
                )
            )
            judges = await wf.root.open_task(
                "judges",
                axes=(m.AxisSpec("judge", "str"), m.AxisSpec("cat", "str")),
                task_fn=fns.at(fn="judge"),
                args={"judge": str_coord("judge"), "cat": str_coord("cat")},
                outputs={"scores": m.FrameOutput(schema=SCORES)},
            )
            t_agg = await wf.root.open_task(
                "agg",
                axes=(),
                task_fn=fns.at(fn="agg"),
                args={"dom": dom.read_only(), "scores": judges.output_frame("scores")},
                outputs={"totals": m.FrameOutput(schema=AGG_SCHEMA)},
            )
            result = await wf.demand_table(t_agg.output_frame("totals"))
            # Each addressed judge cell ran once (+ its own safety replay);
            # the unaddressed (j2, c2) combo never ran.
            assert judge.calls == 3 * 2
            # The aggregate's safety pass replayed the gather as pure hits.
            assert agg.calls == 2
            assert result.rows() == [("j1", 6), ("j2", 3)]
            # One observation per distinct judge cell, whatever the row count.
            report = await wf.inspect_generation(t_agg)
            judge_obs = [
                o
                for o in report.observations
                if o.subject_path == enc.encode_key_path(["judges"]) and o.subject_coords
            ]
            assert len(judge_obs) == 3
            # Served, no re-run.
            assert (await wf.demand_table(t_agg.output_frame("totals"))).rows() == result.rows()
            assert judge.calls == 6 and agg.calls == 2


class RendezvousTask:
    """Each cell waits for the *other* cell's run to start — completes only
    if the gather demands them concurrently."""

    def __init__(self) -> None:
        self.started = {"a": anyio.Event(), "b": anyio.Event()}

    async def __call__(self, ctx, /, *, x):
        me = x.value()
        self.started[me].set()
        other = "b" if me == "a" else "a"
        await self.started[other].wait()
        return tables.make_frame(SCHEMA, [(me, 1)])


JOINED_SCHEMA = (m.ColumnSpec("x", "str"),) + SCHEMA


class FanoutGatherTask:
    async def __call__(self, ctx, /, *, cells):
        driving = tables.make_frame((m.ColumnSpec("x", "str"),), [("a",), ("b",)])
        return await cells.gather(driving, on={"x": "x"})


@pytest.mark.anyio
async def test_gather_demands_concurrently(tmp_path: Path) -> None:
    """The rendezvous proof: sequential per-cell demand would deadlock."""
    state, ents = _paths(tmp_path)
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            await _bind(wf, host, fns, "rendezvous", RendezvousTask())
            await _bind(wf, host, fns, "fanout", FanoutGatherTask())
            cells = await wf.root.open_task(
                "cells",
                axes=X_AXES,
                task_fn=fns.at(fn="rendezvous"),
                args={"x": str_coord("x")},
                outputs={"out": m.FrameOutput(schema=SCHEMA)},
            )
            t = await wf.root.open_task(
                "fan",
                axes=(),
                task_fn=fns.at(fn="fanout"),
                args={"cells": cells.output_frame("out")},
                outputs={"joined": m.FrameOutput(schema=JOINED_SCHEMA)},
            )
            with anyio.fail_after(20):
                result = await wf.demand_table(t.output_frame("joined"))
            assert set(result.rows()) == {("a", "a", 1), ("b", "b", 1)}


# -- the user surface --------------------------------------------------------------------


@pytest.mark.anyio
async def test_user_gather_rules(tmp_path: Path) -> None:
    """The user twin: gated dataframe gathers (ungrounded cells fault),
    per-cell target requirement over task outputs."""
    state, ents = _paths(tmp_path)
    judge = JudgeTask()
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            d = await wf.root.open_dataframe("d", X_AXES, SCHEMA)
            await d.write_table(tables.make_frame(SCHEMA, ROWS), x="a")
            out = await d.gather(_drive("a"), on={"x": "x"})
            assert out.rows() == [("a", *r) for r in canonical(ROWS)]
            # The grounding gate applies per cell: revoking the grid's
            # sourcing leaves the cell ungrounded. The signal is uniform
            # (GatherFailedError naming the per-cell cause).
            assert await wf.revoke_grid_sourcing(wf.root, "d", force=True)
            with pytest.raises(GatherFailedError, match="UngroundedSubjectError"):
                await d.gather(_drive("a"), on={"x": "x"})

            # Task outputs: every addressed cell needs an attached target.
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            await _bind(wf, host, fns, "judge", judge)
            judges = await wf.root.open_task(
                "judges",
                axes=(m.AxisSpec("judge", "str"), m.AxisSpec("cat", "str")),
                task_fn=fns.at(fn="judge"),
                args={"judge": str_coord("judge"), "cat": str_coord("cat")},
                outputs={"scores": m.FrameOutput(schema=SCORES)},
            )
            scores = judges.output_frame("scores")
            driving = tables.make_frame(DOM_SCHEMA, [("j1", "c1"), ("j2", "c1")])
            with pytest.raises(GatherFailedError, match="NotTargetedError"):
                await scores.gather(driving, on={"judge": "judge", "cat": "cat"})
            await wf.add_target(scores, judge="j1", cat="c1")
            await wf.add_target(scores, judge="j2", cat="c1")
            joined = await scores.gather(driving, on={"judge": "judge", "cat": "cat"})
            assert joined.height == 4  # 2 cells × 2 score rows
            assert judge.calls == 4  # 2 cells × (run + safety)
