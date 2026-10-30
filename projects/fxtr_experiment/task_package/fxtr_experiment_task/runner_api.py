"""Task↔runner service contracts: the wire model behind the view calculus.

This module defines the design's service boundary (see
``notes/design_overview.md``): task fns are services the *runner* invokes;
every effectful grid operation proxies back through a per-run **run
capability**; steps are services the runner invokes with a narrower **step
capability**. Everything in this module crosses (or sits directly at) an
fxtr2 service boundary, so every signature stays within the
``FxtrServiceData`` / typed-dialect vocabulary. The protocols are plain
``typing.Protocol`` sketches for now and become ``@service_protocol``\\ s when
the runner lands; nothing here may assume same-process execution.

**Who calls whom.** One run of a task cell (run/generation semantics:
``notes/execution_semantics.md``):

1. The runner resolves the cell's task-fn handle into a
   :class:`WorkflowTaskFnProto` service (provider/registration machinery is
   deferred to the top-level-API design), opens a per-run scope, mounts a
   :class:`RunCapabilityProto` impl into it, and calls :meth:`~
   WorkflowTaskFnProto.run_task`, pushing one :class:`ViewGrant` per rule
   input plus the internals grant and the declared outputs.
2. The task-side *adapter* (library code, not user code) builds local view
   objects (`views.py`) over the grants, constructs the
   :class:`~fxtr_experiment.task_api.TaskContext`, and calls the user's
   :class:`~fxtr_experiment.task_api.TaskFn`. It normalizes the return into
   one ``FxtrID`` per declared output (encoding frame (``polars.DataFrame``)
   outputs as canonical frame entities) and returns them over the standard service
   return path — entity closures ride the return arena, and the runner
   persists them into the entity store *before* recording the ids
   (the liveness invariant).
3. Every effectful view op becomes one capability call carrying a
   :class:`ViewSel`. The runner performs the record-check, records
   observations (and would-create claims on write-authority opens),
   applies demand policy, and serves the
   outcome. Pure rewrites never reach the runner (see the wire view model
   below).
4. ``run_step`` ships a step ``ServiceRef`` plus args; the runner
   record-checks the checkpoint cell and, on a miss, invokes the step with a
   fresh :class:`StepCapabilityProto` mounted in a per-step scope. Step args
   are plain data — steps never receive views; a handle crosses as a
   previously-read *key* (see below). The step's identity in the recorded
   invocation is its fxtr *service type id* (user-controllable; the
   canonical "what this service is" identity).

**Wire view model: opaque tokens, positional addressing.** Absolute grid
paths never cross this boundary. A granted view is a :class:`ViewGrant`: an
opaque :data:`ViewToken` plus the local *shadow* (axis specs, mode,
kind-specific extras) — which is exactly everything task-side code is
allowed to know. The three pure rewrites never cross the wire on their own:

- ``rename_axes`` is entirely task-side — wire addressing is *positional*
  against the token's granted axis order, so local names are free.
- ``at`` accumulates into :attr:`ViewSel.bound` (position → constant),
  shipped with each effectful op.
- ``read_only`` downgrades accumulate task-side too, surfacing only in
  ``open_task`` wiring, where the effective mode is identity-bearing
  (:attr:`WiredArg.mode`, validated against the grant's mode).

Names cross the wire only where they are identity-bearing: the new
coordinates' official names and each kept axis's received name
(``KeepName`` instructions inside :attr:`WiredArg.wiring`). All name
*resolution* — the strip-set, collision rules, local coordinate aliases —
happens holder-side in the view layer, which emits purely positional
wiring instructions ("this open axis is consumed by the p-th
holder-visible child coordinate — the subtree view's axes ++ the new
coordinates — / kept as name N"); the runner maps those to absolute child
positions via its true specs. This is the observability
principle from `model.py` carried to the wire: runner-side semantics are
positional through and through.

**Handles cross as keys.** The outer level reads a handle cell's *key* —
the handle entity id — via :meth:`RunCapabilityProto.read_handle_key`,
which records an ordinary ``cell_value`` (identity) observation. The key
then travels into step args as plain data, and
:meth:`StepCapabilityProto.connect` exchanges it for a live service.
Connect authority is checked against the generation's record — only keys
this generation has read *from a handle grant* may be connected — so
knowledge of an id alone grants nothing, and authority never flows through
data (a handle key stored in a data cell reads back as inert data; tasks
never mint authority — no task-side handle-grid creation or cell writes,
though tasks may *attach* read-only handle views of existing handle grids
through granted subtrees). Consequently the step capability is entirely
calculus-free,
and *no view ever serializes into checkpoint args*: durable records never
contain tokens, making tokens purely ephemeral per-run wire handles with no
cross-run stability requirement.

**Coord grants are wire-inert.** Coordinate views (`views.CoordView`)
follow the same grant model — rule-input coord grants are token-backed, so
forwarding one into a child ``open_task`` is an ordinary :class:`WiredArg`
— but no effectful op accepts their tokens: a bound coord's value rides
the grant (:attr:`ViewGrant.value`, resolved at mint time), a kept coord
is local shadow arithmetic, and reading either is unrecorded-pure.
Locally created coord views have no token and cross open_task wiring as
:class:`WiredCoord`.

**Prefix names on returned grants.** An ``open_*`` returns the child grant
with official stored names for the *new* axes; for the prefix positions
(the subtree view's own axes) the runner also reports official names, and
the task-side adapter overlays whatever local names the holder's subtree
view currently carries — locality of renames is preserved without the
runner ever learning them.

**Error mapping.** Capability calls raise workflow errors runner-side, where
they are persisted at raise time (the poisoning contract, `errors.py` —
``interrupted`` for :class:`~fxtr_experiment.errors.CellFaultError`,
``errored`` otherwise). Across the wire they surface as ``IRException``; the
task-side adapter re-raises the typed workflow error so user code sees the
documented exception types.

Status: the impl side lives in `task_service.py` (adapter, capability
impl, binding registry) and `ops_engine.py` (the record-checked substance).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, NewType, Protocol

from frozendict import frozendict

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef
from fxtr2.services.service_cap_common import FxtrServiceData, UnknownService
from fxtr2.services.typed_service import service_protocol
from mashumaro.types import SerializationStrategy

from fxtr_experiment_task.model import (
    AxisKind,
    AxisSpec,
    CoordValue,
    FrameSchema,
    OutputSpec,
    SliceMode,
)
from fxtr_experiment_task.view_spec import AxisWire, ViewKind

_EMPTY_BOUND: frozendict[int, CoordValue] = frozendict()


class _BoundStrategy(SerializationStrategy):
    """Wire form of ``ViewSel.bound``: sorted ``[position, value]`` pairs.

    Int-keyed mappings are not legal ``FxtrServiceData`` (wire mapping keys
    must be str), so the positional bindings cross as a pair list — found
    the hard way when the typed proxies first carried a real selection.
    """

    def serialize(self, value: "frozendict[int, CoordValue]") -> list[list[object]]:
        """Sorted pairs (canonical order for stable encodings)."""
        return [[k, v] for k, v in sorted(value.items())]

    def deserialize(self, value: object) -> "frozendict[int, CoordValue]":
        """Rebuild the frozendict from pairs."""
        return frozendict({int(k): v for k, v in value})  # type: ignore[union-attr]


ViewToken = NewType("ViewToken", str)
"""Opaque wire identity of one granted view root.

