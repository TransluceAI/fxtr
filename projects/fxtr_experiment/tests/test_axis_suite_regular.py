"""Axis-semantics scenario suite, regular shapes (phase 1 chunk 2).

Hand-written scenarios through the public surface (`open_local` → shared
views → demand via reads), with exact assertions on per-cell results and
freshness. Readability is the value: each scenario is one small story —
shared-axis coupling, the (dynamic) diagonal, whole-slice loop reads,
3-axis grids with renames and local coordinate names, subtree-axis
nesting with internals, and the MH-pilot shape end to end at toy scale
(including the stale → clear → recompute lifecycle and a session reopen).
"""

from pathlib import Path

import pytest

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope

from fxtr_experiment_task import model as m
from fxtr_experiment_task.errors import CellFaultError, ViewAxisError
from fxtr_experiment.user_service import Workflow, open_local
from fxtr_experiment_task.views import DataView, GridView, TaskView, int_coord, str_coord

from .test_coord import _marker
from .test_user_surface import _bind, _paths

FN_AXES = (m.AxisSpec("fn", "str"),)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _short(value: FxtrID) -> str:
    """A stable short tag for building result markers from input ids."""
    return value.id_string()[:8]


async def _currencies(wf: Workflow, view: GridView, cells: list[dict]) -> list[str]:
    """The currency verdict per addressed cell, in ``cells`` order."""
    return [(await wf.inspect(view, **coords)).currency or "?" for coords in cells]


# ---------------------------------------------------------------------------
# Shared-axis coupling: two inputs vmapped over one task axis
# ---------------------------------------------------------------------------


class PairTask:
    """Reads its two per-cell scalars (both inputs consumed by axis i)."""

    async def __call__(self, ctx, /, *, a, b):
        va = await a.read()
        vb = await b.read()
        return _marker(f"pair-{_short(va)}-{_short(vb)}")


@pytest.mark.anyio
async def test_shared_axis_coupling(tmp_path: Path) -> None:
    """input_a[i] and input_b[i] both feed cell i; overwriting input_a[2]
    makes exactly cell 2 stale."""
    state, ents = _paths(tmp_path)
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            await _bind(wf, host, fns, "pair", PairTask())
            a = await wf.root.open_data("a", (m.AxisSpec("i", "int"),))
            b = await wf.root.open_data("b", (m.AxisSpec("i", "int"),))
            for i in (1, 2, 3):
                await a.write(_marker(f"a{i}"), i=i)
                await b.write(_marker(f"b{i}"), i=i)
            t = await wf.root.open_task(
                "t",
                axes=(m.AxisSpec("i", "int"),),
                task_fn=fns.at(fn="pair"),
                args={"a": a.read_only(), "b": b.read_only()},
            )
            for i in (1, 2, 3):
                await wf.add_target(t, i=i)
            results = {i: await t.output_entity().read(i=i) for i in (1, 2, 3)}
            assert results[1] == _marker(f"pair-{_short(_marker('a1'))}-{_short(_marker('b1'))}")
            assert len(set(results.values())) == 3  # per-cell isolation

            await wf.overwrite(a, _marker("a2-edited"), i=2)
            cells = [{"i": 1}, {"i": 2}, {"i": 3}]
            assert await _currencies(wf, t, cells) == ["fresh", "stale", "fresh"]
            report = await wf.inspect(t, i=2)
            assert report.reason is not None and '["a"]' in report.reason


# ---------------------------------------------------------------------------
# Diagonal extraction (the sanctioned dynamic form: coord + kept axes)
# ---------------------------------------------------------------------------


class DiagTask:
    """Receives the whole matrix (both axes kept) + its own coordinate, and
    reads exactly its diagonal element."""

    async def __call__(self, ctx, /, *, mat, idx):
        i = idx.value()
        v = await mat.read(r=i, c=i)
        return _marker(f"diag{i}-{_short(v)}")


