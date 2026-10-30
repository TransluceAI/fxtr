"""Task-side view runtime: shadows + concrete view objects over a backend.

Implements the `views.py` contracts per the opaque-token wire design
(`runner_api.py`): a view object holds a **backend** (anything satisfying
:class:`ViewBackend` — the run capability for tasks, the user capability at
the top level, a fake in tests) and a **shadow** — the complete task-side
knowledge of one view. Shadows are *template-free*: just the grant, the
ordered open axes (with their original grant positions and current local
names), the accumulated ``at`` bindings, the effective mode, and any
selected output. All structural knowledge (paths, Param/Keep templates)
stays runner-side (`view_spec.py` / `spec_registry.py`).

Pure rewrites (`rename_axes` / `at` / `read_only`) replace the shadow;
effectful ops resolve local names to grant positions and emit
`ViewSel`/`WiredArg` values. Validation here is best-effort UX — the
backend re-validates authoritatively.
"""

from __future__ import annotations

import weakref
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Mapping, Protocol, Sequence

if TYPE_CHECKING:
    import polars as pl

from frozendict import frozendict

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope, ServiceRef, get_active_scope
from fxtr2.services.service_cap_common import FxtrServiceData, UnknownService
from fxtr2.services.service_dialect import OverrideThisTypeBeforeUsing
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method

from fxtr_experiment_task.errors import ViewAxisError, typed_wire_call
from fxtr_experiment_task.model import (
    DEFAULT_TASK_OUTPUTS,
    AxisSpec,
    CoordValue,
    EntityOutput,
    FrameOutput,
    FrameSchema,
    OutputSpec,
    SliceMode,
)
from fxtr_experiment_task.task_api import StepFnLike
from fxtr_experiment_task.runner_api import (
    StepCapabilityProto,
    WorkflowStepFnProto,
    FrameWireType,
    ViewGrant,
    ViewSel,
    ViewToken,
    WiredArg,
    WiredCoord,
)
from fxtr_experiment_task import views
from fxtr_experiment_task.view_spec import KeepName, wire_by_names


class ViewBackend(Protocol):
    """The effectful-op seam a view delegates to.

    A structural subset shared by the run and user capabilities (and fakes
    in view-layer tests); each method mirrors the like-named wire op —
    contracts and docstrings live on
    :class:`~fxtr_experiment.runner_api.RunCapabilityProto`.
    """

    async def read_cell_entity(self, sel: ViewSel) -> FxtrID:
        """Mirrors RunCapabilityProto.read_cell_entity."""
        ...

    async def read_cell_frame(self, sel: ViewSel) -> FrameWireType:
        """Mirrors RunCapabilityProto.read_cell_frame."""
        ...

    async def read_handle_key(self, sel: ViewSel) -> FxtrID:
        """Mirrors RunCapabilityProto.read_handle_key."""
        ...

    async def write_cell_entity(self, sel: ViewSel, value: FxtrID) -> None:
        """Mirrors RunCapabilityProto.write_cell_entity."""
        ...

    async def write_cell_frame(self, sel: ViewSel, frame: FrameWireType) -> None:
        """Mirrors RunCapabilityProto.write_cell_frame."""
        ...

    async def gather(self, sel: ViewSel, keys: FrameWireType) -> FrameWireType:
        """Mirrors RunCapabilityProto.gather."""
        ...

    async def open_data(self, subtree: ViewSel, key: str, axes: tuple[AxisSpec, ...]) -> ViewGrant:
        """Mirrors RunCapabilityProto.open_data."""
        ...

    async def open_dataframe(
        self,
        subtree: ViewSel,
        key: str,
        axes: tuple[AxisSpec, ...],
        schema: FrameSchema,
    ) -> ViewGrant:
        """Mirrors RunCapabilityProto.open_dataframe."""
        ...

    async def open_handle(
        self, subtree: ViewSel, key: str, axes: tuple[AxisSpec, ...]
    ) -> ViewGrant:
        """Mirrors RunCapabilityProto.open_handle."""
        ...

    async def open_subtree(
        self, subtree: ViewSel, key: str, axes: tuple[AxisSpec, ...]
    ) -> ViewGrant:
        """Mirrors RunCapabilityProto.open_subtree."""
        ...

    async def open_checkpoint(
        self, subtree: ViewSel, key: str, axes: tuple[AxisSpec, ...]
    ) -> ViewGrant:
        """Mirrors RunCapabilityProto.open_checkpoint."""
        ...

    async def open_task(
        self,
        subtree: ViewSel,
        key: str,
        axes: tuple[AxisSpec, ...],
        task_fn: WiredArg,
        args: "Mapping[str, WiredArg | WiredCoord]",
        outputs: "frozendict[str, OutputSpec]",
    ) -> ViewGrant:
        """Mirrors RunCapabilityProto.open_task."""
        ...

    async def run_step(
        self,
        checkpoint: ViewSel,
        step: "ServiceRef[UnknownService]",
        args: Mapping[str, FxtrServiceData],
    ) -> FxtrServiceData:
        """Mirrors RunCapabilityProto.run_step."""
        ...


