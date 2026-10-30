"""Canonicalizer tests: view specs + open_task composition (executable P2).

Recreates the MH-pilot example (notes/example_mh_pilot.md) at the spec level:
the flat case (root-frame creator) and the nested variants case (creator with
its own axes, correlation through the creation path). The canonicalizer
consumes positional wiring instructions; tests compute them with
``wire_by_names`` — the same holder-side strip-set logic the view layer uses.
"""

import pytest

from fxtr_experiment_task import model as m
from fxtr_experiment_task import view_spec as vs
from fxtr_experiment_task.errors import ViewAxisError

S = lambda name: m.AxisSpec(name, "str")  # noqa: E731


def root_view() -> vs.SubtreeViewSpec:
    """The top-level user's view of the zero-axis root namespace."""
    return vs.SubtreeViewSpec(path=(), target_axes=(), axes=(), mode="write")


def wired(
    spec: vs.ViewSpec, subtree: tuple[str, ...] = (), coords: tuple[str, ...] = ()
) -> vs.WiredSpecArg:
    """Holder-side wiring: strip-set over local names → positional instrs."""
    return vs.WiredSpecArg(
        spec, vs.wire_by_names(subtree, coords, tuple(a.name for a in spec.axes))
    )


# ---------------------------------------------------------------------------
# Rewrites
# ---------------------------------------------------------------------------


def _handle_grid(name: str, axes: tuple[m.AxisSpec, ...]) -> vs.LeafViewSpec:
    return vs.LeafViewSpec(
        kind="handle",
        path=(m.SubgridStepTemplate(cell=(), key=name),),
        target_axes=tuple(m.KeepAxis() for _ in axes),
        axes=axes,
    )


def test_bind_keeps() -> None:
    """Spec-level binding is positional only (names are runner bookkeeping;
    the named layer with unique locals is the view runtime's Shadow)."""
    v = _handle_grid("subjects", (S("subject"),))
    bound = vs.bind_keeps(v, {0: "gpt"})
    assert bound.axes == ()
    assert bound.target_axes == (m.LiteralCoord("gpt"),)
    with pytest.raises(ViewAxisError):
        vs.bind_keeps(v, {3: "x"})  # no such open axis
    with pytest.raises(ViewAxisError):
        vs.bind_keeps(v, {0: 3})  # kind mismatch
    with pytest.raises(ViewAxisError):
        vs.bind_keeps(v, {0: True})  # bool is not an int coordinate


def test_open_child_official_name_rules() -> None:
    """Official names must be unique within one grid's OWN axes (rejected);
    overlap across subtree levels is legal (per-grid uniqueness only — the
    user-facing *local*-name collision rule is task-side, see
    test_view_runtime)."""
    ws = vs.open_child(root_view(), "variants_ws", (S("variant"),), "subtree", mode="write")
    child = vs.open_child(ws, "inner", (S("variant"),), "subtree")
    assert [a.name for a in child.axes] == ["variant", "variant"]  # levels overlap: fine
    with pytest.raises(ViewAxisError, match="duplicate own-axis"):
        vs.open_child(ws, "bad", (S("x"), S("x")), "data")
    # positional binds are unambiguous regardless of repeated names:
    leaf = vs.open_child(child, "leaf", (), "data")
    assert vs.bind_keeps(leaf, {0: "v1"}).axes[0].name == "variant"


# ---------------------------------------------------------------------------
# Flat MH case: creator is the top-level user (empty holder frame)
# ---------------------------------------------------------------------------


