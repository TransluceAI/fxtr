"""Axis-semantics scenario suite, irregular demand (phase 1 chunk 3).

The suite's second half: demand structures where only a subset of a grid's
coordinate space is ever used, and precise staleness propagation over
them. Same style as the regular suite — scenarios through the public
surface with exact assertions. Grids are unbounded partial functions:
"irregular" is a *demand pattern*, never a schema, so the triangle here is
just which cells got demanded (the open-map principle makes the rest
non-facts).
"""

import time
from pathlib import Path

import anyio
import pytest

from fxtr2.data.entity_ref_scope import FxtrScope

from fxtr_experiment_task import model as m
from fxtr_experiment_task.errors import CellFaultError
from fxtr_experiment.user_service import open_local
from fxtr_experiment_task.views import int_coord

from .test_axis_suite_regular import FN_AXES, _currencies, _short
from .test_coord import _marker
from .test_user_surface import _bind, _paths


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# The triangle: demand pattern, presence, staleness surgery
# ---------------------------------------------------------------------------


class TriCellTask:
    """t[i, j] reads edge[i] and edge[j]; on the diagonal it additionally
    reads the shared `special` cell (a conditional, data-shaped read)."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, ctx, /, *, edge, sp, ii, jj):
        self.calls += 1
        i, j = ii.value(), jj.value()
        vi = await edge.read(k=i)
        vj = await edge.read(k=j)
        tag = f"t{i}{j}-{_short(vi)}-{_short(vj)}"
        if i == j:
            tag += f"+{_short(await sp.read())}"
        return _marker(tag)


TRIANGLE = [(i, j) for i in range(3) for j in range(3) if i <= j]
DIAGONAL = [c for c in TRIANGLE if c[0] == c[1]]
OFF_DIAGONAL = [c for c in TRIANGLE if c[0] != c[1]]


async def _open_triangle(wf, fns, task_name: str = "tri"):
    """The triangle shape: edge[k] + a shared special cell → t[i, j]."""
    edge = await wf.root.open_data("edge", (m.AxisSpec("k", "int"),))
    special = await wf.root.open_data("special", ())
    t = await wf.root.open_task(
        "t",
        axes=(m.AxisSpec("i", "int"), m.AxisSpec("j", "int")),
        task_fn=fns.at(fn=task_name),
        args={
            "edge": edge.read_only(),
            "sp": special.read_only(),
            "ii": int_coord("i"),
            "jj": int_coord("j"),
        },
    )
    return edge, special, t


@pytest.mark.anyio
async def test_triangular_demand_and_surgery(tmp_path: Path) -> None:
    """Only the demanded triangle exists (no phantom work); an edit feeding
    the diagonal stales exactly the diagonal; reverting resurrects the
    whole set; clearing one stale cell recomputes only it; a below-diagonal
    cell demanded later is an ordinary new fact."""
    state, ents = _paths(tmp_path)
    task = TriCellTask()
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            await _bind(wf, host, fns, "tri", task)
            edge, special, t = await _open_triangle(wf, fns)
            for k in range(3):
                await edge.write(_marker(f"e{k}"), k=k)
            await special.write(_marker("sp"))

            for i, j in TRIANGLE:
                await wf.add_target(t, i=i, j=j)
                await t.output_entity().read(i=i, j=j)
            assert task.calls == 12  # 6 cells × (run + safety replay)

            # Presence: exactly the triangle — undemanded cells are not
            # facts (the open-map principle), not "pending" rows.
            listed = {c.coords for c in await wf.list_cells(t)}
            assert listed == set(TRIANGLE)

            # Surgery: the special cell feeds only the diagonal.
            diag_cells = [{"i": i, "j": j} for i, j in DIAGONAL]
            off_cells = [{"i": i, "j": j} for i, j in OFF_DIAGONAL]
            await wf.overwrite(special, _marker("sp-edited"))
            assert await _currencies(wf, t, diag_cells) == ["stale"] * 3
            assert await _currencies(wf, t, off_cells) == ["fresh"] * 3

            # Revert: resurrection of the whole set, no recomputation.
            await wf.overwrite(special, _marker("sp"))
            assert await _currencies(wf, t, diag_cells + off_cells) == ["fresh"] * 6
            assert task.calls == 12

            # Edit again, clear ONE stale cell: only it recomputes.
            await wf.overwrite(special, _marker("sp-edited"))
            await wf.clear(t, i=1, j=1)
            fresh_11 = await t.output_entity().read(i=1, j=1)
            assert fresh_11 == _marker(
                f"t11-{_short(_marker('e1'))}-{_short(_marker('e1'))}"
                f"+{_short(_marker('sp-edited'))}"
            )
            assert task.calls == 14  # exactly one cell re-ran (+ its replay)
            assert await _currencies(wf, t, [{"i": 0, "j": 0}, {"i": 2, "j": 2}]) == [
                "stale",
                "stale",
            ]

            # "Irregular" is a demand pattern, not a schema: a below-diagonal
            # cell demanded later is an ordinary new fact.
            await wf.add_target(t, i=2, j=0)
            below = await t.output_entity().read(i=2, j=0)
            assert below == _marker(f"t20-{_short(_marker('e2'))}-{_short(_marker('e0'))}")
            listed = {c.coords for c in await wf.list_cells(t)}
            assert listed == set(TRIANGLE) | {(2, 0)}


# ---------------------------------------------------------------------------
# Two-stage irregular pipeline: aggregation over the triangle
# ---------------------------------------------------------------------------


class TriAggTask:
    """Reads exactly the triangle cells of t — *concurrently*: independent
    subtasks fan out through an ordinary anyio task group (sanctioned:
    records are replay-matched as a set, so outer-level op order is free);
    the result is assembled in deterministic order."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, ctx, /, *, cells):
        self.calls += 1
        got: dict[tuple[int, int], str] = {}

        async def read_one(i: int, j: int) -> None:
            got[(i, j)] = _short(await cells.read(i=i, j=j))

        async with anyio.create_task_group() as tg:
            for i, j in TRIANGLE:
                tg.start_soon(read_one, i, j)
        return _marker("agg-" + "-".join(got[c] for c in TRIANGLE))


