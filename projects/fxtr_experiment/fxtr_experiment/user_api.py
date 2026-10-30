"""Top-level (user-facing) service contracts: driving a workflow from outside.

The **user** is a holder *outside the deterministic world*: they manipulate
grids through the same view calculus as tasks — same wire model
(`runner_api.py`'s :class:`~fxtr_experiment.runner_api.ViewGrant` /
:class:`~fxtr_experiment.runner_api.ViewSel`), same open/read/write
vocabulary — but none of the execution machinery applies. User ops have no
op record and no record-check, are never replayed, record no observations
(the user sits outside the currency model entirely), and do not participate
in the poisoning contract: failures surface as ordinary exceptions to
handle interactively.
What user ops leave behind is *sourcing* (writes/creates auto-upsert
revocable sourcing records; the explicit CRUD below maintains them —
``notes/grounding.md``'s editable root facts) and *targets* (below).
**User demands require targets**: the method called declares the intent,
so a task-output read here needs an attached target on the cell —
current state never substitutes for a declaration (the `Workflow` demand
wrapper auto-adds by default; introspection is how existing values are
looked at). User actions may additionally be
written to a passive event log — provenance of "who did what, when", never
links.

**The user's frame is the root frame.** The root namespace is a zero-axis
subtree grid; the user's one starting grant is a write-mode subtree grant on
it (:meth:`UserCapabilityProto.root`). Because the user's flattened frame is
empty, their local names *are* the official stored names — and, unlike
tasks, introspection results may show absolute paths and coordinates
(subtree transparency is a discipline for task code, not a secret the
system keeps from its owner).

**Targets: durable declarative intent.** A target marks a slice as a root of
the DAG the user wants. It is part of exportable *state* (version-log
commits will capture targets; a recipient learns the experiment's intent
and can keep it while clearing data or changing inputs), fully reversible,
with no side effects on data beyond deterministic derived-flag propagation.
Mechanically a target is shaped like a task's ``grid_config`` observation
*minus any observed values*: it pins the lookup key — the wanted grid's
path and expected canonical config — plus the wanted slice
(:class:`~fxtr_experiment.model.TargetRecord`). A target whose grid is
missing is *dangling*; one whose key now holds an incompatible config is
*still dangling* (the expected-config fingerprint does not match). Dangling
targets are preserved — they are intent, not data — surfaced as incoherent
in the UI, and never silently rebound or dropped. Targets are **fully
bound** (one cell per record; open-slice targets would require enumerating
cells, which the open-map principle forbids — deferred until domain-cell-
driven enumeration exists) and **unique per slice identity** (re-targeting
across config drift means removing the stale record first).

**Materialization is an event, not state.** Reading through this capability
follows the demand policy exactly like a task read (execute if absent,
await if running, resume if interrupted, retry-or-fault if errored, fault
if stale or unsettled) — but it is an ephemeral runner event: not durable,
not exported,
at most event-logged. Targets and materialization are decoupled on the wire;
the convenience coupling ("await this and also target it") is wrapper-level
sugar, **default off** — like ``nix build`` not editing your flake. "Run
the workflow" (demand every currently-targeted slice) is likewise a wrapper
affordance over these primitives, not core surface.

**Privileged surface.** Beyond the shared calculus ops: ``overwrite=`` on
writes (replace an existing different value — direct observers go stale and
their dependents unsettled, which is the machinery working as intended),
:meth:`~UserCapabilityProto.clear_cells`
(drop task-cell generation state — the demand policy's "user explicitly
clears" flow), :meth:`~UserCapabilityProto.delete_grid` (full recursive
removal — the escape hatch for conflicts and breaking task-code changes),
the presence queries (:meth:`~UserCapabilityProto.list_keys` /
:meth:`~UserCapabilityProto.list_cells`), and
:meth:`~UserCapabilityProto.inspect_cell` (a cell's content *with* its
verdict, one snapshot — the sanctioned view of stale/unsettled results).
Presence queries and inspection are pure
introspection — they record nothing and nothing downstream can depend on
them, so the open-map principle stays intact where it matters (tasks); note
presence queries list what is *present*, never "bounds" (grids remain
unbounded), and carry no values. Task internals are *browsable*
(:meth:`~UserCapabilityProto.attach_internals` — read-only, per subtree
transparency) but never writable from here, and there
is no ``open_checkpoint`` and no ``run_step`` — checkpoints belong to
task code.

**Registration is on the wire too.** :meth:`~UserCapabilityProto.
bind_handle` registers a live connector service for a handle entity (task
fns included — a task-fn binding's connector produces the
:class:`~fxtr_experiment.runner_api.WorkflowTaskFnProto` service). fxtr2
deliberately blurs in-process vs remote — a workflow service may live on a
cluster and be driven from a notebook — and connectors are ServiceRefs,
which cross RPC like anything else. Bindings remain *ephemeral,
non-durable* state, but ephemerality is about durability, not location:
a binding's lifetime is bounded by the connector service's lifetime (fxtr2
scopes/leases — a dropped or revoked connector unbinds naturally).

**There is exactly one user capability: this protocol.** Anything a user
can do, they can do through a resolved
``ServiceRef[UserCapabilityProto]``. The :class:`Workflow` wrapper below is
unprivileged glue — convenience, never capability.

The protocols here are decorated ``@service_protocol``\\ s (the live wire
contracts); the implementation and the :class:`~fxtr_experiment.user_service.
Workflow` convenience wrapper live in `user_service.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Protocol

from frozendict import frozendict

from fxtr_sandbox_types import JSONValue

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef
from fxtr2.services.service_cap_common import FxtrServiceData, UnknownService
from fxtr2.services.subscription_types import Subscriber, UnsubscribeHandle
from fxtr2.services.typed_service import service_protocol

from fxtr_experiment.export import ExportReport
from fxtr_experiment.grounding import GroundingVerdict
from fxtr_experiment.importer import ImportReport
from fxtr_experiment.introspect import CurrentMatch, GraphEdgeKind
from fxtr_experiment.lint import FactKind
from fxtr_experiment_task.model import (
    AxisSpec,
    CoordValue,
    Currency,
    ExecStatus,
    FailureKind,
    FrameSchema,
    ObservationKind,
    OutputSpec,
    RequestKind,
    SandboxDefn,
    SliceMode,
    TargetRecord,
)
from fxtr_experiment_task.runner_api import (
    FrameWireType,
    ViewGrant,
    ViewSel,
    ViewToken,
    WiredArg,
    WiredCoord,
)
from fxtr_experiment_task.view_spec import GridKind


@dataclass(frozen=True)
class KeyInfo:
    """One child key of a subtree, as returned by ``list_keys``.

    Keys (and their configs) are subtree-level state shared across all cells
    of the subtree (the regularity invariant), so listing them takes a
    subtree token, not a cell.

    Attributes:
        key: The child key.
        kind: The child grid's kind.
        axes: The child grid's own axes (official stored names).
        grounding: The grid's grounding verdict (``notes/grounding.md``).
        grounding_reason: Its diagnosis; ``None`` iff grounded.
        config_id: The grid's canonical config entity (re-attached, so
            the return covers it) — dispatch-renderable in the UI (task
            rules and wiring, dataframe schemas).
    """

    key: str
    kind: GridKind
    axes: tuple[AxisSpec, ...]
    grounding: GroundingVerdict | None = None
    grounding_reason: str | None = None
    config_id: FxtrID | None = None


@dataclass(frozen=True)
class CellInfo:
    """One nonempty cell, as returned by ``list_cells``.

    Deliberately carries **no values**: presence queries tell you what
    exists, reads tell you what it is. A bulk listing that served values
    would smuggle unlabeled data past the value-pinned demand gate;
    retrieval is always an explicit read/demand — or, for non-fresh
    state, :meth:`UserCapabilityProto.inspect_cell`, which serves the
    value *with* its verdict pinned to one snapshot.

    Attributes:
        coords: The cell's coordinates over the queried selection's *open*
            positions, in positional order.
        status: Execution status, for task cells (``None`` otherwise).
        currency: Derived currency, for task cells (``None`` otherwise).
        reason: Failure summary (from the generation's failure records), if
            errored or interrupted.
        grounding: The fact's grounding verdict (task cells: the current
            generation's; data/handle cells: the value's).
        wanted: The cell's ``wanted`` verdict (task cells only).
    """

    coords: tuple[CoordValue, ...]
    status: ExecStatus | None = None
    currency: Currency | None = None
    reason: str | None = None
    grounding: GroundingVerdict | None = None
    wanted: bool | None = None


@service_protocol
class BindingReleaseProto(Protocol):
    """Release capability returned by ``bind_handle``.

    Follows the workstation's ``ReleaseCallbackProto`` convention
    (`fxtr_workstation.protocols`): the caller keeps the chain-bridged ref
    alive while it wants the binding to remain in effect; dropping the ref
    auto-releases via the dispatch scope's end callback; calling
    :meth:`release` is the explicit, immediate, graceful form. Both paths
    are idempotent.
    """

    async def release(self) -> None:
        """Unbind the handle (idempotent)."""
        ...


@dataclass(frozen=True)
class CellReport:
    """One cell's inspection snapshot: current content *with* its verdict.

    The introspection counterpart of a read (see
    :meth:`UserCapabilityProto.inspect_cell`): where reads serve only
    checked-fresh values (the demand gate), inspection serves whatever is
    there — honestly labeled. Content and verdict come from one session
    snapshot (the currency evaluation's value-pinning), so the pair is a
    coherent fact and nothing is ever observable *as if fresh* without
    the gate.

    Attributes:
        status: Execution status (task cells with a generation; ``None``
            otherwise).
        currency: The derived tri-state verdict (ditto).
        reason: The verdict's diagnosis when not fresh; the failure summary
            when errored/interrupted.
        results: A complete generation's per-output results — whatever its
            currency (that is the point); ``None`` for incomplete
            generations and non-task cells.
        value: The value id, for data/handle cells; ``None`` for task cells
            (use ``results``).
        grounding: The fact's grounding verdict, from the *same* session
            snapshot as the content and currency (the doctrine's coherent
            triple).
        grounding_reason: Its diagnosis; ``None`` iff grounded.
        wanted: The cell's ``wanted`` verdict (task cells only).
        wanted_reason: Its diagnosis; ``None`` iff wanted.
        bound: For populated handle cells: whether a live binding exists
            for the key (ephemeral runner state, deliberately beside the
            snapshot's durable facts); ``None`` otherwise.
    """

    status: ExecStatus | None = None
    currency: Currency | None = None
    reason: str | None = None
    results: "frozendict[str, FxtrID] | None" = None
    value: FxtrID | None = None
    grounding: GroundingVerdict | None = None
    grounding_reason: str | None = None
    wanted: bool | None = None
    wanted_reason: str | None = None
    bound: bool | None = None


@dataclass(frozen=True)
class GridSummary:
    """One grid's dashboard summary, as returned by ``summarize``.

    Locations are absolute (root-frame transparency); counts are the
    tree's at-a-glance rollups. Population counts by kind: task grids
    count generations (a cell with no generation is no fact — the
    open-map principle), checkpoint grids count entries, leaf grids
    count populated cells, and subtree grids report zero (their cells
    materialize implicitly through descendants; aggregate rollups up the
    tree client-side).

    Attributes:
        path: The grid's key-path text.
        kind: The grid's kind.
        axes: The grid's own axes (official stored names).
        config_id: The canonical config entity (re-attached).
        grounding: The grid's grounding verdict.
        grounding_reason: Its diagnosis; ``None`` iff grounded.
        sourcing: The grid-level sourcing record's attachment status;
            ``None`` when no record exists.
        populated: Total populated cells / generations / entries.
        n_pending: Generations by execution status (task grids; zero
            elsewhere).
        n_running: Ditto.
        n_complete: Ditto.
        n_errored: Ditto.
        n_interrupted: Ditto.
        n_stale: Task cells whose current generation is stale.
        n_unsettled: Ditto, unsettled.
        targets_attached: Target records pinning this grid's current
            config.
        targets_dangling: Target records on this path pinning a different
            config (records for *missing* grids appear only in
            ``list_targets`` / ``lint`` — the summary enumerates grids
            that exist).
    """

    path: str
    kind: GridKind
    axes: tuple[AxisSpec, ...]
    config_id: FxtrID
    grounding: GroundingVerdict | None
    grounding_reason: str | None
    sourcing: SourcingStatus | None
    populated: int
    n_pending: int
    n_running: int
    n_complete: int
    n_errored: int
    n_interrupted: int
    n_stale: int
    n_unsettled: int
    targets_attached: int
    targets_dangling: int


@dataclass(frozen=True)
class WorkflowSummary:
    """The whole-namespace summary, one session snapshot (see
    :meth:`UserCapabilityProto.summarize`).

    Attributes:
        grids: One :class:`GridSummary` per existing grid, in key-path
            order (the UI folds these into the tree).
    """

    grids: tuple[GridSummary, ...]


@dataclass(frozen=True)
class ObservationInfo:
    """One recorded observation edge with its direct current-match check
    (part of :class:`GenerationReport` — the "which input moved" view).

    ``current`` is the itemized form of currency's *direct* staleness
    check — recorded fingerprint vs the subject's current settled value,
    no recursion: ``match`` / ``changed`` (the stale cause) / ``missing``
    (deleted cell/grid, or a task output with no complete generation).
    Support-transitive judgement stays with the currency verdict.

    Attributes:
        subject_path: The observed grid's key-path text.
        subject_coords: The observed cell's flattened-coords text; ``''``
            when the subject is the grid itself (``grid_config``).
        subject_output: The observed task-output name; ``''`` otherwise.
        kind: The relation (read / write / config).
        fingerprint: The recorded value id (re-attached).
        current: The direct check.
    """

    subject_path: str
    subject_coords: str
    subject_output: str
    kind: ObservationKind
    fingerprint: FxtrID
    current: CurrentMatch


@dataclass(frozen=True)
class FailureInfo:
    """One failure record of a generation (raise-side; plural per
    generation — concurrent branches may each fault or error).

    Attributes:
        kind: The errored/interrupted split (``error`` / ``fault``).
        error_class: The raised exception's class name (display only).
        message: The failure message.
        ts: Wall-clock timestamp (ISO).
    """

    kind: FailureKind
    error_class: str
    message: str
    ts: str


@dataclass(frozen=True)
class CheckpointInfo:
    """One recorded step entry of a generation (entries exist only for
    completed step invocations — an in-flight step has no entry).

    Attributes:
        grid_path: The checkpoint grid's key-path text.
        coords: The entry's flattened-coords text.
        step_type_id: The recorded step-fn identity (re-attached).
        args_id: The entity-encoded invocation payload (re-attached).
        value_id: The recorded result (re-attached).
    """

    grid_path: str
    coords: str
    step_type_id: FxtrID
    args_id: FxtrID
    value_id: FxtrID


@dataclass(frozen=True)
class RequestInfo:
    """One asked-but-unanswered request edge — for interrupted
    generations, what the run is blocked on (asked at commitment; no
    observation resolved it).

    Attributes:
        grid_path: The requested grid's key-path text.
        coords: The requested cell's flattened-coords text.
        kind: The subject's output-ness.
    """

    grid_path: str
    coords: str
    kind: RequestKind


@dataclass(frozen=True)
class GenerationReport:
    """One task cell's current generation, itemized — the dashboard's
    debug view (see :meth:`UserCapabilityProto.inspect_generation`).

    All fields default to the empty form: a cell with no current
    generation reports ``generation is None`` (the :class:`CellReport`
    precedent — inspection channels never fault on emptiness).

    Attributes:
        generation: The public generation id; ``None`` when the cell has
            no current generation.
        status: Its execution status.
        executed_fn: The task-fn handle entity that ran (re-attached).
        failures: The failure records, insertion order.
        observations: The op record with per-edge current-match flags.
        unanswered_requests: Request edges no observation resolved.
        checkpoints: The recorded step entries.
    """

    generation: str | None = None
    status: ExecStatus | None = None
    executed_fn: FxtrID | None = None
    failures: tuple[FailureInfo, ...] = ()
    observations: tuple[ObservationInfo, ...] = ()
    unanswered_requests: tuple[RequestInfo, ...] = ()
    checkpoints: tuple[CheckpointInfo, ...] = ()


@dataclass(frozen=True)
class ObserverInfo:
    """One observer cell of a subject, as returned by ``list_observers``
    (the reverse edges: who read/wrote/configured against this fact).

    Attributes:
        grid_path: The observer task grid's key-path text (absolute —
            root-frame transparency).
        coords: The observer cell's flattened-coords text.
        status: Its current generation's execution status.
        currency: Its derived currency, same snapshot.
    """

    grid_path: str
    coords: str
    status: ExecStatus
    currency: Currency | None


TargetStatus = Literal["attached", "dangling_missing", "dangling_mismatched"]
"""Attachment state of a target: its grid exists with the expected config
(``attached``), is absent (``dangling_missing``), or exists with a different
canonical config (``dangling_mismatched``). Dangling targets are preserved
and surfaced as incoherent — never silently rebound or dropped."""


@dataclass(frozen=True)
class TargetInfo:
    """One target with its current attachment status.

    ``grid_grounding`` (the wanted grid's verdict, ``None`` for dangling
    targets) is what distinguishes attached-but-unanchored intent from
    healthy roots.

    Attributes:
        target: The record (usable with ``demand_target`` /
            ``remove_target``).
        status: Attachment state (see :data:`TargetStatus`).
        grid_path: The wanted grid's key-path text — absolute per
            root-frame transparency, resolvable whether or not the grid
            currently exists (targets are fully bound, so the location
            is a property of the record itself).
        coords: The wanted cell's flattened-coords text.
        grid_grounding: The wanted grid's verdict; ``None`` for
            dangling targets.
    """

    target: TargetRecord
    status: TargetStatus
    grid_path: str
    coords: str
    grid_grounding: GroundingVerdict | None = None


SourcingStatus = Literal["attached", "dangling_missing", "dangling_mismatched"]
"""Attachment state of a sourcing record, mirroring targets: the pinned
fingerprint matches the live config/value (``attached``), the grid/cell is
absent (``dangling_missing``), or holds a different config/value
(``dangling_mismatched``). Dangling records are preserved and surfaced —
intent is never silently rebound or dropped."""


@dataclass(frozen=True)
class SourcingInfo:
    """One user sourcing record with its current attachment status.

    Locations are absolute (path text + flattened coords) per root-frame
    transparency — introspection results may show official addresses.

    Attributes:
        grid_path: The subject grid's key-path text.
        coords: The subject cell's flattened-coords text; ``''`` for the
            grid-level record.
        fingerprint: The pinned config id (grid rows) / value id (cell
            rows); re-attached, so the return covers it.
        status: Attachment state (see :data:`SourcingStatus`).
    """

    grid_path: str
    coords: str
    fingerprint: FxtrID
    status: SourcingStatus


@dataclass(frozen=True)
class CellLocation:
    """An absolute cell address (root-frame transparency).

    Attributes:
        grid_path: The grid's key-path text.
        coords: The cell's flattened-coords text.
    """

    grid_path: str
    coords: str


BindingState = Literal[
    "external",
    "sandbox_launchable",
    "sandbox_repo_missing",
    "sandbox_launching",
    "sandbox_running",
    "sandbox_faulted",
]
"""A binding's class + launch state in one enum (the class discriminator
is the value's prefix). ``external`` = an ephemeral connector binding (a
live connector; nothing more to say). The ``sandbox_*`` values follow the
sandboxed class's runtime launch state (runtime-only — never persisted;
`bindings.BindingHost`): ``sandbox_repo_missing`` = no repo configured on
the runner, so nothing bound this way can launch; ``sandbox_launchable``
= repo configured, no cached process, no known runner-level blocker (the
flake / commit / dependency pins are verified at connect);
``sandbox_launching`` = a launch is in flight (concurrent demands share
it); ``sandbox_running`` = a cached process is up (connect-per-use);
``sandbox_faulted`` = the last launch attempt failed and nothing is in
flight (the failure message rides ``SandboxBindingInfo.fault_message``;
the next demand re-attempts, ``shutdown_sandbox`` resets to
launchable)."""

PinStatus = Literal["attached", "mismatched", "missing"]
"""One binding dependency's pin vs the live cell: the cell still holds the
pinned key (``attached``), holds a different key now (``mismatched``), or
is empty / its grid is gone (``missing``). Derived at read time (like a
sourcing record's dangling status); a non-``attached`` pin faults the next
connect, but is purely informational here."""


@dataclass(frozen=True)
class SandboxDepInfo:
    """One pinned dependency of a binding, with its live status.

    Attributes:
        name: The dependency name the bound implementation receives.
        grid_path / coords: The pinned handle cell's absolute location.
        pinned_key_id: The handle key pinned at bind time (re-attached).
        pin_status: The pin's live status (see :data:`PinStatus`).
    """

    name: str
    grid_path: str
    coords: str
    pinned_key_id: FxtrID
    pin_status: PinStatus


@dataclass(frozen=True)
class SandboxBindingInfo:
    """The sandboxed-class detail carried by a ``sandbox_*`` binding row.

    Attributes:
        defn: The nix flake reference triple.
        fault_message: The last launch attempt's full failure message —
            set exactly when the row's state is ``sandbox_faulted``
            (runtime evidence; cleared by the next attempt or a
            ``shutdown_sandbox``).
    """

    defn: SandboxDefn
    fault_message: str | None = None


@dataclass(frozen=True)
class BindingInfo:
    """One handle binding (either class) with the cells currently holding
    its key.

    Attributes:
        key: The bound handle entity id (re-attached).
        state: The binding's class + launch state (see
            :data:`BindingState`).
        cells: Every handle cell whose current value is ``key``, from
            the same snapshot — possibly empty (bindings are runner
            config keyed by the handle entity, so they survive cell
            deletion and can precede any write).
        deps: The pinned dependencies with their live pin status,
            ``name``-sorted (deps are an unordered map). Both classes
            carry these — the two are uniform in everything except
            durability and lifecycle.
        config: The binding's JSON config (plain JSON; ``None`` if
            unset). Both classes.
        sandbox: The sandboxed-class detail for a ``sandbox_*`` binding;
            ``None`` for an ``external`` (ephemeral) binding.
    """

    key: FxtrID
    state: BindingState
    cells: tuple[CellLocation, ...]
    deps: tuple[SandboxDepInfo, ...] = ()
    config: JSONValue = None
    sandbox: SandboxBindingInfo | None = None


# -- the grid graph (the plate view's relational overlay) ------------------------


@dataclass(frozen=True)
class GraphEdge:
    """One grid-pair edge of the graph overlay, aggregated over current
    generations (old generations' records cascade at turnover, and the
    aggregation joins through the current-generation table, so this is
    current-only by construction).

    Attributes:
        observer_path: The task grid whose generations produced the record.
        subject_path: The read / written / asserted grid.
        kind: The relationship (see :data:`GraphEdgeKind`).
        count: How many cell-level records collapsed into this grid pair
            (display density hint; the semantic is "some cell").
    """

    observer_path: str
    subject_path: str
    kind: GraphEdgeKind
    count: int


@dataclass(frozen=True)
class InputGrantEdge:
    """One static rule-wiring edge: a task grid is granted a sliced view of
    a target grid (a capability, held whether or not it is ever observed —
    holding ≠ observing). Decoded from the task grid's stored
    `~fxtr_experiment.model.TaskRule` (the ``task_fn`` slice plus each
    `~fxtr_experiment.model.SliceBinding` input); coordinate inputs have no
    storage target and produce no edge.

    Attributes:
        task_path: The task grid holding the grant.
        target_path: The granted grid.
        arg_name: The rule input name; ``''`` for the ``task_fn`` slice.
        is_task_fn: Whether this is the ``task_fn`` slice (vs a named input).
        mode: The granted access mode (`~fxtr_experiment.model.SliceMode`).
        kept: Whether the slice keeps any axis open (a `KeepAxis` in the
            template) — the "reads batched over this axis" hint, as opposed
            to a vmap-consumed (`Param`) binding.
    """

    task_path: str
    target_path: str
    arg_name: str
    is_task_fn: bool
    mode: SliceMode
    kept: bool


@dataclass(frozen=True)
class AxisCorrespondence:
    """One provable 1:1 axis identity, from a `~fxtr_experiment.model.Param`
    (vmap-consumed) binding in a task rule: the target's axis is bound
    positionally to a position of the consuming grid's flattened frame, so
    the two axes index the same replication (`notes/plate_view.md`,
    grant-matching). Both sides are resolved to the grid that *owns* the
    flattened position — which may be an enclosing subtree, not the task
    itself (prefix-position bindings arise through canonicalization).

    The relation is symmetric; the renderer takes the transitive closure to
    form grant-classes. The pair is emitted in a canonical order (sorted by
    ``(grid, axis)``) so the overlay diffs stably.

    Attributes:
        grid_a: One side's owning grid key-path.
        axis_a: That grid's own-axis name at the corresponding position.
        grid_b: The other side's owning grid key-path.
        axis_b: That grid's own-axis name.
    """

    grid_a: str
    axis_a: str
    grid_b: str
    axis_b: str


@dataclass(frozen=True)
class GridGraph:
    """The plate view's relational overlay, one session snapshot (see
    :meth:`UserCapabilityProto.grid_graph`).

    Deliberately **entity-id-free** — every field is path / name text, so
    unlike the other introspection returns it re-attaches nothing (no ids
    on the wire ⇒ no coverage obligation; `notes/boundary_discipline.md`).
    Nodes, containment, axes, and live status are **not** here: the dashboard
    builds those from the workflow summary (`GridSummary`, keyed by path,
    covering every grid), and joins this overlay for edges and cross-tree
    axis merges.

    Attributes:
        edges: The four aggregated relationship families.
        input_grants: The static rule-wiring grants.
        correspondences: The `Param` axis identities (grant-matching).
    """

    edges: tuple[GraphEdge, ...]
    input_grants: tuple[InputGrantEdge, ...]
    correspondences: tuple[AxisCorrespondence, ...]


@dataclass(frozen=True)
class TargetFinding:
    """One attached target whose materialized fact is not grounded — a
    red root of the lint report (see :meth:`UserCapabilityProto.lint`).

    Locations are absolute (root-frame transparency), so the finding is
    actionable without holding any view.

    Attributes:
        target: The target record (usable with ``remove_target`` /
            ``demand_target``).
        grid_path: The wanted grid's key-path text.
        coords: The wanted cell's flattened-coords text.
        verdict: The fact's verdict (task cells: the current
            generation's; data cells: the value's) — never ``grounded``.
        reason: The diagnosis chain's first failing conjunct.
    """

    target: TargetRecord
    grid_path: str
    coords: str
    verdict: GroundingVerdict
    reason: str | None


@dataclass(frozen=True)
class UngroundedFact:
    """One non-grounded fact in the lint inventory.

    The inventory is simply *every* non-grounded fact: once the report is
    sound, cone-transitivity of grounding makes these exactly the facts
    outside every demanded cone (the prune candidates); while a target is
    red, the same list also contains the causes of the redness (named by
    the red target's diagnosis chain).

    Attributes:
        kind: The graded fact kind (``notes/grounding.md``): a grid, a
            populated data/handle cell, or a task cell's current
            generation.
        grid_path: The fact's grid key-path text.
        coords: The cell's flattened-coords text; ``''`` for grids.
        verdict: ``orphaned`` (no claimant at the current config/value)
            or ``unanchored`` (claimed, but the chain does not derive).
        reason: The verdict's diagnosis.
    """

    kind: FactKind
    grid_path: str
    coords: str
    verdict: GroundingVerdict
    reason: str | None


@dataclass(frozen=True)
class LintReport:
    """The whole-workflow structural inventory, one session snapshot.

    The clean-state check of the intent axis (the linter section of
    ``notes/grounding.md``): findings mean "this experiment is not
    constructed properly" — grounding and dangling intent only, never
    staleness (freshness is currency's operational question, judged per
    cell by ``inspect_cell``). Two-level clean state:

    - :attr:`sound` — intent is coherent and every attached target's
      materialized fact is grounded (the structural half of the replay
      contract's hypothesis, checked at the target roots: both grounding
      and freshness are cone-transitive, so root checks *are* the cone
      check and the diagnosis chains itemize any break).
    - :attr:`clean` — sound, and the ungrounded inventory is empty.

    An attached target with nothing materialized yet is *pending intent*,
    deliberately not a finding (its remedy — demand it — is operational).
    """

    dangling_sourcing: tuple[SourcingInfo, ...]
    dangling_targets: tuple[TargetInfo, ...]
    target_findings: tuple[TargetFinding, ...]
    ungrounded: tuple[UngroundedFact, ...]

    @property
    def sound(self) -> bool:
        """No dangling intent and no red targets."""
        return not (self.dangling_sourcing or self.dangling_targets or self.target_findings)

    @property
    def clean(self) -> bool:
        """Sound, with an empty ungrounded inventory."""
        return self.sound and not self.ungrounded


@dataclass(frozen=True)
class DeleteReport:
    """What a ``delete_grid`` broke, reported as it happens.

    v0 proceeds-and-reports (a refuse-unless-``force`` variant is a
    deferred UX affordance); the referrers' grounding verdicts flipped in
    the deleting transaction, so follow-up queries show the damage.

    Attributes:
        broken_referrers: Key paths of task grids whose stored rules
            address the deleted grid (from the reverse rule-reference
            index, enumerated pre-delete).
    """

    broken_referrers: tuple[str, ...]


@service_protocol
class UserCapabilityProto(Protocol):
    """The top-level gateway: the run capability's calculus, minus the
    deterministic-world machinery, plus the privileged ops.

    Reads/writes/opens mirror :class:`~fxtr_experiment.runner_api.
    RunCapabilityProto` signatures (grants and selections are the same wire
    types) with user semantics: no record-check, no observations, no
    poisoning — errors are ordinary exceptions; writes and creates record
    user-sourcing. Reads follow the demand policy (they are materialization
    *events*).
    """

    async def root(self) -> ViewGrant:
        """The user's starting grant: a write-mode, zero-axis subtree grant
        on the root namespace.

        Returns:
            The root subtree grant.
        """
        ...

    # -- calculus ops (run-capability signatures, user semantics) ----------

    async def read_cell_entity(self, sel: ViewSel) -> FxtrID:
        """Read one entity cell; demands task outputs per the demand policy.

        Records nothing. Faults surface as ordinary exceptions. **User
        demands require targets**: a task-output read here requires an
        *attached* target on the cell (the method declares the intent;
        the current state — e.g. the cell being wanted through another
        task's records — never substitutes for a declaration the user
        didn't make; use :meth:`inspect_cell` to look at whatever exists).
        The `Workflow` demand wrapper auto-adds the target by default.

        Args:
            sel: Single-cell selection.

        Returns:
            The entity id at the cell.

        Raises:
            NotTargetedError: A task-output read with no attached target.
            CellFaultError: Missing / errored / interrupted / stale /
                unsettled subject.
        """
        ...

    async def read_cell_frame(self, sel: ViewSel) -> FrameWireType:
        """Read one dataframe cell (or task frame output). See
        :meth:`read_cell_entity` — the same gates apply (task-output reads
        are demands and require an attached target; dataframe-cell reads go
        through the grounding read gate). The payload is arrow IPC stream
        bytes in canonical row order, served from the projection.

        Args:
            sel: Single-cell selection (a dataframe grant, or a task grant
                with a declared ``FrameOutput`` selected).

        Returns:
            The cell's wire frame form.

        Raises:
            NotTargetedError: A task-output read with no attached target.
            CellFaultError: Missing / errored / interrupted / stale /
                unsettled subject.
            ViewAxisError: An entity-output selection.
        """
        ...

    async def read_handle_key(self, sel: ViewSel) -> FxtrID:
        """Read one handle cell's key.

        Args:
            sel: Single-cell selection on a handle grant.

        Returns:
            The handle entity id at the cell.
        """
        ...

    async def write_cell_entity(
        self, sel: ViewSel, value: FxtrID, *, overwrite: bool = False, adopt: bool = False
    ) -> None:
        """Write one entity cell (data or handle grants — handle grids are
        populated this way at the top level).

        Writing is sourcing: upserts the cell's user sourcing record
        (value-pinned, revocable) — *including* on idempotent same-value
        writes (re-writing a value claims it) — which requires the grid's
        own sourcing record first (the grid-before-cell invariant —
        automatic for grids the user created; adopt others, explicitly or
        via ``adopt=True``).

        Args:
            sel: Single-cell selection on a writable grant.
            value: The entity id to store.
            overwrite: Privileged replace: if a *different* value is present,
                replace it (direct observers of the old value go stale,
                their dependents unsettled — the machinery working as
                intended). Without it, a conflict raises.
            adopt: If the grid has no user sourcing record, add one
                (pinned to its current config) instead of refusing — the
                explicit-opt-in adoption shortcut beside ``overwrite``.

        Raises:
            WriteConflictError: Different value present and not ``overwrite``.
            GridNotSourcedError: The grid has no user sourcing record and
                not ``adopt`` (checked before the write — a refusal leaves
                no orphan value).
        """
        ...

    async def write_cell_frame(
        self, sel: ViewSel, frame: FrameWireType, *, overwrite: bool = False, adopt: bool = False
    ) -> None:
        """Write one dataframe cell. See :meth:`write_cell_entity` — the
        same sourcing semantics apply (writing is sourcing; the cell's
        record pins the canonical *frame entity id* as its fingerprint).
        The wire carries arrow IPC bytes; the capability validates them
        against the grid's declared schema and canonicalizes, exactly as
        the run capability does.

        Args:
            sel: Single-cell selection on a writable dataframe grant.
            frame: The wire frame form (the declared schema governs
                validation: names by set, exact dtypes, null legality).
            overwrite: Privileged replace of a different existing multiset.
            adopt: Source the grid first if it has no sourcing record.

        Raises:
            WriteConflictError: Different multiset present, no ``overwrite``.
            GridNotSourcedError: Grid unsourced and not ``adopt``.
        """
        ...

    async def gather(self, sel: ViewSel, keys: FrameWireType) -> FrameWireType:
        """The batched frame read, as an unrecorded interactive event.

        Wire dialect and stitched-response form exactly as the run
        capability's op (`runner_api.RunCapabilityProto.gather`); the
        per-cell serving follows the *user* read rules instead of the
        record: task-output cells require an attached target and are
        demands (:meth:`read_cell_entity`'s rule, applied per cell);
        dataframe cells go through the grounding read gate. Cells resolve
        concurrently; nothing is recorded.

        Raises:
            GatherFailedError: One or more addressed cells failed (a
                missing/ungrounded cell, an untargeted task cell, a failed
                run, ...) — the synthesized signal names every failed cell
                with its error class and message. (Deliberately not a
                ``CellFaultError``, whatever the causes' classes — see the
                type's contract.)
            ViewAxisError: Not a frame target, or a malformed key table.
        """
        ...

    async def open_data(self, subtree: ViewSel, key: str, axes: tuple[AxisSpec, ...]) -> ViewGrant:
        """Open (create-or-attach) a data grid; creates are user-sourced.

        Opens carry no mode — the grant follows the chain's authority
        (write under the user's root; read-only under internals roots and
        sliced windows). Same shape and config-matching semantics as the
        run capability's open (idempotent on canonical config; mismatch
        raises).
        """
        ...

    async def open_dataframe(
        self,
        subtree: ViewSel,
        key: str,
        axes: tuple[AxisSpec, ...],
        schema: FrameSchema,
    ) -> ViewGrant:
        """Open a dataframe grid. See :meth:`open_data`."""
        ...

    async def open_handle(
        self, subtree: ViewSel, key: str, axes: tuple[AxisSpec, ...], create: bool = False
    ) -> ViewGrant:
        """Open a handle grid (cells: service-handle entities).

        Creation and writes are top-level-only, by doctrine: tasks never
        mint authority — no handle-grid creation (even via subtree write
        access) and no handle-cell writes; tasks may only *attach* read-only
        handle views. Task-generated "services" are config *data* that
        parameterizes user-granted handles at connect time (see
        ``notes/design_overview.md``, authority and trust). Because
        creating a handle grid mints an authority surface, it is the one
        open that stays explicit: pass ``create=True`` (the wrapper's
        creation affordance) to create-if-missing; the default attaches
        only (a missing grid faults, like the task-side attach — and like
        the shared views' attach-only shape, supplied by the wrapper's
        view-backend adapter).
        """
        ...

    async def open_subtree(
        self, subtree: ViewSel, key: str, axes: tuple[AxisSpec, ...]
    ) -> ViewGrant:
        """Open a nested subtree grid. See :meth:`open_data`."""
        ...

    async def open_task(
        self,
        subtree: ViewSel,
        key: str,
        axes: tuple[AxisSpec, ...],
        task_fn: WiredArg,
        args: "Mapping[str, WiredArg | WiredCoord]",
        outputs: frozendict[str, OutputSpec],
    ) -> ViewGrant:
        """Open a task grid, canonicalizing the wiring (same as the run
        capability's ``open_task``); creates are user-sourced.
        """
        ...

    async def attach(self, subtree: ViewSel, key: str) -> ViewGrant:
        """Mint a grant for an *existing* child grid from its stored config.

        The browsing counterpart of the ``open_*`` calls: those are
        create-or-attach against a *proposed* canonical config (the shape
        task determinism needs — the caller asserts what the grid is);
        this one asserts nothing and trusts what is stored. The stored
        config carries everything a grant needs (kind, axes, schema,
        declared outputs — task grids included, no rule reconstruction),
        so after a session reopen the whole namespace is reachable from
        ``list_keys`` + ``attach`` alone. Records nothing, creates
        nothing. The grant follows the chain's authority (child authority
        never exceeds the grant's; task grants are read-shaped; internals
        are read-only from the top).

        Args:
            subtree: The subtree grant holding the key.
            key: The existing child key.

        Returns:
            The child grant, exactly as the corresponding open would have
            granted it.

        Raises:
            CellFaultError: No grid at ``key``.
            ViewAxisError: A checkpoint grid (task internals are not
                attachable from the top level; the check is
                defense-in-depth — no top-level subtree grant reaches
                one structurally).
        """
        ...

    async def attach_internals(self, task: ViewSel) -> ViewGrant:
        """A read-only subtree grant on a task's internals.

        The UI/browsing drill-down: internals are the task's *private*
        state as a matter of task-code discipline, not a secret the system
        keeps from its owner (the subtree-transparency principle) — so the
        top level may look, but only look. Everything reached through the
        returned grant is read-mode (:meth:`attach` under an internals
        root forces it: no writes, no create rights on nested subtrees),
        and checkpoint children are listable via :meth:`list_keys` but not
        attachable (browsing recorded step *entries* is a separate future
        affordance). Records nothing; note that *reading* an inner task's
        output through an attached view is an ordinary demand — pure
        browsing should use :meth:`list_cells` / :meth:`inspect_cell`.

        A fully bound selection browses one cell's key-space; open
        positions stay **kept** on the grant — a read-sliced internals
        window spanning the selected cells (the grid-level browse: since
        internals grids store their cells under the task's flattened
        axes, an all-open selection sees every cell's internals at
        once). Read-mode subtree views are sliceable by design, so both
        forms are ordinary browsing windows.

        Args:
            task: A task selection (positions may be open).

        Returns:
            The internals grant (a subtree view rooted at the task's
            key-space, with the selection's open positions kept).

        Raises:
            ViewAxisError: Not a task selection.
        """
        ...

    # -- sourcing (the editable root facts) -----------------------------------

    async def source_grid(self, subtree: ViewSel, key: str) -> None:
        """Upsert the grid-level sourcing record, pinned to the grid's
        *current* stored config — **adoption**: assert that an existing
        grid (task-created included) is directly user-intended, unlocking
        cell-level intent inside it (targets, writes). User creates record
        this automatically; there is no proposed-config variant (proposing
        shapes is what the ``open_*`` calls are for).

        Args:
            subtree: The subtree grant holding the key.
            key: The existing child grid's key.

        Raises:
            CellFaultError: No grid at ``key``.
            ViewAxisError: The grid lies in task internals (a task's
                private state is never user-ownable: adopting it would
                have a replay install internals at phase 0 and would let
                targets be planted inside internals).
        """
        ...

    async def source_cell(self, sel: ViewSel) -> None:
        """Upsert the cell-level sourcing record, pinned to the cell's
        *current* value — adopting a task-written value as user data (its
        grounding then no longer depends on the writer). User writes
        record this automatically.

        Args:
            sel: Single-cell selection on a populated data/handle cell.

        Raises:
            CellFaultError: Empty cell.
            GridNotSourcedError: The grid has no sourcing record (the
                grid-before-cell invariant — adopt the grid first).
        """
        ...

    async def revoke_grid_sourcing(
        self, subtree: ViewSel, key: str, *, force: bool = False
    ) -> bool:
        """Remove the grid-level sourcing record.

        The grid-before-cell invariant is *maintained*: with live
        cell-level intent (cell sourcing records, targets) the call
        refuses unless ``force`` sweeps them in the same transaction.
        Revocation touches records, never data — values stay, becoming
        orphaned/unanchored as the verdicts re-derive.

        Args:
            subtree: The subtree grant holding the key.
            key: The child grid's key (the record may be dangling — a
                missing grid's record is still revocable).
            force: Also delete the grid's cell sourcing records and
                targets (the cascade primitive).

        Returns:
            Whether a record existed.

        Raises:
            StateDbError: Live cell-level intent and not ``force``.
        """
        ...

    async def revoke_cell_sourcing(self, sel: ViewSel) -> bool:
        """Remove one cell-level sourcing record (the value stays; its
        claim goes — the user-side mirror of clearing a writer).

        Args:
            sel: Single-cell selection.

        Returns:
            Whether a record existed.
        """
        ...

    async def list_sourcing(self) -> tuple[SourcingInfo, ...]:
        """All sourcing records with their attachment status (dangling
        records included — intent is never silently dropped).

        Returns:
            One :class:`SourcingInfo` per record.
        """
        ...

    # -- targets ------------------------------------------------------------

    async def add_target(self, sel: ViewSel, adopt: bool = False) -> None:
        """Durably mark a cell as a wanted DAG root (declarative, no side
        effects on data; idempotent on the identical record).

        ``adopt=True`` is the one-call recovery for targeting inside a
        grid the user has not sourced: it first upserts the grid's
        sourcing record (pinned to the current config — the same explicit
        per-call opt-in as the write's ``adopt``; internals still
        refused). Never the default: adoption is a grid-level ownership
        claim a cell-level act must not manufacture silently.

        The stored record pins the lookup key (grid path + expected
        canonical-config fingerprint) and the wanted cell; see the module
        docstring for dangling semantics. Targeting a task cell means
        wanting it computed (all outputs). There is **one record per slice
        identity**: re-targeting after the grid's config changed requires
        removing the stale record first (intent changes are explicit).

        Args:
            sel: The cell to target — **fully bound**. Open positions are
                rejected: running an open slice would mean enumerating its
                cells, which the open-map principle forbids
                (domain-cell-driven targets are a deferred followup).

        Raises:
            ViewAxisError: The selection leaves positions open.
            TargetConflictError: A record for this slice exists with a
                different expected config (remove it first).
            GridNotSourcedError: The grid has no user sourcing record and
                not ``adopt`` (targets are cell-level intent; the
                grid-before-cell invariant applies).
        """
        ...

    async def remove_target(self, sel: ViewSel) -> None:
        """Remove any target record for this slice identity (reversible,
        no data effects; idempotent).

        Matches on the slice alone, ignoring the pinned expected-config
        fingerprint — a dangling record (pinned before config drift) stays
        removable through the same call that created it.

        Args:
            sel: The previously targeted cell (fully bound).
        """
        ...

    async def list_targets(self) -> tuple[TargetInfo, ...]:
        """All target records with their current attachment status.

        Returns:
            One :class:`TargetInfo` per stored target.
        """
        ...

    async def demand_target(self, target: TargetRecord) -> "frozendict[str, FxtrID]":
        """Materialize one stored target now (an ephemeral event).

        The by-record counterpart of a view read — targets are absolute
        (root-frame) slices, so no granted token addresses them; this is
        the primitive ``run_targets`` loops over. Follows the demand
        policy exactly like any read.

        Args:
            target: The record to demand (as returned by
                :meth:`list_targets`). Must currently be *attached*: a
                dangling record (missing grid or config drift) faults —
                intent is never silently rebound.

        Returns:
            Task cells: the validated per-output results. Data/handle
            cells: the value under the single key ``""``.

        Raises:
            CellFaultError: Dangling target, or any demand-policy fault.
        """
        ...

    # -- privileged mutation -------------------------------------------------

    async def clear_cells(self, sel: ViewSel) -> None:
        """Drop the generation state of the addressed task cells.

        Deletes results, op records, checkpoints, and linkage for each
        addressed cell (the demand policy's "user explicitly clears" flow);
        the next demand executes a fresh generation. **Atomic**: if any
        addressed cell is currently running, the whole call refuses and
        clears nothing (no partially-applied states; re-issue once the run
        settles).

        Args:
            sel: Selection over a task grant (positions may be open — all
                addressed nonempty cells are cleared).
        """
        ...

    async def delete_cell(self, sel: ViewSel) -> bool:
        """Remove one populated data/handle cell's value (idempotent).

        Deletion is a user settle event, exactly like ``overwrite=``:
        observers of the deleted subject go stale (missing subject) and
        their dependents unsettled — the machinery working as intended.
        The cell's sourcing record, if any, becomes *dangling* (preserved
        and surfaced — intent is never silently dropped; revoke it
        separately). Deleting a handle cell does **not** release any live
        binding (bindings are ephemeral runner configuration keyed by the
        handle entity, not grid state). Task cells refuse —
        :meth:`clear_cells` is their counterpart.

        Args:
            sel: Single-cell selection on a data/dataframe/handle grant.

        Returns:
            Whether a value existed (``False`` = already empty, no-op).

        Raises:
            ViewAxisError: A task selection, or open positions.
        """
        ...

    async def delete_grid(
        self, subtree: ViewToken, key: str, *, recursive: bool = False
    ) -> DeleteReport:
        """Delete the child grid at ``key``.

        Removes the key's config and contents (cells, executions,
        checkpoint entries). ``recursive=False`` is rmdir-style: errors
        if any child grids exist underneath (delete leaves first, or pass
        ``recursive=True``). ``recursive=True`` deletes the grid **and
        every grid under it** — its whole subtree — which is the only way
        to delete a task grid that has run (a task's internals are
        read-only child grids, so they cannot be emptied from the top).
        Either way, errors if any generation in the deleted set is
        running; there are no partially-deleted states.

        This is the escape hatch for conflicts and breaking changes to
        task code: observers of deleted things become stale (missing
        subject); targets and sourcing records pointing at a deleted grid
        become dangling (preserved, surfaced as incoherent); and the
        returned report names the *external* task grids whose rules
        referenced a deleted path (their grounding flips in the deleting
        transaction — proceed-and-report; a refuse-unless-``force``
        variant is a deferred UX affordance).

        Args:
            subtree: The subtree grant holding the key.
            key: The child key to delete.
            recursive: Delete the grid's whole subtree (default: rmdir,
                refuse on child grids).

        Returns:
            The consequence report.
        """
        ...

    # -- registration ---------------------------------------------------------

    async def bind_handle(
        self,
        handle: FxtrID,
        connector: ServiceRef[UnknownService],
        deps: "Mapping[str, ViewSel]" = frozendict(),
        config: JSONValue = None,
    ) -> ServiceRef[BindingReleaseProto]:
        """Register a live connector for a handle entity (ephemeral state).

        Covers task fns too: a task-fn handle's connector produces the
        :class:`~fxtr_experiment.runner_api.WorkflowTaskFnProto` service.
        Returns a release capability (workstation ``register_service``
        pattern): hold it to keep the binding; dropping it auto-releases;
        ``release()`` unbinds gracefully and immediately. The binding is
        additionally bounded by the connector service's own lifetime — a
        revoked connector unbinds forcefully.

        Binding a handle that is *currently* bound raises (release the
        existing binding first); binding again after a release/drop is
        fine and is exactly the promised-stateless trust contract — the
        registrant asserts the new connector behaves equivalently, per
        the compatibility ratchet (``notes/sandboxed_bindings.md``).

        Args:
            handle: The handle entity id to bind.
            connector: The connector service (a ServiceRef — crosses RPC
                like any other). Must speak
                :class:`~fxtr_experiment.bindings.ConnectorProto`:
                each spawn calls ``connect(request)`` with the standard
                :class:`fxtr_sandbox_types.BindingConnectRequest`, whose
                ``key`` is the handle key being served (one connector
                may be bound to several handles and dispatch on the
                key's contents).
            deps: Dependency name → a fully-bound handle-cell selection,
                pinned now exactly like ``bind_sandbox``'s (same
                validation, same connect-time re-check and resolution —
                the two classes are uniform in what the bound
                implementation receives); the pins live on the in-memory
                entry and die with the release.
            config: Optional machine-local, non-identity JSON config —
                same field and rules as ``bind_sandbox``'s.

        Returns:
            The binding's release capability.

        Raises:
            SandboxDependencyError: A dependency cell is missing, empty,
                or not a handle cell.
        """
        ...

    async def bind_sandbox(
        self,
        handle: FxtrID,
        defn: SandboxDefn,
        deps: "Mapping[str, ViewSel]",
        config: JSONValue = None,
    ) -> None:
        """Register a durable sandboxed binding for a handle key.

        The sandboxed binding class (``notes/sandboxed_bindings.md``): a
        nix service :class:`~fxtr_experiment.model.SandboxDefn` + a pinned
        dependency map + optional JSON config, saved as durable state the
        runner owns (exported with the workflow — chunk 4). Unlike
        ``bind_handle`` there is no release capability and no connector to
        keep alive: the record outlives the registering connection, and
        removal is :meth:`remove_sandbox_binding`. No nix evaluation
        happens here (bind is cheap and repo-independent; the defn is
        resolved against the runner's configured repo at connect time).

        Each dependency is pinned to a **fully-bound handle cell**: the
        cell is read now (it must be a populated handle cell), and its
        current key id becomes the pin re-checked at every connect — a
        later mismatch faults the consuming run (the compatibility
        ratchet: rewiring a dependency cell is an identity-level change,
        surfaced loudly rather than silently absorbed). Deps are an
        unordered map keyed by a unique name.

        Binding a key that is *currently* bound in **either** class
        (ephemeral or sandboxed) raises — a key is bound in at most one
        class, whichever registration comes second refused.

        Args:
            handle: The handle key id to bind.
            defn: The nix flake reference triple.
            deps: Dependency name → a fully-bound handle-cell selection
                (a ``ViewSel`` on a held handle grant, resolved holder-
                side like ``add_target``). Empty for a binding with no
                dependencies.
            config: Optional machine-local, non-identity JSON config
                (concurrency limits, timeouts, base URLs, …). Plain JSON
                only — no entities or refs; identity-bearing arguments
                belong in the key entity, never here.

        Raises:
            HandleAlreadyBoundError: The key is already bound (either
                class).
            SandboxDependencyError: A dependency cell is missing, empty,
                or not a handle cell.
        """
        ...

    async def remove_sandbox_binding(self, handle: FxtrID) -> None:
        """Remove the sandboxed binding for a handle key (a plain state
        mutation — sandboxed bindings have no release capability).

        The binding's cached sandbox process, if any, is dropped
        **release-and-forget**: already-connected guests stay connected
        and the process exits in the background once the last one
        releases; nothing waits here.

        Args:
            handle: The bound handle key id.

        Raises:
            UnknownBindingError: No sandboxed binding for the key.
        """
        ...

    async def shutdown_sandbox(self, handle: FxtrID) -> None:
        """Tear down a sandboxed binding's cached process (the binding
        record stays; the next demand relaunches fresh).

        **Release-and-forget** (``notes/sandboxed_bindings.md``): the
        cache entry is dropped and teardown signalled, returning without
        waiting — already-connected guests stay connected, and the
        process exits in the background when the last connected run
        releases. A ``sandbox_faulted`` state is cleared the same way
        (back to launchable). A no-op when nothing is cached.

        Args:
            handle: The bound handle key id.

        Raises:
            UnknownBindingError: No sandboxed binding for the key.
        """
        ...

    async def watch_workflow(
        self, subscriber: ServiceRef["Subscriber[FxtrServiceData]"]
    ) -> ServiceRef[UnsubscribeHandle]:
        """Subscribe to the workflow-level watch: a live, incrementally-
        patched mirror of everything workflow-wide — the grid summary
        rows (keyed by path), lint, targets, sourcing, bindings.

        The watch surface (all three ``watch_*`` methods; design:
        ``notes/event_system.md``): each takes the ``subscriber`` ref as
        its sole positional arg, with any spec (``path`` / ``coords``)
        as **keyword-only** args. That shape is load-bearing — the
        workstation inbox idiom drives these via
        ``subscribe_to(capability, "watch_*", **spec)``, which forwards
        as ``method(subscriber, **spec)`` (see ``fxtr_workstation``'s
        ``WorkstationSessionEventInbox.subscribe_to``): the subscriber is
        the forwarded leading positional, and the spec rides as kwargs
        (order-independent, self-documenting). Each subscription is
        presence-class (records nothing; nothing downstream can depend on
        it) and delivers members of a typed event union
        (`watch_api.WorkflowWatchEvent` here) — **batches of keyed
        patches** the subscriber folds into an always-current mirror.
        The first batch after subscribing is the snapshot (the diff from
        the empty mirror); an empty batch is the settle marker after a
        ``recomputing`` event (emitted when a slow pass — cold summarize
        force-evaluation, lint — begins). Unchanged state emits nothing.

        Delivery contract: within a live subscription, deliveries are
        FIFO, lossless, and ordered (queue-mode
        `~fxtr2.services.subscription_util.SubscriberHub`); events
        reflect committed state only, and a mutation's effects arrive
        within one coalescing window of its commit (at-least-once
        recompute, diff-suppressed). Any delivery failure drops the
        whole subscription rather than skipping an event — a subscriber
        that observes the break (or suspects a wedged mirror)
        resubscribes for a fresh snapshot. Subscriptions are ephemeral:
        nothing durable, nothing replayed.

        Args:
            subscriber: The receive side (its ``on_event`` gets each
                event, wire-encoded from the family's union).

        Returns:
            The unsubscribe capability (hold it to keep the
            subscription; call ``unsubscribe()`` — or drop it — to end).
            Teardown contract: a live subscription cross-pins the two
            sides' scopes (the source side holds the subscriber's
            bridge; the subscriber side holds this handle), so a scope
            hosting both the subscriber mount and the handle must NOT
            be gracefully drained while the subscription is live — the
            drain parks on the cycle. End the subscription first:
            call ``unsubscribe()``, or hold the handle in a
            shorter-lived scope whose exit releases it (handle-drop
            tears the subscription down) before the subscriber's scope
            drains. (The same cycle is documented at
            ``fxtr_workstation/callback_impl.py``'s inbox parker.)
        """
        ...

    async def watch_cells(
        self, subscriber: ServiceRef["Subscriber[FxtrServiceData]"], *, path: str
    ) -> ServiceRef[UnsubscribeHandle]:
        """Subscribe to one grid's watch: its meta bundle (config,
        grounding, sourcing, **the minted grants** — the grid's own plus
        its parent's token, through the same walk `attach` uses, so
        mutation ``ViewSel``\\ s are built from the payload) and its
        value-free cell rows, keyed by coordinates.

        Path-addressed (absolute key-path text — the summary rows' and
        hints' vocabulary), so the watch survives subject *identity*
        changes: a deleted grid delivers a ``subject_gone`` tombstone
        (the watch stays live), and a re-creation at the same path
        resumes with a fresh snapshot batch carrying fresh grants.
        Events: `watch_api.CellsWatchEvent`. Delivery and teardown
        contracts: :meth:`watch_workflow`.

        Args:
            path: The watched grid's key-path text (need not exist yet —
                a missing grid is a tombstone, not an error).
            subscriber: The receive side.

        Returns:
            The unsubscribe capability (see :meth:`watch_workflow`).

        Raises:
            EncodingError: ``path`` is not key-path text.
        """
        ...

    async def watch_cell(
        self, subscriber: ServiceRef["Subscriber[FxtrServiceData]"], *, path: str, coords: str
    ) -> ServiceRef[UnsubscribeHandle]:
        """Subscribe to one cell's watch: the drawer bundle — the
        `CellReport` (content id with verdicts, the ``inspect_cell``
        shape), plus for task cells the generation report, the reverse
        observers, and the **live run-log stream** (event-native: the
        buffered entries arrive right after the first batch, then
        appends as the run produces them, with a reset marker per new
        run).

        One method for every cell kind — events
        (`watch_api.CellWatchEvent`) are kind-discriminated like
        `CellReport`, and cell *emptiness* is data (an empty report),
        never a tombstone; ``subject_gone`` fires only when the grid
        itself is gone (path addressing, as in :meth:`watch_cells`).
        Delivery and teardown contracts: :meth:`watch_workflow`.

        Args:
            path: The cell's grid key-path text.
            coords: The cell's flattened-coords text (canonical
                ``encode_coords`` form — ``CellInfo`` join-key
                vocabulary).
            subscriber: The receive side.

        Returns:
            The unsubscribe capability (see :meth:`watch_workflow`).

        Raises:
            EncodingError: ``path``/``coords`` are not canonical
                addressing text.
        """
        ...

    async def watch_graph(
        self, subscriber: ServiceRef["Subscriber[FxtrServiceData]"]
    ) -> ServiceRef[UnsubscribeHandle]:
        """Subscribe to the grid-graph watch: the plate view's relational
        overlay (`GridGraph` — edges + input grants + axis correspondences),
        recomputed and diffed like the other families
        (`notes/event_system.md`; design `notes/plate_view.md`).

        Workflow-wide, so — like :meth:`watch_workflow` — the ``subscriber``
        ref is the sole positional arg and there are no spec kwargs. The
        overlay's two halves dirty independently (structure on ``grids``,
        edges on ``cells``); nodes / axes / status are **not** delivered here
        (they ride :meth:`watch_workflow`'s summary). Delivery and teardown
        contracts: :meth:`watch_workflow`.

        Args:
            subscriber: The receive side (its ``on_event`` gets each
                `watch_api.GraphWatchEvent`, wire-encoded).

        Returns:
            The unsubscribe capability (see :meth:`watch_workflow`).
        """
        ...

    async def list_bindings(self) -> tuple[BindingInfo, ...]:
        """The bound handle keys of both classes (ephemeral + sandboxed).

        The introspection half of the binding surface: together with the
        handle grids' contents, this is the workflow's complete
        external-authority surface (``notes/design_overview.md``,
        authority and trust) — auditable without reading task code.
        Presence-class: records nothing; nothing downstream can depend on
        it.

        Returns:
            One :class:`BindingInfo` per bound key (key re-attached), in
            key id order, tagged with its :data:`BindingState` (ephemeral
            keys as ``external``; sandboxed keys carrying their
            :class:`SandboxBindingInfo` detail with per-dep pin status).
            ``cells`` locates the handle cells holding that key (one
            snapshot), giving each binding its grid-side context.
        """
        ...

    # -- inspection (value + verdict, one snapshot) ----------------------------

    async def inspect_cell(self, sel: ViewSel) -> CellReport:
        """A cell's current content together with its currency verdict.

        The sanctioned way to look at a stale or unsettled (but complete)
        cell's results: unlike a read, this never executes, awaits, or
        faults on currency — and unlike :meth:`list_cells`, it serves the
        cell's content, pinned to the same snapshot as the verdict, so the
        caller always knows exactly how much to trust what they got.
        Records nothing; nothing downstream can depend on it.

        Args:
            sel: Single-cell selection (any grant kind).

        Returns:
            The :class:`CellReport` snapshot; all fields ``None`` for an
            empty cell / a task cell with no generation.
        """
        ...

    async def inspect_generation(self, sel: ViewSel) -> GenerationReport:
        """One task cell's current generation, itemized — the debug view.

        Everything the generation durably owns, one snapshot: failure
        records, the op record with a per-edge current-match flag (the
        "which input moved" answer behind a stale verdict), the
        asked-but-unanswered requests (what an interrupted run is blocked
        on), and the recorded step entries — the sanctioned browse of a
        task's checkpoint history. Records nothing; nothing downstream
        can depend on it. Never executes.

        Args:
            sel: Single-cell selection on a task grant.

        Returns:
            The :class:`GenerationReport`; the empty form (``generation
            is None``) when the cell has no current generation.

        Raises:
            ViewAxisError: Not a task selection, or open positions.
        """
        ...

    async def list_observers(
        self, sel: ViewSel, output: str | None = None
    ) -> tuple[ObserverInfo, ...]:
        """The observer cells of one subject — the reverse edges.

        Which task cells' current generations observed this cell (read,
        wrote, or — for grid subjects via their cells — configured
        against it), each with its own status and currency from the same
        snapshot: the "what depends on this" view, and the impact
        preview for an overwrite/delete. ``output`` narrows a task-cell
        subject to one named output. Records nothing; nothing downstream
        can depend on it. Locations in the result are absolute
        (root-frame transparency).

        Args:
            sel: Single-cell selection (any grant kind).
            output: Optional task-output name to narrow the subject.

        Returns:
            One :class:`ObserverInfo` per observer cell.

        Raises:
            ViewAxisError: Open positions.
        """
        ...

    # -- presence queries (pure introspection) --------------------------------

    async def summarize(self) -> WorkflowSummary:
        """Every grid with rollup counts and verdicts, one session snapshot.

        The dashboard tree's one-call backing (``notes/monitoring_ui.md``):
        per grid — kind, axes, config, grounding, sourcing status, target
        counts, population, execution-status counts, and stale/unsettled
        tallies. Currency **force-evaluates**: unstamped task cells derive
        (and stamp) during the sweep, so a cold namespace pays one walk
        and repeats serve stamps. A privileged whole-namespace
        enumeration like :meth:`lint`, deliberately outside the open-map
        rule; records nothing, carries no values.

        Returns:
            The :class:`WorkflowSummary` snapshot.
        """
        ...

    async def grid_graph(self) -> GridGraph:
        """The plate view's relational overlay, one session snapshot
        (design: ``notes/plate_view.md``).

        The grid-level edge families, static input grants, and provable
        ``Param`` axis correspondences — everything the plate view needs
        *beyond* the summary's nodes/axes/status. Deliberately
        **entity-id-free** (paths / names / ints), so it re-attaches nothing.
        A privileged whole-namespace enumeration like :meth:`summarize` /
        :meth:`lint`; records nothing, carries no values, never executes.

        Returns:
            The :class:`GridGraph` snapshot.
        """
        ...

    async def list_keys(self, subtree: ViewToken) -> tuple[KeyInfo, ...]:
        """List the child keys of a subtree (subtree-level state, shared
        across its cells by regularity).

        Records nothing; nothing can depend on it.

        Args:
            subtree: The subtree grant to list.

        Returns:
            One :class:`KeyInfo` per existing key.
        """
        ...

    async def list_cells(self, sel: ViewSel) -> tuple[CellInfo, ...]:
        """List the nonempty cells within a selection, with status.

        Presence, not bounds: grids are unbounded, so this reports what
        exists now, keyed by the selection's open positions. Records
        nothing; nothing can depend on it. Does not demand or execute
        anything.

        Args:
            sel: Selection over any grant (positions may be open).

        Returns:
            One :class:`CellInfo` per nonempty addressed cell.
        """
        ...

    async def lint(self) -> LintReport:
        """The whole-workflow structural inventory, one session snapshot.

        Enumerates every fact and every intent record and reports the
        structural problems (see :class:`LintReport` for the semantics
        and the sound/clean levels). Grounding-only by design; records
        nothing; nothing downstream can depend on it (privileged
        presence-class, like the other introspection channels).

        Returns:
            The findings-only report (empty sections when clean).
        """
        ...

    async def export_state(self, directory: str) -> ExportReport:
        """Rebuild ``directory`` as the workflow's at-rest layout
        (``notes/at_rest_format.md``): the deterministic (git repo +
        entity store) projection — same state, identical bytes.

        The directory path is **server-side** (like the db paths at
        open) and **wholly export-owned**: ownership is proved before
        anything is touched (absent, empty, or every non-dot entry a
        known layout member — else `ExportLayoutError`, with nothing
        modified), the tree is wholly rebuilt, and known members the
        state no longer wants are removed. A crash mid-write leaves the
        ``EXPORT_IN_PROGRESS`` marker explaining the state (git is the
        recovery path). Records nothing in the workflow; the snapshot is
        one session lock hold.

        Returns:
            Counts per exported fact kind.

        Raises:
            NotAtRestError: A generation is running.
            ExportLayoutError: The directory is not provably
                export-owned (nothing was touched).
        """
        ...

    async def import_state(self, directory: str, force: bool = False) -> ImportReport:
        """**Destructively** set the current state to the layout at
        ``directory`` (a completed export of ``notes/at_rest_format.md``,
        server-side path, resolved against this workflow's own entity
        store — the shared-store model).

        Parsing and verification happen before anything is written: the
        layout must be well-formed (manifest version, no in-progress
        marker, canonical files), every referenced id must resolve in
        the store, and every checked-redundant column must agree with
        its decoded truth. The destructive half is one transaction:
        refuse while running; refuse a non-empty state db unless
        ``force`` (which wipes the fact and intent tables — the events
        log survives, and the import appends its own event; export
        first, git is the backup). Everything imports **conservatively
        unstamped** (verdicts re-derive on demand; ``lint`` is the
        what-did-I-just-load check); bindings are ephemeral and must be
        re-registered, exactly as after a session reopen. Outstanding
        view grants may dangle afterward (the ``delete_grid`` analogy —
        ops fault at use time; the root grant survives).

        Args:
            directory: The export directory to load (server-side path).
            force: Replace existing state instead of refusing.

        Returns:
            Counts per imported fact kind.

        Raises:
            ImportFormatError: Not a well-formed layout (named file/row;
                nothing written).
            MissingEntitiesError: Referenced ids absent from the store
                (all listed; nothing written).
            NotAtRestError: A generation is running.
            StateNotEmptyError: State present and ``force`` is False.
        """
        ...
