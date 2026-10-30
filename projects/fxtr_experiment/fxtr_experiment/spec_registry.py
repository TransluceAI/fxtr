"""Runner-side token bookkeeping: grants ↔ true specs, wire → canonicalizer.

The runner holds, per run, the **true spec** behind every granted token — a
holder-frame :data:`~fxtr_experiment.view_spec.ViewSpec` (absolute paths,
Param/Keep templates) that never crosses the boundary. This module is the
translation layer: minting grants from specs, rebuilding sliced specs from
incoming ``ViewSel``/``WiredArg`` values (positions → the spec's kept-axis
names, in order), and feeding ``open_task`` wiring into the existing
canonicalizer (`view_spec.canonicalize_open_task`) unchanged.

Tokens are ephemeral per-registry (a counter); they carry no durable
meaning (see `runner_api.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Awaitable, Callable, Mapping

from frozendict import frozendict

from fxtr_experiment.encoding import decode_key_path, encode_key_path
from fxtr_experiment_task.errors import ViewAxisError
from fxtr_experiment_task.model import (
    AxisKind,
    AxisSpec,
    CoordBinding,
    CoordTuple,
    CoordValue,
    DataFrameGridConfig,
    FrameOutput,
    FrameSchema,
    GridConfig,
    KeepAxis,
    LiteralCoord,
    OutputSpec,
    Param,
    SliceBinding,
    TaskGridConfig,
)
from fxtr_experiment_task.runner_api import ViewGrant, ViewSel, ViewToken, WiredArg, WiredCoord
from fxtr_experiment_task.view_spec import (
    CoordViewSpec,
    TaskViewSpec,
    ViewSpec,
    WiredSpecArg,
    bind_coord,
    bind_keeps,
    canonicalize_open_task,
    check_spec,
    internals_spec,
    select_output,
    spec_from_binding,
    spec_from_coord_binding,
    spec_from_task_fn,
)


@dataclass(frozen=True)
class _Entry:
    """One minted grant's runner-side state: the true spec + the grant
    extras (schema/outputs) needed to reconstruct child grants."""

    spec: ViewSpec
    schema: FrameSchema | None
    outputs: "frozendict[str, OutputSpec] | None"


class SpecRegistry:
    """Token → true-spec map for one run (or one user session)."""

    def __init__(self) -> None:
        """Empty registry; tokens are a per-registry counter (ephemeral)."""
        self._entries: dict[ViewToken, _Entry] = {}
        self._n = 0

    def mint(
        self,
        spec: ViewSpec,
        *,
        schema: FrameSchema | None = None,
        outputs: "frozendict[str, OutputSpec] | None" = None,
        value: CoordValue | None = None,
    ) -> ViewGrant:
        """Register a true spec; returns the grant to push across the wire.

        A task spec with a **pre-selected output** (a rule binding's
        ``SliceTemplate.output``) presents as its *value* kind: there is no
        such thing as "a task view with a selected output" in the
        user-facing calculus — ``TaskView.output()`` yields a
        Data(Frame)View — so the callee's shadow is a data / dataframe
        view (schema from the output declaration; no outputs map), while
        the true spec keeps ``kind="task"`` + ``output`` for resolution
        and result serving.

        Args:
            spec: The true spec to register.
            schema: Grant extra for dataframe targets.
            outputs: Grant extra (and output-resolution input) for task
                targets.
            value: Grant extra for coord specs with a bound template: the
                resolved coordinate (see ``ViewGrant.value``). Rejected
                elsewhere — no other grant serves an unrecorded value.

        Raises:
            ViewAxisError: A selected-output task spec minted without the
                target's declared outputs, or naming an undeclared output;
                a ``value`` on a non-coord spec.
        """
        check_spec(spec)
        if value is not None and not isinstance(spec, CoordViewSpec):
            raise ViewAxisError(f"a {spec.kind!r} grant carries no resolved value")
        shadow_kind, shadow_schema, shadow_outputs = spec.kind, schema, outputs
        if isinstance(spec, TaskViewSpec) and spec.output is not None:
            if outputs is None or spec.output not in outputs:
                raise ViewAxisError(
                    f"selected output {spec.output!r} is not among the target's"
                    f" declared outputs ({sorted(outputs) if outputs else 'none supplied'})"
                )
            out_spec = outputs[spec.output]
            if isinstance(out_spec, FrameOutput):
                shadow_kind, shadow_schema = "dataframe", out_spec.schema
            else:
                shadow_kind = "data"
            shadow_outputs = None
        token = ViewToken(f"t{self._n}")
        self._n += 1
        self._entries[token] = _Entry(spec=spec, schema=shadow_schema, outputs=shadow_outputs)
        return ViewGrant(
            token=token,
            kind=shadow_kind,
            axes=spec.axes,
            mode=spec.mode,
            schema=shadow_schema,
            outputs=shadow_outputs,
            value=value,
        )

    def spec_of(self, token: ViewToken) -> ViewSpec:
        """The true spec behind a token.

        Raises:
            ViewAxisError: Unknown token (not minted by this registry).
        """
        entry = self._entries.get(token)
        if entry is None:
            raise ViewAxisError(f"unknown view token {token!r}")
        return entry.spec

    def frame_schema(self, sel: ViewSel) -> FrameSchema | None:
        """The declared frame schema of the value a selection addresses.

        The capabilities' frame ops resolve the identity-bearing schema —
        and decide frame-vs-entity target kind — through this: a dataframe
        grant answers its grid schema; a task target answers its selected
        output's ``FrameOutput`` schema (whether the output was
        pre-selected by a rule binding — the mint stored its schema — or
        arrives on the wire as ``sel.output``). ``None`` means the
        selection addresses an entity-valued (or unselected-output)
        target. Schemas here come from the stored/proposed config the
        grant was minted from, so they are the declared authority.

        Raises:
            ViewAxisError: Unknown token, or a wire-selected output name
                the target never declared.
        """
        entry = self._entries.get(sel.token)
        if entry is None:
            raise ViewAxisError(f"unknown view token {sel.token!r}")
        spec = entry.spec
        if isinstance(spec, TaskViewSpec):
            if spec.output is not None:
                # Pre-selected output (a rule binding's slice): the mint
                # recorded the output's schema (None for entity outputs).
                return entry.schema
            if sel.output is not None:
                if entry.outputs is None:
                    raise ViewAxisError("task grant carries no output declaration")
                out = entry.outputs.get(sel.output)
                if out is None:
                    raise ViewAxisError(
                        f"{sel.output!r} is not a declared output ({sorted(entry.outputs)})"
                    )
                return out.schema if isinstance(out, FrameOutput) else None
            return None
        if spec.kind == "dataframe":
            return entry.schema
        return None

    def sliced(self, sel: ViewSel) -> ViewSpec:
        """Rebuild the true spec addressed by a selection (bindings applied,
        output selected)."""
        spec = self.spec_of(sel.token)
        if sel.output is not None:
            spec = select_output(spec, sel.output)
        if sel.bound:
            if isinstance(spec, CoordViewSpec):
                spec = bind_coord(spec, dict(sel.bound))
            else:
                spec = bind_keeps(spec, dict(sel.bound))
        return spec

    def wired_spec(self, arg: WiredArg) -> WiredSpecArg:
        """Rebuild a forwarded arg for the canonicalizer: sliced spec with the
        effective mode applied (validated as a downgrade), plus the holder's
        positional wiring instructions passed through untouched. Coord specs
        (a received coord grant forwarded onward) pass through as-is — their
        mode is fixed read."""
        spec = self.sliced(arg.sel)
        if isinstance(spec, CoordViewSpec):
            if arg.mode == "write":
                raise ViewAxisError("cannot upgrade a read grant to write")
            return WiredSpecArg(spec=spec, wiring=arg.wiring)
        if arg.mode == "write" and spec.mode != "write":
            raise ViewAxisError("cannot upgrade a read grant to write")
        return WiredSpecArg(spec=replace(spec, mode=arg.mode), wiring=arg.wiring)

    def open_task(
        self,
        subtree: ViewSel,
        key: str,
        axes: tuple[AxisSpec, ...],
        task_fn: WiredArg,
        args: "Mapping[str, WiredArg | WiredCoord]",
        outputs: "frozendict[str, OutputSpec]",
    ) -> tuple[TaskGridConfig, ViewGrant]:
        """Translate wire ``open_task`` wiring into the canonicalizer.

        Token-backed args (:class:`~fxtr_experiment.runner_api.WiredArg`)
        resolve through this registry; locally created coord args
        (:class:`~fxtr_experiment.runner_api.WiredCoord`) carry their whole
        spec on the wire (:func:`coord_wire_spec`). Returns the canonical
        key-level config (for the ops engine to match/store) and the child
        task grant.
        """
        config, task_view = canonicalize_open_task(
            self.sliced(subtree),
            key=key,
            axes=axes,
            task_fn=self.wired_spec(task_fn),
            args={
                name: (coord_wire_spec(a) if isinstance(a, WiredCoord) else self.wired_spec(a))
                for name, a in args.items()
            },
            outputs=outputs,
        )
        return config, self.mint(task_view, outputs=config.outputs)


def coord_wire_spec(w: WiredCoord) -> WiredSpecArg:
    """A locally created coord arg's wire form → its canonicalizer input.

    No token resolution — the :class:`~fxtr_experiment.runner_api.WiredCoord`
    carries the whole spec: an ``at()``-bound literal, or one open axis with
    exactly one wiring instruction. (The open axis's spec-level name is
    runner-internal bookkeeping; the identity-bearing name, if any, is the
    instruction's ``KeepName``.)

    Raises:
        ViewAxisError: ``bound`` and ``wiring`` inconsistent (both, or
            neither), or a literal/kind mismatch.
    """
    if (w.bound is None) == (len(w.wiring) == 0):
        raise ViewAxisError(
            "a wired coord carries either a bound literal or exactly one"
            f" wiring instruction; got bound={w.bound!r}, {len(w.wiring)} instructions"
        )
    if w.bound is not None:
        spec = CoordViewSpec(value_kind=w.value_kind, template=LiteralCoord(w.bound), axes=())
    else:
        if len(w.wiring) != 1:
            raise ViewAxisError(f"a 1D coord takes one wiring instruction, got {len(w.wiring)}")
        spec = CoordViewSpec(
            value_kind=w.value_kind,
            template=KeepAxis(),
            axes=(AxisSpec(name="coord", kind=w.value_kind),),
        )
    check_spec(spec)  # validates the literal against the kind
    return WiredSpecArg(spec=spec, wiring=w.wiring)


# -- Rule grants (the executor's run setup) -----------------------------------

ConfigLookup = Callable[[str], Awaitable[tuple[str, GridConfig]]]
"""Async lookup of a live grid's ``(kind, canonical config)`` by key-path
text. Supplied by the caller (the executor wires the workflow session's
``get_grid`` + ``load_config``); raises :class:`~fxtr_experiment.errors.
CellFaultError` itself if the grid is absent, or returns normally."""


@dataclass(frozen=True)
class RuleGrants:
    """Everything a run pushes across the boundary, minted from one rule.

    Attributes:
        task_fn: Grant for the run prologue's task-fn key read (zero-axis
            handle view; see `view_spec.spec_from_task_fn`).
        args: One grant per rule input, under the rule's input names.
        internals: The executing cell's internals grant.
    """

    task_fn: ViewGrant
    args: "frozendict[str, ViewGrant]"
    internals: ViewGrant


async def _flat_kinds(
    slice_path_keys: tuple[str, ...], lookup: ConfigLookup
) -> "tuple[AxisKind, ...]":
    """The coordinate kinds of a slice target's flattened axes, by walking
    every grid along the path (own-axis kinds of ``keys[:1]``, then
    ``keys[:2]``, ... then the target's own — kinds are not stored in
    bindings; they live in the grids' configs)."""
    kinds: list[AxisKind] = []
    for depth in range(1, len(slice_path_keys) + 1):
        _, config = await lookup(encode_key_path(slice_path_keys[:depth]))
        kinds.extend(a.kind for a in config.axes)
    return tuple(kinds)


async def _grant_for_binding(
    registry: SpecRegistry, binding: SliceBinding, lookup: ConfigLookup
) -> ViewGrant:
    """One rule slice input → one grant (kind/extras from the live target
    config)."""
    keys = tuple(step.key for step in binding.slice.path)
    target_kind, target_config = await lookup(encode_key_path(keys))
    spec = spec_from_binding(
        binding,
        target_kind=target_kind,  # type: ignore[arg-type]
        flat_kinds=await _flat_kinds(keys, lookup),
    )
    return registry.mint(
        spec,
        schema=target_config.schema if isinstance(target_config, DataFrameGridConfig) else None,
        outputs=target_config.outputs if isinstance(target_config, TaskGridConfig) else None,
    )


def _grant_for_coord(
    registry: SpecRegistry, binding: CoordBinding, holder_coords: CoordTuple
) -> ViewGrant:
    """One rule coord input → one grant, with the value resolved per-cell.

    The only per-cell work in grant minting: a ``Param`` template reads the
    executing cell's own coordinate (coordinates are structure — no
    observation, no record; re-minted grants are identical by construction,
    so the safety pass replays transparently). Kept templates carry no
    value (the view is the identity function, resolved task-side).
    """
    t = binding.template
    if isinstance(t, KeepAxis):
        return registry.mint(spec_from_coord_binding(binding))
    if isinstance(t, LiteralCoord):
        value = t.value
    else:
        if not 0 <= t.position < len(holder_coords):
            raise ViewAxisError(
                f"coord binding references holder axis {t.position}; this cell"
                f" has {len(holder_coords)} flattened coordinates"
            )
        value = holder_coords[t.position]
    return registry.mint(spec_from_coord_binding(binding), value=value)


async def mint_rule_grants(
    registry: SpecRegistry,
    config: TaskGridConfig,
    grid_path: str,
    lookup: ConfigLookup,
    *,
    holder_coords: CoordTuple,
) -> RuleGrants:
    """Mint every grant one run of a task cell pushes, from its stored rule.

    Stored rules are already holder-frame specs (P1: rules are written in
    the frame of the grid that owns them), so this is the mechanical
    re-tagging of `view_spec`'s ``spec_from_*`` functions plus the storage
    walk that reconstructs axis kinds and grant extras from the live
    configs. Coord bindings additionally resolve their per-cell value here
    (the reason this runs per run, with the cell's coordinates in hand).

    Args:
        registry: The run's registry (grants mint fresh tokens here).
        config: The task grid's canonical config (its rule drives this).
        grid_path: The task grid's key-path text.
        lookup: Live-config lookup (see :data:`ConfigLookup`).
        holder_coords: The executing cell's flattened coordinates (resolves
            consumed coord bindings' values).

    Returns:
        The run's grants (task-fn prologue read, args, internals).
    """
    keys = decode_key_path(grid_path)
    task_fn = registry.mint(spec_from_task_fn(config.rule.task_fn))
    args = {
        name: (
            _grant_for_coord(registry, binding, holder_coords)
            if isinstance(binding, CoordBinding)
            else await _grant_for_binding(registry, binding, lookup)
        )
        for name, binding in config.rule.inputs.items()
    }
    enclosing_counts = []
    for depth in range(1, len(keys)):
        _, enclosing = await lookup(encode_key_path(keys[:depth]))
        enclosing_counts.append(len(enclosing.axes))
    internals = registry.mint(
        internals_spec(tuple(keys), tuple(enclosing_counts), len(config.axes))
    )
    return RuleGrants(task_fn=task_fn, args=frozendict(args), internals=internals)