@pytest.mark.anyio
async def test_diagonal_extraction(tmp_path: Path) -> None:
    """task[i] reads matrix[i, i]: off-diagonal edits implicate nothing;
    matrix[2, 2] implicates exactly cell 2. Static diagonal *wiring* is
    inexpressible — the rename near-miss trips the collision error."""
    state, ents = _paths(tmp_path)
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            await _bind(wf, host, fns, "diag", DiagTask())
            mat = await wf.root.open_data("m", (m.AxisSpec("r", "int"), m.AxisSpec("c", "int")))
            for r in range(3):
                for c in range(3):
                    await mat.write(_marker(f"m{r}{c}"), r=r, c=c)

            # The one user-expressible near-miss of static diagonal wiring:
            # naming both axes "i" is an ordinary rename collision.
            with pytest.raises(ViewAxisError, match="duplicate"):
                mat.rename_axes(i="r").rename_axes(i="c")

            d = await wf.root.open_task(
                "d",
                axes=(m.AxisSpec("i", "int"),),
                task_fn=fns.at(fn="diag"),
                args={"mat": mat.read_only(), "idx": int_coord("i")},
            )
            for i in range(3):
                await wf.add_target(d, i=i)
            for i in range(3):
                assert await d.output_entity().read(i=i) == _marker(
                    f"diag{i}-{_short(_marker(f'm{i}{i}'))}"
                )

            cells = [{"i": 0}, {"i": 1}, {"i": 2}]
            await wf.overwrite(mat, _marker("edited"), r=2, c=1)  # off-diagonal
            assert await _currencies(wf, d, cells) == ["fresh", "fresh", "fresh"]
            await wf.overwrite(mat, _marker("edited-diag"), r=2, c=2)
            assert await _currencies(wf, d, cells) == ["fresh", "fresh", "stale"]


# ---------------------------------------------------------------------------
# Whole-slice input: a kept axis read in a loop
# ---------------------------------------------------------------------------


class SumTask:
    """Reads every row cell (kept axis, loop over a small known set)."""

    async def __call__(self, ctx, /, *, row, jdx):
        parts = [_short(await row.read(i=i)) for i in range(3)]
        return _marker(f"s{jdx.value()}-" + "-".join(parts))


@pytest.mark.anyio
async def test_whole_slice_input(tmp_path: Path) -> None:
    """task[j] reads row[i] for every i; every cell observes every row, so
    one row edit makes every task cell stale."""
    state, ents = _paths(tmp_path)
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            await _bind(wf, host, fns, "sum", SumTask())
            row = await wf.root.open_data("row", (m.AxisSpec("i", "int"),))
            for i in range(3):
                await row.write(_marker(f"row{i}"), i=i)
            s = await wf.root.open_task(
                "s",
                axes=(m.AxisSpec("j", "int"),),
                task_fn=fns.at(fn="sum"),
                args={"row": row.read_only(), "jdx": int_coord("j")},
            )
            await wf.add_target(s, j=0)
            await wf.add_target(s, j=1)
            expected_tail = "-".join(_short(_marker(f"row{i}")) for i in range(3))
            assert await s.output_entity().read(j=0) == _marker(f"s0-{expected_tail}")
            assert await s.output_entity().read(j=1) == _marker(f"s1-{expected_tail}")

            await wf.overwrite(row, _marker("row1-edited"), i=1)
            cells = [{"j": 0}, {"j": 1}]
            assert await _currencies(wf, s, cells) == ["stale", "stale"]


# ---------------------------------------------------------------------------
# 3-axis grid, renames along the way, local coordinate names
# ---------------------------------------------------------------------------


class TriTask:
    """Consumes one renamed input axis, loops the other, reads two coords."""

    async def __call__(self, ctx, /, *, a, xdx, ydx):
        vals = "-".join([_short(await a.read(w=w)) for w in ("u", "v")])
        return _marker(f"t3-{xdx.value()}-{ydx.value()}-{vals}")


@pytest.mark.anyio
async def test_three_axes_with_renames(tmp_path: Path) -> None:
    """Mixed consumed/kept axes under renames: the input's ``p`` is consumed
    through the local rename ``x``, its ``q`` is kept as ``w``; the ``y``
    coordinate is consumed through its *local* alias ``yy`` (local_names);
    staleness follows the consumed axis only."""
    state, ents = _paths(tmp_path)
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            await _bind(wf, host, fns, "tri", TriTask())
            a = await wf.root.open_data("a2", (m.AxisSpec("p", "str"), m.AxisSpec("q", "str")))
            for p in ("A", "B"):
                for q in ("u", "v"):
                    await a.write(_marker(f"a-{p}{q}"), p=p, q=q)
            t3 = await wf.root.open_task(
                "t3",
                axes=(m.AxisSpec("x", "str"), m.AxisSpec("y", "int")),
                local_names={"y": "yy"},
                task_fn=fns.at(fn="tri"),
                args={
                    "a": a.read_only().rename_axes(x="p", w="q"),
                    "xdx": str_coord("x"),
                    "ydx": int_coord("yy"),
                },
            )
            # The returned view speaks the holder's locals.
            assert [ax.name for ax in t3.axes] == ["x", "yy"]
            cells = [{"x": x, "yy": y} for x in ("A", "B") for y in (0, 1)]
            for coords in cells:
                await wf.add_target(t3, **coords)
            results = {}
            for coords in cells:
                results[tuple(coords.values())] = await t3.output_entity().read(**coords)
            expected_a = "-".join(_short(_marker(f"a-A{q}")) for q in ("u", "v"))
            assert results[("A", 0)] == _marker(f"t3-A-0-{expected_a}")
            assert len(set(results.values())) == 4

            await wf.overwrite(a, _marker("edited"), p="A", q="u")
            assert await _currencies(wf, t3, cells) == ["stale", "stale", "fresh", "fresh"]


