"""Trivial smoke tests: the package imports and draft/canonical types construct."""

from frozendict import frozendict


def test_import() -> None:
    import fxtr_experiment  # noqa: F401
    import fxtr_experiment_task.errors  # noqa: F401
    import fxtr_experiment_task.model  # noqa: F401
    import fxtr_experiment_task.runner_api  # noqa: F401
    import fxtr_experiment_task.task_api  # noqa: F401
    import fxtr_experiment.user_api  # noqa: F401
    import fxtr_experiment_task.views  # noqa: F401


def test_canonical_constructs() -> None:
    """Stored (positional) form of a judges-over-transcripts task grid.

    The task grid lives at the root (no prefix axes), own axes (category,
    judge) → flattened positions 0, 1.
    """
    from fxtr_experiment_task import model as m

    rule = m.TaskRule(
        task_fn=m.SliceTemplate(
            path=(m.SubgridStepTemplate(cell=(), key="judges"),),
            target_axes=(m.Param(1),),  # judges[judge] ← flattened axis 1 (judge)
        ),
        inputs=frozendict(
            {
                "transcripts": m.SliceBinding(
                    slice=m.SliceTemplate(
                        path=(m.SubgridStepTemplate(cell=(), key="transcripts"),),
                        # transcripts[category, ix]: category ← axis 0, ix kept
                        target_axes=(m.Param(0), m.KeepAxis()),
                    ),
                    mode="read",
                    received_axis_names=("ix",),  # the task fn's name for the kept axis
                ),
            }
        ),
    )
    assert rule.task_fn.is_single_cell()
    transcripts_binding = rule.inputs["transcripts"]
    assert isinstance(transcripts_binding, m.SliceBinding)
    assert not transcripts_binding.slice.is_single_cell()
    outputs = frozendict(
        {
            "result": m.EntityOutput(),
            "scores": m.FrameOutput(
                schema=(m.ColumnSpec("score", "float"), m.ColumnSpec("evidence", "bytes"))
            ),
        }
    )
    config = m.TaskGridConfig(
        axes=(m.AxisSpec("category", "str"), m.AxisSpec("judge", "str")),
        rule=rule,
        outputs=outputs,
    )
    # Config equality is positional in addressing; names (axes, outputs,
    # schemas) are observable contract and participate too.
    assert config == m.TaskGridConfig(
        axes=(m.AxisSpec("category", "str"), m.AxisSpec("judge", "str")),
        rule=rule,
        outputs=outputs,
    )
    # Paths can name task internals without reserved string keys.
    path = (
        m.SubgridStep(cell=(), key="mh_pilot"),
        m.TaskInternalsStep(cell=(), key="judge_calls"),
    )
    assert m.CellRef(grid=path, coords=("p1",))


def test_view_contracts_exist() -> None:
    """The view calculus surface exists with the expected vocabulary."""
    from fxtr_experiment_task import errors as e
    from fxtr_experiment_task import views as v

    for cls in (
        v.DataView,
        v.DataFrameView,
        v.HandleView,
        v.TaskView,
        v.CheckpointView,
        v.SubtreeView,
    ):
        assert issubclass(cls, v.GridView)
    assert hasattr(v.GridView, "rename_axes") and hasattr(v.GridView, "at")
    assert hasattr(v.GridView, "read_only")  # the third pure rewrite (downgrade)
    assert hasattr(v.SubtreeView, "open_task") and hasattr(v.SubtreeView, "open_dataframe")
    assert hasattr(v.SubtreeView, "open_handle")  # attach-only from task code
    assert hasattr(v.TaskView, "output") and hasattr(v.DataFrameView, "gather")
    # Relational data moves as polars.DataFrame — there is no wrapper type;
    # the conversion/validation layer is tables.py.
    from fxtr_experiment_task import tables as t

    assert set(t.KIND_TO_DTYPE) == {"str", "int", "float", "bool", "bytes"}
    assert callable(t.make_frame) and callable(t.validate_and_align)
    # The wire contracts exist with the expected vocabulary.
    from fxtr_experiment_task import runner_api as r

    grant = r.ViewGrant(token=r.ViewToken("args:judge"), kind="handle", axes=())
    sel = r.ViewSel(token=grant.token, bound=frozendict({0: "p1"}))
    from fxtr_experiment_task.view_spec import KeepName

    assert r.WiredArg(sel=sel, wiring=(KeepName("ix"),))
    for proto in (r.RunCapabilityProto, r.StepCapabilityProto, r.WorkflowTaskFnProto):
        assert hasattr(proto, "__protocol_attrs__") or True  # importable protocols
    assert hasattr(r.RunCapabilityProto, "run_step")
    assert hasattr(r.StepCapabilityProto, "connect")
    assert hasattr(r.WorkflowTaskFnProto, "run_task")
    from fxtr_experiment import user_api as u

    for name in ("root", "add_target", "clear_cells", "delete_grid", "list_cells"):
        assert hasattr(u.UserCapabilityProto, name)
    from fxtr_experiment import user_service as us

    assert hasattr(us.Workflow, "run_targets")
    # All workflow errors (including view misuse) share the poisoning base.
    for err in (
        e.CellFaultError,
        e.WriteConflictError,
        e.GridConfigMismatchError,
        e.ViewAxisError,
        e.CheckpointCorruptionError,
        e.NondeterminismError,
    ):
        assert issubclass(err, e.WorkflowError)
