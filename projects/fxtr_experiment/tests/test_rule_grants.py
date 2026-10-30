"""Rule → run-grant translation tests: the executor's run-setup direction
(`view_spec.spec_from_*` + `spec_registry.mint_rule_grants`), against
hand-built canonical configs and a dict-backed config lookup."""

import pytest
from frozendict import frozendict

from fxtr_experiment_task import model as m
from fxtr_experiment.encoding import encode_key_path
from fxtr_experiment_task.errors import ViewAxisError
from fxtr_experiment.spec_registry import RuleGrants, SpecRegistry, mint_rule_grants
from fxtr_experiment_task.view_spec import (
    SubtreeViewSpec,
    TaskViewSpec,
    internals_spec,
    spec_from_binding,
    spec_from_task_fn,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# The variants-example shape: a task grid ws/rollouts (frame: variant,
# subject) whose rule reads its variant's cfg cell, one output of a grades
# task grid (keeping judge), and a table, executed by fns[fn="rollout"].

WS_CFG = m.SubtreeGridConfig(axes=(m.AxisSpec("variant", "str"),))
VARIANT_CFG = m.DataGridConfig(axes=(m.AxisSpec("variant", "str"),))
FNS_CFG = m.HandleGridConfig(axes=(m.AxisSpec("fn", "str"),))
TABLE_SCHEMA = (m.ColumnSpec("subject", "str"),)
TABLE_CFG = m.DataFrameGridConfig(axes=(), schema=TABLE_SCHEMA)
GRADES_CFG = m.TaskGridConfig(
    axes=(m.AxisSpec("subject", "str"), m.AxisSpec("judge", "str")),
    rule=m.TaskRule(
        task_fn=m.SliceTemplate(
            path=(m.SubgridStepTemplate(cell=(), key="fns"),),
            target_axes=(m.LiteralCoord("grade"),),
        ),
        inputs=frozendict(),
    ),
    outputs=frozendict({"score": m.EntityOutput()}),
)

TASK_FN_SLICE = m.SliceTemplate(
    path=(m.SubgridStepTemplate(cell=(), key="fns"),),
    target_axes=(m.LiteralCoord("rollout"),),
)
CFG_BINDING = m.SliceBinding(
    slice=m.SliceTemplate(
        path=(m.SubgridStepTemplate(cell=(), key="variant_cfg"),),
        target_axes=(m.Param(0),),
    ),
    mode="read",
    received_axis_names=(),
)
GRADES_BINDING = m.SliceBinding(
    slice=m.SliceTemplate(
        path=(m.SubgridStepTemplate(cell=(), key="grades"),),
        target_axes=(m.Param(1), m.KeepAxis()),
        output="score",
    ),
    mode="read",
    received_axis_names=("judge",),
)
TABLE_BINDING = m.SliceBinding(
    slice=m.SliceTemplate(
        path=(m.SubgridStepTemplate(cell=(), key="table"),),
        target_axes=(),
    ),
    mode="read",
    received_axis_names=(),
)
ROLLOUTS_CFG = m.TaskGridConfig(
    axes=(m.AxisSpec("subject", "str"),),
    rule=m.TaskRule(
        task_fn=TASK_FN_SLICE,
        inputs=frozendict({"cfg": CFG_BINDING, "grades": GRADES_BINDING, "table": TABLE_BINDING}),
    ),
    outputs=m.DEFAULT_TASK_OUTPUTS,
)

GRIDS: dict[str, tuple[str, m.GridConfig]] = {
    encode_key_path(["ws"]): ("subtree", WS_CFG),
    encode_key_path(["ws", "rollouts"]): ("task", ROLLOUTS_CFG),
    encode_key_path(["variant_cfg"]): ("data", VARIANT_CFG),
    encode_key_path(["fns"]): ("handle", FNS_CFG),
    encode_key_path(["grades"]): ("task", GRADES_CFG),
    encode_key_path(["table"]): ("dataframe", TABLE_CFG),
}


async def _lookup(key_path: str) -> tuple[str, m.GridConfig]:
    return GRIDS[key_path]


def test_spec_from_binding_attaches_names_and_kinds() -> None:
    spec = spec_from_binding(GRADES_BINDING, target_kind="task", flat_kinds=("str", "str"))
    assert spec.kind == "task"
    assert spec.path == GRADES_BINDING.slice.path  # templates pass through
    assert spec.target_axes == GRADES_BINDING.slice.target_axes
    assert spec.axes == (m.AxisSpec("judge", "str"),)  # received name + config kind
    assert spec.mode == "read"
    assert spec.output == "score"


def test_spec_from_binding_arity_errors() -> None:
    with pytest.raises(ViewAxisError):  # kinds don't cover the templates
        spec_from_binding(GRADES_BINDING, target_kind="task", flat_kinds=("str",))
    bad_names = m.SliceBinding(
        slice=GRADES_BINDING.slice, mode="read", received_axis_names=("judge", "extra")
    )
    with pytest.raises(ViewAxisError):  # names != kept positions
        spec_from_binding(bad_names, target_kind="task", flat_kinds=("str", "str"))


def test_spec_from_task_fn() -> None:
    spec = spec_from_task_fn(TASK_FN_SLICE)
    assert (spec.kind, spec.axes, spec.mode) == ("handle", (), "read")
    with pytest.raises(ViewAxisError):  # not single-cell
        spec_from_task_fn(
            m.SliceTemplate(
                path=(m.SubgridStepTemplate(cell=(), key="fns"),),
                target_axes=(m.KeepAxis(),),
            )
        )
    with pytest.raises(ViewAxisError):  # handle slices have no outputs
        spec_from_task_fn(
            m.SliceTemplate(
                path=(m.SubgridStepTemplate(cell=(), key="fns"),),
                target_axes=(m.LiteralCoord("x"),),
                output="oops",
            )
        )


def test_internals_spec_vmaps_every_flattened_axis() -> None:
    spec = internals_spec(("ws", "rollouts"), (1,), 1)
    assert spec.internals and spec.mode == "write" and spec.axes == ()
    assert spec.path == (
        m.SubgridStepTemplate(cell=(), key="ws"),
        m.SubgridStepTemplate(cell=(m.Param(0),), key="rollouts"),
    )
    assert spec.target_axes == (m.Param(1),)
    with pytest.raises(ViewAxisError):  # counts must match the path
        internals_spec(("ws", "rollouts"), (), 1)


@pytest.mark.anyio
async def test_mint_rule_grants_end_to_end() -> None:
    registry = SpecRegistry()
    grants = await mint_rule_grants(
        registry,
        ROLLOUTS_CFG,
        encode_key_path(["ws", "rollouts"]),
        _lookup,
        holder_coords=("v1", "gpt"),
    )
    assert isinstance(grants, RuleGrants)
    # The prologue's task-fn read: zero-axis handle grant.
    assert (grants.task_fn.kind, grants.task_fn.axes, grants.task_fn.mode) == (
        "handle",
        (),
        "read",
    )
    # Args: names, kinds, and per-kind extras all come from the live configs.
    assert set(grants.args) == {"cfg", "grades", "table"}
    assert grants.args["cfg"].axes == ()
    grades = grants.args["grades"]
    # A pre-selected output presents as its VALUE kind: the callee holds a
    # data view (an entity output), never "a task view with an output
    # selected" — the true spec keeps kind="task" + output runner-side.
    assert grades.kind == "data"
    assert grades.axes == (m.AxisSpec("judge", "str"),)
    assert grades.outputs is None
    grades_spec = registry.spec_of(grades.token)
    assert isinstance(grades_spec, TaskViewSpec) and grades_spec.output == "score"
    assert grants.args["table"].schema == TABLE_SCHEMA  # dataframe extras
    # Internals: registered spec is the full-frame pseudo-subtree.
    internals = registry.spec_of(grants.internals.token)
    assert isinstance(internals, SubtreeViewSpec) and internals.internals
    assert internals.target_axes == (m.Param(1),)