# ---------------------------------------------------------------------------
# Subtree-axis nesting: prefix resolution, internals under a multi-axis cell
# ---------------------------------------------------------------------------


class NestTask:
    """Reads its prefix-vmapped config, learns both coordinates, and writes
    a private note into its per-cell internals."""

    async def __call__(self, ctx, /, *, cfg, which, idx):
        v = await cfg.read()
        notes = await ctx.own_internals().open_data("notes", ())
        await notes.write(_marker(f"note-{which.value()}-{idx.value()}"))
        return _marker(f"n-{which.value()}-{idx.value()}-{_short(v)}")


@pytest.mark.anyio
async def test_subtree_axis_nesting(tmp_path: Path) -> None:
    """A task grid inside a subtree with its own axis: flattened-prefix
    resolution (enclosing coord + own coord), vmap over the subtree axis,
    and internals under a multi-axis task cell."""
    state, ents = _paths(tmp_path)
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            await _bind(wf, host, fns, "nest", NestTask())
            ws = await wf.root.open_subtree("ws", (m.AxisSpec("v", "str"),))
            cfg = await ws.open_data("cfg", ())
            await cfg.write(_marker("cfg-red"), v="red")
            await cfg.write(_marker("cfg-blue"), v="blue")
            t = await ws.open_task(
                "t",
                axes=(m.AxisSpec("k", "int"),),
                task_fn=fns.at(fn="nest"),
                args={"cfg": cfg.read_only(), "which": str_coord("v"), "idx": int_coord("k")},
            )
            cells = [{"v": v, "k": k} for v in ("red", "blue") for k in (0, 1)]
            for coords in cells:
                await wf.add_target(t, **coords)
            for coords in cells:
                expected = _marker(
                    f"n-{coords['v']}-{coords['k']}-{_short(_marker('cfg-' + coords['v']))}"
                )
                assert await t.output_entity().read(**coords) == expected

            # Presence: exactly the computed cells, complete and fresh.
            listed = {c.coords: (c.status, c.currency) for c in await wf.list_cells(t)}
            assert listed == {
                (v, k): ("complete", "fresh") for v in ("red", "blue") for k in (0, 1)
            }

            # Internals browsing: each cell has its own private note.
            sub = await wf.internals(t, v="red", k=1)
            keys = {k.key for k in await wf.list_keys(sub)}
            assert "notes" in keys
            notes = await wf.attach(sub, "notes")
            report = await wf.inspect(notes)
            assert report.value == _marker("note-red-1")

            # Staleness follows the prefix-vmapped input.
            await wf.overwrite(cfg, _marker("cfg-red-edited"), v="red")
            assert await _currencies(wf, t, cells) == ["stale", "stale", "fresh", "fresh"]


# ---------------------------------------------------------------------------
# The MH-pilot shape end to end at toy scale
# ---------------------------------------------------------------------------

SUBJECTS = ("s0", "s1")
USIMS = ("u0", "u1")
JUDGES = ("j0", "j1")


class RolloutTask:
    """subject × usim → transcript."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, ctx, /, *, subject, usim):
        self.calls += 1
        return _marker(f"tr-{_short(await subject.read())}-{_short(await usim.read())}")


class GradeTask:
    """(subject, usim, judge) → grade of that transcript by that judge."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, ctx, /, *, transcript, jcfg):
        self.calls += 1
        return _marker(f"g-{_short(await transcript.read())}-{_short(await jcfg.read())}")


