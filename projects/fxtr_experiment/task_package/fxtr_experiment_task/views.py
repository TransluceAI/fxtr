"""User-facing grid views: the capability-rewrite calculus.

A **view** is the capability object task (and top-level user) code holds on
grid data: a named-axis window onto stored grids. Axis names on a view are
*local to the holder* — they are the only vocabulary user code ever sees, and
they are backed by hidden positional storage axes that user code never sees.
The runtime tracks the name→position map inside each view; renaming or
slicing a view changes how you access data, never what is stored or its
canonical identity.

Views are transformed by a small closed set of **pure, local rewrites**
(``rename_axes``, ``at``, ``read_only``) and consumed by **effectful
operations** (reads, writes, demands, opens, step checkpoints). Only effectful operations touch
the runtime and appear in the replayed op set; rewrites are free and
unrecorded. The one *read* that is pure is :meth:`CoordView.value` —
coordinates are structure, not data, so no observation exists to record.
View specs are symbolic (paths + positional bindings + name maps,
never per-cell values), so identical code in every cell of a task grid
produces identical specs — the regularity-by-construction property. Views
never cross into step code: steps consume plain data (handles cross as keys
read via :meth:`HandleView.read_key`).

A view may additionally be implicitly "vmapped" over ambient axes — the
creating context's own bound coordinates — which never appear in its axis set
and are invisible to the holder (subtree-transparency).

Views are constructed over a *backing capability* — a task's run capability
or the top level's user capability. The calculus (axes, rewrites, the op
vocabulary) is identical in both worlds, which is the familiarity/code-reuse
goal; op *semantics* follow the backing: the docstrings here describe the
task context (observation recording, record-check, poisoning), while
user-backed views perform the same operations as unrecorded interactive
events. Privileged top-level ops (targets, overwrite, clear, delete,
presence queries) are deliberately *not* view methods — they live on
`user_api.Workflow`, taking views as arguments — so holding or forwarding a
view never carries privilege.

Name scoping: a view's axis names must be unique within that one view only.
Distinct views held by the same code may reuse names freely. When an
operation would combine namespaces (e.g. opening a child grid appends its
axes to the view's), collisions are resolved by renaming *your own view*
first; a collision is an error, never silent shadowing.

Error contract: every workflow error raised by an effectful view operation
(and even by a pure rewrite) participates in the poisoning contract — it is
persisted and marks the running task failed (``errored``, or ``interrupted``
for ``CellFaultError``) whether or not task code catches it. See
``errors.py``.

Terminology (holder, frame, prefix, mapped/unmapped, strip-set) and a worked
example of how view wiring canonicalizes into stored configs live in
`view_spec.py`'s module docstring — the reference exposition.

The classes here are the abstract contracts — their docstrings are the API
reference; the concrete implementations live in `view_runtime.py` (shadow
objects over granted wire tokens). The one concrete class here is the
local coordinate view behind :func:`int_coord` / :func:`str_coord`:
creation is local and the view has no backend at all, so there is nothing
to defer to the runtime layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping, Self

from fxtr2.data.entity_core import FxtrID
from fxtr2.services.service_cap_common import FxtrServiceData

from fxtr_experiment_task.errors import ViewAxisError
from fxtr_experiment_task.model import (
    AxisKind,
    AxisSpec,
    CoordValue,
    FrameSchema,
    OutputSpec,
)

if TYPE_CHECKING:
    import polars as pl

    from fxtr2.data.entity_ref_scope import ServiceRef
    from fxtr2.services.service_cap_common import UnknownService

    from fxtr_experiment_task.task_api import StepFnLike


# Relational data moves through the calculus as ``polars.DataFrame`` values:
# dataframe cells read as frames, gathers extend frames, and a materialized
# frame is plain observed data — querying it locally (polars, or duckdb via
# ``__arrow_c_stream__``) is ordinary pure computation, legal at a task's
# outer level. There is deliberately no wrapper type: polars *is* the table
# runtime, its interop surface (``to_arrow``, ``write_ipc_stream``, ...) is
# the one users get, and a future backend swap is a client-side change behind
# the unchanged arrow-bytes wire (``runner_api.FrameWireType``). Building and
# converting frames — the dtype mapping, ``make_frame``, IPC/rows converters,
# write-boundary validation — lives in `tables.py`.
#
# The *semantic* contract is a schema plus rows with **multiset semantics at
# the storage boundary**: stored row order never carries meaning
# (order-meaningful data carries an explicit rank column), and canonical
# identity is defined on the multiset. An in-memory frame nevertheless has a
# definite, deterministic row order — a gather result arrives in driving-row
# order, a read in canonical order — so iterating one at a task's outer level
# is replay-safe. A frame's declared-schema nullability is not carried by the
# frame (polars cannot express NOT NULL); it lives only in the grid's
# ``FrameSchema`` and is enforced at the write boundary. Whole design:
# ``notes/dataframes.md``.


class GridView:
    """Base class for all views. See the module docstring for the model."""

    @property
    def axes(self) -> tuple[AxisSpec, ...]:
        """The view's named axes, in display order.

        Returns:
            One :class:`~fxtr_experiment.model.AxisSpec` per remaining axis.
            Names are unique within this view and local to its holder.
        """
        raise NotImplementedError

    def rename_axes(self, **renames: str) -> Self:
        """Return a view with some axes renamed.

        Keyword = new name, value = old name: ``view.rename_axes(x="a",
        z1="b")`` renames axis ``a`` to ``x`` and ``b`` to ``z1``. Pure and
        local; stored identity is unaffected.

        Args:
            **renames: Map from new axis name to existing axis name.

        Returns:
            A new view of the same kind with the renamed axis set.

        Raises:
            ViewAxisError: If an old name is absent, or a new name collides
                with a remaining axis name.
        """
        raise NotImplementedError

    def at(self, **coords: CoordValue) -> Self:
        """Return a view with the given axes bound to constant coordinates.

        The bound axes are removed from the view. Pure and local.

        Args:
            **coords: Constant coordinate per axis name to bind.

        Returns:
            A new view of the same kind without the bound axes.

        Raises:
            ViewAxisError: If a named axis is absent from the view.
        """
        raise NotImplementedError

    def read_only(self) -> Self:
        """Return this view with its privileges locally dropped to read.

        Pure and local, like all rewrites; upgrades do not exist. The drop
        is holder-local (the view layer rejects writes/creates through it;
        the capability behind it keeps the chain's authority) — where it
        becomes *real* is wiring: forwarding a view into a child task's
        rule populates the binding's identity-bearing mode from the
        view's effective mode, so ``read_only()`` is how you grant a grid
        onward read-only (an undropped view wires as a write grant).
        Holding both the original and a dropped view is fine
        — holding is not observing (P5); what is forbidden is *performing*
        both reads and writes of one grid within one generation, an
        op-level integrity check made against the generation's record.

        Returns:
            A read-effective view of the same slice.
        """
        raise NotImplementedError


class DataView(GridView):
    """View of a data grid (cells are entities, stored as FxtrIDs).

    A data view carries the authority of the chain that granted it —
    a rule binding's mode, or the opening view's authority — possibly
    dropped locally by :meth:`~GridView.read_only`. Writing requires
    write authority; performing both reads and writes of one grid in one
    generation is the op-level read-xor-write violation.
    """

    async def read(self, **coords: CoordValue) -> FxtrID:
        """Read one cell, binding all remaining axes.

        Records a ``cell_value`` observation of the cell.

        Args:
            **coords: Coordinate per remaining axis name; must resolve the
                view to exactly one cell.

        Returns:
            The entity id stored at the cell.

        Raises:
            CellFaultError: If the cell is missing or invalidated.
            ViewAxisError: If ``coords`` does not resolve the view to one cell.
        """
        raise NotImplementedError

    async def write(self, value: FxtrID, **coords: CoordValue) -> None:
        """Write ``value`` at one cell, binding all remaining axes.

        Idempotent — rewriting the identical entity id is a no-op success
        (this is what makes outer-level replay work). Records a
        ``wrote_cell`` observation (the writer asserts the
        cell's presence — the generation's write claim on the value).

        Args:
            value: The entity id to store.
            **coords: Coordinate per remaining axis name; must resolve the
                view to exactly one cell.

        Raises:
            WriteConflictError: If a different entity is already present.
            ViewAxisError: If ``coords`` does not resolve the view to one cell.
        """
        raise NotImplementedError


class DataFrameView(GridView):
    """View of a dataframe grid (cells are row multisets with a config schema).

    Also the shape of a task's dataframe *output* view (via
    :meth:`TaskView.output`), which reads the same way but is not writable
    and forces execution on read.
    """

    @property
    def schema(self) -> FrameSchema:
        """The declared column schema of the target's cells."""
        raise NotImplementedError

    async def read_table(self, **coords: CoordValue) -> "pl.DataFrame":
        """Read one cell's rows, binding all remaining axes.

        Records a ``cell_value`` observation (fingerprint: the cell's
        canonical frame entity).

        Returns:
            The cell's rows as a frame with this view's declared schema
            (canonical dtypes; rows in canonical order). Declared-schema
            nullability is not carried by the frame.

        Raises:
            CellFaultError: If the cell is missing or errored.
            ViewAxisError: If ``coords`` does not resolve the view to one cell.
        """
        raise NotImplementedError

    async def write_table(self, table: "pl.DataFrame", **coords: CoordValue) -> None:
        """Write ``table``'s rows at one cell (schema must match).

        ``table`` is validated against this view's declared schema: columns
        matched by name (order irrelevant), dtypes exactly (no implicit
        casts), and no null in a non-nullable column. Idempotent on multiset
        equality (canonical frame entity id); a different multiset already
        present is a conflict.

        Args:
            table: The rows to write. Its own nullability/order carry no
                meaning; the grid's declared schema governs.

        Raises:
            WriteConflictError: If a different row multiset is already present.
            ViewAxisError: On schema mismatch or unresolved coords.
        """
        raise NotImplementedError

    async def gather(
        self, table: "pl.DataFrame", on: Mapping[str, str], *, column_prefix: str | None = None
    ) -> "pl.DataFrame":
        """Join each row of ``table`` with the rows of its addressed cell.

        The fault-on-missing join for frames: for each row, bind this
        view's axes per ``on`` and read the addressed cell; the result
        contains one output row per (input row × cell row), with the cell's
        schema columns appended, in driving-row order. A missing/errored
        cell faults; an *empty* cell (a populated cell holding a zero-row
        frame) contributes zero rows without fault — its driving rows
        simply do not appear in the output (inner-join arity; gather never
        null-pads). Records a ``cell_value`` observation per distinct
        addressed cell; the addressed cells resolve concurrently (missing
        task outputs are demanded in parallel).

        Only the key columns named in ``on`` are constrained; ``table``'s
        other (payload) columns ride through untouched and may be any polars
        dtype, since they never become identity.

        Args:
            table: The driving frame.
            on: Map from this view's axis names to ``table`` column names;
                must cover all of the view's axes. Each key column's kind
                must exactly match its axis kind (str/int); a null key
                value is an error.
            column_prefix: Optional prefix prepended to every appended column
                name — the appended names come from the target's config
                schema (not renamable on a view), so this is the rename point
                for avoiding collisions with ``table``'s columns (e.g.
                ``column_prefix="g_"`` when gathering the same schema twice).

        Returns:
            The joined frame: ``table``'s columns, then this schema's columns
            (prefixed if ``column_prefix`` is given).

        Raises:
            GatherFailedError: If any addressed cell fails (missing,
                errored, ...): failure semantics are per request — each
                failing cell is its own recorded fact, exactly as if the
                cells were parallel single reads — and the one raised
                signal names every failed cell (it is not a
                ``CellFaultError``, whatever the causes' classes).
            ViewAxisError: On axis/column mismatches, or if an appended
                (post-prefix) column name collides with ``table``'s columns.
        """
        raise NotImplementedError