@dataclass(frozen=True)
class _OpenAxis:
    """One still-open axis of a shadow.

    Attributes:
        position: Index into the grant's axes (the wire-positional space
            ``ViewSel.bound`` is keyed by); fixed for the shadow's life.
        name: The current local name (rewritten by renames).
        kind: Coordinate kind ("str" | "int"), for bind validation.
    """

    position: int
    name: str
    kind: str


@dataclass(frozen=True)
class Shadow:
    """Complete task-side state of one view.

    A shadow is everything the holder knows about a granted view — and
    deliberately nothing more: no paths, no Param/Keep templates (those are
    runner-side; see the module docstring). Pure rewrites return new
    shadows; effectful view ops resolve local names against the shadow and
    emit positional wire values. Local-name uniqueness is an invariant:
    enforced at construction and preserved by every rewrite — the shadow is
    *the* object all name resolution is relative to.

    Attributes:
        grant: The wire grant this shadow is a window onto. Its ``axes``
            order defines the positional index space used by ``bound`` and
            by every emitted ``ViewSel``.
        opens: The still-open axes, in grant order. Each carries its
            original grant position, its current *local* name (unique
            within this shadow; renames rewrite it), and its coordinate
            kind.
        bound: Accumulated ``at`` bindings: original grant position →
            constant.
        mode: The effective access mode — the grant's mode (the chain's
            authority), possibly dropped to read locally by ``read_only``
            or by slicing a subtree view. The drop is a *holder-local
            privilege drop*: this layer enforces it (writes/creates
            reject), but it is not a capability-level fact — the
            runner-side true spec keeps the chain's authority. Where the
            flag becomes real is wiring: forwarding the view into a child
            rule populates the binding's identity-bearing mode from it.
            Upgrades do not exist.
        output: For task-grid views: the declared output selected via
            ``TaskView.output()``. ``None`` otherwise.
    """

    grant: ViewGrant
    opens: tuple[_OpenAxis, ...]
    bound: "frozendict[int, CoordValue]"
    mode: SliceMode
    output: str | None = None

    @classmethod
    def of_grant(cls, grant: ViewGrant, local_names: Sequence[str] | None = None) -> "Shadow":
        """Build the initial shadow for a grant.

        Args:
            grant: The pushed grant.
            local_names: Local names for the grant's axes, in grant order
                (default: the grant's official names). Child opens use this
                to overlay the holder's names onto prefix positions.

        Returns:
            A shadow with every axis open and nothing bound.

        Raises:
            ViewAxisError: Name-count mismatch or duplicate local names.
        """
        names = list(local_names) if local_names is not None else [a.name for a in grant.axes]
        if len(names) != len(grant.axes):
            raise ViewAxisError(f"{len(names)} local names for {len(grant.axes)} grant axes")
        if len(set(names)) != len(names):
            raise ViewAxisError(f"duplicate local axis names: {names}")
        opens = tuple(
            _OpenAxis(position=i, name=names[i], kind=a.kind) for i, a in enumerate(grant.axes)
        )
        return cls(grant=grant, opens=opens, bound=frozendict(), mode=grant.mode)

    def axes(self) -> tuple[AxisSpec, ...]:
        """The open axes as (local name, kind) specs, in positional order."""
        return tuple(AxisSpec(name=o.name, kind=o.kind) for o in self.opens)  # type: ignore[arg-type]

    def rename(self, renames: Mapping[str, str]) -> "Shadow":
        """Rename open axes (pure).

        Args:
            renames: New name → existing local name (the
                ``views.rename_axes`` keyword convention).

        Returns:
            A shadow with the same opens under the new names.

        Raises:
            ViewAxisError: Absent old name, an old name renamed twice, or a
                resulting duplicate.
        """
        current = {o.name for o in self.opens}
        old_to_new: dict[str, str] = {}
        for new, old in renames.items():
            if old not in current:
                raise ViewAxisError(f"cannot rename absent axis {old!r}")
            if old in old_to_new:
                raise ViewAxisError(f"axis {old!r} renamed twice")
            old_to_new[old] = new
        opens = tuple(replace(o, name=old_to_new.get(o.name, o.name)) for o in self.opens)
        names = [o.name for o in opens]
        if len(set(names)) != len(names):
            raise ViewAxisError(f"rename produces duplicate axis names: {names}")
        return replace(self, opens=opens)

    def bind(self, coords: Mapping[str, CoordValue]) -> "Shadow":
        """Bind open axes to constants (the ``at`` rewrite; pure).

        Args:
            coords: Constant per local axis name. Coordinate types are
                exact: int axes take ``int`` (bool rejected), str axes
                take ``str``.

        Returns:
            A shadow without the bound axes; the constants accumulate into
            :attr:`bound` under the axes' original grant positions.

        Raises:
            ViewAxisError: Absent axis name or kind mismatch.
        """
        by_name = {o.name: o for o in self.opens}
        new_bound = dict(self.bound)
        for name, value in coords.items():
            o = by_name.get(name)
            if o is None:
                raise ViewAxisError(f"cannot bind absent axis {name!r}")
            if (o.kind == "int") != (type(value) is int):
                raise ViewAxisError(f"axis {name!r} has kind {o.kind!r} but got {value!r}")
            new_bound[o.position] = value
        opens = tuple(o for o in self.opens if o.name not in coords)
        return replace(self, opens=opens, bound=frozendict(new_bound))

    def read_only(self) -> "Shadow":
        """Downgrade the effective mode to read (pure; upgrades don't exist)."""
        return replace(self, mode="read")

    def sel(self, coords: Mapping[str, CoordValue] | None = None) -> ViewSel:
        """Emit the wire selection for a single-cell effectful op.

        Args:
            coords: Coordinates for the remaining open axes, by local name.
                After applying them, no axis may remain open.

        Returns:
            The ``ViewSel``: token + all accumulated positional bindings +
            the selected output (if any).

        Raises:
            ViewAxisError: If any axis remains open, or on bad coords.
        """
        s = self.bind(coords) if coords else self
        if s.opens:
            raise ViewAxisError(
                f"axes {[o.name for o in s.opens]} left unbound; a single cell is required"
            )
        return ViewSel(token=self.grant.token, bound=s.bound, output=self.output)

    def sel_partial(self, coords: Mapping[str, CoordValue] | None = None) -> ViewSel:
        """Emit a wire selection that may leave positions open.

        The multi-cell counterpart of :meth:`sel`, for ops whose contract
        addresses *every* matching cell (the user surface's ``list_cells``
        / ``clear_cells``); single-cell effectful ops keep :meth:`sel`'s
        fully-bound check.

        Args:
            coords: Coordinates for any subset of the open axes, by local
                name.
        """
        s = self.bind(coords) if coords else self
        return ViewSel(token=self.grant.token, bound=s.bound, output=self.output)

    def wired(
        self, subtree_names: tuple[str, ...] = (), coord_names: tuple[str, ...] = ()
    ) -> WiredArg:
        """Emit this view's ``open_task`` wiring (the strip-set, resolved).

        Matches each open axis's local name against, in order, the creating
        subtree view's local axis names then the local coordinate names
        (the two vocabularies are collision-checked by the caller), and
        emits one positional instruction per open axis; unmatched names
        become the child's *received* names.

        Args:
            subtree_names: The creating subtree view's local axis names.
            coord_names: The new coordinates' local names.

        Returns:
            The ``WiredArg``: selection + instructions + effective mode.
        """
        return WiredArg(
            sel=ViewSel(token=self.grant.token, bound=self.bound, output=self.output),
            wiring=wire_by_names(subtree_names, coord_names, tuple(o.name for o in self.opens)),
            mode=self.mode,
        )


