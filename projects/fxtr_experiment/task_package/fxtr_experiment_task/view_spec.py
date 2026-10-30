"""Runtime-internal view specs + the canonicalizer (executable P2).

This module sits between the user-facing view calculus (`views.py`) and the
canonical stored model (`model.py`). Its docstring doubles as the reference
exposition for the axis/frame terminology used across all three — read it
top to bottom once. A running example ("the variants example") is threaded
throughout::

    variants_ws    subtree grid, own axes (variant)
    variant_cfg    data grid,    own axes (variant)
    subjects       handle grid,  own axes (subject)
    fns            handle grid,  own axes (fn)
    mh_variant     task grid,    own axes (variant)   # runs the code below

**Storage concepts (recap of `model.py`).** A grid's cells are addressed by
its **flattened axes**: the ordered positions formed by every enclosing
subtree's own axes followed by the grid's own axes. A grid created inside
``variants_ws`` — say ``variants_ws/rollouts`` with own axis ``subject`` —
therefore has flattened axes ``(variant, subject)``; the inherited part
``(variant,)`` is called its **prefix**. Task grids carry a **stored rule**
(`model.TaskRule`, attached to the subtree key), written *in the frame of
the grid that owns it*: ``Param(q)`` in a stored rule means "flattened axis
``q`` of the rule-owning grid". The stored rule we will construct below for
``rollouts`` (frame: 0=variant, 1=subject) is::

    task_fn:   fns[fn="rollout"]
    "subject": read subjects[subject=Param(1)]
    "cfg":     read variant_cfg[variant=Param(0)]

**Holders and frames.** A **holder** is a context that holds views and can
create grids: either the top-level user, or *the code of one task grid*
(e.g. ``mh_variant``). The **holder frame** is the holder's flattened axes —
``(variant,)`` for ``mh_variant``, ``()`` for the user. The frame belongs to
the *grid*, not to any one cell: every cell of ``mh_variant`` runs the same
code against the same frame, and the specific executing cell never appears
in specs or canonicalization (it matters only when cell contents are
actually read).

**Views and specs.** A view (`views.GridView`) is the named-axis capability
user code holds; its symbolic backing is a **spec** — one of the disjoint
:data:`ViewSpec` family of dataclasses (:class:`LeafViewSpec` /
:class:`TaskViewSpec` / :class:`SubtreeViewSpec`, plus the path-less
:class:`CoordViewSpec`), discriminated by their ``kind`` field. Every
stored-grid spec is an absolute address of a *stored* grid, with one
template per flattened position of the target, written *in the holder's
frame* (a coord spec has exactly one template and no address — its
"grid" is the 1D identity table, and the same three template forms apply
to its single position). The three template forms mean:

- ``LiteralCoord(v)`` — this target position is fixed to ``v``.
- ``Param(i)`` — this target position equals **holder axis ``i``**: the view
  is *vmapped* over that holder axis ("the cfg cell for *my* variant").
- ``KeepAxis`` — this target position is open; it is one of the view's own
  named axes (the spec's ``axes`` names the keeps, in order), addressable by
  the holder and forwardable onward.

So the same template datatypes appear in two frames, and keeping them apart
is the key to this module: **in a stored rule, ``Param`` means an axis of
the rule-owning grid (a storage concept); in a spec, ``Param`` means an axis
of the holder (a runtime concept). Canonicalization is exactly the
translation from holder frame to created-grid frame.** In the variants
example, ``mh_variant``'s wiring granted it these views (specs in *its*
frame, where 0 = variant)::

    workspace = variants_ws{variant: Param(0)}   axes ()         (subtree)
    cfg       = variant_cfg[variant: Param(0)]   axes ()         (data)
    subjects  = subjects[subject: Keep]          axes (subject,) (handle)
    fns       = fns[fn: Keep]                    axes (fn,)      (handle)

**Canonicalizing an open_task.** Now ``mh_variant``'s code runs::

    workspace.open_task("rollouts", axes=(subject,),
                        task_fn=fns.at(fn="rollout"),
                        args={"subject": subjects, "cfg": cfg})

and :func:`canonicalize_open_task` must emit ``rollouts``'s stored rule —
in *rollouts'* frame — from specs in *mh_variant's* frame. Definitions:

- The **creation path** is the subtree view being opened into (here
  ``workspace``). The child's frame is the subtree's flattened axes (the
  prefix) followed by the new own ``axes``: here ``(variant, subject)``.
- The creation path **maps holder axis i to child prefix position q** when
  its template at ``q`` is ``Param(i)``. Here ``workspace``'s template at
  ``q=0`` is ``Param(0)``: holder axis 0 (mh_variant's variant) is mapped to
  child position 0 — *which workspace cell you create into varies along your
  variant, and the child's variant coordinate tracks that one-to-one*. An
  axis appearing in none of the creation path's templates is **unmapped**:
  every holder cell along it creates into the very same subtree cells.
- The **strip-set** is the set of names vmap-consumed by this open: the
  subtree view's axis names plus the (local) coordinate names — here just
  ``{"subject"}``. **The strip-set is resolved holder-side**: the view layer
  matches local names (:func:`wire_by_names`) and hands the canonicalizer
  one *positional* instruction per open axis (consume-child-coordinate-p /
  keep-with-received-name); the exposition below keeps
  name terms for readability, but no name resolution happens here, and
  spec-level axis names are runner-internal bookkeeping.

Translation then goes template by template through each arg spec:

- ``cfg``'s ``Param(0)`` → holder axis 0 is mapped → child ``Param(0)``. ✓
- ``subjects``'s ``Keep "subject"`` → name in the strip-set → consumed:
  child ``Param(1)`` (prefix length 1 + coordinate index 0). ✓
- ``fns``'s ``fn`` axis was bound by code (``at(fn="rollout")``) → already a
  ``LiteralCoord``, passes through. Literals in stored configs come *only*
  from such explicit code, which is the same for every holder cell.
- A ``Keep`` whose name is **not** in the strip-set stays kept, and its
  holder-local name becomes the official **received axis name** the child's
  task fn addresses it by (none here; the flat MH example's ``agg`` receives
  ``grades`` with all of ``subject, usim, judge`` kept).
- A ``Param`` over an **unmapped** holder axis is **rejected** (the
  batch-invariance principle, with the created grid as the out-parameter):
  unmapped holder cells all create into the same key, so wiring that varies
  along that axis would collapse a grid of distinct configs into one
  smaller-dimensional key — a guaranteed regularity conflict. Corollary:
  canonicalization takes no per-cell input and is cell-independent by
  construction (regularity by construction).

The call returns the stored config shown at the top plus the child view —
``rollouts`` in *mh_variant's* frame: ``{variant: Param(0)}[subject: Keep]``
with axes ``(subject,)`` — ready for further wiring (e.g. as ``grades``'s
``transcript`` arg).

**"Template at q", and the mapped relation by example.** A spec carries one
template per flattened storage position of its target, in order (path-step
cells first, then ``target_axes``); "the template at ``q``" is the ``q``-th
entry of that list. The mapped relation is *purely positional* — names play
no role in it; names enter only at ``KeepAxis`` positions, via the
strip-set. The variants example obscures this because holder axis 0 and
storage axis 0 happen to both be called "variant", so here is a second
example with deliberately mismatched names. Storage::

    sweeps      subtree grid, own axes (model, dataset)
    lrs         data grid,    own axes (model, dataset)
    rep_seeds   data grid,    own axes (r)
    tokenizers  handle grid,  own axes (tok)
    runner      task grid,    own axes (m, rep)     # the holder below

``runner``'s holder frame is ``0 = m, 1 = rep``. Its wiring granted::

    workspace = sweeps{model: Param(0), dataset: Keep}   axes ("ds",)
        # "the sweeps row for MY m, across all datasets, which I call ds"
    lr        = lrs[model: Param(0), dataset: Keep]      axes ("ds",)
        # a distinct view also naming an axis "ds" — fine, names are per-view
    seeds     = rep_seeds[r: Param(1)]                   axes ()
    toks      = tokenizers[tok: Keep]                    axes ("tok",)

``runner``'s code calls ``workspace.open_task("train", axes=(step: int,),
...)``. The child is ``sweeps/train`` with flattened frame
``(model, dataset; step)``: positions ``0=model, 1=dataset, 2=step``.
Reading the creation path (``workspace``'s templates) plus the new axes:

- template at ``q=0`` is ``Param(0)`` → holder axis 0 (``m``) is **mapped**
  to child position 0. Positional through and through: holder ``m`` and
  storage ``model`` never need to agree on names.
- template at ``q=1`` is ``Keep`` (named ``ds`` on the view) → not a
  mapping; instead the *name* ``ds`` joins the **strip-set**, consuming to
  child position 1.
- coordinate ``step`` → the name ``step`` joins the strip-set, consuming to
  child position 2 (prefix length 2 + coordinate index 0).
- holder axis 1 (``rep``) appears in no template of ``workspace`` →
  **unmapped**.

Translating some args:

- ``args={"lr": lr}``: ``Param(0)`` → mapped → child ``Param(0)``; ``Keep
  "ds"`` → in the strip-set → child ``Param(1)``. Fully consumed — the
  child's task fn receives ``lr`` as a scalar.
- ``args={"toks": toks}``: ``Keep "tok"`` → not in the strip-set → stays
  kept; the child receives a one-axis view officially named ``tok``.
- ``args={"seeds": seeds}``: ``Param(1)`` → holder axis ``rep`` is unmapped
  → **rejected**. All ``runner`` cells along ``rep`` create into the very
  same ``sweeps`` cells, so wiring that varies with ``rep`` would collapse
  distinct configs into one key. (``runner`` may still *read* ``seeds``
  itself and use the value in data it writes — it just cannot wire
  per-``rep`` children here. To get per-rep children, ``rep`` must appear in
  the creation path — e.g. a workspace subtree with a rep-mapped axis — or
  as one of the child's own coordinates.)

Specs are pure values; the pure spec operations (positional `bind_keeps`,
`select_output`, `open_child`) never touch storage. Names on specs are
runner-internal bookkeeping — the holder-facing named layer (with unique,
renamable local names) is the view runtime's Shadow — so no spec operation
resolves names, and nothing in this module touches
storage or is user-facing (`views.py` objects will wrap specs and delegate).

Glossary: **flattened axes / prefix** (storage: a grid's positions /
inherited positions) · **stored rule** (storage: templates in the owning
grid's frame) · **holder / holder frame** (runtime: the creating context /
its grid's flattened axes) · **view / spec** (runtime: named-axis capability
/ its symbolic backing, templates in the holder frame) · **creation path /
mapped / unmapped / strip-set** (canonicalization: see above).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal, Mapping, TypeVar

from frozendict import frozendict

from fxtr_experiment_task.errors import ViewAxisError
from fxtr_experiment_task.model import (
    DEFAULT_TASK_OUTPUTS,
    AxisKind,
    AxisSpec,
    AxisTemplate,
    CoordBinding,
    CoordValue,
    InputBinding,
    InternalsStepTemplate,
    KeepAxis,
    LiteralCoord,
    OutputSpec,
    Param,
    PathStepTemplate,
    SliceBinding,
    SliceMode,
    SliceTemplate,
    SubgridStepTemplate,
    TaskGridConfig,
    TaskRule,
)

GridKind = Literal["data", "dataframe", "handle", "task", "checkpoint", "subtree"]


# -- Wiring instructions -----------------------------------------------------
#
# The canonicalizer consumes *positional* wiring instructions, one per open
# (kept) axis of each forwarded view. Name resolution — the strip-set — is
# entirely the holder side's job (`view_runtime.py` computes instructions
# from its local vocabularies via :func:`wire_by_names`); the only names that
# reach the canonicalizer are the identity-bearing received names.


@dataclass(frozen=True)
class Consume:
    """This open axis is vmap-consumed by holder-visible child coordinate ``p``.

    The consumable index space is the concatenation *subtree view's axes ++
    new coordinates* (collision-free by the task-side naming rule). Whether
    ``p`` lands on a kept prefix position or a new own axis is a runner-side
    detail — both are just Params of the child's frame.
    """

    p: int


@dataclass(frozen=True)
class KeepName:
    """This open axis stays kept; ``name`` becomes its official received name."""

    name: str


AxisWire = Consume | KeepName


@dataclass(frozen=True)
class WiredSpecArg:
    """One forwarded view at the canonicalizer: its (sliced, effective-mode)
    spec plus one instruction per open axis, in order."""

    spec: ViewSpec
    wiring: tuple[AxisWire, ...]


def wire_by_names(
    subtree_names: tuple[str, ...],
    coord_names: tuple[str, ...],
    arg_names: tuple[str, ...],
) -> tuple[AxisWire, ...]:
    """The holder-side strip-set, as a pure function over local vocabularies.

    For each of a view's open-axis local names: consumed by the identically
    named subtree-view axis, else by the identically named (local) new
    coordinate, else kept (the local name is promoted to the received name).
    """
    consumable = (*subtree_names, *coord_names)
    wiring: list[AxisWire] = []
    for name in arg_names:
        if name in consumable:
            wiring.append(Consume(consumable.index(name)))
        else:
            wiring.append(KeepName(name))
    return tuple(wiring)


LeafGridKind = Literal["data", "dataframe", "handle", "checkpoint"]
"""The stored grid kinds addressed by :class:`LeafViewSpec` — every kind
whose spec carries no kind-specific state."""


@dataclass(frozen=True, kw_only=True)
class _StoredSpecBase:
    """Shared shape of the stored-grid spec classes (:data:`ViewSpec`).

    Symbolic state of one grid view, written in the holder's frame.
    (Terminology — holder, frame, mapped, strip-set — is defined in the
    module docstring's exposition.) The concrete classes form a
    discriminated union on their ``kind`` fields (per-class restricted
    ``Literal`` types, so both ``kind`` comparisons and ``isinstance``
    narrow); kind-specific state lives only on the class it belongs to —
    pattern-match before touching it.

    Attributes:
        path: Absolute template path to the target grid. ``Param(i)`` means
            holder axis ``i`` (the view is vmapped over it); ``KeepAxis``
            marks open, holder-addressable positions.
        target_axes: One template per *own* axis of the target grid.
        axes: The holder-local axis specs, one per ``KeepAxis`` across
            ``path`` cells + ``target_axes``, in flattened order. Names are
            unique within the spec.
        mode: Access mode this view carries.
    """

    path: tuple[PathStepTemplate, ...]
    target_axes: tuple[AxisTemplate, ...]
    axes: tuple[AxisSpec, ...]
    mode: SliceMode = "read"


@dataclass(frozen=True, kw_only=True)
class LeafViewSpec(_StoredSpecBase):
    """Spec of a plain value-grid view: data, dataframe, handle, or
    checkpoint — the kinds with no kind-specific spec state (the two that
    carry any get their own class: :class:`TaskViewSpec`,
    :class:`SubtreeViewSpec`)."""

    kind: LeafGridKind


@dataclass(frozen=True, kw_only=True)
class TaskViewSpec(_StoredSpecBase):
    """Spec of a task-grid view — the only spec with an output selection.

    Attributes:
        output: The named output this view addresses (``None`` = the whole
            cell, e.g. a task view before output-selection).
    """

    kind: Literal["task"] = "task"
    output: str | None = None


@dataclass(frozen=True, kw_only=True)
class SubtreeViewSpec(_StoredSpecBase):
    """Spec of a subtree view — the only spec that opens children (a
    creation site: full-frame, never sliceable).

    Attributes:
        internals: True for the pseudo-subtree view of a task cell's own
            internals: ``path``/``target_axes`` then address the *task
            grid* (cell templates = the task cell), and opening a child
            appends an :class:`~fxtr_experiment.model.InternalsStepTemplate`
            instead of a :class:`~fxtr_experiment.model.SubgridStepTemplate`.
    """

    kind: Literal["subtree"] = "subtree"
    internals: bool = False


@dataclass(frozen=True, kw_only=True)
class CoordViewSpec:
    """Spec of a coordinate view (`views.CoordView`) — pure metadata, no path.

    The backing "grid" is the 1D identity table whose value along its single
    axis *is* the coordinate; there is no storage target, so this spec
    deliberately does not share the stored-grid base — walking a coord
    spec's path is not a well-defined operation and cannot be expressed.

    Attributes:
        value_kind: The coordinate's value kind (identity-bearing: the task
            observes "I receive an int coord").
        template: The one template, in the holder's frame: ``Param(i)``
            (the view is vmapped over holder axis ``i`` — its value is that
            coordinate), ``KeepAxis`` (open: the identity view), or
            ``LiteralCoord`` (a bound constant).
        axes: The holder-local axis spec for the kept position — one entry
            iff ``template`` is ``KeepAxis``, else empty (mirrors the
            stored-grid keeps invariant).
        mode: Always ``"read"`` (coord views are trivially read-only).
    """

    kind: Literal["coord"] = "coord"
    value_kind: AxisKind
    template: AxisTemplate
    axes: tuple[AxisSpec, ...]
    mode: Literal["read"] = "read"


StoredViewSpec = LeafViewSpec | TaskViewSpec | SubtreeViewSpec
"""The stored-grid spec family: every spec that addresses a stored grid."""

ViewSpec = StoredViewSpec | CoordViewSpec
"""Any view spec: a stored-grid spec, or the path-less coordinate spec."""

ViewKind = Literal["data", "dataframe", "handle", "task", "checkpoint", "subtree", "coord"]
"""Any view/grant kind: the stored grid kinds plus ``"coord"`` (which is
never a stored grid kind — coord views have no storage target)."""

_StoredSpecT = TypeVar("_StoredSpecT", bound=_StoredSpecBase)


def make_stored_spec(
    kind: GridKind,
    *,
    path: tuple[PathStepTemplate, ...],
    target_axes: tuple[AxisTemplate, ...],
    axes: tuple[AxisSpec, ...],
    mode: SliceMode,
    output: str | None = None,
) -> StoredViewSpec:
    """Construct the spec class for a stored grid kind.

    The construction-side dual of the consumption-side pattern matches:
    shared by the child-derivation (:func:`open_child`) and
    rule-rehydration (:func:`spec_from_binding`) paths, whose kinds arrive
    as data (a caller argument / a stored rule's target kind).

    Args:
        kind: The target grid's kind.
        path: Absolute template path to the target grid.
        target_axes: One template per own axis of the target.
        axes: The holder-local axis specs for the kept positions.
        mode: Access mode the spec carries.
        output: The selected output — only legal for ``"task"``.

    Raises:
        ViewAxisError: ``output`` given for a kind that has none.
    """
    if kind == "task":
        return TaskViewSpec(path=path, target_axes=target_axes, axes=axes, mode=mode, output=output)
    if output is not None:
        raise ViewAxisError(f"a {kind!r} view has no outputs to select")
    if kind == "subtree":
        return SubtreeViewSpec(path=path, target_axes=target_axes, axes=axes, mode=mode)
    return LeafViewSpec(kind=kind, path=path, target_axes=target_axes, axes=axes, mode=mode)


def select_output(spec: ViewSpec, name: str) -> TaskViewSpec:
    """Select a named output of a task view (pure).

    Called runner-side when an incoming selection names an output
    (``ViewSel.output``). Exactly one wire path produces that: the task
    side holds an *unselected* task grant and derives a Data(Frame)View
    from it via ``TaskView.output(name)``, which stamps the name into the
    selections the derived view sends — so ``spec`` here is always a
    task spec with no output selected yet. (The other way an output gets
    selected — a rule binding's ``SliceTemplate.output`` — never arrives
    here: such a binding is granted as a data / dataframe shadow in the
    first place, see `spec_registry.SpecRegistry.mint`, and those views'
    selections carry no output name.)

    Args:
        spec: A task-grid view spec with no output selected yet.
        name: The declared output name.

    Returns:
        The same view addressing output ``name``.

    Raises:
        ViewAxisError: If ``spec`` is not a task view, or already selected
            (no legitimate wire path double-selects; see above).
    """
    if not isinstance(spec, TaskViewSpec):
        raise ViewAxisError(f"cannot select an output on a {spec.kind!r} view")
    if spec.output is not None:
        raise ViewAxisError(f"output already selected ({spec.output!r})")
    return replace(spec, output=name)


def _flat_templates(spec: _StoredSpecBase) -> list[AxisTemplate]:
    """All templates of ``spec`` in flattened order (path cells, then target)."""
    return [t for step in spec.path for t in step.cell] + list(spec.target_axes)


def _keep_positions(flat: list[AxisTemplate]) -> list[int]:
    """Indices of the KeepAxis entries in a flattened template list."""
    return [i for i, t in enumerate(flat) if isinstance(t, KeepAxis)]


def _rebuild(
    spec: _StoredSpecBase, flat: list[AxisTemplate]
) -> tuple[tuple[PathStepTemplate, ...], tuple[AxisTemplate, ...]]:
    """Re-chunk a flattened template list back into ``spec``'s path/target shape."""
    steps: list[PathStepTemplate] = []
    i = 0
    for step in spec.path:
        n = len(step.cell)
        steps.append(replace(step, cell=tuple(flat[i : i + n])))
        i += n
    return tuple(steps), tuple(flat[i:])


def check_spec(spec: ViewSpec) -> None:
    """Validate internal consistency (keep-count matches axes; names unique).

    Raises:
        ViewAxisError: On any inconsistency.
    """
    if isinstance(spec, CoordViewSpec):
        keeps_n = 1 if isinstance(spec.template, KeepAxis) else 0
        if keeps_n != len(spec.axes):
            raise ViewAxisError(
                f"coord spec has {keeps_n} kept positions but {len(spec.axes)} named axes"
            )
        if isinstance(spec.template, LiteralCoord):
            expected = int if spec.value_kind == "int" else str
            if type(spec.template.value) is not expected:
                raise ViewAxisError(
                    f"coord spec has kind {spec.value_kind!r} but literal"
                    f" {spec.template.value!r}"
                )
        return
    keeps = _keep_positions(_flat_templates(spec))
    if len(keeps) != len(spec.axes):
        raise ViewAxisError(f"spec has {len(keeps)} kept positions but {len(spec.axes)} named axes")
    # Names may legally repeat across subtree levels (uniqueness is per
    # grid's own axes; spec names are runner-internal bookkeeping) — the
    # name-based rewrites below guard their own ambiguity instead.


def check_no_diagonal(spec: SubtreeViewSpec) -> None:
    """Reject duplicated Params on a subtree view (any mode).

    A diagonal (one holder axis bound to two target positions) would make
    the mapped-axis relation non-invertible and forwarded-arg translation
    ambiguous off the diagonal; nothing legitimate produces one.

    Raises:
        ViewAxisError: If a template is a duplicated Param.
    """
    seen: set[int] = set()
    for t in _flat_templates(spec):
        if isinstance(t, Param):
            if t.position in seen:
                raise ViewAxisError(
                    f"subtree view binds holder axis {t.position} twice (a "
                    "diagonal); creation requires a full-frame subtree view"
                )
            seen.add(t.position)


def check_subtree_full_frame(spec: SubtreeViewSpec) -> None:
    """Enforce the full-frame invariant on a subtree (or internals) view.

    **Full-frame is a property of creation capability.** A subtree view
    that can create — and the *creation path* of ``open_task``, whatever
    its mode — must be total: every flattened axis of the target either a
    named ``KeepAxis`` or bound to a **distinct** holder axis (``Param``)
    — never a literal, never a duplicated Param. This makes the
    mapped-axis relation (see module docstring) a bijection between the
    Param-bound prefix positions and the holder axes that appear, so
    canonicalization never guesses: slicing into a subtree (a literal)
    would silently broadcast creations across the removed axis, and a
    diagonal (duplicate Param) would make forwarded-arg translation
    ambiguous off the diagonal.

    *Read-mode* subtree views are exempt from the literal half — they
    cannot create, so a sliced read view is an ordinary browsing window
    (e.g. the top level's ``attach_internals`` view of one task cell's
    internals, whose cell coordinates are root-frame literals). That gate
    lives at the call sites (:func:`open_child` checks full-frame only
    for write-mode views); this function is the strict form.

    Raises:
        ViewAxisError: If a template is a literal or a duplicated Param.
    """
    check_no_diagonal(spec)
    for t in _flat_templates(spec):
        if isinstance(t, LiteralCoord):
            raise ViewAxisError(
                "subtree view is sliced (has a literal-bound axis); creation "
                "requires a full-frame subtree view"
            )


def bind_keeps(spec: _StoredSpecT, bound: Mapping[int, CoordValue]) -> _StoredSpecT:
    """Pure constant binding by keep *index* (position in ``spec.axes`` order).

    The only spec-level bind: runner-side code addresses positionally (axis
    names on specs are bookkeeping and may legally repeat across levels).
    Name resolution belongs to the view runtime's Shadow.

    **Slicing a subtree spec sheds its create authority**: a slice of a
    creation site is definitionally a browsing window, so a bound subtree
    spec comes back read-mode whatever the grant's mode — a pure
    downgrade (upgrades still do not exist), which is what makes the
    full-frame invariant's broadcast concern evaporate for sliced views.
    (Holder-side, the view layer additionally requires an explicit
    ``read_only()`` before ``at()`` on a subtree view, so the shed
    authority is visible in the code that does it; the wire-level
    downgrade here is what makes crafted selections sound too.)
    """
    flat = _flat_templates(spec)
    keeps = _keep_positions(flat)
    for j, value in bound.items():
        if not 0 <= j < len(keeps):
            raise ViewAxisError(f"no open axis {j}")
        expected = spec.axes[j].kind
        if type(value) is not (int if expected == "int" else str):
            raise ViewAxisError(f"axis {j} has kind {expected!r} but got {value!r}")
        flat[keeps[j]] = LiteralCoord(value)
    path, target = _rebuild(spec, flat)
    remaining = tuple(a for j, a in enumerate(spec.axes) if j not in bound)
    result = replace(spec, path=path, target_axes=target, axes=remaining)
    if bound and isinstance(result, SubtreeViewSpec):
        result = replace(result, mode="read")
    return result


def bind_coord(spec: CoordViewSpec, bound: Mapping[int, CoordValue]) -> CoordViewSpec:
    """The coord counterpart of :func:`bind_keeps` (``at()`` on the kept axis).

    Raises:
        ViewAxisError: No such open axis, or a kind mismatch.
    """
    if not bound:
        return spec
    open_indices = {0} if isinstance(spec.template, KeepAxis) else set()
    for j, value in bound.items():
        if j not in open_indices:
            raise ViewAxisError(f"no open axis {j}")
        if type(value) is not (int if spec.value_kind == "int" else str):
            raise ViewAxisError(f"axis {j} has kind {spec.value_kind!r} but got {value!r}")
    return replace(spec, template=LiteralCoord(bound[0]), axes=())


def open_child(
    spec: ViewSpec,
    key: str,
    new_axes: tuple[AxisSpec, ...],
    child_kind: GridKind,
    *,
    mode: SliceMode = "read",
    local_axes: tuple[AxisSpec, ...] | None = None,
) -> StoredViewSpec:
    """Derive the child view spec for opening ``key`` under a subtree view.

    Args:
        spec: A subtree (or task-internals) view spec. Non-subtree specs
            are rejected at runtime (wire tokens are untrusted), not just
            by type.
        key: The child key.
        new_axes: The child's own axes (official stored names).
        child_kind: The child grid's kind.
        mode: Access mode of the returned view. Never exceeds the subtree
            view's own mode: a read-mode subtree view — sliced or not —
            opens children read-only (read access to a subtree confers no
            write authority inside it).
        local_axes: Optional holder-local names for the new axes (defaults to
            ``new_axes``; used by ``open_task``'s ``local_names``).

    Returns:
        The child view spec: path extended by one step whose cell templates
        are this view's ``target_axes``, target = all-keep over ``new_axes``,
        axes = this view's axes + the (local) new axes.

    Raises:
        ViewAxisError: If ``spec`` cannot open children, names collide, or
            ``mode`` is write on a read-mode subtree view.
    """
    if not isinstance(spec, SubtreeViewSpec):
        raise ViewAxisError(f"cannot open children under a {spec.kind!r} view")
    if spec.mode == "write":
        check_subtree_full_frame(spec)
    else:
        # Read-mode subtree views may be sliced: they cannot create (both
        # open paths refuse creates without write access), so the
        # full-frame invariant's broadcast concern does not apply — the
        # child view is a browsing window through the literal prefix.
        check_no_diagonal(spec)
        if mode == "write":
            raise ViewAxisError(
                "a read-mode subtree view opens children read-only (child "
                "mode never exceeds the subtree grant's mode)"
            )
    local = local_axes if local_axes is not None else new_axes
    # Official names must be unique within one grid's OWN axes (the model
    # invariant); overlap with prefix levels is legal — names are per-grid,
    # and spec-level names are runner-internal bookkeeping anyway (the
    # user-facing local-name collision rule is enforced by the view layer).
    names = [a.name for a in local]
    if len(set(names)) != len(names):
        raise ViewAxisError(f"duplicate own-axis names: {names}")
    step_cls = InternalsStepTemplate if spec.internals else SubgridStepTemplate
    return make_stored_spec(
        child_kind,
        path=spec.path + (step_cls(cell=spec.target_axes, key=key),),
        target_axes=tuple(KeepAxis() for _ in new_axes),
        axes=spec.axes + local,
        mode=mode,
    )


def canonicalize_open_task(
    subtree: ViewSpec,
    *,
    key: str,
    axes: tuple[AxisSpec, ...],
    task_fn: WiredSpecArg,
    args: Mapping[str, WiredSpecArg],
    outputs: Mapping[str, OutputSpec] | None = None,
) -> tuple[TaskGridConfig, ViewSpec]:
    """Canonicalize one ``open_task`` call into a stored config (executable P2).

    Takes no per-cell input, and — post the chunk-3 rework — **no name
    resolution**: each forwarded view arrives with positional wiring
    instructions (one per open axis; computed holder-side from local
    vocabularies, e.g. via :func:`wire_by_names`). The only names consumed
    here are the identity-bearing ones: the new coordinates' official names
    and each ``KeepName``'s received name.

    Args:
        subtree: The subtree view being opened into (holder-frame spec).
        key: The child key.
        axes: The new task grid's own axes (official stored names).
        task_fn: Handle view for the task fn; every open axis must be
            consumed (no ``KeepName`` instructions) so exactly one fn cell
            resolves per task cell.
        args: Named wired views the task gets access to. Every holder axis a
            view is bound to must be *mapped* by the creation path (see the
            module docstring).
        outputs: The task's declared named outputs; part of the canonical
            config. Defaults to
            :data:`~fxtr_experiment.model.DEFAULT_TASK_OUTPUTS`.

    Returns:
        ``(config, task_view)``: the canonical key-level config, and the
        returned task view spec (axes = subtree view's axes + the new axes;
        spec-level names are runner-internal — holders overlay locals).

    Raises:
        ViewAxisError: On bad wiring (wrong arity, out-of-range or duplicated
            consumption targets, duplicate received names), an unconsumed
            task_fn axis, or a view bound to an unmapped holder axis.
    """
    if task_fn.spec.kind != "handle":
        raise ViewAxisError(f"task_fn must be a handle view, got {task_fn.spec.kind!r}")
    for name, arg in args.items():
        if arg.spec.kind == "checkpoint":
            raise ViewAxisError(f"arg {name!r}: checkpoint views cannot be granted")

    if not isinstance(subtree, SubtreeViewSpec):
        raise ViewAxisError(f"cannot open children under a {subtree.kind!r} view")
    check_subtree_full_frame(subtree)

    official = [c.name for c in axes]
    if len(set(official)) != len(official):
        raise ViewAxisError(f"duplicate coordinate names: {official}")

    # The child's prefix frame is the subtree's flattened axes; the subtree
    # view's templates tell us, per prefix position, how the holder addresses
    # it — that is the creation-path correspondence.
    prefix_templates = _flat_templates(subtree)
    prefix_len = len(prefix_templates)
    # The mapped-axis relation (module docstring): holder axis i is mapped to
    # child prefix position q iff the subtree view's template at q is
    # Param(i). check_subtree_full_frame guarantees Params are distinct (no
    # diagonals) and there are no literals, so this is a bijection on the
    # Param-bound positions — translation never guesses.
    holder_axis_to_child_pos: dict[int, int] = {
        t.position: q for q, t in enumerate(prefix_templates) if isinstance(t, Param)
    }
    # The subtree view's k-th open axis is the k-th *kept* prefix position.
    kept_prefix = _keep_positions(prefix_templates)

    def mapped_param(position: int, arg_name: str) -> Param:
        """Re-express one holder ``Param`` in the child frame, or reject."""
        if position not in holder_axis_to_child_pos:
            # Batch-invariance rejection: this wiring would vary per
            # creating cell, collapsing distinct configs into one key.
            raise ViewAxisError(
                f"{arg_name!r} is bound to holder axis {position}, "
                "which the creation path does not map to any "
                "coordinate of the created grid; wiring cannot vary "
                "with an unmapped holder axis"
            )
        return Param(holder_axis_to_child_pos[position])

    def consumed_param(p: int, arg_name: str, consumed: set[int]) -> Param:
        """Resolve one ``Consume(p)`` instruction to a child-frame Param.

        ``p`` indexes the holder-visible child coordinates (kept prefix
        positions ++ new own axes); duplicate consumption within one arg
        is rejected.
        """
        if not 0 <= p < len(kept_prefix) + len(axes):
            raise ViewAxisError(f"{arg_name!r}: no consumable child coordinate {p}")
        if p in consumed:
            raise ViewAxisError(f"{arg_name!r}: child coordinate {p} consumed twice")
        consumed.add(p)
        if p < len(kept_prefix):
            return Param(kept_prefix[p])
        return Param(prefix_len + (p - len(kept_prefix)))

    def translate(
        arg: WiredSpecArg, spec: _StoredSpecBase, arg_name: str
    ) -> tuple[SliceTemplate, tuple[str, ...]]:
        """Translate one wired stored-grid arg into the child frame.

        Literals pass through; holder Params re-express via the mapped
        relation (unmapped → batch-invariance rejection); each open axis
        consumes its wiring instruction. Returns the stored slice plus the
        received names, validating arity / ranges / duplicate consumption /
        duplicate received names along the way.
        """
        flat = _flat_templates(spec)
        n_keeps = sum(isinstance(t, KeepAxis) for t in flat)
        if len(arg.wiring) != n_keeps:
            raise ViewAxisError(
                f"{arg_name!r}: {len(arg.wiring)} wiring instructions for " f"{n_keeps} open axes"
            )
        out: list[AxisTemplate] = []
        received: list[str] = []
        consumed: set[int] = set()
        keep_j = 0
        for t in flat:
            if isinstance(t, LiteralCoord):
                out.append(t)
            elif isinstance(t, Param):
                out.append(mapped_param(t.position, arg_name))
            else:  # KeepAxis
                instr = arg.wiring[keep_j]
                keep_j += 1
                if isinstance(instr, Consume):
                    out.append(consumed_param(instr.p, arg_name, consumed))
                else:
                    out.append(KeepAxis())
                    received.append(instr.name)
        if len(set(received)) != len(received):
            raise ViewAxisError(f"{arg_name!r}: duplicate received names {received}")
        path, target = _rebuild(spec, out)
        output = spec.output if isinstance(spec, TaskViewSpec) else None
        return (
            SliceTemplate(path=path, target_axes=target, output=output),
            tuple(received),
        )

    def translate_coord(arg: WiredSpecArg, spec: CoordViewSpec, arg_name: str) -> CoordBinding:
        """Translate one wired coord arg — a single-template spec through
        exactly the same per-template rules as a stored-grid arg."""
        t = spec.template
        expected_wiring = 1 if isinstance(t, KeepAxis) else 0
        if len(arg.wiring) != expected_wiring:
            raise ViewAxisError(
                f"{arg_name!r}: {len(arg.wiring)} wiring instructions for "
                f"{expected_wiring} open axes"
            )
        if isinstance(t, LiteralCoord):
            return CoordBinding(kind=spec.value_kind, template=t, received_axis_name=None)
        if isinstance(t, Param):
            return CoordBinding(
                kind=spec.value_kind,
                template=mapped_param(t.position, arg_name),
                received_axis_name=None,
            )
        instr = arg.wiring[0]
        if isinstance(instr, Consume):
            return CoordBinding(
                kind=spec.value_kind,
                template=consumed_param(instr.p, arg_name, set()),
                received_axis_name=None,
            )
        return CoordBinding(
            kind=spec.value_kind, template=KeepAxis(), received_axis_name=instr.name
        )

    def translate_arg(name: str, arg: WiredSpecArg) -> "InputBinding":
        if isinstance(arg.spec, CoordViewSpec):
            return translate_coord(arg, arg.spec, name)
        s, received = translate(arg, arg.spec, name)
        return SliceBinding(slice=s, mode=arg.spec.mode, received_axis_names=received)

    fn_spec = task_fn.spec
    assert not isinstance(fn_spec, CoordViewSpec)  # the "handle" kind check above
    fn_slice, fn_received = translate(task_fn, fn_spec, "task_fn")
    if fn_received:
        raise ViewAxisError(f"task_fn axes {list(fn_received)} not consumed by the wiring")
    inputs = frozendict({name: translate_arg(name, arg) for name, arg in args.items()})
    config = TaskGridConfig(
        axes=axes,
        rule=TaskRule(task_fn=fn_slice, inputs=inputs),
        outputs=frozendict(outputs) if outputs is not None else DEFAULT_TASK_OUTPUTS,
    )
    task_view = open_child(subtree, key, axes, "task")
    return config, task_view


# -- Stored rules → holder-frame specs (the executor's run-setup direction) --
#
# These functions are the reverse *data-flow* direction of the
# canonicalizer, and the relationship decomposes into two orthogonal
# translations:
#
# - **Type translation** (view specs ↔ SliceTemplate/SliceBinding): the
#   canonicalizer converts the creator's specs into the stored-rule types
#   inside ``TaskGridConfig.rule``; these functions take those rule parts
#   individually and turn them back into ViewSpec instances for the
#   *callee* (the executing cell's task fn). This part is mechanical and
#   reversible — the templates pass through unchanged; only the stored
#   observable vocabulary (received names → spec axis names) and the
#   never-stored runner bookkeeping (axis kinds, grid kind — supplied by
#   the caller from live configs; the storage walk lives in
#   `spec_registry.mint_rule_grants`) get attached.
#
# - **Frame translation** (creator frame → created grid's frame): one-way,
#   and it happens *only* in the canonicalizer — that is the
#   "canonicalization" part (the creation-path correspondence, P2). No
#   inverse exists here because none is needed: a rule is stored in the
#   frame of the grid that owns it (P1), and the execution-time holder is
#   a cell of that same grid, so the rule's frame IS the callee's holder
#   frame. The two readings of ``Param`` (stored rule: "axis i of the
#   rule-owning grid"; spec: "holder axis i") coincide exactly at
#   execution, which is what makes this direction pure re-tagging.


def spec_from_binding(
    binding: SliceBinding, *, target_kind: GridKind, flat_kinds: "tuple[AxisKind, ...]"
) -> StoredViewSpec:
    """One rule input binding → the holder-frame spec its grant serves.

    Args:
        binding: The stored binding (slice + mode + received names).
        target_kind: The target grid's kind (from its live config).
        flat_kinds: The coordinate kind of every flattened axis of the
            target, in flattened order (own-axis kinds of each grid along
            the slice's path, then the target's own) — the walk is the
            caller's job.

    Returns:
        The spec to mint this input's grant from: the binding's templates
        unchanged, kept positions named by the received names and typed by
        ``flat_kinds``.

    Raises:
        ViewAxisError: ``flat_kinds`` doesn't cover the slice's templates,
            or the received names don't match the kept positions 1:1.
    """
    slice_ = binding.slice
    templates = [t for step in slice_.path for t in step.cell] + list(slice_.target_axes)
    if len(flat_kinds) != len(templates):
        raise ViewAxisError(
            f"slice has {len(templates)} flattened positions but {len(flat_kinds)} kinds given"
        )
    kept_kinds: list[AxisKind] = [
        k for t, k in zip(templates, flat_kinds) if isinstance(t, KeepAxis)
    ]
    if len(binding.received_axis_names) != len(kept_kinds):
        raise ViewAxisError(
            f"binding names {list(binding.received_axis_names)} != {len(kept_kinds)} kept axes"
        )
    axes = tuple(AxisSpec(name=n, kind=k) for n, k in zip(binding.received_axis_names, kept_kinds))
    return make_stored_spec(
        target_kind,
        path=slice_.path,
        target_axes=slice_.target_axes,
        axes=axes,
        mode=binding.mode,
        output=slice_.output,
    )


def spec_from_coord_binding(binding: CoordBinding) -> CoordViewSpec:
    """One rule coord binding → the holder-frame spec its grant serves.

    Pure re-tagging like :func:`spec_from_binding` (P1: the rule's frame is
    the callee's holder frame, so the template passes through unchanged);
    the kept axis takes the received name.

    Raises:
        ViewAxisError: The received name is inconsistent with the template
            (present iff kept — the `model.CoordBinding` invariant).
    """
    kept = isinstance(binding.template, KeepAxis)
    if kept != (binding.received_axis_name is not None):
        raise ViewAxisError(
            f"coord binding has received name {binding.received_axis_name!r}"
            f" but its template is {'kept' if kept else 'bound'}"
        )
    axes = (
        (AxisSpec(name=binding.received_axis_name, kind=binding.kind),)
        if binding.received_axis_name is not None
        else ()
    )
    return CoordViewSpec(value_kind=binding.kind, template=binding.template, axes=axes)


def spec_from_task_fn(slice_: SliceTemplate) -> LeafViewSpec:
    """The rule's ``task_fn`` slice → the run prologue's handle-read spec.

    The executor reads the task-fn cell's key through this spec at the
    start of every run (the *run prologue* — see
    ``notes/execution_semantics.md``), so the observation record-checks
    like any other op.

    Raises:
        ViewAxisError: The slice is not single-cell (``TaskRule.task_fn``'s
            contract) or selects an output (handle grids have none).
    """
    if not slice_.is_single_cell():
        raise ViewAxisError("a rule's task_fn slice must resolve to exactly one cell")
    if slice_.output is not None:
        raise ViewAxisError("a task_fn slice targets a handle grid; no output to select")
    return LeafViewSpec(
        kind="handle",
        path=slice_.path,
        target_axes=slice_.target_axes,
        axes=(),
        mode="read",
    )


def internals_spec(
    keys: tuple[str, ...], enclosing_axis_counts: tuple[int, ...], own_axis_count: int
) -> SubtreeViewSpec:
    """The executing cell's internals view: every flattened axis vmapped.

    The internals pseudo-subtree addresses the task grid itself with the
    executing cell's full coordinates: ``Param(i)`` over every flattened
    position, in order (path steps cover the enclosing grids' axes, the
    target templates the task's own).

    Args:
        keys: The task grid's key path from the root.
        enclosing_axis_counts: Own-axis count of each *enclosing* grid
            along the path — one entry per path step past the first (the
            grid at ``keys[:d]`` for step depth ``d``).
        own_axis_count: The task grid's own-axis count.

    Raises:
        ViewAxisError: ``enclosing_axis_counts`` doesn't match the path
            (needs ``len(keys) - 1`` entries).
    """
    if len(enclosing_axis_counts) != len(keys) - 1:
        raise ViewAxisError(
            f"path of {len(keys)} keys needs {len(keys) - 1} enclosing axis counts,"
            f" got {len(enclosing_axis_counts)}"
        )
    steps: list[PathStepTemplate] = []
    position = 0
    for depth, key in enumerate(keys):
        count = 0 if depth == 0 else enclosing_axis_counts[depth - 1]
        cell = tuple(Param(position + i) for i in range(count))
        position += count
        steps.append(SubgridStepTemplate(cell=cell, key=key))
    target = tuple(Param(position + i) for i in range(own_axis_count))
    return SubtreeViewSpec(
        path=tuple(steps),
        target_axes=target,
        axes=(),
        mode="write",
        internals=True,
    )