class CoordView(GridView):
    """View of a coordinate — the path-less 1D identity "grid".

    Coordinates are structure, not data: this view's value along its single
    axis *is* the coordinate, so there is no storage target and nothing to
    observe — :meth:`value` is a pure **sync** local call (no observation,
    no record entry, no staleness edge; nothing can ever implicate it).

    The primary use is wiring one into a task's args with its axis
    vmap-consumed, so each task cell learns its own coordinate — the
    sanctioned way in, since consumed axes are otherwise deliberately
    unobservable. The common received form is therefore the zero-axis view,
    read as ``value()`` with no args. A *kept* axis is the identity
    function (``value(i=5) == 5``); an ``at()``-bound view is a constant.
    All of these are identity-bearing rule inputs (`model.CoordBinding`):
    the task can observe "I receive an int coord named ``idx``".

    Construct locally with :func:`int_coord` / :func:`str_coord` — usable
    identically task-side and at the top level (no wire call; there is
    nothing to open). Coord views received through a rule are token-backed,
    so forwarding one into a child ``open_task`` translates like any other
    view (batch-invariance rejection included); they are trivially
    read-only, and no effectful operation accepts them.
    """

    def value(self, **coords: CoordValue) -> CoordValue:
        """The coordinate this view denotes, binding any remaining axis.

        Pure, local, and synchronous — deliberately not ``async``, since
        this is never an operation.

        Args:
            **coords: Coordinate for the remaining open axis, if any; the
                view must resolve to exactly one position.

        Returns:
            The resolved coordinate: the executing cell's own coordinate
            for a vmap-consumed view, the bound constant for an
            ``at()``-bound one, or the given coordinate itself for a kept
            axis (the identity function).

        Raises:
            ViewAxisError: If an axis remains unbound, or ``coords`` names
                an absent axis.
        """
        raise NotImplementedError