class _RuntimeViewMixin:
    """Shared concrete behavior for the ``Runtime*View`` classes.

    The contract classes in `views.py` stay abstract — their docstrings are
    the API reference — and each concrete class multiply inherits this
    mixin *plus* its contract base: the mixin contributes the shared
    ``(backend, shadow)`` state and the three pure rewrites, the base
    contributes the type (so ``isinstance(view, views.DataView)`` etc.
    hold for task code), and per-kind effectful methods live on the
    concrete class. "Runtime" = the executable implementations, as opposed
    to the abstract contracts.
    """

    def __init__(self, backend: ViewBackend, shadow: Shadow) -> None:
        """Views are built via :func:`build_view` / subtree opens, not directly."""
        self._backend = backend
        self._shadow = shadow

    @property
    def axes(self) -> tuple[AxisSpec, ...]:
        """The view's named axes (contract: views.GridView.axes)."""
        return self._shadow.axes()

    def _with(self, shadow: Shadow):
        """Same view class over a rewritten shadow (the pure-rewrite helper)."""
        return type(self)(self._backend, shadow)  # type: ignore[call-arg]

    def rename_axes(self, **renames: str):
        """Pure rename; contract on views.GridView.rename_axes."""
        return self._with(self._shadow.rename(renames))

    def at(self, **coords: CoordValue):
        """Pure constant binding; contract on views.GridView.at."""
        return self._with(self._shadow.bind(coords))

    def read_only(self):
        """Pure mode downgrade; contract on views.GridView.read_only."""
        return self._with(self._shadow.read_only())