@pytest.mark.anyio
async def test_aggregation_over_triangle(tmp_path: Path) -> None:
    """The aggregator's observation set is the triangle: a stale root makes
    it unsettled; reverting resolves everything back to fresh with zero
    recomputation; and *new* facts outside the triangle (a below-diagonal
    cell computed later) never implicate it."""
    state, ents = _paths(tmp_path)
    task, agg_task = TriCellTask(), TriAggTask()
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            await _bind(wf, host, fns, "tri", task)
            await _bind(wf, host, fns, "agg", agg_task)
            edge, special, t = await _open_triangle(wf, fns)
            for k in range(3):
                await edge.write(_marker(f"e{k}"), k=k)
            await special.write(_marker("sp"))
            agg = await wf.root.open_task(
                "agg", axes=(), task_fn=fns.at(fn="agg"), args={"cells": t.output()}
            )

            await wf.add_target(agg)
            first = await agg.output_entity().read()  # drives the whole triangle
            assert task.calls == 12 and agg_task.calls == 2

            # Stale root → dependent diagonal stale → aggregator unsettled.
            await wf.overwrite(special, _marker("sp-edited"))
            assert (await wf.inspect(t, i=0, j=0)).currency == "stale"
            agg_report = await wf.inspect(agg)
            assert agg_report.currency == "unsettled"
            assert agg_report.reason is not None and "support" in agg_report.reason
            with pytest.raises(CellFaultError):
                await agg.output_entity().read()  # the strict gate refuses

            # Resolve by reverting: the whole pipeline resurrects, free.
            await wf.overwrite(special, _marker("sp"))
            assert (await wf.inspect(agg)).currency == "fresh"
            assert await agg.output_entity().read() == first
            assert task.calls == 12 and agg_task.calls == 2  # nothing re-ran

            # Open-map: computing a NEW cell outside the triangle adds a
            # fact; adding facts never invalidates anyone.
            await wf.add_target(t, i=2, j=0)
            await t.output_entity().read(i=2, j=0)
            assert (await wf.inspect(agg)).currency == "fresh"

            # Resolve the other way: edit, clear the dependent slice, rerun.
            await wf.overwrite(special, _marker("sp-final"))
            for i, j in DIAGONAL:
                await wf.clear(t, i=i, j=j)
            await wf.clear(agg)
            second = await agg.output_entity().read()
            assert second != first
            # 12 (triangle) + 2 (the below-diagonal cell) + 6 (the three
            # cleared diagonal cells re-ran); agg ran twice in total.
            assert task.calls == 20 and agg_task.calls == 4
            assert (await wf.inspect(agg)).currency == "fresh"


