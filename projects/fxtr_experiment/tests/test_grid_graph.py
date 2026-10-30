"""The grid-graph overlay behind the plate view (``notes/plate_view.md``):
the pure structure builder (input-grant decode + ``Param`` axis
correspondence resolution, including heterogeneous names, prefix-position
bindings, and the coord/dangling/kept skips), the edge aggregation over a
real run, and the ``watch_graph`` snapshot/turnover delivery.

Reuses ``test_watches.py``'s ``_wired`` runner + ``WatchRecorder`` and
``test_user_surface.py``'s ``EchoTask`` harness.
"""

from pathlib import Path

import pytest
from frozendict import frozendict

from fxtr_experiment import encoding as enc
from fxtr_experiment_task import model as m
from fxtr_experiment.introspect import (
    AxisCorrespondenceRow,
    InputGrantRow,
    build_graph_structure,
)
from fxtr_experiment.watch_api import GraphWatchEvent

from .test_introspection import _value
from .test_user_surface import FN_AXES, X_AXES, EchoTask, _bind, _local_runner, _paths
from .test_watches import WatchRecorder, _first_batch_after_recomputing, _wired


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _p(*keys: str) -> str:
    return enc.encode_key_path(list(keys))


ROOT = _p()


def _fns_config() -> m.HandleGridConfig:
    return m.HandleGridConfig(axes=(m.AxisSpec("fn", "str"),))


def _task_fn_slice() -> m.SliceTemplate:
    """A task_fn slice pinning ``fns[fn="echo"]`` (a literal — no Param)."""
    return m.SliceTemplate(
        path=(m.SubgridStepTemplate(cell=(), key="fns"),),
        target_axes=(m.LiteralCoord("echo"),),
    )


# -- 1. the pure structure builder ------------------------------------------------


def test_build_structure_grants_and_correspondence() -> None:
    """A top-level ``inputs`` data grid (axis ``x``) vmap-consumed by a task
    ``t`` (own axis ``x``): the task_fn slice and the input both yield grant
    edges, and the ``Param`` binding yields one ``inputs.x ~ t.x``
    correspondence (canonically ordered)."""
    configs: dict[str, m.GridConfig] = {
        ROOT: m.SubtreeGridConfig(axes=()),
        _p("fns"): _fns_config(),
        _p("inputs"): m.DataGridConfig(axes=(m.AxisSpec("x", "str"),)),
        _p("t"): m.TaskGridConfig(
            axes=(m.AxisSpec("x", "str"),),
            rule=m.TaskRule(
                task_fn=_task_fn_slice(),
                inputs=frozendict(
                    {
                        "inp": m.SliceBinding(
                            slice=m.SliceTemplate(
                                path=(m.SubgridStepTemplate(cell=(), key="inputs"),),
                                target_axes=(m.Param(0),),
                            ),
                            mode="read",
                            received_axis_names=(),
                        )
                    }
                ),
            ),
            outputs=m.DEFAULT_TASK_OUTPUTS,
        ),
    }
    grants, corrs = build_graph_structure(configs)
    assert set(grants) == {
        InputGrantRow(_p("t"), _p("fns"), "", True, "read", False),
        InputGrantRow(_p("t"), _p("inputs"), "inp", False, "read", False),
    }
    # Canonical order: ("inputs", "x") sorts before ("t", "x").
    assert corrs == (AxisCorrespondenceRow(_p("inputs"), "x", _p("t"), "x"),)


def test_build_structure_heterogeneous_names_and_prefix_position() -> None:
    """A task inside a subtree (``box``, axis ``g``) vmaps a top-level
    ``src`` (axis ``s``) over its enclosing subtree's axis: the
    correspondence resolves the consuming side to the *subtree*'s axis (a
    prefix position, not the task's own) and merges axes with different
    names (``box.g ~ src.s``)."""
    configs: dict[str, m.GridConfig] = {
        ROOT: m.SubtreeGridConfig(axes=()),
        _p("fns"): _fns_config(),
        _p("src"): m.DataGridConfig(axes=(m.AxisSpec("s", "str"),)),
        _p("box"): m.SubtreeGridConfig(axes=(m.AxisSpec("g", "str"),)),
        _p("box", "t"): m.TaskGridConfig(
            axes=(),
            rule=m.TaskRule(
                task_fn=_task_fn_slice(),
                inputs=frozendict(
                    {
                        "d": m.SliceBinding(
                            slice=m.SliceTemplate(
                                path=(m.SubgridStepTemplate(cell=(), key="src"),),
                                target_axes=(m.Param(0),),
                            ),
                            mode="read",
                            received_axis_names=(),
                        )
                    }
                ),
            ),
            outputs=m.DEFAULT_TASK_OUTPUTS,
        ),
    }
    _grants, corrs = build_graph_structure(configs)
    assert corrs == (AxisCorrespondenceRow(_p("box"), "g", _p("src"), "s"),)