class RuntimeDataView(_RuntimeViewMixin, views.DataView):
    """Concrete DataView (contract + docstrings on views.DataView)."""

    async def read(self, **coords: CoordValue) -> FxtrID:
        """Contract on views.DataView.read; resolves via the shadow, delegates."""
        return await typed_wire_call(self._backend.read_cell_entity(self._shadow.sel(coords)))

    async def write(self, value: FxtrID, **coords: CoordValue) -> None:
        """Contract on views.DataView.write (+ the effective-mode gate)."""
        if self._shadow.mode != "write":
            raise ViewAxisError("view is not writable (effective mode is read)")
        await typed_wire_call(self._backend.write_cell_entity(self._shadow.sel(coords), value))


class RuntimeDataFrameView(_RuntimeViewMixin, views.DataFrameView):
    """Concrete DataFrameView (contract + docstrings on views.DataFrameView).

    The ``pl.DataFrame`` ⇆ wire-bytes boundary lives here (both directions
    via `tables`); everything identity-bearing is backend-side.

    TODO(phase5 chunk 4): gather.
    """

    @property
    def schema(self) -> FrameSchema:
        """Contract on views.DataFrameView.schema.

        Three grant shapes carry it: a dataframe grant's own ``schema``, a
        rule-binding grant whose output was pre-selected runner-side (the
        mint copied the output's schema onto the grant), and a task grant
        with the output selected task-side (``TaskView.output()`` — the
        schema lives in the grant's ``outputs`` declaration).
        """
        grant = self._shadow.grant
        if self._shadow.output is not None and grant.outputs is not None:
            spec = grant.outputs.get(self._shadow.output)
            if isinstance(spec, FrameOutput):
                return spec.schema
        if grant.schema is not None:
            return grant.schema
        raise ViewAxisError("dataframe view carries no declared schema")  # mint invariant

    async def read_table(self, **coords: CoordValue) -> "pl.DataFrame":
        """Contract on views.DataFrameView.read_table. Framework-produced
        bytes are trusted — parsed straight into a frame, no re-validation."""
        # Lazy import: polars loads on first frame op, not at package import.
        from fxtr_experiment_task import tables

        data = await typed_wire_call(self._backend.read_cell_frame(self._shadow.sel(coords)))
        return tables.ipc_to_frame(data)

    async def write_table(self, table: "pl.DataFrame", **coords: CoordValue) -> None:
        """Contract on views.DataFrameView.write_table (+ the effective-mode
        gate). Serialization only — validation against the declared schema
        and identity minting are host-side."""
        if self._shadow.mode != "write":
            raise ViewAxisError("view is not writable (effective mode is read)")
        from fxtr_experiment_task import tables

        await typed_wire_call(
            self._backend.write_cell_frame(self._shadow.sel(coords), tables.frame_to_ipc(table))
        )

    async def gather(
        self,
        table: "pl.DataFrame",
        on: Mapping[str, str],
        *,
        column_prefix: str | None = None,
    ) -> "pl.DataFrame":
        """Contract on views.DataFrameView.gather.

        The local half of the split (``notes/dataframes.md`` §Gather):
        resolve ``on`` against the open axes, extract + dedupe the key
        columns (`tables.extract_keys`), one wire call, then the key-join
        back onto the caller's table (`tables.gather_join` — row-index +
        stable sort, so output order is driving-row order × the stream's
        canonical order by construction).
        """
        opens = self._shadow.opens
        open_names = [o.name for o in opens]
        if set(on) != set(open_names):
            raise ViewAxisError(
                f"on= must bind exactly the open axes {open_names}; got {sorted(on)}"
            )
        key_cols = [on[name] for name in open_names]
        if len(set(key_cols)) != len(key_cols):
            raise ViewAxisError(
                f"on= binds several axes from one driving column ({key_cols});"
                " bind each axis from a distinct column"
            )
        kinds = tuple(o.kind for o in opens)
        from fxtr_experiment_task import tables

        keys = tables.extract_keys(table, key_cols, kinds)  # type: ignore[arg-type]  # _OpenAxis.kind is AxisKind by construction
        data = await typed_wire_call(
            self._backend.gather(self._shadow.sel_partial(), tables.frame_to_ipc(keys))
        )
        return tables.gather_join(
            table, key_cols, tables.ipc_to_frame(data), self.schema, column_prefix
        )