class _LocalCoordView(CoordView):
    """A locally created coord view: no grant, no token, pure shadow.

    The concrete form :func:`int_coord` / :func:`str_coord` return (received
    coord views are grant-backed instead: `view_runtime.RuntimeCoordView`).
    State is one axis that is either open (``_axis``) or bound
    (``_bound``); ``open_task`` wiring reads these to emit the token-less
    wire variant.
    """

    def __init__(self, kind: "AxisKind", axis: str | None, bound: CoordValue | None) -> None:
        """Exactly one of ``axis`` (open) / ``bound`` (constant) is set."""
        assert (axis is None) != (bound is None)
        self.value_kind: AxisKind = kind
        self._axis = axis
        self._bound = bound

    @property
    def axes(self) -> tuple[AxisSpec, ...]:
        """Contract on GridView.axes."""
        if self._axis is None:
            return ()
        return (AxisSpec(name=self._axis, kind=self.value_kind),)

    def _check_kind(self, value: CoordValue) -> None:
        if type(value) is not (int if self.value_kind == "int" else str):
            raise ViewAxisError(
                f"axis {self._axis!r} has kind {self.value_kind!r} but got {value!r}"
            )

    def rename_axes(self, **renames: str) -> "_LocalCoordView":
        """Contract on GridView.rename_axes (pure)."""
        if not renames:
            return self
        if self._axis is None or set(renames.values()) != {self._axis}:
            missing = [old for old in renames.values() if old != self._axis]
            raise ViewAxisError(f"cannot rename absent axis {missing[0]!r}")
        if len(renames) != 1:
            raise ViewAxisError(f"axis {self._axis!r} renamed twice")
        return _LocalCoordView(self.value_kind, next(iter(renames)), None)

    def at(self, **coords: CoordValue) -> "_LocalCoordView":
        """Contract on GridView.at (pure; binds the single axis)."""
        if not coords:
            return self
        if self._axis is None or set(coords) != {self._axis}:
            missing = [name for name in coords if name != self._axis]
            raise ViewAxisError(f"cannot bind absent axis {missing[0]!r}")
        value = coords[self._axis]
        self._check_kind(value)
        return _LocalCoordView(self.value_kind, None, value)

    def read_only(self) -> "_LocalCoordView":
        """Contract on GridView.read_only (coord views are already read-only)."""
        return self

    def value(self, **coords: CoordValue) -> CoordValue:
        """Contract on CoordView.value."""
        if self._bound is not None:
            if coords:
                raise ViewAxisError(f"cannot bind absent axis {next(iter(coords))!r}")
            return self._bound
        bound = self.at(**coords)._bound
        if bound is None:
            raise ViewAxisError(f"axis {self._axis!r} left unbound; value() requires it")
        return bound


