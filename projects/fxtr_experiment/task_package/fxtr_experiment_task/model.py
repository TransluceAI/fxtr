"""Canonical (stored/internal) data-model types for the grid/workflow system.

The workflow state is a tree of **grids**: tables whose cells are indexed by
coordinate axes and typed as data (entities), service handles, task results,
checkpoints, or subtrees of further grids. A grid is a *partial function* from
coordinates to values with an open (non-enumerable) domain: the set of
populated cells is not an observable fact, so adding a fact never invalidates
anything — only modifying or removing facts can. Expected index sets come from
*domain cells*, a usage convention: zero-axis data cells whose scalar value
lists the valid values of a semantic axis.

**Positional axis addressing; names as views.** All *addressing* in this
module is positional. A grid's full coordinate space is the ordered tuple of
its *flattened axes*: the concatenation, in path order, of every enclosing
subtree's own axes followed by the grid's own axes. Axis *names* are the
user-facing view of these hidden positional dimensions: user code (`views.py`)
only and always addresses by local names; storage only and always addresses by
positions, remembering name→position translations at the interface points.
Which names are stored — and identity-bearing — follows the **observability
principle**: config identity is exactly what task logic can observe. A grid's
own axis names (its task addresses them) and each binding's received axis
names (the vocabulary its task fn is handed) participate in config equality;
names never participate in addressing. Name uniqueness is only required
within one view/grid's own axes; collisions across the flattened prefix are a
rendering concern, disambiguated by path.

Configs store their grid's *own* axes only; a rule's flattened axis list is
derived by walking the grid's path and concatenating (configs are fixed at
creation, so a denormalized cached copy would also be safe — deferred).

This module is the canonical representation the runtime persists and matches
on. User code never constructs these types directly: it builds and forwards
grid *views* (`views.py`, the capability-rewrite calculus); the runtime
canonicalizes view wiring into these types at grid-creation time.

These are plain frozen dataclasses in memory. Grid configs and slice
templates additionally have canonical *entity* encodings (``encoding.py``)
whose ids serve as fingerprints; link/state records live in the workflow's
sqlite database (``storage.py``) and are never entities.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NewType

from frozendict import frozendict

from fxtr2.data.entity_core import FxtrID

from fxtr_experiment_task.errors import EncodingError

# ---------------------------------------------------------------------------
# Axes, coordinates, and paths
# ---------------------------------------------------------------------------

CoordValue = str | int
"""A single coordinate value along one axis."""

AxisKind = Literal["str", "int"]


@dataclass(frozen=True)
class AxisSpec:
    """One coordinate axis of a grid.

    Attributes:
        name: Official stored name — local to this grid (unique within its own
            axes only). Identity-bearing per the observability principle (the
            grid's task addresses by it; introspection/visualization shows it)
            but never used for addressing, which is positional.
        kind: Value type of coordinates along this axis.
    """

    name: str
    kind: AxisKind


CoordTuple = tuple[CoordValue, ...]
"""Position-aligned coordinates over some grid's *own* axes.

Path steps and cell references each carry the component for one grid; the full
flattened coordinate of a cell is the concatenation of its path's step cells
plus its final ``coords``.
"""


@dataclass(frozen=True)
class SubgridStep:
    """One step of a path through the subtree hierarchy.

    Reading order: from the current subtree grid, select the cell at ``cell``
    (positional over that subtree's own axes), then descend into the child grid
    named ``key`` within that cell.

    The root namespace is modeled as a subtree grid with zero axes, so the
    first step of every path has ``cell == ()``.
    """

    cell: CoordTuple
    key: str


@dataclass(frozen=True)
class TaskInternalsStep:
    """Descend into the grid named ``key`` in one task cell's private internals.

    A task grid doubles as a subtree grid: alongside its output cells it keeps
    a per-key internals config map (checkpoint grids and durable private
    state), exactly parallel to :class:`SubtreeGridConfig`'s key space. The
    tagged step variant — rather than a reserved string key — is what keeps
    the internals key-space from colliding with anything user-visible. Only
    the owning task cell (at its current generation) may open or write grids
    below this step; how far read grants may travel outward is an open
    question (see ``notes/design_overview.md``).

    The *bare* internals root is deliberately not path-addressable (this step
    always names a key), so a whole internals space can never be a slice
    target — to share space out of internals, create a named subtree inside it
    and grant that. There is likewise no "output" step: a task cell's named
    outputs are selected by :attr:`SliceTemplate.output` on slices that target
    the task grid, not by descending a path.
    """

    cell: CoordTuple
    key: str


GridPathStep = SubgridStep | TaskInternalsStep

GridPath = tuple[GridPathStep, ...]
"""Absolute path from the root namespace to one grid."""


@dataclass(frozen=True)
class CellRef:
    """Absolute reference to one cell: a grid plus coordinates within it.

    ``coords`` is positional over the grid's *own* axes (the path's step cells
    carry the prefix components). Cell references are the *only* identity used
    by linkage records — there are no internal ids for cells.
    """

    grid: GridPath
    coords: CoordTuple


# ---------------------------------------------------------------------------
# Dataframe schemas and task output declarations
# ---------------------------------------------------------------------------
#
# A dataframe cell holds zero or more rows matching a config-declared schema,
# with row-*multiset* semantics at the storage boundary (stored row order
# never carries meaning; order-meaningful data carries an explicit rank
# column). Columns are scalar-only — there is deliberately no entity column
# kind, so a frame entity is a *leaf* (no strong references) and frame
# payloads can move through the system as opaque bytes. Canonical identity:
# a frame is encoded as a sorted-rows entity (`frame_encoding.py`), so a
# dataframe cell is still an FxtrID under the hood — fingerprints, write
# conflicts (multiset equality is an id comparison), and idempotent replay
# all work unchanged. The whole design: notes/dataframes.md.

ColumnKind = Literal["str", "int", "float", "bool", "bytes"]
"""Value type of one dataframe column (scalar-only — see the section
comment above)."""

TableValue = CoordValue | float | bool | bytes | None
"""One table/frame cell value. Null is legal only in declared-nullable
columns (`ColumnSpec.nullable`)."""


@dataclass(frozen=True)
class ColumnSpec:
    """One column of a dataframe schema.

    Attributes:
        name: Column name — unique within a schema; validated against the
            column-name law at config creation
            (:func:`validate_frame_schema`).
        kind: Value type of the column's cells.
        nullable: Whether cells may hold null. Identity-bearing like the
            rest of the schema; the default (non-nullable) is omitted from
            the canonical encoding, so pre-flag schemas keep their
            fingerprints.
    """

    name: str
    kind: ColumnKind
    nullable: bool = False


FrameSchema = tuple[ColumnSpec, ...]
"""Ordered column list of a dataframe (order is display/canonical-encoding
order; row semantics are a multiset)."""

FrameRow = tuple[TableValue, ...]
"""One canonical row: values in schema column order (`frame_encoding.py`
owns the canonical sorted-rows entity these make up)."""


def validate_frame_schema(schema: FrameSchema) -> None:
    """Validate a frame schema against the column-name law.

    The dataframe counterpart of `encoding.validate_grid_key`, enforced at
    `encoding.store_config` — the funnel every open path passes through
    before a config can exist — so no invalid schema ever gets a
    fingerprint. The law is deliberately light (identifier-safety is left
    to users; column names never become filesystem paths): at least one
    column; names non-empty, unique within the schema, and free of
    control characters (code points below 0x20, and 0x7f). Zero-column
    schemas are rejected outright — a frame with no columns is just a
    row counter, a degenerate encoding with no use case (relaxing later
    is compatible, un-relaxing is not).

    Lives here with the schema types, not with the other charset laws in
    `encoding.py`: its three consumers straddle the task/runner boundary
    (`encoding.store_config`, `frame_encoding.build_frame_entity`, and the
    task-side frame runtime's write boundary in `tables.py`), and this is
    the module upstream of all three.

    Raises:
        EncodingError: On an invalid schema.
    """
    if not schema:
        raise EncodingError("invalid frame schema: at least one column required")
    seen: set[str] = set()
    for col in schema:
        if not col.name:
            raise EncodingError("invalid frame schema: empty column name")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in col.name):
            raise EncodingError(f"invalid column name {col.name!r}: control characters")
        if col.name in seen:
            raise EncodingError(f"invalid frame schema: duplicate column name {col.name!r}")
        seen.add(col.name)


@dataclass(frozen=True)
class FrameOutput:
    """Task output slot: a dataframe with a declared schema."""

    schema: FrameSchema


@dataclass(frozen=True)
class EntityOutput:
    """Task output slot: a single entity."""


OutputSpec = FrameOutput | EntityOutput
"""Declared type of one named task output.

Canonical encoding is explicitly tagged (see `encoding.py`); variant order
carries no codec meaning.
"""

DEFAULT_TASK_OUTPUTS: frozendict[str, OutputSpec] = frozendict({"result": EntityOutput()})
"""The default output declaration for the common single-entity task: one
``EntityOutput`` named ``"result"`` (read via ``task_view.output("result")``)."""


# ---------------------------------------------------------------------------
# Canonical rules: stored slice templates
# ---------------------------------------------------------------------------
#
# A task grid is defined by a static rule mapping its cells' coordinates to
# (a) the task-fn handle cell to run and (b) a set of named, pre-sliced views
# of other grids. The rule is deliberately *not* load-bearing for staleness
# (runtime observations are the truth); it is load-bearing for ACCESS (which
# grids a task may touch at all), SHAPE (vmap-like axis binding), and grid
# identity (rules are fixed at creation).
#
# A stored slice is conceptually a function from the owning grid's flattened
# axes to a location: `Param(i)` references flattened position i. Slices with
# kept axes are partial applications — the residual (kept) positions are the
# coordinate space of the capability handed to the task.


@dataclass(frozen=True)
class LiteralCoord:
    """Template coordinate: a fixed literal value."""

    value: CoordValue


@dataclass(frozen=True)
class Param:
    """Template coordinate: position ``position`` of the owning grid's flattened axes.

    Flattened axes = enclosing subtrees' own axes in path order, then the
    grid's own axes. This is the vmap-like binding: the owning grid's axis
    "maps over" the corresponding axis of the target grid. Prefix-position
    bindings arise only through canonicalization's composition with the
    creator's rule — view wiring cannot express them directly.
    """

    position: int


@dataclass(frozen=True)
class KeepAxis:
    """Template coordinate: keep this flattened axis of the target unbound.

    The capability's view of the target is addressed by the kept positions in
    flattened order (partial application: kept positions are the residual
    parameters). Axes may be kept anywhere in the flattened space — in path
    steps (spanning cells of enclosing subtrees; well-defined because
    regularity gives every cell the same config for a key) as well as in
    ``target_axes``.
    """


AxisTemplate = LiteralCoord | Param | KeepAxis
"""Stored template for one flattened axis of a slice's target: literal, bound
to an owning-grid position, or kept.

Canonical encoding is explicitly tagged (see `encoding.py`); variant order
carries no codec meaning.
"""


@dataclass(frozen=True)
class SubgridStepTemplate:
    """Stored template form of :class:`SubgridStep`.

    Cell coordinates may be literal, Param-bound, or kept (a kept step axis
    makes the slice span the corresponding cells of that subtree).
    """

    cell: tuple[AxisTemplate, ...]
    key: str


@dataclass(frozen=True)
class InternalsStepTemplate:
    """Stored template form of :class:`TaskInternalsStep`."""

    cell: tuple[AxisTemplate, ...]
    key: str


PathStepTemplate = SubgridStepTemplate | InternalsStepTemplate


@dataclass(frozen=True)
class SliceTemplate:
    """A stored rule's description of one sliced view of a target.

    Conceptually a partial application over the target's *flattened* axes:
    the concatenation of the step cell templates and ``target_axes`` assigns
    every flattened position a literal, a Param (bound to an owning-grid
    position), or KeepAxis. The residual (kept) positions, in flattened
    order, are the coordinate space of the capability handed to the task.

    Attributes:
        path: Template path from the root; the keys select grids, the cell
            templates cover the enclosing subtrees' axis positions.
        target_axes: One entry per *own* axis of the target grid.
        output: When the target is a task grid, the named output this slice
            addresses (task outputs are selected here, not by path steps).
            ``None`` for non-task targets.
    """

    path: tuple[PathStepTemplate, ...]
    target_axes: tuple[AxisTemplate, ...]
    output: str | None = None

    def is_single_cell(self) -> bool:
        """True if no flattened axis is kept, i.e. the slice is exactly one cell."""
        step_templates = (t for step in self.path for t in step.cell)
        return not any(isinstance(t, KeepAxis) for t in (*step_templates, *self.target_axes))


SliceMode = Literal["read", "write"]
"""Access mode granted on a slice.

``"read"`` on a data grid: read cell values; on a handle grid: read the
cell's handle *key* (identity) from the outer level (steps connect by key).
``"read"`` on a task grid: demand + read results.
``"write"`` on a data grid: write cell values (conflict if a different value is
already present). ``"write"`` on a subtree: create grids there, implying
write/create access to everything transitively inside (write access *is*
create access).

Modes gate which effectful ops a view may perform; the pure ``read_only``
rewrite downgrades write→read (e.g. to grant reading onward for a grid you
created for writing) — upgrades do not exist. The read-xor-write exclusion
is *op-level, per generation, per grid*: a generation's op record may not
contain both reads and writes of the same grid (enforced at op time; an
integrity violation → ``errored``). Holding both a write view and a derived
read view is fine — holding a slice observes nothing; a slice may be held
purely to grant it onward into created grids' rules (capability passing);
only reads / demands / writes record observations.
"""


@dataclass(frozen=True)
class SliceBinding:
    """One named input/output slice granted to a task by its rule.

    Attributes:
        slice: The positional slice template.
        mode: Access mode granted.
        received_axis_names: The official names the task fn receives for this
            arg's kept axes — one per kept position, in flattened order.
            These are the creator's local names at creation time, promoted to
            official. Identity-bearing (the task fn observes them; config
            equality includes them) but non-addressing.
    """

    slice: SliceTemplate
    mode: SliceMode
    received_axis_names: tuple[str, ...]


@dataclass(frozen=True)
class CoordBinding:
    """One coordinate input granted to a task by its rule (a `views.CoordView`).

    Coordinates are structure, not data: the bound "grid" is the 1D identity
    table whose value along its single axis *is* the coordinate. There is no
    storage target — no path, no cells — so reading through the granted view
    is unrecorded-pure (no observation, no staleness edge); for a consumed
    axis the value resolves per-cell at grant-mint time and rides the grant.
    The binding is identity-bearing like any input: the task can observe
    "I receive an int coord named ``idx``", so kind, template, and received
    name all participate in config equality.

    Attributes:
        kind: The coordinate's value kind.
        template: The wiring over the owning grid's flattened frame:
            ``Param`` (vmap-consumed — the task receives its own coordinate
            as a zero-axis view), ``KeepAxis`` (the identity view over one
            open axis), or ``LiteralCoord`` (a constant; its value is
            identity-bearing like any stored literal).
        received_axis_name: The official name the task fn receives for the
            kept axis; ``None`` iff ``template`` is not ``KeepAxis``.
    """

    kind: AxisKind
    template: AxisTemplate
    received_axis_name: str | None


InputBinding = SliceBinding | CoordBinding
"""One named input granted by a rule: a stored-grid slice or a coordinate.

Canonical encoding is explicitly tagged (see `encoding.py`); variant order
carries no codec meaning.
"""


@dataclass(frozen=True)
class TaskRule:
    """Static definition of a task grid: what runs, against which slices.

    This is the *stored canonical* form: slice paths are absolute from the
    root, ``Param`` bindings are positional over the grid's flattened axes,
    and the whole rule is attached to the grid's subtree key (one rule per key
    across all cells of the enclosing subtree — the regularity invariant as
    storage format). Addressing is purely positional; the only names in a
    rule are the observable ones (input names and per-binding received axis
    names — the task fn's vocabulary), which participate in config equality
    per the observability principle. Task code never builds rules directly:
    it wires views via ``SubtreeView.open_task`` (see `views.py`); the runtime
    canonicalizes the wiring at creation time.

    Attributes:
        task_fn: Slice resolving to exactly one handle-grid cell per task cell
            (``is_single_cell()`` must hold): the task function to execute.
            Every executing cell reads its task_fn cell at execution time (a
            ``cell_value`` observation — the source of ``executed_fn``).
        inputs: Named bindings the task gets access to — stored-grid slices
            or coordinate inputs. Names are the keys the task uses to look up
            its capabilities at run time (names here are API vocabulary, not
            axis addressing, so they stay).

    The rule is fixed when the task grid is created; changing it means creating
    a different grid (see the rule-migration followup in
    ``notes/design_overview.md``).
    """

    task_fn: SliceTemplate
    inputs: frozendict[str, InputBinding]


def rule_reference_paths(rule: TaskRule) -> tuple[tuple[str, ...], ...]:
    """The key paths a stored rule addresses (its *terminal* slice paths).

    One entry per distinct referenced grid: the ``task_fn`` slice's grid
    plus every :class:`SliceBinding` input's grid (coordinate inputs have
    no storage target). Feeds the reverse rule-reference index at
    grid-create time (`storage.py`: ``grid_refs``). Enclosing subtree
    grids along the paths are deliberately not listed — grid-level
    grounding walks parents itself, and the rmdir-style delete cannot
    remove a parent while the terminal grid exists.

    Returns:
        Distinct key tuples (absolute from the root, like the stored
        slices), in first-appearance order with ``task_fn`` first.
    """
    seen: dict[tuple[str, ...], None] = {}
    slices = [rule.task_fn] + [b.slice for b in rule.inputs.values() if isinstance(b, SliceBinding)]
    for s in slices:
        seen[tuple(step.key for step in s.path)] = None
    return tuple(seen.keys())


# ---------------------------------------------------------------------------
# Grid configurations
# ---------------------------------------------------------------------------
#
# Configs attach to subtree *keys* (one config per key across all cells of the
# enclosing subtree). Each config stores its grid's OWN axes only; the
# flattened axis list is derived by walking the grid's path.


@dataclass(frozen=True)
class DataGridConfig:
    """Grid of directly-stored entities (each cell is a FxtrID).

    Written at the top level by users or by tasks holding a write slice.
    """

    axes: tuple[AxisSpec, ...]


@dataclass(frozen=True)
class DataFrameGridConfig:
    """Grid of directly-stored dataframes (each cell: rows matching ``schema``).

    The dataframe counterpart of :class:`DataGridConfig` (symmetry: task
    entity-output ↔ entity data grid; task dataframe-output ↔ dataframe data
    grid; tasks may write to either kind). Cells are row multisets encoded as
    canonical entities (see the dataframe section above). Zero-axis dataframe
    cells are the natural representation for *domain cells* (schema = the
    semantic axis), making domain enumeration native.
    """

    axes: tuple[AxisSpec, ...]
    schema: FrameSchema


@dataclass(frozen=True)
class HandleGridConfig:
    """Grid of service handles (each cell is the FxtrID of a handle entity).

    Reading a handle observes its *identity*, not its behavior — external
    handle binding is an explicit trust boundary.
    """

    axes: tuple[AxisSpec, ...]


@dataclass(frozen=True)
class TaskGridConfig:
    """Grid of task results, defined by a rule; not user-editable.

    Demand-driven: any coordinates may be demanded; missing inputs fault at
    read time. Each task cell also owns a private internals key-space
    (reached via :class:`TaskInternalsStep`).

    A task declares **named outputs** (``outputs``), each an entity or a
    dataframe with a declared schema; every complete execution fills all of
    them. Slices address a specific output via
    :attr:`SliceTemplate.output`. "Indexer"-shaped tasks are just tasks whose
    outputs are dataframes and which happen never to checkpoint or connect —
    there is no separate indexer concept.
    """

    axes: tuple[AxisSpec, ...]
    rule: TaskRule
    outputs: frozendict[str, OutputSpec]


@dataclass(frozen=True)
class CheckpointGridConfig:
    """Grid of durable step results, owned by task cells *structurally*.

    A checkpoint grid's key-path necessarily passes through the owning task
    grid's internals step (:class:`TaskInternalsStep`), so the owning cell
    of any checkpoint cell is simply the flattened-coordinate prefix up to
    that step — there is no ownership record anywhere. Entries are bound to
    a specific execution generation of the owning cell; a new generation
    atomically drops all old entries, and deleting/replacing a task grid
    cleans up its internals wholesale.
    """

    axes: tuple[AxisSpec, ...]


@dataclass(frozen=True)
class SubtreeGridConfig:
    """Grid whose cells are namespaces mapping string keys to child grids.

    Regularity invariant: across all cells of the subtree, every child key has
    the same grid kind and configuration — the per-key config map is shared
    subtree-level *state*, fixed by whoever first opens each key (a mismatched
    open is an error). The subtree's own axes prepend onto each child grid's
    flattened axes.
    """

    axes: tuple[AxisSpec, ...]


GridConfig = (
    DataGridConfig
    | DataFrameGridConfig
    | HandleGridConfig
    | TaskGridConfig
    | CheckpointGridConfig
    | SubtreeGridConfig
)


# ---------------------------------------------------------------------------
# Execution state and generations
# ---------------------------------------------------------------------------

GenerationId = NewType("GenerationId", str)
"""Identity of one logical execution of a task cell.

A new generation is minted when a stale (or explicitly rerun) task cell is
re-executed; crash-retries within a generation are *attempts* and share the
generation's checkpoints. All old-generation state (checkpoints, observations,
grid assertions) is deleted atomically before a new generation starts — a
generation's claims expire with it.
"""

ExecStatus = Literal["pending", "running", "complete", "errored", "interrupted"]
"""Execution state of a task cell's current generation.