Ephemeral and per-run: tokens are meaningful only against the run capability
that granted them, never enter durable records (steps receive only plain
data, so no view serializes into checkpoint args), and neither derive from
nor reveal storage paths.
"""

FrameWireType = bytes
"""Wire form of one dataframe cell / frame output: arrow IPC **stream** bytes.

Transport only, never identity-bearing (arrow IPC output is not stable
across library versions; fingerprints must not be codec-derived) — identity
is the canonical frame entity (`frame_encoding.py`), minted host-side after
config resolution, and **frame entity ids never cross this wire**.
Deliberately aliased: post-v0 this may become a capability or opaque token
addressing lazy/chunked backing storage rather than one materialized blob.
Treat it as opaque; do not rely on its bytes-ness.
"""


@dataclass(frozen=True)
class ViewGrant:
    """One granted view as pushed across the boundary: token + local shadow.

    The shadow is the complete task-side knowledge about the view: enough to
    validate rewrites, resolve names to positions, and construct the local
    view object — and nothing more (no paths, no prefix literals). Flat with
    optional per-kind fields for now; may become a per-kind discriminated
    union later (wire-compatible either way).

    Attributes:
        token: The opaque wire identity ops refer back to.
        kind: The target's view kind (a stored grid kind, or ``"coord"``
            for coordinate grants — which have no storage target).
        axes: The view's *own* (kept) axes in wire-positional order — only
            the positions the holder can address. Vmap-consumed positions
            are invisible here and everywhere task-side. For rule-input
            grants these carry the official *received* names recorded in
            the rule; for grants returned by opens, the new axes carry
            their official stored names (prefix names are overlaid
            task-side; see module docstring).
        mode: Access mode this grant carries.
        schema: The cell schema, for ``dataframe`` grants (and dataframe
            outputs of ``task`` grants, keyed in ``outputs``). ``None``
            otherwise.
        outputs: For ``task`` grants: the target grid's declared outputs
            (drives task-side ``output()`` selection and no-arg resolution).
            ``None`` otherwise.
        value: For ``coord`` grants whose template is bound (vmap-consumed
            or literal): the resolved coordinate value, computed runner-side
            at grant-mint time — coordinates are structure, so serving the
            value here creates no observation and no wire op exists to read
            it. ``None`` for kept coord grants (the value is the identity
            function, local shadow arithmetic) and all stored-grid grants.
    """

    token: ViewToken
    kind: ViewKind
    axes: tuple[AxisSpec, ...]
    mode: SliceMode = "read"
    schema: FrameSchema | None = None
    outputs: frozendict[str, OutputSpec] | None = None
    value: CoordValue | None = None


@dataclass(frozen=True)
class ViewSel:
    """A wire *selection* through a granted view: which cells an op addresses.

    Purely positional: ``bound`` maps positions in the grant's ``axes`` order
    to constants (the accumulated ``at`` rewrites plus the op's own
    coordinates, resolved from local names task-side). Positions absent from
    ``bound`` remain open — single-cell ops require none open.

    A selection never derives a new view: opening a child of a subtree is an
    effectful op (it can fail; the backend records it) that takes a
    selection on the subtree grant and mints a fresh child grant with its
    own token — a ``ViewSel`` itself only addresses cells of an existing
    grant. (A subtree selection with bound positions is a *sliced read
    view*; write grants are full-frame, so their selections bind nothing.)

    Attributes:
        token: The granted view being addressed.
        bound: Position (index into the grant's ``axes``) → constant.
        output: For task-grant targets: the selected declared output (always
            the concrete name; no-arg selection resolves task-side). ``None``
            otherwise.
    """

    token: ViewToken
    bound: "frozendict[int, CoordValue]" = field(
        default=_EMPTY_BOUND, metadata={"serialization_strategy": _BoundStrategy()}
    )
    output: str | None = None


@dataclass(frozen=True)
class WiredArg:
    """One forwarded view in ``open_task`` wiring: selection, instructions, mode.

    ``wiring`` carries one *positional* instruction per open axis of ``sel``
    (in order): consumed by the p-th holder-visible child coordinate (the
    subtree view's axes ++ the new coordinates), or kept with an
    (identity-bearing) received name. The
    strip-set — matching by local names — is computed entirely holder-side
    (`view_runtime.py`); no other names cross the wire.

    ``mode`` is the *effective* access mode the child's binding receives —
    the forwarded view's mode after any task-side ``read_only`` downgrades
    (the runner validates it does not exceed the grant's mode). Wiring is
    where mode becomes identity-bearing (``SliceBinding.mode``).

    *Received* coord views forward this way too — their token resolves to
    the runner's true coord spec, so translation (batch invariance
    included) works like any other spec and a consumed coord's per-cell
    value never has to be literalized into the wiring. Only *locally
    created* coord views (which have no token) cross as
    :class:`WiredCoord`.
    """

    sel: ViewSel
    wiring: tuple[AxisWire, ...]
    mode: SliceMode = "read"


@dataclass(frozen=True)
class WiredCoord:
    """A locally created coord view in ``open_task`` wiring (token-less).

    Local coord views (`views.int_coord` / `views.str_coord`) were never
    granted, so there is no token to select through; the wire carries the
    full (tiny) state instead: the value kind, and either the ``at()``-bound
    literal or the single open axis's wiring instruction. Mode is not
    carried — coord views are trivially read-only.

    Attributes:
        value_kind: The coordinate's value kind (identity-bearing).
        bound: The bound literal, if the view was ``at()``-bound. Mutually
            exclusive with ``wiring``.
        wiring: The open axis's instruction (exactly one entry) when
            unbound: consumed by the p-th holder-visible child coordinate,
            or kept with a received name — same vocabulary as
            :attr:`WiredArg.wiring`.
    """

    value_kind: AxisKind
    bound: CoordValue | None = None
    wiring: tuple[AxisWire, ...] = ()


@service_protocol
class RunCapabilityProto(Protocol):
    """The per-run effectful-op gateway a task's outer level runs against.

    One impl is mounted per run, in the per-run scope; every effectful view
    operation from `views.py` lowers to exactly one call here. The impl owns
    the record-check (hit serves / miss goes live and records — see
    ``notes/execution_semantics.md``), observation and claim
    recording, demand policy, conflict/regularity checking, and all state-db
    writes. Task-side code never sees storage paths; every method addresses
    grids through granted tokens.

    All methods participate in the poisoning contract: workflow errors are
    persisted at raise time and fail the generation (``interrupted`` for
    cell faults, ``errored`` otherwise) regardless of task-side handling.
    """

    async def read_cell_entity(self, sel: ViewSel) -> FxtrID:
        """Read one entity cell (data grid, or a task grid's entity output).

        Demands the cell if the target is a task output (execution is forced
        per the demand policy). Records a ``cell_value`` observation; the
        returned id is the observation fingerprint, so replay hits serve it
        directly.

        Args:
            sel: Single-cell selection (no open positions; ``output`` set
                iff the target is a task grant).

        Returns:
            The entity id at the cell (loadable from the run scope).

        Raises:
            CellFaultError: Missing / errored / interrupted / stale subject.
            ViewAxisError: If ``sel`` leaves positions open.
        """
        ...

    async def read_cell_frame(self, sel: ViewSel) -> FrameWireType:
        """Read one dataframe cell (or a task grid's frame output).

        Same contract as :meth:`read_cell_entity` (demand policy, one
        ``cell_value`` observation fingerprinted by the canonical frame
        entity id, record hits serve the recorded fingerprint); the payload
        is the cell's wire frame form (:data:`FrameWireType`) in canonical
        row order, served runner-side from the projection, which the task
        side parses straight into a ``polars.DataFrame`` (framework-produced
        bytes are trusted — no task-side re-validation).

        Args:
            sel: Single-cell selection (a dataframe grant, or a task grant
                with a declared ``FrameOutput`` selected).

        Returns:
            The cell's wire frame form.

        Raises:
            CellFaultError: Missing / errored / interrupted / stale subject.
            ViewAxisError: If ``sel`` leaves positions open, or selects an
                entity output.
        """
        ...

    async def read_handle_key(self, sel: ViewSel) -> FxtrID:
        """Read one handle cell's *key* (the handle entity id).

        Records a ``cell_value`` observation of the handle cell — observing
        the handle's *identity*, not its behavior (the external-binding
        trust boundary). The key is plain data: the task may checkpoint it
        into step args, and step code exchanges it for a live service via
        :meth:`StepCapabilityProto.connect`. Connect authority follows the
        record: only keys read *from handle grants* by the current
        generation may be connected — ids read from data cells confer
        nothing.

        Args:
            sel: Single-cell selection on a handle grant.

        Returns:
            The handle entity id at the cell.

        Raises:
            CellFaultError: The handle cell is missing.
            ViewAxisError: Open positions, or not a handle grant.
        """
        ...

    async def write_cell_entity(self, sel: ViewSel, value: FxtrID) -> None:
        """Write one entity cell (idempotent on identical id).

        The value's closure must already have been shipped (it rides this
        call's arena); the runner persists it to the entity store before
        recording. Records a ``wrote_cell`` observation (which doubles as
        the generation's write claim on the value).
        A record-hit with a different payload is ``NondeterminismError``; a
        record-miss against a live cell holding a different id is
        ``WriteConflictError``.

        Args:
            sel: Single-cell selection on a write-mode data grant.
            value: The entity id to store.

        Raises:
            WriteConflictError: Different live value already present.
            NondeterminismError: Different value recorded by this generation.
            ViewAxisError: Open positions, or the grant is not write-mode.
        """
        ...

    async def write_cell_frame(self, sel: ViewSel, frame: FrameWireType) -> None:
        """Write one dataframe cell (idempotent on multiset equality).

        ``frame`` is the wire frame form (the task side serializes the
        ``polars.DataFrame``); the *runner* validates it against the grid's
        declared schema (`tables.validate_and_align`) and canonicalizes to
        the frame entity whose id makes multiset equality an id comparison —
        no identity code runs task-side. Otherwise identical to
        :meth:`write_cell_entity`.

        Args:
            sel: Single-cell selection on a write-mode dataframe grant.
            frame: The wire frame form (the declared schema governs
                validation: names by set, exact dtypes, null legality).

        Raises:
            WriteConflictError: Different live multiset already present.
            NondeterminismError: Different multiset recorded this generation.
            ViewAxisError: Open positions, schema mismatch, or not write-mode.
        """
        ...

    async def gather(self, sel: ViewSel, keys: FrameWireType) -> FrameWireType:
        """Read many frame cells of one grant in one batched, stitched op.

        The wire half of ``DataFrameView.gather`` (semantics + the local
        key-join sugar: `views.py`; design: ``notes/dataframes.md``
        §Gather). ``sel`` may leave positions open; ``keys`` is the
        *deduped key table* — arrow IPC bytes whose columns are named
        **positionally** (``"0"``, ``"1"``, … binding ``sel``'s open
        positions in ascending order; no axis names cross), dtypes exactly
        the axis kinds (str↔String, int↔Int64), no nulls. Each distinct
        key addresses one cell: the record-check / demand policy / faults
        apply **per cell** exactly as for :meth:`read_cell_frame` (one
        ``cell_value`` observation per distinct cell; request edges for
        every miss are committed before any resolution; misses resolve
        concurrently). Duplicate keys are deduped host-side, never
        rejected; an empty key table addresses zero cells (zero
        observations, an empty typed stream); zero open positions address
        the single bound cell.

        Returns:
            One stitched arrow stream: the positional key columns + the
            cells' schema columns, cells concatenated in sorted-coords
            order, each cell's rows in canonical order (input key order is
            deliberately irrelevant). A cell holding a zero-row frame
            contributes zero rows.

        Failure semantics are per request, as if the cells were k parallel
        single reads: each failing cell records its own failure row at
        raise time, classified from its own typed exception (observations
        already recorded stand; a resumed identical gather replays them
        as hits).

        Raises:
            GatherFailedError: One or more addressed cells failed — the
                synthesized signal names every failed cell with its error
                class; the per-cell rows are the durable causes.
                (Deliberately not a ``CellFaultError``, whatever the
                causes' classes — see the type's contract.)
            ViewAxisError: Not a frame target, a malformed key table
                (names/dtypes/nulls), or a read of a grid this generation
                wrote (op-level failures, recorded as the op's own row).
        """
        ...

    async def open_data(self, subtree: ViewSel, key: str, axes: tuple[AxisSpec, ...]) -> ViewGrant:
        """Open (create-or-attach) the data grid at ``key`` under a subtree grant.

        Opens carry no mode: the grant follows the chain's authority —
        write under a write-authority grant, read-only under a read grant
        or a sliced window (which cannot create; a missing grid there is
        an attach failure). The selection addresses the subtree cells
        opened across. Idempotent on canonical config; records a
        ``grid_config`` observation, plus a would-create claim when the
        open ran through write authority (grounding's ``asserted_grid`` —
        hit or miss alike). A
        record-hit with a different config is ``NondeterminismError``; a
        live mismatch is ``GridConfigMismatchError``.

        Args:
            subtree: Selection on the subtree (or internals) grant to open
                under.
            key: The child key.
            axes: The child's own axes (official stored names).

        Returns:
            The child grant (axes = subtree's axes + ``axes``), carrying
            the chain's authority.

        Raises:
            GridConfigMismatchError: Key exists with a different config.
            NondeterminismError: Config differs from this generation's record.
            ViewAxisError: If the grant cannot open children, or creation
                is needed without create authority.
        """
        ...

    async def open_dataframe(
        self,
        subtree: ViewSel,
        key: str,
        axes: tuple[AxisSpec, ...],
        schema: FrameSchema,
    ) -> ViewGrant:
        """Open the dataframe grid at ``key``. See :meth:`open_data`.

        Args:
            subtree: The subtree (or internals) grant to open under.
            key: The child key.
            axes: The child's own axes (official stored names).
            schema: The declared cell schema (part of the canonical config).

        Returns:
            The child grant.
        """
        ...

    async def open_handle(
        self, subtree: ViewSel, key: str, axes: tuple[AxisSpec, ...]
    ) -> ViewGrant:
        """Attach to the existing handle grid at ``key`` (read-only).

        Attach-only: tasks never mint authority, so handle grids are created
        and written only at the top level — subtree write access does *not*
        confer handle-grid creation (the one carve-out from write-implies-
        create). A missing key is a not-ready dependency. Records a
        ``grid_config`` observation on success.

        Args:
            subtree: The subtree (or internals) grant to open under.
            key: The child key.
            axes: The handle grid's own axes (official stored names).

        Returns:
            The read-mode child grant.

        Raises:
            CellFaultError: ``key`` does not exist (→ interrupted; create it
                at the top level and re-demand).
            GridConfigMismatchError: ``key`` exists with a different config.
        """
        ...

    async def open_subtree(
        self, subtree: ViewSel, key: str, axes: tuple[AxisSpec, ...]
    ) -> ViewGrant:
        """Open the nested subtree grid at ``key``. See :meth:`open_data`.

        Args:
            subtree: The subtree (or internals) grant to open under.
            key: The child key.
            axes: The child subtree's own axes (official stored names).

        Returns:
            The child grant, inheriting the opening grant's mode: a
            creation site under a write grant, a read-only browse one
            level deeper under a read grant (child mode never exceeds
            the grant's).
        """
        ...

    async def open_checkpoint(
        self, subtree: ViewSel, key: str, axes: tuple[AxisSpec, ...]
    ) -> ViewGrant:
        """Open the checkpoint grid at ``key`` (internals grants only).

        The grid is bound to the calling task cell at creation; only its
        owner ever reaches it (checkpoint grants cannot be re-granted into
        child rules, enforced at ``open_task``).

        Args:
            subtree: An internals-derived subtree grant.
            key: The child key.
            axes: The checkpoint grid's own axes (official stored names).

        Returns:
            The child grant.

        Raises:
            GridConfigMismatchError: Key exists with a different config, or
                the grant is not within the caller's own internals.
        """
        ...

    async def open_task(
        self,
        subtree: ViewSel,
        key: str,
        axes: tuple[AxisSpec, ...],
        task_fn: WiredArg,
        args: Mapping[str, WiredArg | WiredCoord],
        outputs: frozendict[str, OutputSpec],
    ) -> ViewGrant:
        """Open the task grid at ``key``, canonicalizing the wiring.

        The runner rebuilds each arg's holder-frame spec from its grant +
        selection (or, for a locally created coord arg, directly from its
        :class:`WiredCoord` state) and feeds it, with the arg's positional
        wiring instructions, to the canonicalization of `view_spec.py`
        (batch-invariance rejection; received-name promotion) to produce
        and store the key-level config. Name resolution already happened
        holder-side.

        Args:
            subtree: The subtree grant to create into (creation site — must
                be full-frame, which grants are by construction).
            key: The child key.
            axes: The task grid's own axes (official stored names).
            task_fn: Handle selection that must resolve to exactly one fn
                cell per task cell after strip-consumption.
            args: Named wired args (selection + local names for open
                positions; see :class:`WiredArg` / :class:`WiredCoord`).
            outputs: The declared named outputs (task-side default already
                applied; always explicit on the wire).

        Returns:
            The task grant (axes = subtree's axes + the new own axes).

        Raises:
            GridConfigMismatchError: Key exists with a different canonical
                config (incl. received-name disagreements).
            NondeterminismError: Config differs from this generation's record.
            ViewAxisError: Name collisions, unconsumed task_fn axes, a
                checkpoint grant among ``args``, or an arg bound to an
                unmapped holder axis (batch-invariance rejection).
        """
        ...

    async def run_step(
        self,
        checkpoint: ViewSel,
        step: ServiceRef[UnknownService],
        args: Mapping[str, FxtrServiceData],
    ) -> FxtrServiceData:
        """Run a step durably into one checkpoint cell.

        Record-check on the cell: a hit whose recorded invocation — the
        step's *service type id* plus ``args`` — matches returns the stored
        result without invoking anything; a mismatched hit is
        ``NondeterminismError``. On a miss the runner opens a per-step
        scope, mounts a :class:`StepCapabilityProto`, invokes
        ``step.run(step_capability, **args)``, persists the result (entity
        closures ride the return arena), writes the checkpoint entry, and
        serves the result. ``args`` are plain ``FxtrServiceData`` — views
        never appear here; handles cross as keys previously read via
        :meth:`read_handle_key`.

        Args:
            checkpoint: Single-cell selection on a checkpoint grant.
            step: The step service. Its service type id is the recorded
                identity (registrants control it, and can point it at an
                entity encoding the step's name and other identity info).
            args: Named step arguments, recorded in the invocation and
                delivered as the step's kwargs.

        Returns:
            The step's (possibly replayed) result.

        Raises:
            CheckpointCorruptionError: Entry bound to a foreign generation.
            NondeterminismError: Recorded invocation differs.
            ViewAxisError: If ``checkpoint`` leaves positions open.
        """
        ...

    async def log(self, message: str) -> None:
        """Emit a human-facing outer-level progress/log line (not
        load-bearing; the task-side ``TaskContext.log`` gateway).

        Appends to the cell's runtime-only run log with no step
        identity — the outer-level counterpart to
        :meth:`StepCapabilityProto.log`. Records nothing and is not an
        effectful op, so it is permitted (and silent) during the safety
        pass and record-hit replays: the frozen engine's discarded sink
        makes replay produce no log effect.
        """
        ...


@service_protocol
class StepCapabilityProto(Protocol):
    """The per-step gateway — the only path to nondeterministic resources.

    Mounted in the per-step scope for exactly one step invocation; everything
    minted through it dies with the step. Steps have no access to
    :class:`RunCapabilityProto` and no view-calculus vocabulary at all — they
    consume plain data (the outer/inner split is structural).
    """

    async def connect(self, handle: FxtrID) -> ServiceRef[UnknownService]:
        """Exchange a previously-read handle key for a live service.

        Resolves ``handle`` (a handle entity id, typically received through
        the step's checkpointed args) into a running service via the
        runner's handle bindings. Records no observation — the observation
        happened when the outer level read the key
        (:meth:`RunCapabilityProto.read_handle_key`); authority is checked
        against that record, so only keys the current generation has read
        may be connected. The returned ref lives at most as long as the
        step invocation (per-step scope).

        Args:
            handle: The handle entity id to connect to.

        Returns:
            A service ref for the connected service; retype with
            ``replace_service_type`` before use.

        Raises:
            HandleUnboundError: No live binding exists for the key in this
                runner process — a not-ready dependency. Bindings are
                untracked runner config, so a result computed around a
                missing one could never be invalidated later; hence this
                poisons the *step invocation* (its checkpoint entry is not
                written, even if step code catches the error) and interrupts
                the task. Bind the handle and re-demand.
            WorkflowError: The key was not read from a handle grant by the
                current generation (authority follows the record; ids read
                from data cells confer nothing) — an integrity violation
                (``errored``).
        """
        ...

    async def log(self, message: str) -> None:
        """Emit a human-facing progress line (not load-bearing).

        Lands in the cell's runtime-only run log (`run_log.py`) — stamped
        with this step invocation's identity, streamed to live cell
        watches, never persisted. Permitted during replay with no effect
        difference (record hits never invoke step fns; the safety pass
        writes to a discarded sink).

        Args:
            message: The line to log.
        """
        ...

    async def open_channel(self) -> ServiceRef[UnknownService]:
        """Open a structured output channel for this step.

        Tracks the requirement (fxtr2 output-channel tree); plumbing is
        deferred — :meth:`log` is the first implemented path.

        Returns:
            A ref to a channel-writer service scoped to (at most) the step
            invocation; retype to ``FxtrChannelWriter`` before use. (A bare
            protocol return is not wire-encodable — services return refs.)
        """
        ...


@service_protocol
class WorkflowTaskFnProto(Protocol):
    """The service contract a task-fn handle resolves to.

    This is what the *runner* invokes — user code implements
    :class:`~fxtr_experiment.task_api.TaskFn` and the task-side adapter
    wraps it into this protocol (building views from grants, constructing
    the context, normalizing the return). The handle entity's identity is
    the code identity that staleness and migrations track (``executed_fn`` /
    ``current_fn``).
    """

    async def run_task(
        self,
        capability: ServiceRef[UnknownService],
        args: Mapping[str, ViewGrant],
        internals: ViewGrant,
        outputs: frozendict[str, OutputSpec],
    ) -> Mapping[str, FxtrID | FrameWireType]:
        """Execute one run of one task cell.

        Args:
            capability: The run capability (a :class:`RunCapabilityProto`
                service ref; typed as unknown on the wire, retyped by the
                adapter).
            args: One grant per rule input, keyed by the rule's input names
                (delivered to the user fn as keyword args).
            internals: The zero-axis subtree grant for this cell's private
                internals (backs ``ctx.own_internals()``).
            outputs: The declared outputs (lets the adapter normalize a
                bare single-output return and encode frame (``polars.DataFrame``)
                outputs against their declared schemas).

        Returns:
            One value per declared output: the entity id for
            ``EntityOutput`` slots, the wire frame form
            (:data:`FrameWireType`) for ``FrameOutput`` slots (the adapter
            encodes frames). Closures ride the return arena; the runner
            persists them before recording results.
        """
        ...


@service_protocol
class WorkflowStepFnProto(Protocol):
    """The service contract a step crosses the boundary as.

    This is what the *runner's* ``run_step`` miss path invokes: user code
    implements :class:`~fxtr_experiment.task_api.StepFn` and the task-side
    adapter wraps it into this protocol (or the step is a real remote
    service implementing it directly). The step's *service type id* is its
    recorded identity — the replay-compared half of the checkpoint
    invocation beside the args fingerprint.
    """

    async def run_step(
        self, capability: ServiceRef[UnknownService], args: Mapping[str, FxtrServiceData]
    ) -> FxtrServiceData:
        """Execute one step invocation.

        Args:
            capability: The per-invocation step capability
                (:class:`StepCapabilityProto`; retype before resolving) —
                the only path to nondeterministic resources, alive at most
                as long as this invocation.
            args: The checkpoint-recorded arguments (durable plain data;
                see `encoding`'s step-payload contract).

        Returns:
            The step result — durable plain data, recorded in the
            checkpoint entry (``ServiceRef``\\ s are rejected).
        """
        ...