def int_coord(name: str) -> CoordView:
    """A fresh int-valued coordinate view over one axis called ``name``.

    See :class:`CoordView`. Wire it into ``open_task`` args: the axis is
    vmap-consumed when ``name`` matches the strip-set (the task learns its
    own coordinate), kept as the identity view otherwise.
    """
    return _LocalCoordView("int", name, None)


def str_coord(name: str) -> CoordView:
    """A fresh str-valued coordinate view over one axis called ``name``.

    See :func:`int_coord`.
    """
    return _LocalCoordView("str", name, None)


class HandleView(GridView):
    """View of a handle grid (cells are service-handle entities).

    The outer level reads a handle cell's *key* — the handle entity id — via
    :meth:`read_key`; the key is plain data that can be checkpointed into
    step args, and step code exchanges it for a live service via
    :meth:`~fxtr_experiment.task_api.StepContext.connect`. Views themselves
    never cross into steps. A single-cell handle view is also what
    :meth:`SubtreeView.open_task` takes as the task fn.
    """

    async def read_key(self, **coords: CoordValue) -> FxtrID:
        """Read one handle cell's key, binding all remaining axes.

        Records a ``cell_value`` observation of the handle cell — observing
        the handle's *identity*, not its behavior (the external-binding
        trust boundary). Connect authority follows the record: only keys
        read *from handle views* by the current generation may be connected
        from its steps — authority never flows through data.

        Args:
            **coords: Coordinate per remaining axis name; must resolve the
                view to exactly one cell.

        Returns:
            The handle entity id stored at the cell.

        Raises:
            CellFaultError: If the cell is missing.
            ViewAxisError: If ``coords`` does not resolve the view to one cell.
        """
        raise NotImplementedError