def test_mh_flat_rollouts() -> None:
    root = root_view()
    fns = _handle_grid("fns", (S("fn"),))
    subjects = _handle_grid("subjects", (S("subject"),))
    usims = _handle_grid("usims", (S("usim"),))
    mh = vs.open_child(root, "mh", (), "subtree", mode="write")
    coords = ("subject", "usim")

    config, view = vs.canonicalize_open_task(
        mh,
        key="rollouts",
        axes=(S("subject"), S("usim")),
        task_fn=wired(vs.bind_keeps(fns, {0: "rollout"})),
        args={
            "subject": wired(subjects, coords=coords),
            "usim": wired(usims, coords=coords),
        },
        # outputs omitted → the single-entity default
    )
    assert config.outputs == m.DEFAULT_TASK_OUTPUTS
    assert config.outputs == {"result": m.EntityOutput()}
    # mh is a zero-axis subtree → no prefix; own axes are positions 0, 1.
    assert config.axes == (S("subject"), S("usim"))
    assert config.rule.task_fn == m.SliceTemplate(
        path=(m.SubgridStepTemplate(cell=(), key="fns"),),
        target_axes=(m.LiteralCoord("rollout"),),
    )
    assert config.rule.inputs["subject"] == m.SliceBinding(
        slice=m.SliceTemplate(
            path=(m.SubgridStepTemplate(cell=(), key="subjects"),),
            target_axes=(m.Param(0),),
        ),
        mode="read",
        received_axis_names=(),
    )
    usim = config.rule.inputs["usim"]
    assert isinstance(usim, m.SliceBinding)
    assert usim.slice.target_axes == (m.Param(1),)
    assert [a.name for a in view.axes] == ["subject", "usim"]
    assert view.kind == "task"

    # agg: everything kept → received names recorded, in flattened order.
    grades = vs.TaskViewSpec(
        path=(
            m.SubgridStepTemplate(cell=(), key="mh"),
            m.SubgridStepTemplate(cell=(), key="grades"),
        ),
        target_axes=(m.KeepAxis(), m.KeepAxis(), m.KeepAxis()),
        axes=(S("subject"), S("usim"), S("judge")),
    )
    config2, _ = vs.canonicalize_open_task(
        mh,
        key="agg",
        axes=(),
        task_fn=wired(vs.bind_keeps(fns, {0: "aggregate"})),
        args={"grades": wired(vs.select_output(grades, "scores"))},
        outputs={"summary": m.EntityOutput()},
    )
    binding = config2.rule.inputs["grades"]
    assert isinstance(binding, m.SliceBinding)
    assert binding.received_axis_names == ("subject", "usim", "judge")
    assert binding.slice.target_axes == (m.KeepAxis(), m.KeepAxis(), m.KeepAxis())
    assert binding.slice.output == "scores"  # output selection threads through
    assert config2.outputs == {"summary": m.EntityOutput()}
    with pytest.raises(ViewAxisError):
        vs.select_output(vs.select_output(grades, "scores"), "scores")  # double select
    with pytest.raises(ViewAxisError):
        vs.select_output(mh, "scores")  # not a task view


# ---------------------------------------------------------------------------
# Nested MH case: creator has its own axes; correlation through the path
# ---------------------------------------------------------------------------


def _nested_creator_inputs() -> dict[str, vs.StoredViewSpec]:
    """Views held by one mh_variant cell (holder frame = (variant,)).

    The wiring rule bound workspace to variants_ws{variant=$0} and cfg to
    variant_cfg[variant=$0]; subjects kept its axis.
    """
    return {
        "workspace": vs.SubtreeViewSpec(
            path=(m.SubgridStepTemplate(cell=(), key="variants_ws"),),
            target_axes=(m.Param(0),),
            axes=(),
            mode="write",
        ),
        "cfg": vs.LeafViewSpec(
            kind="data",
            path=(m.SubgridStepTemplate(cell=(), key="variant_cfg"),),
            target_axes=(m.Param(0),),
            axes=(),
        ),
        "subjects": _handle_grid("subjects", (S("subject"),)),
        "fns": _handle_grid("fns", (S("fn"),)),
    }


