"""Read-sliced subtree views (phase 1 chunk 3.post1).

Full-frame is a property of creation capability: write-mode subtree views
stay total, read-mode subtree views slice like any other view (browsing
windows). Covered here: slice → open → read in one holder (top-level and
task-side — the ViewSel-carrying opens), forwarding a sliced read subtree
as a task arg, the boundaries (write-mode ``at()`` rejected, creates
through sliced read views refused, ``open_task`` through a sliced view
rejected), and internals browsing riding the same rule.
"""

from pathlib import Path

import pytest

from fxtr2.data.entity_ref_scope import FxtrScope

from fxtr_experiment_task import model as m
from fxtr_experiment_task.errors import CellFaultError, ViewAxisError
from fxtr_experiment.user_service import open_local
from fxtr_experiment_task.views import DataView

from .test_axis_suite_regular import FN_AXES, _short
from .test_coord import _marker
from .test_user_surface import _bind, _paths

V_AXES = (m.AxisSpec("v", "str"),)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_top_level_slice_then_open(tmp_path: Path) -> None:
    """A read-only subtree view slices into a per-cell browsing window:
    opens under it attach single-cell child views; creates are refused;
    write-mode slicing and open_task through a slice stay rejected."""
    state, ents = _paths(tmp_path)
    async with open_local(state, ents) as wf:
        ws = await wf.root.open_subtree("ws", V_AXES)
        cfg = await ws.open_data("cfg", ())
        await cfg.write(_marker("red-cfg"), v="red")
        await cfg.write(_marker("blue-cfg"), v="blue")

        # Slicing a write-mode subtree view always succeeds now; the
        # resulting window sheds create authority (slicing is look-only).
        with pytest.raises(ViewAxisError, match="create access"):
            await ws.at(v="red").open_data("new_grid", ())

        browse = ws.read_only().at(v="red")
        window = await browse.open_data("cfg", ())
        assert isinstance(window, DataView) and window.axes == ()
        assert await window.read() == _marker("red-cfg")

        # attach composes with the slice the same way the opens do.
        attached = await wf.attach(browse, "cfg")
        assert await attached.read() == _marker("red-cfg")  # type: ignore[attr-defined]

        # A sliced read view cannot create (no create access) ...
        with pytest.raises(ViewAxisError, match="create access"):
            await browse.open_data("new_grid", ())
        # ... and cannot open even an existing child writable: a window is
        # look-only, so the child comes back read-only and writes fault.
        window_ro = await browse.open_data("cfg", ())
        with pytest.raises(ViewAxisError, match="not writable"):
            await window_ro.write(_marker("nope"))
        # ... and cannot be a creation path for a task grid (total frames).
        fns = await wf.open_handle(wf.root, "fns", FN_AXES)
        with pytest.raises(ViewAxisError, match="full-frame"):
            await browse.open_task("t", axes=(), task_fn=fns.at(fn="x"), args={})

        # Nested subtrees browse through the window: the child inherits
        # the grant's read mode (look-only all the way down).
        nest = await ws.open_subtree("nest", ())
        inner = await nest.open_data("inner", ())
        await inner.write(_marker("inner-red"), v="red")
        nest_window = await browse.open_subtree("nest", ())
        inner_window = await nest_window.open_data("inner", ())
        assert await inner_window.read() == _marker("inner-red")
        # The child opened through the window is read-only: writes fault.
        with pytest.raises(ViewAxisError, match="not writable"):
            await inner_window.write(_marker("nope"))


class PeekTask:
    """Browses one workspace cell two ways: through a pre-sliced window
    received as an arg, and by slicing a kept-axis subtree view itself
    (the holder-local slice → open path through the run capability)."""

    async def __call__(self, ctx, /, *, window, ws_all):
        via_arg = await (await window.open_data("cfg", ())).read()
        via_slice = await (await ws_all.at(v="red").open_data("cfg", ())).read()
        assert via_arg == via_slice
        return _marker(f"peek-{_short(via_arg)}")


@pytest.mark.anyio
async def test_sliced_subtree_through_tasks(tmp_path: Path) -> None:
    """Sliced read subtrees flow into rules (the literal bakes into the
    stored binding) and slice task-side; the task's observations follow
    the window — only the browsed cell implicates it."""
    state, ents = _paths(tmp_path)
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            await _bind(wf, host, fns, "peek", PeekTask())
            ws = await wf.root.open_subtree("ws", V_AXES)
            cfg = await ws.open_data("cfg", ())
            await cfg.write(_marker("red-cfg"), v="red")
            await cfg.write(_marker("blue-cfg"), v="blue")
            t = await wf.root.open_task(
                "t",
                axes=(),
                task_fn=fns.at(fn="peek"),
                args={
                    "window": ws.read_only().at(v="red"),  # pre-sliced arg
                    "ws_all": ws.read_only(),  # kept axis, sliced task-side
                },
            )
            await wf.add_target(t)
            assert await t.output_entity().read() == _marker(f"peek-{_short(_marker('red-cfg'))}")

            # The observation set is the window: blue never implicates it.
            await wf.overwrite(cfg, _marker("blue-cfg-2"), v="blue")
            assert (await wf.inspect(t)).currency == "fresh"
            await wf.overwrite(cfg, _marker("red-cfg-2"), v="red")
            assert (await wf.inspect(t)).currency == "stale"


class EscalateTask:
    """Opens an existing child through an *unsliced* read-mode subtree grant
    (which comes back read-only) and tries to write through it — the
    mode-ceiling rejection (the op raises, poisoning the run)."""

    async def __call__(self, ctx, /, *, ws_all):
        child = await ws_all.open_data("cfg", ())
        await child.write(_marker("never"), v="red")
        return _marker("never")


class BrowseDeeperTask:
    """Descends through a read grant: slices a window, opens the nested
    subtree (the child inherits read), and reads through it."""

    async def __call__(self, ctx, /, *, ws_all):
        nest = await ws_all.at(v="red").open_subtree("nest", ())
        value = await (await nest.open_data("inner", ())).read()
        return _marker(f"deep-{_short(value)}")


@pytest.mark.anyio
async def test_child_mode_never_exceeds_grant_task_side(tmp_path: Path) -> None:
    """Task-side mode ceiling: a rule-granted read subtree view — sliced
    or not — cannot write-open even an existing child (the run faults);
    nested subtree opens inherit read, so browsing descends."""
    state, ents = _paths(tmp_path)
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            await _bind(wf, host, fns, "escalate", EscalateTask())
            await _bind(wf, host, fns, "browse", BrowseDeeperTask())
            ws = await wf.root.open_subtree("ws", V_AXES)
            cfg = await ws.open_data("cfg", ())
            await cfg.write(_marker("red-cfg"), v="red")
            nest = await ws.open_subtree("nest", ())
            inner = await nest.open_data("inner", ())
            await inner.write(_marker("inner-red"), v="red")

            t_bad = await wf.root.open_task(
                "t_bad",
                axes=(),
                task_fn=fns.at(fn="escalate"),
                args={"ws_all": ws.read_only()},
            )
            await wf.add_target(t_bad)
            with pytest.raises(CellFaultError):
                await t_bad.output_entity().read()

            t_ok = await wf.root.open_task(
                "t_ok",
                axes=(),
                task_fn=fns.at(fn="browse"),
                args={"ws_all": ws.read_only()},
            )
            await wf.add_target(t_ok)
            expected = _marker(f"deep-{_short(_marker('inner-red'))}")
            assert await t_ok.output_entity().read() == expected