class TaskView(GridView):
    """View of a task grid (cells are demand-driven, with named outputs).

    A task declares named outputs (entity or dataframe); *reading any output
    forces execution* of the demanded cell, and the read couples the
    reader's currency to the subject: if the observed output later loses
    freshness the reader is unsettled until it resolves, and stale if the
    subject settles on a different value (see the currency section of
    ``notes/execution_semantics.md``). There is no output-less read — use
    :meth:`output` to get a readable view. Observations (and therefore
    currency) are per-output: an idempotent rerun that reproduces output A
    identically keeps A's observers fresh.
    """

    def output(self, name: str | None = None) -> "DataView | DataFrameView":
        """Select one declared output (pure selection).

        Entity outputs yield a read-only :class:`DataView`-shaped view;
        dataframe outputs a read-only :class:`DataFrameView`-shaped view.
        Reads/gathers through it force execution and fault if the demanded
        cell errors (fault propagation).

        Args:
            name: A name declared in the task grid's ``outputs``, or ``None``
                to select the grid's *sole* declared output (an error if it
                declares several). The no-arg form is resolved to the concrete
                name immediately — specs and stored rules always carry
                explicit output names, so implicit and explicit selection
                canonicalize identically.

        Raises:
            ViewAxisError: If ``name`` is not a declared output, ``None`` is
                used on a multi-output grid, or an output was already
                selected on this view.
        """
        raise NotImplementedError

    def output_entity(self, name: str | None = None) -> "DataView":
        """:meth:`output`, statically typed for a declared *entity* output.

        Args:
            name: As :meth:`output`.

        Raises:
            ViewAxisError: As :meth:`output`, or if the selected output is
                declared as a dataframe output.
        """
        view = self.output(name)
        if not isinstance(view, DataView):
            raise ViewAxisError(f"output {name!r} is a dataframe output, not an entity output")
        return view

    def output_frame(self, name: str | None = None) -> "DataFrameView":
        """:meth:`output`, statically typed for a declared *dataframe* output.

        Args:
            name: As :meth:`output`.

        Raises:
            ViewAxisError: As :meth:`output`, or if the selected output is
                declared as an entity output.
        """
        view = self.output(name)
        if not isinstance(view, DataFrameView):
            raise ViewAxisError(f"output {name!r} is an entity output, not a dataframe output")
        return view