class RuntimeCoordView(_RuntimeViewMixin, views.CoordView):
    """Concrete grant-backed CoordView (contract on views.CoordView).

    The received form of a coord view: rule-input coord grants are
    token-backed (so forwarding into a child ``open_task`` translates like
    any other view — see `runner_api.WiredArg`), but :meth:`value` never
    touches the backend: a bound template's resolved value rode the grant
    (``ViewGrant.value``), and a kept axis is local shadow arithmetic.
    Locally created coord views are `views._LocalCoordView` instead.
    """

    def value(self, **coords: CoordValue) -> CoordValue:
        """Contract on views.CoordView.value (pure; no backend call)."""
        if self._shadow.grant.value is not None:
            # Consumed/literal template: zero-axis grant, value rode it.
            if coords:
                raise ViewAxisError(f"cannot bind absent axis {next(iter(coords))!r}")
            return self._shadow.grant.value
        s = self._shadow.bind(coords) if coords else self._shadow
        if s.opens:
            raise ViewAxisError(f"axis {s.opens[0].name!r} left unbound; value() requires it")
        # Kept axis: the identity function — the value is the binding at
        # the single grant position (accumulated at() or the call's coords).
        return s.bound[0]


class RuntimeHandleView(_RuntimeViewMixin, views.HandleView):
    """Concrete HandleView (contract on views.HandleView)."""

    async def read_key(self, **coords: CoordValue) -> FxtrID:
        """Contract on views.HandleView.read_key."""
        return await typed_wire_call(self._backend.read_handle_key(self._shadow.sel(coords)))


class RuntimeTaskView(_RuntimeViewMixin, views.TaskView):
    """Concrete TaskView (contract on views.TaskView)."""

    def output(self, name: str | None = None):
        """Contract on views.TaskView.output; picks the view class by output kind."""
        outputs = self._shadow.grant.outputs
        if self._shadow.output is not None:
            raise ViewAxisError(f"output already selected ({self._shadow.output!r})")
        if outputs is None:
            raise ViewAxisError("task grant carries no output declaration")
        if name is None:
            if len(outputs) != 1:
                raise ViewAxisError(f"output name required (grid declares {sorted(outputs)})")
            name = next(iter(outputs))
        spec = outputs.get(name)
        if spec is None:
            raise ViewAxisError(f"{name!r} is not a declared output ({sorted(outputs)})")
        shadow = replace(self._shadow, output=name, mode="read")
        cls = RuntimeDataView if isinstance(spec, EntityOutput) else RuntimeDataFrameView
        return cls(self._backend, shadow)


class RuntimeCheckpointView(_RuntimeViewMixin, views.CheckpointView):
    """Concrete CheckpointView (contract on views.CheckpointView)."""

    async def run_step(self, step_fn, *, args, coords) -> FxtrServiceData:
        """Contract on views.CheckpointView.run_step.

        A ``ServiceRef`` passes through as the step service. Otherwise the
        step fn must carry its declared identity (``step_type_id``, minted
        by the :func:`step_fn` decorator): a :class:`StepFnAdapter` already
        is one, and any other object with ``step_type_id`` is wrapped in a
        fresh adapter. The adapter is mounted on the caller's active scope —
        the task fn's execution context (the scope-discipline user-boundary
        capture), which outlives this call — and the mount is memoized per
        scope (see :meth:`StepFnAdapter.mount_on`), so a task looping over
        ``run_step`` with one long-lived adapter reuses the first mount.
        """
        sel = self._shadow.sel(coords)
        if isinstance(step_fn, ServiceRef):
            ref = step_fn
        elif isinstance(step_fn, StepFnAdapter):
            ref = await step_fn.mount_on(get_active_scope())
        else:
            type_id = getattr(step_fn, "step_type_id", None)
            if type_id is None:
                raise TypeError(
                    "plain step fns must declare their identity — decorate the"
                    " definition with @view_runtime.step_fn(type_id=...) (or pass"
                    " a ServiceRef to a step service)"
                )
            # A non-adapter carrying step_type_id shouldn't normally occur
            # (the decorator returns an adapter); wrap it to mount.
            ref = await StepFnAdapter(step_fn, type_id).mount_on(get_active_scope())
        return await typed_wire_call(self._backend.run_step(sel, ref, dict(args)))