def test_build_structure_skips_coord_dangling_and_marks_kept() -> None:
    """Coordinate inputs produce no edge; a ``KeepAxis`` input is a grant
    with ``kept=True`` and no correspondence; a slice to a missing grid
    still yields a grant (its mode preserved) but no correspondence (an
    unresolvable target frame)."""
    configs: dict[str, m.GridConfig] = {
        ROOT: m.SubtreeGridConfig(axes=()),
        _p("fns"): _fns_config(),
        _p("keep_src"): m.DataGridConfig(axes=(m.AxisSpec("k", "str"),)),
        _p("t"): m.TaskGridConfig(
            axes=(),
            rule=m.TaskRule(
                task_fn=_task_fn_slice(),
                inputs=frozendict(
                    {
                        "coord": m.CoordBinding(
                            kind="str", template=m.KeepAxis(), received_axis_name="c"
                        ),
                        "kept": m.SliceBinding(
                            slice=m.SliceTemplate(
                                path=(m.SubgridStepTemplate(cell=(), key="keep_src"),),
                                target_axes=(m.KeepAxis(),),
                            ),
                            mode="read",
                            received_axis_names=("k",),
                        ),
                        "gone": m.SliceBinding(
                            slice=m.SliceTemplate(
                                path=(m.SubgridStepTemplate(cell=(), key="gone"),),
                                target_axes=(),
                            ),
                            mode="write",
                            received_axis_names=(),
                        ),
                    }
                ),
            ),
            outputs=m.DEFAULT_TASK_OUTPUTS,
        ),
    }
    grants, corrs = build_graph_structure(configs)
    assert set(grants) == {
        InputGrantRow(_p("t"), _p("fns"), "", True, "read", False),
        InputGrantRow(_p("t"), _p("keep_src"), "kept", False, "read", True),
        InputGrantRow(_p("t"), _p("gone"), "gone", False, "write", False),
    }
    assert corrs == ()  # coord skipped, kept is not a Param, gone target absent


# -- 2. edges over a real run -----------------------------------------------------


@pytest.mark.anyio
async def test_grid_graph_edges_and_grants_e2e(tmp_path: Path) -> None:
    """A completed ``inputs -> t`` pipeline: ``grid_graph`` reports the
    rule's input grants (``t`` -> ``fns`` as the task_fn, ``t`` -> ``inputs``
    as ``inp``) and the run's read observation edges (``inputs`` and ``fns``
    both read by ``t``)."""
    from fxtr2.data.entity_ref_scope import FxtrScope

    state, ents = _paths(tmp_path)
    task = EchoTask()
    async with FxtrScope(name="test-host") as host:
        async with _local_runner(state, ents) as (wf, _session):
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            await inputs.write(_value("v1"), x="a")
            await _bind(wf, host, fns, "echo", task)
            t = await wf.root.open_task(
                "t", axes=(), task_fn=fns.at(fn="echo"), args={"inp": inputs.read_only()}
            )
            await wf.add_target(t)
            await t.output_entity().read()  # complete + fresh

            graph = await wf.grid_graph()
            grants = {
                (g.task_path, g.target_path, g.arg_name, g.is_task_fn) for g in graph.input_grants
            }
            assert (_p("t"), _p("fns"), "", True) in grants
            assert (_p("t"), _p("inputs"), "inp", False) in grants
            # `inp` is held with x kept open (the task received the grid view).
            inp_grant = next(g for g in graph.input_grants if g.arg_name == "inp")
            assert inp_grant.mode == "read" and inp_grant.kept is True

            reads = {(e.observer_path, e.subject_path) for e in graph.edges if e.kind == "read"}
            assert (_p("t"), _p("inputs")) in reads  # read the input cell
            assert (_p("t"), _p("fns")) in reads  # read its task_fn handle cell


# -- 3. the watch --------------------------------------------------------------------


@pytest.mark.anyio
async def test_watch_graph_snapshot_then_turnover(tmp_path: Path) -> None:
    """``watch_graph`` delivers a snapshot batch (a whole-value
    ``GraphUpdate`` after the slow-flagged ``recomputing``), and a grid
    creation redelivers the overlay with the new input grant."""
    async with _wired(tmp_path) as (wf, host, _runner, _session):
        fns = await wf.open_handle(wf.root, "fns", FN_AXES)
        inputs = await wf.root.open_data("inputs", X_AXES)
        await inputs.write(_value("v1"), x="a")

        sub = WatchRecorder(GraphWatchEvent)
        await wf.watch_graph(await host.mount(sub))
        await sub.wait_for(lambda es: bool([e for e in es if e.kind == "batch"]))
        snapshot = _first_batch_after_recomputing(sub)
        assert [p.kind for p in snapshot.patches] == ["graph"]
        # No task yet ⇒ no input grants in the first overlay.
        assert snapshot.patches[0].graph.input_grants == ()

        # Creating a task grid (a `grids` hint) redelivers the structure
        # half with the new grants.
        await wf.root.open_task(
            "t", axes=(), task_fn=fns.at(fn="echo"), args={"inp": inputs.read_only()}
        )
        await sub.wait_for(
            lambda es: any(
                p.kind == "graph" and p.graph.input_grants
                for b in [e for e in es if e.kind == "batch"][1:]
                for p in b.patches
            )
        )