def test_nested_canonicalization_is_cell_independent() -> None:
    """Wiring canonicalizes with no per-cell input at all (P2 + batch invariance).

    The canonicalizer takes nothing that varies by creating cell, so the
    regularity property (identical configs from every cell) holds trivially.
    """
    inp = _nested_creator_inputs()
    config, view = vs.canonicalize_open_task(
        inp["workspace"],
        key="rollouts",
        axes=(S("subject"),),
        task_fn=wired(vs.bind_keeps(inp["fns"], {0: "rollout"})),
        args={
            "subject": wired(inp["subjects"], coords=("subject",)),
            "cfg": wired(inp["cfg"]),
        },
        outputs={"result": m.EntityOutput()},
    )
    assert [a.name for a in view.axes] == ["subject"]  # workspace view is 0-axis
    # cfg's holder Param(0) re-expressed as the child's prefix position 0
    # (variants_ws's own axis).
    cfg_binding = config.rule.inputs["cfg"]
    subject_binding = config.rule.inputs["subject"]
    assert isinstance(cfg_binding, m.SliceBinding)
    assert isinstance(subject_binding, m.SliceBinding)
    assert cfg_binding.slice.target_axes == (m.Param(0),)
    # subject binds the child's own axis: prefix_len 1 + index 0 → Param(1).
    assert subject_binding.slice.target_axes == (m.Param(1),)


def test_uncorrelated_leaf_forwarding_rejected() -> None:
    """Batch-invariance rejection: wiring cannot vary with an unmapped holder axis.

    The creator (variant, seed) forwards a seed-bound leaf view into a child
    created in a workspace correlated only with variant — the wiring would
    have to differ per seed cell, collapsing distinct configs into one key.
    """
    inp = _nested_creator_inputs()
    seed_cfg = vs.LeafViewSpec(  # bound to holder axis 1 ("seed"), not in the path
        kind="data",
        path=(m.SubgridStepTemplate(cell=(), key="seed_cfg"),),
        target_axes=(m.Param(1),),
        axes=(),
    )
    with pytest.raises(ViewAxisError, match="does not map"):
        vs.canonicalize_open_task(
            inp["workspace"],
            key="rollouts",
            axes=(S("subject"),),
            task_fn=wired(vs.bind_keeps(inp["fns"], {0: "rollout"})),
            args={"seed_cfg": wired(seed_cfg)},
            outputs={"result": m.EntityOutput()},
        )


def test_local_coordinate_names_resolve_holder_side() -> None:
    """official/local coordinate rename: locals drive the (holder-side)
    strip-set; only officials reach the canonicalizer."""
    inp = _nested_creator_inputs()
    # The coordinate is officially "subject" but the holder's local alias is
    # "s_local" — so an arg keeping its own "subject" axis is NOT consumed.
    config, view = vs.canonicalize_open_task(
        inp["workspace"],
        key="t",
        axes=(S("subject"),),
        outputs={"result": m.EntityOutput()},
        task_fn=wired(vs.bind_keeps(inp["fns"], {0: "rollout"})),
        args={"subjects_all": wired(inp["subjects"], coords=("s_local",))},
    )
    subjects_all = config.rule.inputs["subjects_all"]
    assert isinstance(subjects_all, m.SliceBinding)
    assert subjects_all.received_axis_names == ("subject",)
    assert config.axes == (S("subject"),)  # official name stored
    # Spec-level view names are runner-internal (holders overlay locals).
    assert len(view.axes) == 1


def test_slicing_a_subtree_spec_sheds_create_authority() -> None:
    """Binding a subtree spec is legal and downgrades it to read whatever
    the grant's mode — a slice of a creation site is a browsing window
    (the view layer's write-mode ``at()`` rejection is intent-visibility
    on top of this, not the enforcement)."""
    ws = vs.open_child(root_view(), "variants_ws", (S("variant"),), "subtree", mode="write")
    sliced = vs.bind_keeps(ws, {0: "v1"})
    assert isinstance(sliced, vs.SubtreeViewSpec)
    assert sliced.mode == "read" and sliced.axes == ()
    assert sliced.target_axes == (m.LiteralCoord("v1"),)