class RuntimeSubtreeView(_RuntimeViewMixin, views.SubtreeView):
    """Concrete SubtreeView: opens + the task-side collision/strip logic."""

    def at(self, **coords: CoordValue):
        """Slice a subtree view into a browsing window. Slicing sheds
        create authority (locally here; at the wire via ``bind_keeps``) —
        contract on views.SubtreeView.at."""
        shadow = self._shadow.bind(coords)
        if shadow.mode == "write":
            shadow = replace(shadow, mode="read")
        return self._with(shadow)

    def _check_new_axes(self, axes: tuple[AxisSpec, ...]) -> None:
        """Task-side collision rule: child axis names must not collide with
        this view's *local* names (rename your own view first).

        Raises:
            ViewAxisError: On a collision.
        """
        collisions = {o.name for o in self._shadow.opens} & {a.name for a in axes}
        if collisions:
            raise ViewAxisError(
                f"child axes {sorted(collisions)} collide with the view's axes; rename first"
            )

    def _child(self, grant: ViewGrant, new_local: Sequence[str]) -> Any:
        """Wrap a returned child grant, overlaying this view's local names
        onto the prefix positions (the runner never learns locals).

        Args:
            grant: The child grant from the backend.
            new_local: Local names for the child's own (new) axes.

        Returns:
            The concrete child view, typed per the grant's kind.
        """
        prefix = tuple(o.name for o in self._shadow.opens)
        shadow = Shadow.of_grant(grant, local_names=(*prefix, *new_local))
        return build_view(self._backend, grant, shadow=shadow)

    async def open_data(self, key, axes):
        """Contract on views.SubtreeView.open_data."""
        self._check_new_axes(axes)
        grant = await typed_wire_call(
            self._backend.open_data(self._shadow.sel_partial(), key, axes)
        )
        return self._child(grant, [a.name for a in axes])

    async def open_dataframe(self, key, axes, schema):
        """Contract on views.SubtreeView.open_dataframe."""
        self._check_new_axes(axes)
        grant = await typed_wire_call(
            self._backend.open_dataframe(self._shadow.sel_partial(), key, axes, schema)
        )
        return self._child(grant, [a.name for a in axes])

    async def open_handle(self, key, axes):
        """Contract on views.SubtreeView.open_handle (attach-only for tasks)."""
        self._check_new_axes(axes)
        grant = await typed_wire_call(
            self._backend.open_handle(self._shadow.sel_partial(), key, axes)
        )
        return self._child(grant, [a.name for a in axes])

    async def open_subtree(self, key, axes):
        """Contract on views.SubtreeView.open_subtree."""
        self._check_new_axes(axes)
        grant = await typed_wire_call(
            self._backend.open_subtree(self._shadow.sel_partial(), key, axes)
        )
        return self._child(grant, [a.name for a in axes])

    async def open_checkpoint(self, key, axes):
        """Contract on views.SubtreeView.open_checkpoint."""
        self._check_new_axes(axes)
        grant = await typed_wire_call(
            self._backend.open_checkpoint(self._shadow.sel_partial(), key, axes)
        )
        return self._child(grant, [a.name for a in axes])

    async def open_task(self, key, *, axes, task_fn, args, outputs=None, local_names=None):
        """Contract on views.SubtreeView.open_task. This is where the
        strip-set resolves: local coordinate aliases apply, vocabularies are
        collision-checked, and each arg's positional wiring instructions are
        computed (Shadow.wired) before the purely positional wire call.
        """
        # Name resolution — the strip-set — happens entirely here, over the
        # holder's local vocabularies; the wire carries only positional
        # wiring instructions plus the identity-bearing names.
        local_map = dict(local_names) if local_names else {}
        if extra := set(local_map) - {a.name for a in axes}:
            raise ViewAxisError(f"local_names for unknown coordinates: {sorted(extra)}")
        locals_ = [local_map.get(a.name, a.name) for a in axes]
        if len(set(locals_)) != len(locals_):
            raise ViewAxisError(f"duplicate local coordinate names: {locals_}")
        self._check_new_axes(tuple(AxisSpec(n, a.kind) for n, a in zip(locals_, axes)))
        if not isinstance(task_fn, RuntimeHandleView):
            raise ViewAxisError("task_fn must be a handle view")
        strip = (tuple(o.name for o in self._shadow.opens), tuple(locals_))
        fn_arg = task_fn._shadow.wired(*strip)
        if unconsumed := [w.name for w in fn_arg.wiring if isinstance(w, KeepName)]:
            raise ViewAxisError(f"task_fn axes {unconsumed} not consumed by the wiring")
        wired_args: dict[str, WiredArg | WiredCoord] = {}
        for name, view in args.items():
            if isinstance(view, views._LocalCoordView):
                # Token-less: a local coord view was never granted, so its
                # whole (tiny) state crosses inline. Received coord views
                # are grant-backed and take the generic branch below.
                wired_args[name] = _wire_local_coord(view, *strip)
            elif isinstance(view, _RuntimeViewMixin):
                wired_args[name] = view._shadow.wired(*strip)
            else:
                raise ViewAxisError(f"arg {name!r} is not a view")
        grant = await typed_wire_call(
            self._backend.open_task(
                self._shadow.sel_partial(),
                key,
                tuple(axes),
                fn_arg,
                wired_args,
                frozendict(outputs) if outputs is not None else DEFAULT_TASK_OUTPUTS,
            )
        )
        return self._child(grant, locals_)