# ---------------------------------------------------------------------------
# Concurrent demands from the top level (incl. same-cell fan-in)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_concurrent_demands_deduplicate(tmp_path: Path) -> None:
    """Concurrent top-level demands — including many of the SAME cell — run
    each generation exactly once. (The in-flight dedup must register before
    the demand's first await; without that, same-cell fan-in double-runs.)"""
    state, ents = _paths(tmp_path)
    task = TriCellTask()
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            await _bind(wf, host, fns, "tri", task)
            edge, special, t = await _open_triangle(wf, fns)
            for k in range(3):
                await edge.write(_marker(f"e{k}"), k=k)
            await special.write(_marker("sp"))

            # Target every cell up front (add_target is not the unit under
            # test here — concurrent demand dedup is); the reads stay raw.
            for i, j in TRIANGLE:
                await wf.add_target(t, i=i, j=j)

            async def read_cell(i: int, j: int) -> None:
                await t.output_entity().read(i=i, j=j)

            async with anyio.create_task_group() as tg:
                for i, j in TRIANGLE:
                    tg.start_soon(read_cell, i, j)
                for _ in range(8):
                    tg.start_soon(read_cell, 0, 0)  # same-cell fan-in
            assert task.calls == 12  # 6 cells × (run + safety), no doubles
            listed = {c.coords for c in await wf.list_cells(t)}
            assert listed == set(TRIANGLE)


# ---------------------------------------------------------------------------
# Data-dependent selection (the sanctioned data-dependent-read pattern)
# ---------------------------------------------------------------------------


class SelectorTask:
    """Reads a control cell, then reads only the input cells it names.

    The mapping from control values (entity ids) to selections is the
    task's own logic (test-side: a closure table); what the system sees is
    a run whose *observation set* depends on data it read.
    """

    def __init__(self, choices) -> None:
        self.choices = choices
        self.calls = 0

    async def __call__(self, ctx, /, *, ctrl, x):
        self.calls += 1
        which = await ctrl.read()
        parts = [_short(await x.read(k=k)) for k in self.choices[which]]
        return _marker("sel-" + "-".join(parts))


@pytest.mark.anyio
async def test_data_dependent_selection(tmp_path: Path) -> None:
    """The observation set follows the control: only the named cells
    implicate the task; changing the control reshapes the set on the fresh
    generation after a clear; a control naming a missing cell interrupts
    and resumes (replay hits) once the cell exists."""
    state, ents = _paths(tmp_path)
    ctrl_a, ctrl_b, ctrl_missing = _marker("ctrl-a"), _marker("ctrl-b"), _marker("ctrl-x")
    task = SelectorTask({ctrl_a: (0, 1), ctrl_b: (2, 3), ctrl_missing: (9,)})
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            await _bind(wf, host, fns, "sel", task)
            ctrl = await wf.root.open_data("ctrl", ())
            x = await wf.root.open_data("x", (m.AxisSpec("k", "int"),))
            for k in range(4):
                await x.write(_marker(f"x{k}"), k=k)
            await ctrl.write(ctrl_a)
            t = await wf.root.open_task(
                "t",
                axes=(),
                task_fn=fns.at(fn="sel"),
                args={"ctrl": ctrl.read_only(), "x": x.read_only()},
            )

            await wf.add_target(t)
            first = await t.output_entity().read()
            assert first == _marker(f"sel-{_short(_marker('x0'))}-{_short(_marker('x1'))}")

            # Only the *named* cells implicate the task.
            await wf.overwrite(x, _marker("x2-edited"), k=2)
            assert (await wf.inspect(t)).currency == "fresh"
            await wf.overwrite(x, _marker("x0-edited"), k=0)
            assert (await wf.inspect(t)).currency == "stale"
            await wf.overwrite(x, _marker("x0"), k=0)  # revert

            # Changing the control reshapes the observation set (after the
            # v0 clear): x0 stops mattering, x3 starts.
            await wf.overwrite(ctrl, ctrl_b)
            assert (await wf.inspect(t)).currency == "stale"
            await wf.clear(t)
            second = await t.output_entity().read()
            assert second == _marker(f"sel-{_short(_marker('x2-edited'))}-{_short(_marker('x3'))}")
            await wf.overwrite(x, _marker("x0-again"), k=0)
            assert (await wf.inspect(t)).currency == "fresh"
            await wf.overwrite(x, _marker("x3-edited"), k=3)
            assert (await wf.inspect(t)).currency == "stale"

            # A control naming a missing cell: interrupted, then a resume
            # that replays the control read as a hit and goes live only at
            # the frontier.
            await wf.overwrite(ctrl, ctrl_missing)
            await wf.clear(t)
            calls_before = task.calls
            with pytest.raises(CellFaultError):
                await t.output_entity().read()
            assert (await wf.inspect(t)).status == "interrupted"
            assert task.calls == calls_before + 1  # one attempt, no replay
            await x.write(_marker("x9"), k=9)
            resumed = await t.output_entity().read()  # same generation, resumed
            assert resumed == _marker(f"sel-{_short(_marker('x9'))}")
            assert task.calls == calls_before + 3  # resume + safety replay