class AggTask:
    """Reads every grade (all axes kept; loop over the known sets)."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, ctx, /, *, grades):
        self.calls += 1
        parts = [
            _short(await grades.read(s=s, u=u, j=j))
            for s in SUBJECTS
            for u in USIMS
            for j in JUDGES
        ]
        return _marker("agg-" + "-".join(parts))


async def _mh_open(wf: Workflow, fns) -> tuple[DataView, TaskView, TaskView, TaskView]:
    """Open (or re-attach — the opens are create-or-attach) the MH shape."""
    subjects = await wf.root.open_data("subjects", (m.AxisSpec("s", "str"),))
    usims = await wf.root.open_data("usims", (m.AxisSpec("u", "str"),))
    jcfg = await wf.root.open_data("judges", (m.AxisSpec("j", "str"),))
    rollouts = await wf.root.open_task(
        "rollouts",
        axes=(m.AxisSpec("s", "str"), m.AxisSpec("u", "str")),
        task_fn=fns.at(fn="rollout"),
        args={"subject": subjects.read_only(), "usim": usims.read_only()},
    )
    grades = await wf.root.open_task(
        "grades",
        axes=(m.AxisSpec("s", "str"), m.AxisSpec("u", "str"), m.AxisSpec("j", "str")),
        task_fn=fns.at(fn="grade"),
        args={"transcript": rollouts.output(), "jcfg": jcfg.read_only()},
    )
    agg = await wf.root.open_task(
        "agg",
        axes=(),
        task_fn=fns.at(fn="agg"),
        args={"grades": grades.output()},
    )
    return usims, rollouts, grades, agg


@pytest.mark.anyio
async def test_mh_pilot_shape_end_to_end(tmp_path: Path) -> None:
    """The shape the design was sized against, actually run: one demand of
    the aggregate computes the whole DAG; an input edit propagates as
    stale → unsettled; the strict gate refuses until the user clears; the
    cleared slice recomputes against fresh neighbors; and the finished
    state survives a session reopen without re-running anything."""
    state, ents = _paths(tmp_path)
    rollout, grade, aggregate = RolloutTask(), GradeTask(), AggTask()
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            await _bind(wf, host, fns, "rollout", rollout)
            await _bind(wf, host, fns, "grade", grade)
            await _bind(wf, host, fns, "agg", aggregate)
            usims, rollouts, grades, agg = await _mh_open(wf, fns)
            subjects = await wf.root.open_data("subjects", (m.AxisSpec("s", "str"),))
            jcfg = await wf.root.open_data("judges", (m.AxisSpec("j", "str"),))
            for s in SUBJECTS:
                await subjects.write(_marker(f"cfg-{s}"), s=s)
            for u in USIMS:
                await usims.write(_marker(f"cfg-{u}"), u=u)
            for j in JUDGES:
                await jcfg.write(_marker(f"cfg-{j}"), j=j)

            # One demand of the root computes the whole DAG, depth-first.
            # Target the aggregate root (durable — survives the reopen below).
            await wf.add_target(agg)
            first = await agg.output_entity().read()
            assert (rollout.calls, grade.calls, aggregate.calls) == (8, 16, 2)
            # (4 rollouts + 8 grades + 1 agg, each run + safety replay.)
            assert len(await wf.list_cells(rollouts)) == 4
            assert len(await wf.list_cells(grades)) == 8
            assert all(c.currency == "fresh" for c in await wf.list_cells(grades))

            # An input edit: rollouts on u1 go stale; their observers are
            # unsettled (their support lost freshness, nothing re-ran).
            await wf.overwrite(usims, _marker("cfg-u1-edited"), u="u1")
            assert (await wf.inspect(rollouts, s="s0", u="u1")).currency == "stale"
            assert (await wf.inspect(rollouts, s="s0", u="u0")).currency == "fresh"
            assert (await wf.inspect(grades, s="s0", u="u1", j="j0")).currency == "unsettled"
            assert (await wf.inspect(grades, s="s0", u="u0", j="j0")).currency == "fresh"
            agg_report = await wf.inspect(agg)
            assert agg_report.currency == "unsettled"

            # The strict gate refuses to serve or recompute around it.
            with pytest.raises(CellFaultError):
                await agg.output_entity().read()

            # Repair = clear the affected slice (v0 policy: the user clears),
            # then re-demand: the cleared cells recompute against the fresh
            # remainder, nothing else re-runs.
            await wf.clear(rollouts, u="u1")
            await wf.clear(grades, u="u1")
            await wf.clear(agg)
            second = await agg.output_entity().read()
            assert second != first  # the edit is visible in the result
            assert (rollout.calls, grade.calls, aggregate.calls) == (12, 24, 4)
            # (2 rollouts + 4 grades + 1 agg re-ran, each with its replay.)
            assert all(c.currency == "fresh" for c in await wf.list_cells(grades))

        # Reopen: everything served from disk; no task runs at all. (The
        # wrapper's write-mode open_handle: _bind re-writes the handle
        # cells, idempotently — the shared view's open_handle is the
        # attach-only read shape.)
        async with open_local(state, ents) as wf2:
            fns2 = await wf2.open_handle(wf2.root, "fns", FN_AXES)
            await _bind(wf2, host, fns2, "rollout", rollout)
            await _bind(wf2, host, fns2, "grade", grade)
            await _bind(wf2, host, fns2, "agg", aggregate)
            _, rollouts2, grades2, agg2 = await _mh_open(wf2, fns2)
            assert await agg2.output_entity().read() == second
            assert (rollout.calls, grade.calls, aggregate.calls) == (12, 24, 4)
            listed = {c.coords for c in await wf2.list_cells(grades2)}
            assert listed == {(s, u, j) for s in SUBJECTS for u in USIMS for j in JUDGES}