def _wire_local_coord(
    view: "views._LocalCoordView", subtree_names: tuple[str, ...], coord_names: tuple[str, ...]
) -> WiredCoord:
    """A local coord view's ``open_task`` wiring: bound → the literal;
    open → its one axis through the same strip-set as any other view."""
    if view._axis is None:
        return WiredCoord(value_kind=view.value_kind, bound=view._bound)
    return WiredCoord(
        value_kind=view.value_kind,
        wiring=wire_by_names(subtree_names, coord_names, (view._axis,)),
    )


_KIND_TO_CLASS = {
    "data": RuntimeDataView,
    "dataframe": RuntimeDataFrameView,
    "handle": RuntimeHandleView,
    "task": RuntimeTaskView,
    "checkpoint": RuntimeCheckpointView,
    "subtree": RuntimeSubtreeView,
    "coord": RuntimeCoordView,
}


def build_view(
    backend: ViewBackend, grant: ViewGrant, *, shadow: Shadow | None = None
) -> views.GridView:
    """Construct the concrete view object for a grant.

    The entry point for whoever receives grants (the task-side adapter for
    rule args and the internals grant; the user wrapper for the root).

    Args:
        backend: The effectful-op seam the view will delegate to.
        grant: The grant to wrap.
        shadow: Optional pre-built shadow (used by child opens to overlay
            the holder's local names onto prefix positions); defaults to
            ``Shadow.of_grant(grant)``.

    Returns:
        The concrete view, typed as the grant's kind's contract class.
    """
    cls = _KIND_TO_CLASS[grant.kind]
    return cls(backend, shadow if shadow is not None else Shadow.of_grant(grant))


class _StepContext:
    """The concrete :class:`~fxtr_experiment.task_api.StepContext` over a
    resolved step-capability proxy (one step invocation)."""

    def __init__(self, capability: StepCapabilityProto) -> None:
        """Bind to the invocation's resolved capability proxy."""
        self._capability = capability

    async def connect(self, handle: FxtrID) -> "ServiceRef[OverrideThisTypeBeforeUsing]":
        """Contract on task_api.StepContext.connect (retype before use)."""
        ref = await self._capability.connect(handle)
        return ref.replace_service_type(OverrideThisTypeBeforeUsing)

    async def log(self, message: str) -> None:
        """Contract on task_api.StepContext.log."""
        await self._capability.log(message)

    async def open_channel(self):
        """Contract on task_api.StepContext.open_channel (plumbing is
        deferred there; string ``log`` is the implemented path)."""
        raise NotImplementedError("step output channels are not plumbed yet; use log()")