def test_open_into_sliced_subtree_rejected() -> None:
    """Even a hand-constructed sliced subtree spec is rejected at open time."""
    sliced = vs.SubtreeViewSpec(
        path=(m.SubgridStepTemplate(cell=(), key="variants_ws"),),
        target_axes=(m.LiteralCoord("v1"),),
        axes=(),
        mode="write",
    )
    with pytest.raises(ViewAxisError, match="full-frame"):
        vs.open_child(sliced, "child", (), "data")
    with pytest.raises(ViewAxisError, match="full-frame"):
        vs.canonicalize_open_task(
            sliced,
            key="t",
            axes=(),
            task_fn=wired(vs.bind_keeps(_handle_grid("fns", (S("fn"),)), {0: "f"})),
            args={},
            outputs={"result": m.EntityOutput()},
        )


def test_child_mode_never_exceeds_grant_mode() -> None:
    """Write-mode child opens are rejected under a read-mode subtree spec
    even when it is *unsliced* (e.g. a read-mode rule input): child mode
    never exceeds the subtree grant's mode. The sliced browsing window is
    an instance — sliced specs are read-mode by construction (see
    ``test_slicing_a_subtree_spec_sheds_create_authority``)."""
    ro = vs.SubtreeViewSpec(
        path=(m.SubgridStepTemplate(cell=(), key="ws"),),
        target_axes=(m.KeepAxis(),),
        axes=(S("v"),),
        mode="read",
    )
    with pytest.raises(ViewAxisError, match="read-only"):
        vs.open_child(ro, "child", (), "data", mode="write")
    child = vs.open_child(ro, "child", (), "data", mode="read")
    assert child.mode == "read"
    # Nested subtree derivation stays possible read-mode (browsing descends).
    nested = vs.open_child(ro, "nest", (), "subtree", mode="read")
    assert isinstance(nested, vs.SubtreeViewSpec) and nested.mode == "read"


def test_uncorrelated_subtree_forwarding_rejected() -> None:
    """The batch-invariance rejection applies to subtree args like any other
    (and additionally protects the child's full-frame invariant)."""
    inp = _nested_creator_inputs()
    other_ws = vs.SubtreeViewSpec(  # bound to holder axis 1, not in the creation path
        path=(m.SubgridStepTemplate(cell=(), key="other_ws"),),
        target_axes=(m.Param(1),),
        axes=(),
        mode="write",
    )
    with pytest.raises(ViewAxisError, match="does not map"):
        vs.canonicalize_open_task(
            inp["workspace"],
            key="t",
            axes=(),
            task_fn=wired(vs.bind_keeps(inp["fns"], {0: "rollout"})),
            args={"other_ws": wired(other_ws)},
            outputs={"result": m.EntityOutput()},
        )


def test_task_fn_must_be_consumed() -> None:
    inp = _nested_creator_inputs()
    with pytest.raises(ViewAxisError, match="not consumed"):
        vs.canonicalize_open_task(
            inp["workspace"],
            key="t",
            axes=(),
            task_fn=wired(inp["fns"]),  # keeps axis "fn" → unconsumed
            args={},
            outputs={"result": m.EntityOutput()},
        )


def test_bad_wiring_rejected() -> None:
    """Instruction validation: arity, range, duplicate consumption."""
    inp = _nested_creator_inputs()
    fn = wired(vs.bind_keeps(inp["fns"], {0: "f"}))
    two_axes = _handle_grid("pairs", (S("a"), S("b")))

    def canon(arg: vs.WiredSpecArg):
        return vs.canonicalize_open_task(
            inp["workspace"],
            key="t",
            axes=(S("x"),),
            task_fn=fn,
            args={"arg": arg},
            outputs={"result": m.EntityOutput()},
        )

    with pytest.raises(ViewAxisError, match="wiring instructions"):
        canon(vs.WiredSpecArg(two_axes, (vs.KeepName("a"),)))  # arity
    with pytest.raises(ViewAxisError, match="no consumable"):
        canon(vs.WiredSpecArg(two_axes, (vs.Consume(0), vs.Consume(5))))
    with pytest.raises(ViewAxisError, match="consumed twice"):
        canon(vs.WiredSpecArg(two_axes, (vs.Consume(0), vs.Consume(0))))
    with pytest.raises(ViewAxisError, match="duplicate received"):
        canon(vs.WiredSpecArg(two_axes, (vs.KeepName("n"), vs.KeepName("n"))))