# ---------------------------------------------------------------------------
# Deliberately uncomfortable scale (chunk 4's motivation)
# ---------------------------------------------------------------------------

N_SCALE = 20  # triangle of a 20×20 grid → 210 cells


class CheapTask:
    async def __call__(self, ctx, /, *, edge, ii, jj):
        return _marker(f"c{ii.value()}-{jj.value()}-{_short(await edge.read(k=ii.value()))}")


class CheapAggTask:
    """Fans out every triangle read concurrently (the realistic pattern for
    independent subtasks: fire them all, let the runner sort it out)."""

    async def __call__(self, ctx, /, *, cells):
        n = 0

        async def read_one(i: int, j: int) -> None:
            nonlocal n
            await cells.read(i=i, j=j)
            n += 1

        async with anyio.create_task_group() as tg:
            for i in range(N_SCALE):
                for j in range(i, N_SCALE):
                    tg.start_soon(read_one, i, j)
        return _marker(f"agg-{n}")


@pytest.mark.anyio
async def test_triangle_at_scale(tmp_path: Path) -> None:
    """A few hundred cells driven through one aggregator demand: a smoke
    check that nothing in-process is accidentally quadratic, and a rough
    timing anchor for chunk 4 (the currency walk over ~200 generations —
    see the chunk 3 notes for captured numbers)."""
    state, ents = _paths(tmp_path)
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            await _bind(wf, host, fns, "cheap", CheapTask())
            await _bind(wf, host, fns, "agg", CheapAggTask())
            edge = await wf.root.open_data("edge", (m.AxisSpec("k", "int"),))
            for k in range(N_SCALE):
                await edge.write(_marker(f"e{k}"), k=k)
            t = await wf.root.open_task(
                "t",
                axes=(m.AxisSpec("i", "int"), m.AxisSpec("j", "int")),
                task_fn=fns.at(fn="cheap"),
                args={"edge": edge.read_only(), "ii": int_coord("i"), "jj": int_coord("j")},
            )
            agg = await wf.root.open_task(
                "agg", axes=(), task_fn=fns.at(fn="agg"), args={"cells": t.output()}
            )

            await wf.add_target(agg)
            t0 = time.monotonic()
            await agg.output_entity().read()
            compute_s = time.monotonic() - t0
            n_cells = N_SCALE * (N_SCALE + 1) // 2
            assert len(await wf.list_cells(t)) == n_cells

            # One edit, then one full currency evaluation over the DAG (the
            # in-process walk chunk 4 moves into the db).
            await wf.overwrite(edge, _marker("e0-edited"), k=0)
            t0 = time.monotonic()
            report = await wf.inspect(agg)
            walk_s = time.monotonic() - t0
            assert report.currency == "unsettled"
            stale = [c for c in await wf.list_cells(t) if c.currency == "stale"]
            assert len(stale) == N_SCALE  # row i=0 of the triangle
            print(
                f"\nscale smoke: {n_cells} cells computed in {compute_s:.2f}s;"
                f" one currency walk {walk_s * 1000:.1f}ms"
            )