class StepFnAdapter(TypedServiceImpl[WorkflowStepFnProto]):
    """A plain step fn, mounted as the ``WorkflowStepFnProto`` service.

    The task-side half of the step boundary (the sibling of
    `task_service.TaskFnAdapter`): builds the step context from the pushed
    capability ref and delegates to the user fn. The adapter's **service
    type id is the step's declared identity** — exactly what the runner
    records as the invocation's step half.

    Constructing the adapter is exactly what the :func:`step_fn` decorator
    does — ``@step_fn(type_id=...)`` over a callable and
    ``StepFnAdapter(callable, type_id)`` are interchangeable. The adapter is
    itself a :class:`~fxtr_experiment.task_api.StepFn` (see :meth:`__call__`),
    so a decorated fn behaves like the undecorated one when called locally.

    Attributes:
        step_type_id: The step's declared identity, owned by the *definition*
            site (two call sites must never disagree about what a step is).
            Returned as the mounted service's type id, and therefore the
            replay-compared half of every checkpoint invocation beside the
            args fingerprint. ``RuntimeCheckpointView.run_step`` sniffs this
            attribute to mount a plain (non-adapter) object carrying it.
    """

    def __init__(self, step_fn: "StepFnLike", type_id: FxtrID) -> None:
        """Wrap ``step_fn`` under its declared identity ``type_id``."""
        super().__init__()
        self._step_fn = step_fn
        self.step_type_id = type_id
        # Task-side convenience: one mount per execution scope. fxtr2 supports
        # mounting one impl instance into multiple scopes (per-mount state
        # rides each mount's lifespan), so this memo is not service state —
        # it only spares a task looping over run_step from re-mounting the
        # same long-lived adapter on the same scope each iteration. Keyed
        # weakly by scope so entries die with the scope.
        self._mounts: "weakref.WeakKeyDictionary[FxtrScope, ServiceRef[WorkflowStepFnProto]]" = (
            weakref.WeakKeyDictionary()
        )

    def _target_type_annotation(self) -> Any:
        """The proxy type this service resolves as."""
        return WorkflowStepFnProto

    def _service_type_id(self) -> FxtrID:
        """The step's declared identity (see the class docstring)."""
        return self.step_type_id

    async def mount_on(self, scope: FxtrScope) -> ServiceRef[WorkflowStepFnProto]:
        """Mount this adapter on ``scope``, memoized per scope.

        Args:
            scope: The scope to mount on (the task fn's execution context).

        Returns:
            A ref to the mounted step service, resolvable in ``scope``.
            Repeated calls with the same scope return the first mount's ref:
            on a memo hit the ref was mounted on *this same* scope, so it is
            still resolvable there.

        Note:
            A benign race is accepted: two concurrent calls may both mount
            before the memo populates. Both mounts are valid (fxtr2 allows an
            impl in multiple scopes); the memo simply keeps the last stored.
        """
        ref = self._mounts.get(scope)
        if ref is not None:
            return ref
        ref = await scope.mount(self)
        self._mounts[scope] = ref
        return ref

    @service_method
    async def run_step(
        self,
        capability: ServiceRef[UnknownService],
        args: Mapping[str, FxtrServiceData],
    ) -> FxtrServiceData:
        """Contract on `runner_api.WorkflowStepFnProto.run_step`: resolve
        the capability, build the context, invoke the user fn."""
        # Active scope: the per-call dispatch scope (the documented
        # service-method exception) — where the framework decoded
        # `capability`, so it resolves here.
        cap = await capability.replace_service_type(StepCapabilityProto).resolve()
        return await self._step_fn(_StepContext(cap), **dict(args))

    async def __call__(self, ctx: Any, /, **args: Any) -> Any:
        """Delegate to the wrapped fn — the :class:`~fxtr_experiment.task_api.
        StepFn` contract, so a decorated fn is interchangeable with the
        undecorated one when called locally.

        Not a ``@service_method``: dunders are excluded from service-method
        discovery (``walk_service_methods(..., forbid_dunders=True)`` walks
        only tagged methods), so this is never exposed on the wire — the
        service surface is :meth:`run_step` alone.
        """
        return await self._step_fn(ctx, **args)


def step_fn(*, type_id: FxtrID):
    """Declare a step at its definition site: ``@step_fn(type_id=...)``.

    Decorating *is* wrapping in :class:`StepFnAdapter` — the decorator and the
    constructor are interchangeable.

    Args:
        type_id: The step's identity (mint via
            ``fxtr2.data.system_entities.define_type`` in your project's
            namespace, optionally pointing at an entity describing the step).
            Identity-bearing: a checkpoint replays only if the recorded
            invocation's type id and args both match.

    Returns:
        The decorator producing a :class:`StepFnAdapter` with ``step_type_id``
        set — what ``RuntimeCheckpointView.run_step`` mounts.
    """

    def decorate(fn: "StepFnLike") -> StepFnAdapter:
        return StepFnAdapter(fn, type_id)

    return decorate