``errored``: the task's own fault — its logic raised, an error escaped a
step, or it violated integrity (conflict, config mismatch, axis misuse,
nondeterminism). ``interrupted``: progress was attempted but the task needed
something that isn't ready (a ``CellFaultError`` dependency fault) or the run
was cut off (a ``running`` status found at runner startup is flipped to
``interrupted``). Both fault readers identically; they surface differently at
the top level (one root cause, a tree of interrupted dependents).
Re-demanding an interrupted, fresh cell resumes the same generation via
record replay.
"""


TerminalStatus = Literal["complete", "errored", "interrupted"]
"""The subset of `ExecStatus` a finished run resolves its generation to.

``pending`` and ``running`` are in-flight states, never a run's outcome:
every run ends complete (clean return, empty failure set), errored (any
error-class failure, or an escape with none recorded), or interrupted
(fault-class failures only, or the run was cut off)."""


Currency = Literal["fresh", "stale", "unsettled"]
"""Derived currency of a task cell's current generation (never stored; the
full model is the currency section of ``notes/execution_semantics.md``).

``fresh``: every observation matches its subject's current settled value,
with well-founded support. ``stale``: some observation's subject settled on
a different value — a definite, local fact that never propagates
transitively. ``unsettled``: neither yet — the support DAG contains
something stale, re-running, cleared, errored, or cyclic, and the cell
resolves to fresh or stale when it settles. A cell is *settled* iff fresh
or stale."""


@dataclass(frozen=True)
class TaskCellExecution:
    """Durable record of the current generation of one task cell.

    Currency (`Currency`) is *not* stored here — it is derived from
    observation records and current grid contents. Execution state and
    currency are orthogonal axes; a generation can be running and already
    stale (v0 lets it finish).

    Attributes:
        generation: This execution's generation id.
        status: Execution state machine position.
        results: Per-output result ids (one entry per declared output; frame
            outputs are canonical frame entities), once complete.
        error: Human-readable reason, if errored or interrupted (for
            interrupted: what wasn't ready, or what cut the run off).
            Transient/environment errors surface here and retry via
            re-demand (checkpoints replay).
            Completing *with* an invalid value is different: it is the task
            asserting the error is representative/visible (e.g. a model
            refusal) — validation of that lives in downstream checker tasks,
            and re-sampling is either an in-task step-retry loop or an
            explicit invalidation by the user.
        current_fn: The task-fn handle identity this execution is considered
            current against. Normally equals ``executed_fn``; handle migrations
            may *evolve* it forward without re-execution.
        executed_fn: The task-fn handle identity that actually ran. Never
            rewritten; kept for debugging migrations.
    """

    generation: GenerationId
    status: ExecStatus
    results: frozendict[str, FxtrID] | None
    error: str | None
    current_fn: FxtrID
    executed_fn: FxtrID


# ---------------------------------------------------------------------------
# Linkage records
# ---------------------------------------------------------------------------
#
# All linkage is tracked by path + positional coordinates (+ generation),
# never by any other internal id. These records are expected to live in the
# workflow sqlite db and to be replay-matched *as a set* (order-insensitive)
# by the safety re-run — outer-level task code may therefore use ordinary
# anyio concurrency.

ObservationKind = Literal["cell_value", "wrote_cell", "grid_config"]
"""What a task observed about a subject.

``cell_value``: read a cell; fingerprint is the value's entity id. Handle
cells read this way yield the handle *key* (identity, not behavior) — the
outer level reads it and steps connect by key, so there is no separate
connect-time observation.
``wrote_cell``: wrote a cell (asserts its presence with that value — removal of
the written cell makes the writer stale); fingerprint is the written entity id.
``grid_config``: opened/created a grid, asserting its configuration;
fingerprint identifies the config (representation TBD — likely the id of an
entity-encoded config).
"""


@dataclass(frozen=True)
class ObservationRecord:
    """Task cell A (at some generation) observed subject B with this fingerprint.

    A is *stale* iff some observation's subject currently holds a settled
    value different from the recorded fingerprint; *fresh* iff every
    observation matches and every observed task cell is itself fresh (with
    well-founded support); *unsettled* otherwise. See `Currency` and the
    currency section of ``notes/execution_semantics.md``.
    """

    observer: CellRef
    generation: GenerationId
    subject_grid: GridPath
    subject_coords: CoordTuple | None
    """Cell coordinates, or None when the subject is the grid itself
    (``grid_config`` observations)."""
    subject_output: str | None
    """For task-cell subjects: the named output observed (currency is
    per-output — an idempotent rerun that reproduces output A identically
    keeps A's observers fresh even if output B changed). None otherwise."""
    kind: ObservationKind
    fingerprint: FxtrID


@dataclass(frozen=True)
class TargetRecord:
    """A durable, declarative user marking: this slice is a wanted DAG root.

    Shaped like a ``grid_config`` observation *minus any observed values*:
    it pins the lookup key — the wanted grid's path and the fingerprint of
    its expected canonical config — plus the wanted cell, expressed in the
    root frame (literals only: the user's flattened frame is empty so no
    ``Param`` can appear, and ``add_target`` rejects open positions — one
    fully-bound cell per record, unique per slice identity). Targets are
    intent, not data: fully
    reversible, no side effects beyond deterministic derived-flag
    propagation, captured by version-log commits, and *preserved* when
    unsatisfiable — a target whose grid is missing, or whose key now holds
    a different canonical config, is *dangling* (surfaced as incoherent in
    the UI; never silently rebound or dropped). Targeting a task cell
    means wanting it computed (all outputs; ``slice.output`` is
    ``None``).
    """

    slice: SliceTemplate
    expected_config: FxtrID
    """Id of the entity-encoded canonical config of the wanted grid, taken
    at marking time."""


@dataclass(frozen=True)
class SandboxDefn:
    """A sandboxed service definition: the nix flake reference triple.

    The `notes/sandboxed_bindings.md` defn of a durable sandboxed binding —
    the same reference triple as `fxtr_sandboxing`'s ``Nix*ServiceDefn``
    (the attr's ``kind`` is read at spawn time, not here; no nix eval
    happens at bind). Shared by the ``bind_sandbox`` wire surface
    (`user_api.py`) and the state-db storage (`storage.py`). Bindings are
    not identity, so this type has no canonical entity encoding — it lives
    in db columns and exports as TOML.

    Attributes:
        git_commit: The pinned commit, resolved against the runner's
            configured repo at spawn time (chunk 3).
        flake_attr: The flake attribute selecting the service.
        flake_path: Optional sub-path within the flake; ``None`` = the
            flake root.
    """

    git_commit: str
    flake_attr: str
    flake_path: str | None = None


@dataclass(frozen=True)
class CheckpointEntry:
    """One durable step result, bound to its generation.

    Replay: if the entry exists and is bound to the currently-executing
    generation, the stored value is returned without re-running the step; a
    same-coordinate entry bound to any *other* generation is a corruption error
    (generation turnover must have deleted it).
    """

    coords: CoordTuple
    generation: GenerationId
    value: FxtrID


# -- the generation record (the at-rest form of "everything a generation owns") --

RequestKind = Literal["task_output", "data"]
"""A request edge's subject class: a task-output read (feeds ``wanted``)
or a data/handle-cell read (the no-demand channel)."""

FailureKind = Literal["error", "fault"]
"""A failure record's class: the task's own fault (→ ``errored``) or a
not-ready dependency (→ ``interrupted``)."""


@dataclass(frozen=True)
class ObservationFact:
    """One op-record observation: (relation, location, fingerprint).

    Attributes:
        kind: The relation to the subject (see :data:`ObservationKind`).
        grid_path: The subject grid's key-path text.
        coords: The subject cell's flattened-coords text (``''`` for grid
            subjects).
        output: The subject's output name (``''`` for non-output
            subjects).
        fingerprint: The observed value/config id.
    """

    kind: ObservationKind
    grid_path: str
    coords: str
    output: str
    fingerprint: FxtrID


@dataclass(frozen=True)
class AssertionFact:
    """One would-create claim: the generation opened ``grid_path`` through
    write authority, proposing the canonical config ``config_id``."""

    grid_path: str
    config_id: FxtrID


@dataclass(frozen=True)
class RequestFact:
    """One request edge: the generation committed to reading the subject
    (``coords`` is the flattened-coords text)."""

    kind: RequestKind
    grid_path: str
    coords: str


@dataclass(frozen=True)
class FailureFact:
    """One failure record (raise-side; order across a generation's
    failures is insertion order — meaningful, unlike the storage row ids).

    Attributes:
        kind: The errored/interrupted split (see :data:`FailureKind`).
        error_class: The raised exception's class name (display only).
        message: Human-readable failure message.
        ts: Raise-time wall-clock timestamp (ISO; provenance only).
    """

    kind: FailureKind
    error_class: str
    message: str
    ts: str


@dataclass(frozen=True)
class CheckpointFact:
    """One durable step entry owned by the generation.

    Attributes:
        grid_path: The checkpoint grid's key-path text.
        coords: The entry's flattened-coords text.
        step_type_id: The step service's fxtr type id (the recorded step
            identity).
        args_id: Entity id of the recorded invocation args payload.
        value_id: Entity id of the recorded result payload.
    """

    grid_path: str
    coords: str
    step_type_id: FxtrID
    args_id: FxtrID
    value_id: FxtrID


@dataclass(frozen=True)
class GenerationRecord:
    """Everything one non-running generation owns, in canonical form.

    The at-rest source of truth for a task cell's current generation
    (``notes/at_rest_format.md``): stored as an entity at flip time and
    referenced by the state db / the export layout. Deliberately
    autoincrement-agnostic — no exec_ids, no version_ids, no failure row
    ids; those are storage bookkeeping, re-created on import. The
    canonical *entity* form additionally sorts the set-valued fields
    (`encoding.py` owns that; ``failures`` keeps insertion order —
    it is meaningful display order).

    Attributes:
        generation: The public generation uuid (stable across retries of
            an incomplete generation; a *new* uuid means turnover).
        status: The at-rest status (terminal — running generations have
            no record).
        current_fn: Task-fn handle entity id this generation is current
            against.
        executed_fn: Task-fn handle entity id that actually ran.
        results: Per-output result ids (complete generations; empty
            otherwise).
        failures: The poisoning record, insertion-ordered.
        observations: The op record (one entry per subject location).
        assertions: The would-create claims.
        request_edges: The demand record (written at commitment).
        checkpoints: The generation's durable step entries.
    """

    generation: str
    status: TerminalStatus
    current_fn: FxtrID
    executed_fn: FxtrID
    results: "frozendict[str, FxtrID]"
    failures: tuple[FailureFact, ...]
    observations: tuple[ObservationFact, ...]
    assertions: tuple[AssertionFact, ...]
    request_edges: tuple[RequestFact, ...]
    checkpoints: tuple[CheckpointFact, ...]