class CheckpointView(GridView):
    """View of a checkpoint grid — only ever held by the task cell bound to it."""

    async def run_step(
        self,
        step_fn: "StepFnLike | ServiceRef[UnknownService]",
        *,
        args: Mapping[str, FxtrServiceData],
        coords: Mapping[str, CoordValue],
    ) -> FxtrServiceData:
        """Run a step durably into one checkpoint cell.

        Unlike the other view operations, both mappings are explicit keyword
        parameters (no ``**kwargs`` slot): a step invocation carries two
        name-keyed mappings — step arguments and cell coordinates — and
        letting either claim the kwargs slot invites confusion between them.

        Replay: if the cell holds an entry bound to the current generation,
        the recorded invocation (step fn identity + args) is compared against
        this one — on a match the stored result is returned without running
        the step; on a mismatch the outer level was nondeterministic and the
        task is poisoned. Otherwise the step runs, and its result is written
        and bound.

        Args:
            step_fn: The step to run. Delivered its args as keyword arguments
                (``await step_fn(ctx, **args)``).
            args: Named step arguments, recorded in the checkpoint entry.
                Values are plain data; handles cross as keys previously
                read via :meth:`HandleView.read_key` (views never serialize
                into step args).
            coords: Coordinate per remaining axis name; must resolve the view
                to exactly one cell (``{}`` for a fully-bound view).

        Returns:
            The step's (possibly replayed) result.

        Raises:
            CheckpointCorruptionError: If the cell holds an entry bound to a
                different generation.
            NondeterminismError: If a current-generation entry exists but its
                recorded invocation differs from this one.
            ViewAxisError: If ``coords`` does not resolve the view to one cell.
        """
        raise NotImplementedError


class SubtreeView(GridView):
    """View of a subtree grid: a namespace of child grids, opened by key.

    **Background.** A subtree grid is a grid whose cells are namespaces: each
    cell maps string keys to child grids. The *regularity invariant* says
    that across all cells of one subtree, a given key has the same grid kind
    and configuration — so "the child grid at key K" is well-defined across
    the whole subtree, and a child's full coordinate space is the subtree's
    axes followed by the child's own axes.

    **Opens act on the whole view.** Because of regularity, opening is an
    operation over every cell this view can address at once, not on one cell:
    ``open_data("results", axes=(...))`` opens (or creates) key ``results``
    across the view and returns a child view whose axes are *this view's axes
    followed by the child's new axes*. If any of those names collide, the
    open raises — rename this view first (names are local, so this is always
    possible and affects nothing stored). To address a specific cell instead,
    bind constants first: ``view.at(variant="v1").open_data(...)``.

    **Opening is idempotent create-or-attach**, matched on the canonical
    config (positional structure + the observable names): if the key already
    exists with the same config, the existing child is attached; a different
    config is an error. Every open records a ``grid_config`` observation (the
    opener asserts the child's shape — it becomes stale if that changes);
    an open through write authority additionally records a *would-create
    claim* (it would create the grid in an empty world — grounding's
    ``asserted_grid``), whether or not this open physically created it.

    **Access follows the chain.** Opens carry no mode: children carry
    whatever authority this view's chain confers. Write on a subtree *is*
    create access and implies write/create access to everything
    transitively inside; **child authority never exceeds the subtree
    view's** — under a read-authority view (a received read grant, a
    sliced window, an internals root browsed from the top) children come
    back read-only, so read access to a subtree confers no write
    authority anywhere inside it. Forwarding this view into a
    created task's args grants the child task access (capability passing)
    at the view's *effective* mode — drop with :meth:`~GridView.read_only`
    to wire it read-only; merely holding a view observes nothing.

    **Full-frame invariant — a property of creation capability.** A
    write-authority subtree view is a creation site and is always
    total: every one of the target subtree's axes
    is always either named on this view or bound to one of the holder's own
    coordinates. Consequently creation is never a silent broadcast over a
    removed axis, and the only sanctioned way to "remove" a subtree axis is
    vmap consumption by a created task's coordinates — where the created
    grid takes responsibility for a coherent config across it. Slicing
    (:meth:`at`) **sheds create authority**: a sliced view is an ordinary
    browsing window — opens under it attach
    per-cell child views (read-only, like every child of a read view;
    creates are refused for lack of create access), and ``open_task``
    through it is rejected (creation paths must be
    total, whatever their mode). Relatedly (batch-invariance): forwarding
    *any* view bound to a holder axis that the creation path does not map
    to a child coordinate is rejected at create time — a created grid's
    wiring cannot secretly vary with an unmapped axis of its creator.
    (Precise definition of *mapped*: see `view_spec.py`'s module
    docstring.)
    """

    def at(self, **coords: CoordValue) -> Self:
        """Slice this subtree view into a browsing window.

        Slicing sheds create authority: the returned view is
        read-effective whatever this view's authority (a slice of a
        creation site is definitionally a window — enforced locally here
        and at the wire), so opens through it attach existing grids
        read-only and creates are refused.

        Raises:
            ViewAxisError: A named axis is absent from the view.
        """
        raise NotImplementedError

    async def open_data(self, key: str, axes: tuple[AxisSpec, ...]) -> DataView:
        """Open (create-or-attach) the data grid at ``key``.

        Opens carry no mode: the returned view follows this view's
        authority — writable under a write-authority view (creation
        included), read-only under a read view or a sliced window (where
        a missing grid is an attach failure). Use
        :meth:`~GridView.read_only` on the *returned* view to drop
        privileges before wiring it onward read-only.

        Args:
            key: The child key within this subtree.
            axes: The child's own axes. Their names are the official stored
                names, and (unchanged) the returned view's local names for the
                new axes.

        Returns:
            A :class:`DataView` with axes = this view's axes + ``axes``.

        Raises:
            GridConfigMismatchError: If ``key`` exists with a different config.
            ViewAxisError: If ``axes`` names collide with this view's axes,
                or creation is needed without create authority.
        """
        raise NotImplementedError

    async def open_dataframe(
        self, key: str, axes: tuple[AxisSpec, ...], schema: FrameSchema
    ) -> DataFrameView:
        """Open (create-or-attach) the dataframe grid ``key``. See
        :meth:`open_data` (opens carry no mode).

        Args:
            key: The child key within this subtree.
            axes: The child's own axes (official stored names).
            schema: The declared column schema of every cell.

        Returns:
            A :class:`DataFrameView` with axes = this view's axes + ``axes``.

        Raises:
            GridConfigMismatchError: If ``key`` exists with a different config
                (axes or schema).
            ViewAxisError: If ``axes`` names collide with this view's axes.
        """
        raise NotImplementedError

    async def open_handle(self, key: str, axes: tuple[AxisSpec, ...]) -> HandleView:
        """Attach to the existing handle grid at ``key`` (read-only).

        Attach-only from task code: tasks never mint authority, so handle
        grids are *created* and their cells *written* only at the top level
        — even subtree write access does not confer handle-grid creation.
        A missing key is a not-ready dependency and faults (the user may
        create it and re-demand). Records a ``grid_config`` observation
        like every open.

        Args:
            key: The child key within this subtree.
            axes: The handle grid's own axes (official stored names).

        Returns:
            A read-only :class:`HandleView` with axes = this view's axes +
            ``axes``.

        Raises:
            CellFaultError: If ``key`` does not exist (not creatable here).
            GridConfigMismatchError: If ``key`` exists with a different
                config.
            ViewAxisError: If ``axes`` names collide with this view's axes.
        """
        raise NotImplementedError

    async def open_subtree(self, key: str, axes: tuple[AxisSpec, ...]) -> "SubtreeView":
        """Open (or create) the nested subtree grid at ``key``.

        The child inherits this view's mode: under a write-mode view it is
        another creation site; under a read-mode view it is a read-only
        browse one level deeper (creation still requires write access).

        Args:
            key: The child key within this subtree.
            axes: The child subtree's own axes (official stored names).

        Returns:
            A :class:`SubtreeView` with axes = this view's axes + ``axes``.

        Raises:
            GridConfigMismatchError: If ``key`` exists with a different config.
            ViewAxisError: If ``axes`` names collide with this view's axes.
        """
        raise NotImplementedError

    async def open_checkpoint(self, key: str, axes: tuple[AxisSpec, ...]) -> CheckpointView:
        """Open (or create) the checkpoint grid at ``key``.

        Only succeeds within the calling task's own internals subtree
        (reached via :meth:`~fxtr_experiment.task_api.TaskContext.own_internals`);
        the grid is bound to the calling task cell at creation.

        Args:
            key: The child key within this (internals) subtree.
            axes: The checkpoint grid's own axes (official stored names).

        Returns:
            A :class:`CheckpointView` with axes = this view's axes + ``axes``.

        Raises:
            GridConfigMismatchError: If ``key`` exists with a different config,
                or this view is not within the caller's own internals.
            ViewAxisError: If ``axes`` names collide with this view's axes.
        """
        raise NotImplementedError

    async def open_task(
        self,
        key: str,
        *,
        axes: tuple[AxisSpec, ...],
        task_fn: HandleView,
        args: Mapping[str, GridView],
        outputs: Mapping[str, OutputSpec] | None = None,
        local_names: Mapping[str, str] | None = None,
    ) -> TaskView:
        """Open (or create) the task grid at ``key``, wiring it by name matching.

        The **strip-set** is this view's axis names plus the local coordinate
        names. Wiring consumes strip-set names from ``task_fn`` and each arg
        view (vmap: those axes get bound to the corresponding task-cell
        coordinate); whatever names remain on an arg view are kept and become
        the official names the task fn receives for that arg.

        Example (``my_subtree`` has axes ``(x, y)``)::

            await my_subtree.open_task(
                "t", axes=(AxisSpec("z", "int"),),
                local_names={"z": "z1"},                # optional; rarely needed
                task_fn=my_task_handles,                # axes (y) — fully consumed
                args={
                    "one": my_arg_1.rename_axes(z1="z"),   # (y, z1) → received as scalar
                    "two": my_arg_2.rename_axes(x="a", z1="b"),  # (x, z1, c) → received as (c)
                })                                      # → TaskView with axes (x, y, z1)

        Args:
            key: The child key within this subtree.
            axes: The task grid's own axes — the new coordinates it maps
                over. Their names are the *official* stored names (used by
                visualization and introspection outside tasks).
            task_fn: Single-fn wiring: a handle view that, after removing the
                strip-set names, must have **no axes left**, so exactly one fn
                cell resolves per task cell.
            args: Named views the task gets access to. Strip-set-named axes
                are vmap-consumed; each remaining local name becomes the
                official received name for that axis (recorded in the
                canonical binding; identity-bearing). Access mode travels
                with each view.
            outputs: The task's declared named outputs — each
                :class:`~fxtr_experiment.model.EntityOutput` or
                :class:`~fxtr_experiment.model.FrameOutput` (schema
                included); part of the canonical config. Every complete
                execution must fill all of them. Defaults to the common
                single-entity case,
                :data:`~fxtr_experiment.model.DEFAULT_TASK_OUTPUTS`
                (one ``EntityOutput`` named ``"result"``).
            local_names: Optional map from official coordinate names to the
                local names used in this creation context (default:
                identity). Local names must not overlap this view's axis
                names.

        Returns:
            A :class:`TaskView` with axes = this view's axes + the local
            coordinate names.

        Raises:
            GridConfigMismatchError: If ``key`` exists with a different
                canonical config (including received-name disagreements).
            ViewAxisError: On name collisions, or a ``task_fn`` axis left
                unconsumed by the strip-set.
        """
        raise NotImplementedError
